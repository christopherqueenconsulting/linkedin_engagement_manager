"""The ONE alignment core shared by every generated content type (newsletters, posts, comments,
replies, seed comments, group posts). Voice comes from the durable profile synthesis, subject
steering from the user's engagement preferences (focus topics + goals), purpose from LEM's
relationship-building engagement philosophy, and the self-promo policy is expressed ONCE here:
a HARD no-self-promo guardrail for comments/posts, a LIGHT soft-promo allowance for the author's
own newsletter. Keeping all of this in one module is what stops the content types from drifting
out of alignment with each other over time."""

import math
import os
import random
import re
from typing import Optional

from cqc_lem.utilities.linkedin_formatter import normalize_public_text

# Tight, engagement-optimized targets. Short is the default: LinkedIn rewards comments that
# earn a REPLY (threads), and a punchy, specific comment out-performs a long essay. Even
# "short" stays >~25 words so it clears the quality floor.
COMMENT_LENGTH_CHARS = {"short": 180, "medium": 320, "long": 550}

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


def style_directive(prefs: dict = None, content_type: str = "comment") -> str:
    """Turn the user's engagement preferences into an explicit style directive that overrides
    the profile-inferred defaults (tone, length, emoji/hashtag rules, freeform style). The
    comment-length cap only applies to comments — posts and newsletters carry their own length
    guidance in their prompts."""
    if not prefs:
        return ""
    parts = []
    tone = prefs.get("tone")
    if tone:
        parts.append(f"Write in a {tone} tone.")
    if content_type == "comment":
        length = prefs.get("comment_length") or "short"
        parts.append(f"Keep it {length} — at most ~{COMMENT_LENGTH_CHARS.get(length, 180)} characters "
                     f"(a few sentences); brevity beats length.")
    parts.append("You may use one tasteful emoji." if prefs.get("use_emojis") else "Do not use emojis.")
    parts.append("Relevant hashtags are okay." if prefs.get("use_hashtags") else "Do not use any hashtags.")
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


def select_focus_topic(prefs: dict = None, sequence_index: Optional[int] = None) -> Optional[str]:
    """The SUBJECT anchor for one trend-based post: rotate deterministically across the user's
    declared focus topics (keyed off a stable per-post integer — the post id — the same way the
    lead-magnet CTA rotation works) so anchoring never collapses every post onto one topic. Without
    a sequence key it falls back to a random pick among the topics (variety over determinism, and
    consistent with how the industry itself is randomly chosen). Returns None when the user declared
    no focus topics — callers keep their current profile-industry-only behavior."""
    topics = _focus_topics(prefs)
    if not topics:
        return None
    if sequence_index is None:
        return random.choice(topics)
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


def alignment_directive(prefs: dict = None, lead_magnet_cta: str = "") -> str:
    """Anti-self-promo guardrail + focus/goal steering, appended to POST prompts so generated posts
    stay aligned to the user's real business/personal goals instead of drifting into promoting
    whatever the user happens to be building right now. `lead_magnet_cta` (built by
    lead_magnet_cta_directive) is the ONE sanctioned exception to the guardrail and is appended
    only for the posts the rotation selects — see should_include_lead_magnet_cta."""
    return ("\n\nContent alignment rules:\n- " + NO_SELF_PROMO_GUARDRAIL
            + "\n- " + engagement_purpose("post") + focus_directive(prefs)
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
        f"- End the post with a short, soft invitation for readers to comment the exact word "
        f"\"{keyword}\" to receive the resource; it will be delivered by DM after they comment.\n"
        "- Write it in the user's own voice — plainspoken, no hype, no hard sell, and NO link in the "
        "post body (the DM delivers it). Honor the user's emoji/hashtag settings.\n"
        "- Keep it to one clean ask; do NOT stack multiple asks or use engagement-bait phrasing "
        "(no 'tag a friend', no 'like if')." + context_line + "\n")


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


def ensure_lead_magnet_cta(content: Optional[str], lead_magnet: Optional[dict], post_id: Optional[int],
                           use_emojis: bool = False, every_n: Optional[int] = None) -> Optional[str]:
    """Deterministic verify-and-repair run AFTER the refinement pipeline: if this post was selected
    for the lead-magnet CTA but the comment-keyword mechanic didn't survive the LLM rewrites, append
    a short soft-ask chosen from LEAD_MAGNET_CTA_REPAIR_MENU by post_id. Returns content unchanged
    (byte-identical) when not selected, lead magnet off, or a working mechanic is already present."""
    if not content or not should_include_lead_magnet_cta(lead_magnet, post_id, every_n):
        return content
    keyword = str(lead_magnet.get("keyword")).strip()
    if has_lead_magnet_cta_mechanic(content, keyword):
        return content
    # Index by the SELECTION ORDINAL (post_id // n), not raw post_id: selected posts are all
    # multiples of n, so raw post_id % len(menu) would only ever hit gcd(n, len) of the variants —
    # the ordinal makes consecutive selected posts cycle through the whole menu.
    n = _effective_cta_every_n(every_n)
    idx = (int(post_id) // n) % len(LEAD_MAGNET_CTA_REPAIR_MENU)
    line = LEAD_MAGNET_CTA_REPAIR_MENU[idx].format(
        keyword=keyword, resource=_resource_label(lead_magnet))
    if use_emojis:
        line += " " + _CTA_REPAIR_EMOJI
    return normalize_public_text(content.rstrip() + "\n\n" + line)


def voice_reference(profile, profile_synthesis: Optional[str] = None) -> str:
    """The VOICE/TONE/credibility reference string dropped into a generation prompt. Prefers the
    compact, stable synthesis; falls back to the guarded full profile JSON only when no synthesis was
    supplied (keeps behavior working before the first weekly refresh has run)."""
    if profile_synthesis and profile_synthesis.strip():
        return profile_synthesis.strip()
    return profile.model_dump_json()
