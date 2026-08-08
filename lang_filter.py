"""Lightweight English detection for post filtering (no external deps)."""
from __future__ import annotations

import re

# Common English function words — enough signal for LinkedIn-length posts.
_EN_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "could", "should", "may", "might", "must", "shall", "can",
    "this", "that", "these", "those", "it", "its", "they", "them", "their", "we", "our",
    "you", "your", "i", "my", "me", "not", "no", "yes", "if", "as", "by", "from", "about",
    "into", "through", "during", "before", "after", "when", "where", "why", "how", "all",
    "each", "every", "both", "few", "more", "most", "other", "some", "such", "than", "too",
    "very", "just", "also", "now", "here", "there", "what", "which", "who", "while",
    "because", "until", "although", "though", "than", "then", "than", "only", "own", "same",
    "so", "than", "up", "out", "over", "under", "again", "once", "any", "both", "each",
})

_NON_LATIN_RE = re.compile(
    "["
    "\u0400-\u04ff"  # Cyrillic
    "\u0600-\u06ff"  # Arabic
    "\u0590-\u05ff"  # Hebrew
    "\u4e00-\u9fff"  # CJK
    "\u3040-\u30ff"  # Japanese kana
    "\uac00-\ud7af"  # Korean
    "\u0e00-\u0e7f"  # Thai
    "\u0900-\u097f"  # Devanagari
    "]"
)

# High-signal non-English tokens (PT/ES/FR/DE) common on LinkedIn.
_NON_ENGLISH_MARKERS = re.compile(
    r"\b("
    r"não|nao|você|voce|estratégia|estratégia|compradores|também|tambem|vocês|isso|"
    r"muito|porque|quando|onde|como|para|pelo|pela|uma|uns|umas|dos|das|nos|nas|"
    r"está|están|estamos|también|tambien|qué|que|más|mas|por|pero|muy|"
    r"vous|nous|avec|pour|cette|ceux|très|tres|"
    r"und|eine|einer|nicht|auch|"
    r"¿|¡"
    r")\b",
    re.I,
)

_WORD_RE = re.compile(r"[a-zA-Z']{3,}")


def is_probably_english(text: str) -> bool:
    """Return True when post content is likely English."""
    text = (text or "").strip()
    if not text:
        return False
    if _NON_LATIN_RE.search(text):
        return False
    if len(_NON_ENGLISH_MARKERS.findall(text)) >= 2:
        return False
    words = _WORD_RE.findall(text.lower())
    if len(words) < 10:
        # Short snippets: lean on markers only (already checked above).
        return len(_NON_ENGLISH_MARKERS.findall(text)) == 0
    hits = sum(1 for w in words if w in _EN_STOPWORDS)
    return (hits / len(words)) >= 0.14
