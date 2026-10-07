"""Every SQL statement LEM runs against `curated_sources` and the curated columns on `posts`.

The curated-sources feature (docs/curated-sources.md). `cqc_lem.utilities.db` re-exports every
name below, so importers use the facade; a test patches the name HERE, where it lives.

Reads that decide whether something may PUBLISH fail CLOSED: `get_post_curated_context` answers
None on a read error, and the publish path treats an unreadable context on a post it knows is
curated as a refusal. Reads that only decide whether a slot MAY be curated fail towards "not
curated": `get_curated_neighbors` returns None on an error, which the drafter reads as "do not
draft one now".
"""

import hashlib
from datetime import datetime
from typing import Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor, to_naive_utc
from cqc_lem.utilities.logger import log_error

_SOURCE_COLUMNS = ("id, user_id, platform, url, canonical_id, activity_urn, link_only, author, "
                   "publisher, title, excerpt, og_image_url, licence, paywalled, published_at, "
                   "fetched_at, status, block_reason, post_id")
_LIMITS = {"url": 1024, "canonical_id": 128, "activity_urn": 128, "author": 255,
           "publisher": 255, "title": 512, "og_image_url": 1024, "licence": 32,
           "block_reason": 255, "excerpt": 8000}


def curated_url_hash(url: str) -> str:
    """The dedupe key for a source URL: sha256 of the stripped URL."""
    return hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()


def _clip(field: str, value):
    if value is None or not isinstance(value, str):
        return value
    return value[:_LIMITS[field]] if field in _LIMITS else value


def _row(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    out = dict(row)
    out["link_only"] = bool(out.get("link_only"))
    out["paywalled"] = bool(out.get("paywalled"))
    return out


def insert_curated_source(user_id: int, item: dict, status: str = "new",
                          block_reason: Optional[str] = None) -> Optional[int]:
    """Record one collected candidate. Returns its id, or None when it already exists or failed.

    `INSERT IGNORE` on the (user_id, url_hash) key: a re-collected URL is a no-op, never a second
    row, so a source the owner rejected is not offered again by the next collector run.
    """
    url = (item.get("url") or "").strip()
    if not url:
        return None
    published_at = item.get("published_at")
    if isinstance(published_at, datetime):
        published_at = to_naive_utc(published_at)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO curated_sources (user_id, platform, url, url_hash, canonical_id, "
                "activity_urn, link_only, author, publisher, title, excerpt, og_image_url, licence, "
                "paywalled, published_at, status, block_reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, item.get("platform"), _clip("url", url), curated_url_hash(url),
                 _clip("canonical_id", item.get("canonical_id")),
                 _clip("activity_urn", item.get("activity_urn")),
                 1 if item.get("link_only") else 0,
                 _clip("author", item.get("author")), _clip("publisher", item.get("publisher")),
                 _clip("title", item.get("title")), _clip("excerpt", item.get("excerpt")),
                 _clip("og_image_url", item.get("og_image_url")),
                 _clip("licence", item.get("licence") or "unknown"),
                 1 if item.get("paywalled") else 0, published_at, status,
                 _clip("block_reason", block_reason)))
            return cursor.lastrowid if cursor.rowcount else None
    except mysql.connector.Error as err:
        log_error("Could not record a curated source", exc=err, user_id=user_id)
        return None


def get_curated_sources(user_id: int, status: Optional[str] = None,
                        limit: int = 50) -> Optional[list]:
    """The user's curated sources, newest first. None on a read error — never an empty list.

    The API turns None into a 503: "no sources" and "could not read the sources" are different
    answers, and rendering the second as the first hides a database fault.
    """
    where, params = "WHERE user_id = %s", [user_id]
    if status:
        where += " AND status = %s"
        params.append(status)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT {_SOURCE_COLUMNS} FROM curated_sources {where} "
                           "ORDER BY fetched_at DESC, id DESC LIMIT %s", params + [int(limit)])
            return [_row(r) for r in cursor.fetchall() or []]
    except mysql.connector.Error as err:
        log_error("Could not list curated sources", exc=err, user_id=user_id)
        return None


def get_curated_source(source_id: int, user_id: Optional[int] = None) -> Optional[dict]:
    """One curated source, scoped to `user_id` when given. None when absent or unreadable."""
    where, params = "WHERE id = %s", [source_id]
    if user_id is not None:
        where += " AND user_id = %s"
        params.append(user_id)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT {_SOURCE_COLUMNS} FROM curated_sources {where}", params)
            return _row(cursor.fetchone())
    except mysql.connector.Error as err:
        log_error("Could not read a curated source", exc=err, source_id=source_id)
        return None


def get_draftable_curated_sources(user_id: int, limit: int = 10) -> list:
    """The user's `new` sources, freshest first — the drafter's candidate list. [] on error."""
    rows = get_curated_sources(user_id, status="new", limit=limit)
    return rows or []


def get_recent_curated_publishers(user_id: int, limit: int = 3) -> list:
    """The publishers of the user's `limit` most recently DRAFTED curated sources, newest first.

    The curated pick's diversity rule (showcase round 4): two Google items in four posts read as a
    news feed, so a candidate from a publisher in this list ranks behind one that is not. [] on
    error — an unreadable history only loses the diversity preference.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT publisher FROM curated_sources WHERE user_id = %s "
                "AND status IN ('drafted', 'approved', 'published') AND post_id IS NOT NULL "
                "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
            return [r[0] for r in cursor.fetchall() or [] if r and r[0]]
    except mysql.connector.Error as err:
        log_error("Could not read the recent curated publishers", exc=err, user_id=user_id)
        return []


def update_curated_source_status(source_id: int, status: str, block_reason: Optional[str] = None,
                                 post_id: Optional[int] = None) -> bool:
    """Move a source to `status`, recording a block reason and/or the post it became."""
    sets, params = ["status = %s"], [status]
    if block_reason is not None:
        sets.append("block_reason = %s")
        params.append(_clip("block_reason", block_reason))
    if post_id is not None:
        sets.append("post_id = %s")
        params.append(post_id)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE curated_sources SET {', '.join(sets)} WHERE id = %s",
                           params + [source_id])
            return True
    except mysql.connector.Error as err:
        log_error("Could not update a curated source", exc=err, source_id=source_id)
        return False


def attach_curated_source_to_post(post_id: int, source_id: int, treatment: str) -> bool:
    """Mark `post_id` as the curated post built from `source_id` with `treatment`."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE posts SET curated_source_id = %s, source_treatment = %s "
                           "WHERE id = %s", (source_id, treatment, post_id))
            return True
    except mysql.connector.Error as err:
        log_error("Could not attach a curated source to its post", exc=err, post_id=post_id)
        return False


def post_is_curated(post_id: int) -> Optional[bool]:
    """True when the post was built from a curated source; None when unreadable."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT curated_source_id FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return bool(row and row[0] is not None)
    except mysql.connector.Error as err:
        log_error("Could not read whether a post is curated", exc=err, post_id=post_id)
        return None


def get_post_curated_context(post_id: int) -> Optional[dict]:
    """Everything the publisher needs for a curated post, or None (not curated / unreadable).

    The post's `status`, `approved_by`, `source_treatment` and `image_url` beside its source row
    (under `source`). `{"unreadable": True}` on a read error, so the caller can tell "this post is
    not curated" from "we could not tell" and fail closed on the second.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT p.id AS post_id, p.user_id AS post_user_id, p.status AS post_status, "
                "p.approved_by, p.source_treatment, p.image_url, p.curated_source_id "
                "FROM posts p WHERE p.id = %s", (post_id,))
            post = cursor.fetchone()
            if not post or post.get("curated_source_id") is None:
                return None
            cursor.execute(f"SELECT {_SOURCE_COLUMNS} FROM curated_sources WHERE id = %s",
                           (post["curated_source_id"],))
            source = _row(cursor.fetchone())
    except mysql.connector.Error as err:
        log_error("Could not read a curated post's source", exc=err, post_id=post_id)
        return {"unreadable": True}
    return {**post, "source": source}


def get_curated_neighbors(user_id: int, post_id: int,
                          reach: int = 2) -> Optional[tuple[list, list]]:
    """Whether each of the `reach` posts either side of `post_id` (by schedule) is curated.

    Returns `(before_oldest_first, after_nearest_first)` lists of bools, or None on a read error.
    Rejected posts never publish, so they do not count towards the ceiling.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT scheduled_time FROM posts WHERE id = %s AND user_id = %s",
                           (post_id, user_id))
            row = cursor.fetchone()
            if not row or row[0] is None:
                return None
            slot = row[0]
            cursor.execute(
                "SELECT curated_source_id IS NOT NULL FROM posts WHERE user_id = %s AND id <> %s "
                "AND status <> 'rejected' AND (scheduled_time < %s OR (scheduled_time = %s AND "
                "id < %s)) ORDER BY scheduled_time DESC, id DESC LIMIT %s",
                (user_id, post_id, slot, slot, post_id, reach))
            before = [bool(r[0]) for r in cursor.fetchall() or []][::-1]
            cursor.execute(
                "SELECT curated_source_id IS NOT NULL FROM posts WHERE user_id = %s AND id <> %s "
                "AND status <> 'rejected' AND (scheduled_time > %s OR (scheduled_time = %s AND "
                "id > %s)) ORDER BY scheduled_time ASC, id ASC LIMIT %s",
                (user_id, post_id, slot, slot, post_id, reach))
            after = [bool(r[0]) for r in cursor.fetchall() or []]
            return before, after
    except mysql.connector.Error as err:
        log_error("Could not read the curated ceiling window", exc=err, user_id=user_id,
                  post_id=post_id)
        return None


def get_curated_summaries(user_id: int, source_ids: list) -> dict:
    """`{source_id: row}` for the Content Studio's credit line. {} on error or no ids."""
    ids = [int(i) for i in source_ids or [] if i is not None]
    if not ids:
        return {}
    marks = ",".join(["%s"] * len(ids))
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT {_SOURCE_COLUMNS} FROM curated_sources "
                           f"WHERE user_id = %s AND id IN ({marks})", [user_id, *ids])
            return {r["id"]: _row(r) for r in cursor.fetchall() or []}
    except mysql.connector.Error as err:
        log_error("Could not read curated sources for the post list", exc=err, user_id=user_id)
        return {}
