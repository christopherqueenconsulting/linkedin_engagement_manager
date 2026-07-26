"""The ONE alignment core shared by every generated content type (newsletters, posts, comments,
replies, seed comments, group posts). Voice comes from the durable profile synthesis, subject
steering from the user's engagement preferences (focus topics + goals), purpose from LEM's
relationship-building engagement philosophy, and the self-promo policy is expressed ONCE here:
a HARD no-self-promo guardrail for comments/posts, a LIGHT soft-promo allowance for the author's
own newsletter. Keeping all of this in one module is what stops the content types from drifting
out of alignment with each other over time."""

import json
import math
import os
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Optional

from cqc_lem.utilities.linkedin_formatter import normalize_public_text

if TYPE_CHECKING:
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile

# Engagement-optimized upper caps. MEDIUM is the default (issue #394): 2026 LinkedIn weights a
# substantive comment (≥15 words) ~2.5× a short one-liner, so the baseline aims for a real, specific
# reply that can earn a thread — not a throwaway. The ≥15-word target is steered via the prompt (see
# COMMENT_MIN_WORDS / style_directive), not enforced by post-generation validation; the char cap only
# bounds the top end so "long" doesn't become an essay.
COMMENT_LENGTH_CHARS = {"short": 180, "medium": 320, "long": 550}
# Substantive-length TARGET injected into the comment/reply prompt regardless of the length
# preference. This is prompt guidance (the model is told to meet it), not a runtime word-count gate.
COMMENT_MIN_WORDS = 15

# Hard guardrail attached to EVERY generated comment, reply, and post. Without it, a user whose
# profile / recent activity is dominated by a project they are building (e.g. their own internal
# tooling) makes the model pull that project in as the SUBJECT of otherwise unrelated content —
# the exact LEM-drift bug this guardrail prevents. Background is VOICE and credibility only.
NO_SELF_PROMO_GUARDRAIL = (
    "Never mention, name, promote, or allude to the user's own internal tools, apps, software, "
    "platforms, side projects, or anything they are personally building (including any product "
    "referred to as 'LEM' or the engagement platform itself). Treat the user's profile and "
    "background strictly as VOICE, TONE, and credibility — never as the subject matter, and never "
    "as something to advertise. Do not turn the output into self-promotion."
)

# Unlike comments/posts (which carry the HARD guardrail above), a newsletter is the author's OWN
# publication, so a LIGHT, occasional, SECONDARY mention of the tools/approach the author uses is
# acceptable when it genuinely serves the reader. The edition's SUBJECT and value must still stand
# on their own — never turn an edition into an ad, and never let a tool become the whole subject.
NEWSLETTER_SOFT_PROMO_NOTE = (
    "This is the author's OWN newsletter. A LIGHT, OCCASIONAL, SECONDARY mention of a tool, product, "
    "or approach the author uses is allowed when it genuinely helps the reader — but keep it brief and "
    "secondary. The edition's subject and value must stand on their own; never turn it into an ad and "
    "never let a product become the whole subject."
)

# Effective server-side DEFAULT when the user has NOT declared focus topics / goals. LEM's core
# engagement philosophy: every comment should build a relationship — connect genuinely to the POSTER
# (the author of the target post) or to POTENTIAL FOLLOWERS reading the thread — grounded in the
# post's actual topic and in the user's authentic voice. This makes blank-config generation produce
# aligned, relationship-building comments instead of unaligned or self-referential ones.
DEFAULT_ENGAGEMENT_INTENTION = (
    "Build a genuine relationship: every comment should draw a real connection either to the POSTER "
    "(the author of this post) or to POTENTIAL FOLLOWERS reading the thread — start a conversation "
    "worth replying to, grounded in the post's actual topic and written in the user's authentic voice."
)

# LEM's engagement PURPOSE, phrased per content type and consumable by every generator, so what the
# system is FOR (relationships + profile engagement, not broadcast) is stated once and everywhere.
_ENGAGEMENT_PURPOSE = {
    "comment": ("Purpose: earn a REPLY and build a relationship — comment threads (back-and-forth "
                "conversation) drive far more reach than likes, so start a conversation, never "
                "deliver a monologue."),
    "post": ("Purpose: start conversations that pull readers to the author's profile — a post that "
             "sparks a few real comment threads beats one that collects passive likes, so invite "
             "genuine discussion and make the post worth saving."),
    "newsletter": ("Purpose: deliver enough real value that subscribers reply, comment, and share — "
                   "each edition should deepen the author-reader relationship, not just broadcast."),
}


def engagement_purpose(content_type: str) -> str:
    return _ENGAGEMENT_PURPOSE.get(content_type, _ENGAGEMENT_PURPOSE["post"])


def promo_policy(content_type: str) -> str:
    """The self-promo policy line for a content type — the HARD guardrail everywhere except the
    author's own newsletter, which gets the light soft-promo allowance."""
    return NEWSLETTER_SOFT_PROMO_NOTE if content_type == "newsletter" else NO_SELF_PROMO_GUARDRAIL


# --- 70/20/10 content mix governor (issue #618) ---------------------------------------------------
# The July 2026 audit found nearly every planned post selling the diagnostic conversation, and 2026
# demotes salesy content by up to -70% reach. Winning creators run the Hills 70/20/10 split, so the
# content plan now CLASSIFIES every post and the classification steers generation:
#   value     (70%) — audience-value/awareness content, zero selling
#   authority (20%) — teach the author's expertise, still zero selling
#   promo     (10%) — the ONE in ten soft-promo slot, case-study shaped and no-pressure framed
# The class is assigned in code (no LLM), persisted on posts.content_mix, and reported back as the
# mix-compliance ratio on the analytics dashboard. Defined HERE (not db.py) because it is the
# structural half of the promo policy above — db.py just stores the string, as it does buyer_stage.
class ContentMix(StrEnum):
    VALUE = "value"
    AUTHORITY = "authority"
    PROMO = "promo"


CONTENT_MIX_TARGET: dict = {
    ContentMix.VALUE.value: 0.70,
    ContentMix.AUTHORITY.value: 0.20,
    ContentMix.PROMO.value: 0.10,
}

# The ceiling compliance is measured against — a plan is out of policy the moment promo posts pass it.
PROMO_MAX_RATIO = 0.10

# One promo post per N planned posts. The audit's band is "≤1 per 7-10 posts"; we take the SPARSE end
# as the floor (1-in-10 = exactly PROMO_MAX_RATIO) because a 1-in-7 cadence would put promo at ~14% —
# over the ceiling the same audit sets. Ops can only make promo RARER, never denser.
PROMO_EVERY_N_POSTS_DEFAULT = 10
PROMO_EVERY_N_POSTS_MIN = 10
PROMO_EVERY_N_POSTS_MAX = 30

# Authority education lands on 1-in-5 posts (20%), phased so it can never collide with the 1-in-10
# promo slot (promo indices are ≡9 mod 10, authority ≡2 and ≡7) — leaving 70% value by construction.
_AUTHORITY_EVERY_N = 5
_AUTHORITY_PHASE = 2


def promo_every_n(every_n: Optional[int] = None) -> int:
    """The promo cadence actually used, clamped so the governor can never exceed PROMO_MAX_RATIO.
    Read at call time (the POST_SIMILARITY_MAX live-env pattern) so ops can dial promo down without
    a restart."""
    raw = every_n if every_n is not None else os.getenv("PROMO_EVERY_N_POSTS")
    try:
        n = int(raw) if raw not in (None, "") else PROMO_EVERY_N_POSTS_DEFAULT
    except (TypeError, ValueError):
        return PROMO_EVERY_N_POSTS_DEFAULT
    return min(PROMO_EVERY_N_POSTS_MAX, max(PROMO_EVERY_N_POSTS_MIN, n))


def normalize_content_mix(value) -> Optional[str]:
    """Coerce a stored/supplied mix class to a known value; None for anything unrecognized (a legacy
    post with no class must never be treated as promo)."""
    key = str(value or "").strip().lower()
    return key if key in CONTENT_MIX_TARGET else None


def assign_content_mix(count: int, offset: int = 0, every_n: Optional[int] = None) -> list:
    """The GOVERNOR: deterministic 70/20/10 class for each of `count` consecutively planned posts.
    `offset` continues the rotation across plans (pass the user's existing post count) so a new plan
    can't restart the cadence and land two promo posts back to back. Promo wins any collision, so the
    promo cadence is exact and authority absorbs the rounding."""
    n = promo_every_n(every_n)
    out = []
    for i in range(max(0, int(count or 0))):
        seq = int(offset or 0) + i
        if seq % n == n - 1:
            out.append(ContentMix.PROMO.value)
        elif seq % _AUTHORITY_EVERY_N == _AUTHORITY_PHASE:
            out.append(ContentMix.AUTHORITY.value)
        else:
            out.append(ContentMix.VALUE.value)
    return out


_CONTENT_MIX_GUIDANCE: dict = {
    ContentMix.VALUE.value: (
        "MIX CLASS: AUDIENCE VALUE (70% of this author's plan). This post exists to be genuinely "
        "useful or worth talking about for the reader ALONE. Sell nothing: no offer, no services, no "
        "availability, no 'here's how I help', no pitch of any kind — not even a soft one."),
    ContentMix.AUTHORITY.value: (
        "MIX CLASS: AUTHORITY EDUCATION (20% of this author's plan). Teach one thing the author knows "
        "from doing the work, in enough depth that the reader could act on it. Expertise is shown by "
        "the TEACHING — still sell nothing: no offer, no services, no availability, no pitch."),
    ContentMix.PROMO.value: (
        "MIX CLASS: SOFT PROMO (the single allowed promo slot in ten posts). Shape it as a CASE STUDY, "
        "never an ad: the client/project is the hero, the author is the guide — problem, what was "
        "actually done, and the outcome with one specific real number or observable result (never an "
        "invented figure). Frame it explicitly NO-PRESSURE (e.g. it may not be a fit for everyone, "
        "and the reader is welcome to just take the takeaway). No urgency, no scarcity, no hard sell, "
        "and no meeting ask of any kind."),
}


def mix_directive(content_mix) -> str:
    """The mix-class rules injected into a POST prompt, plus the artifact-CTA policy the promo slot
    makes load-bearing. Returns "" for an unclassified post so callers can append unconditionally
    (behavior for legacy/manual posts is unchanged)."""
    mix = normalize_content_mix(content_mix)
    if not mix:
        return ""
    return ("\n\nContent mix (70/20/10 governor — this post's role in the author's plan):\n- "
            + _CONTENT_MIX_GUIDANCE[mix] + "\n- " + ARTIFACT_CTA_POLICY + "\n")


def content_mix_compliance(counts: Optional[dict]) -> dict:
    """Mix-compliance summary for the analytics dashboard (issue #395 surface): the classified counts,
    their ratios, the 70/20/10 target, and whether promo is inside the ceiling. Unclassified posts are
    reported but kept OUT of the ratios so legacy posts don't dilute the measurement."""
    counts = counts or {}
    classified = {k: int(counts.get(k) or 0) for k in CONTENT_MIX_TARGET}
    unclassified = int(counts.get("unclassified") or 0)
    total = sum(classified.values())
    ratios = {k: (v / total if total else None) for k, v in classified.items()}
    promo_ratio = ratios[ContentMix.PROMO.value]
    return {
        "counts": {**classified, "unclassified": unclassified},
        "total": total,
        "ratios": ratios,
        "target": dict(CONTENT_MIX_TARGET),
        "promo_max_ratio": PROMO_MAX_RATIO,
        "promo_every_n": promo_every_n(),
        # No classified posts yet = nothing out of policy (an empty plan is not a violation).
        "compliant": True if promo_ratio is None else promo_ratio <= PROMO_MAX_RATIO + 1e-9,
    }


# --- Artifact CTAs, never a meeting ask (issue #618) ----------------------------------------------
# The audit's other half: every promo CTA in the wild was a meeting ask ("book a call", "DM me to
# discuss"), which is exactly what 2026 demotes. A compliant offer is always an ARTIFACT the reader
# gets without talking to anyone — the user's configured lead magnet or their newsletter. This is the
# ONE definition of that policy: the prompt-side ban, the detector, and the deterministic repair.
ARTIFACT_CTA_POLICY = (
    "NEVER ask for a meeting: no 'book a call', 'schedule a call/demo/consult', 'let's set up time', "
    "'hop on a quick call', calendar links, and no 'DM me to discuss'. If this post offers anything at "
    "all it must be an ARTIFACT the reader can get without talking to anyone (a guide, template, "
    "checklist, or the author's newsletter). Otherwise close on the assigned conversation CTA."
)

# Precision over recall, the _SOFT_OFFER_RE philosophy: each pattern needs explicit meeting intent, so
# ordinary prose ("we scheduled the migration", "let's talk about why this matters") is never flagged.
_MEETING_ASK_PATTERNS: tuple = (
    r"\bbook(?:ing)?\s+(?:a|your|some|my)\s+(?:call|time|slot|demo|meeting|chat|consult\w*|session|intro\w*)",
    r"\bschedul(?:e|ing)\s+(?:a|your|some|our)\s+(?:call|time|demo|meeting|chat|consult\w*|session|intro\w*)",
    r"\b(?:hop|jump|get)\s+on\s+a\s+(?:quick\s+)?(?:call|zoom|chat|huddle)",
    r"\bset\s+up\s+(?:a|some)\s+(?:call|time|meeting|chat|demo|consult\w*)",
    r"\blet'?s\s+(?:set\s+up|schedule|book|find|grab)\s+(?:a|some)?\s*(?:call|time|chat|coffee|meeting)",
    # Offer-verb context is REQUIRED: a bare "discovery call" / "strategy session" noun phrase also
    # appears in ordinary first-person narrative ("I ran a discovery call last week" — often the
    # story-bank anecdote itself), and the repair DELETES matching sentences, so flagging the noun
    # phrase alone would silently destroy real story content.
    r"\b(?:grab|claim|snag|book|schedule|join|reserve|get|offer(?:ing)?)\s+(?:a|your|my|this|some)\s+"
    r"(?:(?:free|quick|short|discovery|intro(?:ductory)?|strategy|\d{1,2}[- ]min(?:ute)?)\s+)+"
    r"(?:call|consult\w*|session|chat|demo)\b",
    r"\b(?:calendly|savvycal|my\s+calendar\s+link|calendar\s+link|link\s+to\s+my\s+calendar)\b",
    r"\bdm\s+me\s+(?:to|if\s+you\s+want\s+to|and\s+we(?:'ll|\s+will|\s+can))\s+"
    r"(?:discuss|chat|talk|connect|explore|scope|walk)",
    r"\breach\s+out\s+(?:to\s+me\s+)?(?:to|and\s+we\s+can)\s+(?:discuss|chat|talk|book|schedule|scope)",
    r"\bslots?\s+(?:open|available)\s+(?:this|next)\s+\w+",
)
_MEETING_ASK_RE = re.compile("|".join(_MEETING_ASK_PATTERNS), re.IGNORECASE)


def meeting_ask_excerpts(content: Optional[str]) -> list:
    """The literal meeting-ask phrases found, for the review-queue finding's details."""
    if not content:
        return []
    seen = []
    for match in _MEETING_ASK_RE.findall(content):
        phrase = (match if isinstance(match, str) else next((m for m in match if m), "")).strip()
        if phrase and phrase.lower() not in [s.lower() for s in seen]:
            seen.append(phrase)
    return seen


def contains_meeting_ask(content: Optional[str]) -> bool:
    """True when the content closes on a meeting/call ask — the CTA shape the 70/20/10 policy bans."""
    return bool(content) and bool(_MEETING_ASK_RE.search(content))


def artifact_cta_line(lead_magnet: Optional[dict] = None, newsletter: Optional[dict] = None,
                      post_id: Optional[int] = None, use_emojis: bool = False) -> str:
    """The replacement CTA, routed to an asset the user ACTUALLY has: their configured lead magnet
    first (the comment-keyword mechanic the automation already delivers on), else their newsletter.
    Returns "" when the user has neither — we drop the banned ask rather than invent an asset."""
    if lead_magnet_enabled(lead_magnet):
        idx = (int(post_id or 0)) % len(LEAD_MAGNET_CTA_REPAIR_MENU)
        line = LEAD_MAGNET_CTA_REPAIR_MENU[idx].format(
            keyword=str(lead_magnet.get("keyword")).strip(), resource=_resource_label(lead_magnet))
        return line + (" " + _CTA_REPAIR_EMOJI if use_emojis else "")
    if newsletter and newsletter.get("enabled"):
        title = str(newsletter.get("title") or "").strip()
        named = f"my newsletter, {title}," if title else "my newsletter"
        return (f"I break this kind of thing down in more depth in {named} "
                "— subscribe if it would help.")
    return ""


def replace_meeting_ask_cta(content: Optional[str], lead_magnet: Optional[dict] = None,
                            newsletter: Optional[dict] = None, post_id: Optional[int] = None,
                            use_emojis: bool = False) -> Optional[str]:
    """Deterministic repair (no LLM) for a draft that came back with a banned meeting ask: drop the
    sentences carrying it and, when the user has a real artifact to point at, close on that instead.
    Returns the content byte-identical when there was no meeting ask to remove."""
    if not contains_meeting_ask(content):
        return content
    out_lines = []
    for raw_line in content.split("\n"):
        kept = [s for s in _SENTENCE_SPLIT_RE.split(raw_line)
                if not s.strip() or not _MEETING_ASK_RE.search(s)]
        line = " ".join(s for s in kept if s).rstrip()
        if line or not raw_line.strip():
            out_lines.append(line if raw_line.strip() else raw_line)
    stripped = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).rstrip()
    replacement = artifact_cta_line(lead_magnet, newsletter, post_id, use_emojis)
    # An already-working keyword ask means the artifact CTA is present — appending would double it.
    if replacement and not has_lead_magnet_cta_mechanic(
            stripped, (lead_magnet or {}).get("keyword")):
        stripped = stripped + "\n\n" + replacement
    return normalize_public_text(stripped)


def style_directive(prefs: dict = None, content_type: str = "comment") -> str:
    """Turn the user's engagement preferences into an explicit style directive that overrides
    the profile-inferred defaults (tone, length, emoji/hashtag rules, freeform style). The
    comment-length cap only applies to comments — posts and newsletters carry their own length
    guidance in their prompts."""
    from cqc_lem.utilities.ai.content_framework import hashtag_directive
    if not prefs:
        return ""
    parts = []
    tone = prefs.get("tone")
    if tone:
        parts.append(f"Write in a {tone} tone.")
    if content_type == "comment":
        length = prefs.get("comment_length") or "medium"
        parts.append(f"Write a substantive comment of at least {COMMENT_MIN_WORDS} words — add a specific "
                     f"insight, example, or genuine question that invites a reply; never a generic "
                     f"one-liner like \"Great post!\". Keep it {length}: up to "
                     f"~{COMMENT_LENGTH_CHARS.get(length, 320)} characters.")
    parts.append("You may use one tasteful emoji." if prefs.get("use_emojis") else "Do not use emojis.")
    parts.append(hashtag_directive(prefs))
    if prefs.get("comment_style"):
        parts.append(f"Style guidance: {prefs['comment_style']}.")
    return "\n\nStyle requirements (follow these):\n- " + "\n- ".join(parts) + "\n"


def focus_directive(prefs: dict = None) -> str:
    """Soft SUBJECT steering from the user's declared focus topics + business/personal goals. It is
    used only to choose which ANGLE to take when it genuinely fits — it must never override the
    actual subject (the target post for comments, the chosen industry/story for posts). Returns ""
    when nothing is declared (callers supply their own baseline)."""
    if not prefs:
        return ""
    parts = []
    topics = [str(t).strip() for t in (prefs.get("focus_topics") or []) if str(t).strip()]
    if topics:
        parts.append(f"Focus topics the user wants to be known for: {', '.join(topics)}.")
    business = (prefs.get("business_goals") or "").strip()
    if business:
        parts.append(f"Business goals: {business}.")
    personal = (prefs.get("personal_goals") or "").strip()
    if personal:
        parts.append(f"Personal goals: {personal}.")
    if not parts:
        return ""
    return ("\n\nSoft steering (use ONLY to choose the angle when it genuinely fits the subject; "
            "never force it in and never let it change the subject):\n- " + "\n- ".join(parts) + "\n")


def _focus_topics(prefs: dict = None) -> list:
    return [str(t).strip() for t in ((prefs or {}).get("focus_topics") or []) if str(t).strip()]


def select_focus_topic(prefs: dict = None, sequence_index: Optional[int] = None,
                       profile: "Optional[LinkedInProfile]" = None) -> Optional[str]:
    """The SUBJECT anchor for one trend-based post: rotate deterministically across the user's
    declared focus topics (keyed off a stable per-post integer — the post id — the same way the
    lead-magnet CTA rotation works) so anchoring never collapses every post onto one topic. Without
    a sequence key it deterministically falls back to the FIRST topic — reproducible and testable,
    no per-call randomness. When the user declared NO focus topics, fall back to on-niche anchors
    derived from `profile` (Topic-DNA steering, issue #384) so subjects still stay on-niche; with no
    usable profile anchors either, returns None and callers keep their profile-industry-only
    behavior."""
    topics = _focus_topics(prefs)
    if not topics and profile is not None:
        topics = profile_niche_anchors(profile)
    if not topics:
        return None
    if sequence_index is None:
        return topics[0]
    return topics[int(sequence_index) % len(topics)]


def content_matches_focus(content: str, focus_topics: list, subject: str = None) -> bool:
    """Cheap deterministic alignment heuristic — NO LLM call: does the content plausibly relate to
    at least one declared focus topic (or the post's assigned `subject`)? A topic matches when at
    least half of its meaningful tokens appear in the content. Empty focus list (or no usable topic
    tokens) is a no-op: True."""
    from cqc_lem.utilities.ai.content_framework import content_tokens
    candidates = [str(t) for t in (focus_topics or [])]
    if subject:
        candidates.append(str(subject))
    ctokens = content_tokens(content)
    checked_any = False
    for cand in candidates:
        needed = content_tokens(cand)
        if not needed:
            continue
        checked_any = True
        hits = len(needed & ctokens)
        if hits >= max(1, math.ceil(len(needed) / 2)):
            return True
    return not checked_any


# ---------------------------------------------------------------------------
# Topic Authority (Topic DNA) governor — issue #384. 2026 LinkedIn ranking (per 360Brew) derives a
# 'Topic DNA' from the author's headline / about / posting history and SUPPRESSES off-niche posts, so
# profile↔content consistency is now a ranking input. This is the deterministic, NO-LLM measure of
# how tightly a draft sits inside the user's niche vocabulary (declared focus topics PLUS the profile
# headline/about), plus the profile-derived subject steering that keeps drafts on-niche in the first
# place. It reuses content_framework.content_tokens so it stays aligned with the similarity engine.
# ---------------------------------------------------------------------------

# Minimum topic-authority score below which a draft reads as off-niche and gets flagged/steered.
# 0.15 is deliberately lenient — the governor is meant to catch clearly off-niche drafts (which score
# ~0.0) without punishing an on-niche post that spends most of its words on connective prose. Override
# per-deploy with TOPIC_AUTHORITY_MIN (same live-env pattern as POST_SIMILARITY_MAX).
TOPIC_AUTHORITY_MIN_DEFAULT = 0.15


def topic_authority_min() -> float:
    """The off-niche threshold, read at call time so ops/tests can tune TOPIC_AUTHORITY_MIN without a
    restart (the POST_SIMILARITY_MAX / research-toggle live-env pattern)."""
    raw = (os.environ.get("TOPIC_AUTHORITY_MIN") or "").strip()
    if not raw:
        return TOPIC_AUTHORITY_MIN_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return TOPIC_AUTHORITY_MIN_DEFAULT
    # Reject nan/inf and out-of-range values — score is always in [0, 1], so a non-finite or
    # out-of-band threshold would make every comparison misbehave (e.g. score >= nan is always False).
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return TOPIC_AUTHORITY_MIN_DEFAULT
    return value


def topic_dna_tokens(focus_topics: list = None, headline: str = None, about: str = None) -> set:
    """The user's 'Topic DNA' as a meaningful-token set: the union of their declared focus topics and
    the vocabulary of their profile headline + about. Stopwords / 1-char noise are dropped by
    content_tokens so only niche-bearing words remain."""
    from cqc_lem.utilities.ai.content_framework import content_tokens
    dna: set = set()
    for t in (focus_topics or []):
        dna |= content_tokens(str(t))
    dna |= content_tokens(headline or "")
    dna |= content_tokens(about or "")
    return dna


def topic_authority_score(content: str, focus_topics: list = None, headline: str = None,
                          about: str = None) -> float:
    """0.0–1.0 consistency of a draft with the user's Topic DNA (focus topics + profile headline/about
    vocabulary). Uses the same deterministic token-set OVERLAP COEFFICIENT as
    content_framework.text_similarity — |content∩dna| / min(|content|,|dna|) — so a short, tightly
    on-niche post scores high and an off-niche post scores ~0. Returns 1.0 (a no-op, never flagged)
    when there is no Topic DNA to judge against or the content has no scorable tokens, mirroring
    content_matches_focus's empty-input behavior. NO LLM call."""
    from cqc_lem.utilities.ai.content_framework import content_tokens
    dna = topic_dna_tokens(focus_topics, headline, about)
    ctokens = content_tokens(content or "")
    if not dna or not ctokens:
        return 1.0
    return len(ctokens & dna) / min(len(ctokens), len(dna))


def is_on_niche(content: str, focus_topics: list = None, headline: str = None, about: str = None,
                threshold: Optional[float] = None) -> bool:
    """True when the draft's topic-authority score clears the off-niche threshold (defaults to
    topic_authority_min()). The boolean companion to topic_authority_score for gate/flag callers."""
    t = topic_authority_min() if threshold is None else threshold
    return topic_authority_score(content, focus_topics, headline, about) >= t


def _typed_terms(values, limit: Optional[int] = None) -> list:
    """Ordered, de-duplicated, whitespace-trimmed niche terms from a profile field. Only genuine
    strings (or objects exposing a string `.name`, e.g. LinkedInSkill) are kept, so a MagicMock or a
    stray non-string never leaks in as a bogus term. A non-sequence input (e.g. a MagicMock profile
    field) is treated as empty rather than raising."""
    if not isinstance(values, (list, tuple)):
        return []
    out, seen = [], set()
    for v in values:
        name = v if isinstance(v, str) else getattr(v, "name", None)
        if not isinstance(name, str) or not name.strip():
            continue
        key = name.strip().lower()
        if key not in seen:
            seen.add(key)
            out.append(name.strip())
        if limit is not None and len(out) >= limit:
            break
    return out


def profile_topic_dna(profile: "Optional[LinkedInProfile]" = None,
                      profile_synthesis: Optional[str] = None) -> tuple:
    """Assemble the (headline, about) Topic-DNA strings from a LinkedInProfile for the scorer:
    HEADLINE ≈ job title + industry (the LinkedIn 'headline' analogue LEM stores), ABOUT ≈ the durable
    voice/credibility synthesis brief plus the profile's listed skills (the 'about'/expertise
    analogue). Every field is optional and defensively typed, so a partial or mock profile yields
    ('', '') rather than raising. NO LLM call."""
    headline_parts = _typed_terms(
        [getattr(profile, "job_title", None), getattr(profile, "industry", None)])
    about_parts = []
    if isinstance(profile_synthesis, str) and profile_synthesis.strip():
        about_parts.append(profile_synthesis.strip())
    about_parts.extend(_typed_terms(getattr(profile, "skills", None)))
    return " ".join(headline_parts), " ".join(about_parts)


def profile_niche_anchors(profile: "Optional[LinkedInProfile]" = None) -> list:
    """Fallback Topic-DNA subject anchors derived from a profile when the user declared NO focus
    topics — this is the on-niche subject STEERING (issue #384). Prefers explicit niche signals (the
    listed skills) over the raw job title so the anchor is a TOPIC, not a role, and deliberately omits
    the industry (which trend subjects already carry) to avoid an '{industry} in the {industry}'
    tautology. Returns [] for a missing/mock profile, so callers fall back to their prior
    profile-industry-only behavior unchanged."""
    if profile is None:
        return []
    return _typed_terms(getattr(profile, "skills", None), limit=5)


def intention_directive(prefs: dict = None) -> str:
    """Engagement steering for comments/replies/seed comments. ALWAYS states LEM's baseline
    relationship-building intention (the effective default that works with everything left blank);
    when the user has declared focus topics / goals, those are LAYERED on top to refine the angle —
    they refine, not replace, the baseline, and win only where they directly conflict with it."""
    directive = ("\n\nEngagement intention (baseline — always applies):\n- "
                 + DEFAULT_ENGAGEMENT_INTENTION + "\n")
    focus = focus_directive(prefs)
    if focus:
        directive += ("\nLayer the user's declared focus on top of that baseline when it genuinely "
                      "fits (their stated goals take precedence only if they directly conflict):" + focus)
    return directive


def alignment_directive(prefs: dict = None, lead_magnet_cta: str = "",
                        content_mix=None) -> str:
    """Anti-self-promo guardrail + focus/goal steering, appended to POST prompts so generated posts
    stay aligned to the user's real business/personal goals instead of drifting into promoting
    whatever the user happens to be building right now. `lead_magnet_cta` (built by
    lead_magnet_cta_directive) is the ONE sanctioned exception to the guardrail and is appended
    only for the posts the rotation selects — see should_include_lead_magnet_cta. `content_mix` is
    this post's 70/20/10 class from the content-plan governor (issue #618); unclassified posts add
    nothing."""
    return ("\n\nContent alignment rules:\n- " + NO_SELF_PROMO_GUARDRAIL
            + "\n- " + engagement_purpose("post") + focus_directive(prefs)
            + mix_directive(content_mix)
            + (lead_magnet_cta or ""))


# The lead-magnet soft-ask ("comment KEYWORD and I'll DM it to you") is the compliant way to share a
# resource on LinkedIn — links in the post body get down-ranked, and it's what fires the keyword
# listener in run_automation. But it must NOT appear on every post: a repeated CTA reads as spam and
# gets pattern-flagged. So it rides a deterministic 1-in-N rotation (default N=3, env-overridable).
LEAD_MAGNET_CTA_EVERY_N = int(os.getenv("LEAD_MAGNET_CTA_EVERY_N", "3") or "3")
_DEFAULT_CTA_EVERY_N = 3


def _effective_cta_every_n(every_n: Optional[int]) -> int:
    """The 1-in-N cadence actually used. A misconfigured n < 1 (e.g. LEAD_MAGNET_CTA_EVERY_N=0)
    must NEVER mean 'every post' — fall back to the default cadence. n == 1 stays a valid explicit
    operator choice meaning every post."""
    try:
        n = int(every_n) if every_n is not None else int(LEAD_MAGNET_CTA_EVERY_N)
    except (TypeError, ValueError):
        return _DEFAULT_CTA_EVERY_N
    return n if n >= 1 else _DEFAULT_CTA_EVERY_N


def lead_magnet_enabled(lead_magnet: Optional[dict]) -> bool:
    """The lead magnet is usable only when the user turned it ON and gave BOTH a non-empty trigger
    keyword AND a non-empty message — the automation DM dispatch gate requires all three, so a
    keyword-only config would invite comments that never get a DM."""
    return bool(lead_magnet and lead_magnet.get("enabled")
                and str(lead_magnet.get("keyword") or "").strip()
                and str(lead_magnet.get("message") or "").strip())


def should_include_lead_magnet_cta(lead_magnet: Optional[dict], sequence_index: Optional[int],
                                   every_n: Optional[int] = None) -> bool:
    """Deterministic 1-in-N selection: the soft-ask goes on SOME posts, never all. `sequence_index`
    is any stable per-post integer (the post id works) so the choice is reproducible and testable —
    no per-call randomness. Returns False when the lead magnet is off/unkeyworded or no index is
    available."""
    if not lead_magnet_enabled(lead_magnet) or sequence_index is None:
        return False
    n = _effective_cta_every_n(every_n)
    if n == 1:
        return True
    return int(sequence_index) % n == 0


def lead_magnet_cta_directive(lead_magnet: Optional[dict], include: bool) -> str:
    """The SANCTIONED lead-magnet CTA line appended to a post prompt. This is the ONE allowed
    exception to NO_SELF_PROMO_GUARDRAIL because it is the user's OWN explicitly-configured offer.
    Woven in the user's voice by the model, references the ACTUAL trigger keyword, and describes the
    resource's value. Returns "" when not selected or when the lead magnet is off — so callers can
    always append it unconditionally."""
    if not include or not lead_magnet_enabled(lead_magnet):
        return ""
    keyword = str(lead_magnet.get("keyword")).strip()
    resource_context = str(lead_magnet.get("message") or "").strip()[:240]
    context_line = (f"\n- For context, the resource being offered is described as: "
                    f"\"{resource_context}\". Convey its value in one plainspoken sentence."
                    if resource_context else "")
    return (
        "\n\nSANCTIONED lead-magnet call-to-action (this post ONLY — the user has explicitly "
        "configured this offer, so it OVERRIDES the no-self-promo guardrail for THIS CTA):\n"
        f"- REQUIRED: end the post with ONE short line that literally asks readers to comment the "
        f"exact word \"{keyword}\" to receive the resource by DM. The words 'comment' and "
        f"\"{keyword}\" must BOTH appear in that line — e.g. 'Comment {keyword} and I'll DM it to "
        "you.'\n"
        "- Do NOT paraphrase the mechanic: 'reach out', 'DM me', 'message me', 'send me a note', or "
        "a link are NOT acceptable substitutes — an automation watches the comments for that exact "
        "word, so a paraphrase breaks delivery.\n"
        "- Write it in the user's own voice — plainspoken, no hype, no hard sell, and NO link in the "
        "post body (the DM delivers it). Honor the user's emoji/hashtag settings.\n"
        "- Keep it to exactly ONE ask; do NOT also add softer offers of the same resource, and no "
        "engagement-bait phrasing (no 'tag a friend', no 'like if')." + context_line + "\n")


def lead_magnet_preserve_note(keyword: Optional[str]) -> str:
    """Preservation instruction for the downstream REWRITE passes (refinement / hook optimization).
    Those prompts carry their own anti-engagement-bait rules, which is exactly what rewrites
    'comment KEYWORD' into 'reach out for...' — this note carves out the sanctioned CTA so the
    mechanic survives. Empty string when no keyword, so callers can append unconditionally."""
    keyword = str(keyword or "").strip()
    if not keyword:
        return ""
    return (
        f"\n\nREQUIRED CTA — PRESERVE: the draft ends with a line asking readers to comment the "
        f"exact word \"{keyword}\" to receive a resource by DM. That line is a sanctioned, "
        "user-configured call-to-action, NOT engagement-bait: keep it (verbatim or lightly "
        "polished), never paraphrase it into 'reach out'/'DM me'/'message me', never remove the "
        f"word \"{keyword}\" or the word 'comment' from it, and keep exactly ONE such ask.")


# The prompt directive above is only a REQUEST — the downstream refinement passes (LLM rewrites +
# bait strip) can reword "comment AUDIT" into "reach out for..." or drop it entirely, and then the
# comment-keyword DM listener never fires. The menu below is the deterministic REPAIR: a small set
# of phrasings picked by post_id so repaired CTAs across a user's feed are not word-for-word
# identical. Every variant must (a) carry the exact keyword verbatim, (b) say it's DM'd after they
# comment, (c) carry no link/hashtag, and (d) never trip strip_engagement_bait's _BAIT_PATTERNS.
LEAD_MAGNET_CTA_REPAIR_MENU = (
    "Want {resource}? Comment {keyword} and I'll DM it to you.",
    "If you'd like {resource}, comment {keyword} below and I'll send it your way by DM.",
    "Comment {keyword} and I'll DM you {resource}.",
    "Drop {keyword} in the comments and I'll DM you {resource}.",
    "Curious? Comment {keyword} and I'll send {resource} straight to your DMs.",
    "Comment {keyword} on this post and I'll DM {resource} over to you.",
)

# Envelope (mail = DM delivery) — deliberately a BMP character: ChromeDriver send_keys throws on
# non-BMP emoji, so the repair line must never depend on downstream stripping to post cleanly.
_CTA_REPAIR_EMOJI = "✉️"

# Messages that read as full sentences ("I made a checklist that...") don't compress into a noun
# label — fall back to the generic "the resource" instead of producing garbled grammar.
_LABEL_SENTENCE_STARTS = {"i", "i'll", "i'd", "i've", "we", "we'll", "we've", "you", "it", "this is"}
_LABEL_DETERMINERS = {"a", "an", "the", "my", "our", "your", "this"}


def _resource_label(lead_magnet: Optional[dict]) -> str:
    """A few plain words describing the offered resource, derived from the configured lead-magnet
    message; generic 'the resource' when the message is missing, sentence-shaped, or too long. Links,
    hashtags, and emoji are stripped so the repair line's own constraints can't be violated."""
    msg = str((lead_magnet or {}).get("message") or "").strip()
    msg = re.sub(r"https?://\S+|www\.\S+", "", msg)
    msg = re.sub(r"#\S+", "", msg)
    first = msg.split("\n")[0].strip().strip("\"'").rstrip(".!?").strip()
    # Drop emoji/symbols so the appended line keeps its own one-emoji budget.
    first = "".join(c for c in first if ord(c) <= 0xFFFF and not (0x2190 <= ord(c) <= 0x2BFF))
    words = first.split()
    # Check both one- and two-word sentence starts ("this is ...") — a single-word check can never
    # match the multi-word entries in _LABEL_SENTENCE_STARTS.
    leading = {words[0].lower(), " ".join(w.lower() for w in words[:2])} if words else set()
    if not words or (leading & _LABEL_SENTENCE_STARTS) or len(words) > 10:
        return "the resource"
    label = " ".join(words)
    if words[0].lower() in _LABEL_DETERMINERS:
        return label[0].lower() + label[1:]
    # No leading determiner — "I'll DM you my free checklist" reads naturally, bare noun doesn't.
    return "my " + label


def _cta_mechanic_re(keyword: str) -> re.Pattern:
    """Matches a FUNCTIONING comment-keyword ask — the keyword near the word 'comment' within one
    sentence — while rejecting incidental keyword mentions. The (?<![\\w-]) / (?![\\w-]) guards
    reject hyphenated compounds like 'bias-audit', and 'comment on/about X' is excluded because it
    means discussing X, not typing the keyword."""
    kw = re.escape(keyword)
    kw_pat = rf"(?<![\w-])[\"']?{kw}[\"']?(?![\w-])"
    ask = rf"\bcomment(?:s|ing|ed)?\b(?!\s+(?:on|about)\b)[^.\n!?]{{0,40}}{kw_pat}"
    drop = rf"{kw_pat}[^.\n!?]{{0,40}}\bcomments?\b"
    return re.compile(rf"(?:{ask})|(?:{drop})", re.IGNORECASE)


def has_lead_magnet_cta_mechanic(content: Optional[str], keyword: Optional[str]) -> bool:
    """True when the content still asks readers to COMMENT the trigger keyword (the mechanic the
    automation's keyword listener needs). Case-insensitive; incidental keyword mentions don't count."""
    keyword = str(keyword or "").strip()
    if not content or not keyword:
        return False
    return bool(_cta_mechanic_re(keyword).search(content))


# A "soft offer" is the model's PARAPHRASED version of the lead-magnet CTA — an offer to send the
# resource without the comment-keyword mechanic ("Feel free to reach out if you'd like a
# checklist..."). Left in place next to the appended repair line, it reads as a doubled ask. Both
# halves are required for a strip (offer verb + resource noun) so ordinary sentences like "Reach
# out to your vendor" or "Here's a checklist" are never touched — precision over recall: a missed
# soft line is cosmetic, an over-strip loses real content.
_SOFT_OFFER_VERBS = (r"(?:feel free to )?reach out|dm me|message me|send me a (?:dm|message|note)|"
                     r"i(?:'|’)ll (?:send|share|dm)|happy to (?:send|share|dm|walk)")
_SOFT_OFFER_NOUNS = (r"checklist|guide|template|playbook|worksheet|resource|framework|cheat ?sheet|"
                     r"ebook|e-book|toolkit|audit|scorecard|assessment|list|copy")
_SOFT_OFFER_RE = re.compile(
    rf"(?=.*\b(?:{_SOFT_OFFER_VERBS})\b)(?=.*\b(?:{_SOFT_OFFER_NOUNS})\b)", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _strip_soft_resource_offers(content: str, keyword: str) -> str:
    """Remove sentences that soft-paraphrase the lead-magnet offer (offer verb + resource noun)
    WITHOUT the comment-keyword mechanic — so the deterministic repair line never ships alongside
    the model's mangled version of the same ask. Sentences carrying the real mechanic are kept."""
    mech = _cta_mechanic_re(keyword)
    out_lines = []
    for raw_line in content.split("\n"):
        sentences = _SENTENCE_SPLIT_RE.split(raw_line)
        kept = [s for s in sentences
                if not s.strip() or mech.search(s) or not _SOFT_OFFER_RE.search(s)]
        line = " ".join(s for s in kept if s).rstrip()
        # Drop a line the strip emptied entirely (was purely the soft offer).
        if line or not raw_line.strip():
            out_lines.append(line if raw_line.strip() else raw_line)
    stripped = "\n".join(out_lines)
    return re.sub(r"\n{3,}", "\n\n", stripped)


def ensure_lead_magnet_cta(content: Optional[str], lead_magnet: Optional[dict], post_id: Optional[int],
                           use_emojis: bool = False, every_n: Optional[int] = None) -> Optional[str]:
    """Deterministic verify-and-repair run AFTER the refinement pipeline: if this post was selected
    for the lead-magnet CTA but the comment-keyword mechanic didn't survive the LLM rewrites, append
    a short soft-ask chosen from LEAD_MAGNET_CTA_REPAIR_MENU by post_id. Returns content unchanged
    (byte-identical) when not selected, lead magnet off, or a working mechanic is already present."""
    if not content or not should_include_lead_magnet_cta(lead_magnet, post_id, every_n):
        return content
    keyword = str(lead_magnet.get("keyword")).strip()
    # Strip the model's soft PARAPHRASE of the same offer first ("reach out if you'd like the
    # checklist...") — whether the real mechanic survived or not, a second softer ask next to it
    # reads as a doubled CTA.
    deduped = _strip_soft_resource_offers(content, keyword)
    if has_lead_magnet_cta_mechanic(deduped, keyword):
        return content if deduped == content else normalize_public_text(deduped)
    # Index by the SELECTION ORDINAL (post_id // n), not raw post_id: selected posts are all
    # multiples of n, so raw post_id % len(menu) would only ever hit gcd(n, len) of the variants —
    # the ordinal makes consecutive selected posts cycle through the whole menu.
    n = _effective_cta_every_n(every_n)
    idx = (int(post_id) // n) % len(LEAD_MAGNET_CTA_REPAIR_MENU)
    line = LEAD_MAGNET_CTA_REPAIR_MENU[idx].format(
        keyword=keyword, resource=_resource_label(lead_magnet))
    if use_emojis:
        line += " " + _CTA_REPAIR_EMOJI
    return normalize_public_text(deduped.rstrip() + "\n\n" + line)


# --- Link-in-first-comment (issue #392 - C3) ------------------------------------------------------
# An external link in the post BODY carries a ~60-68% reach penalty; the SAME link in the author's
# first comment costs nothing. These helpers are the deterministic (no-LLM) half of the mechanic:
# split the carried links out of a finished body at publish time, and rebuild the comment line that
# delivers them. LinkedIn's own URLs are left alone - they are not the off-platform penalty.
_LINK_RE = re.compile(r"(?:https?://|www\.)[^\s<>\[\]{}|\\^\"']+", re.IGNORECASE)
_INTERNAL_LINK_HOSTS = ("linkedin.com", "lnkd.in")
# Sentence punctuation that commonly abuts a URL and does not belong to it.
_LINK_TRAILING_PUNCT = ".,;:!?\"'"

# Carry at most a few links, and never more than the posts.first_comment_link column holds - links
# beyond the budget stay in the body rather than being silently dropped.
FIRST_COMMENT_LINK_MAX = 3
FIRST_COMMENT_LINK_MAX_CHARS = 1000

# Rotated by post_id (the LEAD_MAGNET_CTA_REPAIR_MENU idiom) so a user's first comments are not
# word-for-word identical post after post.
FIRST_COMMENT_LINK_MENU = (
    "Link to the full piece: {link}",
    "Full write-up here: {link}",
    "Here's the link if you want the details: {link}",
    "The whole thing lives here: {link}",
    "Details are here: {link}",
)


def _clean_link(raw: str) -> str:
    """Trim sentence punctuation the URL regex swallowed, and drop a trailing ')' that closes a
    parenthetical rather than being part of the URL."""
    link = (raw or "").strip()
    while link and link[-1] in _LINK_TRAILING_PUNCT:
        link = link[:-1]
    while link.endswith(")") and link.count(")") > link.count("("):
        link = link[:-1]
    return link


def is_external_link(url: Optional[str]) -> bool:
    """True for an off-platform link (the one LinkedIn's reach penalty applies to)."""
    link = (url or "").strip().lower()
    if not link:
        return False
    host = re.sub(r"^(?:https?://)?(?:www\.)?", "", link).split("/")[0].split("?")[0]
    return not any(host == h or host.endswith("." + h) for h in _INTERNAL_LINK_HOSTS)


def extract_external_links(content: Optional[str]) -> list:
    """Ordered, de-duplicated external links found in the content."""
    links = []
    for match in _LINK_RE.findall(content or ""):
        link = _clean_link(match)
        if link and is_external_link(link) and link not in links:
            links.append(link)
    return links


def _tidy_after_link_removal(line: str) -> str:
    """Repair the seam a removed URL leaves behind: empty brackets, a dangling 'here:' colon or
    arrow, doubled spaces, and a space before sentence punctuation."""
    line = re.sub(r"\(\s*\)|\[\s*\]|<\s*>", "", line)
    # A connector that pointed AT the link ("Read more:", "Full piece ->") now points at nothing.
    line = re.sub(r"[ \t]*[:>\-][ \t]*(?=[.,;!?]|$)", "", line)
    line = re.sub(r"[ \t]+([.,;:!?])", r"\1", line)
    line = re.sub(r"[ \t]{2,}", " ", line)
    return line.rstrip()


def split_link_for_first_comment(content: Optional[str], enabled: bool = True,
                                 max_links: int = FIRST_COMMENT_LINK_MAX,
                                 max_chars: int = FIRST_COMMENT_LINK_MAX_CHARS) -> tuple:
    """Split a finished post body into (body_without_carried_links, carried_links).

    Only links that will actually be carried into the first comment are removed, so a link can never
    be lost: over-budget links stay in the body. Returns the content unchanged with an empty list
    when disabled or when there is nothing external to move."""
    if not content or not enabled:
        return content, []
    links = extract_external_links(content)
    if not links:
        return content, []

    carried, budget = [], 0
    for link in links[:max_links]:
        cost = len(link) + (1 if carried else 0)  # newline-joined when persisted
        if budget + cost > max_chars:
            break
        carried.append(link)
        budget += cost
    if not carried:
        return content, []

    body = content
    # Longest first: removing "https://x.io/a" before "https://x.io/a/b" would corrupt the longer one.
    for link in sorted(carried, key=len, reverse=True):
        # The body may hold the link with trailing punctuation attached; only the URL itself goes.
        body = body.replace(link, "")
    cleaned_lines = []
    for raw_line in body.split("\n"):
        line = _tidy_after_link_removal(raw_line)
        # A line that existed only to hold the link disappears entirely.
        if line or not raw_line.strip():
            cleaned_lines.append(line)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()
    return body, carried


def first_comment_link_text(links, post_id: Optional[int] = None) -> str:
    """The link-delivery line(s) for the author's first comment. Empty string when no links."""
    links = [str(l).strip() for l in (links or []) if str(l or "").strip()]
    if not links:
        return ""
    idx = (int(post_id) % len(FIRST_COMMENT_LINK_MENU)) if post_id is not None else 0
    lines = [FIRST_COMMENT_LINK_MENU[idx].format(link=links[0])] + links[1:]
    return "\n".join(lines)


def append_link_to_comment(comment: Optional[str], links, post_id: Optional[int] = None) -> str:
    """Attach the carried link(s) to the generated seed comment. The AI half never writes links (the
    prompt forbids them), so this deterministic append is what actually delivers the mechanic - and
    it still returns a link-only comment when seed generation came back empty."""
    link_text = first_comment_link_text(links, post_id)
    base = (comment or "").strip()
    if not link_text:
        return base
    return normalize_public_text(f"{base}\n\n{link_text}" if base else link_text)


def personal_proof_directive(profile_synthesis: Optional[str] = None) -> str:
    """The A2 first-person proof requirement, SOURCED from the durable profile synthesis (the
    author's real credibility/expertise brief from get_or_create_profile_synthesis) plus whatever
    blog/experience content already rides in the prompt: instruct the writer to mine ONE concrete,
    lived detail — a real number, a moment in time, a named example, or a concrete outcome — out of
    that background and land it in the first person. This is the sourcing half of the mandatory proof
    slot content_framework.blueprint_directive injects; content_framework.has_first_person_proof is
    the deterministic gate that reject/regenerates a draft that comes back generic. Always returns a
    non-empty directive so callers can append it unconditionally on a regeneration retry. It asks for
    lived EXPERTISE, never a plug — so it stays inside the NO_SELF_PROMO_GUARDRAIL."""
    directive = (
        "\n\nFIRST-PERSON PROOF (required — 2026 authenticity, feeds the anti-generic gate):\n"
        "- Include at least ONE specific, first-person lived detail the author has actually earned: "
        "a real number, a moment in time, a named example, or a concrete outcome from their own work "
        "or experience — never a generic, could-be-anyone claim, and never invented.\n"
        "- Own it in the first person (\"I\"/\"we\") so it reads as genuine expertise, not AI filler. "
        "This is proof of experience, not self-promotion — do not turn it into a plug.\n")
    synth = (profile_synthesis or "").strip()
    if synth:
        directive += ("- Draw that detail from the author's real background below; pick a concrete "
                      "specific already grounded in it rather than inventing one:\n"
                      + synth[:800] + "\n")
    return directive


def voice_reference(profile, profile_synthesis: Optional[str] = None) -> str:
    """The VOICE/TONE/credibility reference string dropped into a generation prompt. Prefers the
    compact, stable synthesis; falls back to the guarded full profile JSON only when no synthesis was
    supplied (keeps behavior working before the first weekly refresh has run)."""
    if profile_synthesis and profile_synthesis.strip():
        return profile_synthesis.strip()
    return profile.model_dump_json()


# --- Authenticity gate (issue #382 — 360Brew / LinkedIn 2026 Authenticity Update defense) ---------
# LinkedIn's 2026 ranking (360Brew + the Authenticity Update) demotes generic AI content — the single
# biggest threat to an AI-content-heavy product. Before the deterministic similarity gate, an LLM judge
# (reusing lem-medium) scores a FINISHED draft on how generically-AI it reads AND how consistent it is
# with the author's own profile/topics: 0 = obviously generic AI slop, 100 = authentic, specific, and
# on-voice. Drafts below AUTHENTICITY_SCORE_MIN are held for human review instead of auto-approved —
# the same demote-not-block posture as the similarity gate. Fails OPEN (a scorer hiccup never blocks
# publishing). Rubric anchored to the 360Brew failure modes: no specifics, hollow buzzwords, listicle
# scaffolding, and drift away from the author's stated expertise.
AUTHENTICITY_SCORE_MIN_DEFAULT = 60


def authenticity_score_min(prefs: dict = None) -> int:
    """Score below which a draft is demoted APPROVED -> PENDING. The user's own setting
    (engagement_preferences.authenticity_score_min, issue #421) wins when set; otherwise read live
    (like post_similarity_max) so ops/tests can tune AUTHENTICITY_SCORE_MIN without a restart.
    Clamped to 0-100."""
    override = (prefs or {}).get("authenticity_score_min")
    if override is not None:
        try:
            return max(0, min(100, int(override)))
        except (TypeError, ValueError):
            pass
    raw = (os.environ.get("AUTHENTICITY_SCORE_MIN") or "").strip()
    try:
        value = int(raw) if raw else AUTHENTICITY_SCORE_MIN_DEFAULT
    except ValueError:
        value = AUTHENTICITY_SCORE_MIN_DEFAULT
    return max(0, min(100, value))


def authenticity_gate_enabled() -> bool:
    """The authenticity gate defaults ON — it is the core 360Brew defense. Set AUTHENTICITY_GATE_ENABLED
    to a falsey value (0/false/no/off) to disable it per-deploy."""
    raw = (os.environ.get("AUTHENTICITY_GATE_ENABLED") or "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


_AUTHENTICITY_JUDGE_SYSTEM = (
    "You are a strict LinkedIn content authenticity judge modeling LinkedIn's 2026 ranking (360Brew + "
    "the Authenticity Update), which DEMOTES generic AI-written content. Score how AUTHENTIC and "
    "human/specific a post reads, and how CONSISTENT it is with the author's stated expertise and voice.\n"
    "Penalize (lower score): generic buzzword filler with no concrete specifics; interchangeable "
    "'thought-leader' phrasing that could be posted by anyone; hollow listicle scaffolding; obvious "
    "AI tells; and topic drift away from the author's actual expertise.\n"
    "Reward (higher score): a specific point of view, concrete detail/example/number, a genuine "
    "personal or first-hand angle, and clear consistency with the author's profile topics.\n"
    "0 = obviously generic AI slop; 100 = authentic, specific, and unmistakably on-voice.\n"
    "Respond ONLY with a compact JSON object: {\"score\": <int 0-100>, \"reasons\": [\"...\", \"...\"]}."
)


def _coerce_authenticity_result(raw_text: str) -> Optional[dict]:
    """Parse the judge's JSON reply into {score:int 0-100, reasons:list[str]} or None if unusable."""
    if not raw_text:
        return None
    text = raw_text.strip()
    # Tolerate ```json fences and any prose around the object.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "score" not in data:
        return None
    try:
        score = int(round(float(data["score"])))
    except (ValueError, TypeError):
        return None
    reasons = data.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    reasons = [str(r).strip() for r in reasons if str(r).strip()]
    return {"score": max(0, min(100, score)), "reasons": reasons}


# --- Humanization / anti-AI-tell rewrite pass (issue #416 — A5) -----------------------------------
# The FINAL de-slop rewrite before the A1 authenticity gate (#382) and human review, applied to every
# AI-written text LEM publishes (posts, newsletters, DMs, comments, seed comments). It reimplements the
# READER-mode rules of the owner-supplied `anti-ai` skill (reference set committed under
# `anti_ai_skill/`): kill AI cliché lexicon + constructions, restore sentence-length variance, add
# contractions, cap em-dashes, end on a concrete point. DETECTOR-mode fracture is deliberately OUT of
# this automated path — it fabricates first-person specifics a human must replace before publishing.
# HARD CONSTRAINT: this pass NEVER invents facts — it draws real specifics ONLY from the draft or the
# author's own profile synthesis, and fails OPEN (returns the input unchanged) on disable/empty/error,
# so it can never block or corrupt publishing.

# Tier-1 AI-tell lexicon from anti_ai_skill/references/wordbank.md — words that read as machine-written
# and must go to zero. Used both to steer the rewrite prompt and as a deterministic, testable audit.
AI_TELL_WORDS = frozenset({
    # verbs
    "delve", "leverage", "underscore", "harness", "foster", "utilize", "facilitate", "streamline",
    "bolster", "illuminate", "showcase", "embark", "elevate", "empower", "unleash", "unlock",
    "uncover", "optimize", "garner", "resonate", "revolutionize", "synthesize", "elucidate",
    "transcend", "reimagine", "intertwine", "entwine", "espouse", "exemplify", "underpin",
    # nouns
    "tapestry", "landscape", "realm", "ecosystem", "paradigm", "synergy", "testament", "beacon",
    "journey", "interplay", "intricacies", "symphony", "kaleidoscope", "tempest", "whimsy", "quest",
    "roadmap", "endeavor", "myriad", "plethora", "advancements", "trajectory",
    # adjectives / adverbs
    "pivotal", "crucial", "seamless", "seamlessly", "robust", "vibrant", "intricate", "meticulous",
    "meticulously", "nuanced", "cutting-edge", "transformative", "game-changing", "groundbreaking",
    "unparalleled", "invaluable", "multifaceted", "commendable", "indelible", "poignant", "profound",
    "profoundly", "relentless", "relentlessly", "tireless", "tirelessly", "unwavering", "unyielding",
    "timeless", "ever-evolving", "fast-paced",
})

_WORD_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z-]*")


def find_ai_tell_words(text: Optional[str]) -> list[str]:
    """Deterministic audit: the tier-1 AI-tell words (from AI_TELL_WORDS) present in `text`, in order
    of first appearance, de-duplicated and case-insensitive. NO LLM call."""
    out, seen = [], set()
    for tok in _WORD_TOKEN_RE.findall(text or ""):
        low = tok.lower()
        if low in AI_TELL_WORDS and low not in seen:
            seen.add(low)
            out.append(low)
    return out


# Em-dash "—" and the double-hyphen "--" variant — both count as the em-dash tell (the compulsive
# mid-sentence pivot). Surrounding whitespace is consumed so a replacement reads cleanly.
_EM_DASH_TOKEN_RE = re.compile(r"\s*(?:—|--)\s*")


def count_em_dashes(text: str) -> int:
    """Deterministic count of em-dash tells (— and --) in `text`. NO LLM call."""
    return len(_EM_DASH_TOKEN_RE.findall(text or ""))


def cap_em_dashes(text: str, max_dashes: int = 1) -> str:
    """Hold em-dash tells (— and --) to at most `max_dashes`, replacing the excess with a comma (the
    plain human default from the tell checklist). Keeps the FIRST `max_dashes` as-is. Deterministic."""
    if not text:
        return text
    state = {"n": 0}

    def _repl(m):
        state["n"] += 1
        return m.group(0) if state["n"] <= max_dashes else ", "

    return _EM_DASH_TOKEN_RE.sub(_repl, text)


# Conservative, unambiguous "X is/are/not" -> contraction map (the wordbank's top human marker).
# Replacement stored lowercase; _apply_contractions restores a leading capital from the match.
_CONTRACTIONS = (
    (r"it is", "it's"), (r"that is", "that's"), (r"there is", "there's"), (r"here is", "here's"),
    (r"what is", "what's"), (r"who is", "who's"), (r"he is", "he's"), (r"she is", "she's"),
    (r"we are", "we're"), (r"you are", "you're"), (r"they are", "they're"),
    (r"we will", "we'll"), (r"you will", "you'll"), (r"I am", "i'm"), (r"I will", "i'll"),
    (r"I have", "i've"), (r"we have", "we've"), (r"you have", "you've"),
    (r"do not", "don't"), (r"does not", "doesn't"), (r"did not", "didn't"), (r"is not", "isn't"),
    (r"are not", "aren't"), (r"was not", "wasn't"), (r"were not", "weren't"), (r"will not", "won't"),
    (r"cannot", "can't"), (r"can not", "can't"), (r"would not", "wouldn't"),
    (r"could not", "couldn't"), (r"should not", "shouldn't"), (r"have not", "haven't"),
    (r"has not", "hasn't"),
)
_CONTRACTION_RES = tuple((re.compile(rf"\b{p}\b", re.IGNORECASE), r) for p, r in _CONTRACTIONS)


def apply_contractions(text: str) -> str:
    """Deterministically fold the common expanded forms ("it is" -> "it's", "do not" -> "don't") into
    contractions, preserving a leading capital. Idempotent — already-contracted text is untouched."""
    if not text:
        return text

    def _mk(repl):
        def _f(m):
            return repl[0].upper() + repl[1:] if m.group(0)[:1].isupper() else repl
        return _f

    for rx, repl in _CONTRACTION_RES:
        text = rx.sub(_mk(repl), text)
    return text


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def humanize_enabled(content_type: Optional[str] = None) -> bool:
    """Master + per-type toggle for the humanization pass. HUMANIZE_ENABLED (default ON) gates
    everything; an explicit per-type flag HUMANIZE_<TYPE>_ENABLED (e.g. HUMANIZE_COMMENT_ENABLED) can
    further disable one content type without touching the others. Read live so ops can flip it without
    a restart. HUMANIZE_ENABLED=off restores the exact prior behavior everywhere."""
    if not _env_flag("HUMANIZE_ENABLED", True):
        return False
    if content_type:
        raw = (os.environ.get(f"HUMANIZE_{content_type.strip().upper()}_ENABLED") or "").strip().lower()
        if raw:
            return raw in ("1", "true", "yes", "on")
    return True


_HUMANIZE_SYSTEM = (
    "You rewrite AI-drafted text so no human reader can tell a machine wrote it, WITHOUT changing its "
    "meaning, its facts, its format, or the author's intent. This is a final polish before the text is "
    "published in the author's own name.\n\n"
    "HARD RULES (these override everything else):\n"
    "- NEVER invent or add facts: no names, numbers, dates, prices, places, quotes, statistics, or "
    "events that are not already in the draft (or in the author-background section below, when one is "
    "given). If a sentence wants a concrete specific and none is available, make it plainer instead of "
    "inventing one. Never add placeholder text like [example].\n"
    "- Keep the same format (a post stays a post, a comment stays a comment) and roughly the same "
    "length. Keep every claim the draft actually makes — you may cut filler, never facts.\n\n"
    "REWRITE FOR A REAL HUMAN VOICE:\n"
    "- Kill AI cliche words; use the plain word you'd say out loud: use (not leverage/utilize), deal "
    "with (not navigate), big or key (not pivotal/crucial), show (not showcase/underscore), a lot of "
    "(not myriad/plethora). Drop delve, tapestry, realm, ecosystem, paradigm, seamless, robust, "
    "testament, journey, transformative, foster, unlock, elevate, landscape, and their kin.\n"
    "- Cut AI constructions: no 'It's not X, it's Y' framing (just say Y), no 'not only... but also', "
    "no rule-of-three lists built for rhythm, no rhetorical 'The result? ...', no 'serves as / stands "
    "as' (use 'is'), no hedge stacks ('it's important to note'), no 'In conclusion / In summary', no "
    "'Here's the kicker / the thing'.\n"
    "- Restore burstiness: mix at least one very short sentence (<=6 words) with a long one (25+ "
    "words); let paragraphs be uneven; you may start a sentence with And, But, or Because.\n"
    "- Turn expanded forms into contractions (it's, don't, we're, you're).\n"
    "- At most ONE em-dash in the whole piece; prefer a comma or a period.\n"
    "- Strip emoji bullets, bold-first bullets, and Title-Case headings; end on a concrete point, "
    "never a pep-talk or a restated summary.\n"
    "- Keep a real voice: a mild opinion, a dry aside, or an admission is good. Do NOT scrub the text "
    "into something tell-free but flat and personality-free.\n\n"
    "Output ONLY the rewritten text — no preface, no notes, no explanation, no quotes around it."
)

_HUMANIZE_TYPE_NOTE = {
    "comment": "\n\nThis is a short LinkedIn comment: keep it to a few sentences and stay conversational.",
    "dm": "\n\nThis is a short direct message: keep it brief and warm, and do not add a subject line.",
    "newsletter": ("\n\nThis is a newsletter body: keep its sections and depth — humanize the prose, "
                   "do not shorten the substance."),
    "post": "\n\nThis is a LinkedIn post: keep the hook up front and the same overall shape.",
}


def humanize_text(content: Optional[str], content_type: str = "post",
                  profile_synthesis: Optional[str] = None,
                  prefs: Optional[dict] = None, max_chars: Optional[int] = None) -> Optional[str]:
    """READER-mode humanization pass (issue #416): the final anti-AI-tell rewrite before the A1
    authenticity gate and human review. Returns a de-slopped version of `content` in the author's
    voice — AI cliches/constructions removed, sentence-length variance restored, contractions in,
    em-dashes capped at one — WITHOUT fabricating any fact (real specifics come ONLY from the draft or
    `profile_synthesis`). FAILS OPEN: returns `content` byte-identical when the pass is disabled, the
    input is empty, the model errors, the rewrite looks truncated, or (when `max_chars` is set) the
    rewrite would exceed the caller's hard length budget — so it can never block or corrupt publishing."""
    if not content or not str(content).strip():
        return content
    if not humanize_enabled(content_type):
        return content
    original = content
    try:
        extra = _HUMANIZE_TYPE_NOTE.get(content_type, _HUMANIZE_TYPE_NOTE["post"])
        synth = (profile_synthesis or "").strip()
        if synth:
            extra += ("\n\nAuthor background — the ONLY source of real specifics you may draw on (never "
                      "copy it verbatim, never invent beyond it):\n" + synth[:800])
        hits = find_ai_tell_words(str(content))
        if hits:
            extra += "\n\nRemove these AI-tell words that appear in the draft: " + ", ".join(hits[:20]) + "."
        if max_chars:
            extra += f"\n\nHard limit: the rewrite MUST be at most {int(max_chars)} characters."
        # Lazy import avoids a circular import (ai_helper imports this module).
        from cqc_lem.utilities.ai.ai_helper import _call_llm
        resp = _call_llm(
            model="lem-medium",
            messages=[
                {"role": "system", "content": _HUMANIZE_SYSTEM + extra},
                {"role": "user", "content": str(content)},
            ],
            temperature=0.4,
        )
        rewritten = (resp.choices[0].message.content or "").strip()
    except Exception:
        return original
    if not rewritten:
        return original
    # A legitimate de-slop trims some length; a collapse to a fragment of a long draft means the model
    # refused or got cut off — keep the original rather than ship a truncated post.
    if len(rewritten) < max(24, int(len(original.strip()) * 0.25)):
        return original
    rewritten = apply_contractions(cap_em_dashes(rewritten, 1)).strip()
    # Never ship a rewrite that blew past the caller's hard budget (e.g. a DM over its char cap) — the
    # pre-humanize text was already within budget.
    if max_chars and len(rewritten) > int(max_chars):
        return original
    return rewritten


# --- Title de-hype pass (issue #439) --------------------------------------------------------------
# A headline is not prose: the full READER-mode rewrite above flattens it into a sentence and costs the
# open rate. Titles get their own pass that strips the hype/AI tells while KEEPING one real hook.

# Headline-only hype tells (clickbait superlatives + growth-hack verbs) that aren't in the tier-1 prose
# wordbank. Used to steer the rewrite prompt and as a deterministic, testable audit.
TITLE_HYPE_WORDS = frozenset({
    "explosive", "explode", "exploding", "supercharge", "supercharged", "skyrocket", "skyrocketing",
    "insane", "jaw-dropping", "mind-blowing", "shocking", "ultimate", "secret", "secrets", "hack",
    "hacks", "guru", "ninja", "viral", "effortless", "effortlessly", "proven", "foolproof",
    "must-have", "no-brainer", "revolutionary", "epic", "killer", "supercharging", "10x",
})


# Headline tokens may START with a digit ("10x"), unlike the prose wordbank, so this pass gets its own
# tokenizer instead of _WORD_TOKEN_RE.
_TITLE_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]*")


def find_title_slop_words(text: Optional[str]) -> list[str]:
    """Deterministic audit for headlines: tier-1 AI-tell words (AI_TELL_WORDS) plus the headline-only
    hype lexicon (TITLE_HYPE_WORDS) present in `text`, in order of first appearance, de-duplicated and
    case-insensitive. NO LLM call."""
    out, seen = [], set()
    for tok in _TITLE_TOKEN_RE.findall(text or ""):
        low = tok.lower()
        if (low in AI_TELL_WORDS or low in TITLE_HYPE_WORDS) and low not in seen:
            seen.add(low)
            out.append(low)
    return out


_HUMANIZE_TITLE_SYSTEM = (
    "You rewrite an AI-drafted HEADLINE so it reads like a human editor wrote it, without changing what "
    "the piece is about. This headline is published in the author's own name.\n\n"
    "HARD RULES:\n"
    "- NEVER invent facts: no numbers, names, dates, claims, or specifics that are not already in the "
    "draft headline (or the author-background section below, when one is given).\n"
    "- Keep the same subject. If the draft names a real specific (a tool, a number, a place), keep it.\n"
    "- It stays a HEADLINE: one short line, no trailing period, no quotes around it, no subtitle, no "
    "explanation. Aim for under ~90 characters.\n"
    "- KEEP ONE genuine hook - a tension, a specific promise, a contrarian angle, a concrete outcome. "
    "Do NOT flatten it into a bland topic label, and do NOT turn it into a sentence of prose or a rant.\n\n"
    "DE-HYPE (this is the job):\n"
    "- Cut hype adjectives: game-changing, groundbreaking, revolutionary, explosive, ultimate, "
    "unparalleled, cutting-edge, transformative, insane, must-have, secret, proven.\n"
    "- Cut AI verbs: unlock, unleash, elevate, harness, leverage, supercharge, master, delve, "
    "revolutionize, skyrocket.\n"
    "- Drop clickbait scaffolds: '7 X That Will...', 'The Ultimate Guide to', 'You Won't Believe', "
    "'Here's Why/How', 'X Is Killing Y'. A number stays only if it names something real in the draft.\n"
    "- No emoji, no ALL-CAPS words, no exclamation marks, no em dashes, no curly quotes.\n"
    "- Use the plain words a person would say out loud.\n\n"
    "Output ONLY the rewritten headline."
)

_TITLE_LABEL_RE = re.compile(r"^(?:title|headline)\s*[:\-]\s*", re.IGNORECASE)


def _clean_title_line(text: str) -> str:
    """Coerce a model reply back into a single headline: first non-empty line, no 'Title:' label, no
    wrapping quotes, whitespace collapsed, em-dash tells removed."""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    line = _TITLE_LABEL_RE.sub("", line).strip()
    if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'":
        line = line[1:-1].strip()
    line = re.sub(r"\s+", " ", cap_em_dashes(line, 0)).strip()
    if line.endswith(".") and not line.endswith("..."):
        line = line[:-1].rstrip()
    return line


def humanize_title(title: Optional[str], content_type: str = "newsletter",
                   profile_synthesis: Optional[str] = None,
                   prefs: Optional[dict] = None, max_chars: int = 255) -> Optional[str]:
    """Title-appropriate de-hype pass (issue #439). Strips the AI/hype tells from an AI-drafted headline
    (wordbank hits, clickbait scaffolds, superlatives) while keeping ONE compelling hook, so titles stop
    reading like "7 Game-Changing Tactics for Explosive Growth". Shares the HUMANIZE_ENABLED /
    HUMANIZE_<TYPE>_ENABLED toggles with humanize_text and FAILS OPEN the same way: returns `title`
    unchanged when the pass is disabled, the input is empty, the model errors, the reply collapses to a
    fragment, it runs past the headline budget (a reply that long is prose, not a headline), or the
    rewrite carries more hype/AI tells than what came in.

    Headline budget = min(max_chars, max(90, len(title))). The 90-char floor matches the "aim for under
    ~90 characters" instruction in the prompt: de-hyping a SHORT hype headline ("10x Your Reach") into
    plain words legitimately needs a little more room, so capping at the draft's own length there would
    fail open on exactly the titles this pass exists for. Longer drafts never get to grow."""
    if not title or not str(title).strip():
        return title
    if not humanize_enabled(content_type):
        return title
    original = title
    try:
        extra = ""
        synth = (profile_synthesis or "").strip()
        if synth:
            extra += ("\n\nAuthor background - the ONLY source of real specifics you may draw on (never "
                      "copy it verbatim, never invent beyond it):\n" + synth[:400])
        hits = find_title_slop_words(str(title))
        if hits:
            extra += ("\n\nRemove these hype / AI-tell words that appear in the draft headline: "
                      + ", ".join(hits[:20]) + ".")
        extra += f"\n\nHard limit: the headline MUST be at most {int(max_chars)} characters."
        # Lazy import avoids a circular import (ai_helper imports this module).
        from cqc_lem.utilities.ai.ai_helper import _call_llm
        resp = _call_llm(
            model="lem-simple",
            messages=[
                {"role": "system", "content": _HUMANIZE_TITLE_SYSTEM + extra},
                {"role": "user", "content": str(title).strip()},
            ],
            temperature=0.4,
        )
        rewritten = _clean_title_line(resp.choices[0].message.content or "")
    except Exception:
        return original
    # A headline that came back as a couple of words lost its hook (or the model refused). Past the
    # headline budget (see docstring) the reply is prose/an explanation, not a title.
    budget = min(int(max_chars), max(90, len(str(title).strip())))
    if len(rewritten) < 12 or len(rewritten) > budget:
        return original
    if len(find_title_slop_words(rewritten)) > len(find_title_slop_words(str(title))):
        return original
    return rewritten


def score_authenticity(content: str, profile=None, profile_synthesis: Optional[str] = None,
                       prefs: dict = None) -> dict:
    """LLM-judge the authenticity / generic-AI risk of a finished post draft.

    Returns {"score": int 0-100, "reasons": list[str], "flagged": bool} where flagged means the score
    fell below authenticity_score_min(). Reuses lem-medium. FAILS OPEN — on any error (or empty input)
    it returns a passing, unflagged result so a judge hiccup never blocks publishing."""
    passing = {"score": 100, "reasons": [], "flagged": False}
    if not content or not content.strip():
        return passing
    try:
        voice = voice_reference(profile, profile_synthesis) if profile is not None else ""
        topics = ", ".join(_focus_topics(prefs)) if prefs else ""
        context_lines = []
        if voice:
            context_lines.append(f"Author voice/profile reference:\n{voice[:1500]}")
        if topics:
            context_lines.append(f"Author's declared focus topics: {topics}")
        context = ("\n\n".join(context_lines) + "\n\n") if context_lines else ""

        # Lazy import avoids a circular import (ai_helper imports this module).
        from cqc_lem.utilities.ai.ai_helper import _call_llm
        resp = _call_llm(
            model="lem-medium",
            messages=[
                {"role": "system", "content": _AUTHENTICITY_JUDGE_SYSTEM},
                {"role": "user", "content": f"{context}Post to score:\n{content[:3000]}"},
            ],
            temperature=0,
        )
        parsed = _coerce_authenticity_result(resp.choices[0].message.content or "")
        if parsed is None:
            return passing
        parsed["flagged"] = parsed["score"] < authenticity_score_min(prefs)
        return parsed
    except Exception:
        return passing
