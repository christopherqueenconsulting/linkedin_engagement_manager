"""The pending-only edit every queue-only machine seat is held to (MCP epic #2682, W1 #2684).

A queue-only seat (`agent`, an `operator` on a `standard` account) may save a draft for a human to
review. It may not touch a row a human has already acted on: an approved DM, a sent invite, a
dismissed lead, a published post. Checking the status in the handler and then writing is a race —
the scanner, or the owner in another tab, can approve the row between the two — so the rule is
enforced in the SQL that writes: `UPDATE … WHERE id = %s AND user_id = %s AND status IN (…)`.

`cqc_lem.utilities.db` re-exports every public name below, so importers use the facade; a test
patches the name HERE, where it lives.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any, NamedTuple, Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor, to_naive_utc
from cqc_lem.utilities.logger import log_error


class PendingEdit(str, Enum):
    """What a pending-only edit did.

    `NOT_PENDING` covers a row that is not this user's, does not exist, or has left its pending
    state; the handler has already proved ownership, so in practice it means the last of these.
    """

    UPDATED = "updated"
    NOT_PENDING = "not_pending"
    FAILED = "failed"


class _EditableTable(NamedTuple):
    table: str
    pending: tuple[str, ...]
    columns: frozenset[str]


# The whitelist is what keeps the interpolated table and column names injection-safe: a kind or a
# column that is not listed here is refused before any SQL is built.
# `approved_by` / `approved_at` are here for ONE caller: an MCP seat's internal/sandbox approval
# (owner ruling O1, MCP W2), which moves a row out of pending through this same pending-only write,
# so approving something already approved, sent, canceled or failed is NOT_PENDING (409).
_APPROVAL_COLUMNS = frozenset({"approved_by", "approved_at"})
_TABLES: dict[str, _EditableTable] = {
    "scheduled_dm": _EditableTable(
        "scheduled_dms", ("pending",),
        frozenset({"recipient_profile_url", "recipient_name", "message", "scheduled_time",
                   "status"}) | _APPROVAL_COLUMNS),
    "connection_request": _EditableTable(
        "connection_requests", ("pending",),
        frozenset({"recipient_profile_url", "recipient_name", "message", "recipient_email",
                   "status", "failure_reason"}) | _APPROVAL_COLUMNS),
    # A lead signal's draft state is 'new' (LeadSignalStatus), not 'pending'.
    "lead_signal": _EditableTable(
        "lead_signals", ("new",), frozenset({"draft_response", "status"})),
    # `edited_via`: 'mcp' when an MCP seat rewrote the draft (MCP W2), so the review never
    # bulk-approves text an agent wrote.
    "catchup_touch": _EditableTable(
        "catchup_touches", ("pending",),
        frozenset({"message", "person_name", "status", "edited_via"}) | _APPROVAL_COLUMNS),
    "outreach_target": _EditableTable(
        "outreach_funnel_targets", ("pending",),
        frozenset({"target_name", "context_url", "draft_text", "status"}) | _APPROVAL_COLUMNS),
    "post": _EditableTable(
        "posts", ("pending",),
        frozenset({"content", "video_url", "scheduled_time", "post_type", "status",
                   "rejection_reason"})),
    # A curated source has no 'pending'. Before a human acts it is a candidate ('new') or has
    # become a draft post ('drafted'); 'approved' and 'published' are the owner's decisions.
    "curated_source": _EditableTable(
        "curated_sources", ("new", "drafted"), frozenset({"status"})),
}


def _bind(value: Any) -> Any:
    """The value the connector can bind: enums by value, datetimes as naive UTC."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return to_naive_utc(value)
    return value


def update_pending_row(kind: str, row_id: int, user_id: int,
                       fields: Mapping[str, Any]) -> PendingEdit:
    """Write `fields` onto one of `user_id`'s rows, only while that row is still pending.

    The row is locked with `SELECT … FOR UPDATE` and the UPDATE repeats the same predicate, so the
    status cannot move between the check and the write. The locked read, not `rowcount`, decides
    the answer: the connection does not set `CLIENT.FOUND_ROWS`, so MySQL reports CHANGED rows, and
    re-saving a draft with the text it already holds would otherwise read as "not pending".

    Args:
        kind: Which queue the row is in; a key of `_TABLES`.
        row_id: The row's id.
        user_id: The account that must own the row.
        fields: Column to value. A None value writes NULL; enums and datetimes are converted.

    Returns:
        UPDATED when the row was pending and the write ran, NOT_PENDING when no pending row of this
        user's matched (nothing is written), FAILED on a database error.

    Raises:
        ValueError: `kind` is unknown, `fields` is empty, or names a column outside the whitelist.
    """
    spec = _TABLES.get(kind)
    if spec is None:
        raise ValueError(f"unknown pending-edit kind {kind!r}")
    if not fields:
        raise ValueError("a pending-only edit needs at least one field")
    unknown = set(fields) - spec.columns
    if unknown:
        raise ValueError(f"columns not editable on {kind}: {sorted(unknown)}")

    columns = sorted(fields)
    assignments = ", ".join(f"{column} = %s" for column in columns)
    pending_in = ", ".join(["%s"] * len(spec.pending))
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"SELECT status FROM {spec.table} WHERE id = %s AND user_id = %s "
                           "FOR UPDATE", (row_id, user_id))
            row = cursor.fetchone()
            if not row or str(row[0]) not in spec.pending:
                return PendingEdit.NOT_PENDING
            cursor.execute(
                f"UPDATE {spec.table} SET {assignments} "
                f"WHERE id = %s AND user_id = %s AND status IN ({pending_in})",
                (*(_bind(fields[column]) for column in columns), row_id, user_id, *spec.pending))
            return PendingEdit.UPDATED
    except mysql.connector.Error as err:
        log_error("Could not apply a pending-only edit", exc=err, user_id=user_id,
                  action_type=kind)
        return PendingEdit.FAILED


def attach_email_to_connection_request(request_id: int, user_id: int,
                                       recipient_email: str) -> PendingEdit:
    """A queue-only seat's ONE edit on a non-pending row: the address an email challenge asked for.

    The contract with the `backfill` supplier (christopherqueenconsulting/backfill#44, ruled the
    narrow carve-out on #2684): a seat may write `recipient_email`, and nothing else, onto a request
    that is still `pending`, or that is `failed` with exactly `EMAIL_VERIFICATION_REQUIRED_MESSAGE`
    as its reason. The status is never written, so a failed row stays failed until a human retries
    it. Same lock-then-update shape as `update_pending_row`, for the same reasons.

    Args:
        request_id: The connection request.
        user_id: The account that must own it.
        recipient_email: The address to attach.

    Returns:
        UPDATED, NOT_PENDING when the row is in neither state (nothing is written), or FAILED on a
        database error.
    """
    from cqc_lem.platform.db.repositories.outreach import EMAIL_VERIFICATION_REQUIRED_MESSAGE

    predicate = "(status = 'pending' OR (status = 'failed' AND failure_reason = %s))"
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("SELECT status FROM connection_requests WHERE id = %s AND user_id = %s "
                           f"AND {predicate} FOR UPDATE",
                           (request_id, user_id, EMAIL_VERIFICATION_REQUIRED_MESSAGE))
            if not cursor.fetchone():
                return PendingEdit.NOT_PENDING
            cursor.execute("UPDATE connection_requests SET recipient_email = %s "
                           f"WHERE id = %s AND user_id = %s AND {predicate}",
                           (recipient_email, request_id, user_id,
                            EMAIL_VERIFICATION_REQUIRED_MESSAGE))
            return PendingEdit.UPDATED
    except mysql.connector.Error as err:
        log_error("Could not attach an email to a connection request", exc=err, user_id=user_id,
                  action_type="connection_request")
        return PendingEdit.FAILED


def reject_pending_curated_source(source_id: int, post_id: Optional[int], user_id: int,
                                  rejection_reason: str) -> PendingEdit:
    """Reject a curated source and its draft post together, only if no human has acted on either.

    Both rows are locked and checked before either is written, in ONE transaction, so the reject
    never half-applies: a draft post the owner approved a moment ago leaves the source untouched too.

    Args:
        source_id: The curated source.
        post_id: The draft post it became, or None when it has none this user owns.
        user_id: The account that must own both.
        rejection_reason: Written onto the post with its `rejected` status.

    Returns:
        UPDATED when both writes ran, NOT_PENDING when either row has left its pending state
        (nothing is written), FAILED on a database error.
    """
    source_spec, post_spec = _TABLES["curated_source"], _TABLES["post"]
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("SELECT status FROM curated_sources WHERE id = %s AND user_id = %s "
                           "FOR UPDATE", (source_id, user_id))
            source = cursor.fetchone()
            if not source or str(source[0]) not in source_spec.pending:
                return PendingEdit.NOT_PENDING
            if post_id:
                cursor.execute("SELECT status FROM posts WHERE id = %s AND user_id = %s FOR UPDATE",
                               (post_id, user_id))
                post = cursor.fetchone()
                if not post or str(post[0]) not in post_spec.pending:
                    return PendingEdit.NOT_PENDING
                cursor.execute("UPDATE posts SET status = 'rejected', rejection_reason = %s "
                               "WHERE id = %s AND user_id = %s AND status = 'pending'",
                               (rejection_reason, post_id, user_id))
            cursor.execute("UPDATE curated_sources SET status = 'rejected' WHERE id = %s "
                           "AND user_id = %s AND status IN ('new', 'drafted')",
                           (source_id, user_id))
            return PendingEdit.UPDATED
    except mysql.connector.Error as err:
        log_error("Could not reject a curated source", exc=err, user_id=user_id,
                  action_type="curated_source")
        return PendingEdit.FAILED
