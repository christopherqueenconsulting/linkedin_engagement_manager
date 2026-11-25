"""`POST /api/user/story-bank/draft`: the ONLY way an MCP agent adds to the story bank (#2804).

Story-bank entries are FACT (#620): the content engine may state them as the author's own
experience. So an agent never writes the user's facts directly. This route CREATES one entry and
nothing else, and the entry lands UNVERIFIED (`source='mcp'`, `verified_at` NULL): every reader the
content engine uses excludes it (`feedback.STORY_VERIFIED_SQL`) until the user confirms it, or edits
and saves it, in the SPA's story-bank editor.

1. **MCP seats only.** Any other scope is a 403 `mcp_only`. The SPA creates entries with
   `PUT /user/story-bank`, which no MCP scope is granted: an agent can neither edit nor delete an
   existing entry (the grant table in `api/main.py` is verb-and-path, `_MCP_WRITE_METHODS`).
2. **Behind `mcp-server-writes` AND `mcp-story-drafts`.** Either off is a 403.
3. **Bounded.** At most `MAX_UNVERIFIED_STORY_DRAFTS` unconfirmed drafts per account (409
   `too_many_unverified_drafts`), and a bank that cannot be counted is a 503, never a free pass.

The tool (`api/mcp/tools_story.py`) writes the `mcp_story_draft` audit row once the write lands.
"""

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import ConfigDict, Field, field_validator

from cqc_lem.api.models import ResponseModel, SessionTokenField
from cqc_lem.api.routers.user import StoryBankItem
from cqc_lem.utilities.db import (
    MAX_UNVERIFIED_STORY_DRAFTS,
    MCP_SESSION_SCOPES,
    count_unverified_story_drafts,
    insert_story_draft,
)
from cqc_lem.utilities.flags import MCP_SERVER_WRITES, MCP_STORY_DRAFTS, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_error

router = APIRouter(prefix="/api/user/story-bank")

_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "The entry was added, unverified"},
    403: {"description": "Not an MCP seat, or the feature is off for this account"},
    409: {"description": "Too many unconfirmed drafts already wait for the user"},
    422: {"description": "Invalid arguments"},
    503: {"description": "The story bank could not be read or written"},
}


class StoryDraftRequest(StoryBankItem):
    """Body of `POST /user/story-bank/draft`: a `StoryBankItem` the agent may CREATE, never address.

    `StoryBankItem` is the SPA's own validation (kind, title and body lengths), reused so an agent
    draft obeys exactly what a user entry does. `id` and `active` come with it and are refused.
    """

    model_config = ConfigDict(extra="forbid")

    session_token: SessionTokenField = None
    body: str = Field(min_length=1, max_length=5000)

    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank")
        return v

    @field_validator("happened_at")
    @classmethod
    def _happened_at_is_a_date(cls, v: Any) -> Any:
        if v in (None, ""):
            return None
        date.fromisoformat(str(v))  # ValueError -> 422
        return str(v)


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@router.post("/draft", responses=_RESPONSES)
def add_story_draft(request: StoryDraftRequest) -> ResponseModel[dict[str, Any]]:
    """Add ONE unverified story-bank entry on behalf of an MCP agent."""
    user_id = _main.require_session_user_id(request.session_token)
    if _main._request_session_scope.get() not in MCP_SESSION_SCOPES:
        raise _refuse(403, "mcp_only", "This route serves the LEM MCP server only.")
    if not (flag_enabled(MCP_SERVER_WRITES, user_id) and flag_enabled(MCP_STORY_DRAFTS, user_id)):
        log_debug("MCP story draft refused: feature off for this account", user_id=user_id)
        raise _refuse(403, "mcp_story_drafts_disabled",
                      "Story drafts are not enabled for this account.")
    if request.id is not None:
        raise _refuse(422, "create_only",
                      "An agent can only add a new entry, never address an existing one.")
    waiting = count_unverified_story_drafts(user_id)
    if waiting is None:
        raise _refuse(503, "mcp_unavailable", "LEM could not read the story bank. Try again shortly.")
    if waiting >= MAX_UNVERIFIED_STORY_DRAFTS:
        log_debug("MCP story draft refused: drafts waiting", user_id=user_id, waiting=waiting)
        raise _refuse(409, "too_many_unverified_drafts",
                      f"{waiting} drafts already wait for the user to confirm or discard them in "
                      "LEM. Ask the user to review those first.")
    entry_id = insert_story_draft(user_id, request.kind, request.title or "", request.body,
                                  request.happened_at)
    if entry_id is None:
        log_error("Could not create the MCP agent's story draft", user_id=user_id)
        raise _refuse(503, "upstream_error", "LEM could not save the draft. Try again shortly.")
    return ResponseModel(status_code=200, detail={"entry_id": entry_id, "verified": False})


__all__ = ["router", "StoryDraftRequest"]

from cqc_lem.api import main as _main  # noqa: E402  — last; see `routers/__init__.py`
