"""
Deterministic comment inspector + DeepSeek repair loop for LinkedIn room comments.

Usage:
  from comment_inspector import inspect_and_repair_variants
  result = inspect_and_repair_variants(api_key, model, parsed, context, repair_fn=deepseek_chat)
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

MIN_WORDS = 50
MIN_WORDS_E = 35
MAX_WORDS = 150
MAX_SENTENCES = 6
MAX_REPAIR_ATTEMPTS = 2
# Diversity-only fails: log them, but do not rewrite (rewrites cause telegraphese)
SOFT_LINT_FAILURES = frozenset({
    "duplicate_angle_across_variants",
    "too_similar_to_another_variant",
    "duplicate_skeleton_opener",
})

# --- Structural patterns ---
EM_DASH_RE = re.compile(r"[—–]")
EXCLAMATION_RE = re.compile(r"!")
EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FEFF]"
)
BULLET_RE = re.compile(r"(^|\s)[•\-]\s|\n[•\-]\s")
URL_RE = re.compile(r"https?://|www\.|lnkd\.in|zurl\.co", re.I)
MULTIPLE_QUESTION_RE = re.compile(r"\?.*\?", re.S)
INVENTED_STAT_RE = re.compile(
    r"\b\d{1,3}(?:\.\d+)?\s*%|\b\d+x\b|\b\d+\s*(?:%|percent|times|weeks|months|days)\b",
    re.I,
)
FABRICATED_OUTCOME_VARIANT_C_RE = re.compile(
    r"\b(we saw a|i saw a|lifted? by|jumped? \d|increased by|decreased by)\b",
    re.I,
)
INVENTED_MULTIPLIER_RE = re.compile(r"\b(doubled?|tripled?|10x|2x|3x|4x|5x)\b", re.I)
CORPORATE_PHRASE_RE = re.compile(
    r"\b(furthermore|therefore|consequently|in conclusion|it is evident that|"
    r"it is evident|as a result,?|thus,?)\b",
    re.I,
)
GUARANTEE_RE = re.compile(
    r"\b(guarantee[ds]?|will get cited|will rank|#1 ranking|dominate rankings|always cite)\b",
    re.I,
)
WRONG_DOMAIN_TERMS_RE = re.compile(
    r"\b(cms handoff|content calendar|blog calendar|h2 layout|speed-to-publish|semrush|writer brief)\b",
    re.I,
)
POST_IS_CONTENT_TOPIC_RE = re.compile(
    r"\b(seo|geo|aeo|blog|content|cms|schema|citation|publish|keyword|serp|llm|pipeline)\b",
    re.I,
)
ALLOWED_PROOF_PHRASES = (
    "client-site.example.com",
    "on my own site",
    "on our own site",
    "in our stack",
    "running this daily",
    "built by the system",
    "+10k ai citations",
    "10k ai citations",
    "bing/copilot citations",
    "~50 posts",
    "50 posts",
    "research",
    "content queue",
    "content pipeline",
    "scope gate",
    "title strategy",
    "cms handoff",
)
AUTHOR_ADDRESS_RE = re.compile(
    r"\b(you nailed|your point|you('re| are) right|you said|your post|great post|"
    r"love this|thanks for sharing|well said|spot on|couldn't agree more|"
    r"completely agree|excellent insights|this resonates|so true|you've captured|your insight)\b",
    re.I,
)
PITCH_RE = re.compile(
    r"\b(dm me|book a (?:call|demo)|schedule a demo|free trial|"
    r"link in (?:bio|comments)|check out my|sign up|register now|try the client product)\b",
    re.I,
)
CLIENT_PRODUCT_PITCH_RE = re.compile(
    r"\bthe client product\b(?!\.com)",
    re.I,
)
GENERIC_OPENER_RE = re.compile(
    r"^(great|love|so true|thanks|well said|spot on|couldn't agree|completely agree|"
    r"excellent insights|this is|here's the thing|it's worth noting|at the end of the day|"
    r"the key is|in today's|the future of|winners will|those who)\b",
    re.I,
)
SLOGAN_RE = re.compile(
    r"\b(no .+, no .+|.+\s+is the new .+|the future belongs to|"
    r"traffic is rented|trust travels|game.?changer|revolutionary|"
    r"mind.?blowing|must read|don't miss|you need to)\b",
    re.I,
)
CONSULTANT_RE = re.compile(
    r"\b(lean into|double down|move the needle|low.?hanging fruit|"
    r"circle back|deep dive|bandwidth|synergy|holistic|robust|seamless|"
    r"best practices|key takeaway|food for thought|value.?add|"
    r"thought leader|cutting.?edge|world.?class|empower|unlock|unleash|"
    r"supercharge|at scale|digital landscape|fast.?paced|ecosystem)\b",
    re.I,
)
EMPTY_INTENSIFIER_RE = re.compile(
    r"\b(truly|incredibly|absolutely|definitely|literally|super)\b",
    re.I,
)
ENGAGEMENT_BAIT_RE = re.compile(
    r"\b(curious your thoughts|would love to hear|what do you think\??|"
    r"let me know what you think|agree\??|thoughts\??)\s*$",
    re.I,
)
TEMPLATE_BRIDGE_RE = re.compile(
    r"\b(we see the same pattern|we see this all the time|in our experience|"
    r"great article|another great|hidden multiplier|the missing link|"
    r"here's the thing|it's worth noting|the key is|natural evolution)\b",
    re.I,
)
REAL_X_IS_RE = re.compile(
    r"\bthe real (?:\w+\s+){0,2}(?:gap|drag|bottleneck|issue|problem|challenge|shift|leverage|win)\b",
    re.I,
)
AUTHOR_DIRECT_QUESTION_RE = re.compile(
    r"\b(how do you|how are you|how did you|what do you|what's your|have you|did you|do you)\b",
    re.I,
)
COMMENT_STOPWORDS = frozenset({
    "about", "after", "again", "against", "their", "there", "these", "those",
    "through", "under", "where", "which", "while", "would", "could", "should",
    "being", "between", "before", "other", "first", "still", "daily", "fresh",
    "teams", "most", "that", "this", "with", "from", "into", "your", "when",
    "what", "than", "then", "them", "they", "have", "been", "were", "will",
    "just", "also", "only", "more", "much", "many", "some", "such", "very",
    "because", "without", "across", "every", "build", "built", "keep", "keeps",
})
GENERIC_SEO_BRO_RE = re.compile(
    r"\b(content is king|seo is dead|seo isn't dead|the new seo|"
    r"schema is the new|backlinks are dead|ai is changing everything|"
    r"adapt or die|stay ahead of the curve|in the age of ai|"
    r"table stakes|low hanging|paradigm shift|sea change|"
    r"north star metric|10x your|level up your|crush it|"
    r"dominate|win citations\. dominate|stack up across|"
    r"operational shift|rebuilding the pipeline|research depth|"
    r"formatted for direct extraction|ai models.? shortlists?|"
    r"rebuilding the pipeline that gets you)\b",
    re.I,
)
VAGUE_ABSTRACTION_RE = re.compile(
    r"\b(strategic alignment|value creation|intelligence layer|synergy|"
    r"digital transformation journey|unlock growth|drive impact|"
    r"elevate your brand|build trust|foster engagement)\b",
    re.I,
)
RUNNING_ON_SITE_CLICHE_RE = re.compile(
    r"\b(running this on our own site|on our own site:?\s*we saw a \d)",
    re.I,
)
SAME_SKELETON_RE = re.compile(
    r"^(most teams|most brands|most companies|most startups|most people|"
    r"the real |many teams|a lot of teams)\b",
    re.I,
)
OPS_JARGON_TERMS = (
    "pipeline",
    "schema",
    "parser",
    "handoff",
    "scope gate",
    "entity cluster",
    "information density",
    "cms handoff",
    "content queue",
    "index cycle",
    "answer block",
    "content cadence",
    "dedup",
)
STOCK_C_OPENER_RE = re.compile(
    r"^(in our stack|running this on the client product\.com|on the client product\.com:?\s|"
    r"we run this on the client product\.com|we run this on the client product|"
    r"we run this daily on our own site|running this daily on our own site)",
    re.I,
)
WHAT_BREAKS_TEMPLATE_RE = re.compile(
    r"\bwhat breaks first when\b|\bwhat usually breaks\b|\bwhat breaks when you\b",
    re.I,
)
OVERUSED_OPENER_RE = re.compile(
    r"^(?:one thing (?:that )?surprised (?:us|me)|we expected|we ran an experiment|"
    r"running this daily|we started tracking|when we started|one thing surprised|"
    r"what caught me off guard)",
    re.I,
)
OVERUSED_ENDING_RE = re.compile(r"\b(makes me wonder|makes you wonder)\b", re.I)
RESEARCH_REPORT_RE = re.compile(
    r"\b(?:our (?:tests?|research|analysis|data) (?:showed|suggest|indicate|demonstrate)|"
    r"the (?:findings?|results?|data) (?:show|suggest|indicate)|"
    r"we (?:found|discovered|observed) that|this (?:demonstrates|indicates|suggests) that|"
    r"the pattern (?:holds|suggests|indicates)|our (?:experiment|test) (?:showed|proved))\b",
    re.I,
)
VAGUE_EXPERIMENT_RE = re.compile(
    r"\bwe (?:tested|ran|tried) (?:answer pages?|structured content|this approach|"
    r"our pages|the same pages)\b",
    re.I,
)
AI_SEO_JARGON_TERMS = (
    "ai citation",
    "ai citations",
    "answer page",
    "answer pages",
    " llm",
    "llms",
    "structured content",
    "generative engine",
    "geo ",
    "zero-click",
    "aeo ",
    "schema markup",
)
STALE_INSIGHT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "answer_pages_lesson",
        re.compile(
            r"answer pages?|single[- ]question pages?|one question per page|"
            r"long[- ]form (?:articles?|posts?|guides?)",
            re.I,
        ),
    ),
    (
        "citations_before_rankings",
        re.compile(
            r"(?:citations?|mentions?).{0,40}(?:before|prior to|ahead of|weeks before).{0,30}"
            r"(?:rankings?|organic|traffic)|"
            r"rankings?.{0,30}(?:lagging|stayed flat|didn't move|looked fine)",
            re.I,
        ),
    ),
    (
        "structure_beats_keywords",
        re.compile(
            r"structure beats?|format(?:ting)? beats? keywords?|"
            r"keywords? (?:don't|didn't|won't|no longer) matter",
            re.I,
        ),
    ),
    (
        "measuring_wrong_closer",
        re.compile(
            r"\b(?:we(?:'re| are)|i(?:'m| am)|teams? (?:are|were)) measuring the wrong\b"
            r"|\bmeasuring the wrong (?:thing|kpi|metric)\b",
            re.I,
        ),
    ),
]
MEASURING_WRONG_ENDING_RE = re.compile(
    r"\bmeasuring the wrong (?:thing|kpi|metric)\b",
    re.I,
)
CURIOUS_ENDING_RE = re.compile(
    r"\bcurious if (?:others|anyone|you)\b",
    re.I,
)
GENERIC_HOOK_WORDS = frozenset({
    "traffic", "clicks", "click", "search", "google", "model", "models", "answer",
    "answers", "competitor", "competitors", "optimize", "visibility", "citation",
    "citations", "content", "website", "organic", "ranking", "rankings", "page",
    "pages", "users", "business", "businesses", "marketing", "brand", "brands",
    "share", "voice", "engine", "engines", "query", "queries", "session",
    "sessions", "metric", "metrics", "performance", "digital", "online",
    "strategy", "strategies", "recent", "article", "reported", "people",
})
GENERIC_CITATION_PLAYBOOK_RE = re.compile(
    r"\bcompetitor(?:s)? (?:appear|appeared|show(?:ed)? up) in (?:every )?(?:ai )?answers?\b"
    r"|share of voice (?:now )?means"
    r"|optimize for (?:clicks|being the answer)"
    r"|user never leaves the search page"
    r"|without (?:any )?detectable traffic"
    r"|the answer the model chooses"
    r"|ai overview(?:s)? (?:stole|hijacked)"
    r"|attribution models? (?:broke|broken)"
    r"|measuring the wrong (?:thing|kpi|metric)\b",
    re.I,
)
PROOF_ARTIFACTS_RE = re.compile(
    r"\b(\+?10k|10,?000)\s*(?:ai\s+)?citations?|"
    r"~\s*50\s+posts?|50\s+system[- ]generated|"
    r"the client product\.com|\bthe client product\b|"
    r"system-generated posts?|daily experiment\b",
    re.I,
)
MINI_BLOG_RE = re.compile(
    r"\b(furthermore|in addition|firstly|secondly|thirdly|"
    r"let me explain|here's why|the reason is|to summarize)\b",
    re.I,
)
REFINEMENT_OPENING_ALTERNATIVES = (
    "I didn't expect..., We almost missed..., Looking back..., "
    "This only became obvious after..., It took us weeks to notice..., "
    "The weird part was..., Counterintuitively..., We almost ignored..., "
    "The assumption that broke for us was..."
)
REFINEMENT_ENDING_ALTERNATIVES = (
    "I'm still trying to explain why., That's the part we're testing next., "
    "That's what changed my mind., Still validating whether this holds., "
    "That's the question I keep coming back to., I keep coming back to that."
)
V4_INSIGHT_ANGLES = (
    "measurement, attribution, retrieval, entity consistency, crawl behavior, "
    "trust signals, user behavior, analytics, debugging, content operations, "
    "AI model differences, publishing workflows"
)
# Display / pick-likelihood order for inbox UI (labels unchanged A–E)
VARIANT_DISPLAY_ORDER = ("A", "E", "D", "C", "B")

# Literal banned phrases (case-insensitive substring)
BANNED_LITERALS: list[str] = [
    "great post",
    "love this",
    "excellent insights",
    "completely agree",
    "so true",
    "thanks for sharing",
    "well said",
    "spot on",
    "couldn't agree more",
    "this resonates",
    "you nailed it",
    "your point about",
    "the real gap is",
    "the real drag is",
    "the real bottleneck is",
    "the real bottleneck",
    "the real shift is",
    "the real issue is",
    "the real problem is",
    "the real leverage is",
    "what breaks first when",
    "what usually breaks",
    "what breaks when you",
    "6.6k",
    "6.6 k",
    "we see the same pattern",
    "we see this all the time",
    "hidden multiplier",
    "game changer",
    "game-changer",
    "in today's digital landscape",
    "in today's fast-paced",
    "at the end of the day",
    "it's worth noting",
    "here's the thing",
    "the key is",
    "food for thought",
    "key takeaway",
    "best practices",
    "thought leader",
    "cutting edge",
    "cutting-edge",
    "world class",
    "world-class",
    "move the needle",
    "low hanging fruit",
    "low-hanging fruit",
    "double down",
    "lean into",
    "north star",
    "circle back",
    "deep dive",
    "value add",
    "value-add",
    "digital landscape",
    "paradigm shift",
    "sea change",
    "adapt or die",
    "stay ahead of the curve",
    "content is king",
    "seo is dead",
    "seo isn't dead",
    "ai is changing everything",
    "revolutionary",
    "mind blowing",
    "mind-blowing",
    "must read",
    "don't miss",
    "you need to",
    "dm me",
    "link in comments",
    "link in bio",
    "free audit",
    "book a call",
    "schedule a demo",
    "curious your thoughts",
    "would love to hear",
    "what do you think",
    "let me know",
    "check out my",
    "sign up for",
    "register now",
    "natural evolution",
    "this is the future of",
    "the future belongs to",
    "winners will",
    "those who figure out",
    "no schema, no",
    "no content, no",
    "no x, no y",
    "is the new ",
    "table stakes",
    "crush it",
    "level up",
    "10x your",
    "supercharge",
    "unleash",
    "unlock",
    "empower",
    "robust",
    "seamless",
    "holistic",
    "synergy",
    "synergies",
    "ecosystem",
    "leverage synergies",
    "drive impact",
    "elevate your",
    "foster engagement",
    "build trust at scale",
    "schema alone isn't enough",
    "schema alone is not enough",
    "the infrastructure gap is real",
    "the infrastructure gap is",
    "the missing piece is",
    "the operational challenge is",
    "the shift is real",
    "this is huge",
    "huge if true",
    "banger post",
    "fire post",
    "dropping gems",
    "spitting facts",
    "say it louder",
    "needed this",
    "this needed to be said",
    "more people need to hear",
    "take my money",
    "chef's kiss",
    "mic drop",
    "full stop",
    "periodt",
    "let that sink in",
    "read that again",
    "save this post",
    "bookmark this",
    "share this with",
    "tag someone who",
    "comment for reach",
    "commenting for reach",
    "furthermore",
    "therefore",
    "consequently",
    "in conclusion",
    "it is evident that",
    "it is evident",
    "thanks for the share",
    "appreciate you sharing",
    "adding this to",
    "stealing this",
    "pinning this",
    "following for more",
    "more of this please",
    "exactly this",
    "this exactly",
    "100%",
    "100 percent",
    "nailed it",
    "hits different",
    "on point",
    "facts",
    "big facts",
    "truth bomb",
    "hard agree",
    "soft agree",
    "gentle agree",
    "underrated point",
    "overlooked point",
    "underappreciated",
    "cannot stress this enough",
    "if you know you know",
    "iykyk",
    "hot take but",
    "unpopular opinion but",
    "controversial but",
    "plot twist",
    "pro tip",
    "quick tip",
    "friendly reminder",
    "gentle reminder",
    "psa:",
    "public service announcement",
    "thread",
    "1/",
    "a thread",
    "breaking:",
    "just my two cents",
    "my two cents",
    "for what it's worth",
    "fwiw",
    "imo",
    "imho",
    "tbh",
    "ngl",
    "at the risk of",
    "devil's advocate",
    "playing devil's advocate",
    "with all due respect",
    "respectfully",
    "hear me out",
    "unironically",
    "ironically",
    "literally everyone",
    "everyone needs to",
    "most people don't realize",
    "most people don't understand",
    "most people still think",
    "the uncomfortable truth",
    "the hard truth",
    "the brutal truth",
    "the simple truth",
    "the honest truth",
    "let me be clear",
    "make no mistake",
    "mark my words",
    "remember this",
    "write this down",
    "screenshot this",
]

STOPWORDS = frozenset(
    "the a an and or but in on at to for of is are was were be been being "
    "that this it with as by from their they them we you your our has have had "
    "not can will just about into than then also very much many some any all "
    "when what how who which where why if so up out over after before".split()
)


def _banned_literal_match(lower: str, literal: str) -> bool:
    """Short literals use word boundaries to avoid substring false positives (e.g. ngl in single)."""
    if len(literal) <= 4:
        return bool(re.search(r"\b" + re.escape(literal) + r"\b", lower))
    return literal in lower


def count_ai_seo_jargon(text: str) -> int:
    lower = text.lower()
    return sum(1 for term in AI_SEO_JARGON_TERMS if term in lower)


def _opening_signature(text: str) -> str:
    words = re.findall(r"\b[\w']+\b", (text or "").lower())[:6]
    return " ".join(words)


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w']+\b", text))


def sanitize_comment_text(text: str) -> str:
    """Deterministic cleanup before ship — em/en dashes break LinkedIn voice rules."""
    if not text:
        return text
    out = EM_DASH_RE.sub(", ", text)
    out = re.sub(
        r"^What surprised me about Fable 5 was that Firstly,?\s*",
        "What surprised me about Fable 5: ",
        out,
        flags=re.I,
    )
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r",\s*,", ", ", out)
    return out.strip()


def _opening_words(text: str, limit: int = 10) -> list[str]:
    first = re.split(r"[.!?\n]", (text or "").strip(), maxsplit=1)[0]
    filler = frozenset({"around", "really", "just", "even", "also"})
    return [
        w for w in re.findall(r"[a-z']+", first.lower())
        if w not in COMMENT_STOPWORDS and w not in filler and len(w) > 1
    ][:limit]


def echoes_post_opening(comment: str, post_content: str) -> bool:
    """True when comment opens by mirroring the author's first sentence."""
    if not post_content or not comment:
        return False
    pw = _opening_words(post_content, 10)
    cw = _opening_words(comment, 10)
    if len(pw) < 4 or len(cw) < 4:
        return False
    n = min(7, len(pw), len(cw))
    matches = sum(1 for a, b in zip(pw[:n], cw[:n]) if a == b)
    if matches >= 5:
        return True
    post_joined = " ".join(pw)
    for i in range(max(1, len(cw) - 4)):
        chunk = " ".join(cw[i : i + 5])
        if len(chunk) > 12 and chunk in post_joined:
            return True
    return False


def sentence_count(text: str) -> int:
    """Count sentence-like units ending in . ! ? (ignores empty trailing)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return 0
    parts = re.split(r"[.!?]+(?:\s+|$)", cleaned)
    return len([p for p in parts if p.strip()])


def hard_lint_failures(failures: list[str]) -> list[str]:
    """Failures that should trigger a rewrite (excludes soft diversity noise)."""
    return [f for f in failures if f.split(":")[0] not in SOFT_LINT_FAILURES]

def extract_post_terms(post_content: str, min_len: int = 5) -> set[str]:
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9\-]{%d,}\b" % (min_len - 1), post_content.lower())
    return {w for w in words if w not in STOPWORDS and len(w) >= min_len}


def extract_rare_phrases(post_content: str, min_words: int = 2) -> list[str]:
    """Multi-word phrases from post that look like coined slogans (3+ words or Title Case runs)."""
    phrases: list[str] = []
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", post_content):
        phrases.append(m.group(1).lower())
    for line in (post_content or "").splitlines():
        line = line.strip()
        if len(line.split()) >= min_words and re.search(r"[A-Z]{2,}|[A-Z][a-z]+\s+[A-Z]", line):
            phrases.append(line.lower()[:80])
    return phrases


def count_ops_jargon(text: str) -> int:
    lower = text.lower()
    return sum(1 for term in OPS_JARGON_TERMS if term in lower)


def count_forced_echo(comment: str, post_content: str) -> int:
    """Count rare multi-word phrases from post echoed in comment."""
    if not post_content:
        return 0
    lower_comment = comment.lower()
    echoes = 0
    for phrase in extract_rare_phrases(post_content):
        if len(phrase.split()) >= 2 and phrase in lower_comment:
            echoes += 1
    return echoes


def extract_allowed_stats(post_content: str) -> set[str]:
    allowed: set[str] = set()
    for m in INVENTED_STAT_RE.finditer(post_content):
        allowed.add(m.group(0).lower())
    return allowed


def extract_hook_anchors(post_content: str, article_title: str = "") -> set[str]:
    """Distinctive names, brands, and stats from the post opening — not generic SEO words."""
    orig = f"{article_title}\n{(post_content or '')}"[:900]
    anchors: set[str] = set()
    for m in re.finditer(r"\b[A-Z][a-zA-Z']+(?:\s+[A-Z][a-zA-Z']+)+\b", orig):
        phrase = m.group(0).lower()
        if not all(w in GENERIC_HOOK_WORDS for w in phrase.split()):
            anchors.add(phrase)
    for m in re.finditer(r"\b[A-Z][a-zA-Z]{3,}\b", orig):
        w = m.group(0).lower()
        if w not in GENERIC_HOOK_WORDS and w not in STOPWORDS:
            anchors.add(w)
    opening_lower = orig.lower()
    for m in re.finditer(r"\b\d{1,3}(?:\.\d+)?%|\$\d+(?:\.\d+)?\s*billion\b", opening_lower):
        anchors.add(m.group(0))
    for phrase in (
        "wall street journal", "wsj", "ai overviews", "answer engine",
        "publishers", "alphabet", "cnn", "reddit",
    ):
        if phrase in opening_lower:
            anchors.add(phrase)
    return anchors


def hook_anchor_hits(comment: str, anchors: set[str]) -> int:
    lower = (comment or "").lower()
    return sum(1 for a in anchors if a in lower)


def lint_comment(
    comment: str,
    *,
    post_content: str = "",
    article_title: str = "",
    variant_label: str = "A",
    saturation_snippets: list[str] | None = None,
    author_companies: list[str] | None = None,
    post_hashtags: list[str] | None = None,
) -> list[str]:
    """Return list of failure codes. Empty = pass."""
    failures: list[str] = []
    text = (comment or "").strip()
    if not text:
        return ["empty_comment"]

    lower = text.lower()
    label = variant_label.upper()
    min_words = MIN_WORDS_E if label == "E" else MIN_WORDS

    if word_count(text) < min_words:
        failures.append(f"too_short_{word_count(text)}_words")
    if word_count(text) > MAX_WORDS:
        failures.append(f"too_long_{word_count(text)}_words")

    sc = sentence_count(text)
    if sc > MAX_SENTENCES:
        failures.append(f"too_many_sentences_{sc}")

    if EM_DASH_RE.search(text):
        failures.append("em_dash")
    if EXCLAMATION_RE.search(text):
        failures.append("exclamation")
    if EMOJI_RE.search(text):
        failures.append("emoji")
    if BULLET_RE.search(text):
        failures.append("bullet_list")
    if URL_RE.search(text):
        failures.append("url_or_link")
    if AUTHOR_ADDRESS_RE.search(text):
        failures.append("author_address")
    if PITCH_RE.search(text):
        failures.append("pitch_or_cta")
    if CLIENT_PRODUCT_PITCH_RE.search(lower):
        failures.append("the client product_product_pitch")
    if GENERIC_OPENER_RE.search(text):
        failures.append("generic_opener")
    if SLOGAN_RE.search(lower):
        failures.append("slogan_or_aphorism")
    if CONSULTANT_RE.search(lower):
        failures.append("consultant_speak")
    if CORPORATE_PHRASE_RE.search(lower):
        failures.append("corporate_phrasing")
    if EMPTY_INTENSIFIER_RE.search(lower):
        failures.append("empty_intensifier")
    if TEMPLATE_BRIDGE_RE.search(lower):
        failures.append("template_bridge")
    if REAL_X_IS_RE.search(lower):
        failures.append("real_x_is_template")
    if GENERIC_SEO_BRO_RE.search(lower):
        failures.append("generic_seo_bro")
    if VAGUE_ABSTRACTION_RE.search(lower):
        failures.append("vague_abstraction")
    if RUNNING_ON_SITE_CLICHE_RE.search(lower):
        failures.append("running_on_site_cliche")
    if WHAT_BREAKS_TEMPLATE_RE.search(lower):
        failures.append("what_breaks_template")
    if OVERUSED_OPENER_RE.search(text.strip()):
        failures.append("overused_opener")
    if RESEARCH_REPORT_RE.search(lower):
        failures.append("research_report_tone")
    if VAGUE_EXPERIMENT_RE.search(lower):
        failures.append("vague_broad_experiment")
    jargon_count = count_ai_seo_jargon(lower)
    if jargon_count >= 2:
        failures.append(f"ai_seo_jargon_density:{jargon_count}")
    if MINI_BLOG_RE.search(lower):
        failures.append("mini_blog_tone")

    if label != "C" and PROOF_ARTIFACTS_RE.search(lower):
        failures.append("proof_artifacts_outside_variant_c")

    jargon_count = count_ops_jargon(lower)
    if jargon_count >= 2:
        failures.append(f"ops_jargon_density:{jargon_count}")

    if variant_label.upper() == "C" and STOCK_C_OPENER_RE.search(text):
        failures.append("stock_variant_c_opener")

    if post_content and echoes_post_opening(text, post_content):
        failures.append("echoes_post_opening")

    if post_content and count_forced_echo(text, post_content) >= 2:
        failures.append("forced_buzzword_echo")

    if GUARANTEE_RE.search(lower):
        failures.append("ranking_or_citation_guarantee")

    if post_content and not POST_IS_CONTENT_TOPIC_RE.search(post_content):
        if WRONG_DOMAIN_TERMS_RE.search(lower):
            failures.append("wrong_domain_vocabulary")

    # Variant C: stricter proof — no fabricated numeric outcomes
    if label == "C":
        if FABRICATED_OUTCOME_VARIANT_C_RE.search(lower):
            failures.append("fabricated_outcome_variant_c")
        for m in INVENTED_STAT_RE.finditer(text):
            token = m.group(0).lower()
            if not _stat_in_post(token, post_content):
                failures.append(f"invented_stat_variant_c:{token}")
                break

    # Invented multipliers (all variants) unless cited in post
    for m in INVENTED_MULTIPLIER_RE.finditer(text):
        token = m.group(0).lower()
        if not _stat_in_post(token, post_content):
            failures.append(f"invented_multiplier:{token}")
            break

    for literal in BANNED_LITERALS:
        if _banned_literal_match(lower, literal):
            failures.append(f"banned_literal:{literal[:40]}")
            break

    # Open-loop questions OK in any variant; block bait and author-directed questions
    if ENGAGEMENT_BAIT_RE.search(text):
        failures.append("engagement_bait_question")
    elif AUTHOR_DIRECT_QUESTION_RE.search(text):
        failures.append("author_direct_question")

    if MULTIPLE_QUESTION_RE.search(text):
        failures.append("multiple_questions")

    # Invented stats
    allowed_stats = extract_allowed_stats(post_content)
    for m in INVENTED_STAT_RE.finditer(text):
        token = m.group(0).lower()
        if token not in allowed_stats and not _stat_in_post(token, post_content):
            failures.append(f"invented_stat:{token}")
            break

    # Post relevance: no hard term-overlap requirement — plain-English comments
    # often paraphrase without sharing post nouns. Relevance is enforced by the LLM prompt.

    # Repeat saturation angles from thread
    if saturation_snippets:
        for snippet in saturation_snippets[:3]:
            snippet_terms = extract_post_terms(snippet, min_len=6)
            overlap = snippet_terms & extract_post_terms(text, min_len=6)
            if len(overlap) >= 2:
                failures.append("repeats_thread_comment_angle")
                break

    # Identity: never speak as the author's employer
    if author_companies:
        for co in author_companies:
            co_l = co.lower()
            if re.search(rf"\b(?:at|@)\s*{re.escape(co_l)}\b", lower):
                failures.append("claims_author_employer")
                break
            if re.search(rf"\b(?:back at|looking back at|pattern at)\s+{re.escape(co_l)}\b", lower):
                failures.append("claims_author_employer")
                break

    if article_title and len(article_title) >= 24:
        title_key = article_title.lower().strip()[:50]
        if title_key in lower:
            failures.append("recycles_article_title")

    if post_hashtags:
        hits = sum(1 for tag in post_hashtags if f"#{tag}" in lower or re.search(rf"\b{re.escape(tag)}\b", lower))
        if hits >= 2:
            failures.append("copies_post_hashtags")

    return failures


def _stat_in_post(stat_token: str, post_content: str) -> bool:
    lower_post = post_content.lower()
    # allow if the number appears in post
    nums = re.findall(r"\d+", stat_token)
    if nums and nums[0] in lower_post:
        return True
    return stat_token in lower_post


def _significant_tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z]{5,}", (text or "").lower())
        if w not in COMMENT_STOPWORDS
    }


def _lint_cross_variant_stale_insights(
    variants: list[dict], results: dict[str, list[str]],
) -> None:
    """Flag when 2+ variants repeat the same predictable SEO lesson."""
    theme_labels: dict[str, list[str]] = {}
    for v in variants:
        label = (v.get("label") or "").strip().upper()
        lower = (v.get("comment") or "").lower()
        if not lower:
            continue
        for theme, pat in STALE_INSIGHT_PATTERNS:
            if pat.search(lower):
                theme_labels.setdefault(theme, []).append(label)
    for labels in theme_labels.values():
        if len(labels) >= 2:
            for label in labels:
                results.setdefault(label, []).append("stale_insight_repeated")


def _lint_cross_variant_refinement(variants: list[dict], results: dict[str, list[str]]) -> None:
    """Cross-variant refinement: duplicate openers/endings, proof spam."""
    sig_to_labels: dict[str, list[str]] = {}
    wonder_labels: list[str] = []
    curious_labels: list[str] = []
    measuring_labels: list[str] = []

    for v in variants:
        label = (v.get("label") or "").strip().upper()
        comment = (v.get("comment") or "").strip()
        if not comment:
            continue
        sig = _opening_signature(comment)
        if len(sig.split()) >= 3:
            sig_to_labels.setdefault(sig, []).append(label)
        if OVERUSED_ENDING_RE.search(comment):
            wonder_labels.append(label)
        if CURIOUS_ENDING_RE.search(comment):
            curious_labels.append(label)
        if MEASURING_WRONG_ENDING_RE.search(comment):
            measuring_labels.append(label)

    for labels in sig_to_labels.values():
        if len(labels) > 1:
            for label in labels:
                results.setdefault(label, []).append("duplicate_opening_across_variants")

    if len(wonder_labels) >= 2:
        for label in wonder_labels:
            results.setdefault(label, []).append("overused_ending_wonder")

    if len(curious_labels) >= 3:
        for label in curious_labels:
            results.setdefault(label, []).append("overused_ending_curious")

    if len(measuring_labels) >= 2:
        for label in measuring_labels:
            results.setdefault(label, []).append("overused_ending_measuring_wrong")


def _lint_cross_variant_diversity(variants: list[dict], results: dict[str, list[str]]) -> None:
    tokens_by_label: dict[str, set[str]] = {}
    for v in variants:
        label = (v.get("label") or "").strip().upper()
        tokens_by_label[label] = _significant_tokens(v.get("comment") or "")

    labels = list(tokens_by_label.keys())
    for i, la in enumerate(labels):
        for lb in labels[i + 1:]:
            ta, tb = tokens_by_label[la], tokens_by_label[lb]
            if not ta or not tb:
                continue
            overlap = len(ta & tb)
            smaller = min(len(ta), len(tb))
            if smaller >= 3 and overlap / smaller >= 0.5:
                results.setdefault(la, []).append("too_similar_to_another_variant")
                results.setdefault(lb, []).append("too_similar_to_another_variant")

    from collections import Counter
    word_counts: Counter[str] = Counter()
    for tokens in tokens_by_label.values():
        for t in tokens:
            if len(t) >= 6:
                word_counts[t] += 1
    repeated = {w for w, c in word_counts.items() if c >= 3}
    if repeated:
        for label, tokens in tokens_by_label.items():
            if tokens & repeated:
                results.setdefault(label, []).append("duplicate_angle_across_variants")


def lint_variants(
    variants: list[dict],
    *,
    post_content: str = "",
    article_title: str = "",
    saturation_snippets: list[str] | None = None,
    author_companies: list[str] | None = None,
    post_hashtags: list[str] | None = None,
) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    openers: list[str] = []
    for v in variants:
        label = (v.get("label") or "").strip().upper()
        comment = (v.get("comment") or "").strip()
        fails = lint_comment(
            comment,
            post_content=post_content,
            article_title=article_title,
            variant_label=label,
            saturation_snippets=saturation_snippets,
            author_companies=author_companies,
            post_hashtags=post_hashtags,
        )
        # Same skeleton across variants
        m = SAME_SKELETON_RE.match(comment.lower())
        if m:
            opener = m.group(0).strip()
            if opener in openers:
                fails.append("duplicate_skeleton_opener")
            openers.append(opener)
        results[label] = fails
    _lint_cross_variant_stale_insights(variants, results)
    _lint_cross_variant_refinement(variants, results)
    _lint_cross_variant_diversity(variants, results)
    return results


def repair_prompt(failures: list[str], comment: str, context: dict) -> str:
    label = (context.get("variant_label") or "A").upper()
    min_w = MIN_WORDS_E if label == "E" else MIN_WORDS
    their_cos = context.get("author_companies") or []
    return json.dumps({
        "task": (
            "Rewrite as client spokesperson — external peer, not the post author. "
            f"Fix failed checks. {min_w}–120 words. React to their post; use YOUR experience on client-site.example.com or my own site."
        ),
        "failed_checks": failures,
        "original_comment": comment,
        "opening_hook": (context.get("opening_hook") or "")[:400],
        "post_excerpt": (context.get("post_content") or "")[:600],
        "author_companies_not_yours": their_cos,
        "variant_label": label,
        "rules": [
            "Never write 'at [their company], we...'",
            "Do not quote their article headline or paste their hashtags",
            "Proof only in variant C",
            "No em dashes, no invented stats, no the client product product pitch",
        ],
        "output": {"comment": "rewritten comment only"},
    }, indent=2)


REPAIR_SYSTEM = """You fix LinkedIn room comments for client spokesperson.
Return JSON only: {"comment": "..."}.
External peer on someone else's thread — never claim their company as yours.
Target 50–120 words. Proof only in variant C."""


def repair_comment(
    api_key: str,
    model: str,
    comment: str,
    failures: list[str],
    context: dict,
    repair_fn: Callable[..., dict],
) -> str:
    user = repair_prompt(failures, comment, context)
    result = repair_fn(api_key, model, REPAIR_SYSTEM, user)
    return (result.get("comment") or "").strip()


def inspect_and_repair_variants(
    api_key: str,
    model: str,
    parsed: dict,
    context: dict,
    repair_fn: Callable[..., dict],
    *,
    max_attempts: int = MAX_REPAIR_ATTEMPTS,
) -> dict:
    """
    Lint all variants; repair hard failures via DeepSeek; re-lint until pass or max attempts.
    Soft diversity fails are reported but not rewritten (avoids clunky telegraphese).
    Adds inspection_report to parsed dict.
    """
    if not parsed.get("commentable", True):
        return parsed

    variants = list(parsed.get("variants") or [])
    post_content = context.get("post_content") or ""
    article_title = context.get("article_title") or ""
    saturation = context.get("saturation_snippets") or []
    report: dict[str, Any] = {"variants": {}, "repaired": [], "still_failing": []}

    for attempt in range(max_attempts + 1):
        lint_results = lint_variants(
            variants,
            post_content=post_content,
            article_title=article_title,
            saturation_snippets=saturation,
            author_companies=context.get("author_companies"),
            post_hashtags=context.get("post_hashtags"),
        )
        any_hard_fail = False
        for v in variants:
            label = (v.get("label") or "").strip().upper()
            fails = lint_results.get(label, [])
            hard = hard_lint_failures(fails)
            report["variants"][label] = {
                "failures": fails,
                "hard_failures": hard,
                "attempt": attempt,
            }
            if hard:
                any_hard_fail = True
                if attempt < max_attempts:
                    ctx = {
                        **context,
                        "variant_label": label,
                        "post_content": post_content,
                    }
                    try:
                        fixed = repair_comment(api_key, model, v.get("comment") or "", hard, ctx, repair_fn)
                        if fixed:
                            v["comment"] = sanitize_comment_text(fixed)
                            report["repaired"].append(label)
                    except Exception as e:
                        report["variants"][label]["repair_error"] = str(e)
        if not any_hard_fail:
            break

    for v in variants:
        if v.get("comment"):
            v["comment"] = sanitize_comment_text(v.get("comment") or "")

    final_lint = lint_variants(
        variants,
        post_content=post_content,
        article_title=article_title,
        saturation_snippets=saturation,
        author_companies=context.get("author_companies"),
        post_hashtags=context.get("post_hashtags"),
    )
    for label, fails in final_lint.items():
        hard = hard_lint_failures(fails)
        if hard:
            report["still_failing"].append({"label": label, "failures": hard})

    parsed["variants"] = variants
    parsed["inspection_report"] = report
    return parsed

def inspection_summary(report: dict) -> str:
    repaired = len(report.get("repaired") or [])
    failing = len(report.get("still_failing") or [])
    if failing:
        return f"inspector:repaired={repaired},still_failing={failing}"
    if repaired:
        return f"inspector:repaired={repaired},all_pass"
    return "inspector:all_pass"
