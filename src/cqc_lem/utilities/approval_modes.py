"""Per-user approval mode for the outbound lanes (issue #2624).

ONE place that turns a stored `*_mode` preference plus the lane's process-wide env switch into
"may this lane skip human review for this user".
"""
import os
from typing import Optional

PRE_REVIEW = "pre_review"
AUTO_APPROVE = "auto_approve"
VALID_APPROVAL_MODES = (PRE_REVIEW, AUTO_APPROVE)

# Mode field -> the env var that can force that lane back to review for the whole deploy.
NURTURE_DM_MODE = "nurture_dm_mode"
ARTIFACT_DM_MODE = "artifact_dm_mode"
GROUP_POST_MODE = "group_post_mode"
FOLLOWUP_STEP_MODE = "followup_step_mode"
APPRECIATION_DM_MODE = "appreciation_dm_mode"
FUNNEL_STAGE_MODE = "funnel_stage_mode"
# `outreach_funnel_targets.approved_by` for a stage the mode approved (#2664). A `system:` value, so
# it never releases the brand acquisition hold the way a human `user:<id>` Approve does (#2623).
FUNNEL_STAGE_APPROVER = "system:funnel_stage_mode"
APPROVAL_MODE_FIELDS = (NURTURE_DM_MODE, ARTIFACT_DM_MODE, GROUP_POST_MODE, FOLLOWUP_STEP_MODE,
                        APPRECIATION_DM_MODE, FUNNEL_STAGE_MODE)
APPROVAL_MODE_ENV = {
    NURTURE_DM_MODE: "DM_NURTURE_AUTO_APPROVE",
    ARTIFACT_DM_MODE: "DM_ARTIFACT_AUTO_APPROVE",
    GROUP_POST_MODE: "GROUP_POST_AUTO_APPROVE",
    FOLLOWUP_STEP_MODE: "DM_FOLLOWUP_AUTO_APPROVE",
    APPRECIATION_DM_MODE: "DM_APPRECIATION_AUTO_APPROVE",
    FUNNEL_STAGE_MODE: "FUNNEL_STAGE_AUTO_APPROVE",
}
_OFF_VALUES = ("0", "false", "no", "off")


def env_forces_review(field: str) -> bool:
    """Whether the lane's env var is set to an explicit off-value (0/false/no/off).

    Only an off-value forces review; unset, blank or anything else defers to the per-user mode, so
    a typo can never switch a lane ON, and the env can never switch one on at all.
    """
    raw = os.environ.get(APPROVAL_MODE_ENV[field])
    return raw is not None and raw.strip().lower() in _OFF_VALUES


def may_auto_approve(user_id: int, field: str, prefs: Optional[dict] = None) -> bool:
    """True only when the user chose `auto_approve` for this lane and no env force-off applies.

    Fails CLOSED: a missing/unreadable preference, or an unknown stored value, means review.

    Args:
        user_id: Whose preference to read when `prefs` is not supplied.
        field: One of `APPROVAL_MODE_FIELDS`.
        prefs: Already-loaded engagement preferences; authoritative when given, so a dict without
            the field means review (not a second read).
    """
    if env_forces_review(field):
        return False
    if prefs is None:
        from cqc_lem.utilities.db import get_engagement_preferences
        prefs = get_engagement_preferences(user_id)
    return (prefs or {}).get(field) == AUTO_APPROVE
