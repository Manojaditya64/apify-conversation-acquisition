"""
Simple LinkedIn room-comment generation.

Thin system prompt + structured user JSON. No layered markdown dumps.
"""
from __future__ import annotations

import json
import re
from typing import Any

COMMENT_SYSTEM_PROMPT = """You write LinkedIn thread comments as Manojaditya Nadar.

ROLE
- External operator peer commenting on someone else's post.
- Audience = readers in the thread, not convincing the author.
- You are NOT the post author and NOT employed at their company.

YOUR BACKGROUND (use for stories — never pitch)
- AI systems architect; runs content workflows daily on zelitho.com.
- Dogfood site: ~50 system-generated posts; +10K AI citations from different sources (snapshot, not a guarantee for others).
- Never name "Zelitho" as a product. Say zelitho.com or my own site.

OUTPUT — JSON only, no markdown:
{
  "commentable": true,
  "skip_reason": null,
  "variants": [
    {"label": "A", "comment": "..."},
    {"label": "B", "comment": "..."},
    {"label": "C", "comment": "..."},
    {"label": "D", "comment": "..."},
    {"label": "E", "comment": "..."}
  ]
}

RULES
- 50–120 words per variant (E may be 35–80). Max 150 words.
- Five different angles — not the same idea rephrased.
- React to their specific claim (tool, stat, framework). Add something new.
- Your stories = your site/work. Never write "at [their company], we...".
- Do not quote their article headline or paste their hashtags.
- Do not open with praise (Great post, Love this, Well said).
- Do not mirror their opening sentence.
- Variant C only: may use allowed proof from the user payload.
- No em dashes (—). No invented stats. No engagement bait questions to the author.
- If the post is pure event promo with nothing to add, return commentable false.

VARIANTS (light guide, not a template)
- A: your observation tied to their hook
- B: respectful tension or nuance on their claim
- C: first-person micro story (proof allowed)
- D: where teams stumble on this topic
- E: short compressed take
"""


def opening_hook(content: str, max_len: int = 280) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    para = re.split(r"\n\s*\n", text, maxsplit=1)[0].strip()
    if len(para) <= max_len:
        return para
    return para[:max_len].rsplit(" ", 1)[0] + "…"


def extract_their_companies(author: dict, post: dict) -> list[str]:
    """Companies that belong to the post author — commenter must never claim these."""
    companies: set[str] = set()
    headline = (
        (author.get("job_title") or "")
        or (author.get("headline") or "")
        or ((post.get("author") or {}).get("info") or "")
    )
    for m in re.finditer(
        r"\bat\s+([A-Z][A-Za-z0-9&'.\- ]{1,48})",
        headline,
    ):
        name = re.split(r"[•|,/]", m.group(1))[0].strip()
        if len(name) > 2:
            companies.add(name)
    for m in re.finditer(
        r"\b(?:co-?founder|founder)\s+@([A-Za-z0-9][A-Za-z0-9&'.\- ]{1,40})",
        headline,
        re.I,
    ):
        companies.add(m.group(1).strip())
    author_name = (
        (author.get("full_name") or "").strip()
        or ((post.get("author") or {}).get("name") or "").strip()
    )
    if (post.get("author") or {}).get("type") == "company" and author_name:
        companies.add(author_name)
    for attr in post.get("contentAttributes") or []:
        if attr.get("type") == "COMPANY_NAME":
            co = (attr.get("company") or {}).get("name") or ""
            if co:
                companies.add(co.strip())
    content = (post.get("content") or "")[:1200]
    for m in re.finditer(r"\bAt\s+([A-Z][A-Za-z0-9'&.\-]{2,40}),", content):
        companies.add(m.group(1).strip())
    return sorted(companies, key=str.lower)


def extract_post_hashtags(content: str) -> list[str]:
    return [m.group(1).lower() for m in re.finditer(r"#([A-Za-z][A-Za-z0-9_]{2,40})\b", content or "")]


def build_comment_user_payload(item: dict) -> dict[str, Any]:
    post = item.get("post") or {}
    author = item.get("author") or {}
    intel = item.get("intelligence") or {}
    content = (post.get("content") or "").strip()
    article_title = ((post.get("article") or {}).get("title") or "").strip()
    their_companies = extract_their_companies(author, post)
    author_name = author.get("full_name") or (post.get("author") or {}).get("name") or "the author"
    hashtags = extract_post_hashtags(content)

    return {
        "you": {
            "name": "Manojaditya Nadar",
            "role": "AI systems architect; runs research → draft → publish daily",
            "your_site": "zelitho.com",
            "allowed_proof_variant_c_only": [
                "~50 posts on zelitho.com",
                "+10K AI citations from different sources (snapshot, not a guarantee)",
                "my own site / when we publish on zelitho.com",
            ],
            "never_do": [
                "Pitch Zelitho by name",
                "Invent stats or timelines",
                f"Claim employment at: {', '.join(their_companies) or 'their company'}",
                "Quote their article headline verbatim",
                "Paste their post hashtags into your comment",
            ],
        },
        "author": {
            "name": author_name,
            "headline": author.get("job_title") or (post.get("author") or {}).get("info") or "",
            "their_companies": their_companies,
        },
        "post": {
            "content": content,
            "opening_hook": opening_hook(content),
            "article_title_for_context_only": article_title,
            "hashtags_in_post": hashtags[:8],
        },
        "context": {
            "conversation_type": intel.get("conversation_type") or "",
            "skip_if_pure_promo": bool((intel.get("post_brief") or {}).get("skip_comment")),
        },
        "task": (
            f"Write 5 comments on {author_name}'s post. "
            "You are an external peer — react to their claim with YOUR experience on zelitho.com or my own site. "
            "Each variant must be a different angle. Engage their topic; never speak as their employee."
        ),
    }


def generate_comments(
    api_key: str,
    model: str,
    item: dict,
    *,
    chat_fn,
) -> dict:
    """Single LLM call — system + user JSON. chat_fn(api_key, model, system, user) -> dict."""
    payload = build_comment_user_payload(item)
    user = json.dumps(payload, indent=2)
    return chat_fn(api_key, model, COMMENT_SYSTEM_PROMPT, user)
