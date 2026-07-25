"""Config + policy for the LEM brand (dogfooding) account — issue #504.

The brand account is a FIRST-CLASS user of LEM, not a special case: its 30-day content plan, feed
commenting, connect targeting, appreciation/outreach DMs + follow-ups, newsletter and company-page
invites all reach it through the same per-active-user beat tasks a paying customer goes through, and
its volume is enforced by the same `engagement_preferences` caps, 429 backoff (`rate_limit.py`) and
per-user proxy. So this module owns no outreach primitives at all — it only decides WHAT the brand
user's caps and focus topics should be for the current `LAUNCH_PHASE`, so self-marketing can never
quietly run hotter than the owner signed off on.
"""

from typing import Optional

from cqc_lem.utilities.env_constants import (BRAND_ACCOUNT_EMAIL, BRAND_ACCOUNT_ENABLED,
                                             BRAND_SIGNUP_URL, LAUNCH_PHASE)
from cqc_lem.utilities.logger import log_debug, log_warning

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


def get_brand_user_id() -> Optional[int]:
    """The brand account's user id, or None when dogfooding is off, no email is configured, or no
    user matches it — every caller treats None as "there is no brand account to drive"."""
    if not BRAND_ACCOUNT_ENABLED:
        return None
    email = (BRAND_ACCOUNT_EMAIL or "").strip()
    if not email:
        log_warning("Brand account enabled but BRAND_ACCOUNT_EMAIL is unset — skipping")
        return None
    from cqc_lem.utilities.db import get_user_id
    user_id = get_user_id(email)
    if not user_id:
        log_warning(f"Brand account email {email} does not match any user — skipping")
        return None
    return int(user_id)


def is_brand_user(user_id: int) -> bool:
    """Whether `user_id` is the brand account (for attribution/observability, not for privileges)."""
    brand_id = get_brand_user_id()
    return brand_id is not None and int(user_id) == brand_id


def brand_preference_overrides(existing: Optional[dict] = None, phase: Optional[str] = None) -> dict:
    """The engagement-preference fields the brand policy owns, given the account's current prefs.

    Caps and approval posture are ALWAYS re-asserted from the phase — that is the volume gate, so a
    manual edit in the SPA must not survive it. The seeded content fields (focus topics, business
    goals) are only filled when empty, so the owner can retune the brand's messaging without this
    task stomping it back every night.
    """
    overrides = brand_outbound_policy(phase)
    current = existing or {}
    if not [t for t in (current.get("focus_topics") or []) if str(t).strip()]:
        overrides["focus_topics"] = list(BRAND_FOCUS_TOPICS)
    signup_url = (BRAND_SIGNUP_URL or "").strip()
    if signup_url and not (current.get("business_goals") or "").strip():
        overrides["business_goals"] = f"Drive free-trial signups at {signup_url}"
    return overrides


def sync_brand_preferences(phase: Optional[str] = None) -> Optional[dict]:
    """Push the current phase's outbound policy onto the brand account's engagement preferences.

    Returns the applied overrides, or None when there is no brand account to sync. The whole upsert
    is one row (the V52 incident), so the existing prefs are merged in rather than replaced — voice,
    tone and targeting the owner set stay exactly as they are.
    """
    user_id = get_brand_user_id()
    if user_id is None:
        return None
    from cqc_lem.utilities.db import get_engagement_preferences, update_engagement_preferences
    resolved_phase = (phase or current_launch_phase()).strip().upper()
    existing = get_engagement_preferences(user_id) or {}
    overrides = brand_preference_overrides(existing, resolved_phase)
    if not update_engagement_preferences(user_id, {**existing, **overrides}):
        log_warning("Could not sync brand account preferences", user_id=user_id)
        return None
    log_debug(f"Brand account synced to phase {resolved_phase}", user_id=user_id)
    return overrides
