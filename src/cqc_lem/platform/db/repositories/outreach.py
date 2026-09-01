"""Every SQL statement LEM runs against the outreach tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

from datetime import (
    date,
    datetime,
    timedelta,
    timezone,
)
from typing import Any, Optional

import mysql.connector

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.connection import (
    db_cursor,
    to_naive_utc,
)
from cqc_lem.platform.db.enums import (
    CatchupEventType,
    CatchupTouchStatus,
    ConnectionRequestStatus,
    ConnectStatus,
    FollowStatus,
    FollowupStatus,
    LeadSignalChannel,
    LeadSignalKind,
    LeadSignalSource,
    LeadSignalStatus,
    LeadStage,
    OutreachStage,
    OutreachStatus,
    PostStatus,
    ScheduledDmStatus,
)
from cqc_lem.platform.db.repositories.posts import get_engager_candidates
from cqc_lem.platform.db.shared import (
    ENGAGEMENT_TARGET_CONNECT_STATUSES,
    ENGAGEMENT_TARGET_FOLLOW_STATUSES,
    ENGAGEMENT_TARGET_WEEKLY_DEFAULT,
    BlockedVisit,
)
from cqc_lem.utilities.logger import log_error

# --- Target-creator engagement roster (issue #616) ---
# A curated list of accounts to comment on FIRST, ahead of the home feed. The blend the rotation
# aims for is 50% peers / 30% ICP / 20% large creators; the per-author weekly cap is the anti-pod
# guard so the same account never absorbs a run's whole comment budget.
ENGAGEMENT_TARGET_CATEGORIES = ("peer", "icp", "creator")
ENGAGEMENT_TARGET_SOURCES = ("user", "suggested")
ENGAGEMENT_TARGET_WEEKLY_MAX = 14
# Failed follow attempts before a target goes 'follow_failed'. Two, because one failure is usually a
# render race and the second says the control genuinely is not there.
ENGAGEMENT_TARGET_FOLLOW_MAX_ATTEMPTS = 2
def upsert_engagement_targets(user_id: int, targets: list) -> bool:
    """Upsert roster rows keyed on (user_id, profile_url). Only the editable fields are written —
    last_engaged_at / the weekly counter belong to the automation, so an edit never resets a cap.
    """
    rows = []
    for t in targets or []:
        url = str(t.get("profile_url") or "").strip()
        if not url:
            continue
        category = t.get("category")
        source = t.get("source")
        cap = t.get("max_comments_per_week")
        try:
            cap = int(cap) if cap is not None else ENGAGEMENT_TARGET_WEEKLY_DEFAULT
        except (TypeError, ValueError):
            cap = ENGAGEMENT_TARGET_WEEKLY_DEFAULT
        rows.append((
            user_id, url, (t.get("name") or None),
            category if category in ENGAGEMENT_TARGET_CATEGORIES else "peer",
            max(0, min(ENGAGEMENT_TARGET_WEEKLY_MAX, cap)),
            1 if t.get("active", True) else 0,
            source if source in ENGAGEMENT_TARGET_SOURCES else "user"))
    if not rows:
        return True
    try:
        with db_cursor(commit=True) as cursor:
            cursor.executemany(
                "INSERT INTO engagement_targets (user_id, profile_url, name, category, "
                "max_comments_per_week, active, source) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), category=VALUES(category), "
                "max_comments_per_week=VALUES(max_comments_per_week), active=VALUES(active), "
                "source=VALUES(source)", rows)
            return True
    except mysql.connector.Error as err:
        log_error("Could not upsert engagement targets", exc=err, user_id=user_id)
        return False
def delete_engagement_target(user_id: int, profile_url: str) -> bool:
    """Remove a roster author from the user's engagement targets.

    True means the DELETE ran, not that the target existed.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM engagement_targets WHERE user_id=%s AND profile_url=%s",
                           (user_id, str(profile_url or "").strip()))
            return True
    except mysql.connector.Error as err:
        log_error("Could not delete engagement target", exc=err, user_id=user_id)
        return False
def record_target_comment_blocked(user_id: int, profile_url: str) -> BlockedVisit:
    """Count ONE visit where a roster target's posts rendered but none carried a comment affordance
    — the restricted-comments signature (issue #962) — and escalate the target to
    'needs_connection' when following has demonstrably not unlocked it (issue #979).

    Returns the new streak (0 if nothing was written) so the caller can log the surface crossing
    exactly once, plus the resulting connect state.

    Distinct from "no posts / only reshares", which the caller never reports here: that is a plain
    skip and says nothing about whether the author accepts comments.

    The escalation is guarded on evidence, not on hope: the target must be `following`, must have a
    `followed_at`, and its PREVIOUS blocked visit must already have been after that follow. So this
    visit is the SECOND post-follow block — one is a render race, two is the account telling us
    following was not the missing permission. A target that was never followed is never escalated.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    url = str(profile_url or "").strip()
    try:
        cursor.execute(
            "UPDATE engagement_targets SET "
            # connect_status is assigned FIRST on purpose: MySQL evaluates SET clauses left to right
            # and a later one already sees the new value, so updating last_blocked_at first would
            # destroy the very evidence this test reads (the PREVIOUS blocked visit's timestamp).
            "connect_status = IF(connect_status = %s AND follow_status = %s "
            "                    AND followed_at IS NOT NULL AND last_blocked_at IS NOT NULL "
            "                    AND last_blocked_at > followed_at, %s, connect_status), "
            # LEAST() keeps the TINYINT UNSIGNED column from wrapping on a target that stays blocked
            # for months — the badge only cares that the streak is at or past its threshold.
            "comment_blocked_streak = LEAST(255, comment_blocked_streak + 1), last_blocked_at = NOW() "
            "WHERE user_id=%s AND profile_url=%s",
            (ConnectStatus.UNKNOWN.value, FollowStatus.FOLLOWING.value,
             ConnectStatus.NEEDS_CONNECTION.value, user_id, url))
        connection.commit()
        if cursor.rowcount <= 0:
            return BlockedVisit(0, ConnectStatus.UNKNOWN.value)
        cursor.execute("SELECT comment_blocked_streak, connect_status FROM engagement_targets "
                       "WHERE user_id=%s AND profile_url=%s", (user_id, url))
        row = cursor.fetchone()
        if not row:
            return BlockedVisit(0, ConnectStatus.UNKNOWN.value)
        return BlockedVisit(int(row[0]), str(row[1] or ConnectStatus.UNKNOWN.value))
    except mysql.connector.Error as err:
        log_error("Could not record blocked roster target", exc=err, user_id=user_id)
        return BlockedVisit(0, ConnectStatus.UNKNOWN.value)
    finally:
        cursor.close()
        connection.close()
def set_target_follow_status(user_id: int, profile_url: str, status: FollowStatus) -> bool:
    """Write a roster target's follow state (issue #962). 'following' stamps `followed_at` and
    clears the attempt counter — it is reached both by a verified click and by the zero-cost
    catch-up where the top card already said "Following".
    """
    if status not in ENGAGEMENT_TARGET_FOLLOW_STATUSES:
        log_error(f"Refusing to write unknown follow status {status!r}", user_id=user_id)
        return False
    status = FollowStatus(status)
    try:
        with db_cursor(commit=True) as cursor:
            if status is FollowStatus.FOLLOWING:
                cursor.execute(
                    "UPDATE engagement_targets SET follow_status=%s, followed_at=NOW(), "
                    "follow_attempts=0 WHERE user_id=%s AND profile_url=%s",
                    (status.value, user_id, str(profile_url or "").strip()))
            else:
                cursor.execute(
                    "UPDATE engagement_targets SET follow_status=%s WHERE user_id=%s AND profile_url=%s",
                    (status.value, user_id, str(profile_url or "").strip()))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not set roster target follow status", exc=err, user_id=user_id)
        return False
def record_target_follow_failure(user_id: int, profile_url: str) -> int:
    """One failed follow attempt on a roster target. Returns the new attempt count.

    At `ENGAGEMENT_TARGET_FOLLOW_MAX_ATTEMPTS` the status goes terminal ('follow_failed') in the
    same statement, which both badges the target for the user and stops the roster pass from
    spending a click on it every single run.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    url = str(profile_url or "").strip()
    try:
        cursor.execute(
            "UPDATE engagement_targets SET "
            # follow_status is assigned FIRST on purpose: MySQL evaluates SET clauses left to right
            # and a later one already sees the new value, so incrementing first would make this test
            # read the post-increment count and fire a run early.
            "follow_status = IF(follow_attempts + 1 >= %s, %s, %s), "
            "follow_attempts = LEAST(255, follow_attempts + 1) "
            "WHERE user_id=%s AND profile_url=%s",
            (ENGAGEMENT_TARGET_FOLLOW_MAX_ATTEMPTS, FollowStatus.FOLLOW_FAILED.value,
             FollowStatus.NOT_FOLLOWING.value, user_id, url))
        connection.commit()
        if cursor.rowcount <= 0:
            return 0
        cursor.execute("SELECT follow_attempts FROM engagement_targets "
                       "WHERE user_id=%s AND profile_url=%s", (user_id, url))
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not record roster follow failure", exc=err, user_id=user_id)
        return 0
    finally:
        cursor.close()
        connection.close()
def set_target_connect_status(user_id: int, profile_url: str, status: ConnectStatus) -> bool:
    """Write a roster target's connect state (issue #979).

    'requested' stamps `connect_requested_at` ONCE (`COALESCE`) — the column means "when our one
    invite went out", and a later read-only visit that merely re-observes a Pending control must not
    keep moving that date forward. Standing a target back down to 'needs_connection' (a dispatch
    that was throttled before anything reached LinkedIn) clears the stamp, because no invite exists
    to date.
    """
    if status not in ENGAGEMENT_TARGET_CONNECT_STATUSES:
        log_error(f"Refusing to write unknown connect status {status!r}", user_id=user_id)
        return False
    status = ConnectStatus(status)
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    url = str(profile_url or "").strip()
    try:
        if status is ConnectStatus.REQUESTED:
            cursor.execute(
                "UPDATE engagement_targets SET connect_status=%s, "
                "connect_requested_at=COALESCE(connect_requested_at, NOW()) "
                "WHERE user_id=%s AND profile_url=%s", (status.value, user_id, url))
        elif status is ConnectStatus.NEEDS_CONNECTION:
            cursor.execute(
                "UPDATE engagement_targets SET connect_status=%s, connect_requested_at=NULL "
                "WHERE user_id=%s AND profile_url=%s", (status.value, user_id, url))
        else:
            cursor.execute(
                "UPDATE engagement_targets SET connect_status=%s WHERE user_id=%s AND profile_url=%s",
                (status.value, user_id, url))
        connection.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not set roster target connect status", exc=err, user_id=user_id)
        return False
    finally:
        cursor.close()
        connection.close()
# --- Scheduled 1:1 DMs (issue #306) — mirrors the post scheduler ---
_SCHED_DM_COLS = ("id", "user_id", "recipient_profile_url", "recipient_name", "message",
                  "source", "scheduled_time", "status", "created_at", "updated_at")
# Statuses where a drafted nurture DM is still "live" for its thread — a second draft to the same
# person while one of these is open would be two messages queued for one reply.
_OPEN_SCHED_DM_STATUSES = (ScheduledDmStatus.PENDING, ScheduledDmStatus.APPROVED,
                           ScheduledDmStatus.SCHEDULED)
def insert_scheduled_dm(user_id: int, recipient_profile_url: str, message: str,
                        scheduled_time: datetime, recipient_name: str = None,
                        status: "ScheduledDmStatus" = ScheduledDmStatus.PENDING,
                        source: str = None) -> Optional[int]:
    """Queue a DM draft and return its id; None when the insert failed.

    Nothing is sent from here — the default status is PENDING, which is an approval gate. `source` names
    the mechanic that drafted it ('nurture', 'artifact'; NULL means a person wrote it by hand), and that
    is what lets each mechanic carry its own daily draft cap while sharing the one-open-draft rule.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO scheduled_dms (user_id, recipient_profile_url, recipient_name, message, "
                "source, scheduled_time, status) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_id, recipient_profile_url, recipient_name, message,
                 str(source) if source else None, to_naive_utc(scheduled_time), str(status)))
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_error("Could not insert scheduled DM", exc=err, user_id=user_id)
        return None
def has_open_scheduled_dm(user_id: int, recipient_profile_url: str, source: str = None) -> bool:
    """True when this person already has a queued DM that hasn't gone out yet (issue #485 dedup —
    one drafted next message per conversation). Fails SAFE to True: on a DB error we skip drafting
    rather than risk stacking two messages on one thread.
    """
    if not recipient_profile_url:
        return True
    where = "user_id=%s AND recipient_profile_url=%s"
    params: list = [user_id, recipient_profile_url]
    if source:
        where += " AND source=%s"
        params.append(str(source))
    placeholders = ", ".join(["%s"] * len(_OPEN_SCHED_DM_STATUSES))
    try:
        with db_cursor() as cursor:
            cursor.execute(f"SELECT 1 FROM scheduled_dms WHERE {where} "
                           f"AND status IN ({placeholders}) LIMIT 1",
                           tuple(params + [str(s) for s in _OPEN_SCHED_DM_STATUSES]))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error("Could not check open scheduled DMs", exc=err, user_id=user_id)
        return True
def count_scheduled_dms_created_today(user_id: int, source: str = None) -> int:
    """How many DMs were DRAFTED for this user today (optionally only from one source). The daily
    send cap already guards delivery; this bounds the auto-nurture drafting itself, since each draft
    costs an LLM call and fills the operator's approval queue.
    """
    where = "user_id=%s AND created_at >= CURDATE()"
    params: list = [user_id]
    if source:
        where += " AND source=%s"
        params.append(str(source))
    try:
        with db_cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM scheduled_dms WHERE {where}", tuple(params))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] else 0
    except mysql.connector.Error as err:
        log_error("Could not count today's scheduled DMs", exc=err, user_id=user_id)
        return 0
def get_scheduled_dm(dm_id: int) -> Optional[dict]:
    """One scheduled-DM row, or None when it does not exist or the read failed."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT {', '.join(_SCHED_DM_COLS)} FROM scheduled_dms WHERE id = %s", (dm_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get scheduled DM {dm_id}", exc=err)
        return None
def get_scheduled_dms(user_id: int, status_filter: str = None, page: int = 1,
                      page_size: int = 25, sort_order: str = "asc") -> dict:
    """Paginated list of a user's scheduled DMs (mirrors the posts list response)."""
    order = "DESC" if str(sort_order).lower() == "desc" else "ASC"
    where = "WHERE user_id = %s"
    params: list = [user_id]
    if status_filter:
        where += " AND status = %s"
        params.append(status_filter)
    offset = max(0, (max(1, page) - 1) * page_size)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT COUNT(*) AS c FROM scheduled_dms {where}", tuple(params))
            total = int(cursor.fetchone()["c"])
            cursor.execute(
                f"SELECT {', '.join(_SCHED_DM_COLS)} FROM scheduled_dms {where} "
                f"ORDER BY scheduled_time {order} LIMIT %s OFFSET %s",
                tuple(params + [page_size, offset]))
            rows = cursor.fetchall()
            for r in rows:
                if isinstance(r.get("scheduled_time"), datetime):
                    r["scheduled_time"] = r["scheduled_time"].isoformat()
                for k in ("created_at", "updated_at"):
                    if isinstance(r.get(k), datetime):
                        r[k] = r[k].isoformat()
            return {"dms": rows, "total": total, "page": page, "page_size": page_size}
    except mysql.connector.Error as err:
        log_error("Could not list scheduled DMs", exc=err, user_id=user_id)
        return {"dms": [], "total": 0, "page": page, "page_size": page_size}
def get_due_scheduled_dms(post_time_delta_minutes: int = 20) -> list:
    """Approved DMs whose scheduled_time is at or before now+delta. Deliberately NO lower bound:
    an approved DM can drift arbitrarily far past its slot (e.g. deferred repeatedly by the daily
    DM cap) and must stay eligible until sent/canceled. Oldest first so overdue DMs drain in order.
    Returns (id, scheduled_time, user_id) tuples.
    """
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=post_time_delta_minutes)
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT id, scheduled_time, user_id FROM scheduled_dms "
                "WHERE status = 'approved' AND scheduled_time <= %s "
                "ORDER BY scheduled_time ASC",
                (window_end,))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get due scheduled DMs", exc=err)
        return []
def get_orphaned_scheduled_dms(lookback_hours: int = 2) -> list:
    """DMs stuck in 'scheduled' whose send task was lost (e.g. Celery queue purged on container
    restart) before reaching sent/failed. Mirrors get_orphaned_scheduled_posts — the lookback gap
    avoids racing a task that is still in flight. Returns (id, scheduled_time, user_id) tuples.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT id, scheduled_time, user_id FROM scheduled_dms "
                "WHERE status = 'scheduled' AND scheduled_time <= %s "
                "ORDER BY scheduled_time ASC",
                (cutoff,))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get orphaned scheduled DMs", exc=err)
        return []
def update_scheduled_dm_status(dm_id: int, status: "ScheduledDmStatus") -> bool:
    """Move a queued DM's status.

    True whenever the UPDATE ran — a status write against an id that no longer exists is not reported.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE scheduled_dms SET status = %s WHERE id = %s", (str(status), dm_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not update scheduled DM {dm_id} status", exc=err)
        return False
def update_scheduled_dm(dm_id: int, recipient_profile_url: str = None, recipient_name: str = None,
                        message: str = None, scheduled_time: datetime = None,
                        status: "ScheduledDmStatus" = None) -> bool:
    """Patch only the fields that were supplied; False when none were.

    Omitted arguments are left alone rather than nulled, so the editor can save one field without
    blanking the rest. True means the UPDATE ran, not that a row matched.
    """
    fields, params = [], []
    for col, val in (("recipient_profile_url", recipient_profile_url), ("recipient_name", recipient_name),
                     ("message", message), ("scheduled_time", to_naive_utc(scheduled_time))):
        if val is not None:
            fields.append(f"{col} = %s")
            params.append(val)
    if status is not None:
        fields.append("status = %s")
        params.append(str(status))
    if not fields:
        return False
    params.append(dm_id)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE scheduled_dms SET {', '.join(fields)} WHERE id = %s", tuple(params))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not update scheduled DM {dm_id}", exc=err)
        return False
# --- Proactive connection requests (issue #398) — approval-gated, daily-capped; reuses invite_to_connect ---
# source/icp_score/reasons carry issue #486's targeting provenance (which engagement surfaced this
# person, how well they fit) so the operator approving a request can see why it exists.
_CONN_REQ_COLS = ("id", "user_id", "recipient_profile_url", "recipient_name", "message", "source",
                  "icp_score", "reasons", "failure_reason", "status", "attempts", "created_at",
                  "updated_at", "recipient_email IS NOT NULL AS has_recipient_email")
_CONN_REQ_DISPATCH_COLS = (*_CONN_REQ_COLS[:-1], "recipient_email")
# A row lands here (issue #1836) when the Connect dialog opened but demanded the recipient's email
# and the app cleared it once nothing more can be done with it — see EMAIL_VERIFICATION_REQUIRED_MESSAGE
# below and the migration comment. Normalized to lower case so enum and raw-string callers both hit.
_CONNECTION_REQUEST_TERMINAL_STATUSES = frozenset({
    ConnectionRequestStatus.SENT.value, ConnectionRequestStatus.FAILED.value,
    ConnectionRequestStatus.CANCELED.value})


def _is_terminal_connection_request_status(status: "ConnectionRequestStatus | str") -> bool:
    """Whether a status permanently stops a request, so its recipient email must be cleared."""
    return str(status).lower() in _CONNECTION_REQUEST_TERMINAL_STATUSES
# Real dispatch attempts (issue #1814) before an unreachable target goes terminal instead of cycling
# 'approved' forever. Only a dispatch that actually called invite_to_connect_now counts — the
# invite hold, the daily cap and LinkedInRateLimited all defer without calling it, so an
# indefinitely-throttled target never burns this down. Three, matching
# ENGAGEMENT_TARGET_FOLLOW_MAX_ATTEMPTS's reasoning one step looser: a full invite dispatch
# (login + navigate + open the dialog) has more moving parts that can flake once before genuinely
# failing than a single follow click does.
CONNECTION_REQUEST_MAX_ATTEMPTS = 3
def insert_connection_request(user_id: int, recipient_profile_url: str, message: str = None,
                              recipient_name: str = None,
                              status: "ConnectionRequestStatus" = ConnectionRequestStatus.PENDING,
                              source: str = None, icp_score: int = None, reasons: str = None,
                              recipient_email: Optional[str] = None) -> Optional[int]:
    """Queue an approval-gated connection request and return its id; None when the insert failed.

    Nothing is sent from here — the default status is PENDING. `source` / `icp_score` / `reasons` carry
    the targeting provenance (issue #486) so the person approving can see WHY the row exists.
    `recipient_email` (issue #1836) is the recipient's email, used only when LinkedIn's Connect
    dialog demands one to verify the connection — most rows never carry one.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO connection_requests (user_id, recipient_profile_url, recipient_name, "
                "message, status, source, icp_score, reasons, recipient_email) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, recipient_profile_url, recipient_name, message, str(status),
                 source, icp_score, (reasons or None), recipient_email))
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_error("Could not insert connection request", exc=err, user_id=user_id)
        return None
def get_connection_request(request_id: int) -> Optional[dict]:
    """One connection-request row, or None when it does not exist or the read failed."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {', '.join(_CONN_REQ_DISPATCH_COLS)} FROM connection_requests WHERE id = %s",
                (request_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get connection request {request_id}", exc=err)
        return None
def get_connection_requests(user_id: int, status_filter: str = None, page: int = 1,
                            page_size: int = 25, sort_order: str = "desc") -> dict:
    """Paginated list of a user's connection requests (mirrors get_scheduled_dms)."""
    order = "ASC" if str(sort_order).lower() == "asc" else "DESC"
    where = "WHERE user_id = %s"
    params: list = [user_id]
    if status_filter:
        where += " AND status = %s"
        params.append(status_filter)
    offset = max(0, (max(1, page) - 1) * page_size)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT COUNT(*) AS c FROM connection_requests {where}", tuple(params))
            total = int(cursor.fetchone()["c"])
            cursor.execute(
                f"SELECT {', '.join(_CONN_REQ_COLS)} FROM connection_requests {where} "
                f"ORDER BY created_at {order} LIMIT %s OFFSET %s",
                tuple(params + [page_size, offset]))
            rows = cursor.fetchall()
            for r in rows:
                for k in ("created_at", "updated_at"):
                    if isinstance(r.get(k), datetime):
                        r[k] = r[k].isoformat()
            return {"requests": rows, "total": total, "page": page, "page_size": page_size}
    except mysql.connector.Error as err:
        log_error("Could not list connection requests", exc=err, user_id=user_id)
        return {"requests": [], "total": 0, "page": page, "page_size": page_size}
def get_approved_connection_requests() -> list:
    """Approved connection requests waiting to be sent, oldest first. Returns (id, user_id) tuples.
    The daily cap is enforced by the scanner/send task, not here.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT id, user_id FROM connection_requests WHERE status = 'approved' "
                "ORDER BY created_at ASC")
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get approved connection requests", exc=err)
        return []
def get_orphaned_connection_requests(lookback_hours: int = 2) -> list:
    """Requests stuck in 'sending' whose send task was lost (e.g. Celery queue purged on restart).
    Mirrors get_orphaned_scheduled_dms — the lookback gap avoids racing an in-flight task.
    Returns (id, user_id) tuples.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT id, user_id FROM connection_requests WHERE status = 'sending' "
                "AND updated_at <= %s ORDER BY updated_at ASC",
                (cutoff,))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get orphaned connection requests", exc=err)
        return []
def update_connection_request_status(request_id: int, status: "ConnectionRequestStatus",
                                     failure_reason: str = None) -> bool:
    """Move a request to `status`. `failure_reason` records WHY a send failed (issue #623) — it is
    written on every call, so a request that later succeeds or is deferred clears the stale reason
    instead of showing yesterday's failure next to today's status.

    A move into a TERMINAL status (issue #1836) also clears `recipient_email` — the bounded-exposure
    half of the storage decision in the migration comment: nothing more will ever be done with the
    address once the row can no longer be dispatched.
    """
    clear_email = ", recipient_email = NULL" if _is_terminal_connection_request_status(status) else ""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"UPDATE connection_requests SET status = %s, failure_reason = %s{clear_email} "
                "WHERE id = %s",
                (str(status), (str(failure_reason)[:512] if failure_reason else None), request_id))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not update connection request {request_id} status", exc=err)
        return False
def record_connection_request_attempt(request_id: int, failure_reason: str,
                                      terminal: bool = False) -> "tuple[bool, int]":
    """Record ONE real dispatch attempt that reached LinkedIn and did not send (issue #1814).

    At `CONNECTION_REQUEST_MAX_ATTEMPTS` the row goes terminal ('failed') in the same statement
    instead of back to 'approved', so the scanner stops re-dispatching a target that has genuinely
    never been reachable — every browser session it costs is a slot the shared `se_outreach` lane
    needed. Below the ceiling it goes back to 'approved' for the next scan, same as an untouched
    retry. Returns (terminal, attempts); (False, 0) means the row was gone or the write failed.

    `terminal=True` retires the row on THIS attempt regardless of the count (issue #1813). The
    ceiling exists to stop guessing about a target that keeps failing for reasons we cannot read;
    a caller that has PROVEN the target is unreachable — an out-of-network profile offering nothing
    but Follow — has nothing left to learn from two more Chrome sessions. It stays one statement so
    the attempt and the retirement can never land separately.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE connection_requests SET "
                # status is assigned FIRST on purpose: MySQL evaluates SET clauses left to right, so
                # a later 'attempts = attempts + 1' would make this test read the post-increment count.
                "status = IF(%s OR attempts + 1 >= %s, %s, %s), "
                # Terminal here too (issue #1836) — same bounded-exposure rule as
                # update_connection_request_status: an email held for a target that just went FAILED
                # will never be used again.
                "recipient_email = IF(%s OR attempts + 1 >= %s, NULL, recipient_email), "
                "attempts = attempts + 1, "
                "failure_reason = %s "
                "WHERE id = %s",
                (1 if terminal else 0, CONNECTION_REQUEST_MAX_ATTEMPTS,
                 ConnectionRequestStatus.FAILED.value, ConnectionRequestStatus.APPROVED.value,
                 1 if terminal else 0, CONNECTION_REQUEST_MAX_ATTEMPTS,
                 str(failure_reason or "")[:512], request_id))
            if cursor.rowcount <= 0:
                return False, 0
            cursor.execute("SELECT attempts FROM connection_requests WHERE id = %s", (request_id,))
            row = cursor.fetchone()
            attempts = int(row[0]) if row else 0
            return bool(terminal) or attempts >= CONNECTION_REQUEST_MAX_ATTEMPTS, attempts
    except mysql.connector.Error as err:
        log_error(f"Could not record connection request attempt {request_id}", exc=err)
        return False, 0
def update_connection_request(request_id: int, recipient_profile_url: str = None,
                              recipient_name: str = None, message: str = None,
                              status: "ConnectionRequestStatus" = None,
                              recipient_email: Optional[str] = None) -> bool:
    """Patch only the fields that were supplied; False when none were.

    Reports `rowcount > 0`, unlike the scheduled-DM updater — so False here also means "no such row", or
    that every supplied value was already what the row held. A status change also clears `failure_reason`
    (issue #1735), matching `update_connection_request_status` — a retried or re-approved row must not
    keep showing yesterday's failure next to today's status.

    A move into a TERMINAL status also clears `recipient_email` (issue #1836), same as
    `update_connection_request_status` — UNLESS this same call is also supplying a fresh
    `recipient_email`, which wins (two `recipient_email = ...` clauses in one UPDATE is invalid SQL).
    """
    fields, params = [], []
    for col, val in (("recipient_profile_url", recipient_profile_url),
                     ("recipient_name", recipient_name), ("message", message),
                     ("recipient_email", recipient_email)):
        if val is not None:
            fields.append(f"{col} = %s")
            params.append(val)
    if status is not None:
        fields.append("status = %s")
        params.append(str(status))
        fields.append("failure_reason = NULL")
        if recipient_email is None and _is_terminal_connection_request_status(status):
            fields.append("recipient_email = NULL")
    if not fields:
        return False
    params.append(request_id)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE connection_requests SET {', '.join(fields)} WHERE id = %s", tuple(params))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not update connection request {request_id}", exc=err)
        return False
def count_open_connection_requests(user_id: int) -> int:
    """Targets already queued but not yet sent (pending / approved / sending). The sourcing scan
    subtracts these from the daily invite budget so it can't pile up a backlog that would spend
    tomorrow's cap the moment it opens.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM connection_requests WHERE user_id=%s "
                "AND status IN ('pending','approved','sending')", (user_id,))
            r = cursor.fetchone()
            return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        log_error("Could not count open connection requests", exc=err, user_id=user_id)
        return 0
def get_requested_person_keys(user_id: int) -> set:
    """person_key()s for everyone this user has EVER had a connection request row for, any status.
    The dedup set for the nightly sourcing scan: a canceled/failed target must not come back every
    night, and someone already invited must never be invited twice.
    """
    from cqc_lem.utilities.lead_scoring import person_key
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT recipient_name, recipient_profile_url FROM connection_requests WHERE user_id=%s",
                (user_id,))
            keys = set()
            for name, url in cursor.fetchall():
                key = person_key(name, url)
                if key:
                    keys.add(key)
            return keys
    except mysql.connector.Error as err:
        log_error("Could not read requested person keys", exc=err, user_id=user_id)
        return set()
# --- Comment-first outreach funnel (issue #399) — approval-gated comment->connect->DM ---
_OUTREACH_COLS = ("id", "user_id", "target_profile_url", "target_name", "stage", "status",
                  "context_url", "draft_text", "notes", "created_at", "updated_at")
def insert_outreach_target(user_id: int, target_profile_url: str, target_name: str = None,
                           context_url: str = None, draft_text: str = None,
                           stage: "OutreachStage" = OutreachStage.COMMENT,
                           status: "OutreachStatus" = OutreachStatus.PENDING) -> Optional[int]:
    """Enter a person into the comment-first outreach funnel and return the row id.

    Starts at the COMMENT stage, PENDING: the funnel is approval-gated end to end, so this only queues.
    None when the insert failed — including when the (user, profile) pair is already in the funnel.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO outreach_funnel_targets (user_id, target_profile_url, target_name, stage, "
                "status, context_url, draft_text) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_id, target_profile_url, target_name, str(stage), str(status), context_url, draft_text))
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_error("Could not insert outreach target", exc=err, user_id=user_id)
        return None
def get_outreach_target(target_id: int) -> Optional[dict]:
    """One outreach-funnel row, or None when it does not exist or the read failed."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {', '.join(_OUTREACH_COLS)} FROM outreach_funnel_targets WHERE id = %s", (target_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get outreach target {target_id}", exc=err)
        return None
def get_outreach_target_by_url(user_id: int, target_profile_url: str) -> Optional[dict]:
    """This user's funnel row for a target profile, or None.

    The dedup lookup: `(user_id, target_profile_url)` is UNIQUE, so one person is only ever in the funnel
    once and this is how a sourcing pass finds out.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {', '.join(_OUTREACH_COLS)} FROM outreach_funnel_targets "
                "WHERE user_id = %s AND target_profile_url = %s", (user_id, target_profile_url))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not look up outreach target", exc=err, user_id=user_id)
        return None
def get_outreach_targets(user_id: int, status_filter: str = None, stage_filter: str = None,
                         page: int = 1, page_size: int = 25, sort_order: str = "asc") -> dict:
    """Paginated list of a user's outreach-funnel targets (mirrors the scheduled-DM list response)."""
    order = "DESC" if str(sort_order).lower() == "desc" else "ASC"
    where = "WHERE user_id = %s"
    params: list = [user_id]
    if status_filter:
        where += " AND status = %s"
        params.append(status_filter)
    if stage_filter:
        where += " AND stage = %s"
        params.append(stage_filter)
    offset = max(0, (max(1, page) - 1) * page_size)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT COUNT(*) AS c FROM outreach_funnel_targets {where}", tuple(params))
            total = int(cursor.fetchone()["c"])
            cursor.execute(
                f"SELECT {', '.join(_OUTREACH_COLS)} FROM outreach_funnel_targets {where} "
                f"ORDER BY updated_at {order} LIMIT %s OFFSET %s",
                tuple(params + [page_size, offset]))
            rows = cursor.fetchall()
            for r in rows:
                for k in ("created_at", "updated_at"):
                    if isinstance(r.get(k), datetime):
                        r[k] = r[k].isoformat()
            return {"targets": rows, "total": total, "page": page, "page_size": page_size}
    except mysql.connector.Error as err:
        log_error("Could not list outreach targets", exc=err, user_id=user_id)
        return {"targets": [], "total": 0, "page": page, "page_size": page_size}
def get_approved_outreach_targets(user_id: int) -> list:
    """Approved, not-yet-completed funnel targets for a user — the rows the processor may fire.
    Oldest-updated first so a backlog drains in order. Returns dict rows.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {', '.join(_OUTREACH_COLS)} FROM outreach_funnel_targets "
                "WHERE user_id = %s AND status = 'approved' AND stage <> 'completed' "
                "ORDER BY updated_at ASC", (user_id,))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get approved outreach targets", exc=err, user_id=user_id)
        return []
def count_open_outreach_targets(user_id: int) -> int:
    """Funnel targets still awaiting a human or a stage fire (pending / approved, not completed).
    The sourcing scan (issue #623) stops adding once this backlog is deep enough — a review queue
    nobody works through is the same as no queue at all.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM outreach_funnel_targets WHERE user_id=%s "
                "AND status IN ('pending','approved') AND stage <> 'completed'", (user_id,))
            r = cursor.fetchone()
            return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        log_error("Could not count open outreach targets", exc=err, user_id=user_id)
        return 0
def get_users_with_approved_outreach() -> list:
    """Distinct user_ids that have at least one approved, non-completed funnel target (dispatcher)."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT user_id FROM outreach_funnel_targets "
                "WHERE status = 'approved' AND stage <> 'completed'")
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get users with approved outreach", exc=err)
        return []
def update_outreach_target_status(target_id: int, status: "OutreachStatus") -> bool:
    """Move an outreach target's status.

    True whenever the UPDATE ran, matched or not.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE outreach_funnel_targets SET status = %s WHERE id = %s",
                           (str(status), target_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not update outreach target {target_id} status", exc=err)
        return False
def update_outreach_target(target_id: int, target_profile_url: str = None, target_name: str = None,
                           context_url: str = None, draft_text: str = None, notes: str = None,
                           stage: "OutreachStage" = None, status: "OutreachStatus" = None) -> bool:
    """Patch only the fields that were supplied; False when none were.

    Stage and status are stringified from their enums; omitted arguments are left alone rather than
    nulled. True means the UPDATE ran, not that a row matched.
    """
    fields, params = [], []
    for col, val in (("target_profile_url", target_profile_url), ("target_name", target_name),
                     ("context_url", context_url), ("draft_text", draft_text), ("notes", notes)):
        if val is not None:
            fields.append(f"{col} = %s")
            params.append(val)
    for col, val in (("stage", stage), ("status", status)):
        if val is not None:
            fields.append(f"{col} = %s")
            params.append(str(val))
    if not fields:
        return False
    params.append(target_id)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE outreach_funnel_targets SET {', '.join(fields)} WHERE id = %s",
                           tuple(params))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not update outreach target {target_id}", exc=err)
        return False
# --- LinkedIn Catch-up touches (issue #482) — approval-gated, deduped milestone congratulations ---
_CATCHUP_COLS = ("id", "user_id", "profile_url", "person_name", "event_type", "event_detail",
                 "event_period", "score", "message", "status", "created_at", "updated_at")
def insert_catchup_touch(user_id: int, profile_url: str, event_type: "CatchupEventType",
                         event_period: str, person_name: str = None, event_detail: str = None,
                         message: str = None, score: int = 0,
                         status: "CatchupTouchStatus" = CatchupTouchStatus.PENDING) -> Optional[int]:
    """Record a drafted catch-up touch. Returns None when the milestone is already in the ledger —
    the (user, profile, event_type, event_period) unique key is the dedup guarantee, so a moment that
    stays in the feed for days can never be messaged twice.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO catchup_touches (user_id, profile_url, person_name, event_type, "
                "event_detail, event_period, score, message, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, profile_url, person_name, str(event_type), event_detail, event_period,
                 int(score), message, str(status)))
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_error("Could not insert catchup touch", exc=err, user_id=user_id)
        return None
def get_catchup_touch(touch_id: int) -> Optional[dict]:
    """One catch-up touch row, or None when it does not exist or the read failed."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT {', '.join(_CATCHUP_COLS)} FROM catchup_touches WHERE id = %s",
                           (touch_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get catchup touch {touch_id}", exc=err)
        return None
def has_catchup_touch(user_id: int, profile_url: str, event_type: "CatchupEventType",
                      event_period: str) -> bool:
    """True if this exact milestone has already been drafted/sent — checked before drafting so we
    don't spend an LLM call on a moment the unique key would reject anyway.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM catchup_touches WHERE user_id = %s AND profile_url = %s "
                "AND event_type = %s AND event_period = %s LIMIT 1",
                (user_id, profile_url, str(event_type), event_period))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error("Could not check catchup touch", exc=err, user_id=user_id)
        return False
_CATCHUP_SORT_COLUMNS = {"score": "score", "date": "created_at"}


def get_catchup_touches(user_id: int, status_filter: str = None, event_type_filter: str = None,
                        page: int = 1, page_size: int = 25, sort_order: str = "desc",
                        sort_by: str = "score", start_date: Optional[datetime] = None,
                        end_date: Optional[datetime] = None) -> dict:
    """Paginated list of a user's catch-up touches (mirrors get_connection_requests).

    `sort_by` picks the PRIMARY key through `_CATCHUP_SORT_COLUMNS` (anything unknown falls back to
    `score`) because the column name is interpolated rather than parameterized, and `sort_order`
    directs it; the other column is the stable tiebreak. `start_date`/`end_date` bound `created_at`
    — the date the queue shows and the reporter of issue #1464 filters on — coerced to naive UTC.

    A read error returns an EMPTY page, never a partial one.
    """
    order = "ASC" if str(sort_order).lower() == "asc" else "DESC"
    sort_col = _CATCHUP_SORT_COLUMNS.get(str(sort_by or "").lower(), "score")
    tiebreak = "created_at DESC" if sort_col == "score" else "score DESC"
    where = "WHERE user_id = %s"
    params: list = [user_id]
    if status_filter:
        where += " AND status = %s"
        params.append(status_filter)
    if event_type_filter:
        where += " AND event_type = %s"
        params.append(event_type_filter)
    if start_date is not None:
        where += " AND created_at >= %s"
        params.append(to_naive_utc(start_date))
    if end_date is not None:
        where += " AND created_at <= %s"
        params.append(to_naive_utc(end_date))
    offset = max(0, (max(1, page) - 1) * page_size)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT COUNT(*) AS c FROM catchup_touches {where}", tuple(params))
            total = int(cursor.fetchone()["c"])
            cursor.execute(
                f"SELECT {', '.join(_CATCHUP_COLS)} FROM catchup_touches {where} "
                f"ORDER BY {sort_col} {order}, {tiebreak} LIMIT %s OFFSET %s",
                tuple(params + [page_size, offset]))
            rows = cursor.fetchall()
            for r in rows:
                for k in ("created_at", "updated_at"):
                    if isinstance(r.get(k), datetime):
                        r[k] = r[k].isoformat()
            return {"touches": rows, "total": total, "page": page, "page_size": page_size}
    except mysql.connector.Error as err:
        log_error("Could not list catchup touches", exc=err, user_id=user_id)
        return {"touches": [], "total": 0, "page": page, "page_size": page_size}
def get_approved_catchup_touches() -> list:
    """Approved touches waiting to be sent, highest-scoring first so the best moments go out within
    the daily cap. Returns (id, user_id) tuples; the cap is enforced by the scanner/send task.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT id, user_id FROM catchup_touches WHERE status = 'approved' "
                "ORDER BY score DESC, created_at ASC")
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get approved catchup touches", exc=err)
        return []
def count_pending_catchup_touches() -> int:
    """Drafted touches still waiting on human approval, fleet-wide. The send drip reports this so a
    queue that exists but was never approved cannot read as an empty one (issue #792) — the drafts
    land 'pending' unless the user opted into catchup_touch_mode='auto_approve'.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM catchup_touches WHERE status = 'pending'")
            r = cursor.fetchone()
            return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        # ERROR, not myprint (which logs at INFO): a failed count returns 0, which reads on the beat
        # as `nothing_to_send` — the exact silence issue #792 exists to remove. It has to be visible.
        log_error("Could not count pending catchup touches", exc=err)
        return 0
def get_orphaned_catchup_touches(lookback_hours: int = 2) -> list:
    """Touches stuck in 'sending' whose send task was lost (e.g. Celery queue purged on restart).
    Mirrors get_orphaned_connection_requests. Returns (id, user_id) tuples.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT id, user_id FROM catchup_touches WHERE status = 'sending' "
                "AND updated_at <= %s ORDER BY updated_at ASC", (cutoff,))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get orphaned catchup touches", exc=err)
        return []
def count_catchup_touches_sent_today(user_id: int) -> int:
    """Catch-up DMs sent today (UTC) — the per-day cap is on top of the overall DM cap."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM catchup_touches WHERE user_id = %s AND status = 'sent' "
                "AND updated_at >= CURDATE()", (user_id,))
            r = cursor.fetchone()
            return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        log_error("Could not count catchup touches", exc=err, user_id=user_id)
        return 0
def update_catchup_touch_status(touch_id: int, status: "CatchupTouchStatus") -> bool:
    """Move a catch-up touch's status.

    When the new status is `sent`, `last_sent_at` is stamped as well so the per-contact cooldown
    guard can see real delivery history. True whenever the UPDATE ran, matched or not.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if str(status) == str(CatchupTouchStatus.SENT):
                cursor.execute(
                    "UPDATE catchup_touches SET status = %s, last_sent_at = NOW() WHERE id = %s",
                    (str(status), touch_id))
            else:
                cursor.execute("UPDATE catchup_touches SET status = %s WHERE id = %s",
                               (str(status), touch_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not update catchup touch {touch_id} status", exc=err)
        return False
def update_catchup_touch(touch_id: int, message: str = None, person_name: str = None,
                         status: "CatchupTouchStatus" = None) -> bool:
    """Patch only the fields that were supplied; False when none were.

    Omitted arguments are left alone rather than nulled. An explicit `status='sent'` also stamps
    `last_sent_at` so the per-contact cooldown sees the real delivery time. True means the UPDATE ran,
    not that a row matched.
    """
    fields, params = [], []
    for col, val in (("message", message), ("person_name", person_name)):
        if val is not None:
            fields.append(f"{col} = %s")
            params.append(val)
    if status is not None:
        fields.append("status = %s")
        params.append(str(status))
        if str(status) == str(CatchupTouchStatus.SENT):
            fields.append("last_sent_at = NOW()")
    if not fields:
        return False
    params.append(touch_id)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE catchup_touches SET {', '.join(fields)} WHERE id = %s", tuple(params))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not update catchup touch {touch_id}", exc=err)
        return False
def last_catchup_sent_at(user_id: int, profile_url: str) -> Optional[datetime]:
    """The most recent `last_sent_at` for this contact, or None when no catch-up has been sent.

    Returns None on DB error so a broken read can't block the lane.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT MAX(last_sent_at) FROM catchup_touches WHERE user_id = %s AND profile_url = %s "
                "AND status = 'sent'", (user_id, profile_url))
            r = cursor.fetchone()
            return r[0] if r and r[0] else None
    except mysql.connector.Error as err:
        log_error("Could not read last catch-up sent at", exc=err, user_id=user_id)
        return None
def count_catchup_touches_for_contact_in_window(user_id: int, profile_url: str,
                                                days: int) -> int:
    """How many catch-up DMs this user has sent to this contact in the last `days` days.

    A non-positive `days` returns 0; a DB error returns 0 so it never caps by itself.
    """
    days = max(0, int(days or 0))
    if days <= 0:
        return 0
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM catchup_touches WHERE user_id = %s AND profile_url = %s "
                "AND status = 'sent' AND last_sent_at >= DATE_SUB(NOW(), INTERVAL %s DAY)",
                (user_id, profile_url, days))
            r = cursor.fetchone()
            return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        log_error(f"Could not count catch-up touches for contact (user_id {user_id})", exc=err)
        return 0
_LEAD_MAGNET_DEFAULTS: dict = {"enabled": False, "keyword": None, "message": None}
def get_lead_magnet_settings(user_id: int) -> dict:
    """The user's lead-magnet keyword settings, always as a complete dict.

    A missing row AND a failed read both return a copy of `_LEAD_MAGNET_DEFAULTS` (disabled), so the
    "is this mechanic on?" check fails CLOSED and no caller needs a None branch.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT enabled, keyword, message FROM lead_magnet_settings WHERE user_id=%s", (user_id,))
            row = cursor.fetchone()
            if row is None:
                return dict(_LEAD_MAGNET_DEFAULTS)
            row["enabled"] = bool(row.get("enabled"))
            return row
    except mysql.connector.Error as err:
        log_error("Could not get lead magnet", exc=err, user_id=user_id)
        return dict(_LEAD_MAGNET_DEFAULTS)
def update_lead_magnet_settings(user_id: int, settings: dict) -> bool:
    """Upsert the lead-magnet settings for a user.

    True whenever the statement ran.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO lead_magnet_settings (user_id, enabled, keyword, message) VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE enabled=VALUES(enabled), keyword=VALUES(keyword), message=VALUES(message)",
                (user_id, 1 if settings.get("enabled") else 0, settings.get("keyword"), settings.get("message")))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update lead magnet", exc=err, user_id=user_id)
        return False
def has_received_lead_magnet(user_id: int, recipient_profile: str) -> bool:
    """Has this person already been sent this user's lead magnet?

    Fails CLOSED, and that is the whole point: a read error returns True, so a database blip skips the
    send rather than DMing someone the same asset a second time.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT 1 FROM lead_magnet_sent WHERE user_id=%s AND recipient_profile=%s LIMIT 1",
                           (user_id, recipient_profile))
            return cursor.fetchone() is not None
    except mysql.connector.Error:
        return True   # fail safe: assume sent (don't double-DM on error)
def record_lead_magnet_sent(user_id: int, recipient_profile: str, post_id: int = None) -> bool:
    """Write the claim that this person received the lead magnet — the row `has_received_lead_magnet` reads.

    INSERT IGNORE, so a repeat is a no-op rather than an error; True only means the statement ran.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO lead_magnet_sent (user_id, recipient_profile, post_id) VALUES (%s,%s,%s)",
                (user_id, recipient_profile, post_id))
            return True
    except mysql.connector.Error as err:
        log_error("Could not record lead magnet sent", exc=err, user_id=user_id)
        return False
# --- inbound hot-lead signals (issue #483) ----------------------------------------------------
_LEAD_SIGNAL_COLS = ("id", "user_id", "source", "channel", "person_name", "person_profile_url",
                     "thread_key", "snippet", "score", "matched_signals", "post_id", "context_url",
                     "draft_response", "status", "created_at", "updated_at")
def insert_lead_signal(user_id: int, source: "LeadSignalSource", thread_key: str,
                       person_name: str = None, person_profile_url: str = None,
                       snippet: str = None, score: int = 0, matched_signals: str = None,
                       post_id: int = None, context_url: str = None, draft_response: str = None,
                       channel: "LeadSignalChannel" = LeadSignalChannel.REPLY) -> Optional[int]:
    """Record a detected buying signal. Deduped by UNIQUE(user_id, thread_key) — a second detection
    on the same conversation returns None instead of re-flagging it (and never overwrites an
    operator's edited draft or their dismissal).
    """
    if not thread_key:
        return None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO lead_signals (user_id, source, channel, person_name, "
                "person_profile_url, thread_key, snippet, score, matched_signals, post_id, context_url, "
                "draft_response) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, str(source), str(channel), person_name, person_profile_url,
                 str(thread_key)[:255], snippet, max(0, min(255, int(score or 0))), matched_signals,
                 post_id, context_url, draft_response))
            return cursor.lastrowid or None
    except mysql.connector.Error as err:
        log_error("Could not insert lead signal", exc=err, user_id=user_id)
        return None
def has_lead_signal(user_id: int, thread_key: str) -> bool:
    """True if this conversation was already flagged. Checked BEFORE the expensive draft generation
    so a re-scan of the same thread costs nothing. Fails safe to True (skip) on error.
    """
    if not thread_key:
        return True
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT 1 FROM lead_signals WHERE user_id=%s AND thread_key=%s LIMIT 1",
                           (user_id, str(thread_key)[:255]))
            return cursor.fetchone() is not None
    except mysql.connector.Error:
        return True
def get_lead_signal(signal_id: int) -> Optional[dict]:
    """One inbound lead-signal row, or None when it does not exist or the read failed."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT {', '.join(_LEAD_SIGNAL_COLS)} FROM lead_signals WHERE id=%s", (signal_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get lead signal {signal_id}", exc=err)
        return None
def get_lead_signals(user_id: int, status_filter: str = None, page: int = 1, page_size: int = 25,
                     sort_order: str = "desc") -> dict:
    """Paginated leads inbox for a user (mirrors the scheduled-DM/outreach list response). Hottest
    first within a timestamp: newest signals matter most — a slow response kills an inbound lead.
    """
    order = "ASC" if str(sort_order).lower() == "asc" else "DESC"
    where = "WHERE user_id = %s"
    params: list = [user_id]
    if status_filter:
        where += " AND status = %s"
        params.append(status_filter)
    offset = max(0, (max(1, page) - 1) * page_size)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT COUNT(*) AS c FROM lead_signals {where}", tuple(params))
            total = int(cursor.fetchone()["c"])
            cursor.execute(
                f"SELECT {', '.join(_LEAD_SIGNAL_COLS)} FROM lead_signals {where} "
                f"ORDER BY created_at {order}, score DESC LIMIT %s OFFSET %s",
                tuple(params + [page_size, offset]))
            rows = cursor.fetchall()
            for r in rows:
                for k in ("created_at", "updated_at"):
                    if isinstance(r.get(k), datetime):
                        r[k] = r[k].isoformat()
            return {"signals": rows, "total": total, "page": page, "page_size": page_size}
    except mysql.connector.Error as err:
        log_error("Could not list lead signals", exc=err, user_id=user_id)
        return {"signals": [], "total": 0, "page": page, "page_size": page_size}
def update_lead_signal(signal_id: int, draft_response: str = None,
                       status: "LeadSignalStatus" = None,
                       channel: "LeadSignalChannel" = None) -> bool:
    """Edit a lead's draft and/or move its status. Only the supplied fields change."""
    fields, params = [], []
    if draft_response is not None:
        fields.append("draft_response = %s")
        params.append(draft_response)
    for col, val in (("status", status), ("channel", channel)):
        if val is not None:
            fields.append(f"{col} = %s")
            params.append(str(val))
    if not fields:
        return False
    params.append(signal_id)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE lead_signals SET {', '.join(fields)} WHERE id = %s", tuple(params))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not update lead signal {signal_id}", exc=err)
        return False
def count_new_lead_signals(user_id: int) -> int:
    """Unactioned hot leads — the inbox badge count."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM lead_signals WHERE user_id=%s AND status='new'", (user_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] else 0
    except mysql.connector.Error:
        return 0
_LEAD_COLS = ("id", "user_id", "person_key", "person_name", "person_profile_url", "score",
              "icp_score", "engagement_score", "stage", "signals", "signal_count", "reasons",
              "next_action", "first_signal_at", "last_signal_at", "manual_stage", "notes",
              "dismissed", "created_at", "updated_at")
def reset_lead_scores(user_id: int) -> bool:
    """Zero every computed score before a rebuild so someone who went quiet actually decays out of
    'hot' instead of keeping a stale score forever. next_action is cleared with the rest: a lead
    with no fresh activity gets no upsert, and a leftover 'reach out today' on someone who decayed
    to cold is worse than no recommendation. Operator columns (manual_stage, notes, dismissed) are
    untouched.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE leads SET score=0, engagement_score=0, stage='cold', signal_count=0, "
                "signals=NULL, reasons=NULL, next_action=NULL WHERE user_id=%s", (user_id,))
            return True
    except mysql.connector.Error as err:
        log_error("Could not reset lead scores", exc=err, user_id=user_id)
        return False
def upsert_lead(user_id: int, person_key: str, person_name: str = None,
                person_profile_url: str = None, score: int = 0, icp_score: int = 0,
                engagement_score: int = 0, stage: "LeadStage" = LeadStage.COLD,
                signals: str = None, signal_count: int = 0, reasons: str = None,
                next_action: str = None, first_signal_at: "datetime" = None,
                last_signal_at: "datetime" = None) -> bool:
    """Write one scored lead. Idempotent on (user_id, person_key) — the nightly rebuild re-runs
    freely. Only COMPUTED columns are updated: an operator's manual_stage, notes, and dismissal
    survive every re-score.
    """
    if not person_key:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO leads (user_id, person_key, person_name, person_profile_url, score, "
                "icp_score, engagement_score, stage, signals, signal_count, reasons, next_action, "
                "first_signal_at, last_signal_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE "
                "person_name=COALESCE(VALUES(person_name), person_name), "
                "person_profile_url=COALESCE(VALUES(person_profile_url), person_profile_url), "
                "score=VALUES(score), icp_score=VALUES(icp_score), "
                "engagement_score=VALUES(engagement_score), stage=VALUES(stage), "
                "signals=VALUES(signals), signal_count=VALUES(signal_count), "
                "reasons=VALUES(reasons), next_action=VALUES(next_action), "
                "first_signal_at=LEAST(COALESCE(first_signal_at, VALUES(first_signal_at)), "
                "                      COALESCE(VALUES(first_signal_at), first_signal_at)), "
                "last_signal_at=GREATEST(COALESCE(last_signal_at, VALUES(last_signal_at)), "
                "                        COALESCE(VALUES(last_signal_at), last_signal_at))",
                (user_id, str(person_key)[:255], (person_name or None), (person_profile_url or None),
                 max(0, min(255, int(score or 0))), max(0, min(255, int(icp_score or 0))),
                 max(0, min(255, int(engagement_score or 0))), str(stage),
                 (signals or None), max(0, int(signal_count or 0)), (reasons or None),
                 (next_action or None), first_signal_at, last_signal_at))
            return True
    except mysql.connector.Error as err:
        log_error("Could not upsert lead", exc=err, user_id=user_id)
        return False
def get_lead(lead_id: int) -> Optional[dict]:
    """One lead row from the pipeline board, or None when it does not exist or the read failed."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT {', '.join(_LEAD_COLS)} FROM leads WHERE id=%s", (lead_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get lead {lead_id}", exc=err)
        return None
def get_leads(user_id: int, stage_filter: str = None, include_dismissed: bool = False,
              page: int = 1, page_size: int = 100) -> dict:
    """The pipeline board: scored leads hottest first. A manual_stage overrides the computed stage
    for filtering, so moving someone by hand actually moves them on the board.
    """
    where = "WHERE user_id = %s"
    params: list = [user_id]
    if not include_dismissed:
        where += " AND dismissed = 0"
    if stage_filter:
        where += " AND COALESCE(manual_stage, stage) = %s"
        params.append(stage_filter)
    offset = max(0, (max(1, page) - 1) * page_size)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT COUNT(*) AS c FROM leads {where}", tuple(params))
            total = int(cursor.fetchone()["c"])
            cursor.execute(
                f"SELECT {', '.join(_LEAD_COLS)} FROM leads {where} "
                "ORDER BY score DESC, last_signal_at DESC, id DESC LIMIT %s OFFSET %s",
                tuple(params + [page_size, offset]))
            rows = cursor.fetchall()
            for r in rows:
                for k in ("first_signal_at", "last_signal_at", "created_at", "updated_at"):
                    if isinstance(r.get(k), datetime):
                        r[k] = r[k].isoformat()
                r["dismissed"] = bool(r.get("dismissed"))
            return {"leads": rows, "total": total, "page": page, "page_size": page_size}
    except mysql.connector.Error as err:
        log_error("Could not list leads", exc=err, user_id=user_id)
        return {"leads": [], "total": 0, "page": page, "page_size": page_size}
def get_hot_leads(user_id: int, limit: int = 10) -> list:
    """Today's hot list — the leads worth acting on now, with the WHY and the suggested action."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {', '.join(_LEAD_COLS)} FROM leads WHERE user_id=%s AND dismissed=0 "
                "AND COALESCE(manual_stage, stage) IN ('hot','in_conversation','opportunity') "
                "ORDER BY score DESC, last_signal_at DESC LIMIT %s", (user_id, max(1, int(limit))))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get hot leads", exc=err, user_id=user_id)
        return []
def count_hot_leads(user_id: int) -> int:
    """Board badge: how many leads are hot or further along."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM leads WHERE user_id=%s AND dismissed=0 "
                "AND COALESCE(manual_stage, stage) IN ('hot','in_conversation','opportunity')",
                (user_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] else 0
    except mysql.connector.Error:
        return 0
def update_lead(lead_id: int, notes: str = None, manual_stage: "LeadStage" = None,
                dismissed: bool = None) -> bool:
    """Operator edits only — the nightly rebuild never writes these columns, so a correction sticks.
    Passing manual_stage='' clears the override and hands the lead back to the scorer.
    """
    fields, params = [], []
    if notes is not None:
        fields.append("notes = %s")
        params.append(notes[:512] or None)
    if manual_stage is not None:
        fields.append("manual_stage = %s")
        params.append(str(manual_stage) or None)
    if dismissed is not None:
        fields.append("dismissed = %s")
        params.append(1 if dismissed else 0)
    if not fields:
        return False
    params.append(lead_id)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE leads SET {', '.join(fields)} WHERE id = %s", tuple(params))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not update lead {lead_id}", exc=err)
        return False
# Default DM templates = today's hard-coded strings, so behaviour is unchanged until a user
# customizes. {first_name},{headline},{blog_url} are filled at send time.
_DM_DEFAULT_TEMPLATES = {
    "connection_accepted": "Hi {first_name}, I appreciate you connecting with me on LinkedIn. "
                           "I look forward to learning more about you and your work.",
    "recommendation_received": "Hi {first_name}, thank you so much for the kind recommendation on LinkedIn! "
                               "I really appreciate you taking the time to share your experience working with me. "
                               "I hope we have the opportunity to collaborate again in the future.",
    # What actually fires this is a MENTION — somebody put this user's name in their own post or
    # comment (#968). The old wording thanked them for a project neither party may have worked on,
    # so it says what happened. DEFAULT only: a user who customized their template keeps theirs.
    "collaboration": "Hi {first_name}, thanks for the mention — genuinely appreciated. "
                     "What are you working on at the moment?",
    "profile_viewer": "Hi {first_name}, I noticed you viewed my LinkedIn profile and wanted to reach out. "
                      "I share insights on {headline} and thought there might be synergy between our work. "
                      "Would love to connect more directly — feel free to share what you're working on!",
    "manual": "Hi {first_name}, thanks for connecting!",
    "funnel": "Hi {first_name}, really glad we connected. I've enjoyed following the work you're "
              "sharing and would love to swap notes sometime. What are you focused on this quarter?",
    # Direction for the next message after a lead REPLIES (issue #485). The nurture draft is written
    # against what they actually said — this template sets the intent, not the wording.
    "nurture": "Thanks for coming back to me, {first_name} — that's helpful context. Happy to share "
               "what I've seen work on {headline}; would a short call be useful, or shall I just "
               "send over the one thing that would help most?",
    # Catch-up milestone congratulations (issue #482). The congrats IS the message — no pitch. Each
    # references the specific moment ({event_detail}) so it can never read as a generic "Congrats!".
    "job_change": "Hi {first_name}, congratulations on the new role — {event_detail}. That's a great "
                  "step. What drew you to it?",
    "promotion": "Hi {first_name}, congratulations on the promotion — {event_detail}. Well earned. "
                 "What are you most looking forward to in the new scope?",
    "work_anniversary": "Hi {first_name}, congratulations on the work anniversary — {event_detail}. "
                        "That's a real milestone. What's changed most for you over that stretch?",
    "birthday": "Hi {first_name}, happy birthday! Hope you get to step away from the inbox for a bit "
                "today.",
    "education": "Hi {first_name}, congratulations — {event_detail}. That's a big commitment to see "
                 "through. What's next now that it's done?",
    "in_the_news": "Hi {first_name}, saw you in the news — {event_detail}. Congratulations, that's "
                   "great visibility. How did it come about?",
}
def get_dm_template(user_id: int, event_type: str, step: int = 0) -> Optional[dict]:
    """Return {template_text, delay_hours, step} for (user, event, step). Falls back to the
    code default for step 0; None for higher steps that aren't configured.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT template_text, delay_hours, step FROM dm_templates "
                "WHERE user_id=%s AND event_type=%s AND step=%s AND is_active=1",
                (user_id, str(event_type), step))
            row = cursor.fetchone()
            if row:
                return row
    except mysql.connector.Error as err:
        log_error("Could not get dm template", exc=err, user_id=user_id)
    if step == 0 and event_type in _DM_DEFAULT_TEMPLATES:
        return {"template_text": _DM_DEFAULT_TEMPLATES[event_type], "delay_hours": 0, "step": 0}
    return None
def get_dm_templates(user_id: int) -> Optional[list]:
    """Every DM template the user has, ordered by event type then step, with `is_active` as a real bool.

    **None on a read error, never []** — this read FAILS CLOSED because it is the editor's only
    picture of the set, and `upsert_dm_templates` now deletes whatever the next save leaves out
    (issue #1575). An empty list served for a transient DB fault renders as "you have no templates",
    and the first field the user then edits posts a one-row set that destroys every other ladder they
    had. [] therefore has to mean the table really is empty; the caller answers None with a 503.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT event_type, step, delay_hours, template_text, is_active "
                "FROM dm_templates WHERE user_id=%s ORDER BY event_type, step", (user_id,))
            rows = cursor.fetchall() or []
            for r in rows:
                r["is_active"] = bool(r.get("is_active"))
            return rows
    except mysql.connector.Error as err:
        log_error("Could not list dm templates", exc=err, user_id=user_id)
        return None
def upsert_dm_templates(user_id: int, templates: list) -> bool:
    """Replace the user's WHOLE template set: upsert what was posted, DELETE the rest (issue #1575).

    The posted set is authoritative, which is what `PUT /user/dm-templates` has always documented and
    what the Settings editor assumes: it posts every non-blank row it renders, so a step the user
    removed, and a template they blanked to fall back to the built-in default, are both absent from
    the payload. Upserting alone left those rows in `dm_templates` still `is_active=1`, so the
    follow-up sequencer kept rendering and SENDING a step the user had deleted — a DM nobody had
    configured, arriving out of sequence with the message before it.

    An empty list therefore clears the user's ladders and returns them to the code defaults. The
    delete runs in the same transaction as the upserts, so a failure leaves the set as it was.
    """
    try:
        with db_cursor(commit=True) as cursor:
            keep: list = []
            for t in templates:
                event_type, step = str(t.get("event_type")), int(t.get("step", 0))
                cursor.execute(
                    "INSERT INTO dm_templates (user_id, event_type, step, delay_hours, template_text, is_active) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                    "delay_hours=VALUES(delay_hours), template_text=VALUES(template_text), is_active=VALUES(is_active)",
                    (user_id, event_type, step, int(t.get("delay_hours", 0)),
                     t.get("template_text", ""), 1 if t.get("is_active", True) else 0))
                keep.append((event_type, step))
            if keep:
                placeholders = ",".join(["(%s,%s)"] * len(keep))
                cursor.execute(
                    f"DELETE FROM dm_templates WHERE user_id=%s AND (event_type, step) NOT IN ({placeholders})",
                    (user_id, *[value for pair in keep for value in pair]))
            else:
                cursor.execute("DELETE FROM dm_templates WHERE user_id=%s", (user_id,))
            return True
    except mysql.connector.Error as err:
        log_error("Could not upsert dm templates", exc=err, user_id=user_id)
        return False
def claim_appreciation_touch(user_id: int, profile_url: str, event_type: str,
                             person_name: str = None) -> bool:
    """Claim the right to send ONE appreciation DM to this person for this event (issue #968).

    True only when THIS call inserted the row. The recommendation and collaboration sources read a
    standing LinkedIn surface (a recommendation never leaves the profile, a mention sits in the
    notifications feed for weeks) and the appreciation beat re-queues itself every ~60s, so the
    unique key is the only thing between "thanked once" and "thanked every minute".

    Claim BEFORE dispatch, never after: a thank-you that fails to send is recoverable by a human,
    one sent twenty times is not. A DB error therefore returns False — no claim, no DM.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO appreciation_touches (user_id, profile_url, person_name, event_type) "
                "VALUES (%s,%s,%s,%s)",
                (user_id, profile_url, person_name, str(event_type)))
            return cursor.rowcount == 1
    except mysql.connector.Error as err:
        log_error("Could not claim appreciation touch", exc=err, user_id=user_id)
        return False
def claim_catchup_send_attempt(touch_id: int, user_id: int, profile_url: str,
                             event_type: "CatchupEventType", event_period: str) -> bool:
    """Claim the right to send ONE catch-up DM for this milestone (issue #1078).

    True only when THIS call inserted the `catchup_send_attempts` row. The unique key is on the
    milestone identity (user, profile_url, event_type, event_period), so a retry, a worker restart,
    or a lost status update can never produce a second send. A failed claim means either the touch was
    already sent or the ledger is unreadable — either way, do not send.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO catchup_send_attempts (touch_id, user_id, profile_url, event_type, "
                "event_period) VALUES (%s,%s,%s,%s,%s)",
                (touch_id, user_id, profile_url, str(event_type), event_period))
            return cursor.rowcount == 1
    except mysql.connector.Error as err:
        log_error(f"Could not claim catch-up send attempt for touch_id {touch_id}", exc=err)
        return False
def release_catchup_send_attempt(user_id: int, profile_url: str, event_type: "CatchupEventType",
                                 event_period: str) -> bool:
    """Give the claim back when NOTHING was sent (issue #1078).

    Only call this where the send provably never reached LinkedIn — the 429 breaker refusing before a
    composer was ever opened. A send whose outcome is unknown must KEEP its claim: the whole point of
    the ledger is that an ambiguous attempt is treated as sent. Without this the throttle deferral,
    which puts the touch back to `approved` for the next scan, would leave a claim no later attempt
    could ever beat — so the retry would mark the touch `sent` having sent nothing.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM catchup_send_attempts WHERE user_id = %s AND profile_url = %s "
                "AND event_type = %s AND event_period = %s",
                (user_id, profile_url, str(event_type), event_period))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not release catch-up send claim", exc=err, user_id=user_id)
        return False
def has_appreciation_touch(user_id: int, profile_url: str, event_type: str) -> bool:
    """Whether this person was already thanked for this event. Read-only — the CLAIM is what makes
    the decision (see claim_appreciation_touch); this exists so a scraper can skip parsing work and
    so the live probe can report what production would do without writing a row.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM appreciation_touches WHERE user_id = %s AND profile_url = %s "
                "AND event_type = %s LIMIT 1", (user_id, profile_url, str(event_type)))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error("Could not check appreciation touch", exc=err, user_id=user_id)
        return False
def enqueue_followup(user_id: int, profile_url: str, first_name: str, event_type: str,
                     next_step: int, due_at: Optional[datetime]) -> bool:
    """Schedule a follow-up DM touch.

    `due_at` goes through `to_naive_utc`, the one storage-side conversion
    (docs/timezone-contract.md): `dm_followups.due_at` holds NAIVE UTC, and `get_due_followups`
    normalizes its `now` the same way, so reader and writer cannot drift onto different clocks.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO dm_followups (user_id, profile_url, first_name, event_type, next_step, due_at, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_id, profile_url, first_name, str(event_type), next_step, to_naive_utc(due_at),
                 str(FollowupStatus.PENDING)))
            return True
    except mysql.connector.Error as err:
        log_error("Could not enqueue followup", exc=err, user_id=user_id)
        return False
def get_due_followups(now: Optional[datetime]) -> list:
    """Pending follow-ups whose due_at has passed.

    `now` is normalized through `to_naive_utc` — the same conversion every writer of
    `dm_followups.due_at` applies — so an aware caller and a naive-UTC one compare identically.
    Without it an aware local `now` would be serialized as its local wall clock and rows would come
    due hours early or late (docs/timezone-contract.md).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, profile_url, first_name, event_type, next_step, unreadable_reads "
                "FROM dm_followups WHERE status=%s AND due_at <= %s ORDER BY due_at",
                (str(FollowupStatus.PENDING), to_naive_utc(now)))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get due followups", exc=err)
        return []
def mark_followup(followup_id: int, status: str) -> bool:
    """Move one follow-up row to `status`.

    True whenever the UPDATE ran, matched or not.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE dm_followups SET status=%s WHERE id=%s", (str(status), followup_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not mark followup {followup_id}", exc=err)
        return False
def record_unreadable_read(followup_id: int, due_at: Optional[datetime] = None) -> bool:
    """Count one UNKNOWN reply-detection read against this row (#1815).

    When `due_at` is given, also pushes the row's next read out to it (normalized through
    `to_naive_utc`, the same conversion `enqueue_followup` and `get_due_followups` use). The row
    stays `FollowupStatus.PENDING` either way — #731's fail-closed UNKNOWN skip is a READ failure,
    not a send failure, so it must never drop off the retry ladder the way
    `mark_followup(..., 'failed')` would.

    Returns True only when a pending row was ACTUALLY counted. Unlike `mark_followup`, "the UPDATE
    ran" is not good enough here: the caller decides whether to back off from the count it read a
    moment ago, so a zero-match (the row moved to a terminal status between the read and this write)
    has to be distinguishable — otherwise the caller warns about a backoff that never happened.
    Matching is safe to read off `rowcount` because the counter always changes when the row matches.

    The read-then-increment is deliberately not atomic. `process_user_followups` is a `QueueOnce`
    task keyed on `user_id`, so exactly one worker ever holds a given user's rows; the SQL-side
    `+ 1` is what keeps the stored count right even if that ever stopped being true, and the worst
    a lost race could do is delay one backoff step by one beat.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if due_at is not None:
                cursor.execute(
                    "UPDATE dm_followups SET unreadable_reads = unreadable_reads + 1, due_at = %s "
                    "WHERE id = %s AND status = %s",
                    (to_naive_utc(due_at), followup_id, str(FollowupStatus.PENDING)))
            else:
                cursor.execute(
                    "UPDATE dm_followups SET unreadable_reads = unreadable_reads + 1 "
                    "WHERE id = %s AND status = %s", (followup_id, str(FollowupStatus.PENDING)))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not record unreadable read for followup {followup_id}", exc=err)
        return False
def reset_unreadable_reads(followup_id: int) -> bool:
    """Clear this row's consecutive-UNKNOWN streak (#1815).

    Called explicitly on any outcome `check_dm_replied` ACTUALLY read, so the counter only ever
    describes an unbroken run of unreadable reads. Deliberately not folded into `mark_followup`:
    that is a generic status setter, and hiding a counter reset inside it would mean any future
    caller — an error path, a reschedule — silently wiped a legitimate streak.

    True whenever the UPDATE ran, matched or not: a row that has already moved on has nothing to
    reset, which is not a failure.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE dm_followups SET unreadable_reads = 0 WHERE id = %s", (followup_id,))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not reset unreadable reads for followup {followup_id}", exc=err)
        return False
def stop_followups_for_profile(user_id: int, profile_url: str) -> int:
    """Stop all pending follow-ups to a profile (e.g. once they've replied). Returns count."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE dm_followups SET status='stopped' "
                           "WHERE user_id=%s AND profile_url=%s AND status='pending'",
                           (user_id, profile_url))
            return cursor.rowcount
    except mysql.connector.Error as err:
        log_error("Could not stop followups", exc=err, user_id=user_id)
        return 0
def get_most_recent_dm_thread_target(user_id: int) -> "dict | None":
    """The profile we most recently DM'd, for a message-thread probe target (issue #1770).

    Any `dm_followups` row is evidence a thread was opened — including a `stopped` one (they
    replied, or the thread went cold), so the status is deliberately not filtered: a thread that
    ended does not stop existing. `None` when the account has never sent a follow-up-tracked DM.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT profile_url, first_name FROM dm_followups WHERE user_id=%s "
                "ORDER BY id DESC LIMIT 1", (user_id,))
            row = cursor.fetchone()
            return {"profile_url": row["profile_url"], "first_name": row.get("first_name") or ""} \
                if row and row.get("profile_url") else None
    except mysql.connector.Error as err:
        log_error("Could not get most recent DM thread target", exc=err, user_id=user_id)
        return None


# Why a proactive invite was abandoned before it was attempted (issue #623). Stored as the request's
# failure_reason so the Connections review UI explains a FAILED row instead of just colouring it red.
ALREADY_CONNECTED_MESSAGE = "Already connected (1st-degree) — no invite to send"
# The profile offered no Connect affordance at all — neither the direct button nor one inside the
# More-actions menu (issue #571). Usually an invite is already pending or LinkedIn only offers
# Follow/Message on that profile; either way there is nothing to send, so the invite stops here
# rather than falling through to the note/send steps that can only fail after it.
NO_CONNECT_BUTTON_MESSAGE = "No Connect option on this profile (invite may already be pending)"
# The profile offered Follow and NOTHING connect-shaped at all — no custom-invite anchor naming the
# target, no Connect button naming them, no pending badge (issue #1813). That is out of network: a
# fact about the TARGET, not a miss, so it is terminal on the first read and it feeds neither the
# invite-dialog miss streak nor the 6-hour hold those misses arm. Measured in production on
# `burkegriffin`, `scott-stephenson-` and `aditabraham`, each costing a ~90 s Chrome session per
# cycle and braking the lane for the reachable targets behind them. The #979 ladder already has a
# follow rung for exactly these people, which is why this invents no new state.
FOLLOW_ONLY_MESSAGE = "This profile offers Follow only — out of network, so there is no invite to send"
# The Connect dialog opened but neither Send affordance could be clicked, so nothing went out
# (issue #573). Unlike a missing note this does NOT degrade gracefully — the invite is lost — which
# is why it stays an error and gets its own reason on the request row.
INVITE_NOT_SENT_MESSAGE = "Connect dialog opened but the invitation could not be sent"
# The dialog opened but is the EMAIL-VERIFICATION variant and no email is known for this target —
# Class C (issue #1836). A target fact ("LinkedIn is deliberately gating this person"), never
# selector drift, so this must NOT feed record_invite_dialog_miss or hold_invites the way an
# ordinary miss does, and it goes terminal ('failed') on the FIRST occurrence rather than burning
# attempts — retrying without a new email would spend a ~90s Chrome session learning nothing new.
# `retry` (PUT action=retry, refused for an agent session) is the way back in once one is supplied.
EMAIL_VERIFICATION_REQUIRED_MESSAGE = (
    "Connect dialog requires the recipient's email to verify the connection, which we do not have")
# The wall was the ACCOUNT, not the profile (#1733). Distinct from NO_CONNECT_BUTTON_MESSAGE on
# purpose: a limit reads the same on every profile, so grading it as "no Connect option" sends an
# operator hunting a selector that is fine and lets the scanner re-dispatch the whole queue into it.
# A request that hit one of these is DEFERRED (left `approved`), never `failed` — nothing was
# attempted, so nothing failed.
INVITE_LIMIT_REACHED_MESSAGE = (
    "LinkedIn's invitation limit is reached for this account — invites are held, not failed")
ACCOUNT_RESTRICTED_MESSAGE = "LinkedIn has restricted this account's invitations"
# LinkedIn's hard cap on a connection-request note. Also the point past which a drafted note is
# refined down rather than typed and silently truncated by the textarea's own maxlength.
CONNECT_NOTE_MAX_CHARS = 300
# TERMINAL for AUTOMATION: one shot per target. 'requested' and 'failed' both mean LinkedIn has our
# one invite (or refused it), and re-inviting someone who declined is the pattern that gets accounts
# restricted — the user decides manually from there. 'connected' is the ladder finishing.
ENGAGEMENT_TARGET_CONNECT_TERMINAL = frozenset({ConnectStatus.REQUESTED, ConnectStatus.CONNECTED,
                                                ConnectStatus.FAILED})
# TERMINAL for CLICKING: the roster pass never spends another follow click on a target that reached
# either. 'follow_failed' is still re-READ on later visits (a read-only correction costs nothing and
# a follow that landed but could not be verified must not be retired forever) — see
# `reconcile_roster_follow_state`.
ENGAGEMENT_TARGET_FOLLOW_TERMINAL = frozenset({FollowStatus.FOLLOWING, FollowStatus.FOLLOW_FAILED})
# Consecutive BLOCKED VISITS before the roster card badges the target. Two distinct visits, not two
# cards on one visit: a single page that happened to render only reshares is not evidence that the
# author restricts commenting.
ENGAGEMENT_TARGET_BLOCKED_BADGE_STREAK = 2
_ENGAGEMENT_TARGET_COLS = ("id", "profile_url", "name", "category", "max_comments_per_week",
                           "active", "last_engaged_at", "comments_this_week", "week_start",
                           "source", "comment_blocked_streak", "last_blocked_at", "follow_status",
                           "followed_at", "follow_attempts", "connect_status", "connect_requested_at")
def resolve_weekly_cap(value: Any) -> int:
    """The per-author weekly cap, with an EXPLICIT 0 preserved. 0 is how the SPA pauses an account
    without removing it, so `value or DEFAULT` would read that pause as "unset" and hand the account
    the default two comments a week — the opposite of what the operator asked for.
    """
    if value is None:
        return ENGAGEMENT_TARGET_WEEKLY_DEFAULT
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return ENGAGEMENT_TARGET_WEEKLY_DEFAULT
def engagement_week_start(today: Optional[date] = None) -> date:
    """Monday of the week `today` falls in — the reset boundary for the per-author weekly cap."""
    today = today or datetime.now().date()
    return today - timedelta(days=today.weekday())
def _clean_target_row(row: dict) -> dict:
    """Normalize a roster row: bools as bools, and a STALE weekly counter reported as 0 so a target
    whose cap was spent last week is immediately eligible again without a reset job.
    """
    row["active"] = bool(row.get("active"))
    if row.get("week_start") != engagement_week_start():
        row["comments_this_week"] = 0
    row["comments_this_week"] = int(row.get("comments_this_week") or 0)
    row["max_comments_per_week"] = resolve_weekly_cap(row.get("max_comments_per_week"))
    row["comment_blocked_streak"] = int(row.get("comment_blocked_streak") or 0)
    row["follow_attempts"] = int(row.get("follow_attempts") or 0)
    if row.get("follow_status") not in ENGAGEMENT_TARGET_FOLLOW_STATUSES:
        row["follow_status"] = FollowStatus.UNKNOWN.value
    if row.get("connect_status") not in ENGAGEMENT_TARGET_CONNECT_STATUSES:
        row["connect_status"] = ConnectStatus.UNKNOWN.value
    return row
def get_engagement_targets(user_id: int, active_only: bool = False) -> list:
    """The user's engagement roster, grouped by category and oldest-configured first within each
    category. `comments_this_week` is already week-aware (0 once the stored week_start is not the
    current week).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            sql = (f"SELECT {', '.join(_ENGAGEMENT_TARGET_COLS)} FROM engagement_targets "
                   f"WHERE user_id=%s")
            if active_only:
                sql += " AND active=1"
            sql += " ORDER BY category, id"
            cursor.execute(sql, (user_id,))
            return [_clean_target_row(r) for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not list engagement targets", exc=err, user_id=user_id)
        return []
def record_target_engagement(user_id: int, profile_url: str) -> bool:
    """Count one comment against a roster author's weekly cap and stamp last_engaged_at. The
    counter resets in the same statement when the stored week_start is not the current week, so a
    new week always starts from 1 without a separate reset job.

    A landed comment also clears `comment_blocked_streak` (issue #962): the streak means "we could
    not comment here", and this IS the proof that we could. Folded into this one statement rather
    than a second call site so the two can never disagree.

    For the same reason a pending 'needs_connection' escalation (issue #979) is stood back down to
    'unknown': it means "following did not unlock commenting", and commenting just worked. Only that
    one state is cleared — an invite already sent ('requested'/'failed'/'connected') is a fact about
    LinkedIn that a comment landing does not undo.
    """
    week = engagement_week_start()
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE engagement_targets SET "
                "comments_this_week = IF(week_start = %s, comments_this_week + 1, 1), "
                "week_start = %s, last_engaged_at = NOW(), comment_blocked_streak = 0, "
                f"connect_status = IF(connect_status = '{ConnectStatus.NEEDS_CONNECTION.value}', "
                f"'{ConnectStatus.UNKNOWN.value}', connect_status) "
                "WHERE user_id=%s AND profile_url=%s", (week, week, user_id, str(profile_url or "").strip()))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not record target engagement", exc=err, user_id=user_id)
        return False
def suggest_engagement_targets(user_id: int, limit: int = 20) -> list:
    """Seed candidates for an empty roster: people who recently engaged with the user's OWN posts
    (post_engagers), minus anyone already on the roster. Costs no scraping. Suggested as 'icp' —
    someone reacting to your content is far likelier to be a buyer than a peer — and the operator
    re-categorizes in the editor.
    """
    if limit <= 0:
        return []
    existing = {str(t.get("profile_url") or "").rstrip("/").lower()
                for t in get_engagement_targets(user_id)}
    out = []
    for cand in get_engager_candidates(user_id, days=60):
        url = str(cand.get("person_profile_url") or "").strip()
        if not url or url.rstrip("/").lower() in existing:
            continue
        existing.add(url.rstrip("/").lower())
        out.append({"profile_url": url, "name": cand.get("person_name"), "category": "icp",
                    "max_comments_per_week": ENGAGEMENT_TARGET_WEEKLY_DEFAULT,
                    "active": True, "source": "suggested"})
        if len(out) >= limit:
            break
    return out
# scheduled_dms.source for an auto-drafted DM-nurture reply (issue #485). NULL/absent means an
# operator wrote it by hand, which is what every pre-#485 row is.
SCHEDULED_DM_SOURCE_NURTURE = 'nurture'
# scheduled_dms.source for an approval-gated owned-asset delivery (issue #624) — the lead magnet a
# commenter asked for by keyword. Kept distinct from 'nurture' so each mechanic gets its own daily
# draft cap and its own delivery count; the one-open-draft rule is deliberately SHARED across the
# two (both write to the same thread, so two queued messages would read as spam to one person).
SCHEDULED_DM_SOURCE_ARTIFACT = 'artifact'
# scheduled_dms.source for a profile-viewer DM held for approval (issue #1137) — the cold lane, so
# its draft is the thing the operator sees before it can reach anyone. Its own source value is what
# lets the send path re-start the 'profile_viewer' follow-up ladder at the moment the DM actually
# LANDS, rather than when it was drafted for a decision that may never come.
SCHEDULED_DM_SOURCE_PROFILE_VIEWER = 'profile_viewer'
# connection_requests.source for the same lane's other branch. `connection_requests` already carries
# `source` as targeting provenance (issue #486), so the person approving sees which walk found them.
CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER = 'profile_viewer'
def get_scheduled_dm_user_id(dm_id: int) -> Optional[int]:
    """Who owns a scheduled DM, for the API's target-authorisation check (issue #914).

    None for a missing OR unreadable row: callers compare it against the session user, so either way the
    request is denied rather than allowed.
    """
    row = get_scheduled_dm(dm_id)
    return row["user_id"] if row else None
def get_connection_request_user_id(request_id: int) -> Optional[int]:
    """Who owns a connection request, for the API's target-authorisation check (issue #914).

    None for a missing OR unreadable row, which denies rather than allows.
    """
    row = get_connection_request(request_id)
    return row["user_id"] if row else None
def get_outreach_target_user_id(target_id: int) -> Optional[int]:
    """Who owns an outreach target, for the API's target-authorisation check (issue #914).

    None for a missing OR unreadable row, which denies rather than allows.
    """
    row = get_outreach_target(target_id)
    return row["user_id"] if row else None
def get_catchup_touch_user_id(touch_id: int) -> Optional[int]:
    """Who owns a catch-up touch, for the API's target-authorisation check (issue #914).

    None for a missing OR unreadable row, which denies rather than allows.
    """
    row = get_catchup_touch(touch_id)
    return row["user_id"] if row else None
# --- lead scoring & CRM-lite pipeline (issue #484) ---------------------------------------------
# Every source below is engagement we ALREADY record, read back as one normalized activity stream
# (kind, person_name, person_profile_url, occurred_at, detail). No new scraping.
_LEAD_ACTIVITY_SOURCES: tuple = (
    (LeadSignalKind.ENGAGED,
     "SELECT engager_name AS person_name, engager_profile_url AS person_profile_url, "
     "last_engaged_at AS occurred_at, '' AS detail FROM post_engagers "
     "WHERE user_id=%s AND last_engaged_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.INTENT,
     "SELECT person_name, person_profile_url, created_at AS occurred_at, status AS detail "
     "FROM lead_signals WHERE user_id=%s AND created_at >= (NOW() - INTERVAL %s DAY) "
     "AND status <> 'dismissed'"),
    (LeadSignalKind.DM,
     "SELECT recipient_name AS person_name, recipient_profile_url AS person_profile_url, "
     "updated_at AS occurred_at, status AS detail FROM scheduled_dms "
     "WHERE user_id=%s AND status='sent' AND updated_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.DM,
     "SELECT first_name AS person_name, profile_url AS person_profile_url, "
     "created_at AS occurred_at, event_type AS detail FROM dm_followups "
     "WHERE user_id=%s AND event_type <> 'profile_viewer' "
     "AND created_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.PROFILE_VIEW,
     "SELECT first_name AS person_name, profile_url AS person_profile_url, "
     "created_at AS occurred_at, event_type AS detail FROM dm_followups "
     "WHERE user_id=%s AND event_type='profile_viewer' "
     "AND created_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.CONNECT,
     "SELECT recipient_name AS person_name, recipient_profile_url AS person_profile_url, "
     "updated_at AS occurred_at, status AS detail FROM connection_requests "
     "WHERE user_id=%s AND status='sent' AND updated_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.FUNNEL,
     "SELECT target_name AS person_name, target_profile_url AS person_profile_url, "
     "updated_at AS occurred_at, stage AS detail FROM outreach_funnel_targets "
     "WHERE user_id=%s AND status <> 'canceled' AND updated_at >= (NOW() - INTERVAL %s DAY)"),
)
def get_lead_activity(user_id: int, days: int = 90) -> list:
    """Every engagement signal about every person who touched this user in the window, normalized
    for the scorer. Each source is queried independently so one unavailable table degrades that
    signal instead of losing the whole pipeline.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    rows: list = []
    try:
        for kind, sql in _LEAD_ACTIVITY_SOURCES:
            try:
                cursor.execute(sql, (user_id, days))
                for row in cursor.fetchall():
                    row["kind"] = str(kind)
                    rows.append(row)
            except mysql.connector.Error as err:
                log_error(f"Could not read {kind} lead activity", exc=err, user_id=user_id)
        return rows
    finally:
        cursor.close()
        connection.close()
def _like_literal(value: str, escape: str = "!") -> str:
    """Escape LIKE metacharacters so a value is matched literally. A newsletter URL can carry
    percent-encoding ('%20'), and an unescaped '%' inside the pattern matches ANY text — which would
    silently over-count the attribution it feeds.
    """
    return (str(value).replace(escape, escape + escape)
            .replace("%", escape + "%").replace("_", escape + "_"))
def count_artifact_cta_deliveries(user_id: int, days: int = 90,
                                  newsletter_url: Optional[str] = None) -> dict:
    """Owned-asset CTA deliveries in the last `days` (issue #624) — the attribution half of the loop,
    so subscriber growth can be read against the CTAs that were actually delivered.

    The two mechanics are counted SEPARATELY because they deliver differently and one of them is not
    a send at all: `lead_magnet_dms` counts the approval-gated DM drafts this automation queued, and
    `newsletter_links` counts the published posts that carried the subscribe URL. `newsletter_links`
    is None — not 0 — when the user has no newsletter URL configured: there was nothing to carry,
    which is a different fact from "carried nothing".

    The link side matches EITHER column, because which one holds the URL depends on the host and
    only one of the two cases is the common one: `newsletter_url` is written by
    `mark_newsletter_published` from a linkedin.com article URL, and #392's split deliberately leaves
    in-platform links in the BODY (they carry no reach penalty), so `first_comment_link` alone would
    report 0 forever for the mainline LinkedIn newsletter. An off-platform newsletter (Substack &c.)
    is the reverse: the split moves it out of `content` and into `first_comment_link`.
    """
    window = max(1, int(days or 1))
    out: dict = {"window_days": window, "lead_magnet_dms": 0, "newsletter_links": None}
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM scheduled_dms WHERE user_id = %s AND source = %s "
                "AND created_at >= (NOW() - INTERVAL %s DAY)",
                (user_id, SCHEDULED_DM_SOURCE_ARTIFACT, window))
            row = cursor.fetchone()
            out["lead_magnet_dms"] = int(row[0]) if row and row[0] else 0
            url = str(newsletter_url or "").strip()
            if url:
                pattern = f"%{_like_literal(url)}%"
                cursor.execute(
                    "SELECT COUNT(*) FROM posts WHERE user_id = %s AND status = %s "
                    "AND (content LIKE %s ESCAPE '!' OR first_comment_link LIKE %s ESCAPE '!') "
                    "AND updated_at >= (NOW() - INTERVAL %s DAY)",
                    (user_id, PostStatus.POSTED.value, pattern, pattern, window))
                row = cursor.fetchone()
                out["newsletter_links"] = int(row[0]) if row and row[0] else 0
            return out
    except mysql.connector.Error as err:
        log_error("Could not count artifact CTA deliveries", exc=err, user_id=user_id)
        return out
# The appreciation triggers that share `_dispatch_appreciation_dms` — and therefore the ledger below.
APPRECIATION_EVENT_TYPES = ("connection_accepted", "recommendation_received", "collaboration")
def count_existing_double_sent_catchups() -> int:
    """Count contacts who were sent the SAME catch-up congratulations more than once.

    Measured off `logs`, not `catchup_touches`: the ledger carries a UNIQUE key on
    (user, profile_url, event_type, event_period), so grouping IT by that key can never return a
    duplicate — the historical double-send this issue is about came from ONE touch row being sent
    twice (a retry or an orphan re-queue after the status update was lost), which shows up only as
    two `success` DM log rows carrying the same body to the same person.

    This is the duplicate SURFACE, not a proven double-send count: the default congratulations are
    deterministic per event type and name, so one contact's repeated annual milestone produces the
    same body twice and is counted here. `list_existing_double_sent_catchups()` carries the send
    gap that separates the two — read it before reporting this number as double-sends.

    Read-only, run once at deploy time to report the historical duplicate surface on the issue
    (#1078). Returns 0 when nothing is double-sent or the read fails.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT l.user_id, l.post_url, l.message FROM logs l "
                "WHERE l.action_type = 'dm' AND l.result = 'success' "
                # EXISTS rather than a JOIN: two milestones can share one body (the deterministic
                # fallback congratulations), and a join would multiply ONE log row into a fake duplicate.
                "AND EXISTS (SELECT 1 FROM catchup_touches c WHERE c.user_id = l.user_id "
                "AND c.profile_url = l.post_url AND c.message = l.message) "
                "GROUP BY l.user_id, l.post_url, l.message HAVING COUNT(*) > 1"
                ") dupes")
            r = cursor.fetchone()
            return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        log_error("Could not count existing double-sent catch-ups", exc=err)
        return 0
def list_existing_double_sent_catchups() -> list[dict]:
    """List the (user, contact) pairs behind `count_existing_double_sent_catchups()`.

    Same read, one grain up: the counter returns how many catch-up BODIES were sent more than once,
    this rolls those rows up per contact so a non-zero count can be judged per person (does anyone
    need an apology, or was it one contact hit twice?). `sends` is every `success` DM row across the
    repeated bodies, so a body sent twice contributes 2; `duplicate_bodies` is how many distinct
    bodies repeated, and summing it over the list reproduces the counter.

    The message body itself is deliberately not returned — it is DM content, and the pair plus the
    send counts is what the judgement needs.

    The timestamps are not decoration: the repeated BODY alone cannot tell a double-send from a
    legitimate repeat. `_CATCHUP_DEFAULT_CONGRATS` is deterministic per event type and first name,
    so Jane's 2025 and 2026 work anniversaries — two correctly-deduped touches, different
    `event_period` — both send the literal string "Happy work anniversary, Jane!" and land in this
    read as one repeated body. What separates them is the GAP: a retry or the orphan re-queue fires
    seconds to hours apart, an annual or monthly milestone weeks to a year. `repeat_gap_seconds` is
    the tightest such gap on the pair, so the owner can tell "sent twice by mistake" from
    "congratulated twice, correctly, a year apart" without ever seeing the message.

    Returns:
        One dict per affected pair (`user_id`, `profile_url`, `sends`, `duplicate_bodies`,
        `first_sent_at`, `last_sent_at`, `repeat_gap_seconds`), worst first. Empty when nothing is
        double-sent or the read fails.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT d.user_id, d.post_url AS profile_url, "
                "SUM(d.sends) AS sends, COUNT(*) AS duplicate_bodies, "
                "MIN(d.first_sent) AS first_sent_at, MAX(d.last_sent) AS last_sent_at, "
                "MIN(TIMESTAMPDIFF(SECOND, d.first_sent, d.last_sent)) AS repeat_gap_seconds FROM ("
                "SELECT l.user_id, l.post_url, l.message, COUNT(*) AS sends, "
                "MIN(l.created_at) AS first_sent, MAX(l.created_at) AS last_sent FROM logs l "
                "WHERE l.action_type = 'dm' AND l.result = 'success' "
                # EXISTS rather than a JOIN, for the same reason as the counter above: two
                # milestones can share one body, and a join would fake a duplicate out of one row.
                "AND EXISTS (SELECT 1 FROM catchup_touches c WHERE c.user_id = l.user_id "
                "AND c.profile_url = l.post_url AND c.message = l.message) "
                "GROUP BY l.user_id, l.post_url, l.message HAVING COUNT(*) > 1"
                ") d GROUP BY d.user_id, d.post_url ORDER BY sends DESC, d.user_id")
            return [
                {
                    "user_id": int(row["user_id"]),
                    "profile_url": row["profile_url"],
                    "sends": int(row["sends"]),
                    "duplicate_bodies": int(row["duplicate_bodies"]),
                    "first_sent_at": row["first_sent_at"],
                    "last_sent_at": row["last_sent_at"],
                    # `logs.created_at` carries no NOT NULL, so an unstamped row reads as an
                    # UNKNOWN gap — never as 0, which is the strongest possible retry signal.
                    "repeat_gap_seconds": (None if row["repeat_gap_seconds"] is None
                                           else int(row["repeat_gap_seconds"])),
                }
                for row in (cursor.fetchall() or [])
            ]
    except mysql.connector.Error as err:
        log_error("Could not list existing double-sent catch-ups", exc=err)
        return []
