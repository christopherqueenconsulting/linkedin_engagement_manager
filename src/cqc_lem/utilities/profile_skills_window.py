"""Profile-skills re-index window (issue #1075).

When a profile re-scrape shows the top-5 ordered skills changed vs the previous scrape, LEM opens a
~14-day window during which those skill keywords are woven into generated content as soft subject
steering. The window state is persisted in Redis (so retries never re-roll it) and anchored by the
`last_recorded_skills` snapshot stored on the `profiles` row.

The directive is the same shape as `focus_directive` from `content_alignment.py`; callers append it
to post/comment/reply prompts where they already append focus steering. Existing gates (topic-DNA,
slop lint, similarity) still decide what ships — this is steering, not a bypass.
"""

import json
from typing import TYPE_CHECKING, Optional

from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_debug

if TYPE_CHECKING:
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile

WINDOW_DAYS = 14
_KEY_PREFIX = "lem:profile_skills_window"


def _key(user_id: int) -> str:
    return f"{_KEY_PREFIX}:{user_id}"


def _top_skills(profile: "LinkedInProfile") -> list:
    """The top-5 profile skill names in scrape order, lower-cased and trimmed.

    Skills arrive as plain strings or `LinkedInSkill` objects. Non-strings / empties are skipped so
    a partial scrape never pollutes the snapshot.
    """
    raw = getattr(profile, "skills", None) or []
    out = []
    for item in raw:
        name = item if isinstance(item, str) else getattr(item, "name", None)
        if not isinstance(name, str):
            continue
        cleaned = name.strip().lower()
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= 5:
            break
    return out


def skills_changed_since_last_recorded(user_id: int, profile: "LinkedInProfile") -> bool:
    """True when the current top-5 skill ordered list differs from the stored snapshot.

    The FIRST successful scrape for a user has no prior snapshot — that is NOT a change, so it
    returns False after recording the initial snapshot. This avoids opening a spurious re-index
    window for every brand-new account.

    A scrape that yields NO skills is an UNDETECTABLE diff, not an emptied skills list: LinkedIn's
    skills section is one of the first things a partial render drops. Recording [] would wipe the
    baseline, and the next real reorder would then read as a first scrape and open no window at
    all — so an empty read leaves the snapshot untouched (expected no-op → DEBUG).
    """
    from cqc_lem.utilities.db import get_last_recorded_skills, set_last_recorded_skills
    current = _top_skills(profile)
    if not current:
        log_debug("Profile-skills window: scrape carried no skills — snapshot left untouched",
                  user_id=user_id)
        return False
    previous = get_last_recorded_skills(user_id)
    if current == previous:
        return False
    set_last_recorded_skills(user_id, current)
    return bool(previous)


def open_profile_skills_window(user_id: int, profile: "LinkedInProfile") -> list:
    """Record a new re-index window for the current top-5 skills and return those skills.

    Idempotent: calling twice with the same skills overwrites the same Redis key, so a retry never
    creates a second window. Logs DEBUG when the window is opened.
    """
    skills = _top_skills(profile)
    if not skills:
        return []
    client = shared_redis_client()
    if client is None:
        log_debug("Profile-skills window: Redis unavailable — cannot open window", user_id=user_id)
        return skills
    try:
        client.setex(_key(user_id), WINDOW_DAYS * 24 * 60 * 60, json.dumps(skills))
    except Exception as e:
        log_debug("Profile-skills window: could not write Redis key", exc=e, user_id=user_id)
    return skills


def close_profile_skills_window(user_id: int) -> bool:
    """Close an open window explicitly (used mainly by tests)."""
    client = shared_redis_client()
    if client is None:
        return False
    try:
        client.delete(_key(user_id))
        return True
    except Exception:
        return False


def get_profile_skills_window(user_id: int) -> Optional[list]:
    """The currently open skill keywords, or None when the window is closed/expired.

    Fails open: if Redis is unavailable we report no window rather than block content generation.
    A missing `user_id` is answered without touching Redis — several generation entry points still
    have no user in hand, and they run at comment volume.
    """
    if not user_id:
        return None
    client = shared_redis_client()
    if client is None:
        return None
    try:
        raw = client.get(_key(user_id))
    except Exception as e:
        log_debug("Profile-skills window: could not read Redis key", exc=e, user_id=user_id)
        return None
    if raw is None:
        return None
    try:
        parsed = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        return parsed if isinstance(parsed, list) else None
    except (ValueError, TypeError, AttributeError):
        return None


def record_profile_skills_change(user_id: int, profile: "LinkedInProfile") -> list:
    """Check for a skills change and, if one is detected, open/refresh the re-index window.

    Returns the open window skills when a change triggered (or refreshed) the window, otherwise [].
    Callers run this on every successful profile re-scrape (daily beat + on-demand refresh).
    """
    if not skills_changed_since_last_recorded(user_id, profile):
        return []
    skills = _top_skills(profile)
    log_debug("Profile skills changed — opening re-index window",
              user_id=user_id, skills=skills)
    return open_profile_skills_window(user_id, profile)


def profile_skills_directive(user_id: int) -> str:
    """Soft subject steering from any open re-index window.

    Same contract as `focus_directive` in `content_alignment.py`: it layers on top of existing
    steering and must never override the actual subject. Returns "" when no window is open.
    """
    skills = get_profile_skills_window(user_id)
    if not skills:
        return ""
    text = ", ".join(skills)
    return (
        f"\n\nSoft steering (the user's profile recently highlighted these skills — weave them in "
        f"ONLY when they genuinely fit the subject; never force them):\n- Profile skills to echo: "
        f"{text}.\n"
    )
