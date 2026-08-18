"""Every SQL statement LEM runs against the engagement tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

from typing import Optional

import mysql.connector

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.platform.db.enums import (
    LogActionType,
    LogResultType,
)
from cqc_lem.utilities.logger import log_error, log_info, log_warning

# Marker message logged (as ENGAGED/SUCCESS) whenever a LinkedIn invite is actually sent — reactive
# profile-viewer AND proactive (issue #398) sends both flow through invite_to_connect_now, so the
# combined daily invite budget is counted from these log rows (see count_invites_sent_today).
CONNECTION_REQUEST_SENT_MESSAGE = "Connection Request Sent Successfully"
# Marker message logged (as ENGAGED/SUCCESS) for a COMPANY-PAGE invite batch (issue #732). Page
# invites are a single batched UI action — select N invitees, click Invite once — so ONE row carries
# the count, and `count_company_page_invites_sent_today` SUMS the trailing number rather than
# counting rows. Keep the "<message>: <n>" shape: that suffix is what the SUM parses.
COMPANY_PAGE_INVITE_SENT_MESSAGE = "Company page invites sent"
# Marker message logged for every stale pending invite this account WITHDREW (issue #969). Logged on
# DISPATCH — result SUCCESS when the row was verifiably gone afterwards, FAILURE when it was not —
# and `count_invite_withdrawals_today` counts BOTH, because the click already reached LinkedIn and a
# lane whose verification broke must not be free to click every row on the page.
STALE_INVITE_WITHDRAWN_MESSAGE = "Stale connection invite withdrawn"
def insert_new_log(user_id: int, action_type: LogActionType, result: LogResultType,
                   post_id: Optional[int] = None, post_url: Optional[str] = None,
                   message: Optional[str] = None):
    """Append one row to `logs`: the durable record that an action was attempted, and its outcome.

    These rows are also the ledger the per-day caps and dedup checks count off, so a failed insert —
    logged, returns False, never raises — is an action the account has already spent but will not see
    when it next checks its remaining budget.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("""
                INSERT INTO logs (user_id, action_type, post_id, post_url, message, result)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, action_type.value, post_id, post_url, message, result.value))

            success = cursor.rowcount == 1
    except mysql.connector.Error as err:
        log_error("Could not insert new log", exc=err)
        success = False

    return success
def count_user_comments_on_post_url(user_id: int, post_url: str) -> int:
    """How many top-level comments WE have successfully left on this post URL. Replies
    (LogActionType.REPLY) are deliberately not counted — the self-comment cap (issue #622) is about
    seeding our own thread, not about answering the people in it.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM logs WHERE user_id = %s AND post_url = %s AND action_type = %s AND result = %s",
                (user_id, post_url, LogActionType.COMMENT.value, LogResultType.SUCCESS.value))
            count = cursor.fetchone()[0]
    except mysql.connector.Error as err:
        log_error("Could not count user comments on post url", exc=err)
        count = 0

    return int(count or 0)
def get_post_age_minutes(user_id: int, post_id: int):
    """Minutes since this post actually went live, or None when it never published.

    Measured from the successful POST log row — not `posts.scheduled_time`, because a post that
    published late (queue backlog, retry) would otherwise make an on-time golden-hour sweep look
    late (issue #622). The subtraction is done in SQL against the server's own NOW() so the reading
    never depends on the app and the database agreeing about the timezone: `logs.created_at` is
    written in the DB session's zone (`TZ`, not UTC), so comparing it to a Python UTC clock would
    skew every latency by the offset.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""SELECT TIMESTAMPDIFF(SECOND, created_at, NOW()) FROM logs
                WHERE user_id = %s AND post_id = %s AND action_type = %s AND result = %s
                ORDER BY created_at DESC
                LIMIT 1""",
                           (user_id, post_id, LogActionType.POST.value, LogResultType.SUCCESS.value))
            row = cursor.fetchone()
            seconds = row[0] if row else None
    except mysql.connector.Error as err:
        log_error("Could not get post age from log for user", exc=err)
        seconds = None

    return None if seconds is None else max(0.0, float(seconds) / 60.0)
def get_post_url_from_log_for_user(user_id: int, post_id: int) -> Optional[str]:
    """The permalink LinkedIn gave us for this post, from the most recent successful POST log row.

    `posts` records what we intended to publish; only `logs.post_url` records where it landed, so this is
    the read that turns a post id into something the browser can open. None when the post never published
    successfully, or when the read failed.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""SELECT post_url FROM logs
                WHERE user_id = %s AND post_id = %s AND action_type = %s AND result = %s
                ORDER BY created_at DESC
                LIMIT 1""",
                           (user_id, post_id, LogActionType.POST.value, LogResultType.SUCCESS.value))
            row = cursor.fetchone()
            post_url = row[0] if row else None
    except mysql.connector.Error as err:
        log_warning("Could not get post url from log for user", exc=err,
                    user_id=user_id, post_id=post_id)
        post_url = None

    return post_url
def get_post_message_from_log_for_user(user_id: int, post_id: int) -> Optional[str]:
    """The message body recorded on the most recent successful POST log row for this post.

    The fallback for grounding replies and seed comments in what actually went out when
    `get_post_content` has nothing (issue #344). None when the post never published, or the read failed.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""SELECT message FROM logs
                WHERE user_id = %s AND post_id = %s AND action_type = %s AND result = %s
                ORDER BY created_at DESC
                LIMIT 1""",
                           (user_id, post_id, LogActionType.POST.value, LogResultType.SUCCESS.value))
            row = cursor.fetchone()
            message = row[0] if row else None
    except mysql.connector.Error as err:
        log_warning("Could not get post message from log for user", exc=err,
                    user_id=user_id, post_id=post_id)
        message = None

    return message
def has_engaged_url_with_x_days(user_id: int, post_url: str, days: int):
    """Did we record an ENGAGED action against this URL inside the last `days` days?

    The dedup that stops a flow re-touching the same person day after day. A failed read counts zero,
    i.e. reads as "not engaged" and lets the action through.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM logs WHERE user_id = %s AND post_url = %s AND action_type = %s AND result = %s AND created_at > NOW() - INTERVAL %s DAY",
                (user_id, post_url, LogActionType.ENGAGED.value, LogResultType.SUCCESS.value, days))
            count = cursor.fetchone()[0]
    except mysql.connector.Error as err:
        log_error("Could not determine if user engaged with url with x days", exc=err)
        count = 0

    return count > 0
def get_dm_history_for_profile(user_id: int, profile_url: str) -> list[str]:
    """Return all DM messages previously sent by user_id to profile_url, oldest first."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT message FROM logs WHERE user_id = %s AND post_url = %s AND action_type = %s ORDER BY created_at ASC",
                (user_id, profile_url, LogActionType.DM.value),
            )
            rows = cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get DM history for profile", exc=err)
        rows = []
    return [row[0] for row in rows if row[0]]
def get_recent_logs(user_id: int, limit: int = 20) -> list:
    """The newest `limit` activity rows for one account, for the SPA's activity feed.

    [] on a read error, so the feed renders empty rather than erroring.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT id, action_type, result, post_id, post_url, message, created_at
                   FROM logs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
                (user_id, limit)
            )
            rows = cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get recent logs", exc=err)
        rows = []

    return rows
def _count_actions_today(user_id: int, action_type: "LogActionType") -> int:
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM logs WHERE user_id=%s AND action_type=%s AND result=%s "
                "AND created_at >= CURDATE()",
                (user_id, str(action_type), str(LogResultType.SUCCESS)))
            r = cursor.fetchone()
            return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        log_error("Could not count actions", exc=err, user_id=user_id)
        return 0
def count_comments_today(user_id: int) -> int:
    """Comments logged as SUCCESS since the database's own midnight — what the per-day cap is checked against.

    Counted from `logs` rather than from anything a task remembers, so a retry or a second worker cannot
    spend the same budget twice. A read error counts 0, which fails OPEN: the cap stops nothing.
    """
    return _count_actions_today(user_id, LogActionType.COMMENT)
def count_invites_sent_today(user_id: int) -> int:
    """Invitations actually sent today, counted as a COMBINED daily budget (issue #398 owner review):
    both the reactive profile-viewer flow and the proactive connect flow send via invite_to_connect_now,
    which logs an ENGAGED/SUCCESS row with CONNECTION_REQUEST_SENT_MESSAGE on every real send. Counting
    those immutable logs (by created_at) covers both flows without double-counting a proactive send (which
    also has a connection_requests row) and avoids the mutable connection_requests.updated_at clock.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM logs WHERE user_id=%s AND action_type=%s AND result=%s "
                "AND message=%s AND created_at >= CURDATE()",
                (user_id, LogActionType.ENGAGED.value, LogResultType.SUCCESS.value,
                 CONNECTION_REQUEST_SENT_MESSAGE))
            r = cursor.fetchone()
            return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        log_error("Could not count invites", exc=err, user_id=user_id)
        return 0
def count_invite_withdrawals_today(user_id: int) -> int:
    """Stale pending invites this user's account withdrew today (issue #969) — the durable half of
    the daily cap.

    Counts BOTH result values on purpose: the row is written when the withdrawal is DISPATCHED, and
    an unverified one still cost LinkedIn an action. Reading it back out of the immutable logs (not
    Redis) is what keeps a second run the same day, or a worker restart, from re-spending the day's
    allowance.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM logs WHERE user_id=%s AND action_type=%s AND message=%s "
                "AND created_at >= CURDATE()",
                (user_id, LogActionType.ENGAGED.value, STALE_INVITE_WITHDRAWN_MESSAGE))
            r = cursor.fetchone()
            return max(0, int(r[0])) if r and r[0] is not None else 0
    except (mysql.connector.Error, TypeError, ValueError) as err:
        log_error("Could not count invite withdrawals", exc=err, user_id=user_id)
        return 0
def count_company_page_invites_sent_today(user_id: int) -> int:
    """Company-page invites sent today (issue #732) — the durable half of the daily cap.

    A page invite is a BATCH action (select N, click Invite once), so one log row carries a count
    rather than one row per invitee; the trailing number in COMPANY_PAGE_INVITE_SENT_MESSAGE is
    summed instead of the rows being counted. Reading it back out of the immutable logs (not Redis)
    is what makes a second run the same day idempotent across worker restarts.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(CAST(REGEXP_SUBSTR(message, '[0-9]+') AS UNSIGNED)), 0) FROM logs "
                "WHERE user_id=%s AND action_type=%s AND result=%s AND message LIKE %s "
                "AND created_at >= CURDATE()",
                (user_id, LogActionType.ENGAGED.value, LogResultType.SUCCESS.value,
                 f"{COMPANY_PAGE_INVITE_SENT_MESSAGE}:%"))
            r = cursor.fetchone()
            return max(0, int(r[0])) if r and r[0] is not None else 0
    except (mysql.connector.Error, TypeError, ValueError) as err:
        log_error("Could not count company page invites", exc=err, user_id=user_id)
        return 0
CLAIM_STALE_MINUTES = 60
def claim_post_for_comment(user_id: int, post_key: str, stale_after_minutes: int = CLAIM_STALE_MINUTES) -> bool:
    """Atomically claim a feed post for commenting. Returns True only for the caller that WON the
    claim (safe to comment); False if the post was already claimed/commented by any prior or
    concurrent run — the loser must back off. The UNIQUE (user_id, post_key) constraint resolves
    the race at the DB, so this is safe across sequential re-scans, retries, and parallel workers.

    A claim left in 'claimed' state for longer than `stale_after_minutes` is taken over: the run
    that made it died before it could comment OR release (a worker SIGKILLed by a deploy has no
    chance to run its except-branch release). Without the takeover, a task re-queued by
    task_acks_late would keep short-circuiting on its own abandoned claim and the comment would be
    lost for good — see issue #549. The window is far longer than a comment run, so a takeover
    can't race a task that is genuinely still working.
    """
    if not post_key or not str(post_key).strip():
        return False
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO commented_posts (user_id, post_key, status) VALUES (%s,%s,'claimed')",
            (user_id, str(post_key)[:255]))
        connection.commit()
        return cursor.rowcount == 1
    except mysql.connector.IntegrityError:
        # Duplicate key — someone holds this post. Only a STALE, never-posted claim is takeable;
        # a 'commented' row is never reclaimed, so this can't cause a double comment.
        try:
            cursor.execute(
                "UPDATE commented_posts SET status='claimed', updated_at=CURRENT_TIMESTAMP "
                "WHERE user_id=%s AND post_key=%s AND status='claimed' "
                "AND updated_at < NOW() - INTERVAL %s MINUTE",
                (user_id, str(post_key)[:255], max(1, int(stale_after_minutes))))
            connection.commit()
            if cursor.rowcount == 1:
                log_info(f"Took over a stale comment claim | {post_key}",
                         user_id=user_id, action_type="comment")
                return True
        except mysql.connector.Error as err:
            log_error("Could not take over stale comment claim", exc=err,
                      user_id=user_id, action_type="comment")
        return False
    except mysql.connector.Error as err:
        log_error("Could not claim post for comment", exc=err,
                  user_id=user_id, action_type="comment")
        return False
    finally:
        cursor.close()
        connection.close()
def mark_post_commented(user_id: int, post_key: str) -> bool:
    """Promote a won claim to 'commented' once the comment has actually posted."""
    if not post_key:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE commented_posts SET status='commented' WHERE user_id=%s AND post_key=%s",
                (user_id, str(post_key)[:255]))
            return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        log_error("Could not mark post commented", exc=err, user_id=user_id)
        return False
def mark_post_reacted(user_id: int, post_key: str) -> bool:
    """Record that we also left a reaction on this post (audit + 'react at most once' tracking)."""
    if not post_key:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE commented_posts SET reacted=1 WHERE user_id=%s AND post_key=%s",
                (user_id, str(post_key)[:255]))
            return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        log_error("Could not mark post reacted", exc=err, user_id=user_id)
        return False
def release_post_claim(user_id: int, post_key: str) -> bool:
    """Release an in-flight claim (comment never posted) so a later run can retry the post. Only
    deletes rows still in the 'claimed' state — a successful 'commented' record is never released.
    """
    if not post_key:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM commented_posts WHERE user_id=%s AND post_key=%s AND status='claimed'",
                (user_id, str(post_key)[:255]))
            return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        log_error("Could not release post claim", exc=err, user_id=user_id)
        return False
def has_commented_post(user_id: int, post_key: str) -> bool:
    """True if this post is already claimed or commented for the user (persistent, cross-run
    dedup). Empty/False if the ledger table isn't present yet.
    """
    if not post_key:
        return False
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM commented_posts WHERE user_id=%s AND post_key=%s",
                (user_id, str(post_key)[:255]))
            row = cursor.fetchone()
            return bool(row and row[0])
    except mysql.connector.Error:
        return False
def get_recent_navigable_commented_posts(user_id: int, days: int = 3) -> list:
    """Posts we automated a comment on in the last `days` whose ledger key is a navigable URN
    (feedurn://urn:li:...), newest first. These are the posts the follow-up sweep can revisit to
    handle replies to our comment (issue #478). Pre-#474 'feedpost://' hash keys aren't navigable
    and are excluded.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT post_key, created_at FROM commented_posts "
                "WHERE user_id=%s AND status='commented' AND post_key LIKE 'feedurn://%%' "
                "AND created_at >= (NOW() - INTERVAL %s DAY) ORDER BY created_at DESC",
                (user_id, int(days)))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error:
        return []
def get_recent_commented_rows_with_text(user_id: int, days: int = 3) -> list:
    """Recent commented_posts rows plus the comment text we left (from the most recent matching
    SUCCESS comment log), for the URN reconcile backfill. Includes legacy 'feedpost://' rows so
    they can be matched by text and upgraded (issue #478).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT cp.post_key, cp.created_at, "
                "  (SELECT l.message FROM logs l WHERE l.user_id=cp.user_id AND l.post_url=cp.post_key "
                "   AND l.action_type='comment' AND l.result='success' "
                "   ORDER BY l.created_at DESC LIMIT 1) AS comment_text "
                "FROM commented_posts cp "
                "WHERE cp.user_id=%s AND cp.status='commented' "
                "  AND cp.created_at >= (NOW() - INTERVAL %s DAY) "
                "ORDER BY cp.created_at DESC",
                (user_id, int(days)))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error:
        return []
def get_comment_followup(user_id: int, reply_key: str) -> "dict | None":
    """The follow-up ledger row for a specific reply (reacted/replied flags), or None if we have
    never handled this reply. Dedup anchor for the follow-up sweep.
    """
    if not reply_key:
        return None
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT reacted, replied FROM comment_followups WHERE user_id=%s AND reply_key=%s",
                (user_id, str(reply_key)[:255]))
            return cursor.fetchone()
    except mysql.connector.Error:
        return None
def record_comment_followup(user_id: int, post_key: str, reply_key: str,
                            reacted: bool = False, replied: bool = False) -> bool:
    """Upsert a follow-up record for a reply. Flags are latched ON (never cleared) so a later
    partial pass can't undo an earlier action, and the UNIQUE(user_id, reply_key) constraint makes
    this the single source of truth for 'already reacted/replied to this reply'.
    """
    if not reply_key:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO comment_followups (user_id, post_key, reply_key, reacted, replied) "
                "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "reacted=GREATEST(reacted, VALUES(reacted)), replied=GREATEST(replied, VALUES(replied))",
                (user_id, str(post_key)[:255], str(reply_key)[:255], int(bool(reacted)), int(bool(replied))))
            return True
    except mysql.connector.Error as err:
        log_error("Could not record comment followup", exc=err, user_id=user_id)
        return False
def count_followup_replies_today(user_id: int) -> int:
    """Auto-replies posted to comment-replies today — the daily cap for the follow-up feature."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM comment_followups "
                "WHERE user_id=%s AND replied=1 AND updated_at >= CURDATE()",
                (user_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] else 0
    except mysql.connector.Error:
        return 0
def update_commented_post_key(user_id: int, old_key: str, new_key: str) -> bool:
    """Backfill: upgrade a commented_posts row's key from the old 'feedpost://' content hash to the
    navigable 'feedurn://' URN once we recover it (issue #478 reconcile). If the new key already
    exists for this user (already commented under the URN), drop the stale hash row instead so the
    UNIQUE(user_id, post_key) constraint isn't violated.
    """
    if not old_key or not new_key or old_key == new_key:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM commented_posts WHERE user_id=%s AND post_key=%s",
                (user_id, str(new_key)[:255]))
            exists = bool((cursor.fetchone() or [0])[0])
            if exists:
                cursor.execute("DELETE FROM commented_posts WHERE user_id=%s AND post_key=%s",
                               (user_id, str(old_key)[:255]))
            else:
                cursor.execute("UPDATE commented_posts SET post_key=%s WHERE user_id=%s AND post_key=%s",
                               (str(new_key)[:255], user_id, str(old_key)[:255]))
            return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        log_error("Could not update commented post key", exc=err, user_id=user_id)
        return False
def get_comment_outcome_targets(user_id: int, min_age_hours: int = 24, max_age_hours: int = 168,
                                limit: int = 15) -> list:
    """Comments we posted that are old enough to have earned a reply and have never been checked —
    the T+24h outcome sweep's work list (issue #628).

    Only NAVIGABLE ledger keys qualify: a pre-#474 'feedpost://' content hash cannot be revisited,
    so it could never be checked and would sit at the head of the queue forever. The upper bound is
    a week (not the nominal 48h) so a sweep missed for a couple of days still records the sample
    instead of silently dropping it, and the LEFT JOIN is what makes the check at-most-once —
    including for a comment whose check was SKIPPED (that row exists too).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT l.id AS log_id, l.post_url, l.message, l.created_at "
                "FROM logs l LEFT JOIN comment_outcomes co "
                "  ON co.log_id = l.id AND co.user_id = l.user_id "
                "WHERE l.user_id=%s AND l.action_type=%s AND l.result=%s "
                "  AND l.post_url LIKE 'feedurn://%%' "
                "  AND l.created_at <= (NOW() - INTERVAL %s HOUR) "
                "  AND l.created_at >= (NOW() - INTERVAL %s HOUR) "
                "  AND co.id IS NULL "
                "ORDER BY l.created_at ASC LIMIT %s",
                (user_id, LogActionType.COMMENT.value, LogResultType.SUCCESS.value,
                 int(min_age_hours), int(max_age_hours), max(1, int(limit))))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not get comment outcome targets", exc=err, user_id=user_id)
        return []
def record_comment_outcome(user_id: int, log_id: int, post_key: str = None,
                           author_replied: bool = False, reply_count: int = 0,
                           like_count: int = 0, visible_most_relevant: "bool | None" = None,
                           our_reply_sent: bool = False, status: str = "checked",
                           skip_reason: str = None) -> bool:
    """Persist one comment's outcome reading (issue #628). Upsert on (user_id, log_id) so a re-run
    refreshes rather than duplicating; a skipped check writes a row too (status='skipped' with the
    reason), which is what stops an unfindable comment being re-walked every night.

    `visible_most_relevant` stays NULL when the read was ambiguous — never coerced to a boolean,
    because a guess would feed the demotion rate that gates commenting.
    """
    if not log_id:
        return False
    visible = None if visible_most_relevant is None else int(bool(visible_most_relevant))
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO comment_outcomes (user_id, log_id, post_key, author_replied, reply_count, "
                "  like_count, visible_most_relevant, our_reply_sent, status, skip_reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "  post_key=VALUES(post_key), checked_at=CURRENT_TIMESTAMP, "
                "  author_replied=VALUES(author_replied), reply_count=VALUES(reply_count), "
                "  like_count=VALUES(like_count), visible_most_relevant=VALUES(visible_most_relevant), "
                "  our_reply_sent=VALUES(our_reply_sent), status=VALUES(status), "
                "  skip_reason=VALUES(skip_reason)",
                (user_id, int(log_id), (str(post_key)[:255] if post_key else None),
                 int(bool(author_replied)), max(0, int(reply_count or 0)), max(0, int(like_count or 0)),
                 visible, int(bool(our_reply_sent)), str(status or "checked")[:20],
                 (str(skip_reason)[:255] if skip_reason else None)))
            return True
    except mysql.connector.Error as err:
        log_error("Could not record comment outcome", exc=err, user_id=user_id)
        return False
def get_comment_outcomes(user_id: int, days: int = 7) -> list:
    """Comment-outcome rows checked in the last `days`, newest first — the input to the weekly
    quality score (`utilities/comment_outcomes.py`).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT log_id, post_key, checked_at, author_replied, reply_count, like_count, "
                "  visible_most_relevant, our_reply_sent, status, skip_reason "
                "FROM comment_outcomes WHERE user_id=%s AND checked_at >= (NOW() - INTERVAL %s DAY) "
                "ORDER BY checked_at DESC",
                (user_id, max(1, int(days))))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error:
        return []
def get_duplicate_comment_posts(user_id: int, hours: int = 24):
    """Read-only report: posts the user commented on MORE THAN ONCE in the last `hours`, from the
    SUCCESS comment logs. Returns list of (post_url, comment_count, first_at, last_at) ordered by
    most-duplicated first. Used to size/verify the multiple-comment bug and drive consolidation.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT post_url, COUNT(*) AS c, MIN(created_at) AS first_at, MAX(created_at) AS last_at "
                "FROM logs WHERE user_id=%s AND action_type=%s AND result=%s "
                "AND post_url IS NOT NULL AND created_at >= (NOW() - INTERVAL %s HOUR) "
                "GROUP BY post_url HAVING c > 1 ORDER BY c DESC, last_at DESC",
                (user_id, LogActionType.COMMENT.value, LogResultType.SUCCESS.value, hours))
            return [tuple(r) for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get duplicate comment posts", exc=err, user_id=user_id)
        return []
def get_recent_comment_texts(user_id: int, limit: int = 50) -> list:
    """The bodies of the user's most recently POSTED comments, newest first — the history the
    comment-side similarity gate dedups a fresh draft against (issue #617). No new column and no
    stored embeddings: `logs.message` already holds the exact text of every successful comment, so
    the gate recomputes from the log.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT message FROM logs "
                "WHERE user_id=%s AND action_type=%s AND result=%s "
                "AND message IS NOT NULL AND message <> '' "
                "ORDER BY id DESC LIMIT %s",
                (user_id, LogActionType.COMMENT.value, LogResultType.SUCCESS.value, int(limit)))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get recent comment texts", exc=err, user_id=user_id)
        return []
def get_daily_action_counts(user_id: int, days: int = 90,
                            action_types: Optional[list] = None) -> list:
    """Daily count of the user's SUCCESSFUL automation actions, for overlaying what we DID on the
    audience-growth chart (issue #627). Defaults to the outbound actions a follower can react to:
    posts, feed comments, replies and DMs. Returns dicts of {date, action_type, count}.
    """
    types = [LogActionType.POST.value, LogActionType.COMMENT.value, LogActionType.REPLY.value,
             LogActionType.DM.value] if action_types is None else list(action_types)
    if not types:
        return []
    try:
        with db_cursor(dictionary=True) as cursor:
            placeholders = ",".join(["%s"] * len(types))
            cursor.execute(
                "SELECT DATE(created_at) AS `date`, action_type, COUNT(*) AS `count` FROM logs "
                f"WHERE user_id = %s AND result = %s AND action_type IN ({placeholders}) "
                "AND created_at >= (NOW() - INTERVAL %s DAY) "
                "GROUP BY DATE(created_at), action_type ORDER BY `date` ASC",
                (user_id, LogResultType.SUCCESS.value, *types, days))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get daily action counts", exc=err, user_id=user_id)
        return []
def has_automated_engagement(user_id: int) -> bool:
    """True once automation has successfully commented, replied, or DM'd on the user's behalf —
    the engagement half of the activation ("aha") moment.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM logs WHERE user_id = %s AND result = %s "
                "AND action_type IN (%s, %s, %s, %s) LIMIT 1",
                (user_id, str(LogResultType.SUCCESS), str(LogActionType.COMMENT),
                 str(LogActionType.REPLY), str(LogActionType.DM), str(LogActionType.FOLLOWUP)))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error(f"Could not check automated engagement for user_id {user_id}", exc=err)
        return False


def has_user_commented_on_post_url(user_id: int, post_url: str):
    """Have we already left a top-level comment on this post URL?

    Replies do not count (see `count_user_comments_on_post_url`). A failed read counts zero, so an
    unreadable log reads as "not yet" and the post can be commented on again.
    """
    return count_user_comments_on_post_url(user_id, post_url) > 0
def count_dms_sent_today(user_id: int) -> int:
    """DMs logged as SUCCESS since the database's own midnight — the counterpart cap to `count_comments_today`."""
    return _count_actions_today(user_id, LogActionType.DM)
