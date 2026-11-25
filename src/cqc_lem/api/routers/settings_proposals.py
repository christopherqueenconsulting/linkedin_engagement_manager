"""`/api/user/settings-proposals`: the user's side of an agent's gate-loosening requests (#2803).

An agent may ASK to loosen a safety or approval gate (`lem_request_settings_change`); only the signed-in
USER may apply it, here. These three routes are the whole human half of that ruling (epic #2797,
owner ruling 3):

- `GET`   lists the caller's open proposals, each field with its before and requested value and LEM's
          own warning for it. The agent's `reason` is returned as untrusted TEXT.
- `POST /{id}/approve` applies all of a proposal's fields, or a chosen subset, behind a fresh step-up
          when the account has strong auth. Each field's before-value is re-checked first: a field
          that has changed since the agent looked is SKIPPED and reported, never overwritten.
- `POST /{id}/reject` closes it. Rejecting loosens nothing, so it needs no step-up.

**No agent scope reaches any of this.** The scope surfaces are allowlists, so a path nobody listed is
unreachable by `agent`, `operator`, `observer`, `mcp` and `mcp_outreach`; `FORBIDDEN_ROLE_ROUTES`
(`tests/unit/api/test_role_surface_invariants.py`) pins it, and each handler refuses any session that
is not a person's as well. No MCP tool approves.

An approval applies through the merged write the S2 tools use, never a full-record PUT: only the
approved keys are written over the stored row (`update_engagement_preferences` merges;
`auto_publish_newsletters` is merged over the saved newsletter settings by hand).
"""

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cqc_lem.api.mcp import settings_policy as policy
from cqc_lem.api.models import ResponseModel, SessionTokenField
from cqc_lem.utilities.db import (
    PROPOSAL_APPROVED,
    PROPOSAL_EXPIRED,
    PROPOSAL_KIND_SETTINGS,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    SESSION_SCOPE_FULL,
    AuthAuditEvent,
    decide_agent_proposal,
    expire_agent_proposals,
    get_agent_proposal,
    get_engagement_preferences,
    get_newsletter_settings,
    list_pending_agent_proposals,
    record_auth_event,
    reopen_agent_proposal,
    set_agent_proposal_outcome,
    update_engagement_preferences,
    update_newsletter_settings,
)
from cqc_lem.utilities.logger import log_info, log_warning

router = APIRouter(prefix="/api/user/settings-proposals")

STEP_UP_ACTION = "approve_settings_proposal"


class ApproveProposalRequest(BaseModel):
    """Body of `POST /user/settings-proposals/{id}/approve`."""

    session_token: SessionTokenField = None
    fields: Optional[list[str]] = Field(default=None, max_length=20, description=(
        "The fields to approve. Omit to approve every field of the proposal."))


class RejectProposalRequest(BaseModel):
    """Body of `POST /user/settings-proposals/{id}/reject`."""

    session_token: SessionTokenField = None


def _human(session_token: Optional[str]) -> int:
    """The signed-in PERSON's id: 401 for no session, 403 for any machine scope (defence in depth)."""
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if _main._request_session_scope.get() not in (None, SESSION_SCOPE_FULL):
        raise HTTPException(status_code=403, detail={
            "code": "proposal_decision_is_human_only",
            "message": "Only the signed-in account owner can decide a change request."})
    return user_id


def _session_id(session_token: Optional[str]) -> Optional[int]:
    token = _main.current_session_token(session_token)
    return _main.get_session_id(token) if token else None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _read_values(user_id: int) -> dict[str, Any]:
    """The stored value of every requestable gate field, `{field: value}`."""
    prefs = get_engagement_preferences(user_id, raise_on_error=True) or {}
    newsletter = get_newsletter_settings(user_id) or {}
    return {field: (prefs if rule.resource == policy.PREFS else newsletter).get(rule.key)
            for field, rule in policy.GATE_RULES.items()}


def _effective(field: str, stored: Any, prefs_defaults: dict[str, Any]) -> Any:
    if stored is None and policy.GATE_RULES[field].kind in (policy.FLOOR, policy.CEILING):
        return prefs_defaults.get(field)
    return stored


def _gate_defaults() -> dict[str, Any]:
    from cqc_lem.utilities.ai.content_alignment import authenticity_score_min
    from cqc_lem.utilities.ai.content_framework import post_similarity_max
    return {"authenticity_score_min": authenticity_score_min(),
            "post_similarity_max_pct": round(post_similarity_max() * 100)}


def _view(row: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    fields = []
    for field, change in (row.get("payload") or {}).items():
        if field not in policy.GATE_RULES or not isinstance(change, dict):
            continue  # a stored key that is not a requestable gate is never shown, so never approved
        rule = policy.GATE_RULES[field]
        before, requested = change.get("before"), change.get("requested")
        fields.append({
            "field": field, "label": rule.label, "before": before, "requested": requested,
            "loosens": policy.loosens(field, _effective(field, before, defaults), requested),
            "warning": policy.warning_for(field, _effective(field, before, defaults), requested)})
    return {"id": row["id"], "kind": row.get("kind"), "status": row.get("status"),
            # Untrusted agent text, returned as a plain string. The SPA renders it as text only.
            "reason": row.get("reason"), "fields": fields,
            "created_at": _main._utc_iso(row.get("created_at")),
            "expires_at": _main._utc_iso(row.get("expires_at"))}


@router.get("", responses={
    200: {"description": "The caller's open change requests, newest first"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Not a person's session"},
    503: {"description": "The requests could not be read"},
})
def list_settings_proposals(session_token: SessionTokenField = None) -> ResponseModel[dict[str, Any]]:
    """The "Changes your agent asked for" card's read: open proposals, expired ones swept first.

    A failed read is 503, never an empty list that reads as "your agent asked for nothing".
    """
    user_id = _human(session_token)
    rows = list_pending_agent_proposals(user_id, PROPOSAL_KIND_SETTINGS, _now())
    if rows is None:
        raise HTTPException(status_code=503, detail="Could not read your change requests. Try again.")
    defaults = _gate_defaults() if rows else {}
    return ResponseModel(status_code=200, detail={
        "proposals": [_view(r, defaults) for r in rows], "count": len(rows)})


def _load_pending(user_id: int, proposal_id: int) -> dict[str, Any]:
    """The caller's pending, unexpired proposal; 404 / 409 otherwise. Sweeps expired rows first."""
    expire_agent_proposals(user_id, _now())
    row = get_agent_proposal(user_id, proposal_id)
    if row is None or row.get("kind") != PROPOSAL_KIND_SETTINGS:
        raise HTTPException(status_code=404, detail="Change request not found")
    if row.get("status") == PROPOSAL_EXPIRED:
        raise HTTPException(status_code=409, detail={
            "code": "proposal_expired", "message": "This request expired. Ask your agent to file it again."})
    if row.get("status") != PROPOSAL_PENDING:
        raise HTTPException(status_code=409, detail={
            "code": "proposal_not_pending",
            "message": f"This request was already {row.get('status')}."})
    return row


def _apply(user_id: int, approved: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Write the approved `{field: value}` over the stored rows; `(applied, failed)` field names."""
    applied: list[str] = []
    failed: list[str] = []
    prefs = {policy.GATE_RULES[f].key: v for f, v in approved.items()
             if policy.GATE_RULES[f].resource == policy.PREFS}
    if prefs:
        names = [f for f in approved if policy.GATE_RULES[f].resource == policy.PREFS]
        (applied if update_engagement_preferences(user_id, prefs) else failed).extend(names)
    news = {policy.GATE_RULES[f].key: v for f, v in approved.items()
            if policy.GATE_RULES[f].resource == policy.NEWSLETTER}
    if news:
        names = [f for f in approved if policy.GATE_RULES[f].resource == policy.NEWSLETTER]
        merged = {**(get_newsletter_settings(user_id) or {}), **news}
        (applied if update_newsletter_settings(user_id, merged) else failed).extend(names)
    return applied, failed


@router.post("/{proposal_id}/approve", responses={
    200: {"description": "Decided; says which fields were applied, stale or declined"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Step-up required, or not a person's session"},
    404: {"description": "No such change request on this account"},
    409: {"description": "Already decided, or expired"},
    422: {"description": "A field that is not in this request"},
    503: {"description": "The current settings could not be read; nothing was changed"},
})
def approve_settings_proposal(
        proposal_id: int, request: ApproveProposalRequest,
        http_request: Request = None) -> ResponseModel[dict[str, Any]]:  # type: ignore[assignment]
    """Apply an agent's request, or the chosen fields of it, under a fresh step-up.

    Order matters: step-up first (a refusal changes nothing), then the pending and expiry checks,
    then every selected field's before-value is re-read and compared. The decision is CLAIMED in the
    SQL (`status = 'pending' AND expires_at > now`) before any write, so a double click applies once.
    A stale field is skipped and reported; the rest apply.
    """
    user_id = _human(request.session_token)
    from cqc_lem.api.routers.user import _require_step_up  # deferred: user imports main imports us
    _require_step_up(user_id, request.session_token, STEP_UP_ACTION, http_request=http_request)
    row = _load_pending(user_id, proposal_id)
    payload = {f: c for f, c in (row.get("payload") or {}).items()
               if f in policy.GATE_RULES and isinstance(c, dict)}
    chosen = list(payload) if request.fields is None else list(dict.fromkeys(request.fields))
    unknown = [f for f in chosen if f not in payload]
    if unknown or not chosen:
        raise HTTPException(status_code=422, detail={
            "code": "proposal_fields_invalid",
            "message": "Choose at least one field from this request."})
    try:
        current = _read_values(user_id)
    except Exception as exc:  # noqa: BLE001 — unreadable settings: change nothing, claim nothing.
        log_warning("Could not read settings to approve a change request", exc=exc, user_id=user_id)
        raise HTTPException(status_code=503, detail="Could not read your settings. Try again.") from None

    from cqc_lem.api.mcp import tools as _registry  # noqa: F401 — loads the registry first: tools_settings needs it
    from cqc_lem.api.mcp.loopback import ToolFailure
    from cqc_lem.api.mcp.tools_settings import normalise_gate
    stale: list[str] = []
    to_apply: dict[str, Any] = {}
    for field in chosen:
        if not policy.same(current.get(field), payload[field].get("before")):
            stale.append(field)
            continue
        try:
            to_apply[field] = normalise_gate(field, payload[field].get("requested"))
        except ToolFailure:
            stale.append(field)  # a stored value LEM would not accept is never written
    declined = [f for f in payload if f not in chosen]

    if not decide_agent_proposal(user_id, proposal_id, PROPOSAL_APPROVED,
                                 _session_id(request.session_token), _now()):
        raise HTTPException(status_code=409, detail={
            "code": "proposal_not_pending",
            "message": "This request was already decided, or has just expired."})
    applied, failed = _apply(user_id, to_apply) if to_apply else ([], [])
    label = ("applied" if applied else "nothing_applied_failed" if failed else "nothing_applied_stale")
    outcome = {"applied": sorted(applied), "stale": sorted(stale), "declined": sorted(declined),
               "failed": sorted(failed), "outcome": label}

    def _audit() -> None:
        # success=False whenever nothing landed or a write failed: the row must not read as a change.
        record_auth_event(AuthAuditEvent.MCP_SETTINGS_APPROVED, user_id=user_id,
                          ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                          session_id=_session_id(request.session_token),
                          success=bool(applied) and not failed,
                          details={"proposal_id": proposal_id, **outcome})

    if failed and not applied:
        reopen_agent_proposal(user_id, proposal_id)
        _audit()
        raise HTTPException(status_code=503, detail="Could not save your settings. Nothing was changed.")
    set_agent_proposal_outcome(user_id, proposal_id, outcome)
    _audit()
    log_info("Agent settings request decided", user_id=user_id, action_type="approve_settings_proposal",
             applied=len(applied), stale=len(stale))
    note = None if applied else ("Nothing was applied: every field you approved had changed since your "
                                 "agent asked, so none was overwritten.")
    return ResponseModel(status_code=200, detail={"proposal_id": proposal_id, "status": PROPOSAL_APPROVED,
                                                   **outcome, "note": note})


@router.post("/{proposal_id}/reject", responses={
    200: {"description": "Rejected; nothing changed"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Not a person's session"},
    404: {"description": "No such change request on this account"},
    409: {"description": "Already decided, or expired"},
})
def reject_settings_proposal(
        proposal_id: int, request: RejectProposalRequest,
        http_request: Request = None) -> ResponseModel[dict[str, Any]]:  # type: ignore[assignment]
    """Close an agent's request without applying any of it. Loosens nothing, so no step-up."""
    user_id = _human(request.session_token)
    row = _load_pending(user_id, proposal_id)
    if not decide_agent_proposal(user_id, proposal_id, PROPOSAL_REJECTED,
                                 _session_id(request.session_token), _now()):
        raise HTTPException(status_code=409, detail={
            "code": "proposal_not_pending",
            "message": "This request was already decided, or has just expired."})
    record_auth_event(AuthAuditEvent.MCP_SETTINGS_REJECTED, user_id=user_id,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      session_id=_session_id(request.session_token),
                      details={"proposal_id": proposal_id, "fields": sorted(row.get("payload") or {})})
    return ResponseModel(status_code=200, detail={"proposal_id": proposal_id, "status": PROPOSAL_REJECTED})


from cqc_lem.api import main as _main  # noqa: E402  — last; see `routers/__init__.py`
