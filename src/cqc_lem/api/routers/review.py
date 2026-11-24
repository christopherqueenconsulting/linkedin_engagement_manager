"""`/api/review/*` — the cross-lane Daily review read side (#2625, Phase 1).

Reads, plus `POST /bulk` (Phase 2). The caller resolves through `get_session_user_id` (as an attribute of `main`, see
`routers/outreach.py` for why). `/api/review/*` is deliberately in NO scoped session surface, so
agent, operator and observer sessions get the resolver's 403: the queue exposes draft text the
author has not yet approved, and it is the page that approves.
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cqc_lem.api.models import ResponseModel
from cqc_lem.utilities.review_actions import (
    BULK_ACTIONS,
    BULK_CAP,
    NOT_WAITING,
    apply_review_action,
    is_approved_unsent_dm,
)
from cqc_lem.utilities.review_queue import LANE_IDS, get_review_queue, summarize

router = APIRouter(prefix="/api/review")


@router.get("/queue")
def review_queue_endpoint(session_token: str,
                          lane: Optional[str] = None) -> ResponseModel[list[dict[str, Any]]]:
    """Every approval waiting on the caller, oldest first; `lane` narrows to one."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if lane is not None and lane not in LANE_IDS:
        raise HTTPException(status_code=422, detail=f"Unknown lane; expected one of {list(LANE_IDS)}")
    return ResponseModel(status_code=200, detail=get_review_queue(user_id, lane=lane))


@router.get("/summary")
def review_summary_endpoint(session_token: str) -> ResponseModel[dict[str, Any]]:
    """`{total, stale_48h, oldest_age_hours, by_lane}` for the nav badge."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=summarize(get_review_queue(user_id)))


class BulkItem(BaseModel):
    """One queue item named by lane and row id."""

    lane: str
    id: int


class BulkReviewRequest(BaseModel):
    """Body of `POST /api/review/bulk` — at most `BULK_CAP` items, one action for all."""

    session_token: str
    items: list[BulkItem]
    action: str


@router.post("/bulk")
def review_bulk_endpoint(request: BulkReviewRequest) -> ResponseModel[list[dict[str, Any]]]:
    """Approve or cancel several queue items; the answer is one `{lane,id,ok,error}` per item.

    Approving releases live sends, so the caller-level guard runs first (an agent session is a 403
    for the whole request) and each item then goes through the same guards as its single-item
    endpoint. A funnel-target cancel aborts that target's WHOLE funnel. A fault on one item is that
    item's error and never stops the rest.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if request.action not in BULK_ACTIONS:
        raise HTTPException(status_code=422, detail=f"Unknown action; expected one of {list(BULK_ACTIONS)}")
    if not request.items:
        raise HTTPException(status_code=422, detail="No items given")
    if len(request.items) > BULK_CAP:
        raise HTTPException(status_code=422, detail=f"At most {BULK_CAP} items per request")
    _main._refuse_agent_approval(request.action)
    # The single-item endpoints trust the id; a bulk call from a stale page would re-approve a row
    # already sent or canceled (a second live send). Only rows still waiting in the queue are acted on.
    waiting = {(q["lane"], q["id"]) for q in get_review_queue(user_id)
               if request.action in q["allowed_actions"]}
    def runnable(i: BulkItem) -> bool:
        if (i.lane, i.id) in waiting:
            return True
        # Undo of a just-approved scheduled DM: it has left the queue but has not been sent.
        return (request.action == "cancel" and i.lane == "scheduled_dms"
                and is_approved_unsent_dm(user_id, i.id))

    results = [apply_review_action(user_id, i.lane, i.id, request.action) if runnable(i)
               else {"lane": i.lane, "id": i.id, "ok": False, "error": NOT_WAITING}
               for i in request.items]
    return ResponseModel(status_code=200, detail=results)


# LAST, as in routers/outreach.py: both routes are bound before `main` can include this router.
from cqc_lem.api import main as _main  # noqa: E402
