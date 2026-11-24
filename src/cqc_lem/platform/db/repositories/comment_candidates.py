"""Every SQL statement LEM runs against `comment_candidates` (issue #2635, Phase 1).

`cqc_lem.utilities.db` re-exports every name below, so importers use the facade; a test patches
the name HERE, where it lives.

Writes fail soft (a candidate row is a by-product of a scan, never worth aborting it). Reads return
None on a fault, never an empty list: "no candidates" and "could not read them" are different
answers.
"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.utilities.logger import log_error

_COLUMNS = ("c.id, c.user_id, c.post_key, c.post_url, c.author, c.source, c.score, "
            "c.score_components, c.scanned_at, c.status")


_POST_TEXT_MAX = 8000

STATUS_NEW = "new"
STATUS_DRAFTED = "drafted"
STATUS_APPROVED = "approved"
STATUS_CANCELED = "canceled"
STATUS_SKIPPED = "skipped"


def _day_bounds(day: Optional[date]) -> tuple:
    day = day or datetime.now(timezone.utc).date()
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def insert_comment_candidate(user_id: int, post_key: str, source: str, score: float,
                             post_url: Optional[str] = None, author: Optional[str] = None,
                             components: Optional[dict] = None,
                             post_text: Optional[str] = None) -> bool:
    """Record one scored candidate. True only when a NEW row was written.

    `INSERT IGNORE` on (user_id, post_key): a post seen again by a later scan is a no-op, so the
    first score stands and the pool never holds a post twice. `post_text` is what a later draft is
    written from (Phase 3).
    """
    if not post_key:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO comment_candidates (user_id, post_key, post_url, author, source, "
                "score, score_components, post_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, str(post_key)[:255], post_url[:1024] if post_url else None,
                 author[:255] if author else None, source[:16], float(score),
                 json.dumps(components) if components else None,
                 post_text[:_POST_TEXT_MAX] if post_text else None))
            return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        log_error("Could not record a comment candidate", exc=err, user_id=user_id)
        return False


def get_comment_candidates(user_id: int, day: Optional[date] = None,
                           limit: int = 150) -> Optional[list]:
    """The user's open candidates scanned on `day` (UTC, default today), best score first.

    Posts already in the `commented_posts` ledger are excluded in SQL, so a post commented on after
    it was scanned never reads as still open. None on a read error.
    """
    start, end = _day_bounds(day)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_COLUMNS} FROM comment_candidates c WHERE c.user_id = %s "
                "AND c.scanned_at >= %s AND c.scanned_at < %s AND c.status = 'new' "
                "AND NOT EXISTS (SELECT 1 FROM commented_posts p WHERE p.user_id = c.user_id "
                "AND p.post_key = c.post_key) "
                "ORDER BY c.score DESC, c.id ASC LIMIT %s",
                (user_id, start, end, int(limit)))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not read comment candidates", exc=err, user_id=user_id)
        return None


def count_comment_candidates(user_id: int, day: Optional[date] = None,
                             source: Optional[str] = None) -> Optional[int]:
    """How many candidates were scanned for the user on `day` (UTC, default today). None on error.

    `source` narrows the count to one lane (`feed` / `roster` / `crawl`); the crawl's daily cap
    reads its own lane so a busy feed scan never spends it.
    """
    start, end = _day_bounds(day)
    sql = ("SELECT COUNT(*) FROM comment_candidates WHERE user_id = %s "
           "AND scanned_at >= %s AND scanned_at < %s")
    params: tuple = (user_id, start, end)
    if source:
        sql += " AND source = %s"
        params += (source[:16],)
    try:
        with db_cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count comment candidates", exc=err, user_id=user_id)
        return None


def get_icp_post_texts(user_id: int, days: int = 90, limit: int = 60) -> Optional[list]:
    """Post text the roster scan stored from the user's ICP targets, newest first.

    The vocabulary source for the monthly profile audit (issue #2754). None on a read error.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT post_text FROM comment_candidates WHERE user_id = %s AND source = 'roster' "
                "AND post_text IS NOT NULL AND post_text <> '' "
                "AND scanned_at >= (NOW() - INTERVAL %s DAY) ORDER BY id DESC LIMIT %s",
                (user_id, int(days), int(limit)))
            return [str(r[0]) for r in (cursor.fetchall() or []) if r and r[0]]
    except mysql.connector.Error as err:
        log_error("Could not read ICP post texts", exc=err, user_id=user_id)
        return None


def get_approved_comment_candidates(user_id: int, limit: int = 150) -> Optional[list]:
    """The user's candidates a human approved and nobody has posted, oldest approval first.

    Posts already in the `commented_posts` ledger are excluded, so Assist never offers a post a
    lane already commented on. Only rows with draft text qualify. None on a read error.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT c.id, c.post_key, c.post_url, c.author, c.draft_text, c.approved_at "
                "FROM comment_candidates c WHERE c.user_id = %s AND c.status = 'approved' "
                "AND c.draft_text IS NOT NULL AND c.draft_text <> '' "
                "AND NOT EXISTS (SELECT 1 FROM commented_posts p WHERE p.user_id = c.user_id "
                "AND p.post_key = c.post_key) "
                "ORDER BY c.approved_at ASC, c.id ASC LIMIT %s",
                (user_id, int(limit)))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not read approved comment candidates", exc=err, user_id=user_id)
        return None


def get_candidates_to_draft(user_id: int, limit: int = 50) -> Optional[list]:
    """Open candidates with saved post text and no draft yet, best score first. None on a fault.

    Posts already in the `commented_posts` ledger are excluded, as in the other readers.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT c.id, c.post_key, c.post_url, c.author, c.score, c.post_text "
                "FROM comment_candidates c WHERE c.user_id = %s AND c.status = 'new' "
                "AND c.draft_text IS NULL AND c.post_text IS NOT NULL AND c.post_text <> '' "
                "AND NOT EXISTS (SELECT 1 FROM commented_posts p WHERE p.user_id = c.user_id "
                "AND p.post_key = c.post_key) "
                "ORDER BY c.score DESC, c.id ASC LIMIT %s",
                (user_id, int(limit)))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not read candidates to draft", exc=err, user_id=user_id)
        return None


def get_drafted_comment_candidates(user_id: int, limit: int = 500) -> Optional[list]:
    """Candidates holding a draft that waits on the author's review, oldest first. None on a fault."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT c.id, c.post_key, c.post_url, c.author, c.score, c.draft_text, c.scanned_at "
                "FROM comment_candidates c WHERE c.user_id = %s AND c.status = 'drafted' "
                "AND NOT EXISTS (SELECT 1 FROM commented_posts p WHERE p.user_id = c.user_id "
                "AND p.post_key = c.post_key) "
                "ORDER BY c.scanned_at ASC, c.id ASC LIMIT %s",
                (user_id, int(limit)))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not read drafted comment candidates", exc=err, user_id=user_id)
        return None


def _move_candidate(user_id: int, candidate_id: int, to_status: str, from_status: str,
                    draft_text: Optional[str] = None, stamp_approval: bool = False) -> bool:
    """One conditional UPDATE: True only when this call moved the row out of `from_status`."""
    sets = ["status = %s"]
    params: list = [to_status]
    if draft_text is not None:
        sets.append("draft_text = %s")
        params.append(draft_text)
    if stamp_approval:
        sets.append("approved_at = CURRENT_TIMESTAMP")
    params += [int(candidate_id), user_id, from_status]
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"UPDATE comment_candidates SET {', '.join(sets)} "
                "WHERE id = %s AND user_id = %s AND status = %s", tuple(params))
            return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        log_error("Could not move a comment candidate", exc=err, user_id=user_id)
        return False


def save_candidate_draft(user_id: int, candidate_id: int, draft_text: str) -> bool:
    """Store a draft and move the row `new` -> `drafted`; False when it was not still `new`."""
    return bool(draft_text and draft_text.strip()) and _move_candidate(
        user_id, candidate_id, STATUS_DRAFTED, STATUS_NEW, draft_text=draft_text.strip())


def skip_candidate(user_id: int, candidate_id: int) -> bool:
    """Retire a `new` candidate the generator declined, so the next run does not re-pay for it."""
    return _move_candidate(user_id, candidate_id, STATUS_SKIPPED, STATUS_NEW)


def approve_comment_candidate(user_id: int, candidate_id: int) -> bool:
    """Approve a `drafted` candidate (stamps `approved_at`); False for any other state or owner."""
    return _move_candidate(user_id, candidate_id, STATUS_APPROVED, STATUS_DRAFTED,
                           stamp_approval=True)


def cancel_comment_candidate(user_id: int, candidate_id: int) -> bool:
    """Cancel a `drafted` candidate; the row stays as the dedupe tombstone."""
    return _move_candidate(user_id, candidate_id, STATUS_CANCELED, STATUS_DRAFTED)


def get_assist_activity(user_id: int, start: datetime, end: datetime) -> Optional[dict]:
    """Approval and posted timestamps of the user's candidates touched in [start, end).

    Returns `{"approved_at": [...], "posted_at": [...]}` (each ascending, NULLs dropped). A
    candidate is `posted` only through the Assist done endpoint, so `posted_at` is the Assist
    comment record. None on a read error: an unreadable week is not an empty one.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT approved_at, posted_at FROM comment_candidates WHERE user_id = %s "
                "AND ((approved_at >= %s AND approved_at < %s) "
                "OR (posted_at >= %s AND posted_at < %s))",
                (user_id, start, end, start, end))
            rows = list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not read assist activity", exc=err, user_id=user_id)
        return None
    approved = sorted(r[0] for r in rows if r[0] is not None and start <= r[0] < end)
    posted = sorted(r[1] for r in rows if r[1] is not None and start <= r[1] < end)
    return {"approved_at": approved, "posted_at": posted}


def claim_approved_candidate_posted(user_id: int, candidate_id: int) -> Optional[dict]:
    """Atomically move ONE approved candidate to `posted`; return its row, or None.

    The single UPDATE is conditioned on `status = 'approved'` and the caller's `user_id`, so a
    candidate that was never approved, is someone else's, or was already reported posted changes
    nothing and returns None: the done endpoint cannot be used to record an unapproved comment or
    to count one twice.
    """
    try:
        with db_cursor(commit=True, dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, post_key, post_url, author, draft_text FROM comment_candidates "
                "WHERE id = %s AND user_id = %s AND status = 'approved' FOR UPDATE",
                (int(candidate_id), user_id))
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "UPDATE comment_candidates SET status = 'posted', posted_at = CURRENT_TIMESTAMP "
                "WHERE id = %s AND user_id = %s AND status = 'approved'",
                (int(candidate_id), user_id))
            return row if cursor.rowcount >= 1 else None
    except mysql.connector.Error as err:
        log_error("Could not claim an approved comment candidate", exc=err, user_id=user_id)
        return None
