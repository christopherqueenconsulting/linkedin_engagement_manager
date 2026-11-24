"""`/api/user/mcp-token`, its `/rotate` and `/info` — minting the end-user MCP credential (#2682).

An MCP token is an ordinary `sessions` row, scope `mcp` or (opted into at mint) `mcp_outreach`,
prefixed `lem_mcp_` so a leaked one is recognisable. It is presented only as a bearer at
`POST /api/mcp`; on raw `/api` it is refused without the gate's in-process dispatch secret
(`api/mcp/dispatch.py`). `docs/agent-mcp-plan.md` §3.5, owner rulings O2, O3 and O11.

The ceremony mirrors `mint_agent_token`, tightened:

- The rollout flag `mcp-server-enabled` gates every route here: off is a 404, so the routes do not
  exist for an account outside the rollout.
- A human session only (no machine seat reaches these paths), then a STRONG FACTOR (403
  `mcp_needs_strong_factor`, owner ruling O3), then step-up with `extension_scope_ok` NOT passed.
- A fixed TTL, 1-90 days, default 30 (O2). At most 10 live MCP tokens per account (409
  `mcp_token_limit`), counted and inserted in one transaction under the user's row lock, so
  concurrent mints cannot overshoot it. And 10 mints an hour (429 `mcp_mint_rate_limited`), a Redis
  limiter that fails open.
- Audited (`mcp_token_minted` / `mcp_token_rotated`) and emailed to the account owner on every
  mint and rotate. A failed email never fails the mint.

Revoking one is the existing `POST /user/sessions/revoke`.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cqc_lem.api.mcp.lifespan import MCP_PATH
from cqc_lem.api.mcp.limits import claim_mint, read_activity
from cqc_lem.api.mcp.notify import notify_token_event
from cqc_lem.api.models import ResponseModel, SessionTokenField
from cqc_lem.api.spa_assets import public_base_url
from cqc_lem.utilities.auth_factors import has_strong_factor
from cqc_lem.utilities.db import (
    MCP_SESSION_SCOPES,
    MCP_TOKEN_PREFIX,
    SESSION_SCOPE_MCP,
    SESSION_SCOPE_MCP_OUTREACH,
    AuthAuditEvent,
    SessionCapReached,
    SessionStoreUnavailable,
    create_session_within_cap,
    get_owned_session,
    list_sessions_by_scope,
    record_auth_event,
    revoke_session,
)
from cqc_lem.utilities.flags import MCP_SERVER, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_info

router = APIRouter(prefix="/api/user/mcp-token")

MCP_TTL_DEFAULT_DAYS = 30
MCP_TTL_MAX_DAYS = 90
MCP_MAX_ACTIVE_TOKENS = 10
MCP_DEFAULT_LABEL = "Coding agent"

# The same rule as every machine-seat label: shown on the Security card and kept in the audit log,
# so it cannot carry an email address, punctuation that reads as a name, or a newline.
_MCP_LABEL_PATTERN = r"^[A-Za-z0-9 ._:\-]*$"


class McpTokenRequest(BaseModel):
    """Body of `POST /user/mcp-token`."""

    session_token: SessionTokenField = None
    label: Optional[str] = Field(default=None, max_length=120, pattern=_MCP_LABEL_PATTERN)
    ttl_days: int = Field(default=MCP_TTL_DEFAULT_DAYS, ge=1, le=MCP_TTL_MAX_DAYS)
    outreach: bool = Field(default=False, description=(
        "Also grant the outreach and leads lists (other people's data): scope `mcp_outreach`."))


class McpTokenRotateRequest(BaseModel):
    """Body of `POST /user/mcp-token/rotate`."""

    session_token: SessionTokenField = None
    session_id: int = Field(ge=1)


def _mcp_url() -> Optional[str]:
    """The URL a client connects to, from the configured public base; None when there is none."""
    base = public_base_url()
    return f"{base}{MCP_PATH}" if base else None


def _caller(session_token: Optional[str]) -> int:
    user_id = _main.get_session_user_id(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user_id


def _require_enabled(user_id: int) -> None:
    """404 when the rollout flag is off for this account: the routes do not exist for it."""
    if not flag_enabled(MCP_SERVER, user_id):
        log_debug("MCP token route refused: rollout flag off", user_id=user_id)
        raise HTTPException(status_code=404, detail="Not Found")


def _require_mint_ceremony(user_id: int, session_token: Optional[str], action: str,
                           http_request: Optional[Request]) -> None:
    """Strong factor first, then step-up, then the hourly mint rate.

    The factor check runs BEFORE the ceremony, so an account that cannot mint is told so without
    being sent through a step-up it would pass for nothing.
    """
    if not has_strong_factor(user_id):
        raise HTTPException(status_code=403, detail={
            "code": "mcp_needs_strong_factor",
            "message": "Add a passkey or an authenticator app before connecting an agent.",
        })
    # Deferred: `routers.user` imports `main`, which imports this module. `extension_scope_ok` is NOT
    # passed: an MCP token can never satisfy the gate that mints its successor.
    from cqc_lem.api.routers.user import _require_step_up
    _require_step_up(user_id, session_token, action, http_request=http_request)
    if not claim_mint(user_id):
        raise HTTPException(status_code=429, headers={"Retry-After": "3600"}, detail={
            "code": "mcp_mint_rate_limited",
            "message": "Too many agent tokens created in the last hour. Try again later.",
        })


def _mint(user_id: int, label: str, scope: str, ttl_days: int,
          http_request: Optional[Request]) -> dict[str, Any]:
    """Create the session row and return the response body. The token appears ONLY in the body.

    The 10-live-token cap is checked by `create_session_within_cap`, in the same transaction as
    the insert and under the user's row lock, so two concurrent mints cannot both pass it.
    """
    try:
        token, session_id = create_session_within_cap(
            user_id, MCP_SESSION_SCOPES, MCP_MAX_ACTIVE_TOKENS, scope,
            user_agent=_main._user_agent(http_request), ip=_main._client_ip(http_request),
            label=label, ttl_hours=ttl_days * 24, token_prefix=MCP_TOKEN_PREFIX)
    except SessionCapReached:
        raise HTTPException(status_code=409, detail={
            "code": "mcp_token_limit",
            "message": f"You already have {MCP_MAX_ACTIVE_TOKENS} agent tokens. Revoke one first.",
        })
    if not token:
        raise HTTPException(status_code=503, detail="Could not create the agent token. Try again.")
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    return {"session_token": token, "session_id": session_id, "mcp_url": _mcp_url(),
            "expires_at": _main._utc_iso(expires_at), "label": label, "scope": scope,
            "ttl_days": ttl_days}


@router.post("", responses={
    200: {"description": "Token minted; shown once"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Step-up required, or no strong factor enrolled"},
    404: {"description": "The MCP server is not enabled for this account"},
    409: {"description": "Already at the active-token limit"},
    429: {"description": "Too many mints this hour"},
    503: {"description": "The token could not be created; nothing was stored"},
})
def mint_mcp_token(request: McpTokenRequest,
                   http_request: Request = None) -> ResponseModel[dict[str, Any]]:  # type: ignore[assignment]
    """Mint an MCP token for the user's own coding agent, shown exactly once in the response.

    Scope `mcp` reads the account's own content, analytics, plan and settings; `outreach=true` mints
    `mcp_outreach`, which adds the lists holding other people's data. Neither can approve, send or
    configure anything, nor reach raw `/api` without the gate (`docs/identity-and-sessions.md`).
    """
    user_id = _caller(request.session_token)
    _require_enabled(user_id)
    _require_mint_ceremony(user_id, request.session_token, "mint_mcp_token", http_request)

    scope = SESSION_SCOPE_MCP_OUTREACH if request.outreach else SESSION_SCOPE_MCP
    label = request.label or MCP_DEFAULT_LABEL
    body = _mint(user_id, label, scope, request.ttl_days, http_request)
    record_auth_event(AuthAuditEvent.MCP_TOKEN_MINTED, user_id=user_id,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      session_id=body["session_id"],
                      details={"scope": scope, "ttl_days": request.ttl_days, "label": label})
    notify_token_event(user_id, "minted", label=label, scope=scope)
    log_info("MCP token minted", user_id=user_id, scope=scope, ttl_days=request.ttl_days)
    return ResponseModel(status_code=200, detail=body)


def _granted_ttl_days(row: dict[str, Any]) -> int:
    """The TTL the row was minted with, in whole days, clamped to what a mint may grant."""
    created, expires = row.get("created_at"), row.get("expires_at")
    if not isinstance(created, datetime) or not isinstance(expires, datetime):
        return MCP_TTL_DEFAULT_DAYS
    days = round((expires - created).total_seconds() / 86400)
    return max(1, min(MCP_TTL_MAX_DAYS, days))


@router.post("/rotate", responses={
    200: {"description": "Old token revoked, new token minted; shown once"},
    401: {"description": "Invalid or expired session"},
    403: {"description": "Step-up required, or no strong factor enrolled"},
    404: {"description": "No such live agent token on this account, or the server is not enabled"},
    429: {"description": "Too many mints this hour"},
    503: {"description": "The token could not be read"},
})
def rotate_mcp_token(request: McpTokenRotateRequest,
                     http_request: Request = None) -> ResponseModel[dict[str, Any]]:  # type: ignore[assignment]
    """Replace one of the caller's MCP tokens with a new one: same label, scope and TTL.

    The old token is revoked FIRST. Rotation is what someone does when a token may have leaked, so
    a failure part-way must leave no live old token behind; the cost is that a failed mint leaves
    the user with none, and they mint again.
    """
    user_id = _caller(request.session_token)
    _require_enabled(user_id)
    _require_mint_ceremony(user_id, request.session_token, "rotate_mcp_token", http_request)

    try:
        row = get_owned_session(user_id, request.session_id, MCP_SESSION_SCOPES)
    except SessionStoreUnavailable:
        raise HTTPException(status_code=503, detail="Could not read that agent token. Try again.")
    if not row:
        raise HTTPException(status_code=404, detail="Agent token not found")
    if not revoke_session(user_id, request.session_id):
        raise HTTPException(status_code=404, detail="Agent token not found")

    scope = row.get("scope") or SESSION_SCOPE_MCP
    label = row.get("label") or MCP_DEFAULT_LABEL
    ttl_days = _granted_ttl_days(row)
    body = _mint(user_id, label, scope, ttl_days, http_request)
    record_auth_event(AuthAuditEvent.MCP_TOKEN_ROTATED, user_id=user_id,
                      ip=_main._client_ip(http_request), user_agent=_main._user_agent(http_request),
                      session_id=body["session_id"],
                      details={"scope": scope, "ttl_days": ttl_days, "label": label,
                               "old_session_id": request.session_id})
    notify_token_event(user_id, "rotated", label=label, scope=scope)
    log_info("MCP token rotated", user_id=user_id, scope=scope)
    return ResponseModel(status_code=200, detail=body)


@router.get("/info", responses={
    200: {"description": "Whether the MCP server is enabled, its URL, and this account's tokens"},
    401: {"description": "Invalid or expired session"},
    503: {"description": "The token list could not be read"},
})
def get_mcp_info(session_token: SessionTokenField = None) -> ResponseModel[dict[str, Any]]:
    """The Agents tab's read: enabled, the URL to connect to, and every MCP token on the account.

    Token-free: rows carry their id, label and dates, never a token or a hash. `calls_7d` and
    `last_client_name` come from Redis and are null when it cannot be read: unknown, never 0.
    Answered even when the rollout flag is off (`enabled: false`), so tokens minted before a
    rollback stay visible and revocable.
    """
    user_id = _caller(session_token)
    rows = list_sessions_by_scope(user_id, MCP_SESSION_SCOPES)
    if rows is None:
        raise HTTPException(status_code=503, detail="Could not read your agent tokens. Try again.")
    activity = read_activity([r["id"] for r in rows]) if rows else {}
    if activity is None:
        log_debug("MCP activity unreadable for the Agents tab", user_id=user_id)
    now = datetime.now(timezone.utc)
    tokens = []
    for row in rows:
        stats = (activity or {}).get(int(row["id"]), {})
        expires = row.get("expires_at")
        if isinstance(expires, datetime) and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        tokens.append({
            "session_id": row["id"],
            "label": row.get("label"),
            "scope": row.get("scope"),
            "created_at": _main._utc_iso(row.get("created_at")),
            "last_seen_at": _main._utc_iso(row.get("last_seen_at")),
            "expires_at": _main._utc_iso(row.get("expires_at")),
            "revoked": row.get("revoked_at") is not None,
            "expired": isinstance(expires, datetime) and expires <= now,
            "calls_7d": stats.get("calls_7d") if activity is not None else None,
            "last_client_name": stats.get("last_client_name") if activity is not None else None,
        })
    return ResponseModel(status_code=200, detail={
        "enabled": flag_enabled(MCP_SERVER, user_id),
        "mcp_url": _mcp_url(),
        "tokens": tokens,
    })


from cqc_lem.api import main as _main  # noqa: E402  — last; see `routers/__init__.py`
