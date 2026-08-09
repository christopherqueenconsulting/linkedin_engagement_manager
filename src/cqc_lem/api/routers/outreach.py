"""`/api/outreach/*` — the comment-first outreach funnel (#399), split out of `main.py` (#1154).

First slice of the router split, and it sets the mechanic for the rest.

**The auth kernel does NOT move.** `get_session_user_id` is the ONE resolver (CLAUDE.md), and its
transitive closure inside `main.py` is 33 symbols / ~300 LOC — `_scope_checked`, `_SCOPE_SURFACES`,
`_AGENT_SESSION_SURFACE`, `_require_client_header`, `_bearer_authenticated`, the lot. That is every
documented auth invariant (#914, #950, #905, #1026, #957) in one move, and it would break ~596 test
patch sites that target `cqc_lem.api.main.get_session_user_id`.

So the handlers below reach it as an ATTRIBUTE of the host module (`_main.get_session_user_id`),
resolved at REQUEST time rather than bound at import time. Two things follow, and both are the
point: there is no import cycle, and `patch("cqc_lem.api.main.get_session_user_id")` still binds
exactly what the handler reads. What DOES move — the six `outreach_*` db functions — is read from
THIS module's globals, so a patch aimed at `main` for one of those correctly stops working and has
to be re-pointed here. Loud, not silent.

`from cqc_lem.api import main as _main` sits at the BOTTOM on purpose; see `routers/__init__.py` for
the prefix rule and the import-order reasoning.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from cqc_lem.api.models import ResponseModel
from cqc_lem.utilities.db import (
    OutreachStatus,
    get_outreach_target_by_url,
    get_outreach_target_user_id,
    get_outreach_targets,
    insert_outreach_target,
    update_outreach_target,
    update_outreach_target_status,
)

# The FULL prefix, declared here rather than passed to include_router: `route.path` is what every
# scope and admin check reads, and an include-time prefix never reaches it.
router = APIRouter(prefix="/api/outreach")


# Comment-first outreach funnel (issue #399) — approval-gated comment->connect->DM
_LEN_OUTREACH_URL = 512    # outreach_funnel_targets.target_profile_url / context_url VARCHAR(512)
_LEN_OUTREACH_NAME = 255   # outreach_funnel_targets.target_name VARCHAR(255)
_LEN_OUTREACH_DRAFT = 3000  # outreach_funnel_targets.draft_text (TEXT; app cap)


class OutreachTargetRequest(BaseModel):
    """Body of `POST /outreach/target` — add ONE prospect to the comment-first funnel (issue #399).

    A target enters at the `comment` stage. `approved` releases only that stage: the processor
    re-drops each fired stage to `pending`, so approving once never lets the whole funnel run away.
    """

    session_token: str
    target_profile_url: str = Field(max_length=_LEN_OUTREACH_URL)
    target_name: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_NAME)
    context_url: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_URL)
    draft_text: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_DRAFT)
    status: str = "pending"  # 'pending' (draft) or 'approved' (let the processor fire this stage)


class UpdateOutreachTargetRequest(BaseModel):
    """Body of `PUT /outreach/target`.

    The two actions are asymmetric: `approve` gates the CURRENT stage only, while `cancel` aborts the whole funnel
    for this target.
    """

    session_token: str
    target_id: int
    target_name: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_NAME)
    context_url: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_URL)
    draft_text: Optional[str] = Field(default=None, max_length=_LEN_OUTREACH_DRAFT)
    action: Optional[str] = None  # 'approve' | 'cancel' | None (save fields only)


class OutreachTargetDeleteRequest(BaseModel):
    """Body of `DELETE /outreach/target` — a SOFT cancel to `canceled`.

    No further stage fires for this target afterwards.
    """

    session_token: str
    target_id: int


@router.post("/target")
def create_outreach_target_endpoint(request: OutreachTargetRequest) -> ResponseModel:
    """Add a prospect to the comment-first outreach funnel (issue #399). The target starts at the
    'comment' stage; every stage is approval-gated — the funnel processor only acts on APPROVED
    stages and re-drops each fired stage to 'pending', so no step auto-fires at volume.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # Normalize at the boundary so whitespace variants ("…/in/jane" vs "…/in/jane ") can't slip past
    # the duplicate check and the unique constraint as distinct rows.
    target_profile_url = request.target_profile_url.strip()
    target_name = request.target_name.strip() if request.target_name else None
    context_url = request.context_url.strip() if request.context_url else None
    if not target_profile_url:
        raise HTTPException(status_code=422, detail="target_profile_url is required")
    if get_outreach_target_by_url(user_id, target_profile_url):
        raise HTTPException(status_code=409, detail="Target is already in the outreach funnel")
    _main._refuse_agent_approved_status(request.status)
    status = OutreachStatus.APPROVED if request.status == "approved" else OutreachStatus.PENDING
    target_id = insert_outreach_target(user_id, target_profile_url,
                                       target_name=target_name, context_url=context_url,
                                       draft_text=request.draft_text, status=status)
    if not target_id:
        raise HTTPException(status_code=500, detail="Could not create outreach target")
    return ResponseModel(status_code=200, detail={"target_id": target_id})


@router.get("/targets")
def list_outreach_targets_endpoint(session_token: str, status_filter: Optional[str] = None,
                                   stage_filter: Optional[str] = None, page: int = 1,
                                   page_size: int = 25, sort_order: str = "asc") -> ResponseModel:
    """The outreach funnel board.

    `stage_filter` and `status_filter` are different questions — which STEP a target is on, versus whether that step
    is approved to fire.
    """
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return ResponseModel(status_code=200, detail=get_outreach_targets(
        user_id, status_filter=status_filter, stage_filter=stage_filter, page=page,
        page_size=page_size, sort_order=sort_order))


@router.put("/target")
def update_outreach_target_endpoint(request: UpdateOutreachTargetRequest) -> ResponseModel:
    """Edit a funnel target's current-stage draft, or approve/cancel it. 'approve' gates the current
    stage for the processor; 'cancel' aborts the whole funnel for this target.
    """
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_outreach_target_user_id(request.target_id) != user_id:
        raise HTTPException(status_code=404, detail="Outreach target not found")
    action_map = {"approve": OutreachStatus.APPROVED, "cancel": OutreachStatus.CANCELED}
    if request.action is not None and request.action not in action_map:
        raise HTTPException(status_code=422,
                            detail=f"Unknown action '{request.action}' — expected 'approve' or 'cancel'")
    _main._refuse_agent_approval(request.action)
    status = action_map.get(request.action)
    if status is None and all(v is None for v in (request.target_name, request.context_url,
                                                  request.draft_text)):
        raise HTTPException(status_code=422, detail="Nothing to update — provide at least one field or an action")
    if not update_outreach_target(request.target_id, target_name=request.target_name,
                                  context_url=request.context_url, draft_text=request.draft_text,
                                  status=status):
        raise HTTPException(status_code=500, detail="Could not update outreach target")
    return ResponseModel(status_code=200, detail="Outreach target updated")


@router.delete("/target")
def delete_outreach_target_endpoint(request: OutreachTargetDeleteRequest) -> ResponseModel:
    """Cancel a funnel target (soft — sets status 'canceled' so no further stage fires)."""
    user_id = _main.get_session_user_id(request.session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if get_outreach_target_user_id(request.target_id) != user_id:
        raise HTTPException(status_code=404, detail="Outreach target not found")
    if not update_outreach_target_status(request.target_id, OutreachStatus.CANCELED):
        raise HTTPException(status_code=500, detail="Could not cancel outreach target")
    return ResponseModel(status_code=200, detail="Outreach target canceled")


# LAST, and deliberately so. The router and all four routes are bound above this line, so whichever
# module is imported first the other sees a COMPLETE router: `main`'s bottom-of-file include cannot
# read a half-populated one. Moving this import to the top makes an empty router get included —
# which serves nothing and fails no import.
from cqc_lem.api import main as _main  # noqa: E402
