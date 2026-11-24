"""`/api/user/mcp-spend/*`: the ONLY way an MCP agent spends on AI or media (W2b, issue #2689).

Owner ruling O4 lets an agent connected through MCP generate a post's text, image and video. W1's
machine-seat rule stays exactly as it was everywhere else: `POST /schedule_post/` and
`POST /update_post/` still refuse every media field from an MCP seat with a 422
`machine_seat_text_only`. These three routes are the explicit, narrow exception, and each one is
guarded on its own:

1. **MCP seats only.** A full session, an agent or an operator seat is a 403 `mcp_only`, so this is
   not a second studio path for anyone. An MCP seat reaches these paths only through the gate's
   in-process loopback (the dispatch-secret binding), and only the three spend tools call them.
2. **Behind `mcp-server-writes`.** Off is a 403 `mcp_writes_disabled`.
3. **Media only for a PENDING post THIS token created** (`api/mcp/spend.py` keeps the mark). Another
   user's post is a 404; a post that is not pending a 409 `requires_pending`; a post of the same
   user that another token, or a human, created a 403 `not_created_by_this_token`.
4. **The free-trial daily AI cap first** (429 `daily_ai_cap`, with `Retry-After`), **then the MCP
   sub-cap** (429 `mcp_spend_cap`), so a capped account never burns a sub-cap unit.
5. **Never inline.** Each route queues the existing generation path in Celery and answers
   `status: queued` at once, inside the MCP tool's 20-second budget. Everything lands PENDING.

Each accepted call writes one `mcp_write` audit row naming the tool and the post, never a body.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from cqc_lem.api.mcp.spend import (
    PLACEHOLDER_CONTENT,
    SPEND_IMAGE,
    SPEND_TEXT,
    SPEND_VIDEO,
    LimitsUnavailable,
    claim_spend,
    created_by_session,
    mark_created_post,
)
from cqc_lem.api.models import ResponseModel, SessionTokenField
from cqc_lem.utilities import ai_spend_cap
from cqc_lem.utilities.ai_spend_cap import DailyAICapReached
from cqc_lem.utilities.db import (
    MCP_SESSION_SCOPES,
    AuthAuditEvent,
    PostStatus,
    PostType,
    get_post_content,
    get_post_status,
    get_post_type,
    get_post_user_id,
    get_user_email,
    insert_post_returning_id,
    update_db_post_status,
)
from cqc_lem.utilities.flags import MCP_SERVER_WRITES, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_error

router = APIRouter(prefix="/api/user/mcp-spend")

DEFAULT_LEAD = timedelta(hours=24)
# Far enough out that the draft is written and reviewed before its slot; never so far that it is
# not this quarter's content.
MIN_LEAD = timedelta(hours=1)
MAX_LEAD = timedelta(days=90)

_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "Generation queued; the post is pending review"},
    403: {"description": "Not an MCP seat, writes off, or not this token's post"},
    404: {"description": "Post not found"},
    409: {"description": "The post is not a pending text draft"},
    422: {"description": "Invalid arguments"},
    429: {"description": "Daily AI cap or MCP spend cap reached"},
    503: {"description": "Spend limits unreadable, or the job could not be queued"},
}


class McpGeneratePostRequest(BaseModel):
    """Body of `POST /user/mcp-spend/post`."""

    model_config = ConfigDict(extra="forbid")

    session_token: SessionTokenField = None
    topic: str = Field(min_length=10, max_length=1000, description=(
        "What the post is about and any direction for it, in the user's own terms."))
    scheduled_datetime: Optional[datetime] = Field(default=None, description=(
        "When the post should publish once approved (ISO 8601 with an offset). Default: 24h out."))


class McpPostMediaRequest(BaseModel):
    """Body of `POST /user/mcp-spend/post-image` and `/post-video`."""

    model_config = ConfigDict(extra="forbid")

    session_token: SessionTokenField = None
    post_id: int = Field(ge=1)


def _refuse(status: int, code: str, message: str, **headers: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message},
                         headers=headers or None)


def _mcp_caller(session_token: Optional[str]) -> tuple[int, int]:
    """The calling MCP seat as `(user_id, session_id)`, or the refusal for anyone else."""
    user_id = _main.require_session_user_id(session_token)
    if _main._request_session_scope.get() not in MCP_SESSION_SCOPES:
        raise _refuse(403, "mcp_only", "This route serves the LEM MCP server only.")
    if not flag_enabled(MCP_SERVER_WRITES, user_id):
        log_debug("MCP spend refused: writes are off for this account", user_id=user_id)
        raise _refuse(403, "mcp_writes_disabled", "Writes are not enabled for this account.")
    session_id = _main.get_session_id(_main.current_session_token(session_token))
    if not session_id:
        raise _refuse(403, "mcp_only", "This MCP token could not be identified.")
    return user_id, session_id


def _cap_refusal(exc: DailyAICapReached) -> HTTPException:
    retry_after = max(1, int((exc.resets_at - datetime.now(timezone.utc)).total_seconds()))
    return _refuse(429, "daily_ai_cap", str(exc), **{"Retry-After": str(retry_after)})


def _admit_spend(user_id: int, kind: str) -> None:
    """The daily AI cap, then one unit of the MCP sub-cap for `kind`."""
    try:
        ai_spend_cap.enforce(user_id, surface=ai_spend_cap.SURFACE_USER_REQUEST)
    except DailyAICapReached as exc:
        raise _cap_refusal(exc) from None
    try:
        claimed = claim_spend(user_id, kind)
    except LimitsUnavailable:
        raise _refuse(503, "mcp_unavailable",
                      "The LEM MCP server is unavailable right now. Try again shortly.") from None
    if not claimed:
        log_debug("MCP spend refused: daily sub-cap reached", user_id=user_id, action_type=kind)
        raise _refuse(429, "mcp_spend_cap",
                      f"This account's daily MCP {kind} generation limit is reached.")


def _owned_pending_text_post(user_id: int, session_id: int, post_id: int) -> None:
    """Refuse media for anything but a pending, fully written text post this token created."""
    if get_post_user_id(post_id) != user_id:
        raise _refuse(404, "not_found", "Post not found.")
    if get_post_status(post_id) != PostStatus.PENDING.value:
        raise _refuse(409, "requires_pending", "Media can only be generated for a pending draft.")
    try:
        mine = created_by_session(post_id, user_id, session_id)
    except LimitsUnavailable:
        raise _refuse(503, "mcp_unavailable",
                      "The LEM MCP server is unavailable right now. Try again shortly.") from None
    if not mine:
        raise _refuse(403, "not_created_by_this_token",
                      "Media can only be generated for a post this MCP token created.")
    if get_post_type(post_id) not in (PostType.TEXT, PostType.TEXT.value):
        raise _refuse(409, "requires_text_post", "Media can only be added to a text draft.")
    if (get_post_content(post_id) or "").strip() in ("", PLACEHOLDER_CONTENT):
        raise _refuse(409, "post_still_generating",
                      "This post's text is still being written. Try again in a few minutes.")


def _audit(user_id: int, session_id: int, tool: str, post_id: int) -> None:
    _main._safe_auth_event(AuthAuditEvent.MCP_WRITE, user_id=user_id, session_id=session_id,
                           details={"tool": tool, "target_id": post_id})


def _schedule(requested: Optional[datetime]) -> datetime:
    now = datetime.now(timezone.utc)
    if requested is None:
        return now + DEFAULT_LEAD
    when = requested if requested.tzinfo else requested.replace(tzinfo=timezone.utc)
    if not now + MIN_LEAD <= when <= now + MAX_LEAD:
        raise _refuse(422, "schedule_out_of_range",
                      "scheduled_datetime must be between 1 hour and 90 days from now.")
    return when


def _queued(post_id: int, kind: str) -> ResponseModel[dict[str, Any]]:
    return ResponseModel(status_code=200, detail={"post_id": post_id, "kind": kind,
                                                  "status": "queued",
                                                  "post_status": PostStatus.PENDING.value})


@router.post("/post", responses=_RESPONSES)
def mcp_generate_post(request: McpGeneratePostRequest) -> ResponseModel[dict[str, Any]]:
    """Create a PENDING text post and queue LEM's own writer to draft it from `topic`."""
    user_id, session_id = _mcp_caller(request.session_token)
    scheduled = _schedule(request.scheduled_datetime)
    _admit_spend(user_id, SPEND_TEXT)
    email = get_user_email(user_id)
    post_id = insert_post_returning_id(email, PLACEHOLDER_CONTENT, scheduled, PostType.TEXT,
                                       status=PostStatus.PENDING) if email else None
    if not post_id:
        log_error("Could not create the MCP agent's post row", user_id=user_id)
        raise _refuse(503, "upstream_error", "LEM could not create the draft. Try again shortly.")
    mark_created_post(post_id, user_id, session_id)
    from cqc_lem.app.run_content_plan import generate_agent_post_task
    try:
        generate_agent_post_task.apply_async(kwargs={"post_id": post_id,
                                                     "guidance": request.topic.strip()})
    except Exception as exc:  # noqa: BLE001 — a broker fault must not strand a placeholder.
        update_db_post_status(post_id, PostStatus.ERROR)
        log_error("Could not queue the MCP agent's post generation", exc=exc, user_id=user_id,
                  post_id=post_id)
        raise _refuse(503, "upstream_error", "LEM could not queue the draft. Try again shortly.")
    _audit(user_id, session_id, "lem_generate_post", post_id)
    return _queued(post_id, SPEND_TEXT)


def _queue_media(request: McpPostMediaRequest, kind: str, tool: str,
                 task: Any) -> ResponseModel[dict[str, Any]]:
    user_id, session_id = _mcp_caller(request.session_token)
    _owned_pending_text_post(user_id, session_id, request.post_id)
    _admit_spend(user_id, kind)
    try:
        task.apply_async(kwargs={"post_id": request.post_id})
    except Exception as exc:  # noqa: BLE001
        log_error("Could not queue the MCP agent's media generation", exc=exc, user_id=user_id,
                  post_id=request.post_id, action_type=kind)
        raise _refuse(503, "upstream_error", "LEM could not queue the render. Try again shortly.")
    _audit(user_id, session_id, tool, request.post_id)
    return _queued(request.post_id, kind)


@router.post("/post-image", responses=_RESPONSES)
def mcp_generate_post_image(request: McpPostMediaRequest) -> ResponseModel[dict[str, Any]]:
    """Queue LEM's gated post-image engine for a pending post this MCP token created."""
    from cqc_lem.app.run_content_plan import generate_agent_post_image_task
    return _queue_media(request, SPEND_IMAGE, "lem_generate_post_image",
                        generate_agent_post_image_task)


@router.post("/post-video", responses=_RESPONSES)
def mcp_generate_post_video(request: McpPostMediaRequest) -> ResponseModel[dict[str, Any]]:
    """Queue LEM's post-video pipeline for a pending post this MCP token created."""
    from cqc_lem.app.run_content_plan import generate_agent_post_video_task
    return _queue_media(request, SPEND_VIDEO, "lem_generate_post_video",
                        generate_agent_post_video_task)


__all__ = ["router", "PLACEHOLDER_CONTENT", "McpGeneratePostRequest", "McpPostMediaRequest"]

from cqc_lem.api import main as _main  # noqa: E402  — last; see `routers/__init__.py`
