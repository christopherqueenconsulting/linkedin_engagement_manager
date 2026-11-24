"""The queue-only MCP write tools (W2, `docs/agent-mcp-plan.md` §4 "Write tools", §5).

Every tool here CREATES or EDITS an item that lands in, and stays in, PENDING: nothing it does can be
sent or published until a human approves it in LEM and a beat task runs it. That is enforced by the
routes, never by this module: an MCP seat is queue-only (`_QUEUE_ONLY_SCOPES`), so a create asking
for `approved` is a 403, a connection request never takes the passive `auto_approve` default, every
edit is SQL pending-only (409 `edit_requires_pending`, W1), and a post is a TEXT draft with no media
spend. The tools add what an agent needs on top: no `status` or `action` argument anywhere, LinkedIn
profile URLs checked in the argument model (and again by the route), `source='mcp'` on DMs and
connection requests (stamped by the route from the session scope), and a `review_url` plus the
"pending your approval" note on every answer.

ONE exception, owner ruling O1: `lem_approve_item` approves a DM, connection request, lead signal,
catch-up touch or outreach target (never a post). It is listed and run ONLY on an internal or
sandbox account (`approval=True`); the route's `_refuse_agent_approval` decides it again server-side
and audits `mcp_approval`, and a standard account is refused 403 `agent_may_not_approve` by both.

The shared write machinery (the `mcp-server-writes` flag, dedupe, the write budgets, the
`mcp_write` audit row, `outcome=unknown` on a timeout) is `writes.py`; these are only the runners.
"""

import datetime as dt
from typing import Any, Literal, Optional

from pydantic import Field, model_validator

from cqc_lem.api.mcp.loopback import Loopback
from cqc_lem.api.mcp.tools import (
    BOTH_SCOPES,
    OUTREACH_ONLY,
    ToolArgs,
    ToolSpec,
    _spa_url,
)

POST = "POST"
PUT = "PUT"

# Plan §4: a recipient or target is a LinkedIn member profile URL and nothing else. The routes run
# the same check for every queue-only seat (W1); here it fails at the argument model, first.
LINKEDIN_PROFILE_URL = r"^https://www\.linkedin\.com/in/[A-Za-z0-9\-_%]+/?$"
# A context link is a LinkedIn page (the post the target was seen on), never an arbitrary host.
LINKEDIN_URL = r"^https://www\.linkedin\.com/[A-Za-z0-9\-_%/.:?=&]+$"
CLIENT_REQUEST_ID = r"^[A-Za-z0-9._:\-]{1,64}$"

PENDING_NOTE = ("Pending your approval in LEM. Nothing has been sent or published; the user "
                "approves it in LEM.")
WRITE_RULE = (" It lands PENDING for the user's approval in LEM: never tell the user it was sent "
              "or published.")


class WriteArgs(ToolArgs):
    """Every write tool's arguments: an optional idempotency key for safe retries."""

    client_request_id: Optional[str] = Field(None, pattern=CLIENT_REQUEST_ID, description=(
        "Your own id for this write (1-64 of A-Z a-z 0-9 . _ : -). Retrying with the same id and "
        "the same arguments returns the first answer instead of writing twice. Omit it and LEM "
        "derives one from the arguments."))


class QueueDmArgs(WriteArgs):
    """A 1:1 DM to queue for review."""

    recipient_profile_url: str = Field(..., max_length=512, pattern=LINKEDIN_PROFILE_URL)
    recipient_name: Optional[str] = Field(None, max_length=255)
    message: str = Field(..., min_length=1, max_length=2000)
    scheduled_datetime: dt.datetime = Field(..., description=(
        "When to send it once approved (ISO 8601 with a timezone)."))


class QueueConnectionArgs(WriteArgs):
    """A connection request to queue for review."""

    recipient_profile_url: str = Field(..., max_length=512, pattern=LINKEDIN_PROFILE_URL)
    recipient_name: Optional[str] = Field(None, max_length=255)
    message: Optional[str] = Field(None, max_length=300, description=(
        "Optional connection note (LinkedIn caps it at 300 characters)."))


class OutreachTargetArgs(WriteArgs):
    """A prospect to add to the comment-first outreach funnel."""

    target_profile_url: str = Field(..., max_length=512, pattern=LINKEDIN_PROFILE_URL)
    target_name: Optional[str] = Field(None, max_length=255)
    context_url: Optional[str] = Field(None, max_length=512, pattern=LINKEDIN_URL, description=(
        "The LinkedIn post or page this person was found on."))
    draft_text: Optional[str] = Field(None, max_length=3000)


class LeadSignalDraftArgs(WriteArgs):
    """A new draft reply for a pending lead signal."""

    signal_id: int = Field(..., ge=1)
    draft_response: str = Field(..., min_length=1, max_length=3000)


class CatchupDraftArgs(WriteArgs):
    """A new draft for a pending catch-up touch."""

    touch_id: int = Field(..., ge=1)
    message: Optional[str] = Field(None, min_length=1, max_length=1000)
    person_name: Optional[str] = Field(None, max_length=255)

    @model_validator(mode="after")
    def _something(self) -> "CatchupDraftArgs":
        if self.message is None and self.person_name is None:
            raise ValueError("give message or person_name")
        return self


LeadStageArg = Literal["", "cold", "warm", "hot", "in_conversation", "opportunity",
                       "meeting_booked", "meeting_held", "won", "lost"]


class UpdateLeadArgs(WriteArgs):
    """Manual corrections to a scored lead: a note, a stage, a deal value, or (un)dismissing it."""

    lead_id: int = Field(..., ge=1)
    notes: Optional[str] = Field(None, max_length=512)
    stage: Optional[LeadStageArg] = Field(None, description=(
        "A manual stage override; an empty string clears it back to the computed stage."))
    deal_value: Optional[float] = Field(None, ge=0, le=9_999_999_999)
    dismissed: Optional[bool] = Field(None, description=(
        "True keeps the lead off the board, False restores it."))

    @model_validator(mode="after")
    def _something(self) -> "UpdateLeadArgs":
        if all(v is None for v in (self.notes, self.stage, self.deal_value, self.dismissed)):
            raise ValueError("give at least one of notes, stage, deal_value, dismissed")
        return self


class DraftPostArgs(WriteArgs):
    """A text post draft. No media, no type, no status: it is always a PENDING text post."""

    content: str = Field(..., min_length=1, max_length=3000)
    scheduled_datetime: dt.datetime = Field(..., description=(
        "When it would publish once approved (ISO 8601 with a timezone)."))


class RevisePostArgs(WriteArgs):
    """New text and/or slot for one of the user's PENDING post drafts."""

    post_id: int = Field(..., ge=1)
    content: Optional[str] = Field(None, min_length=1, max_length=3000)
    scheduled_datetime: Optional[dt.datetime] = None

    @model_validator(mode="after")
    def _something(self) -> "RevisePostArgs":
        if self.content is None and self.scheduled_datetime is None:
            raise ValueError("give content or scheduled_datetime")
        return self


ApprovableKind = Literal["dm", "connection_request", "lead_signal", "catchup_touch",
                         "outreach_target"]


class ApproveArgs(WriteArgs):
    """One outreach item to approve. Never a post."""

    kind: ApprovableKind
    id: int = Field(..., ge=1)


# --- runners --------------------------------------------------------------------------------------


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _queued(kind: str, item_id: Any, review_path: str, **extra: Any) -> dict[str, Any]:
    """The common answer of a write: what, which id, its status and where the user reviews it."""
    return {"kind": kind, "id": item_id, "status": "pending", "review_url": _spa_url(review_path),
            "note": PENDING_NOTE, **extra}


def _detail_id(body: dict[str, Any], key: str) -> Any:
    detail = body.get("detail")
    return detail.get(key) if isinstance(detail, dict) else None


async def _queue_dm(lb: Loopback, args: QueueDmArgs) -> dict[str, Any]:
    body = await lb.write(POST, "/schedule_dm", {
        "recipient_profile_url": args.recipient_profile_url, "recipient_name": args.recipient_name,
        "message": args.message, "scheduled_datetime": _iso(args.scheduled_datetime),
        "status": "pending"})
    return _queued("dm", _detail_id(body, "dm_id"), "/content?tab=dms", source="mcp")


async def _queue_connection(lb: Loopback, args: QueueConnectionArgs) -> dict[str, Any]:
    body = await lb.write(POST, "/connection_request", {
        "recipient_profile_url": args.recipient_profile_url, "recipient_name": args.recipient_name,
        "message": args.message, "status": "pending"})
    return _queued("connection_request", _detail_id(body, "request_id"),
                   "/content?tab=connections", source="mcp")


async def _add_outreach_target(lb: Loopback, args: OutreachTargetArgs) -> dict[str, Any]:
    body = await lb.write(POST, "/outreach/target", {
        "target_profile_url": args.target_profile_url, "target_name": args.target_name,
        "context_url": args.context_url, "draft_text": args.draft_text, "status": "pending"})
    return _queued("outreach_target", _detail_id(body, "target_id"), "/content?tab=outreach")


async def _edit_lead_signal(lb: Loopback, args: LeadSignalDraftArgs) -> dict[str, Any]:
    await lb.write(PUT, "/lead_signal", {"signal_id": args.signal_id,
                                         "draft_response": args.draft_response})
    return _queued("lead_signal", args.signal_id, "/content?tab=leads")


async def _edit_catchup(lb: Loopback, args: CatchupDraftArgs) -> dict[str, Any]:
    await lb.write(PUT, "/catchup/touch", {"touch_id": args.touch_id, "message": args.message,
                                           "person_name": args.person_name})
    return _queued("catchup_touch", args.touch_id, "/content?tab=catchup")


async def _update_lead(lb: Loopback, args: UpdateLeadArgs) -> dict[str, Any]:
    action = None if args.dismissed is None else ("dismiss" if args.dismissed else "restore")
    await lb.write(PUT, "/lead", {"lead_id": args.lead_id, "notes": args.notes,
                                  "stage": args.stage, "deal_value": args.deal_value,
                                  "action": action})
    # A lead is a record, not a send: there is nothing to approve, so no pending status.
    return {"kind": "lead", "id": args.lead_id, "updated": True,
            "review_url": _spa_url("/content?tab=pipeline")}


async def _draft_post(lb: Loopback, args: DraftPostArgs) -> dict[str, Any]:
    body = await lb.write(POST, "/schedule_post", {
        "content": args.content, "scheduled_datetime": _iso(args.scheduled_datetime),
        "post_type": "text", "status": "pending"})
    return _queued("post", body.get("post_id"), "/content?tab=review", post_type="text")


async def _revise_post(lb: Loopback, args: RevisePostArgs) -> dict[str, Any]:
    # Only the fields being revised are sent (`PostUpdateRequest`), so the pending-only write never
    # writes back a field this call did not change: a slot moved meanwhile survives a content-only
    # revise. `status` is never sent; the route writes only while the post is PENDING (SQL).
    await lb.write(POST, "/update_post", {"content": args.content,
                                          "scheduled_datetime": _iso(args.scheduled_datetime)},
                   params={"post_id": args.post_id})
    return _queued("post", args.post_id, "/content?tab=review")


# kind -> (path, id field, review path)
_APPROVE_ROUTES: dict[str, tuple[str, str, str]] = {
    "dm": ("/dm", "dm_id", "/content?tab=dms"),
    "connection_request": ("/connection_request", "request_id", "/content?tab=connections"),
    "lead_signal": ("/lead_signal", "signal_id", "/content?tab=leads"),
    "catchup_touch": ("/catchup/touch", "touch_id", "/content?tab=catchup"),
    "outreach_target": ("/outreach/target", "target_id", "/content?tab=outreach"),
}


# What happens next, per kind, as the route and the beat really behave. Only a still-PENDING item
# can be approved here (the route's pending-only write: anything else is 409).
APPROVAL_NOTES: dict[str, str] = {
    "dm": ("Approved. LEM sends it at its scheduled time, within the daily DM cap; a DM whose slot "
           "has already passed is rescheduled to now."),
    "connection_request": ("Approved. LEM sends the invite on its next connection-request run, "
                           "within the daily invite cap."),
    "lead_signal": "Approved. LEM is sending this reply now.",
    "catchup_touch": ("Approved. LEM sends it on its next catch-up run, within the daily "
                      "catch-up cap."),
    "outreach_target": ("Approved for its CURRENT funnel stage only. LEM runs that stage on its "
                        "next funnel run; the next stage comes back to the queue for approval."),
}


async def _approve_item(lb: Loopback, args: ApproveArgs) -> dict[str, Any]:
    path, id_field, review = _APPROVE_ROUTES[args.kind]
    await lb.write(PUT, path, {id_field: args.id, "action": "approve"})
    return {"kind": args.kind, "id": args.id, "status": "approved", "review_url": _spa_url(review),
            "note": APPROVAL_NOTES[args.kind]}


# --- the write registry ---------------------------------------------------------------------------

WRITE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("lem_draft_post", "Draft a text post",
             "Save a TEXT post draft for a time slot. No images, video or AI generation." + WRITE_RULE,
             DraftPostArgs, BOTH_SCOPES, ((POST, "/schedule_post"),), _draft_post, write=True),
    ToolSpec("lem_revise_post_draft", "Revise a post draft",
             "Change the text and/or slot of one of your PENDING post drafts. A post that is "
             "already approved or posted cannot be changed here." + WRITE_RULE,
             RevisePostArgs, BOTH_SCOPES, ((POST, "/update_post"),), _revise_post, write=True),
    ToolSpec("lem_queue_dm", "Queue a DM",
             "Queue a 1:1 LinkedIn message to a profile URL for a time." + WRITE_RULE,
             QueueDmArgs, OUTREACH_ONLY, ((POST, "/schedule_dm"),), _queue_dm, write=True),
    ToolSpec("lem_queue_connection_request", "Queue a connection request",
             "Queue a LinkedIn connection request, with an optional note, to a profile URL."
             + WRITE_RULE,
             QueueConnectionArgs, OUTREACH_ONLY, ((POST, "/connection_request"),),
             _queue_connection, write=True),
    ToolSpec("lem_add_outreach_target", "Add an outreach target",
             "Add a prospect to the comment-first outreach funnel. Creates only." + WRITE_RULE,
             OutreachTargetArgs, OUTREACH_ONLY, ((POST, "/outreach/target"),),
             _add_outreach_target, write=True),
    ToolSpec("lem_edit_lead_signal_draft", "Edit a lead reply draft",
             "Replace the draft reply on a PENDING lead signal." + WRITE_RULE,
             LeadSignalDraftArgs, OUTREACH_ONLY, ((PUT, "/lead_signal"),), _edit_lead_signal,
             write=True),
    ToolSpec("lem_edit_catchup_draft", "Edit a catch-up draft",
             "Replace the message (or name) on a PENDING catch-up congratulations." + WRITE_RULE,
             CatchupDraftArgs, OUTREACH_ONLY, ((PUT, "/catchup/touch"),), _edit_catchup,
             write=True),
    ToolSpec("lem_update_lead", "Update a lead",
             "Keep a note on a scored lead, set or clear its stage, record a deal value, or "
             "dismiss/restore it. Sends nothing.",
             UpdateLeadArgs, OUTREACH_ONLY, ((PUT, "/lead"),), _update_lead, write=True),
    ToolSpec("lem_approve_item", "Approve an outreach item",
             "Approve one DM, connection request, lead-signal reply, catch-up touch or outreach "
             "target so LEM sends it on its normal schedule. Only on LEM's internal and sandbox "
             "accounts; never a post.",
             ApproveArgs, OUTREACH_ONLY,
             tuple((PUT, path) for path, _f, _r in _APPROVE_ROUTES.values()), _approve_item,
             write=True, approval=True),
)

__all__ = ["WRITE_TOOLS", "PENDING_NOTE", "LINKEDIN_PROFILE_URL", "ApproveArgs", "WriteArgs"]
