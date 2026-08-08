"""
Build self-contained comment inbox HTML from queue CSV + conversation JSON.

Usage:
  python build-comment-inbox.py
  python build-comment-inbox.py --queue path/to/daily-queue-v2.csv
  python build-comment-inbox.py --queue path/to/daily-queue-v2.csv --icp-queue path/to/daily-queue-icp.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from lang_filter import is_probably_english

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_DIR = PROJECT_ROOT / "storage" / "conversation-acquisition"
ICP_DIR = PROJECT_ROOT / "storage" / "icp-warm"  # optional second pipeline
CONVERSATIONS_DIR = OUT_DIR / "conversations"
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "comment-inbox.template.html"
INBOX_HTML = OUT_DIR / "inbox.html"

SCORE_KEYS = [
    "brand_alignment",
    "icp_audience_likelihood",
    "contribution_opportunity",
    "room_to_stand_out",
    "traction_urgency",
    "profile_visit_potential",
]


def find_latest_queue(explicit: Path | None) -> Path:
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(f"Queue not found: {explicit}")
        return explicit
    candidates = sorted(OUT_DIR.glob("daily-queue-v2-*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        candidates = sorted(OUT_DIR.glob("daily-queue-*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No queue CSV in {OUT_DIR}")
    return candidates[0]


def run_date_from_queue_path(queue_path: Path) -> str:
    for pattern in (r"daily-queue-v2-(\d{4}-\d{2}-\d{2})", r"daily-queue-icp-(\d{4}-\d{2}-\d{2})"):
        m = re.search(pattern, queue_path.name)
        if m:
            return m.group(1)
    return ""


def find_icp_queue(explicit: Path | None, run_date: str) -> Path | None:
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"ICP queue not found: {explicit}")
        return explicit
    if not ICP_DIR.exists():
        return None
    if run_date:
        dated = ICP_DIR / f"daily-queue-icp-{run_date}.csv"
        if dated.exists():
            return dated
    candidates = sorted(ICP_DIR.glob("daily-queue-icp-*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def load_conversation(post_id: str) -> dict:
    path = CONVERSATIONS_DIR / f"{post_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def inspector_label(notes: str, report: dict | None) -> str:
    if report:
        failing = report.get("still_failing") or []
        if failing:
            return "manual_review"
        repaired = report.get("repaired") or []
        if repaired:
            return f"repaired_{len(repaired)}"
        return "all_pass"
    if "manual_review" in (notes or ""):
        return "manual_review"
    if "all_pass" in (notes or ""):
        return "all_pass"
    if "repaired" in (notes or ""):
        return "repaired"
    return "unknown"


def topic_tags_from_row(row: dict, conv: dict) -> list[str]:
    intel = conv.get("intelligence") or {}
    tags = list(intel.get("topic_tags") or [])
    sq = (row.get("search_query") or "").strip()
    if sq and sq not in tags:
        tags.insert(0, sq)
    return tags


def merge_card(row: dict) -> dict:
    post_id = (row.get("post_id") or "").strip()
    conv = load_conversation(post_id)
    post = conv.get("post") or {}
    author = post.get("author") or {}
    intel = conv.get("intelligence") or {}
    thread = conv.get("thread") or {}
    report = conv.get("inspection_report")

    avatar = ""
    if isinstance(author.get("avatar"), dict):
        avatar = author["avatar"].get("url") or ""

    full_content = (post.get("content") or row.get("post_snippet") or "").strip()
    preview = full_content[:320] + ("…" if len(full_content) > 320 else "")

    scores = {}
    for k in SCORE_KEYS:
        v = row.get(k) or intel.get(k)
        if v is not None and str(v).strip() != "":
            try:
                scores[k] = float(v)
            except ValueError:
                pass

    variants = {
        label: (row.get(f"variant_{label.lower()}") or "").strip()
        for label in "ABCDE"
    }

    return {
        "post_id": post_id,
        "queue_id": row.get("queue_id") or "",
        "run_date": row.get("run_date") or "",
        "status": row.get("status") or "pending",
        "pipeline": "keyword",
        "source": row.get("source") or "post_search_v2",
        "search_query": row.get("search_query") or "",
        "topic_tags": topic_tags_from_row(row, conv),
        "priority_score": float(row.get("priority_score") or 0),
        "urgency_tier": row.get("urgency_tier") or "normal",
        "engagement_velocity": float(row.get("engagement_velocity") or 0),
        "engagement_likes": int(row.get("engagement_likes") or 0),
        "engagement_comments": int(row.get("engagement_comments") or 0),
        "author_name": author.get("name") or row.get("full_name") or "",
        "author_headline": author.get("info") or row.get("job_title") or "",
        "author_linkedin": author.get("linkedinUrl") or row.get("linkedin_url") or "",
        "author_avatar": avatar,
        "author_persona": row.get("author_persona") or intel.get("author_persona") or "",
        "post_url": row.get("post_url") or post.get("linkedinUrl") or "",
        "post_posted_at": row.get("post_posted_at") or (post.get("postedAt") or {}).get("date") or "",
        "post_preview": preview,
        "post_full": full_content,
        "is_english": is_probably_english(full_content),
        "why_selected": row.get("intelligence_rationale") or intel.get("one_line_rationale") or "",
        "conversation_type": row.get("conversation_type") or intel.get("conversation_type") or "",
        "recommended_comment_style": row.get("recommended_comment_style") or intel.get("recommended_comment_style") or "",
        "saturation_signals": intel.get("saturation_signals") or [],
        "scores": scores,
        "variants": variants,
        "thread_snippets": thread.get("thread_top_comment_snippets") or [],
        "thread_seo_expert_density": thread.get("thread_seo_expert_density"),
        "inspector_status": inspector_label(row.get("notes") or "", report),
        "comment_model": (row.get("comment_model") or conv.get("comment_model") or "").strip(),
        "notes": row.get("notes") or "",
        "engagement": conv.get("engagement") or {},
        "reply_chain": conv.get("reply_chain") or [],
    }


def merge_icp_card(row: dict) -> dict:
    post_id = (row.get("post_id") or "").strip()
    full_content = (row.get("post_snippet") or "").strip()
    preview = full_content[:320] + ("…" if len(full_content) > 320 else "")
    job_title = (row.get("job_title") or "").strip()
    company = (row.get("company_name") or "").strip()
    headline = job_title + (f" · {company}" if company else "")

    variants = {
        label: (row.get(f"variant_{label.lower()}") or "").strip()
        for label in "ABCDE"
    }

    return {
        "post_id": post_id,
        "queue_id": row.get("queue_id") or "",
        "run_date": row.get("run_date") or "",
        "status": row.get("status") or "pending",
        "pipeline": "icp_warm",
        "source": "icp_warm",
        "search_query": "icp_warm",
        "topic_tags": ["icp_warm"],
        "priority_score": 0,
        "urgency_tier": "normal",
        "engagement_velocity": 0,
        "engagement_likes": 0,
        "engagement_comments": 0,
        "author_name": row.get("full_name") or "",
        "author_headline": headline,
        "author_linkedin": row.get("linkedin_url") or "",
        "author_avatar": "",
        "author_persona": "",
        "post_url": row.get("post_url") or "",
        "post_posted_at": row.get("post_posted_at") or "",
        "post_preview": preview,
        "post_full": full_content,
        "is_english": is_probably_english(full_content),
        "why_selected": "ICP warm engagement — peer comment, not product-related.",
        "conversation_type": "",
        "recommended_comment_style": "",
        "saturation_signals": [],
        "scores": {},
        "variants": variants,
        "thread_snippets": [],
        "thread_seo_expert_density": None,
        "inspector_status": "unknown",
        "comment_model": (row.get("comment_model") or "").strip(),
        "notes": row.get("notes") or "",
        "engagement": {},
        "reply_chain": [],
    }


def build_day_cards(queue_path: Path, icp_queue_path: Path | None) -> dict:
    with queue_path.open(encoding="utf-8", errors="replace", newline="") as f:
        keyword_rows = list(csv.DictReader(f))

    keyword_cards = [merge_card(r) for r in keyword_rows if (r.get("post_id") or "").strip()]
    keyword_cards.sort(key=lambda c: c.get("priority_score", 0), reverse=True)

    icp_cards: list[dict] = []
    icp_queue_file = ""
    if icp_queue_path and icp_queue_path.exists():
        with icp_queue_path.open(encoding="utf-8", errors="replace", newline="") as f:
            icp_rows = list(csv.DictReader(f))
        icp_cards = [merge_icp_card(r) for r in icp_rows if (r.get("post_id") or "").strip()]
        icp_queue_file = icp_queue_path.name

    cards = keyword_cards + icp_cards
    run_date = run_date_from_queue_path(queue_path)
    if not run_date and cards:
        run_date = cards[0].get("run_date") or ""

    return {
        "run_date": run_date,
        "queue_file": queue_path.name,
        "icp_queue_file": icp_queue_file,
        "card_count": len(cards),
        "keyword_count": len(keyword_cards),
        "icp_count": len(icp_cards),
        "cards": cards,
    }


def discover_keyword_queues() -> list[Path]:
    queues = [p for p in OUT_DIR.glob("daily-queue-v2-*.csv") if "test" not in p.name.lower()]
    if not queues:
        queues = [p for p in OUT_DIR.glob("daily-queue-*.csv") if "test" not in p.name.lower()]
    return sorted(queues, key=lambda p: run_date_from_queue_path(p) or p.stem, reverse=True)


def build_inbox_data(queue_path: Path, icp_queue_path: Path | None = None) -> dict:
    day = build_day_cards(queue_path, icp_queue_path)
    return {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "default_date": day["run_date"],
        "day_count": 1,
        "days": [day],
        **day,
    }


def build_all_days_inbox_data() -> dict:
    days: list[dict] = []
    for queue_path in discover_keyword_queues():
        run_date = run_date_from_queue_path(queue_path)
        if not run_date:
            continue
        icp_path = ICP_DIR / f"daily-queue-icp-{run_date}.csv"
        icp_queue = icp_path if icp_path.exists() else None
        day = build_day_cards(queue_path, icp_queue)
        if day["cards"]:
            days.append(day)

    if not days:
        raise FileNotFoundError(f"No queue CSVs with cards in {OUT_DIR}")

    default_date = days[0]["run_date"]
    active = days[0]
    return {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "default_date": default_date,
        "day_count": len(days),
        "days": days,
        **active,
    }


def render_html(data: dict) -> str:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Template not found: {TEMPLATE_PATH}")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")
    if "__INBOX_DATA__" not in template:
        raise ValueError("Template missing __INBOX_DATA__ placeholder")
    return template.replace("__INBOX_DATA__", payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build comment inbox HTML")
    parser.add_argument("--queue", type=Path, default=None, help="Keyword queue CSV (single-day mode)")
    parser.add_argument("--icp-queue", type=Path, default=None, help="ICP warm queue CSV (optional)")
    parser.add_argument("--no-icp", action="store_true", help="Skip merging ICP warm queue")
    parser.add_argument(
        "--single-day",
        action="store_true",
        help="Only embed one day (requires --queue or uses latest queue)",
    )
    args = parser.parse_args()

    if args.queue or args.single_day:
        queue_path = find_latest_queue(args.queue)
        run_date = run_date_from_queue_path(queue_path)
        icp_queue_path = None if args.no_icp else find_icp_queue(args.icp_queue, run_date)
        data = build_inbox_data(queue_path, icp_queue_path)
    else:
        data = build_all_days_inbox_data()

    html = render_html(data)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_HTML.write_text(html, encoding="utf-8")
    total_cards = sum(d["card_count"] for d in data["days"])
    print(
        f"Inbox built: {data['day_count']} days, {total_cards} total cards "
        f"(default {data['default_date']}: {data['card_count']} cards, "
        f"{data['keyword_count']} keyword, {data['icp_count']} ICP) -> {INBOX_HTML}"
    )
    print(f"Open: {OUT_DIR / 'inbox.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
