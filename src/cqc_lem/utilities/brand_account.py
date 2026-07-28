"""Config + policy for the LEM brand (dogfooding) account — issue #504.

The brand account is a FIRST-CLASS user of LEM, not a special case: its 30-day content plan, feed
commenting, connect targeting, appreciation/outreach DMs + follow-ups, newsletter and company-page
invites all reach it through the same per-active-user beat tasks a paying customer goes through, and
its volume is enforced by the same `engagement_preferences` caps, 429 backoff (`rate_limit.py`) and
per-user proxy. So this module owns no outreach primitives at all — it only decides WHO the brand
user is, and WHAT its caps and focus topics should be for the current `LAUNCH_PHASE`, so
self-marketing can never quietly run hotter than the owner signed off on.
"""

from typing import Any, Optional

from cqc_lem.utilities.env_constants import BRAND_USER_ID, LAUNCH_PHASE
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.marketing.attribution import (CAMPAIGN_BRAND_PROFILE, MEDIUM_PROFILE,
                                                     SOURCE_LINKEDIN, signup_url)

# The brand account is user 1 BY CONVENTION (issue #736): the first account onboarded on the box is
# the owner's own, and it permanently doubles as the LEM brand account. The old
# BRAND_ACCOUNT_ENABLED/BRAND_ACCOUNT_EMAIL pair kept the whole self-marketing engine dormant for
# months because two env vars were never set in prod — a wiring flag that only ever failed closed.
DEFAULT_BRAND_USER_ID = 1

# Rollout phases from the launch plan (docs/launch-and-marketing-plan.md §A.1): P0 private
# early-adopter, P1 open beta, P2 GA. Advancing a phase is the owner's call.
LAUNCH_PHASES = ("P0", "P1", "P2")
DEFAULT_LAUNCH_PHASE = "P0"

# The ICP's pains, verbatim from the launch plan §C.2 — the brand posts about the problem LEM
# solves, so every post doubles as a live demo of the product's own output.
BRAND_FOCUS_TOPICS = (
    "consistent LinkedIn presence without the grind",
    "solo-founder pipeline",
    "AI content that sounds like you",
)

# Hard ceilings: the brand is NEVER special-cased to go faster than a paying user, so no phase may
# raise a cap above the product's own out-of-the-box defaults (see _ENGAGEMENT_DEFAULTS in db.py).
BRAND_CAP_CEILINGS = {"max_comments_per_day": 20, "max_dms_per_day": 20, "max_invites_per_day": 10}

# Outbound volume + approval posture per phase. Volume ramps only as the account warms and the
# ToS-safety signals hold; P0 additionally keeps a human on every connect (`pre_review`) and files
# sourced targets as drafts only (`suggest`).
PHASE_OUTBOUND_POLICY = {
    "P0": {"max_comments_per_day": 8, "max_dms_per_day": 5, "max_invites_per_day": 5,
           "connection_request_mode": "pre_review", "connection_targeting_mode": "suggest"},
    "P1": {"max_comments_per_day": 15, "max_dms_per_day": 10, "max_invites_per_day": 8,
           "connection_request_mode": "auto_approve", "connection_targeting_mode": "auto_queue"},
    "P2": {"max_comments_per_day": 20, "max_dms_per_day": 15, "max_invites_per_day": 10,
           "connection_request_mode": "auto_approve", "connection_targeting_mode": "auto_queue"},
}

# The phase's numeric caps. Since the brand user is also the owner's ORDINARY account (#736), the
# phase is applied as a CEILING, not as an assignment: it can only ever pull a cap DOWN. A cap the
# owner tuned lower by hand is stricter than the policy, so honouring it can never widen outbound —
# and stomping it nightly is exactly the silent overwrite this convention would otherwise introduce.
CAP_FIELDS = ("max_comments_per_day", "max_dms_per_day", "max_invites_per_day")

# Same rule for the approval posture, which has no numeric scale: strictest first. `pre_review` /
# `off` gate more than the phase does, so a hand-set stricter mode survives the sync.
MODE_STRICTNESS = {
    "connection_request_mode": ("pre_review", "auto_approve"),
    "connection_targeting_mode": ("off", "suggest", "auto_queue"),
}


def current_launch_phase() -> str:
    """The active rollout phase. An unrecognized value falls back to the most conservative phase
    rather than failing open — a typo in the deployment .env must never widen outbound volume."""
    phase = (LAUNCH_PHASE or "").strip().upper()
    if phase not in LAUNCH_PHASES:
        if phase:
            log_warning(f"Unknown LAUNCH_PHASE '{LAUNCH_PHASE}' — falling back to {DEFAULT_LAUNCH_PHASE}")
        return DEFAULT_LAUNCH_PHASE
    return phase


def brand_outbound_policy(phase: Optional[str] = None) -> dict:
    """The brand's caps + approval posture for `phase` (defaults to the active one), clamped to the
    per-user ceilings."""
    resolved = (phase or current_launch_phase()).strip().upper()
    policy = dict(PHASE_OUTBOUND_POLICY.get(resolved) or PHASE_OUTBOUND_POLICY[DEFAULT_LAUNCH_PHASE])
    for cap, ceiling in BRAND_CAP_CEILINGS.items():
        policy[cap] = max(0, min(ceiling, int(policy.get(cap) or 0)))
    return policy


def brand_user_id() -> int:
    """The brand account's user id — user 1 unless a deployment explicitly overrides it.

    Never None: the brand engine has to run with ZERO configuration, so an unset, blank or
    unparseable `BRAND_USER_ID` resolves to the convention rather than switching self-marketing off.
    """
    raw = str(BRAND_USER_ID or "").strip()
    if not raw:
        return DEFAULT_BRAND_USER_ID
    try:
        override = int(raw)
    except (TypeError, ValueError):
        log_warning(f"Unparseable BRAND_USER_ID '{BRAND_USER_ID}' — using user {DEFAULT_BRAND_USER_ID}")
        return DEFAULT_BRAND_USER_ID
    if override <= 0:
        log_warning(f"BRAND_USER_ID '{BRAND_USER_ID}' is not a valid user id — "
                    f"using user {DEFAULT_BRAND_USER_ID}")
        return DEFAULT_BRAND_USER_ID
    return override


def is_brand_user(user_id: Any) -> bool:
    """Whether `user_id` is the brand account (for attribution/observability, not for privileges)."""
    try:
        return int(user_id) == brand_user_id()
    except (TypeError, ValueError):
        return False


def _capped(current: Any, ceiling: int) -> int:
    """`current` held under the phase's ceiling. A missing/unreadable saved value takes the ceiling —
    "no opinion" is not the same as "the owner asked for less"."""
    try:
        saved = int(current)
    except (TypeError, ValueError):
        return ceiling
    return max(0, min(ceiling, saved))


def _strictest(current: Any, phase_value: str, order: tuple) -> str:
    """Whichever of the saved and phase values gates more (`order` is strictest-first). A saved value
    outside the known vocabulary is not comparable, so the phase's wins."""
    if current not in order:
        return phase_value
    if phase_value not in order:
        return str(current)
    return str(current) if order.index(str(current)) < order.index(phase_value) else phase_value


def brand_preference_overrides(existing: Optional[dict] = None, phase: Optional[str] = None) -> dict:
    """The engagement-preference fields the brand policy owns, given the account's current prefs.

    Caps and approval posture are re-asserted from the phase as a CEILING — that is the volume gate,
    so a manual edit can never raise brand outbound above what was signed off on, but a value the
    owner tuned STRICTER survives (the brand user is also his ordinary account, issue #736, and
    clamping down is the only direction that matters for safety). The seeded content fields (focus
    topics, business goals) are only filled when empty, so he can retune the brand's messaging
    without this task stomping it back every night.
    """
    policy = brand_outbound_policy(phase)
    current = existing or {}
    overrides = dict(policy)
    for cap in CAP_FIELDS:
        overrides[cap] = _capped(current.get(cap), policy[cap])
    for field, order in MODE_STRICTNESS.items():
        overrides[field] = _strictest(current.get(field), policy[field], order)
    if not [t for t in (current.get("focus_topics") or []) if str(t).strip()]:
        overrides["focus_topics"] = list(BRAND_FOCUS_TOPICS)
    # The goal line is read by the content prompts, so the URL in it is the one the brand's posts
    # and DMs echo — it has to arrive UTM-tagged or every signup it drives reads as `direct` (#658).
    tagged_signup = signup_url(SOURCE_LINKEDIN, MEDIUM_PROFILE, CAMPAIGN_BRAND_PROFILE)
    if tagged_signup and not (current.get("business_goals") or "").strip():
        overrides["business_goals"] = f"Drive free-trial signups at {tagged_signup}"
    return overrides


def _comparable(value: Any) -> Any:
    """`value` in a shape two sources can be compared in — the DB hands back strings for numbers the
    policy holds as ints, and focus topics as a list."""
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value)
    if value is None or isinstance(value, bool):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value).strip()


def preference_changes(existing: Optional[dict], overrides: dict) -> dict:
    """The fields this sync would actually change, as `{field: (before, after)}`. The brand user is
    the owner's own account, so a sync that edits his settings has to be readable in the logs rather
    than inferred from a nightly "synced" line that says the same thing whether it changed anything
    or not."""
    current = existing or {}
    return {field: (current.get(field), value) for field, value in overrides.items()
            if _comparable(current.get(field)) != _comparable(value)}


def sync_brand_preferences(phase: Optional[str] = None) -> Optional[dict]:
    """Push the current phase's outbound policy onto the brand account's engagement preferences.

    Returns the applied overrides, or None when the upsert failed. The whole upsert is one row (the
    V52 incident), so ONLY the policy fields are sent — voice, tone and targeting the owner set are
    preserved by `update_engagement_preferences`, which merges over the saved row and aborts when it
    can't read it (issue #639). Re-sending `existing` here would defeat that abort: a transient read
    error makes `get_engagement_preferences` return code defaults, and this nightly task would then
    write all 39 of them over the brand account's real settings.
    """
    user_id = brand_user_id()
    from cqc_lem.utilities.db import get_engagement_preferences, update_engagement_preferences
    resolved_phase = (phase or current_launch_phase()).strip().upper()
    existing = get_engagement_preferences(user_id) or {}
    overrides = brand_preference_overrides(existing, resolved_phase)
    changes = preference_changes(existing, overrides)
    if not update_engagement_preferences(user_id, overrides):
        log_warning("Could not sync brand account preferences", user_id=user_id)
        return None
    if changes:
        summary = ", ".join(f"{field}: {before!r} -> {after!r}"
                            for field, (before, after) in sorted(changes.items()))
        log_info(f"Brand phase {resolved_phase} changed engagement preferences — {summary}",
                 user_id=user_id, task_name="sync_brand_preferences")
    else:
        log_debug(f"Brand account already at phase {resolved_phase}", user_id=user_id)
    return overrides
