"""The write side of the Daily review: approve or cancel ONE queued item, per lane (#2625 Phase 2).

Each helper reproduces the guards of the lane's existing `PUT`/`DELETE` endpoint so a bulk call can
never release something the single-item path would refuse: ownership (a foreign id is "not found"),
`approved_by` from #2623, the stale-DM slot reset (#2103), and the non-empty-message rule on a
catch-up touch. The caller-level guard (`_refuse_agent_approval`) runs ONCE in the router before any
helper, because it depends on the request's session scope.

Only lanes whose rows can really be approved or cancelled appear in `ACTION_LANES`; every other lane
is edit-only and a bulk request naming it fails per item.
"""

from datetime import datetime, timezone
from typing import Callable, Optional

from cqc_lem.utilities.db import (
    CatchupTouchStatus,
    ConnectionRequestStatus,
    OutreachStatus,
    ScheduledDmStatus,
    approve_comment_candidate,
    cancel_comment_candidate,
    get_catchup_touch,
    get_connection_request_user_id,
    get_outreach_target_user_id,
    get_scheduled_dm,
    get_scheduled_dm_user_id,
    update_catchup_touch,
    update_catchup_touch_status,
    update_connection_request,
    update_connection_request_status,
    update_outreach_target,
    update_outreach_target_status,
    update_scheduled_dm,
    update_scheduled_dm_status,
    user_approver,
)
from cqc_lem.utilities.logger import log_error

BULK_CAP = 100
BULK_ACTIONS = ("approve", "cancel")

NOT_FOUND = "not_found"
NOT_WAITING = "not_waiting_for_review"
NOT_ACTIONABLE = "lane_not_actionable"
EMPTY_MESSAGE = "empty_message"
WRITE_FAILED = "write_failed"
# An agent-created row (MCP W2) is approved one at a time, never swept up in a multi-item approve.
APPROVE_ONE_AT_A_TIME = "agent_created_approve_individually"


def approve_scheduled_dm(user_id: int, item_id: int) -> Optional[str]:
    """Approve a queued DM; a slot already past its window moves to now (else the scanner bounces it)."""
    if get_scheduled_dm_user_id(item_id) != user_id:
        return NOT_FOUND
    from cqc_lem.app.engagement.outreach import scheduled_dm_is_stale
    dm = get_scheduled_dm(item_id)
    slot = datetime.now(timezone.utc) if dm and scheduled_dm_is_stale(dm.get("scheduled_time")) else None
    return None if update_scheduled_dm(item_id, scheduled_time=slot,
                                       status=ScheduledDmStatus.APPROVED) else WRITE_FAILED


def cancel_scheduled_dm(user_id: int, item_id: int) -> Optional[str]:
    """Soft-cancel a queued DM (status `canceled`, never deleted)."""
    if get_scheduled_dm_user_id(item_id) != user_id:
        return NOT_FOUND
    return None if update_scheduled_dm_status(item_id, ScheduledDmStatus.CANCELED) else WRITE_FAILED


def is_approved_unsent_dm(user_id: int, item_id: int) -> bool:
    """True for the caller's own DM that is approved but not yet dispatched — the Undo window.

    Such a row has left the review queue, so the router's "still waiting" check would refuse
    its Undo; cancel-until-sent is real only while the status is still `approved`.
    """
    dm = get_scheduled_dm(item_id)
    return bool(dm and dm.get("user_id") == user_id and dm.get("status") == ScheduledDmStatus.APPROVED)


def approve_connection_request(user_id: int, item_id: int) -> Optional[str]:
    """Approve a connection request, stamping `approved_by`."""
    if get_connection_request_user_id(item_id) != user_id:
        return NOT_FOUND
    return None if update_connection_request(item_id, status=ConnectionRequestStatus.APPROVED,
                                             approved_by=user_approver(user_id)) else WRITE_FAILED


def cancel_connection_request(user_id: int, item_id: int) -> Optional[str]:
    """Soft-cancel a connection request."""
    if get_connection_request_user_id(item_id) != user_id:
        return NOT_FOUND
    return None if update_connection_request_status(
        item_id, ConnectionRequestStatus.CANCELED) else WRITE_FAILED


def approve_funnel_target(user_id: int, item_id: int) -> Optional[str]:
    """Approve the funnel target's current stage, stamping `approved_by`."""
    if get_outreach_target_user_id(item_id) != user_id:
        return NOT_FOUND
    return None if update_outreach_target(item_id, status=OutreachStatus.APPROVED,
                                          approved_by=user_approver(user_id)) else WRITE_FAILED


def cancel_funnel_target(user_id: int, item_id: int) -> Optional[str]:
    """Aborts the WHOLE funnel for this target, not just the pending stage."""
    if get_outreach_target_user_id(item_id) != user_id:
        return NOT_FOUND
    return None if update_outreach_target_status(item_id, OutreachStatus.CANCELED) else WRITE_FAILED


def approve_catchup_touch(user_id: int, item_id: int) -> Optional[str]:
    """Approve a catch-up touch; a blank message is refused (it would become a permanent skip)."""
    touch = get_catchup_touch(item_id)
    if not touch or touch.get("user_id") != user_id:
        return NOT_FOUND
    if not (touch.get("message") or "").strip():
        return EMPTY_MESSAGE
    return None if update_catchup_touch(item_id, status=CatchupTouchStatus.APPROVED) else WRITE_FAILED


def cancel_catchup_touch(user_id: int, item_id: int) -> Optional[str]:
    """Soft-cancel a catch-up touch (the row stays as the dedup tombstone)."""
    touch = get_catchup_touch(item_id)
    if not touch or touch.get("user_id") != user_id:
        return NOT_FOUND
    return None if update_catchup_touch_status(item_id, CatchupTouchStatus.CANCELED) else WRITE_FAILED


def approve_candidate_draft(user_id: int, item_id: int) -> Optional[str]:
    """Approve a drafted comment; it posts only through the automated lane or Assist, never here.

    The move is one UPDATE conditioned on `drafted` and the caller's id, so a foreign, canceled or
    already-approved row is `NOT_FOUND` and nothing changes.
    """
    return None if approve_comment_candidate(user_id, item_id) else NOT_FOUND


def cancel_candidate_draft(user_id: int, item_id: int) -> Optional[str]:
    """Cancel a drafted comment; the row stays as the dedupe tombstone."""
    return None if cancel_comment_candidate(user_id, item_id) else NOT_FOUND


ACTION_LANES: dict[str, dict[str, Callable[[int, int], Optional[str]]]] = {
    "scheduled_dms": {"approve": approve_scheduled_dm, "cancel": cancel_scheduled_dm},
    "connection_requests": {"approve": approve_connection_request, "cancel": cancel_connection_request},
    "outreach_funnel_targets": {"approve": approve_funnel_target, "cancel": cancel_funnel_target},
    "catchup_touches": {"approve": approve_catchup_touch, "cancel": cancel_catchup_touch},
    "comment_candidates": {"approve": approve_candidate_draft, "cancel": cancel_candidate_draft},
}


def apply_review_action(user_id: int, lane: str, item_id: int, action: str) -> dict:
    """Run one item; never raises — a fault is that item's `error`, the rest of a batch still runs."""
    handler = ACTION_LANES.get(lane, {}).get(action)
    if handler is None:
        return {"lane": lane, "id": item_id, "ok": False, "error": NOT_ACTIONABLE}
    try:
        error = handler(user_id, item_id)
    except Exception as e:
        log_error("Review action failed", exc=e, user_id=user_id, action_type="review_bulk")
        error = WRITE_FAILED
    return {"lane": lane, "id": item_id, "ok": error is None, "error": error}
