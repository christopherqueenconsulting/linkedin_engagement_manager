"""Config + policy for the LEM brand (dogfooding) account — issue #504.

The brand account is a FIRST-CLASS user of LEM, not a special case: its 30-day content plan, feed
commenting, connect targeting, appreciation/outreach DMs + follow-ups, newsletter and company-page
invites all reach it through the same per-active-user beat tasks a paying customer goes through, and
its volume is enforced by the same `engagement_preferences` caps, 429 backoff (`rate_limit.py`) and
per-user proxy. So this module owns no outreach primitives at all — it only decides WHO the brand
user is, and WHAT its caps and focus topics should be for the current `LAUNCH_PHASE`, so
self-marketing can never quietly run hotter than the owner signed off on.
"""

import os
from typing import Any, Optional

from cqc_lem.utilities.env_constants import BRAND_USER_ID, LAUNCH_PHASE
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.marketing.attribution import CAMPAIGN_BRAND_PROFILE, MEDIUM_PROFILE, SOURCE_LINKEDIN, signup_url

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

# The volume + posture fields the phase owns. Since the brand user is also the owner's ORDINARY
# account (#736), the phase SEEDS an account that has never saved its own engagement preferences —
# it is not re-asserted over one that has. Issue #952: re-asserting it nightly pulled the owner's own
# Settings choice back down before morning (the SPA recommends the "Balanced" preset, 15/10/8 — the
# P1 numbers — while prod runs LAUNCH_PHASE=P0, so every night put it back to 8/5/5 with nothing in
# the UI to say why). Saved settings ARE the sign-off; an env var nobody touched is not.
CAP_FIELDS = ("max_comments_per_day", "max_dms_per_day", "max_invites_per_day")
MODE_FIELDS = ("connection_request_mode", "connection_targeting_mode")


def current_launch_phase() -> str:
    """The active rollout phase. An unrecognized value falls back to the most conservative phase
    rather than failing open — a typo in the deployment .env must never widen outbound volume.
    """
    phase = (LAUNCH_PHASE or "").strip().upper()
    if phase not in LAUNCH_PHASES:
        if phase:
            log_warning(f"Unknown LAUNCH_PHASE '{LAUNCH_PHASE}' — falling back to {DEFAULT_LAUNCH_PHASE}")
        return DEFAULT_LAUNCH_PHASE
    return phase


def brand_cap_ceiling(cap: str) -> int:
    """The ceiling for one brand cap. Only the comment ramp (#2647) lifts one above
    `BRAND_CAP_CEILINGS`, and only `max_comments_per_day`; a raw preference never does.
    """
    base = BRAND_CAP_CEILINGS[cap]
    if cap != "max_comments_per_day":
        return base
    try:
        from cqc_lem.utilities.comment_ramp import ramp_comment_ceiling
        return max(base, ramp_comment_ceiling(brand_user_id()))
    except Exception as exc:
        log_debug(f"Comment ramp unreadable — keeping the base brand ceiling: {exc}")
        return base


def brand_outbound_policy(phase: Optional[str] = None) -> dict:
    """The brand's caps + approval posture for `phase` (defaults to the active one), clamped to the
    per-user ceilings.
    """
    resolved = (phase or current_launch_phase()).strip().upper()
    policy = dict(PHASE_OUTBOUND_POLICY.get(resolved) or PHASE_OUTBOUND_POLICY[DEFAULT_LAUNCH_PHASE])
    for cap in BRAND_CAP_CEILINGS:
        policy[cap] = max(0, min(brand_cap_ceiling(cap), int(policy.get(cap) or 0)))
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
    """Whether `user_id` is the brand account.

    Used for attribution/observability and for the acquisition-outbound hold below — never to
    grant the brand anything a paying user does not have.
    """
    try:
        return int(user_id) == brand_user_id()
    except (TypeError, ValueError):
        return False


# Owner ruling 2026-10-10 (issue #2376): the brand runs NO automated outbound sourced for user
# acquisition — agents draft, the owner sends. This is deliberately narrow. It covers only the
# paths that SOURCE prospects for marketing: smart connection targeting (#486, `connection_requests`
# rows whose source is an own-post engager or an adjacent author's commenter) and the comment-first
# outreach funnel's connect and DM stages (#399/#623). The owner's ordinary engagement on the same
# account — feed comments, replies, DMs to existing connections, company-page invites, catch-up
# touches, seed comments, roster follows — keeps its caps. The strings mirror
# `connection_targeting.SOURCE_OWN_POST` / `SOURCE_ADJACENT_POST` and
# `engagement.outreach.CONNECTION_REQUEST_SOURCE_OUTREACH_FUNNEL`; a unit test pins them together.
ACQUISITION_CONNECT_SOURCES = frozenset({"own_post", "adjacent_post", "outreach_funnel"})
ACQUISITION_HOLD_REASON = "held: brand acquisition outbound is sent by the owner (issue #2376)"


def _hold_is_hard() -> bool:
    """`ACQUISITION_HOLD_HARD` restores the unconditional #2376 hold (the rollback switch)."""
    return os.getenv("ACQUISITION_HOLD_HARD", "").strip().lower() in ("1", "true", "yes", "on")


def acquisition_outbound_held(user_id: Any, source: Optional[str] = None,
                              funnel: bool = False, approved_by: Optional[str] = None) -> bool:
    """Whether an outbound action is brand acquisition outreach the ruling holds at 0.

    Args:
        user_id: The account the action would go out from.
        source: The `connection_requests.source` of a connect request, when there is one.
        funnel: True for a connect or DM stage of the comment-first outreach funnel.
        approved_by: The row's `approved_by`. A human Approve (`user:<id>`) is the owner's send
            decision and releases the hold (#2623); `system:*`, NULL and anything else do not.

    Returns:
        True only for the brand account AND an acquisition-sourced action without a human
        approval; False for every other user and for the brand's ordinary engagement. With
        `ACQUISITION_HOLD_HARD` set, a human approval no longer releases it.
    """
    if not is_brand_user(user_id):
        return False
    if not (bool(funnel) or (source or "").strip().lower() in ACQUISITION_CONNECT_SOURCES):
        return False
    return not (str(approved_by or "").startswith("user:") and not _hold_is_hard())


def held_connect_sources(user_id: Any) -> frozenset:
    """The `connection_requests.source` values whose rows can never send for `user_id`.

    The brand account's held acquisition drafts sit at 'pending' until the owner closes them, and an
    open row is counted against the daily invite budget. Lanes that still SEND (profile viewer,
    roster ladder) leave these out of that count so the held drafts do not starve them. Empty for
    every other user.
    """
    return ACQUISITION_CONNECT_SOURCES if is_brand_user(user_id) else frozenset()


def _over_ceiling(current: Any, ceiling: int) -> Optional[int]:
    """`current` pulled back under `ceiling`, or None when there is nothing to change.

    An unreadable value is also None: a cap this task cannot read is not a cap it may rewrite — the
    only source for it is `get_engagement_preferences`, which hands back code DEFAULTS when the read
    failed (issue #639), and rewriting from those is how a fault silently widens outbound.
    """
    try:
        saved = int(current)
    except (TypeError, ValueError):
        return None
    if 0 <= saved <= ceiling:
        return None
    return max(0, min(ceiling, saved))


def brand_preference_overrides(existing: Optional[dict] = None, phase: Optional[str] = None,
                               configured: bool = False) -> dict:
    """The engagement-preference fields the brand policy owns, given the account's current prefs.

    `configured` is whether the account has SAVED an engagement-preferences row of its own:

    * **Not configured** — the phase seeds the caps and the connect posture, exactly as before.
    * **Configured** — those values are the OWNER's (the brand user is also his ordinary account,
      issue #736), so the phase is not re-asserted over them and only `BRAND_CAP_CEILINGS` still
      binds: a cap above the shipped per-user default is pulled back, everything else is left alone.
      Re-asserting the phase every night is issue #952 — the product recommended a volume it then
      silently refused to keep.

    The seeded content fields (focus topics, business goals) are only filled when empty either way,
    so the brand's messaging can be retuned without this task stomping it back.
    """
    policy = brand_outbound_policy(phase)
    current = existing or {}
    overrides: dict = {}
    if configured:
        for cap in CAP_FIELDS:
            clamped = _over_ceiling(current.get(cap), brand_cap_ceiling(cap))
            if clamped is not None:
                overrides[cap] = clamped
    else:
        overrides.update({field: policy[field] for field in CAP_FIELDS + MODE_FIELDS})
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
    policy holds as ints, and focus topics as a list.
    """
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
    or not.
    """
    current = existing or {}
    return {field: (current.get(field), value) for field, value in overrides.items()
            if _comparable(current.get(field)) != _comparable(value)}


def sync_brand_preferences(phase: Optional[str] = None) -> Optional[dict]:
    """Push the current phase's outbound policy onto the brand account's engagement preferences.

    Returns the applied overrides — possibly EMPTY, when the account's own saved settings already
    sit inside the policy — or None when the sync could not run. The whole upsert is one row (the
    V52 incident), so ONLY the policy fields are sent — voice, tone and targeting the owner set are
    preserved by `update_engagement_preferences`, which merges over the saved row and aborts when it
    can't read it (issue #639). Re-sending `existing` here would defeat that abort: a transient read
    error makes `get_engagement_preferences` return code defaults, and this nightly task would then
    write all 39 of them over the brand account's real settings.

    Whether the account has settings of its own decides how much the phase owns (issue #952), so an
    UNREADABLE row skips the sync outright rather than reading as "never configured" — seeding the
    phase over the owner's real caps is the failure being fixed.
    """
    user_id = brand_user_id()
    from cqc_lem.utilities.db import (
        engagement_preferences_are_configured,
        get_engagement_preferences,
        update_engagement_preferences,
    )
    resolved_phase = (phase or current_launch_phase()).strip().upper()
    configured = engagement_preferences_are_configured(user_id)
    if configured is None:
        # The db layer already logged the read fault where it detected it — one condition, one
        # warning. The caller reports the skipped sync in its own return line.
        log_debug("Brand account preferences unreadable — skipping this sync", user_id=user_id)
        return None
    existing = get_engagement_preferences(user_id) or {}
    overrides = brand_preference_overrides(existing, resolved_phase, configured=configured)
    if not overrides:
        log_debug(f"Brand account within phase {resolved_phase} — nothing to apply", user_id=user_id)
        return overrides
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
