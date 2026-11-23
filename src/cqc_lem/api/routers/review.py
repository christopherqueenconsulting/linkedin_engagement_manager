"""`/api/review/*` — the cross-lane Daily review read side (#2625, Phase 1).

Read-only. The caller resolves through `get_session_user_id` (as an attribute of `main`, see
`routers/outreach.py` for why). `/api/review/*` is deliberately in NO scoped session surface, so
agent, operator and observer sessions get the resolver's 403: the queue exposes draft text the
author has not yet approved, and it is the page that approves.
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from cqc_lem.api.models import ResponseModel
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


# LAST, as in routers/outreach.py: both routes are bound before `main` can include this router.
from cqc_lem.api import main as _main  # noqa: E402
