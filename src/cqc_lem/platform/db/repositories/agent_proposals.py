"""Changes an agent ASKED for and only the user may apply (MCP epic #2797, S5 #2803; table of #2691).

`agent_change_proposals` holds one row per request: `payload` is `{field: {before, requested}}`, `reason`
is the agent's own text (untrusted: stored as sent, bounded, never trusted by anything here). A row is
`pending` until the user decides; it then becomes `approved` or `rejected`, or `expired` once its
seven days pass. Nothing in this module changes a setting.

The state machine is enforced in the SQL that writes it, not in the caller that read it first: the
user in two tabs, or a double click, can race a check-then-write. `decide_agent_proposal` is
`UPDATE ... WHERE status = 'pending' AND expires_at > now`, so exactly one decision wins and an
expired row can never be approved, whatever the caller thought it had read.

Times are naive UTC, passed in rather than read from `NOW()`, so a test can pin them.

`cqc_lem.utilities.db` re-exports every public name below, so importers use the facade; a test
patches the name HERE, where it lives.
"""

import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, NamedTuple, Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.utilities.logger import log_error

PROPOSAL_KIND_SETTINGS = "settings"
PROPOSAL_TTL_DAYS = 7
MAX_OPEN_PROPOSALS = 5
MAX_PROPOSAL_REASON_CHARS = 1000

PROPOSAL_PENDING = "pending"
PROPOSAL_APPROVED = "approved"
PROPOSAL_REJECTED = "rejected"
PROPOSAL_EXPIRED = "expired"

_COLUMNS = ("id, user_id, kind, payload, reason, status, source_session_id, created_at, expires_at, "
            "decided_at, decided_by_session_id, outcome")


class ProposalCreate(str, Enum):
    """How `create_agent_proposal` ended."""

    CREATED = "created"
    LIMIT = "limit"
    FAILED = "failed"


class ProposalCreated(NamedTuple):
    """The result of a create: how it ended and, when it worked, the new row's id."""

    status: ProposalCreate
    proposal_id: Optional[int] = None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _decode(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """A row with its JSON columns decoded (the driver hands them back as text)."""
    if row is None:
        return None
    out = dict(row)
    for column in ("payload", "outcome"):
        value = out.get(column)
        if isinstance(value, (bytes, str)):
            try:
                out[column] = json.loads(value)
            except ValueError:
                out[column] = None
    return out


def create_agent_proposal(user_id: int, kind: str, payload: dict[str, Any], reason: Optional[str],
                          source_session_id: Optional[int], *, max_open: int = MAX_OPEN_PROPOSALS,
                          ttl_days: int = PROPOSAL_TTL_DAYS,
                          now: Optional[datetime] = None) -> ProposalCreated:
    """File one pending proposal, unless the user already has `max_open` open ones.

    Args:
        user_id: Whose account it asks to change.
        kind: What it changes (`settings`).
        payload: `{field: {before, requested}}`.
        reason: The agent's own words, cut to `MAX_PROPOSAL_REASON_CHARS`.
        source_session_id: The MCP token's session id.
        max_open: The most pending, unexpired proposals one user may hold.
        ttl_days: Days until it expires.
        now: The clock, for tests.

    Returns:
        `CREATED` with the id, `LIMIT` when the user is at `max_open`, `FAILED` on a database fault.
    """
    moment = now or _now()
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM agent_change_proposals "
                "WHERE user_id = %s AND status = %s AND expires_at > %s",
                (user_id, PROPOSAL_PENDING, moment))
            row = cursor.fetchone()
            if row and int(row[0]) >= max_open:
                return ProposalCreated(ProposalCreate.LIMIT)
            cursor.execute(
                "INSERT INTO agent_change_proposals (user_id, kind, payload, reason, status, "
                "source_session_id, created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (user_id, kind, json.dumps(payload, default=str),
                 (reason or "")[:MAX_PROPOSAL_REASON_CHARS] or None, PROPOSAL_PENDING,
                 source_session_id, moment, moment + timedelta(days=ttl_days)))
            return ProposalCreated(ProposalCreate.CREATED, int(cursor.lastrowid))
    except mysql.connector.Error as err:
        log_error("Could not create an agent change proposal", exc=err, user_id=user_id)
        return ProposalCreated(ProposalCreate.FAILED)


def expire_agent_proposals(user_id: int, now: Optional[datetime] = None) -> int:
    """Mark this user's pending proposals past their expiry as `expired`; the number changed.

    Run before a list and before a decision, so a stale row never reads as pending.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE agent_change_proposals SET status = %s "
                "WHERE user_id = %s AND status = %s AND expires_at <= %s",
                (PROPOSAL_EXPIRED, user_id, PROPOSAL_PENDING, now or _now()))
            return int(cursor.rowcount or 0)
    except mysql.connector.Error as err:
        log_error("Could not expire agent change proposals", exc=err, user_id=user_id)
        return 0


def list_pending_agent_proposals(user_id: int, kind: str = PROPOSAL_KIND_SETTINGS,
                                 now: Optional[datetime] = None) -> Optional[list[dict[str, Any]]]:
    """The user's open proposals, newest first; None, never `[]`, when the read fails.

    An empty answer reads as "your agent asked for nothing", which a database fault must not say.
    """
    moment = now or _now()
    expire_agent_proposals(user_id, moment)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_COLUMNS} FROM agent_change_proposals "
                "WHERE user_id = %s AND kind = %s AND status = %s AND expires_at > %s "
                "ORDER BY id DESC LIMIT 50",
                (user_id, kind, PROPOSAL_PENDING, moment))
            return [_decode(r) for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not list agent change proposals", exc=err, user_id=user_id)
        return None


def count_pending_agent_proposals(user_id: int, now: Optional[datetime] = None) -> Optional[int]:
    """How many open proposals the user has; None when the read fails."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM agent_change_proposals "
                "WHERE user_id = %s AND status = %s AND expires_at > %s",
                (user_id, PROPOSAL_PENDING, now or _now()))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count agent change proposals", exc=err, user_id=user_id)
        return None


def get_agent_proposal(user_id: int, proposal_id: int) -> Optional[dict[str, Any]]:
    """One proposal, only if it is this user's; None when missing, someone else's, or unreadable."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_COLUMNS} FROM agent_change_proposals WHERE id = %s AND user_id = %s",
                (proposal_id, user_id))
            return _decode(cursor.fetchone())
    except mysql.connector.Error as err:
        log_error("Could not read an agent change proposal", exc=err, user_id=user_id)
        return None


def decide_agent_proposal(user_id: int, proposal_id: int, status: str,
                          decided_by_session_id: Optional[int] = None,
                          now: Optional[datetime] = None) -> bool:
    """Move a pending, unexpired proposal to `approved` or `rejected`. True only if THIS call did.

    The status and expiry checks are in the `WHERE`, so a second click, another tab or an expired row
    changes nothing and answers False.
    """
    if status not in (PROPOSAL_APPROVED, PROPOSAL_REJECTED):
        raise ValueError("a proposal is decided as approved or rejected")
    moment = now or _now()
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE agent_change_proposals SET status = %s, decided_at = %s, "
                "decided_by_session_id = %s WHERE id = %s AND user_id = %s AND status = %s "
                "AND expires_at > %s",
                (status, moment, decided_by_session_id, proposal_id, user_id, PROPOSAL_PENDING,
                 moment))
            return cursor.rowcount == 1
    except mysql.connector.Error as err:
        log_error("Could not decide an agent change proposal", exc=err, user_id=user_id)
        return False


def set_agent_proposal_outcome(user_id: int, proposal_id: int, outcome: dict[str, Any]) -> bool:
    """Record what an approval applied, left stale or declined (field names only)."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE agent_change_proposals SET outcome = %s WHERE id = %s AND user_id = %s",
                (json.dumps(outcome, default=str), proposal_id, user_id))
            return cursor.rowcount == 1
    except mysql.connector.Error as err:
        log_error("Could not record an agent change proposal outcome", exc=err, user_id=user_id)
        return False


def reopen_agent_proposal(user_id: int, proposal_id: int) -> bool:
    """Put an approved proposal back to pending: an approval whose every write failed applied nothing."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE agent_change_proposals SET status = %s, decided_at = NULL, "
                "decided_by_session_id = NULL WHERE id = %s AND user_id = %s AND status = %s",
                (PROPOSAL_PENDING, proposal_id, user_id, PROPOSAL_APPROVED))
            return cursor.rowcount == 1
    except mysql.connector.Error as err:
        log_error("Could not reopen an agent change proposal", exc=err, user_id=user_id)
        return False
