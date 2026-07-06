"""The ONE alignment core shared by every generated content type (newsletters, posts, comments,
replies, seed comments, group posts). Voice comes from the durable profile synthesis, subject
steering from the user's engagement preferences (focus topics + goals), purpose from LEM's
relationship-building engagement philosophy, and the self-promo policy is expressed ONCE here:
a HARD no-self-promo guardrail for comments/posts, a LIGHT soft-promo allowance for the author's
own newsletter. Keeping all of this in one module is what stops the content types from drifting
out of alignment with each other over time."""

from typing import Optional

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


def alignment_directive(prefs: dict = None) -> str:
    """Anti-self-promo guardrail + focus/goal steering, appended to POST prompts so generated posts
    stay aligned to the user's real business/personal goals instead of drifting into promoting
    whatever the user happens to be building right now."""
    return ("\n\nContent alignment rules:\n- " + NO_SELF_PROMO_GUARDRAIL
            + "\n- " + engagement_purpose("post") + focus_directive(prefs))


def voice_reference(profile, profile_synthesis: Optional[str] = None) -> str:
    """The VOICE/TONE/credibility reference string dropped into a generation prompt. Prefers the
    compact, stable synthesis; falls back to the guarded full profile JSON only when no synthesis was
    supplied (keeps behavior working before the first weekly refresh has run)."""
    if profile_synthesis and profile_synthesis.strip():
        return profile_synthesis.strip()
    return profile.model_dump_json()
