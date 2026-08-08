"""
LinkedIn Conversation Acquisition Engine V2 — conversation intelligence pipeline.

Scout → filter → pre-rank → AI intelligence → composite rank → thread enrich → style-aware comments.
Also merges today's influencer-feed scrape (no extra Apify cost).

Usage:
  python run-conversation-acquisition.py
  python run-conversation-acquisition.py --max-posts 20 --no-inspect
  python run-conversation-acquisition.py --no-influencer-feed
  python run-conversation-acquisition.py --v1 --max-posts 10   # legacy keyword ranker
"""
from __future__ import annotations

from comment_inspector import (
    inspect_and_repair_variants,
    inspection_summary,
    sanitize_comment_text,
)
from comment_generator import (
    COMMENT_SYSTEM_PROMPT,
    extract_post_hashtags,
    extract_their_companies,
    generate_comments as generate_comments_simple,
    opening_hook as post_opening_hook,
)
from post_brief import apply_post_brief
from lang_filter import is_probably_english
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Windows Task Scheduler / cp1252 consoles choke on non-ASCII author names in print().
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


_PRINT_LOCK = threading.Lock()


def _safe_print(msg: str) -> None:
    try:
        with _PRINT_LOCK:
            print(msg, flush=True)
    except UnicodeEncodeError:
        with _PRINT_LOCK:
            print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


PROJECT_ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"
GUIDELINES_MD = PROMPTS_DIR / "conversation-acquisition-guidelines.md"
COMMENT_V2_MD = PROMPTS_DIR / "comment-writing-system-v2.md"
VOICE_CONTEXT_MD = PROMPTS_DIR / "comment-voice-context.md"
INTELLIGENCE_MD = PROMPTS_DIR / "conversation-intelligence-prompt.md"
OUT_DIR = PROJECT_ROOT / "storage" / "conversation-acquisition"
CONVERSATIONS_DIR = OUT_DIR / "conversations"
ENGAGEMENT_LOG = OUT_DIR / "engagement-log.csv"
RANKING_WEIGHTS_PATH = PROJECT_ROOT / "config" / "ranking-weights.json"
DEFAULT_INFLUENCER_FEED_DIR = PROJECT_ROOT / "data" / "influencer-feed"
INFLUENCER_FEED_PRE_RANK_BOOST = 4
APIFY_API_BASE = "https://api.apify.com/v2"
DEFAULT_ACTOR_ID = "buIWk2uOUzTmcLsuB"  # harvestapi/linkedin-post-search
POLL_SEC = 15
TIMEOUT_SECS = 3600
DEFAULT_MODEL = "deepseek-v4-flash"
COMMENT_MODEL_PRO = "deepseek-v4-flash"
PRO_COMMENT_COUNT = 0
DEFAULT_PARALLEL_WORKERS = 10
DEFAULT_THREAD_ENRICH_WORKERS = 5

DEFAULT_SEARCH_QUERIES = [
    "AI search SEO",
    "GEO SEO",
    "AEO SEO",
    "LLM SEO",
    "AI visibility",
    "AI content marketing",
    "zero click SEO",
    "content operations SEO",
]

TOPIC_KEYWORDS = re.compile(
    r"\b(ai search|geo|aeo|llm seo|ai seo|ai visibility|zero.?click|"
    r"citation|perplexity|chatgpt|content marketing|seo|schema|"
    r"information density|serp|organic|ranking|blog|content ops)\b",
    re.I,
)
HIRING_RE = re.compile(
    r"\b(#hiring|we're hiring|we are hiring|open to work|job opening|apply now|dm me for|i'm hiring)\b",
    re.I,
)
PROMO_RE = re.compile(
    r"\b(register now|sign up for|join our webinar|use my link|discount code|promo code|free trial)\b",
    re.I,
)
CTA_RE = re.compile(
    r"\b(link in comments|free tool|free audit|dm me|book a call|schedule a demo)\b",
    re.I,
)
HYPE_RE = re.compile(
    r"\b(game.?changer|revolutionary|mind.?blowing|you need to|don't miss|must read)\b",
    re.I,
)
BUYER_HEADLINE_RE = re.compile(
    r"\b(cmo|chief marketing|vp marketing|vp of marketing|head of marketing|head of content|"
    r"content lead|marketing director|founder|co-founder|ceo|saas)\b",
    re.I,
)
SEO_FREELANCER_RE = re.compile(
    r"\b(seo specialist|seo expert|digital marketing student|freelance seo|seo consultant|"
    r"helping businesses rank|local seo specialist)\b",
    re.I,
)
SEO_EXPERT_COMMENT_RE = re.compile(
    r"\b(seo|geo|aeo|agency|digital marketing|content marketing|semrush|ahrefs)\b",
    re.I,
)
BUYER_COMMENT_RE = re.compile(
    r"\b(cmo|founder|vp marketing|head of marketing|head of content|saas|marketing director)\b",
    re.I,
)
FOLLOWERS_ONLY_RE = re.compile(r"^\d[\d,]*\s+followers?$", re.I)

QUEUE_V2_COLUMNS = [
    "run_date", "queue_id", "status", "source", "search_query",
    "full_name", "job_title", "company_name", "linkedin_url", "linkedin_public_id",
    "post_id", "post_url", "post_posted_at", "post_snippet",
    "engagement_likes", "engagement_comments", "engagement_velocity",
    "priority_score", "zelitho_alignment", "icp_audience_likelihood",
    "contribution_opportunity", "room_to_stand_out", "traction_urgency",
    "profile_visit_potential", "conversation_type", "author_persona",
    "recommended_comment_style", "intelligence_rationale",
    "thread_seo_expert_density", "urgency_tier",
    "filter_passed", "filter_reason", "comment_model",
    "variant_a", "variant_b", "variant_c", "variant_d", "variant_e",
    "chosen_variant", "commented_at", "notes",
]

QUEUE_V1_COLUMNS = [
    "run_date", "queue_id", "status", "source", "search_query",
    "full_name", "job_title", "company_name", "linkedin_url", "linkedin_public_id",
    "post_id", "post_url", "post_posted_at", "post_snippet",
    "engagement_likes", "engagement_comments", "relevance_score",
    "filter_passed", "filter_reason",
    "variant_a", "variant_b", "variant_c", "variant_d", "variant_e",
    "chosen_variant", "commented_at", "notes",
]

INTELLIGENCE_SUFFIX = "\n\nReturn JSON only. No markdown."


def load_voice_context() -> str:
    if VOICE_CONTEXT_MD.exists():
        return VOICE_CONTEXT_MD.read_text(encoding="utf-8")
    return ""


def build_comment_system_prompt(_guidelines: str = "", _voice_context: str = "") -> str:
    """Legacy hook — generation uses COMMENT_SYSTEM_PROMPT from comment_generator."""
    return COMMENT_SYSTEM_PROMPT


def build_intelligence_system_prompt(intelligence: str, voice_context: str) -> str:
    parts = [intelligence.strip()]
    if voice_context.strip():
        # Intelligence pass only needs mental model + ICP alignment slice
        parts.append(
            "\n---\n## Scorer context: who will comment\n\n"
            "Manojaditya Nadar — Zelitho founder, systems architect, dogfoods content automation on zelitho.com. "
            "Score contribution_opportunity by whether he can add a real operational insight from running "
            "research→draft→publish pipelines and AI search visibility work — not generic SEO takes.\n"
        )
    parts.append(INTELLIGENCE_SUFFIX)
    return "\n".join(parts)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = PROJECT_ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        env.setdefault(k, v)
    return env


def apify_token(env: dict[str, str]) -> str:
    token = (
        env.get("APIFY_API_TOKEN")
        or env.get("APIFY_TOKEN")
        or ""
    )
    if not token:
        raise RuntimeError("Missing Apify token (set APIFY_API_TOKEN in .env)")
    return token


def normalize_linkedin(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    return url.split("?")[0].rstrip("/")


def linkedin_public_id(url: str) -> str:
    url = normalize_linkedin(url)
    m = re.search(r"/in/([^/]+)", url, re.I)
    return m.group(1).lower() if m else ""


def apify_request(method: str, path: str, token: str, body: dict | None = None, timeout: int = 120):
    url = f"{APIFY_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Apify HTTP {e.code}: {detail[:1200]}") from e


def actor_id(env: dict[str, str]) -> str:
    return (env.get("APIFY_LINKEDIN_POST_SEARCH_ACTOR_ID") or DEFAULT_ACTOR_ID).strip()


def run_apify_actor(actor_input: dict, env: dict[str, str]) -> tuple[list[dict], dict]:
    token = apify_token(env)
    aid = actor_id(env)
    qs = urllib.parse.urlencode({"timeout": TIMEOUT_SECS, "memory": 1024})
    path = f"/acts/{aid}/runs?{qs}"
    resp = apify_request("POST", path, token, actor_input)
    data = resp.get("data", resp)
    run_id = data["id"]
    dataset_id = data["defaultDatasetId"]
    deadline = time.time() + TIMEOUT_SECS + 120
    while time.time() < deadline:
        resp = apify_request("GET", f"/actor-runs/{run_id}", token)
        run_data = resp.get("data", resp)
        status = run_data.get("status", "")
        if status == "SUCCEEDED":
            apify_meta = {
                "run_id": run_id,
                "usage_total_usd": run_data.get("usageTotalUsd"),
                "dataset_id": dataset_id,
            }
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {status}: {run_data.get('statusMessage', '')}")
        time.sleep(POLL_SEC)
    else:
        raise RuntimeError("Apify run timed out")

    resp = apify_request("GET", f"/datasets/{dataset_id}/items?limit=5000", token, timeout=300)
    if isinstance(resp, list):
        return resp, apify_meta
    if isinstance(resp, dict):
        inner = resp.get("data", resp)
        return (inner if isinstance(inner, list) else []), apify_meta
    return [], apify_meta


def run_apify_post_search(
    search_queries: list[str], max_posts_per_query: int, posted_limit: str, sort_by: str, env: dict[str, str],
) -> tuple[list[dict], dict]:
    return run_apify_actor({
        "searchQueries": search_queries,
        "maxPosts": max_posts_per_query,
        "postedLimit": posted_limit,
        "sortBy": sort_by,
        "scrapeComments": False,
        "scrapeReactions": False,
        "postNestedComments": False,
        "postNestedReactions": False,
        "profileScraperMode": "short",
    }, env)


def run_apify_thread_enrich(post_urls: list[str], max_comments: int, env: dict[str, str]) -> tuple[list[dict], dict]:
    """Scrape comments for specific posts via search on post URLs."""
    if not post_urls:
        return [], {}
    return run_apify_actor({
        "searchQueries": ["SEO"],
        "authorUrls": post_urls[:10],
        "maxPosts": 1,
        "postedLimit": "week",
        "scrapeComments": True,
        "maxComments": max_comments,
        "postNestedComments": True,
        "scrapeReactions": False,
        "profileScraperMode": "short",
        "commentsProfileScraperMode": "short",
    }, env)


def scrape_threads_by_urls(post_items: list[dict], max_comments: int, env: dict[str, str]) -> dict[str, dict]:
    """Enrich top posts with comment thread data. Returns post_id -> thread enrichment."""
    enriched: dict[str, dict] = {}
    for item in post_items:
        post = item["post"]
        post_id = str(post.get("id") or "")
        post_url = post.get("linkedinUrl") or ""
        if not post_url:
            continue
        try:
            items, _ = run_apify_actor({
                "searchQueries": ["content"],
                "maxPosts": 0,
                "authorUrls": [post_url],
                "postedLimit": "month",
                "scrapeComments": True,
                "maxComments": max_comments,
                "postNestedComments": True,
                "scrapeReactions": False,
                "profileScraperMode": "short",
                "commentsProfileScraperMode": "short",
            }, env)
        except Exception as e:
            enriched[post_id] = {"error": str(e)}
            continue

        matched = None
        for it in items:
            if str(it.get("id") or "") == post_id or it.get("linkedinUrl") == post_url:
                matched = it
                break
        if not matched and items:
            matched = items[0]

        if matched:
            enriched[post_id] = analyze_thread(matched)
        time.sleep(1)
    return enriched


def analyze_thread(post: dict) -> dict:
    comments = post.get("comments") or []
    eng = post.get("engagement") or {}
    profiles: list[dict] = []
    snippets: list[str] = []
    seo_expert_count = 0
    buyer_count = 0
    has_question = False

    for c in comments[:15]:
        text = (c.get("commentary") or "").strip()
        if text:
            snippets.append(text[:200])
        if "?" in text:
            has_question = True
        actor = c.get("actor") or {}
        headline = (actor.get("position") or "").strip()
        profile = {
            "name": actor.get("name") or "",
            "headline": headline,
            "linkedin_url": normalize_linkedin(actor.get("linkedinUrl") or ""),
        }
        profiles.append(profile)
        if SEO_EXPERT_COMMENT_RE.search(headline):
            seo_expert_count += 1
        if BUYER_COMMENT_RE.search(headline):
            buyer_count += 1

    n = max(len(profiles), 1)
    density = round(seo_expert_count / n, 2)
    reply_candidates = [p for p in profiles if BUYER_COMMENT_RE.search(p.get("headline") or "")]

    return {
        "thread_comment_count": int(eng.get("comments") or len(comments)),
        "thread_top_comment_snippets": snippets[:5],
        "thread_seo_expert_density": density,
        "thread_buyer_density": round(buyer_count / n, 2),
        "thread_has_question": has_question,
        "thread_commenter_profiles": profiles,
        "reply_candidates": reply_candidates,
    }


def post_age_hours(post: dict) -> float | None:
    ts = post.get("postedAt") or {}
    if isinstance(ts, dict) and ts.get("timestamp"):
        return (time.time() * 1000 - ts["timestamp"]) / 3_600_000
    if isinstance(ts, dict) and ts.get("date"):
        try:
            dt = datetime.fromisoformat(ts["date"].replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except ValueError:
            return None
    return None


def engagement_velocity(post: dict) -> float:
    eng = post.get("engagement") or {}
    likes = int(eng.get("likes") or 0)
    comments = int(eng.get("comments") or 0)
    age = post_age_hours(post) or 1.0
    return round((likes + comments * 3) / max(age, 0.5), 2)


def extract_author(post: dict) -> dict:
    author = post.get("author") or {}
    url = normalize_linkedin(author.get("linkedinUrl") or "")
    return {
        "full_name": (author.get("name") or "").strip(),
        "job_title": (author.get("info") or "").strip(),
        "company_name": "",
        "linkedin_url": url,
        "linkedin_public_id": linkedin_public_id(url),
        "author_type": (author.get("type") or "profile"),
    }


def passes_engagement_floor(post: dict) -> tuple[bool, str]:
    if post.get("_source") == "influencer_feed":
        return True, "ok"
    eng = post.get("engagement") or {}
    likes = int(eng.get("likes") or 0)
    comments = int(eng.get("comments") or 0)
    traction = likes + comments * 3
    if likes >= 10:
        return True, "ok"
    if traction >= 15:
        return True, "ok"
    if comments >= 5 and likes >= 3:
        return True, "ok"
    return False, f"low_engagement(l={likes},c={comments})"


def hard_filter_post(post: dict, known_post_ids: set[str]) -> tuple[bool, str]:
    if (post.get("type") or "post") != "post":
        return False, "not_a_post"
    post_id = str(post.get("id") or "").strip()
    if not post_id:
        return False, "missing_post_id"
    if post_id in known_post_ids:
        return False, "already_in_log"
    content = (post.get("content") or "").strip()
    if not is_probably_english(content):
        return False, "not_english"
    if len(content) < 80:
        return False, "content_too_short"
    if len(content) > 3000:
        return False, "content_too_long"
    if HIRING_RE.search(content):
        return False, "hiring_post"
    if PROMO_RE.search(content):
        return False, "promo_post"
    if not TOPIC_KEYWORDS.search(content):
        return False, "off_topic"
    ok, reason = passes_engagement_floor(post)
    if not ok:
        return False, reason
    return True, "ok"


def pre_rank_score(post: dict, search_query: str) -> int:
    score = 0
    content = (post.get("content") or "")
    content_lower = content.lower()
    eng = post.get("engagement") or {}
    comments = int(eng.get("comments") or 0)
    age = post_age_hours(post)
    author = post.get("author") or {}
    headline = (author.get("info") or "").strip()

    if TOPIC_KEYWORDS.search(content):
        score += 3
    if search_query.lower() in content_lower:
        score += 2
    if age is not None and age <= 12:
        score += 2
    elif age is not None and age <= 24:
        score += 1
    if BUYER_HEADLINE_RE.search(headline):
        score += 4
    elif SEO_FREELANCER_RE.search(headline):
        score -= 3
    if FOLLOWERS_ONLY_RE.match(headline):
        score -= 2
    if CTA_RE.search(content):
        score -= 3
    if 1 <= comments <= 20:
        score += 1
    vel = engagement_velocity(post)
    if vel >= 2:
        score += 2
    elif vel >= 0.5:
        score += 1
    if HYPE_RE.search(content) and not re.search(r"\b(cms|schema|parser|workflow|pipeline)\b", content_lower):
        score -= 2
    if post.get("_source") == "influencer_feed":
        score += INFLUENCER_FEED_PRE_RANK_BOOST
    return score


def tag_search_query(post: dict, queries: list[str]) -> str:
    if post.get("_source") == "influencer_feed":
        return "influencer_feed"
    q_from_post = (post.get("query") or {}).get("search")
    if q_from_post:
        return q_from_post
    content_lower = (post.get("content") or "").lower()
    for q in queries:
        words = [w for w in q.lower().split() if len(w) > 2]
        if words and all(w in content_lower for w in words):
            return q
    return queries[0] if queries else ""


def load_ranking_weights() -> dict:
    defaults = {
        "zelitho_alignment": 0.25,
        "icp_audience_likelihood": 0.20,
        "contribution_opportunity": 0.20,
        "room_to_stand_out": 0.15,
        "traction_urgency": 0.10,
        "profile_visit_potential": 0.10,
        "min_score_to_queue": 35,
        "saturation_penalty_threshold": 0.6,
        "saturation_room_max": 40,
    }
    if RANKING_WEIGHTS_PATH.exists():
        data = json.loads(RANKING_WEIGHTS_PATH.read_text(encoding="utf-8"))
        defaults.update(data)
    return defaults


def composite_priority(intel: dict, weights: dict) -> float:
    total = 0.0
    for key in ("zelitho_alignment", "icp_audience_likelihood", "contribution_opportunity",
                "room_to_stand_out", "traction_urgency", "profile_visit_potential"):
        w = weights.get(key, 0)
        total += w * float(intel.get(key) or 0)
    return round(total, 1)


def urgency_tier(post: dict, intel: dict) -> str:
    age = post_age_hours(post)
    vel = engagement_velocity(post)
    traction = float(intel.get("traction_urgency") or 0)
    if age is not None and age <= 6 and (vel >= 1.5 or traction >= 70):
        return "immediate"
    if age is not None and age <= 18 and traction >= 50:
        return "today"
    return "normal"


def load_engagement_log() -> list[dict]:
    if not ENGAGEMENT_LOG.exists():
        return []
    with ENGAGEMENT_LOG.open(encoding="utf-8", errors="replace", newline="") as f:
        return list(csv.DictReader(f))


def known_post_ids_from_log(log: list[dict]) -> set[str]:
    return {(row.get("post_id") or "").strip() for row in log if (row.get("post_id") or "").strip()}


def dedupe_posts(posts: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for post in posts:
        pid = str(post.get("id") or "")
        if pid and pid not in seen:
            seen[pid] = post
    return list(seen.values())


def influencer_feed_date_candidates(run_date: str) -> list[str]:
    """Influencer feed filenames use IST calendar day; try UTC run_date first."""
    dates = [run_date]
    ist_today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    if ist_today not in dates:
        dates.append(ist_today)
    return dates


def normalize_influencer_feed_post(post: dict) -> dict:
    """Ensure influencer-feed posts match the shape expected by filters and extract_author."""
    normalized = dict(post)
    normalized.setdefault("type", "post")
    author = dict(normalized.get("author") or {})
    author.setdefault("type", "profile")
    if not author.get("info"):
        author["info"] = ""
    normalized["author"] = author
    normalized["_source"] = "influencer_feed"
    return normalized


def load_influencer_feed_posts(feed_dir: Path, run_date: str) -> tuple[list[dict], dict]:
    """
    Load today's influencer-feed scrape (LinkedIn Ghostwriter morning job).
    No Apify re-scrape — reads influencer-feed/raw/YYYY-MM-DD.json.
    """
    meta: dict = {"path": None, "date": None, "loaded": 0, "skipped": {}}
    if not feed_dir.exists():
        meta["error"] = f"feed_dir_missing:{feed_dir}"
        return [], meta

    for date_str in influencer_feed_date_candidates(run_date):
        path = feed_dir / f"{date_str}.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            meta["error"] = f"read_error:{e}"
            return [], meta

        meta["path"] = str(path)
        meta["date"] = date_str
        meta["scraped_at"] = payload.get("scrapedAt")
        meta["account_count"] = payload.get("accountCount")

        posts: list[dict] = []
        for raw in payload.get("posts") or []:
            if (raw.get("type") or "post") != "post":
                meta["skipped"]["not_a_post"] = meta["skipped"].get("not_a_post", 0) + 1
                continue
            if not (raw.get("content") or "").strip():
                meta["skipped"]["empty_content"] = meta["skipped"].get("empty_content", 0) + 1
                continue
            if not str(raw.get("id") or "").strip():
                meta["skipped"]["missing_id"] = meta["skipped"].get("missing_id", 0) + 1
                continue
            posts.append(normalize_influencer_feed_post(raw))

        meta["loaded"] = len(posts)
        return posts, meta

    meta["error"] = "no_feed_file_for_date"
    return [], meta


def dedupe_by_author(candidates: list[dict], score_key: str = "pre_rank_score") -> list[dict]:
    best: dict[str, dict] = {}
    for item in candidates:
        pub = item["author"].get("linkedin_public_id") or str(item["post"].get("id"))
        prev = best.get(pub)
        if not prev or item[score_key] > prev[score_key]:
            best[pub] = item
    return list(best.values())


def _deepseek_post(
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
) -> dict:
    url = "https://api.deepseek.com/chat/completions"
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0.15,
        "stream": False,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            if attempt == 3:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def deepseek_chat(api_key: str, model: str, system: str, user: str) -> dict:
    data = _deepseek_post(api_key, model, system, user)
    return json.loads(data["choices"][0]["message"]["content"])


CACHE_WARMUP_USER = '{"warmup":true}'
CACHE_WARMUP_SLEEP_SEC = 1.5


def warm_deepseek_cache(api_key: str, model: str, system: str, label: str = "") -> None:
    """One cheap call so the shared system prefix is on disk before a parallel batch."""
    tag = label or model
    print(f"Cache warmup: {tag} ...")
    try:
        data = _deepseek_post(
            api_key, model, system, CACHE_WARMUP_USER, max_tokens=8,
        )
        usage = data.get("usage") or {}
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        print(f"Cache warmup: {tag} ok (hit={hit}, miss={miss})")
        time.sleep(CACHE_WARMUP_SLEEP_SEC)
    except Exception as e:
        print(f"Cache warmup: {tag} failed ({e}) — continuing", file=sys.stderr)


def post_content(item: dict) -> str:
    return ((item.get("post") or {}).get("content") or "").strip()


def english_gate(item: dict) -> bool:
    return is_probably_english(post_content(item))


def calibrate_intelligence(intel: dict, post: dict) -> dict:
    """Fix common intelligence misroutes (listicles scored as educational, etc.)."""
    if not intel or intel.get("skip"):
        return intel
    content = post.get("content") or ""
    lower = content.lower()
    eng = post.get("engagement") or {}
    comment_count = int(eng.get("comments") or 0)
    signals = [str(s).lower() for s in (intel.get("saturation_signals") or [])]

    checklist_hits = len(re.findall(r"[☑✅☐]", content))
    checklist_hits += len(re.findall(r"^\s*[-•]\s", content, re.M))
    channel_keywords = sum(
        1
        for w in (
            "google search", "ai search", "social search", "voice search",
            "bing", "reddit", "youtube", "pinterest", "instagram", "tiktok",
        )
        if w in lower
    )
    is_listicle = (
        intel.get("conversation_type") == "hype_listicle"
        or checklist_hits >= 4
        or channel_keywords >= 4
        or any("listicle" in s or "generic geo" in s for s in signals)
    )
    if is_listicle:
        intel["conversation_type"] = "hype_listicle"
        intel["recommended_comment_style"] = "plain_contrarian"
        if comment_count > 150:
            intel["room_to_stand_out"] = min(int(intel.get("room_to_stand_out") or 50), 40)

    if intel.get("conversation_type") in ("opinion", "debate"):
        intel["recommended_comment_style"] = "calibrated_stance"

    if re.search(r"\b(searchable|free .{0,24}report|lnkd\.in/)\b", lower):
        intel["conversation_type"] = "agency_promo"
        intel["recommended_comment_style"] = "practical_catch"

    if re.search(
        r"\b(publisher|publishers|wsj|wall street journal|usa today|reuters|economist|"
        r"politico|alphabet|media company|news outlet)\b",
        lower,
    ):
        intel["conversation_type"] = "opinion"
        intel["recommended_comment_style"] = "calibrated_stance"
        intel["post_hook_type"] = "publisher_or_media_economics"

    return apply_post_brief(intel, post)


def run_intelligence_pass(
    api_key: str, model: str, prompt: str, voice_context: str, item: dict,
) -> dict:
    post = item["post"]
    author = item["author"]
    user = json.dumps({
        "author": {"name": author.get("full_name"), "headline": author.get("job_title"), "type": author.get("author_type")},
        "post": {
            "content": (post.get("content") or "").strip(),
            "url": post.get("linkedinUrl") or "",
            "posted_at": (post.get("postedAt") or {}).get("date") or "",
            "likes": (post.get("engagement") or {}).get("likes"),
            "comments": (post.get("engagement") or {}).get("comments"),
            "engagement_velocity": item.get("engagement_velocity"),
            "age_hours": post_age_hours(post),
        },
        "search_query": item.get("search_query"),
    }, indent=2)
    return deepseek_chat(api_key, model, build_intelligence_system_prompt(prompt, voice_context), user)


def process_intelligence_item(
    item: dict,
    *,
    api_key: str,
    model: str,
    intelligence_prompt: str,
    voice_context: str,
    weights: dict,
) -> str:
    """Run intelligence for one pool item. Returns stats bucket: skipped, passed, error."""
    if not english_gate(item):
        item["intelligence"] = {"skip": True, "skip_reason": "not_english"}
        item["priority_score"] = 0
        return "skipped"
    try:
        intel = run_intelligence_pass(api_key, model, intelligence_prompt, voice_context, item)
        intel = calibrate_intelligence(intel, item["post"])
        item["intelligence"] = intel
        if intel.get("skip") or intel.get("recommended_comment_style") == "skip":
            item["priority_score"] = 0
            return "skipped"
        item["priority_score"] = composite_priority(intel, weights)
        item["urgency_tier"] = urgency_tier(item["post"], intel)
        return "passed"
    except Exception as e:
        item["intelligence"] = {"skip": True, "skip_reason": f"intel_error: {e}"}
        item["priority_score"] = 0
        return "error"


def enrich_thread_for_item(
    item: dict,
    *,
    max_comments: int,
    env: dict,
    weights: dict,
) -> None:
    post_id = str(item["post"].get("id") or "")
    target_url = item["post"].get("linkedinUrl") or ""
    author_url = item["author"].get("linkedin_url") or ""
    if not author_url or "/company/" in author_url:
        item["thread"] = {"skipped": "company_or_missing_author"}
        return
    try:
        items, _ = run_apify_actor({
            "searchQueries": ["AI SEO content"],
            "authorUrls": [author_url],
            "maxPosts": 5,
            "postedLimit": "week",
            "sortBy": "date",
            "scrapeComments": True,
            "maxComments": max_comments,
            "postNestedComments": True,
            "scrapeReactions": False,
            "profileScraperMode": "short",
            "commentsProfileScraperMode": "short",
        }, env)
        matched = next(
            (p for p in items if str(p.get("id")) == post_id or p.get("linkedinUrl") == target_url),
            None,
        )
        if matched:
            item["thread"] = analyze_thread(matched)
            density = item["thread"].get("thread_seo_expert_density", 0)
            room = float((item.get("intelligence") or {}).get("room_to_stand_out") or 0)
            if density > weights.get("saturation_penalty_threshold", 0.6) and room < weights.get("saturation_room_max", 40):
                item["priority_score"] = max(0, item.get("priority_score", 0) - 15)
                item["saturation_penalized"] = True
        else:
            item["thread"] = {"skipped": "post_not_in_author_feed"}
    except Exception as e:
        item["thread"] = {"error": str(e)}


def process_comment_item(
    item: dict,
    *,
    run_date: str,
    run_ts: str,
    api_key: str,
    comment_model: str,
    guidelines: str,
    voice_context: str,
    inspect: bool,
    scrape_only: bool,
) -> dict:
    row = build_queue_row_v2(item, run_date)
    post_id = row["post_id"]

    if scrape_only:
        row["notes"] = "scrape_only"
        save_conversation_json(post_id, {
            "post": item["post"], "intelligence": item.get("intelligence"),
            "thread": item.get("thread"), "stages": {"scout_at": run_ts},
        })
        return row

    if not api_key:
        return row

    if not english_gate(item):
        row["status"] = "skipped"
        row["filter_passed"] = "no"
        row["filter_reason"] = "not_english"
        row["notes"] = "not_english"
        save_conversation_json(post_id, {
            "post": item["post"], "intelligence": item.get("intelligence"),
            "thread": item.get("thread"), "stages": {"scout_at": run_ts},
        })
        return row

    row["comment_model"] = comment_model
    parsed: dict = {}
    try:
        parsed = generate_and_inspect_comments(
            api_key, comment_model, guidelines, voice_context, item, inspect=inspect
        )
        if not parsed.get("commentable", True):
            row["status"] = "skipped"
            row["filter_passed"] = "no"
            row["filter_reason"] = parsed.get("skip_reason") or "ai_skip"
        else:
            row.update(variant_map(parsed))
            insp = parsed.get("inspection_report") or {}
            dropped = drop_hook_drift_variants(row, insp)
            row["notes"] = inspection_summary(insp)
            if dropped:
                row["notes"] += f";hook_drift_dropped={dropped}"
            if insp.get("still_failing"):
                row["notes"] += ";manual_review"
            if not any(row.get(f"variant_{c}") for c in "abcde"):
                row["status"] = "blocked"
                row["notes"] += ";all_variants_failed_relevance"
    except Exception as e:
        row["status"] = "error"
        row["notes"] = f"comment_error={e}"

    save_conversation_json(post_id, {
        "post": item["post"],
        "intelligence": item.get("intelligence"),
        "thread": item.get("thread"),
        "variants": {k: row.get(k) for k in ("variant_a", "variant_b", "variant_c", "variant_d", "variant_e")},
        "comment_model": comment_model,
        "inspection_report": parsed.get("inspection_report"),
        "priority_score": item.get("priority_score"),
        "urgency_tier": item.get("urgency_tier"),
        "monitor_until": row.pop("_monitor_until", ""),
        "reply_candidates": row.pop("_reply_candidates", []),
        "thread_commenter_profiles": row.pop("_thread_commenter_profiles", []),
        "stages": {"scout_at": run_ts, "intelligence_at": run_ts, "comment_at": run_ts},
    })
    return row


def generate_and_inspect_comments(
    api_key: str,
    model: str,
    guidelines: str,
    voice_context: str,
    item: dict,
    *,
    inspect: bool = True,
) -> dict:
    parsed = generate_comments_v2(api_key, model, guidelines, voice_context, item)
    if not inspect or not parsed.get("commentable", True):
        return parsed
    post = item["post"]
    author = item.get("author") or {}
    thread = item.get("thread") or {}
    post_text = (post.get("content") or "").strip()
    article_title = ((post.get("article") or {}).get("title") or "").strip()
    context = {
        "post_content": post_text,
        "opening_hook": post_opening_hook(post_text),
        "article_title": article_title,
        "author_companies": extract_their_companies(author, post),
        "post_hashtags": extract_post_hashtags(post_text),
        "saturation_snippets": thread.get("thread_top_comment_snippets") or [],
    }
    return inspect_and_repair_variants(
        api_key, model, parsed, context, repair_fn=deepseek_chat
    )


def generate_comments_v2(
    api_key: str, model: str, _guidelines: str, _voice_context: str, item: dict,
) -> dict:
    return generate_comments_simple(api_key, model, item, chat_fn=deepseek_chat)


def variant_map(parsed: dict) -> dict[str, str]:
    out = {f"variant_{l.lower()}": "" for l in "ABCDE"}
    for v in parsed.get("variants") or []:
        label = (v.get("label") or "").strip().upper()
        if label in "ABCDE":
            out[f"variant_{label.lower()}"] = sanitize_comment_text((v.get("comment") or "").strip())
    return out


_IDENTITY_FAILURES = frozenset({
    "claims_author_employer",
    "recycles_article_title",
    "copies_post_hashtags",
    "echoes_post_opening",
})


def drop_identity_fail_variants(variants: dict[str, str], inspection_report: dict) -> int:
    """Blank variants that fail identity checks after repair."""
    dropped = 0
    for fail in inspection_report.get("still_failing") or []:
        label = (fail.get("label") or "").strip().upper()
        hard = fail.get("failures") or []
        if not any(f in _IDENTITY_FAILURES or f.split(":")[0] in _IDENTITY_FAILURES for f in hard):
            continue
        key = f"variant_{label.lower()}"
        if variants.get(key):
            variants[key] = ""
            dropped += 1
    return dropped


# Back-compat alias
drop_hook_drift_variants = drop_identity_fail_variants


def save_conversation_json(post_id: str, record: dict) -> Path:
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = CONVERSATIONS_DIR / f"{post_id}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def append_engagement_log(rows: list[dict], columns: list[str]) -> None:
    ENGAGEMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    write_header = not ENGAGEMENT_LOG.exists()
    with ENGAGEMENT_LOG.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if write_header:
            w.writeheader()
        for row in rows:
            w.writerow(row)


def write_queue(rows: list[dict], run_date: str, suffix: str, columns: list[str], v2: bool) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"-{suffix}" if suffix else ""
    prefix = "daily-queue-v2" if v2 else "daily-queue"
    path = OUT_DIR / f"{prefix}-{run_date}{tag}.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return path


def build_queue_row_v2(item: dict, run_date: str) -> dict:
    post = item["post"]
    author = item["author"]
    intel = item.get("intelligence") or {}
    post_id = str(post.get("id") or "")
    eng = post.get("engagement") or {}
    thread = item.get("thread") or {}
    posted_at = (post.get("postedAt") or {}).get("date") or ""
    monitor_until = ""
    if posted_at:
        try:
            dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
            monitor_until = (dt + timedelta(days=7)).isoformat()
        except ValueError:
            pass

    return {
        "run_date": run_date,
        "queue_id": f"{run_date}-{post_id}",
        "status": "pending",
        "source": (
            "influencer_feed" if post.get("_source") == "influencer_feed" else "post_search_v2"
        ),
        "search_query": item.get("search_query") or "",
        "full_name": author.get("full_name") or "",
        "job_title": author.get("job_title") or "",
        "company_name": "",
        "linkedin_url": author.get("linkedin_url") or "",
        "linkedin_public_id": author.get("linkedin_public_id") or "",
        "post_id": post_id,
        "post_url": post.get("linkedinUrl") or "",
        "post_posted_at": posted_at,
        "post_snippet": (post.get("content") or "")[:280],
        "engagement_likes": eng.get("likes") or 0,
        "engagement_comments": eng.get("comments") or 0,
        "engagement_velocity": item.get("engagement_velocity") or 0,
        "priority_score": item.get("priority_score") or 0,
        "zelitho_alignment": intel.get("zelitho_alignment") or 0,
        "icp_audience_likelihood": intel.get("icp_audience_likelihood") or 0,
        "contribution_opportunity": intel.get("contribution_opportunity") or 0,
        "room_to_stand_out": intel.get("room_to_stand_out") or 0,
        "traction_urgency": intel.get("traction_urgency") or 0,
        "profile_visit_potential": intel.get("profile_visit_potential") or 0,
        "conversation_type": intel.get("conversation_type") or "",
        "author_persona": intel.get("author_persona") or "",
        "recommended_comment_style": intel.get("recommended_comment_style") or "",
        "intelligence_rationale": intel.get("one_line_rationale") or "",
        "thread_seo_expert_density": thread.get("thread_seo_expert_density", ""),
        "urgency_tier": item.get("urgency_tier") or "normal",
        "filter_passed": "yes",
        "filter_reason": item.get("filter_reason") or "ok",
        "comment_model": "",
        "variant_a": "", "variant_b": "", "variant_c": "", "variant_d": "", "variant_e": "",
        "chosen_variant": "", "commented_at": "", "notes": "",
        "_monitor_until": monitor_until,
        "_thread_commenter_profiles": thread.get("thread_commenter_profiles", []),
        "_reply_candidates": thread.get("reply_candidates", []),
    }


def run_v2_pipeline(args: argparse.Namespace, env: dict[str, str]) -> int:
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_ts = datetime.now(timezone.utc).isoformat()
    search_queries = args.search_queries or DEFAULT_SEARCH_QUERIES
    weights = load_ranking_weights()
    model = args.model or env.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
    comment_model_flash = env.get("DEEPSEEK_COMMENT_MODEL") or model
    comment_model_pro = env.get("DEEPSEEK_COMMENT_MODEL_PRO") or COMMENT_MODEL_PRO
    pro_comment_count = int(getattr(args, "pro_comments", None) or PRO_COMMENT_COUNT)
    api_key = env.get("DEEPSEEK_API_KEY", "").strip()

    if not INTELLIGENCE_MD.exists():
        print(f"Intelligence prompt not found: {INTELLIGENCE_MD}", file=sys.stderr)
        return 1
    if not GUIDELINES_MD.exists():
        print(f"Guidelines not found: {GUIDELINES_MD}", file=sys.stderr)
        return 1
    if not VOICE_CONTEXT_MD.exists():
        print(f"Warning: voice context not found: {VOICE_CONTEXT_MD}", file=sys.stderr)

    workers = max(1, int(getattr(args, "parallel_workers", DEFAULT_PARALLEL_WORKERS) or DEFAULT_PARALLEL_WORKERS))
    thread_workers = max(1, int(getattr(args, "thread_enrich_workers", DEFAULT_THREAD_ENRICH_WORKERS) or DEFAULT_THREAD_ENRICH_WORKERS))

    aid = actor_id(env)
    print(
        f"V2 pipeline actor={aid} queries={len(search_queries)} pool={args.intelligence_pool} "
        f"intel_model={model} comment_model={comment_model_flash} "
        f"parallel_workers={workers} thread_workers={thread_workers}"
    )

    raw_path = OUT_DIR / f"scrape-raw-{run_date}.json"
    if getattr(args, "reuse_scrape", False) and raw_path.exists():
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        scout_posts = raw.get("scout") or []
        influencer_posts = raw.get("influencer_feed") or []
        influencer_meta = raw.get("influencer_meta") or {}
        apify_meta = {"reused": True}
        print(
            f"Reuse scrape: {len(scout_posts)} scout + {len(influencer_posts)} influencer "
            f"from {raw_path.name} (skipped Apify scout)"
        )
    else:
        scout_posts, apify_meta = run_apify_post_search(
            search_queries, args.max_posts_per_query, args.posted_limit, args.sort_by, env
        )
        influencer_meta: dict = {"loaded": 0}
        influencer_posts: list[dict] = []
        if not args.no_influencer_feed:
            feed_dir = Path(args.influencer_feed_dir)
            influencer_posts, influencer_meta = load_influencer_feed_posts(feed_dir, run_date)
            if influencer_meta.get("loaded"):
                print(
                    f"Influencer feed: {influencer_meta['loaded']} posts from "
                    f"{influencer_meta.get('path', 'n/a')}"
                )
            elif influencer_meta.get("error"):
                print(f"Influencer feed: skipped ({influencer_meta['error']})")

    # Influencer posts first so dedupe keeps curated source when scout overlaps
    posts = influencer_posts + scout_posts
    if not (getattr(args, "reuse_scrape", False) and raw_path.exists()):
        raw_path.write_text(json.dumps({
            "scout": scout_posts,
            "influencer_feed": influencer_posts,
            "influencer_meta": influencer_meta,
            "merged_count": len(posts),
        }, indent=2), encoding="utf-8")
    print(f"Scout: {len(scout_posts)} raw + influencer {len(influencer_posts)} -> {len(posts)} merged -> {raw_path}")
    if apify_meta.get("usage_total_usd") is not None:
        print(f"Apify scout cost: ${apify_meta['usage_total_usd']:.5f}")

    known_ids = known_post_ids_from_log(load_engagement_log())
    posts = dedupe_posts(posts)
    filter_stats: dict[str, int] = {}
    candidates: list[dict] = []

    for post in posts:
        ok, reason = hard_filter_post(post, known_ids)
        filter_stats[reason] = filter_stats.get(reason, 0) + 1
        if not ok:
            continue
        author = extract_author(post)
        sq = tag_search_query(post, search_queries)
        vel = engagement_velocity(post)
        candidates.append({
            "post": post, "author": author, "search_query": sq,
            "pre_rank_score": pre_rank_score(post, sq),
            "engagement_velocity": vel, "filter_reason": reason,
        })

    candidates = dedupe_by_author(candidates, "pre_rank_score")
    candidates.sort(key=lambda x: x["pre_rank_score"], reverse=True)
    pool = candidates[: args.intelligence_pool]
    non_en = filter_stats.get("not_english", 0)
    print(f"Filter: {len(candidates)} passed, {len(pool)} in intelligence pool (stats: {filter_stats})")
    if non_en:
        print(f"Language gate: {non_en} non-English posts removed before any LLM calls")

    intelligence_prompt = INTELLIGENCE_MD.read_text(encoding="utf-8")
    guidelines = GUIDELINES_MD.read_text(encoding="utf-8")
    voice_context = load_voice_context()
    intel_stats: dict[str, int] = {"skipped": 0, "passed": 0, "error": 0}

    if not args.scrape_only and api_key:
        pool_total = len(pool)
        if not getattr(args, "no_cache_warmup", False):
            intel_system = build_intelligence_system_prompt(intelligence_prompt, voice_context)
            warm_deepseek_cache(api_key, model, intel_system, "intelligence")
        print(f"Intelligence: {pool_total} posts, {workers} parallel workers ...")
        done = 0

        def _intel_job(item: dict) -> tuple[str, str]:
            name = (item["author"].get("full_name") or "")[:35]
            status = process_intelligence_item(
                item,
                api_key=api_key,
                model=model,
                intelligence_prompt=intelligence_prompt,
                voice_context=voice_context,
                weights=weights,
            )
            return status, name

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_intel_job, item): item for item in pool}
            for fut in as_completed(futures):
                try:
                    status, name = fut.result()
                except Exception as e:
                    status, name = "error", "?"
                    item = futures[fut]
                    item["intelligence"] = {"skip": True, "skip_reason": f"intel_error: {e}"}
                    item["priority_score"] = 0
                intel_stats[status] = intel_stats.get(status, 0) + 1
                done += 1
                _safe_print(f"Intelligence [{done}/{pool_total}] {name} -> {status}")
    elif args.scrape_only:
        for item in pool:
            item["intelligence"] = {}
            item["priority_score"] = item["pre_rank_score"]
            item["urgency_tier"] = "normal"
    else:
        print("Missing DEEPSEEK_API_KEY — skipping intelligence pass", file=sys.stderr)
        for item in pool:
            item["priority_score"] = item["pre_rank_score"]
            item["urgency_tier"] = "normal"

    ranked = [c for c in pool if not (c.get("intelligence") or {}).get("skip")]
    ranked.sort(key=lambda x: x.get("priority_score", 0), reverse=True)

    if not args.no_thread_enrich and ranked:
        enrich_n = min(args.thread_enrich_top, len(ranked))
        enrich_items = ranked[:enrich_n]
        print(f"Thread enrich: top {enrich_n} posts, {thread_workers} parallel workers ...")
        done = 0

        with ThreadPoolExecutor(max_workers=thread_workers) as executor:
            futures = {
                executor.submit(
                    enrich_thread_for_item,
                    item,
                    max_comments=args.max_comments,
                    env=env,
                    weights=weights,
                ): item
                for item in enrich_items
            }
            for fut in as_completed(futures):
                item = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    item["thread"] = {"error": str(e)}
                done += 1
                name = (item["author"].get("full_name") or "")[:35]
                _safe_print(f"Thread enrich [{done}/{enrich_n}] {name}")

        ranked.sort(key=lambda x: x.get("priority_score", 0), reverse=True)

    min_score = weights.get("min_score_to_queue", 35)
    final = [c for c in ranked if c.get("priority_score", 0) >= min_score][: args.max_posts]
    print(f"Final queue: {len(final)} posts (min_score={min_score})")

    queue_rows: list[dict] = []
    final_total = len(final)
    if final_total:
        print(f"Comments: {final_total} posts, {workers} parallel workers ...")

    if args.scrape_only or not api_key:
        for item in final:
            row = process_comment_item(
                item,
                run_date=run_date,
                run_ts=run_ts,
                api_key=api_key,
                comment_model=comment_model_flash,
                guidelines=guidelines,
                voice_context=voice_context,
                inspect=not args.no_inspect,
                scrape_only=args.scrape_only,
            )
            queue_rows.append(row)
    else:
        comment_models = [
            comment_model_pro if i < pro_comment_count else comment_model_flash
            for i in range(final_total)
        ]
        if not getattr(args, "no_cache_warmup", False):
            comment_system = build_comment_system_prompt()
            for warm_model in dict.fromkeys(comment_models):
                warm_deepseek_cache(api_key, warm_model, comment_system, f"comments/{warm_model}")
        results: list[dict | None] = [None] * final_total
        done = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {
                executor.submit(
                    process_comment_item,
                    final[i],
                    run_date=run_date,
                    run_ts=run_ts,
                    api_key=api_key,
                    comment_model=comment_models[i],
                    guidelines=guidelines,
                    voice_context=voice_context,
                    inspect=not args.no_inspect,
                    scrape_only=False,
                ): i
                for i in range(final_total)
            }
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                item = final[idx]
                name = (item["author"].get("full_name") or "")[:35]
                try:
                    results[idx] = fut.result()
                except Exception as e:
                    row = build_queue_row_v2(item, run_date)
                    row["status"] = "error"
                    row["notes"] = f"comment_error={e}"
                    results[idx] = row
                done += 1
                model_used = comment_models[idx]
                _safe_print(
                    f"Comments [{done}/{final_total}] {name} "
                    f"(score={item.get('priority_score')}, model={model_used})"
                )
        queue_rows = [r for r in results if r is not None]

    for row in queue_rows:
        row.pop("_monitor_until", None)
        row.pop("_reply_candidates", None)
        row.pop("_thread_commenter_profiles", None)

    to_log = [r for r in queue_rows if r["status"] == "pending" and (args.scrape_only or r.get("variant_a"))]
    if to_log:
        append_engagement_log(to_log, QUEUE_V2_COLUMNS)

    queue_path = write_queue(queue_rows, run_date, args.output_suffix, QUEUE_V2_COLUMNS, v2=True)

    if not args.no_inbox:
        builder = Path(__file__).resolve().parent / "build-comment-inbox.py"
        try:
            subprocess.run(
                [sys.executable, str(builder), "--queue", str(queue_path)],
                check=True,
                cwd=str(builder.parent),
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"Warning: inbox build failed: {e}", file=sys.stderr)

    summary_path = OUT_DIR / f"run-summary-v2-{run_date}.json"
    summary_path.write_text(json.dumps({
        "run_at": run_ts, "engine": "conversation-acquisition-v2",
        "actor_id": actor_id(env), "search_queries": search_queries,
        "scout_posts_raw": len(scout_posts),
        "influencer_feed": influencer_meta,
        "scrape_posts_merged": len(posts),
        "filter_stats": filter_stats,
        "intelligence_stats": intel_stats, "pool_size": len(pool),
        "final_queue": len(final), "queue_csv": str(queue_path),
        "comment_models": {
            "pro": comment_model_pro,
            "flash": comment_model_flash,
            "pro_count": min(pro_comment_count, len(final)),
            "pro_limit": pro_comment_count,
        },
        "parallel_workers": workers,
        "thread_enrich_workers": thread_workers,
        "apify_scout": apify_meta, "ranking_weights": weights,
        "persona_breakdown": _persona_breakdown(final),
        "influencer_in_queue": sum(
            1 for item in final if (item.get("post") or {}).get("_source") == "influencer_feed"
        ),
    }, indent=2), encoding="utf-8")

    print("\nDone (V2).")
    print(f"  Queue: {queue_path}")
    print(f"  Inbox: double-click {OUT_DIR / 'Open Inbox.cmd'}")
    print(f"  Conversations: {CONVERSATIONS_DIR}")
    print(f"  Summary: {summary_path}")
    return 0


def _persona_breakdown(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        p = (item.get("intelligence") or {}).get("author_persona") or "unknown"
        counts[p] = counts.get(p, 0) + 1
    return counts


def run_v1_pipeline(args: argparse.Namespace, env: dict[str, str]) -> int:
    """Legacy V1 keyword ranker."""
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_ts = datetime.now(timezone.utc).isoformat()
    search_queries = args.search_queries or DEFAULT_SEARCH_QUERIES[:5]
    model = args.model or env.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
    api_key = env.get("DEEPSEEK_API_KEY", "").strip()
    guidelines = GUIDELINES_MD.read_text(encoding="utf-8")
    voice_context = load_voice_context()

    posts, apify_meta = run_apify_post_search(
        search_queries, args.max_posts_per_query, args.posted_limit, args.sort_by, env
    )
    known_ids = known_post_ids_from_log(load_engagement_log())
    posts = dedupe_posts(posts)
    candidates = []
    for post in posts:
        ok, reason = hard_filter_post(post, known_ids)
        if not ok:
            continue
        author = extract_author(post)
        sq = tag_search_query(post, search_queries)
        candidates.append({
            "post": post, "author": author, "search_query": sq,
            "relevance_score": pre_rank_score(post, sq), "filter_reason": reason,
        })
    candidates = dedupe_by_author(candidates, "relevance_score")
    candidates.sort(key=lambda x: x["relevance_score"], reverse=True)
    candidates = candidates[: args.max_posts]

    queue_rows = []
    for i, item in enumerate(candidates):
        post, author = item["post"], item["author"]
        eng = post.get("engagement") or {}
        row = {
            "run_date": run_date, "queue_id": f"{run_date}-{post.get('id')}",
            "status": "pending", "source": "post_search",
            "search_query": item["search_query"],
            "full_name": author.get("full_name"), "job_title": author.get("job_title"),
            "company_name": "", "linkedin_url": author.get("linkedin_url"),
            "linkedin_public_id": author.get("linkedin_public_id"),
            "post_id": str(post.get("id")), "post_url": post.get("linkedinUrl"),
            "post_posted_at": (post.get("postedAt") or {}).get("date"),
            "post_snippet": (post.get("content") or "")[:280],
            "engagement_likes": eng.get("likes", 0), "engagement_comments": eng.get("comments", 0),
            "relevance_score": item["relevance_score"], "filter_passed": "yes",
            "filter_reason": item.get("filter_reason", "ok"), "variant_a": "", "variant_b": "", "variant_c": "",
            "variant_d": "", "variant_e": "", "chosen_variant": "", "commented_at": "", "notes": "",
        }
        if not args.scrape_only and api_key:
            if not english_gate(item):
                row["status"] = "skipped"
                row["filter_passed"] = "no"
                row["filter_reason"] = "not_english"
                row["notes"] = "not_english"
            else:
                parsed = generate_and_inspect_comments(
                    api_key, model, guidelines, voice_context,
                    {**item, "intelligence": {}, "thread": {}},
                    inspect=not args.no_inspect,
                )
                row.update(variant_map(parsed))
        queue_rows.append(row)

    to_log = [r for r in queue_rows if r["status"] == "pending" and (args.scrape_only or r.get("variant_a"))]
    if to_log:
        append_engagement_log(to_log, QUEUE_V1_COLUMNS)
    path = write_queue(queue_rows, run_date, args.output_suffix, QUEUE_V1_COLUMNS, v2=False)
    print(f"V1 done: {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LinkedIn Conversation Acquisition Engine")
    parser.add_argument("--max-posts", type=int, default=12,
                        help="Max posts in final queue (default 12)")
    parser.add_argument("--max-posts-per-query", type=int, default=15,
                        help="Apify scout: max posts per search query (~8 queries -> ~120 scraped)")
    parser.add_argument("--intelligence-pool", type=int, default=65,
                        help="Top N candidates for AI intelligence pass")
    parser.add_argument("--thread-enrich-top", type=int, default=25,
                        help="Top N to scrape comments for saturation")
    parser.add_argument("--max-comments", type=int, default=15)
    parser.add_argument("--no-thread-enrich", action="store_true")
    parser.add_argument("--posted-limit", default="24h", choices=["1h", "24h", "week", "month"])
    parser.add_argument("--sort-by", default="date", choices=["date", "relevance"])
    parser.add_argument("--search-queries", nargs="*", default=None)
    parser.add_argument("--influencer-feed-dir", default=str(DEFAULT_INFLUENCER_FEED_DIR),
                        help="Directory with influencer-feed/raw/YYYY-MM-DD.json from morning scrape")
    parser.add_argument("--no-influencer-feed", action="store_true",
                        help="Skip merging today's influencer-feed scrape")
    parser.add_argument("--scrape-only", action="store_true")
    parser.add_argument("--model", default=None,
                        help="Intelligence model (default deepseek-v4-flash via DEEPSEEK_MODEL)")
    parser.add_argument("--pro-comments", type=int, default=PRO_COMMENT_COUNT,
                        help="Top N comment cards to generate with pro model (0 = all flash; default 0)")
    parser.add_argument("--output-suffix", default="")
    parser.add_argument("--no-inspect", action="store_true", help="Skip comment lint + repair loop")
    parser.add_argument("--parallel-workers", type=int, default=DEFAULT_PARALLEL_WORKERS,
                        help="Parallel DeepSeek workers for intelligence + comments (default 10)")
    parser.add_argument("--thread-enrich-workers", type=int, default=DEFAULT_THREAD_ENRICH_WORKERS,
                        help="Parallel Apify workers for thread enrich (default 5)")
    parser.add_argument("--no-cache-warmup", action="store_true",
                        help="Skip one-call DeepSeek cache warmup before intelligence/comment batches")
    parser.add_argument("--reuse-scrape", action="store_true",
                        help="Reuse today's scrape-raw-*.json instead of running Apify scout")
    parser.add_argument("--v1", action="store_true", help="Use legacy V1 keyword ranker")
    parser.add_argument("--no-inbox", action="store_true",
                        help="Skip inbox HTML build (use when unified daily trigger builds inbox)")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    env = load_env()
    if args.v1:
        return run_v1_pipeline(args, env)
    return run_v2_pipeline(args, env)


if __name__ == "__main__":
    raise SystemExit(main())
