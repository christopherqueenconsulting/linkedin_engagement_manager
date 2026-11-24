"""`/api/assist/queue` and `/api/review/comment-posted` — the two routes LEM Assist may call (#2647 P4).

LEM Assist is a sideloaded browser extension that types a comment a human already approved into
LinkedIn's own composer. The human presses Post; the extension never does. Its `assist` session scope
reaches exactly these two paths, and only with the verb each one needs (`_ASSIST_PATH_METHODS` in
`api/main.py`).

- `GET /assist/queue` lists approved, unposted candidates, no more than the day's comment budget
  still allows.
- `POST /review/comment-posted` reports one candidate posted. It refuses anything that is not
  `approved` (409), and records the comment where every other comment is recorded, so Assist
  comments spend the same per-day cap, pacing envelope, per-author weekly cap and suppression
  baseline as an automated one.
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cqc_lem.api.models import ResponseModel
from cqc_lem.utilities.db import (
    LogActionType,
    LogResultType,
    claim_approved_candidate_posted,
    claim_post_for_comment,
    count_comments_today,
    get_approved_comment_candidates,
    insert_new_log,
    mark_post_commented,
    record_target_engagement,
)
from cqc_lem.utilities.human_pacing import ACTION_COMMENT, record_action

router = APIRouter(prefix="/api")

# Hard server-side ceiling on what the queue ever offers in one day, whatever the preference says.
ASSIST_DAILY_CEILING = 100


class CommentPostedRequest(BaseModel):
    """Body of `POST /api/review/comment-posted`."""

    session_token: Optional[str] = None
    candidate_id: int


def _caller(session_token: Optional[str]) -> int:
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user_id


def _daily_cap(user_id: int) -> int:
    prefs = _main.get_engagement_preferences(user_id) or {}
    try:
        cap = int(prefs.get("max_comments_per_day") or 0)
    except (TypeError, ValueError):
        cap = 0
    return max(0, min(cap, ASSIST_DAILY_CEILING))


@router.get("/assist/queue")
def assist_queue(session_token: Optional[str] = None) -> ResponseModel[dict[str, Any]]:
    """Approved comments ready to be typed in, limited to today's remaining comment budget.

    A read failure is a 503, never an empty queue.
    """
    user_id = _caller(session_token)
    remaining = max(0, _daily_cap(user_id) - count_comments_today(user_id))
    rows = get_approved_comment_candidates(user_id, limit=remaining) if remaining else []
    if rows is None:
        raise HTTPException(status_code=503, detail="Could not read the approved queue")
    items = [{"candidate_id": r["id"], "post_url": r.get("post_url"), "author": r.get("author"),
              "text": r.get("draft_text")} for r in rows]
    return ResponseModel(status_code=200, detail={"items": items, "remaining_today": remaining})


@router.post("/review/comment-posted")
def comment_posted(request: CommentPostedRequest) -> ResponseModel[dict[str, Any]]:
    """Record that the human posted an approved candidate's comment.

    The claim is one conditional UPDATE on `status = 'approved'`, so an unapproved, foreign or
    already-reported candidate is a 409 and writes nothing.
    """
    user_id = _caller(request.session_token)
    row = claim_approved_candidate_posted(user_id, request.candidate_id)
    if not row:
        raise HTTPException(status_code=409, detail={
            "code": "candidate_not_approved",
            "message": "That candidate is not approved, or was already reported posted."})
    key = row.get("post_key")
    claim_post_for_comment(user_id, key)  # a ledger row to promote; False if one already exists
    mark_post_commented(user_id, key)
    insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT,
                   result=LogResultType.SUCCESS, post_url=key, message=row.get("draft_text"))
    record_action(user_id, ACTION_COMMENT)
    author = row.get("author")
    if author and str(author).startswith("http"):
        record_target_engagement(user_id, author)
    return ResponseModel(status_code=200, detail={"recorded": True})


from cqc_lem.api import main as _main  # noqa: E402
