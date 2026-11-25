"""`lem_add_story_draft`: an agent adds a story-bank entry that lands UNVERIFIED (#2804, epic #2797).

Story-bank entries are FACT (#620): the content engine states them as the author's own experience.
So the agent may only CREATE an entry, and it lands `source='mcp'` with `verified_at` NULL. Every
reader the content engine uses excludes it (`feedback.STORY_VERIFIED_SQL`) until the user confirms
it, or edits and saves it, in LEM. The tool cannot edit, confirm or delete anything: the route it
calls (`api/routers/mcp_story.py`) is create-only and the scope grant is `POST` on that one path.

It runs through `writes.run_write` like every queue write (dedupe on `client_request_id`, the
shared write budgets, the time limit, `outcome=unknown` after a send) with two differences, both
declared on the spec: it needs the `mcp-story-drafts` flag on top of `mcp-server-writes`, and its
audit row is `mcp_story_draft`, not `mcp_write`.
"""

import datetime as dt
from typing import Any, Literal, Optional

from pydantic import Field

from cqc_lem.api.mcp.loopback import Loopback
from cqc_lem.api.mcp.tools import BOTH_SCOPES, ToolSpec, _spa_url
from cqc_lem.api.mcp.tools_write import WriteArgs
from cqc_lem.utilities.db import STORY_BANK_KINDS, AuthAuditEvent
from cqc_lem.utilities.flags import MCP_STORY_DRAFTS

POST = "POST"
STORY_DRAFT_PATH = "/user/story-bank/draft"

# `StoryBankItem` (`routers/user.py`) is the SPA's validation and the route runs it again; these two
# mirror its caps so a too-long argument fails here, first. A test pins them equal.
STORY_TITLE_MAX = 255
STORY_BODY_MAX = 5000

UNVERIFIED_NOTE = ("Added as an UNVERIFIED draft. LEM will not use it in any post, comment or DM "
                   "until the user confirms it (or edits and saves it) in the story bank, where "
                   "they can also discard it. Tell the user it is waiting for them.")

DESCRIPTION = (
    "Add ONE story-bank entry: a real anecdote, number, opinion, client win, mistake or artifact "
    "from the user's own experience. Story-bank entries are FACT that LEM later states as the "
    "user's own, so only add what the user actually told you in this conversation, in their own "
    "terms: never infer, embellish, round or invent a detail, a number, a name or a date. The "
    "entry stays UNVERIFIED and LEM never uses it until the user confirms it in LEM; you cannot "
    "confirm, edit or delete entries. Creates only.")

StoryKind = Literal[tuple(STORY_BANK_KINDS)]  # type: ignore[valid-type]


class AddStoryDraftArgs(WriteArgs):
    """One story-bank entry to add as an unverified draft."""

    kind: StoryKind = Field(..., description=(
        "anecdote, number, opinion, client_win, mistake or artifact."))
    title: Optional[str] = Field(None, max_length=STORY_TITLE_MAX, description=(
        "A short label. Optional: LEM uses the start of the body."))
    body: str = Field(..., min_length=1, max_length=STORY_BODY_MAX, description=(
        "What actually happened, as the user told it: the real numbers, names, dates and outcome."))
    happened_at: Optional[dt.date] = Field(None, description=(
        "When it happened (YYYY-MM-DD), only if the user said."))


async def _add_story_draft(lb: Loopback, args: AddStoryDraftArgs) -> dict[str, Any]:
    body = await lb.write(POST, STORY_DRAFT_PATH, {
        "kind": args.kind, "title": args.title, "body": args.body,
        "happened_at": args.happened_at.isoformat() if args.happened_at else None})
    detail = body.get("detail")
    entry_id = detail.get("entry_id") if isinstance(detail, dict) else None
    return {"kind": "story_draft", "id": entry_id, "verified": False, "status": "unverified",
            "review_url": _spa_url("/account?section=content"), "note": UNVERIFIED_NOTE}


STORY_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("lem_add_story_draft", "Add a story-bank draft", DESCRIPTION, AddStoryDraftArgs,
             BOTH_SCOPES, ((POST, STORY_DRAFT_PATH),), _add_story_draft, write=True,
             flag=MCP_STORY_DRAFTS, audit_event=AuthAuditEvent.MCP_STORY_DRAFT),
)

__all__ = ["STORY_TOOLS", "AddStoryDraftArgs", "STORY_DRAFT_PATH", "UNVERIFIED_NOTE"]
