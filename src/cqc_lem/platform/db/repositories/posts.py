"""Every SQL statement LEM runs against the posts tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

import json
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from typing import Optional

import mysql.connector

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.connection import (
    db_cursor,
    to_naive_utc,
)
from cqc_lem.platform.db.enums import (
    LogActionType,
    LogResultType,
    PostStatus,
    PostType,
)
from cqc_lem.platform.db.shared import (
    DEFAULT_CONTENT_BUFFER_DAYS,
    DEFAULT_CONTENT_BUFFER_MAX_POSTS,
    MAX_CONTENT_BUFFER_DAYS,
    OwnershipUnprovable,
)
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning


def insert_planned_post(user_id: int, scheduled_time: datetime, post_type: PostType, buyer_stage: str,
                        content_mix: Optional[str] = None) -> bool:
    """Insert the SKELETON of a planned post — schedule slot, type, buyer stage and mix class, no content.

    Lands at `PostStatus.PLANNING` with the literal body 'TBD', which is the placeholder the generation
    pass overwrites later.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    success = False

    try:
        scheduled_time = to_naive_utc(scheduled_time)

        cursor.execute("""
            INSERT INTO posts (scheduled_time, post_type, user_id, buyer_stage, content_mix, status, content)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (scheduled_time, post_type.value, user_id, buyer_stage,
              str(content_mix) if content_mix else None, PostStatus.PLANNING.value, 'TBD'))

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not insert planned post", exc=e)
    finally:
        cursor.close()
        connection.close()
    return success
def insert_occasion_post(user_id: int, scheduled_time: datetime, buyer_stage: str) -> Optional[int]:
    """Insert the SKELETON of an occasion/milestone post and return its id (issue #1074).

    Lands at `PostStatus.PLANNING` with the 'TBD' placeholder, exactly like `insert_planned_post` —
    the drafting task overwrites it — but with `manual_publish = 1`, which is what permanently keeps
    the scheduler and `post_to_linkedin` off the row. The id comes back because the caller has to
    hand it to the drafting task; None means nothing was written.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    post_id = None
    try:
        cursor.execute("""
            INSERT INTO posts (scheduled_time, post_type, user_id, buyer_stage, status, content,
                               manual_publish)
            VALUES (%s, %s, %s, %s, %s, %s, 1)
        """, (to_naive_utc(scheduled_time), PostType.TEXT.value, user_id, buyer_stage,
              PostStatus.PLANNING.value, 'TBD'))
        connection.commit()
        post_id = cursor.lastrowid if cursor.rowcount == 1 else None
    except mysql.connector.Error as e:
        log_error("Could not insert occasion post", exc=e, user_id=user_id)
    finally:
        cursor.close()
        connection.close()
    return post_id
def get_post_manual_publish(post_id: int) -> bool:
    """True when this post publishes by hand through LinkedIn's native occasion composer (#1074).

    Fails CLOSED-ish in the direction that matters: an unreadable row answers False, which is the
    pre-#1074 behaviour for every post that ever existed — the automatic path. The scheduler query
    is the primary gate; this is the publish-time cross-check.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT manual_publish FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not read manual_publish", exc=err, post_id=post_id)
        row = None
    finally:
        cursor.close()
        connection.close()
    return bool(row[0]) if row else False
def update_db_post(content: str, video_url: str, scheduled_time: datetime, post_type: PostType, post_id: int,
                   post_status: PostStatus, user_id: Optional[int] = None) -> bool:
    """`user_id` scopes the write to one account's row — same reason as `bulk_update_posts`."""
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    success = False

    try:

        scheduled_time = to_naive_utc(scheduled_time)

        params: list = [content, video_url, scheduled_time, post_type.value, post_status.value, post_id]
        owner_clause = ""
        if user_id is not None:
            owner_clause = " AND user_id = %s"
            params.append(user_id)

        cursor.execute(
            "UPDATE posts SET content = %s, video_url = %s, scheduled_time =%s, post_type = %s, "
            f"status = %s WHERE id = %s{owner_clause}",
            params
        )

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update post", exc=e)
    finally:
        cursor.close()
        connection.close()

    return success
def update_db_post_content(post_id: int, content: str) -> bool:
    """Overwrite a post's body.

    False means the row was not CHANGED, which is three different facts: the write failed, no row
    matched (this never creates a post), or the row already held this exact content. MySQL reports
    changed rather than matched rows unless the connection sets `CLIENT.FOUND_ROWS`, and
    `_get_mysql_config` does not — so re-saving identical content answers False.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET content = %s WHERE id = %s",
                (content, post_id)
            )

            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update post content", exc=e)

    return success
def update_db_post_video_url(post_id: int, video_url: str) -> bool:
    """Point a post at its rendered video.

    False when the write failed or no row matched.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET video_url = %s WHERE id = %s",
                (video_url, post_id)
            )

            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update post video url", exc=e)

    return success


def update_db_post_video_model(post_id: int, model: Optional[str]) -> bool:
    """Record which model rendered a post's video (issue #1410).

    `model` is the `video_models.VIDEO_MODELS` key the render actually used, or `pexels` for the
    stock fallback. None CLEARS the column, which is what a render that produced no asset at all
    leaves behind — a stale key from a previous attempt would be read as the model of an asset it
    never produced. `posts.video_quality` is not this: that records what was REQUESTED, before the
    no-credits degrade and the stock fallback.

    False when the write failed or no row matched.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET video_model = %s WHERE id = %s",
                (str(model)[:32] if model else None, post_id)
            )

            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update post video model", exc=e, post_id=post_id)

    return success


def update_db_post_captions(post_id: int, caption_text: Optional[str],
                            caption_srt_url: Optional[str]) -> bool:
    """Record the muted-autoplay caption produced for a video post (issue #1278).

    `caption_text` is the caption that was authored from the post's own opening — burned onto the
    frame when the frame allowed it — and `caption_srt_url` the sidecar that was written, which is
    real on every path that gets this far (an avatar-led frame without the overlay opt-in, or a
    burn that failed open, still leaves the author an .srt to attach on LinkedIn). Both nullable,
    because a post with no usable hook legitimately ships uncaptioned.

    False when the write failed or no row matched.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET caption_text = %s, caption_srt_url = %s WHERE id = %s",
                (caption_text, caption_srt_url, post_id)
            )

            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update post captions", exc=e, post_id=post_id)

    return success


def get_post_captions(post_id: int) -> dict:
    """`{"caption_text", "caption_srt_url"}` for a post — both None when it ships uncaptioned.

    An unreadable row answers the same shape with Nones rather than raising: nothing gates on a
    caption, so a DB blip must not fail the read that only wants to display one.
    """
    empty = {"caption_text": None, "caption_srt_url": None}
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT caption_text, caption_srt_url FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as e:
        log_error("Could not get post captions", exc=e, post_id=post_id)
        return empty

    if not row:
        return empty
    return {"caption_text": row.get("caption_text"), "caption_srt_url": row.get("caption_srt_url")}


def update_db_post_status(post_id: int, post_status: PostStatus) -> bool:
    """Move a post to `post_status`.

    The MySQL connector cannot bind a StrEnum, so the `.value` is read first — and that read is wrapped:
    anything without a `.value` (a bare string, say) leaves the fallback in place and the post is written
    as 'posted'. Pass a real `PostStatus`.

    False means the row was not CHANGED, not that the write failed: setting a post to the status it
    already holds answers False, because the connection does not set `CLIENT.FOUND_ROWS` and MySQL
    therefore counts changed rather than matched rows.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    # The MySQL connector can't bind a PostStatus enum directly — it binds the .value string.
    status_str = "posted"
    try:
        status_str = post_status.value
    except Exception as e:
        # WARNING, not INFO: every caller passes a PostStatus, so a raw string here means the row
        # is about to be written 'posted' instead of what the caller asked for. Repeatedly is the
        # defect — some call site is ignoring the enum convention.
        log_warning("post_status was not a PostStatus — defaulting the row to 'posted'",
                    exc=e, post_id=post_id)

    try:
        cursor.execute(
            """UPDATE posts SET status = %s WHERE id = %s""",
            (status_str, post_id)
        )

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update post status", exc=e)
    finally:
        cursor.close()
        connection.close()

    return success
def get_dashboard_counts(user_id: int, week_start) -> dict:
    """Dashboard top-line counts via SQL aggregates over ALL of the user's posts. Replaces the old
    approach of counting in Python over get_posts()'s 10-oldest-posts slice (which made 'posted'
    cap near 10 and 'scheduled this week' read ~0). week_start is coerced to a naive UTC datetime so
    it compares cleanly against the naive UTC scheduled_time column (no tz TypeError).
    """
    if week_start is not None and getattr(week_start, "tzinfo", None) is not None:
        week_start = week_start.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT "
                "  COALESCE(SUM(status IN (%s,%s) AND scheduled_time >= %s), 0) AS scheduled_this_week, "
                "  COALESCE(SUM(status = %s), 0) AS pending_review, "
                "  COALESCE(SUM(status = %s), 0) AS posted_total "
                "FROM posts WHERE user_id = %s",
                (PostStatus.APPROVED.value, PostStatus.PENDING.value, week_start,
                 PostStatus.PENDING.value, PostStatus.POSTED.value, user_id))
            row = cursor.fetchone()
            return {"scheduled_this_week": int(row[0] or 0),
                    "pending_review": int(row[1] or 0),
                    "posted_total": int(row[2] or 0)}
    except mysql.connector.Error as err:
        log_error("Could not get dashboard counts", exc=err, user_id=user_id)
        return {"scheduled_this_week": 0, "pending_review": 0, "posted_total": 0}
def get_posted_posts(user_id: int):
    """Every post this user actually published, oldest first.

    None (not []) when the read failed.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, content, scheduled_time, post_type, status FROM posts WHERE user_id = %s AND status = 'posted' ORDER BY scheduled_time asc",
                (user_id,))

            posts = cursor.fetchall()
    except mysql.connector.Error as err:
        log_error(f"Could not get posted posts for user id: {user_id}", exc=err)
        posts = None

    return posts
def get_post_content(post_id: int):
    """A post's body text, or None when the post does not exist or the read failed."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT content FROM posts WHERE id = %s", (post_id,))

            post = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get post content", exc=err, post_id=post_id)
        post = False

    return post['content'] if post else None
def get_post_user_id(post_id: int):
    """Who owns a post.

    None conflates "no such post" with a failed read, so this is not by itself an authorisation answer —
    `user_owns_posts` is the fail-closed one (issue #914).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))

            post = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get post user id", exc=err, post_id=post_id)
        post = False

    return post['user_id'] if post else None
def user_owns_posts(user_id: int, post_ids: list[int]) -> bool:
    """True only when EVERY id exists AND belongs to `user_id` (issue #914).

    The post-mutating endpoints take a list of ids and used to act on it unchecked, so this is the
    authorisation read that stands between one account and another's drafts. It fails CLOSED: an
    empty list and a missing row both answer False, because "we could not prove ownership" must
    never be spelled the same way as "they own it". A database error raises `OwnershipUnprovable`
    rather than answering False — still a refusal at the call site, but a truthful one.
    """
    if not user_id or not post_ids:
        return False

    unique_ids = list({int(pid) for pid in post_ids})
    try:
        with db_cursor() as cursor:
            placeholders = ', '.join(['%s'] * len(unique_ids))
            cursor.execute(
                f"SELECT COUNT(DISTINCT id) FROM posts WHERE user_id = %s AND id IN ({placeholders})",
                [user_id, *unique_ids],
            )
            row = cursor.fetchone()
            return bool(row) and row[0] == len(unique_ids)
    except mysql.connector.Error as err:
        from cqc_lem.utilities.logger import log_error
        log_error("Could not verify post ownership", exc=err, user_id=user_id)
        raise OwnershipUnprovable(str(err)) from err
def update_db_post_image_url(post_id: int, image_url: Optional[str]) -> bool:
    """Set (or clear, with None) a post's image.

    Returns True whenever the statement ran, including when no row matched — unlike the sibling
    content/video setters, which report `rowcount`.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET image_url = %s WHERE id = %s",
                (image_url, post_id)
            )
            return True
    except mysql.connector.Error as err:
        log_error("Could not update post image_url", exc=err, post_id=post_id)
        return False
def get_post_image_url(post_id: int) -> Optional[str]:
    """A post's stored image path, or None when unset, absent or unreadable."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT image_url FROM posts WHERE id = %s", (post_id,))
            post = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get post image_url", exc=err, post_id=post_id)
        post = None

    return post['image_url'] if post else None
def get_post_video_url(post_id: int):
    """A post's stored video URL, or None when unset, absent or unreadable."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT video_url FROM posts WHERE id = %s", (post_id,))

            post = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get post video_url", exc=err, post_id=post_id)
        post = False

    return post['video_url'] if post else None
def get_post_buyer_stage(post_id: int) -> Optional[str]:
    """The buyer-journey stage the content plan assigned this post, or None when unset or unreadable."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT buyer_stage FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get buyer_stage", exc=err, post_id=post_id)
        row = None
    return row['buyer_stage'] if row else None
def get_post_content_mix(post_id: int) -> Optional[str]:
    """This post's 70/20/10 mix class as assigned by the content-plan governor (issue #618).
    None for a post planned before the governor existed (or created by hand).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT content_mix FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get content_mix", exc=err, post_id=post_id)
        row = None
    return row['content_mix'] if row else None
def get_content_mix_counts(user_id: int, days: Optional[int] = None) -> dict:
    """Planned/published post counts per 70/20/10 mix class for the analytics dashboard's mix-
    compliance ratio (issue #618). Rejected posts are excluded (they were never part of the mix the
    audience saw), unclassified posts are counted under 'unclassified'. `days` windows on
    scheduled_time (None = every post).
    """
    counts = {"unclassified": 0}
    try:
        with db_cursor() as cursor:
            window = "AND scheduled_time >= (NOW() - INTERVAL %s DAY) " if days is not None else ""
            params = (user_id, days) if days is not None else (user_id,)
            cursor.execute(
                "SELECT content_mix, COUNT(*) FROM posts "
                "WHERE user_id = %s AND status <> 'rejected' " + window +
                "GROUP BY content_mix", params)
            for mix, count in (cursor.fetchall() or []):
                key = str(mix).strip().lower() if mix else "unclassified"
                counts[key] = counts.get(key, 0) + int(count or 0)
    except mysql.connector.Error as err:
        log_error("Could not get content mix counts", exc=err, user_id=user_id)
    return counts
def get_post_type(post_id: int) -> Optional[PostType]:
    """A post's `PostType`.

    None covers three different things on purpose: no such post, a failed read, and a stored value that
    is not a member of this build's enum — the last is what a MySQL ENUM the code has not caught up to
    looks like from here.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT post_type FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get post_type", exc=err, post_id=post_id)
        row = None

    if row:
        try:
            return PostType(row['post_type'])
        except ValueError:
            return None
    return None
def parse_carousel_slides(value) -> list:
    """The stored `carousel_slides` column as a list — [] for anything that will not parse.

    One parser for both readers: the poster's `get_carousel_slides` and the nightly quality pass
    (issue #1513) must agree on what a deck's stored slides are, or a deck the poster can publish
    would score as having none.
    """
    if not value:
        return []
    try:
        slides = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return []
    return slides if isinstance(slides, list) else []
def get_carousel_slides(post_id: int) -> list[str]:
    """A post's carousel slide paths as a list — [] whenever there is nothing usable.

    The column holds JSON; a string is parsed, and anything that is not a list (or will not parse)
    collapses to []. Empty is always safe to iterate, so a malformed row degrades to "no slides" instead
    of raising into the poster. `get_post_carousel_slides` hands back the RAW column instead.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT carousel_slides FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get carousel_slides", exc=err, post_id=post_id)
        row = None

    return parse_carousel_slides(row['carousel_slides'] if row else None)
_ALLOWED_POST_CLAUSES = frozenset({"status = %s", "scheduled_time = %s", "rejection_reason = %s"})
def bulk_update_posts(post_ids: list[int], status: Optional[PostStatus] = None,
                      scheduled_time: Optional[datetime] = None,
                      rejection_reason: Optional[str] = None,
                      user_id: Optional[int] = None) -> bool:
    """`user_id` scopes the WHERE clause to one account's rows (issue #914).

    The API checks ownership before it calls this, so the scope is redundant today — that is the
    point. It closes the window between the check and the write, and it means a future caller that
    forgets the check cannot reach across accounts anyway.
    """
    if not post_ids:
        return False

    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    success = False
    try:
        sets = []
        params: list = []

        if status is not None:
            sets.append("status = %s")
            params.append(status.value)
        if scheduled_time is not None:
            sets.append("scheduled_time = %s")
            params.append(to_naive_utc(scheduled_time))
        if rejection_reason is not None:
            sets.append("rejection_reason = %s")
            params.append((rejection_reason or "").strip() or None)

        if not sets:
            return False

        for clause in sets:
            if clause not in _ALLOWED_POST_CLAUSES:
                raise ValueError(f"Disallowed SQL clause: {clause!r}")

        placeholders = ', '.join(['%s'] * len(post_ids))
        params.extend(post_ids)

        owner_clause = ""
        if user_id is not None:
            owner_clause = " AND user_id = %s"
            params.append(user_id)

        cursor.execute(
            f"UPDATE posts SET {', '.join(sets)} WHERE id IN ({placeholders}){owner_clause}",
            params
        )
        connection.commit()
        success = cursor.rowcount > 0
    except mysql.connector.Error as e:
        from cqc_lem.utilities.logger import log_error
        log_error("Could not bulk update posts", exc=e)
        success = False
    finally:
        cursor.close()
        connection.close()

    return success
def update_db_post_rejection_reason(post_id: int, rejection_reason: Optional[str],
                                    user_id: Optional[int] = None) -> bool:
    """Persist WHY a post was rejected (issue #713) so a later regeneration can avoid the same issue.

    Empty or whitespace-only input is stored as NULL so the UI doesn't render a blank reason.
    `user_id` scopes the write to one account's row for the same reason as `bulk_update_posts`
    (issue #914) — every sibling write on this table carries it.
    """
    from cqc_lem.utilities.logger import log_error
    try:
        with db_cursor(commit=True) as cursor:
            params: list = [(rejection_reason or "").strip() or None, post_id]
            owner_clause = ""
            if user_id is not None:
                owner_clause = " AND user_id = %s"
                params.append(user_id)

            cursor.execute(
                f"UPDATE posts SET rejection_reason = %s WHERE id = %s{owner_clause}",
                params
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error(f"Could not update rejection reason for post {post_id}", exc=e, post_id=post_id)
    return success
def get_post_rejection_reason(post_id: int) -> Optional[str]:
    """The persisted rejection reason for a post (issue #713), or None when it has none."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT rejection_reason FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        from cqc_lem.utilities.logger import log_error
        log_error(f"Could not get rejection reason for post {post_id}", exc=err, post_id=post_id)
        return None
def update_db_post_carousel_slides(post_id: int, slides: list[str]) -> bool:
    """Replace a post's carousel slides, stored as JSON.

    False when the write failed or no row matched.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET carousel_slides = %s WHERE id = %s",
                (json.dumps(slides), post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update carousel_slides", exc=e, post_id=post_id)
    return success
def update_db_post_shape(post_id: int, archetype: Optional[str], hook_style: Optional[str],
                         topic: Optional[str] = None) -> bool:
    """Persist the SHAPE (short-form archetype + hook style + topic) assigned to a generated post —
    the rotation history that keeps a user's next post from reusing a recently used shape (V51), and
    the topic attribution the feedback loop reads back off each captured stat row (#386).
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET archetype = %s, hook_style = %s, topic = %s WHERE id = %s",
                (archetype, hook_style, topic, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update shape", exc=e, post_id=post_id)
    return success
def update_db_post_authenticity_score(post_id: int, score: Optional[int]) -> bool:
    """Persist the authenticity gate's LLM-judged score (0-100, or NULL) for a post — the reader that
    gives the previously dead post-quality column a purpose (issue #382, V57 authenticity_score). The
    content-plan status-setter reads this back to demote a low-scoring auto-approve to PENDING.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET authenticity_score = %s WHERE id = %s",
                (score, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update authenticity score", exc=e, post_id=post_id)
    return success
def get_post_authenticity_score(post_id: int) -> Optional[int]:
    """The authenticity gate's persisted score for a post (0-100), or None when unscored (issue #382)."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT authenticity_score FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except mysql.connector.Error as err:
        log_error("Could not get authenticity score", exc=err, post_id=post_id)
        return None
def update_db_post_gate_reason(post_id: int, findings: Optional[list]) -> bool:
    """Persist WHY a post is held for review (issue #421): the quality gates' structured findings
    (see utilities/quality_gates.py) as a JSON array on posts.gate_reason. An empty/None list clears
    the column, so a post that passes on re-score stops showing a stale reason.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET gate_reason = %s WHERE id = %s",
                (json.dumps(findings) if findings else None, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update gate reason", exc=e, post_id=post_id)
    return success
def get_post_gate_reason(post_id: int) -> list:
    """The persisted quality-gate findings for a post (issue #421), or [] when it has none."""
    from cqc_lem.utilities.quality_gates import parse_gate_findings
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT gate_reason FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return parse_gate_findings(row[0] if row else None)
    except mysql.connector.Error as err:
        log_error("Could not get gate reason", exc=err, post_id=post_id)
        return []
def update_db_post_dwell_score(post_id: int, score: Optional[int]) -> bool:
    """Persist the deterministic 0-100 dwell-proxy score for a post (issue #391, dwell_score column).
    Advisory metric stored next to authenticity_score — it is never read back to gate a status, so a
    failed write only costs the datapoint.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET dwell_score = %s WHERE id = %s",
                (score, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update dwell score", exc=e, post_id=post_id)
    return success
def get_post_dwell_score(post_id: int) -> Optional[int]:
    """The persisted dwell-proxy score for a post (0-100), or None when unscored (issue #391)."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT dwell_score FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except mysql.connector.Error as err:
        log_error("Could not get dwell score", exc=err, post_id=post_id)
        return None
def update_db_post_first_comment_link(post_id: int, link: Optional[str]) -> bool:
    """Stash the external link(s) stripped from a post body at publish time (issue #392, C3) so the
    seed-comment task can deliver them in the author's first comment. Newline-separated for multiple
    links; None clears it.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET first_comment_link = %s WHERE id = %s",
                (link, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error("Could not update first comment link", exc=e, post_id=post_id)
    return success
def get_post_first_comment_link(post_id: int) -> Optional[str]:
    """The link(s) held back from a post's body for its first comment, or None (issue #392)."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT first_comment_link FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return row[0] if row and row[0] else None
    except mysql.connector.Error as err:
        log_error("Could not get first comment link", exc=err, post_id=post_id)
        return None
def get_recent_post_shape_history(user_id: int, limit: int = 10) -> list:
    """Recent posts' SHAPE history — {archetype, hook_style} dicts, most-recent first — fed to the
    shared content framework so a new post rotates away from recently used archetypes/hooks (the
    post-side twin of get_recent_newsletter_blueprint_history).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT archetype, hook_style FROM posts "
                "WHERE user_id = %s AND archetype IS NOT NULL "
                "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get post shape history", exc=err, user_id=user_id)
        return []
def get_post_archetype(post_id: int) -> Optional[str]:
    """The short-form ARCHETYPE assigned to one post (V51 `posts.archetype`). The quality gates read
    it back so the archetype-specific checks (the no-fabrication guard on a build receipt, issue
    #619) know which contract this draft was written to.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT archetype FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        log_error("Could not get archetype", exc=err, post_id=post_id)
        return None
def get_recent_post_texts(user_id: int, limit: int = 20,
                          exclude_post_id: Optional[int] = None) -> list:
    """Recent post CONTENT (pending/approved/posted, most-recent first) — the post-side dedup
    history (the newsletter's V49 subject dedup applied to posts). Feeds the opener/subject
    avoidance steering and the pre-persist similarity gate in create_text_post. Openers/subjects
    are derived from content on demand, so no new column is needed. `exclude_post_id` drops one post
    from the history — needed when re-scoring an ALREADY-SAVED post (issue #421), which would
    otherwise match itself at 100%.
    """
    try:
        with db_cursor() as cursor:
            exclude_sql = " AND id <> %s" if exclude_post_id is not None else ""
            params = ((user_id, exclude_post_id, int(limit)) if exclude_post_id is not None
                      else (user_id, int(limit)))
            cursor.execute(
                "SELECT content FROM posts "
                "WHERE user_id = %s AND content IS NOT NULL AND content <> '' "
                "AND status IN ('pending', 'approved', 'posted')"
                f"{exclude_sql} "
                "ORDER BY id DESC LIMIT %s", params)
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get recent post texts", exc=err, user_id=user_id)
        return []
def replace_video_url_base(old_base: str, new_base: str, user_id: Optional[int] = None) -> int:
    """Replace old_base URL prefix with new_base in video_url for all matching posts.

    Scoped to user_id when provided. Returns count of updated rows.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if user_id is not None:
                cursor.execute(
                    "UPDATE posts SET video_url = REPLACE(video_url, %s, %s) "
                    "WHERE video_url LIKE %s AND user_id = %s",
                    (old_base, new_base, f"{old_base}%", user_id)
                )
            else:
                cursor.execute(
                    "UPDATE posts SET video_url = REPLACE(video_url, %s, %s) WHERE video_url LIKE %s",
                    (old_base, new_base, f"{old_base}%")
                )
            updated = cursor.rowcount
    except mysql.connector.Error as e:
        updated = 0
        log_error("Could not replace video URL base", exc=e)
    return updated
def get_ready_to_post_posts(pre_post_time: datetime = None, post_time_delta_minutes=20) -> list:
    """Query the database for any pending posts that are scheduled to post now or earlier.

    Answers `[]` — never None — on a read failure, because the single caller (`run_scheduler`'s
    every-10-minutes publishing beat) iterates the result directly and a None crashed it with a
    TypeError that masked the real mysql error. No post is lost by answering empty: the query's own
    24h lookback plus `get_orphaned_scheduled_posts` recover anything missed on the next tick.
    """
    now = datetime.now(timezone.utc)
    if pre_post_time is None:
        # Get time for post_time_delta after now
        pre_post_time = now + timedelta(minutes=post_time_delta_minutes)

    yesterday = now - timedelta(days=1)

    log_info(f"Getting post between : {yesterday} and {pre_post_time} (UTC)")

    try:
        with db_cursor() as cursor:
        # Get posts that have scheduled time between 24 hours ago and the pre_post_time
            # manual_publish rows are drafted for LinkedIn's native occasion composer, which has no
            # API entity (issue #1074) — the author publishes them by hand, so the scheduler must
            # never see one. Excluding them HERE makes that true for every consumer of this query.
            cursor.execute(
                """SELECT p.id, p.scheduled_time, p.user_id
                    FROM posts AS p
                    WHERE status = 'approved' AND manual_publish = 0
                      AND scheduled_time BETWEEN %s AND %s
                    ORDER BY scheduled_time ASC
                    """,
                (yesterday, pre_post_time,))
            posts = cursor.fetchall()
            # A non-empty poll is a real state transition worth keeping at INFO; an empty one is the
            # scheduler idling and was 220 identical rows in 48h of PostHog Logs.
            ready = [post[0] for post in posts]
            if ready:
                log_info(f"Posts ready to post: {ready}")
            else:
                log_debug("Posts ready to post: []")
    except mysql.connector.Error as err:
        log_error("Could not read the ready-to-post queue", exc=err,
                  task_name="auto_check_scheduled_posts")
        posts = []

    return posts
def get_orphaned_scheduled_posts(lookback_hours: int = 2) -> list:
    """Return posts stuck in 'scheduled' status that never reached 'posted'.

    These arise when Celery tasks are purged on container restart while a post
    has already been transitioned from 'approved' → 'scheduled'. Without this
    recovery query, those posts stay stuck forever.

    A `manual_publish` post is excluded for the same reason it is excluded upstream: only
    `auto_check_scheduled_posts` writes 'scheduled', and it never sees one — so a manual-publish row
    in that state is a bug, and re-queueing it would publish through the API the very post that
    exists because the API cannot carry it (issue #1074).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)

    try:
        with db_cursor() as cursor:
            cursor.execute(
                """SELECT p.id, p.scheduled_time, p.user_id
                   FROM posts AS p
                   WHERE status = 'scheduled' AND manual_publish = 0
                     AND scheduled_time <= %s
                   ORDER BY scheduled_time ASC""",
                (cutoff,),
            )
            posts = cursor.fetchall()
            # Orphans found means the queue lost work — that stays at INFO. Finding none is the healthy
            # case and was 221 identical rows in 48h.
            orphaned = [p[0] for p in posts]
            if orphaned:
                log_info(f"Orphaned scheduled posts to re-queue: {orphaned}")
            else:
                log_debug("Orphaned scheduled posts to re-queue: []")
    except mysql.connector.Error as err:
        log_error("Could not get orphaned scheduled posts", exc=err)
        posts = []

    return posts
def get_post_type_counts(user_id: int):
    """Query the database to get the count of each post_type in the 'posts' table for the given user id."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT post_type, COUNT(*) AS count FROM posts WHERE user_id = %s GROUP BY post_type",
                           (user_id,))
            post_counts = {row['post_type']: row['count'] for row in cursor.fetchall()}
    except mysql.connector.Error as err:
        log_error("Could not get post type counts", exc=err)
        post_counts = {}

    return post_counts
# A post counts against the buffer once its content exists: pending (awaiting approval), approved
# (queued) and scheduled (dispatched, not yet posted) are all "ready" and must not be re-generated.
READY_POST_STATUSES = ('pending', 'approved', 'scheduled')
def count_ready_posts_within_buffer(user_id: int, days: int = DEFAULT_CONTENT_BUFFER_DAYS) -> int:
    """Count posts that already have generated content due within the next `days` days."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM posts"
                " WHERE user_id = %s"
                f" AND status IN ({', '.join(['%s'] * len(READY_POST_STATUSES))})"
                " AND scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY",
                (user_id, *READY_POST_STATUSES, int(days)),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count ready posts within buffer", exc=err, user_id=user_id)
        return 0
def get_planned_posts_within_buffer(user_id: int,
                                    days: int = DEFAULT_CONTENT_BUFFER_DAYS,
                                    max_posts: int = DEFAULT_CONTENT_BUFFER_MAX_POSTS,
                                    already_ready_count: int = 0) -> list[dict]:
    """Return the status=planning posts to generate now to top the buffer back up.

    Posts due within the next `days` days, soonest first, limited to
    `max_posts - already_ready_count` so we only fill the delta and never overshoot the cap.
    """
    limit = int(max_posts) - int(already_ready_count)
    if limit <= 0:
        return []

    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                # scheduled_time rides along so the generator can resolve the slot's day type
                # (issue #621) — the weekday IS the calendar key.
                "SELECT user_id, id, post_type, buyer_stage, content_mix, scheduled_time FROM posts"
                " WHERE status = 'planning' AND user_id = %s"
                " AND scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY"
                " ORDER BY scheduled_time ASC, id ASC LIMIT %s",
                (user_id, int(days), limit),
            )
            planned_content = cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get planned posts within buffer", exc=err, user_id=user_id)
        planned_content = []

    return planned_content
def get_next_planned_posts_after_buffer(user_id: int, days: int, limit: int) -> list[dict]:
    """The soonest status=planning posts due BEYOND the buffer window, soonest first (issue #719).

    The pull-forward list for an explicitly requested run: when the window holds no planning rows
    (every near-term slot was posted or rejected, and rejected slots are never re-planned) the
    Generate button would otherwise no-op forever. Forward-only — a planning row already in the
    past is a stale slot, and generating content for a time that has passed would publish it
    immediately.
    """
    limit = int(limit)
    if limit <= 0:
        return []

    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT user_id, id, post_type, buyer_stage, content_mix, scheduled_time FROM posts"
                " WHERE status = 'planning' AND user_id = %s"
                " AND scheduled_time > NOW() + INTERVAL %s DAY"
                " ORDER BY scheduled_time ASC, id ASC LIMIT %s",
                (user_id, int(days), limit),
            )
            planned_content = cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get planned posts after buffer", exc=err, user_id=user_id)
        planned_content = []

    return planned_content
def get_next_planned_post_date(user_id: int) -> Optional[datetime]:
    """When this user's soonest UPCOMING planning slot is due, or None when nothing is planned.

    Feeds the "nothing to generate right now" explanation (issue #719) — without a date the SPA
    can only say a run produced nothing, which reads as a broken feature.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT MIN(scheduled_time) FROM posts"
                " WHERE status = 'planning' AND user_id = %s AND scheduled_time > NOW()",
                (user_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        log_error("Could not get next planned post date", exc=err, user_id=user_id)
        return None
def get_user_ids_with_planned_posts_within_buffer(days: int = MAX_CONTENT_BUFFER_DAYS) -> list[int]:
    """User IDs that have any status=planning post due within the next `days` days.

    Defaults to the max window so a user with a longer configured buffer is never missed by the
    beat's user discovery; the per-user window is applied by get_planned_posts_within_buffer.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT user_id FROM posts"
                " WHERE status = 'planning'"
                " AND scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY"
                " ORDER BY user_id",
                (int(days),),
            )
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get user ids with planned posts within buffer", exc=err)
        return []
def get_last_planned_post_date_for_user(user_id: int):
    """Query the database to get the last planned post date for the given user."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT MAX(scheduled_time) AS last_planned_date FROM posts "
                "WHERE user_id = %s AND status != 'rejected'",
                (user_id,))
            last_planned_date = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get last planned post date for user", exc=err)
        last_planned_date = None

    return last_planned_date[0] if last_planned_date else None
def get_post_status(post_id: int) -> str | None:
    """Return the current status string of a post, or None if not found."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT status FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get post status", exc=err)
        row = None
    return row[0] if row else None
def get_engager_candidates(user_id: int, days: int = 30) -> list:
    """People who recently engaged with the user's OWN posts, as connection-targeting candidates:
    [{'person_name', 'person_profile_url', 'connection_degree', 'occurred_at'}]. Only rows with a
    profile URL — without one there is nobody to invite. Read from post_engagers, so this costs no
    scraping. `connection_degree` lets the caller drop people we're already connected to (#623).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT engager_name AS person_name, engager_profile_url AS person_profile_url, "
                "connection_degree, last_engaged_at AS occurred_at FROM post_engagers "
                "WHERE user_id=%s AND engager_profile_url IS NOT NULL "
                "AND last_engaged_at >= (NOW() - INTERVAL %s DAY) ORDER BY last_engaged_at DESC",
                (user_id, days))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not read engager candidates", exc=err, user_id=user_id)
        return []
def has_scheduled_post_today(user_id: int) -> bool:
    """True if the user has a post going out today (UTC) — those days are already covered by the
    pre-post commenting trigger, so the standalone daily engagement run should skip them. Fails
    safe to True (skip the standalone run) so an error never causes double-commenting.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM posts WHERE user_id=%s AND DATE(scheduled_time)=UTC_DATE() "
                "AND status IN ('approved','scheduled','posted')", (user_id,))
            r = cursor.fetchone()
            return bool(r and r[0])
    except mysql.connector.Error as err:
        log_error("Could not check today's posts", exc=err, user_id=user_id)
        return True
def upsert_engager(user_id: int, engager_name: str, engager_profile_url: str = None,
                   connection_degree: str = None) -> bool:
    """Record that `engager_name` engaged with the user's post (or refresh their last-engaged
    time). No-op on a blank name or if the table isn't present yet. `connection_degree` is the
    scraped badge ('1st'/'2nd'/'3rd+', issue #623) — COALESCEd, so a later sighting that rendered no
    badge never erases a degree we already know.
    """
    if not engager_name or not engager_name.strip():
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO post_engagers (user_id, engager_name, engager_profile_url, "
                "connection_degree, last_engaged_at) VALUES (%s,%s,%s,%s,NOW()) ON DUPLICATE KEY UPDATE "
                "engager_profile_url=COALESCE(VALUES(engager_profile_url), engager_profile_url), "
                "connection_degree=COALESCE(VALUES(connection_degree), connection_degree), "
                "last_engaged_at=NOW()",
                (user_id, engager_name.strip()[:255], (engager_profile_url or None),
                 (connection_degree or None)))
            return True
    except mysql.connector.Error as err:
        log_error("Could not upsert engager", exc=err, user_id=user_id)
        return False
def get_recent_engagers(user_id: int, days: int = 14) -> set:
    """Lowercased names of people who recently commented on the user's OWN posts — reciprocity
    targets to prioritize commenting back on. Empty set if the tracking table isn't present yet.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT LOWER(engager_name) FROM post_engagers "
                "WHERE user_id=%s AND last_engaged_at >= (NOW() - INTERVAL %s DAY)",
                (user_id, days))
            return {r[0] for r in cursor.fetchall() if r and r[0]}
    except mysql.connector.Error:
        return set()
def get_shipped_content_for_quality(user_id: int, days: int = 1) -> list:
    """Everything the user SHIPPED in the last `days`, across all three writing surfaces, as the input
    to the nightly content-quality scoring pass (issue #630).

    One function and one connection for three queries on purpose: the scorer treats posts, comments and
    newsletter editions as one stream of writing, and three separate readers would let a surface drift
    out of the window silently. Each row is
    ``{surface, ref_id, text, shipped_on, format_key, post_type, video_url, video_model,
    carousel_slides, authenticity_score, reactions, comments, reposts, impressions}`` — the
    engagement fields are None for a surface that has no per-item stats (comments, newsletters) and
    for a post whose stats have not been captured yet, which is the normal case the night it ships.
    `post_type`, `video_url`, `video_model` and `carousel_slides` are present only for posts; they are
    None/[] for comments and newsletters. `video_model` is the model the render actually used (issue
    #1410), and is None for every post that shipped before it was recorded — the scorer falls back to
    the coarse tier read off the URL.
    """
    window = max(1, int(days))
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    rows: list = []
    try:
        # LEFT JOIN: a post shipped tonight has no post_stats row yet and must still be scored — its
        # engagement rate simply reports as unmeasured until the daily scrape catches up.
        cursor.execute(
            "SELECT p.id, p.content, p.archetype, p.post_type, p.video_url, p.video_model, "
            "  p.carousel_slides, p.authenticity_score, DATE(p.scheduled_time) AS shipped_on, "
            "  s.reactions, s.comments, s.reposts, s.impressions "
            "FROM posts p LEFT JOIN post_stats s "
            "  ON s.post_id=p.id AND s.user_id=p.user_id "
            "  AND s.id IN (SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id) "
            "WHERE p.user_id=%s AND p.status=%s AND p.content IS NOT NULL AND p.content <> '' "
            "  AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) "
            "ORDER BY p.scheduled_time DESC",
            (user_id, user_id, PostStatus.POSTED.value, window))
        for r in (cursor.fetchall() or []):
            rows.append({
                "surface": "post", "ref_id": str(r["id"]), "text": r["content"],
                "shipped_on": r["shipped_on"], "format_key": r.get("archetype"),
                "post_type": r.get("post_type"), "video_url": r.get("video_url"),
                "video_model": r.get("video_model"),
                "carousel_slides": parse_carousel_slides(r.get("carousel_slides")),
                "authenticity_score": r.get("authenticity_score"),
                "reactions": r.get("reactions"), "comments": r.get("comments"),
                "reposts": r.get("reposts"), "impressions": r.get("impressions"),
            })

        cursor.execute(
            "SELECT id, message, DATE(created_at) AS shipped_on FROM logs "
            "WHERE user_id=%s AND action_type=%s AND result=%s "
            "  AND message IS NOT NULL AND message <> '' "
            "  AND created_at >= (NOW() - INTERVAL %s DAY) ORDER BY id DESC",
            (user_id, LogActionType.COMMENT.value, LogResultType.SUCCESS.value, window))
        for r in (cursor.fetchall() or []):
            rows.append({
                "surface": "comment", "ref_id": str(r["id"]), "text": r["message"],
                "shipped_on": r["shipped_on"], "format_key": None,
                "authenticity_score": None, "reactions": None, "comments": None,
                "reposts": None, "impressions": None,
            })

        # published_at can be NULL on a row marked published by an older path; scheduled_for is the
        # intended ship day and is NOT NULL, so it is the fallback rather than dropping the edition.
        cursor.execute(
            "SELECT id, body, `format`, DATE(COALESCE(published_at, scheduled_for)) AS shipped_on "
            "FROM newsletter_editions "
            "WHERE user_id=%s AND status='published' AND body IS NOT NULL AND body <> '' "
            "  AND COALESCE(published_at, scheduled_for) >= (NOW() - INTERVAL %s DAY) "
            "ORDER BY id DESC",
            (user_id, window))
        for r in (cursor.fetchall() or []):
            rows.append({
                "surface": "newsletter", "ref_id": str(r["id"]), "text": r["body"],
                "shipped_on": r["shipped_on"], "format_key": r.get("format"),
                "authenticity_score": None, "reactions": None, "comments": None,
                "reposts": None, "impressions": None,
            })
        return rows
    except mysql.connector.Error as err:
        log_error("Could not get shipped content", exc=err, user_id=user_id)
        return rows
    finally:
        cursor.close()
        connection.close()
def record_content_quality_score(user_id: int, score: dict) -> bool:
    """Persist ONE scored piece of content (issue #630). Upsert on (user_id, surface, ref_id) so a
    re-run of the nightly pass refreshes the reading instead of double-counting it — which is what
    makes the weekly rollup's week-over-week comparison stable.

    Every measured column is nullable and is written as NULL when the dimension was not measured; a 0
    would read as "clean" or "no reach" instead of "not scored".
    """
    score = dict(score or {})
    ref_id = str(score.get("ref_id") or "").strip()
    if not ref_id:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO content_quality_scores (user_id, surface, ref_id, shipped_on, slop_hard, "
                "  slop_warn, slop_score, similarity, similarity_measure, authenticity_score, "
                "  hook_chars, hook_within_budget, engagement_rate, impressions, detector_score, "
                "  detector_provider, checks, video_render_ok, video_model_tier, "
                "  video_duration_seconds, video_aspect_ratio, video_asset_probe) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE shipped_on=VALUES(shipped_on), slop_hard=VALUES(slop_hard), "
                "  slop_warn=VALUES(slop_warn), slop_score=VALUES(slop_score), "
                "  similarity=VALUES(similarity), similarity_measure=VALUES(similarity_measure), "
                "  authenticity_score=VALUES(authenticity_score), hook_chars=VALUES(hook_chars), "
                "  hook_within_budget=VALUES(hook_within_budget), "
                "  engagement_rate=VALUES(engagement_rate), impressions=VALUES(impressions), "
                "  detector_score=VALUES(detector_score), detector_provider=VALUES(detector_provider), "
                "  checks=VALUES(checks), video_render_ok=VALUES(video_render_ok), "
                "  video_model_tier=VALUES(video_model_tier), "
                "  video_duration_seconds=VALUES(video_duration_seconds), "
                "  video_aspect_ratio=VALUES(video_aspect_ratio), "
                "  video_asset_probe=VALUES(video_asset_probe), scored_at=CURRENT_TIMESTAMP",
                (user_id, str(score.get("surface") or "")[:20], ref_id[:64], score.get("shipped_on"),
                 score.get("slop_hard"), score.get("slop_warn"), score.get("slop_score"),
                 score.get("similarity"),
                 (str(score.get("similarity_measure"))[:16] if score.get("similarity_measure") else None),
                 score.get("authenticity_score"), score.get("hook_chars"),
                 (None if score.get("hook_within_budget") is None
                  else int(bool(score.get("hook_within_budget")))),
                 score.get("engagement_rate"), score.get("impressions"), score.get("detector_score"),
                 (str(score.get("detector_provider"))[:32] if score.get("detector_provider") else None),
                 json.dumps(score.get("slop_checks") or []),
                 (None if score.get("video_render_ok") is None
                  else int(bool(score.get("video_render_ok")))),
                 (str(score.get("video_model_tier"))[:16] if score.get("video_model_tier") else None),
                 score.get("video_duration_seconds"),
                 (str(score.get("video_aspect_ratio"))[:16] if score.get("video_aspect_ratio") else None),
                 (str(score.get("video_asset_probe"))[:16] if score.get("video_asset_probe") else None)))
            return True
    except mysql.connector.Error as err:
        log_error("Could not record content quality score", exc=err, user_id=user_id)
        return False
def get_content_quality_scores(user_id: int, days: int = 14) -> list:
    """Scored content rows shipped in the last `days`, newest first — the input to the weekly rollup
    and the analytics panel (issue #630). The rollup needs TWO periods, so callers pass twice their
    comparison window.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT surface, ref_id, shipped_on, slop_hard, slop_warn, slop_score, similarity, "
                "  similarity_measure, authenticity_score, hook_chars, hook_within_budget, "
                "  engagement_rate, impressions, detector_score, detector_provider, "
                "  video_render_ok, video_model_tier, video_duration_seconds, video_aspect_ratio, "
                "  video_asset_probe, scored_at "
                "FROM content_quality_scores "
                "WHERE user_id=%s AND shipped_on >= (CURDATE() - INTERVAL %s DAY) "
                "ORDER BY shipped_on DESC, id DESC",
                (user_id, max(1, int(days))))
            rows = cursor.fetchall() or []
            return [
                {**r,
                 "slop_score": float(r["slop_score"]) if r.get("slop_score") is not None else None,
                 "similarity": float(r["similarity"]) if r.get("similarity") is not None else None,
                 "engagement_rate": (float(r["engagement_rate"])
                                     if r.get("engagement_rate") is not None else None),
                 "video_render_ok": bool(r["video_render_ok"]) if r.get("video_render_ok") is not None else None}
                for r in rows
            ]
    except mysql.connector.Error:
        return []  # table not created yet (or unreadable) — the rollup reports an empty window
def record_post_stats(user_id: int, post_id: int, reactions: Optional[int], comments: Optional[int],
                      reposts: Optional[int] = 0, impressions: Optional[int] = None,
                      saves: Optional[int] = 0) -> bool:
    """Append one engagement snapshot for a post, with the post's SHAPE copied in beside the numbers.

    archetype / hook_style / format / topic / buyer_stage are snapshotted from `posts` at capture time so
    the feedback loop (issue #386) still knows which shape earned these numbers after the post is edited.
    That SELECT is scoped to `(post_id, user_id)`; when it matches nothing the stats row is still written,
    with those columns NULL.
    """
    try:
        with db_cursor(commit=True) as cursor:
        # Snapshot the post's content attributes at capture time so the feedback loop (#386) can
        # learn which shape/topic earned engagement even if the post is later edited.
            cursor.execute(
                "SELECT archetype, hook_style, post_type, topic, buyer_stage "
                "FROM posts WHERE id=%s AND user_id=%s",
                (post_id, user_id))
            row = cursor.fetchone()
            archetype, hook_style, fmt, topic, buyer_stage = row if row else (None, None, None, None, None)
            cursor.execute(
                "INSERT INTO post_stats (user_id, post_id, reactions, comments, reposts, impressions, "
                "saves, archetype, hook_style, `format`, topic, buyer_stage) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, post_id, int(reactions or 0), int(comments or 0), int(reposts or 0),
                 impressions, int(saves or 0), archetype, hook_style, fmt, topic, buyer_stage))
            return True
    except mysql.connector.Error as err:
        log_error("Could not record post stats", exc=err, user_id=user_id)
        return False
def get_latest_post_stats(user_id: int, post_id: int) -> Optional[dict]:
    """The most recent captured counts for one post, or None when nothing was ever captured.

    `impressions` stays NULL when the capture never read one — the API probe (#645) grades a
    signal it cannot compare as ungraded rather than as a disagreement.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT reactions, comments, reposts, impressions, saves, captured_at "
                "FROM post_stats WHERE user_id=%s AND post_id=%s ORDER BY id DESC LIMIT 1",
                (user_id, post_id))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not read post stats for user {user_id} post {post_id}", exc=err)
        return None
def get_recent_posted_post_ids(user_id: int, days: int = 21) -> list:
    """Ids of posts published in the last `days`, FRESHEST first.

    The ordering is the budget policy, not a display choice — see the note in the body. [] on a read
    error, silently.
    """
    try:
        with db_cursor() as cursor:
        # Freshest first: the reply sweep prioritizes golden-hour posts, so a rate-limited or
        # capped session spends its budget on the posts still being distributed (#401).
            cursor.execute(
                "SELECT id FROM posts WHERE user_id=%s AND status='posted' "
                "AND scheduled_time >= (NOW() - INTERVAL %s DAY) ORDER BY scheduled_time DESC", (user_id, days))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error:
        return []
def get_uncaptured_posted_post_ids(user_id: int, days: int = 90, limit: int = 5) -> list:
    """Posted posts inside the ANALYTICS window that have no `post_stats` row at all (issue #809).

    The stats sweep only walks `get_recent_posted_post_ids`' short window, but the dashboard reads
    90 days — a post whose capture was missed while it was fresh (automation paused, no permalink
    logged yet, a 429) could never be measured afterwards, which is why the analytics rendered a
    shrinking subset of the account's posts. Newest first and capped, so topping the sweep up costs
    a bounded number of extra page loads.

    Only posts with a logged permalink are offered. The sweep can do nothing with the others, and
    since a post leaves this set only by GAINING a stat row, a handful of permalink-less posts at
    the head of the window would otherwise hold every slot of the cap on every run — the backfill
    would report as working while never reaching a post it could actually capture.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT p.id FROM posts p "
                "LEFT JOIN post_stats s ON s.post_id = p.id AND s.user_id = p.user_id "
                "WHERE p.user_id = %s AND p.status = %s "
                "AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) AND s.id IS NULL "
                "AND EXISTS (SELECT 1 FROM logs l WHERE l.user_id = p.user_id AND l.post_id = p.id "
                "AND l.action_type = %s AND l.result = %s "
                "AND l.post_url IS NOT NULL AND l.post_url <> '') "
                "ORDER BY p.scheduled_time DESC LIMIT %s",
                (user_id, PostStatus.POSTED.value, days, LogActionType.POST.value,
                 LogResultType.SUCCESS.value, max(0, int(limit))))
            return [r[0] for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not get uncaptured posted post ids", exc=err, user_id=user_id)
        return []
def get_post_coverage_counts(user_id: int, days: int = 90) -> dict:
    """How much of the account the analytics dashboard is looking at (issue #809).

    Three unrelated denominators used to share one screen with no way to reconcile them: the
    all-time "posted" tile, the content-mix window, and the per-post table (which only sees posts
    with a captured `post_stats` row). This returns the two POST-side numbers — all-time posted and
    posted within the analytics window — so the UI can say WHY it is showing a subset instead of
    reading as broken. The measured count stays with the stats read (`get_post_performance_rows`),
    so the panel can never contradict its own sample size.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(status = %s), 0), "
                "COALESCE(SUM(status = %s AND scheduled_time >= (NOW() - INTERVAL %s DAY)), 0) "
                "FROM posts WHERE user_id = %s",
                (PostStatus.POSTED.value, PostStatus.POSTED.value, days, user_id))
            row = cursor.fetchone() or (0, 0)
            return {"posted_total": int(row[0] or 0), "posted_in_window": int(row[1] or 0)}
    except mysql.connector.Error as err:
        log_error("Could not get post coverage counts", exc=err, user_id=user_id)
        return {"posted_total": 0, "posted_in_window": 0}
def get_post_engagement_rows(user_id: int) -> list:
    """Latest stats per post joined with when it was posted → rows of
    (scheduled_time, reactions, comments, reposts, archetype, hook_style, format, topic,
    buyer_stage, impressions) for post-time and content-attribution analysis (#386). The
    attribution columns are the snapshot captured on the stat row, so they reflect the post as it
    was when scraped. `impressions` may be NULL (only the author's own view exposes it) — it
    trails the tuple so index-based readers of the older shape keep working, and it lets
    `post_stats` score by engagement RATE when coverage is complete (#388).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT p.scheduled_time, s.reactions, s.comments, s.reposts, "
                "s.archetype, s.hook_style, s.`format`, s.topic, s.buyer_stage, s.impressions "
                "FROM posts p JOIN post_stats s ON s.post_id=p.id AND s.user_id=p.user_id "
                "WHERE p.user_id=%s AND s.id IN "
                "(SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id)",
                (user_id, user_id))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get post engagement rows", exc=err, user_id=user_id)
        return []
def get_shape_performance(user_id: int, days: int = 90) -> dict:
    """Per-SHAPE engagement totals for a user's recently posted content — the outcomes side of the
    performance→content feedback loop (issue #389 / B4). Joins each posted post's assigned shape
    (`posts.archetype` = short-form FORMAT key, `posts.hook_style`) with its LATEST captured
    `post_stats` row and aggregates raw signal counts per shape key.

    Returns ``{"format": {archetype: agg}, "hook": {hook_style: agg}}`` where each ``agg`` is
    ``{"samples", "reactions", "comments", "reposts", "impressions", "impression_samples"}``.
    ``impressions`` sums only rows where impressions is non-NULL (``impression_samples`` counts
    them) so the caller can tell whether impression-normalized scoring is available yet (B2/B3).
    The engagement-metric/weighting policy lives in ``content_framework``; this stays pure access.
    """
    result = {"format": {}, "hook": {}}
    try:
        with db_cursor() as cursor:
            for column, bucket in (("archetype", "format"), ("hook_style", "hook")):
                cursor.execute(
                    f"SELECT p.{column}, COUNT(*), "
                    "COALESCE(SUM(s.reactions),0), COALESCE(SUM(s.comments),0), "
                    "COALESCE(SUM(s.reposts),0), COALESCE(SUM(s.impressions),0), "
                    "SUM(CASE WHEN s.impressions IS NOT NULL THEN 1 ELSE 0 END) "
                    "FROM posts p JOIN post_stats s "
                    "ON s.post_id=p.id AND s.user_id=p.user_id "
                    f"WHERE p.user_id=%s AND p.status='posted' AND p.{column} IS NOT NULL "
                    "AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) "
                    "AND s.id IN (SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id) "
                    f"GROUP BY p.{column}",
                    (user_id, days, user_id))
                for key, samples, reactions, comments, reposts, impressions, imp_samples in cursor.fetchall():
                    result[bucket][key] = {
                        "samples": int(samples or 0),
                        "reactions": int(reactions or 0),
                        "comments": int(comments or 0),
                        "reposts": int(reposts or 0),
                        "impressions": int(impressions or 0),
                        "impression_samples": int(imp_samples or 0),
                    }
            return result
    except mysql.connector.Error as err:
        log_error("Could not get shape performance", exc=err, user_id=user_id)
        return {"format": {}, "hook": {}}
def get_post_performance_rows(user_id: int, days: Optional[int] = None) -> list:
    """Latest captured stat per POSTED post as attribution-tagged dicts for the analytics
    dashboard (issue #395) — the per-post performance table and the engagement-rate/impression
    trend both read this. Like ``get_post_engagement_rows`` it keeps only the newest stat row per
    post (``MAX(id)``), but returns dicts carrying ``post_id`` and ``saves`` so the UI can key each
    row and surface the save signal (#387). ``impressions`` may be NULL (only the author's own view
    exposes it). ``days`` optionally windows to posts scheduled within the last N days (None = all),
    newest first.
    """
    try:
        with db_cursor() as cursor:
            window = "AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) " if days is not None else ""
            params = (user_id, user_id, days) if days is not None else (user_id, user_id)
            cursor.execute(
                "SELECT p.id, p.scheduled_time, s.reactions, s.comments, s.reposts, s.impressions, "
                "s.saves, s.archetype, s.hook_style, s.`format`, s.topic, s.buyer_stage "
                "FROM posts p JOIN post_stats s ON s.post_id=p.id AND s.user_id=p.user_id "
                "WHERE p.user_id=%s AND p.status='posted' "
                "AND s.id IN (SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id) "
                + window +
                "ORDER BY p.scheduled_time DESC",
                params)
            return [
                {"post_id": r[0], "scheduled_time": r[1], "reactions": r[2], "comments": r[3],
                 "reposts": r[4], "impressions": r[5], "saves": r[6], "archetype": r[7],
                 "hook_style": r[8], "format": r[9], "topic": r[10], "buyer_stage": r[11]}
                for r in (cursor.fetchall() or [])
            ]
    except mysql.connector.Error as err:
        log_error("Could not get post performance rows", exc=err, user_id=user_id)
        return []
def record_shipped_variant(user_id: int, post_id: int, variant_key: str,
                           combo: Optional[dict] = None, batch_id: Optional[str] = None,
                           variant_index: Optional[int] = None) -> bool:
    """Persist which A/B variant actually SHIPPED for a post (issue #396 / D2) so its realized
    `post_stats` can be attributed back to that variant when picking winners. One row per post —
    re-recording overwrites. `combo` is stored as JSON for provenance.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO post_variants (user_id, post_id, batch_id, variant_index, variant_key, combo) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE batch_id=VALUES(batch_id), variant_index=VALUES(variant_index), "
                "variant_key=VALUES(variant_key), combo=VALUES(combo), shipped_at=CURRENT_TIMESTAMP",
                (user_id, post_id, batch_id, variant_index, variant_key,
                 json.dumps(combo, default=str) if combo is not None else None))
            return True
    except mysql.connector.Error as err:
        log_error("Could not record shipped variant", exc=err, user_id=user_id)
        return False
def get_shipped_variant_keys(user_id: int) -> dict:
    """``{post_id: variant_key}`` for every A/B variant this user has SHIPPED (issue #396).

    Read once per stats sweep so each `post_outcome` event can carry the variant it belongs to
    (issue #652) — the per-post alternative would be one query per post inside the Selenium loop.
    An empty dict on any DB error: a missing experiment label must never cost us the outcome.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT post_id, variant_key FROM post_variants WHERE user_id=%s", (user_id,))
            return {r[0]: r[1] for r in (cursor.fetchall() or []) if r[1]}
    except mysql.connector.Error as err:
        log_error("Could not get shipped variant keys", exc=err, user_id=user_id)
        return {}
def get_post_types_for_user(user_id: int) -> dict:
    """``{post_id: post_type}`` for every post this user has (issue #1513).

    This is the format each `post_outcome` event reports.

    Read ONCE per stats sweep, the same way `get_shipped_variant_keys` is, so the format rides along
    without a query per post inside the Selenium loop. An empty dict on any DB error: a missing
    format label must never cost us the outcome itself.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT id, post_type FROM posts WHERE user_id=%s", (user_id,))
            return {r[0]: r[1] for r in (cursor.fetchall() or []) if r[1]}
    except mysql.connector.Error as err:
        log_error("Could not get post types", exc=err, user_id=user_id)
        return {}
def get_variant_outcome_rows(user_id: int) -> list:
    """Realized outcomes for shipped A/B variants (issue #396 / D2). Joins each recorded shipped
    variant (`post_variants`) with its post's LATEST captured `post_stats` row → dicts of
    ``{variant_key, scheduled_time, reactions, comments, reposts, impressions}`` that feed
    ``post_stats.select_variant_winners``. `impressions` may be NULL (only the author's own view
    exposes it), so winner selection falls back to raw counts until coverage is complete.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT v.variant_key, p.scheduled_time, s.reactions, s.comments, s.reposts, s.impressions "
                "FROM post_variants v "
                "JOIN posts p ON p.id=v.post_id AND p.user_id=v.user_id "
                "JOIN post_stats s ON s.post_id=v.post_id AND s.user_id=v.user_id "
                "WHERE v.user_id=%s AND s.id IN "
                "(SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id)",
                (user_id, user_id))
            return [
                {"variant_key": r[0], "scheduled_time": r[1], "reactions": r[2],
                 "comments": r[3], "reposts": r[4], "impressions": r[5]}
                for r in (cursor.fetchall() or [])
            ]
    except mysql.connector.Error as err:
        log_error("Could not get variant outcome rows", exc=err, user_id=user_id)
        return []
def record_follower_stat(user_id: int, follower_count: Optional[int] = None,
                         connection_count: Optional[int] = None,
                         profile_views: Optional[int] = None,
                         search_appearances: Optional[int] = None) -> bool:
    """Append one audience snapshot for the user (issue #627). Every count is optional: a value the
    capture could not read is stored as NULL, never 0, so the growth deltas can tell "not measured"
    apart from "the audience really is that size". Returns False when NOTHING was readable — there
    is no point writing an all-NULL row that only adds noise to the series.
    """
    if all(v is None for v in (follower_count, connection_count, profile_views, search_appearances)):
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO follower_stats (user_id, follower_count, connection_count, profile_views, "
                "search_appearances) VALUES (%s, %s, %s, %s, %s)",
                (user_id, follower_count, connection_count, profile_views, search_appearances))
            return cursor.rowcount == 1
    except mysql.connector.Error as err:
        log_error("Could not record follower stat", exc=err, user_id=user_id)
        return False
def get_follower_stats(user_id: int, days: Optional[int] = None, limit: int = 400) -> list:
    """The user's audience snapshots, most recent first (issue #627). `days` optionally windows to
    captures within the last N days. Each item:
    id, follower_count, connection_count, profile_views, search_appearances, captured_at.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            window = "AND captured_at >= (NOW() - INTERVAL %s DAY) " if days is not None else ""
            params = (user_id, days, limit) if days is not None else (user_id, limit)
            cursor.execute(
                "SELECT id, follower_count, connection_count, profile_views, search_appearances, "
                "captured_at FROM follower_stats WHERE user_id = %s " + window +
                "ORDER BY captured_at DESC, id DESC LIMIT %s", params)
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get follower stats", exc=err, user_id=user_id)
        return []
def get_post_video_quality(post_id: int) -> str:
    """A post's video-quality tier.

    Every unknown answer — column unset, no such post, failed read — is 'standard': the tier that costs
    credits is never something we assume.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT video_quality FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return (row["video_quality"] if row and row.get("video_quality") else "standard")
    except mysql.connector.Error as err:
        log_error("Could not get video_quality", exc=err, post_id=post_id)
        return "standard"
def update_post_video_quality(post_id: int, quality: str) -> bool:
    """Set a post's video-quality tier.

    False when no row matched or the value stored was already this one.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute("UPDATE posts SET video_quality = %s WHERE id = %s", (quality, post_id))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not update video_quality", exc=err, post_id=post_id)
        return False
def get_post_carousel_slides(post_id: int):
    """The RAW `carousel_slides` column for a post — the stored JSON, not a list.

    `get_carousel_slides` is the parsed reader; this one hands back whatever the column holds (or None),
    so a caller that iterates it will walk a JSON string character by character.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT carousel_slides FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return row["carousel_slides"] if row else None
    except mysql.connector.Error as err:
        log_error("Could not get carousel_slides", exc=err, post_id=post_id)
        return None
def get_unposted_posts_missing_assets(within_days: int = 14) -> list:
    """Posts not yet posted, due within `within_days`, whose required media asset is
    missing: video posts with no video_url, or carousel posts with no slides. Used by the
    backfill safety net. Returns (id, user_id, post_type, buyer_stage, scheduled_time).
    """
    try:
        with db_cursor() as cursor:
        # Include 'error' so failed posts get a regeneration attempt. A carousel needs
        # regeneration when its slides are empty OR are plain text titles with no real image
        # reference — real slides are stored as URLs (https .../api/assets/...png), so the
        # absence of any image marker means generation never produced images.
            cursor.execute("""
                SELECT id, user_id, post_type, buyer_stage, scheduled_time
                FROM posts
                WHERE status IN ('approved', 'pending', 'scheduled', 'error')
                  AND scheduled_time > NOW()
                  AND scheduled_time <= NOW() + INTERVAL %s DAY
                  AND (
                        (post_type = 'video'    AND (video_url IS NULL OR video_url = ''))
                     OR (post_type IN ('carousel', 'document') AND (
                            carousel_slides IS NULL OR carousel_slides = '' OR carousel_slides = '[]'
                            OR (carousel_slides NOT LIKE '%%http%%'
                                AND carousel_slides NOT LIKE '%%/assets%%'
                                AND carousel_slides NOT LIKE '%%.png%%'
                                AND carousel_slides NOT LIKE '%%.jpg%%')
                        ))
                  )
                ORDER BY scheduled_time
            """, (within_days,))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get unposted posts missing assets", exc=err)
        return []
def get_post_use_avatar(post_id: Optional[int]) -> Optional[bool]:
    """The compose-time avatar choice for a post — None when the user made no choice."""
    if not post_id:
        return None
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT use_avatar FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            if not row or row[0] is None:
                return None
            return bool(row[0])
    except mysql.connector.Error as err:
        log_error("Could not fetch use_avatar", exc=err, post_id=post_id)
        return None
def update_post_use_avatar(post_id: int, use_avatar: Optional[bool]) -> bool:
    """Set the compose-time avatar choice on an existing post. None clears it back to
    "follow my preferences" — the field is three-valued everywhere it is read.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET use_avatar = %s WHERE id = %s",
                (None if use_avatar is None else int(bool(use_avatar)), post_id),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not update use_avatar", exc=err, post_id=post_id)
        return False
def mark_post_avatar_media(post_id: Optional[int]) -> bool:
    """Record that generated media for this post came out of the avatar LoRA.

    This is what lets the caption disclosure cover avatar IMAGES and not just video — the
    generation step that knows an avatar was used is far away from the step that writes the
    caption, so the fact has to be durable.
    """
    if not post_id:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE posts SET avatar_media = 1 WHERE id = %s", (post_id,))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not mark avatar media", exc=err, post_id=post_id)
        return False
def post_avatar_media_state(post_id: Optional[int]) -> Optional[bool]:
    """Three-valued `posts.avatar_media`: True / False / **None when it could not be read**.

    Two callers want opposite fail-soft directions from the same fact, so the read has to be able to
    say "unknown". The AI disclosure treats unknown as False (`post_used_avatar_media` below) —
    a missed disclosure line. The caption burn treats it as True (issue #1278), because painting
    text over a real person's likeness on a guess is the one outcome the avatar guardrails exist to
    prevent. A falsy post_id is a definite False: there is no post, so no avatar media.
    """
    if not post_id:
        return False
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT avatar_media FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return bool(row[0])
    except mysql.connector.Error as err:
        log_error("Could not read avatar_media", exc=err, post_id=post_id)
        return None
def post_used_avatar_media(post_id: Optional[int]) -> bool:
    """Did any generated media on this post come out of the avatar path (issue #744)?

    What the AI-disclosure line is applied on. Fail-soft in both directions: a falsy post_id and a read
    error both return False, so an unreadable flag costs a disclosure rather than the post. Callers
    that must fail the other way read `post_avatar_media_state` instead.
    """
    return bool(post_avatar_media_state(post_id))
def get_post_quality_rows(start_date, end_date) -> list:
    """Per-post QUALITY observations across all users over [start_date, end_date] — the outcome side
    of the cost-aware routing experiment (docs/cost-performance-margin-plan.md §D.1(1), issue #494):
    `{user_id, post_id, day, reactions, comments, reposts, impressions, authenticity_score}` for
    every POSTED post with captured stats, using the LATEST `post_stats` row per post.

    Read-only and cross-user by design — the A/B arms are cohorts of users, so the comparison has to
    see every user's posts, unlike the per-user `get_post_engagement_rows`.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT p.user_id, p.id AS post_id, DATE(p.scheduled_time) AS day, "
                "p.authenticity_score, s.reactions, s.comments, s.reposts, s.impressions "
                "FROM posts p JOIN post_stats s ON s.post_id=p.id AND s.user_id=p.user_id "
                "WHERE p.status='posted' AND p.scheduled_time BETWEEN %s AND %s "
                "AND s.id IN (SELECT MAX(id) FROM post_stats GROUP BY post_id)",
                (start_date, end_date))
            rows = cursor.fetchall() or []
            return [
                {
                    "user_id": r["user_id"],
                    "post_id": r["post_id"],
                    "day": r["day"].isoformat() if hasattr(r.get("day"), "isoformat") else r.get("day"),
                    "reactions": int(r["reactions"] or 0),
                    "comments": int(r["comments"] or 0),
                    "reposts": int(r["reposts"] or 0),
                    "impressions": int(r["impressions"]) if r.get("impressions") else None,
                    "authenticity_score": (int(r["authenticity_score"])
                                           if r.get("authenticity_score") is not None else None),
                }
                for r in rows
            ]
    except mysql.connector.Error as err:
        log_error("Could not get post quality rows", exc=err)
        return []


def get_posts_with_media(limit: int = 500) -> list:
    """Every post row carrying an `image_url` or a `video_url`, newest first (issue #1377).

    The input to the media-integrity report, so it is deliberately NOT scoped to a user or to a
    window: a dangling URL is a property of the row, and the rows that dangle longest are the oldest
    ones. `limit` bounds the walk because each row costs a `stat` on the assets volume.

    Each row is `{id, user_id, status, post_type, image_url, video_url}` — status is what decides
    whether a missing file is `purge_post_assets` doing its job or an asset that went away while the
    post still needed it.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, status, post_type, image_url, video_url FROM posts "
                "WHERE (image_url IS NOT NULL AND image_url <> '') "
                "   OR (video_url IS NOT NULL AND video_url <> '') "
                "ORDER BY id DESC LIMIT %s",
                (max(1, int(limit)),))
            return [dict(r) for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not read posts carrying media", exc=err)
        return []


def has_post_with_status(user_id: int, statuses: tuple) -> bool:
    """True when the user has at least one post in any of the given statuses."""
    if not statuses:
        return False
    try:
        with db_cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(statuses))
            cursor.execute(
                f"SELECT 1 FROM posts WHERE user_id = %s AND status IN ({placeholders}) LIMIT 1",
                (user_id, *[str(s) for s in statuses]))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error(f"Could not check posts for user_id {user_id}", exc=err)
        return False
