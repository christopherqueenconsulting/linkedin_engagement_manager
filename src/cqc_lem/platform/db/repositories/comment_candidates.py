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


def _day_bounds(day: Optional[date]) -> tuple:
    day = day or datetime.now(timezone.utc).date()
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def insert_comment_candidate(user_id: int, post_key: str, source: str, score: float,
                             post_url: Optional[str] = None, author: Optional[str] = None,
                             components: Optional[dict] = None) -> bool:
    """Record one scored candidate. True only when a NEW row was written.

    `INSERT IGNORE` on (user_id, post_key): a post seen again by a later scan is a no-op, so the
    first score stands and the pool never holds a post twice.
    """
    if not post_key:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO comment_candidates (user_id, post_key, post_url, author, source, "
                "score, score_components) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_id, str(post_key)[:255], post_url[:1024] if post_url else None,
                 author[:255] if author else None, source[:16], float(score),
                 json.dumps(components) if components else None))
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
