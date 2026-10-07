"""Dual-audience alternation (showcase round 5) — which READER each post is written for.

An author can serve two audiences: a PRIMARY one (who their goals and focus topics already
describe) and a SECONDARY one that takes a share of the posts. Each post picks ONE audience and
writes for it, and the pick is deterministic: it is read off the audiences recorded on the user's
recent posts (`posts.audience`) so a share of 0.6 is honoured over any rolling
`AUDIENCE_WINDOW`-post window, alternating rather than in streaks.

The setting is `engagement_preferences.audience_mix`, a JSON object::

    {"primary_audience": "...", "secondary_audience": "...",
     "secondary_share": 0.6, "secondary_focus_topics": ["...", ...]}

A share of 0 (the default, and every row saved before this existed) is single-audience: nothing
here changes a post. Everything in this module is a PURE function of the setting and the recorded
history — the DB reads live in `utilities.db`, the wiring in `app.run_content_plan`.
"""

import json
from typing import Optional

from cqc_lem.utilities.ai.content_framework import smb_audience_text

AUDIENCE_PRIMARY = "primary"
AUDIENCE_SECONDARY = "secondary"
AUDIENCES = (AUDIENCE_PRIMARY, AUDIENCE_SECONDARY)

# The rolling window the share is honoured over: this post plus the last nine.
AUDIENCE_WINDOW = 10

# Stored bounds — the API and the repository both apply them, so no caller can store a blob.
AUDIENCE_DESCRIPTION_MAX = 400
AUDIENCE_TOPIC_MAX_CHARS = 120
AUDIENCE_TOPICS_MAX = 12

_FIELDS = ("primary_audience", "secondary_audience", "secondary_share", "secondary_focus_topics")


def _clean_text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_share(value) -> float:
    try:
        share = float(value)
    except (TypeError, ValueError):
        return 0.0
    if share != share:  # NaN
        return 0.0
    return round(min(1.0, max(0.0, share)), 2)


def _clean_topics(values) -> list:
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, (list, tuple)):
        return []
    out: list = []
    for raw in values:
        topic = _clean_text(raw, AUDIENCE_TOPIC_MAX_CHARS)
        if topic and topic.lower() not in {t.lower() for t in out}:
            out.append(topic)
    return out[:AUDIENCE_TOPICS_MAX]


def parse_audience_mix(raw) -> Optional[dict]:
    """The stored `audience_mix` value as a clean dict, or None for "no setting".

    Accepts the column's JSON string/bytes or an already-decoded dict. Unknown keys are dropped,
    text is trimmed and bounded, the share is clamped to 0..1, and topics are de-duplicated.

    Args:
        raw: The column value or a request body's object.

    Returns:
        The normalized setting, or None when it is NULL, unparseable, or carries nothing at all.
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    mix = {
        "primary_audience": _clean_text(raw.get("primary_audience"), AUDIENCE_DESCRIPTION_MAX),
        "secondary_audience": _clean_text(raw.get("secondary_audience"), AUDIENCE_DESCRIPTION_MAX),
        "secondary_share": _clean_share(raw.get("secondary_share")),
        "secondary_focus_topics": _clean_topics(raw.get("secondary_focus_topics")),
    }
    if not (mix["primary_audience"] or mix["secondary_audience"] or mix["secondary_share"]
            or mix["secondary_focus_topics"]):
        return None
    return {k: mix[k] for k in _FIELDS}


def mix_active(mix: Optional[dict]) -> bool:
    """Whether the setting actually alternates: a positive share AND a secondary audience to serve."""
    mix = parse_audience_mix(mix)
    return bool(mix and mix["secondary_share"] > 0 and mix["secondary_audience"])


def select_audience(mix: Optional[dict], recent_audiences: Optional[list]) -> str:
    """The audience THIS post is written for, chosen from the audiences recent posts recorded.

    Deterministic error diffusion over the rolling window: of the last `AUDIENCE_WINDOW - 1`
    posts, only the ones that RECORDED an audience count (posts written before the setting existed
    say nothing about the mix), and this post goes to the secondary audience exactly when that
    keeps the window's secondary count nearest `share × posts`. A 0.6 share from an empty history
    reads S P S P S S P S P S — six of ten, never a streak of six.

    Args:
        mix: The user's `audience_mix` setting.
        recent_audiences: Recent posts' recorded audiences, most recent first (None for unrecorded).

    Returns:
        `AUDIENCE_PRIMARY` or `AUDIENCE_SECONDARY`. Always primary when the mix is not active.
    """
    if not mix_active(mix):
        return AUDIENCE_PRIMARY
    share = parse_audience_mix(mix)["secondary_share"]
    window = [a for a in list(recent_audiences or [])[:AUDIENCE_WINDOW - 1] if a in AUDIENCES]
    secondary = sum(1 for a in window if a == AUDIENCE_SECONDARY)
    return AUDIENCE_SECONDARY if secondary + 0.5 <= share * (len(window) + 1) else AUDIENCE_PRIMARY


def audience_profile(mix: Optional[dict], audience: str, prefs: Optional[dict] = None) -> dict:
    """Everything a post written for `audience` is steered by.

    Args:
        mix: The user's `audience_mix` setting.
        audience: `AUDIENCE_PRIMARY` or `AUDIENCE_SECONDARY`.
        prefs: Engagement preferences — the primary audience's focus topics and goals live there.

    Returns:
        `{audience, description, smb, focus_topics}`. `smb` is whether the reader is a small-business
        owner — the per-post switch for the plain-fold rule, the CTA menu and the hook menu. The
        secondary audience's topics fall back to the user's focus topics when it has none.
    """
    mix = parse_audience_mix(mix) or {}
    prefs = prefs or {}
    focus = [str(t) for t in (prefs.get("focus_topics") or []) if str(t).strip()]
    if audience == AUDIENCE_SECONDARY:
        description = mix.get("secondary_audience") or ""
        topics = list(mix.get("secondary_focus_topics") or []) or focus
    else:
        audience = AUDIENCE_PRIMARY
        description = mix.get("primary_audience") or ""
        topics = focus
    return {"audience": audience, "description": description,
            "smb": smb_audience_text(description), "focus_topics": topics}


def audience_prefs(prefs: Optional[dict], profile: Optional[dict]) -> dict:
    """`prefs` re-aimed at one audience — what the post's topic selection and alignment read.

    The focus topics become the audience's own, so the research and the subject rotate through
    them. For the secondary audience the business goal is re-stated around its reader: the stored
    goal names the PRIMARY audience, and left as is it pulls every post back toward it.

    Args:
        prefs: Engagement preferences.
        profile: `audience_profile(...)` for this post, or None to leave prefs untouched.

    Returns:
        A copy of prefs.
    """
    out = dict(prefs or {})
    if not profile:
        return out
    out["focus_topics"] = list(profile.get("focus_topics") or out.get("focus_topics") or [])
    if profile.get("audience") == AUDIENCE_SECONDARY and profile.get("description"):
        goal = str(out.get("business_goals") or "").strip()
        out["business_goals"] = (f"This post serves {profile['description']}."
                                 + (f" (The author's wider goal: {goal})" if goal else ""))
    return out


def audience_directive(profile: Optional[dict]) -> str:
    """The writer-side injection naming THIS post's ONE reader and how to write for them.

    Args:
        profile: `audience_profile(...)`, or None.

    Returns:
        The directive, or "" when there is no described audience.
    """
    if not profile or not profile.get("description"):
        return ""
    lines = [(f"THIS POST'S READER: {profile['description']}. Write for this reader ONLY — never "
              "address a second audience in the same post, and never name the reader by a job "
              "title they would not use for themselves.")]
    if profile.get("smb"):
        lines.append(
            "READER RULES (small-business owner): lead with the OUTCOME — money, hours, customers or "
            "risk — in plain words they would say out loud. No engineering vocabulary in the first "
            "two lines; any technical detail goes lower down, translated into what it does for the "
            "business. Never call the reader a 'technical leader', 'engineer' or 'team'.")
    else:
        lines.append(
            "READER RULES (practitioner): write peer to peer for people already running AI in "
            "production — concrete mechanisms, real numbers, the trade-off and the failure mode. "
            "Do not explain basics they already know, and do not frame the post for small-business "
            "owners.")
    return "\n".join(lines)
