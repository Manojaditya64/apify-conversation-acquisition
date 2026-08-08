"""
Deterministic post brief — routes comment generation to the post's actual topic.

No extra LLM calls. Used by intelligence calibration and comment generation.
"""
from __future__ import annotations

import re

from comment_generator import extract_their_companies

EVENT_PHRASES = (
    "webinar", "register now", "register here", "save your spot", "rsvp",
    "summit", "conference", "booth", "join us on", "join us for", "live event",
    "save the date", "link in comments", "sign up", "free event",
)
PRODUCT_SIGNALS = (
    r"\b(introducing|announcing|now available|just launched|we built|we're building)\b",
    r"\b(try it|check out our|our (?:new )?tool|product launch)\b",
    r"\b(fable|searchable|notebooklm|screaming frog|contentful|liferay|palmata)\b",
)
TOOL_TIP_RE = (
    r"\b(notebooklm|screaming frog|ollama|semrush|ahrefs|python script|chrome extension)\b|"
    r"\b(here(?:'s| is) how (?:i|we)|tip:|pro tip|workflow|i use .{0,30} for)\b"
)
RESEARCH_RE = (
    r"\b(research paper|arxiv|study found|survey found|benchmark|whitepaper|"
    r"google research|meta analysis|peer.?review)\b"
)
PUBLISHER_RE = (
    r"\b(publisher|publishers|wsj|wall street journal|usa today|alphabet|"
    r"news outlet|media company|traffic (?:drop|down|fell))\b"
)

FORBIDDEN_PIVOT_RULES: dict[str, list[str]] = {
    "tool_tip": [
        "Do NOT pivot to AI citation audits, share of voice, or Claude vs ChatGPT retrieval "
        "unless the post is explicitly about citations.",
    ],
    "product_launch": [
        "Do NOT default to citation measurement or GEO optimization — react to their product "
        "or use case first.",
    ],
    "research": [
        "Do NOT replace their research topic with a generic publishing-network or attribution story.",
    ],
    "publisher_economics": [
        "Do NOT use a generic 'competitor in every AI answer' anecdote — use their outlets, "
        "publishers, or traffic stats.",
    ],
    "event_promo": [
        "Skip unless you can name the event, host, or topic from the post — no generic AEO lecture.",
    ],
    "technical_tip": [
        "Do NOT ignore their technique — mirror it before adding insight. No citation-playbook pivot.",
    ],
}

COMMENT_MODE_INSTRUCTIONS: dict[str, str] = {
    "react_to_tool": (
        "React to their specific tool or workflow. Share a parallel experiment with THAT tool "
        "or technique — not a citation audit."
    ),
    "react_to_product": (
        "React to their product claim or use case list. Extend one use case or share where "
        "you hit a wall — not generic AI visibility."
    ),
    "react_to_research": (
        "React to their finding or paper by name. Add one operational implication tied to "
        "their research topic."
    ),
    "react_to_publisher": (
        "React to publisher/traffic economics using their outlets and stats. No interchangeable "
        "citation story."
    ),
    "react_to_tip": (
        "React to their concrete SEO/ops tip. Reference their method before adding yours."
    ),
    "react_to_opinion": (
        "Take a stance on their claim using their specific nouns — not a GEO monologue."
    ),
    "practical_catch": "One grounded catch on their framework — ignore promo/CTA.",
    "plain_contrarian": "Name ONE list item or one contrarian line — no recap.",
    "calibrated_stance": "Respond to their claim; match tone. Use their hook nouns in sentence one.",
}


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w']+\b", text or ""))


def _substantive_angles(content: str) -> int:
    """Count list items / numbered sections that look like real substance."""
    n = 0
    for line in (content or "").splitlines():
        line = line.strip()
        if len(line) < 20:
            continue
        if re.match(r"^[\d\ufe0f\u20e3]+", line) or re.match(r"^\d+[\.\)]\s", line):
            n += 1
        elif re.match(r"^[-•]\s", line) and len(line) > 40:
            n += 1
    return n


def classify_post_type(content: str, article_title: str = "") -> str:
    combined = f"{article_title}\n{content or ''}"
    lower = combined.lower()

    if "mumbai" in lower and re.search(r"\b(summit|conference|expo)\b", lower):
        return "event_promo"

    if re.search(
        r"\b(summit|conference|expo)\b.{0,120}\b(mumbai|register|booth|visit us|meet us)\b",
        lower,
        re.S,
    ) or re.search(
        r"\b(register now|save your spot)\b",
        lower,
    ):
        if _substantive_angles(content) < 3:
            return "event_promo"

    event_hits = sum(1 for p in EVENT_PHRASES if p in lower)
    if re.search(r"\b(summit|expo|trade show|innovation (?:&|and) technology)\b", lower):
        event_hits += 1
    if event_hits >= 1 and _substantive_angles(content) < 2 and _word_count(content) < 500:
        return "event_promo"
    if event_hits >= 2 and _substantive_angles(content) < 2 and _word_count(content) < 450:
        return "event_promo"

    if re.search(PUBLISHER_RE, lower):
        return "publisher_economics"
    if re.search(RESEARCH_RE, lower):
        return "research"
    if re.search(TOOL_TIP_RE, lower):
        return "tool_tip"
    if any(re.search(p, lower) for p in PRODUCT_SIGNALS):
        return "product_launch"
    if re.search(
        r"\b(here(?:'s| is) (?:a|the|my)|step \d|how (?:i|we) |semantic similarity|embeddings)\b",
        lower,
    ):
        return "technical_tip"
    if re.search(r"\b(think|wrong|hot take|pushback|unpopular opinion)\b", lower):
        return "opinion"
    return "general"


def _comment_mode(post_type: str, recommended_style: str) -> str:
    mapping = {
        "tool_tip": "react_to_tool",
        "product_launch": "react_to_product",
        "research": "react_to_research",
        "publisher_economics": "react_to_publisher",
        "technical_tip": "react_to_tip",
        "opinion": "react_to_opinion",
        "event_promo": "practical_catch",
        "general": recommended_style or "react_to_opinion",
    }
    return mapping.get(post_type, recommended_style or "react_to_opinion")


def _should_skip_brief(post_type: str, content: str, intel: dict) -> tuple[bool, str | None]:
    if post_type != "event_promo":
        return False, None
    lower = (content or "").lower()
    # Webinar with a real discussion hook — allow if they ask a question or teach something
    if "?" in content and _substantive_angles(content) >= 1:
        return False, None
    if re.search(r"\b(notebooklm|aeo|geo|seo|ai visibility|citation)\b", lower) and _word_count(content) > 280:
        return False, None
    return True, "event_promo_no_room_for_non_cta_comment"


def build_post_brief(post: dict, intel: dict | None = None) -> dict:
    """Build structured brief for comment generation. Deterministic, no LLM."""
    intel = intel or {}
    content = (post.get("content") or "").strip()
    article_title = ((post.get("article") or {}).get("title") or "").strip()
    post_type = classify_post_type(content, article_title)
    recommended_style = intel.get("recommended_comment_style") or "operator_insight"
    comment_mode = _comment_mode(post_type, recommended_style)
    their_companies = extract_their_companies(
        {
            "full_name": (post.get("author") or {}).get("name") or "",
            "job_title": (post.get("author") or {}).get("info") or "",
        },
        post,
    )

    forbidden = list(FORBIDDEN_PIVOT_RULES.get(post_type, []))
    if their_companies:
        forbidden.append(
            f"You are NOT at {' / '.join(their_companies)}. "
            "React as external peer; use the client product.com or my own site for your stories."
        )
    if post_type not in ("publisher_economics", "general") and not re.search(
        r"\b(citation|geo|aeo|ai visibility|answer engine|llm seo)\b", content, re.I
    ):
        forbidden.append(
            "Post is NOT about AI citations or GEO — do not force citation/attribution/retrieval angles."
        )

    skip_comment, skip_reason = _should_skip_brief(post_type, content, intel)
    mode_instruction = COMMENT_MODE_INSTRUCTIONS.get(
        comment_mode,
        COMMENT_MODE_INSTRUCTIONS.get(recommended_style, ""),
    )

    brief: dict = {
        "post_type": post_type,
        "comment_mode": comment_mode,
        "comment_mode_instruction": mode_instruction,
        "their_companies": their_companies,
        "forbidden_pivots": forbidden,
        "skip_comment": skip_comment,
        "skip_reason": skip_reason,
    }

    # Style overrides for known types (only when intelligence left generic operator_insight)
    if post_type == "publisher_economics":
        brief["recommended_comment_style"] = "calibrated_stance"
    elif post_type in ("tool_tip", "technical_tip"):
        brief["recommended_comment_style"] = "practical_catch"
    elif post_type == "research":
        brief["recommended_comment_style"] = "implementation_gap"

    return brief


def apply_post_brief(intel: dict, post: dict) -> dict:
    """Merge brief into intelligence; skip pure event promos."""
    if not intel or intel.get("skip"):
        return intel
    brief = build_post_brief(post, intel)
    intel["post_brief"] = brief

    if brief.get("skip_comment"):
        intel["skip"] = True
        intel["skip_reason"] = brief.get("skip_reason") or "brief_skip"
        intel["recommended_comment_style"] = "skip"
        return intel

    if brief.get("recommended_comment_style"):
        intel["recommended_comment_style"] = brief["recommended_comment_style"]
    if brief.get("post_type") == "publisher_economics":
        intel["conversation_type"] = intel.get("conversation_type") or "opinion"
        intel["post_hook_type"] = "publisher_or_media_economics"

    return intel
