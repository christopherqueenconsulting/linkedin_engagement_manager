"""Every SQL statement LEM runs against the newsletter tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

import json
from datetime import datetime
from typing import Optional

import mysql.connector

from cqc_lem.platform.db.connection import (
    db_cursor,
    to_naive_utc,
)
from cqc_lem.utilities.logger import log_error

_NEWSLETTER_DEFAULTS: dict = {
    "enabled": False, "title": None, "topic": None, "cadence": "weekly",
    "align_with_blog": True, "newsletter_url": None, "last_published_at": None,
    "publish_day": 1, "publish_hour": 9, "generate_lead_days": 3, "max_queued_drafts": 1,
    "invite_connections_enabled": False, "max_invites_per_run": 50, "cover_image_auto": False,
}
_NEWSLETTER_COLS = ("enabled", "title", "topic", "cadence", "align_with_blog", "newsletter_url",
                    "publish_day", "publish_hour", "generate_lead_days", "max_queued_drafts",
                    "invite_connections_enabled", "max_invites_per_run", "cover_image_auto")
_NEWSLETTER_BOOL_COLS = ("enabled", "align_with_blog", "invite_connections_enabled",
                         "cover_image_auto")
def get_newsletter_settings(user_id: int) -> dict:
    """Return the user's newsletter config with defaults (disabled) when no row exists."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT enabled, title, topic, cadence, align_with_blog, newsletter_url, last_published_at, "
                "publish_day, publish_hour, generate_lead_days, max_queued_drafts, "
                "invite_connections_enabled, max_invites_per_run, cover_image_auto "
                "FROM newsletter_settings WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row is None:
                return dict(_NEWSLETTER_DEFAULTS)
            for col in _NEWSLETTER_BOOL_COLS:
                row[col] = bool(row.get(col))
            row["publish_day"] = int(row.get("publish_day") if row.get("publish_day") is not None else 1)
            row["publish_hour"] = int(row.get("publish_hour") if row.get("publish_hour") is not None else 9)
            row["generate_lead_days"] = int(
                row.get("generate_lead_days") if row.get("generate_lead_days") is not None else 3)
            row["max_queued_drafts"] = int(
                row.get("max_queued_drafts") if row.get("max_queued_drafts") is not None else 1)
            row["max_invites_per_run"] = int(
                row.get("max_invites_per_run") if row.get("max_invites_per_run") is not None else 50)
            return row
    except mysql.connector.Error as err:
        log_error("Could not get newsletter settings", exc=err, user_id=user_id)
        return dict(_NEWSLETTER_DEFAULTS)
def update_newsletter_settings(user_id: int, settings: dict) -> bool:
    """Upsert the user's newsletter config (title/topic/cadence/enabled/align_with_blog,
    plus the opt-in invite flow: invite_connections_enabled/max_invites_per_run, and the opt-in
    AI cover generation: cover_image_auto).
    """
    merged = {**_NEWSLETTER_DEFAULTS, **{k: v for k, v in settings.items() if k in _NEWSLETTER_COLS}}
    values = [user_id] + [
        (1 if merged[c] else 0) if c in _NEWSLETTER_BOOL_COLS else merged[c]
        for c in _NEWSLETTER_COLS]
    placeholders = ", ".join(["%s"] * (len(_NEWSLETTER_COLS) + 1))
    updates = ", ".join(f"{c}=VALUES({c})" for c in _NEWSLETTER_COLS)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"INSERT INTO newsletter_settings (user_id, {', '.join(_NEWSLETTER_COLS)}) "
                f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}", values)
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not update newsletter settings", exc=err, user_id=user_id)
        return False
def mark_newsletter_published(user_id: int, newsletter_url: str = None) -> bool:
    """Stamp the newsletter as published now, and record its URL when we managed to read one.

    `newsletter_url` is only written when supplied: a publish run that could not scrape the edition's
    link must not blank the link we already had. True whenever the statement ran.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if newsletter_url:
                cursor.execute("UPDATE newsletter_settings SET last_published_at=NOW(), newsletter_url=%s "
                               "WHERE user_id=%s", (newsletter_url, user_id))
            else:
                cursor.execute("UPDATE newsletter_settings SET last_published_at=NOW() WHERE user_id=%s", (user_id,))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error("Could not mark newsletter published", exc=err, user_id=user_id)
        return False
def record_newsletter_subscriber_stat(user_id: int, subscriber_count: "int | None" = None,
                                      invites_sent: int = 0) -> bool:
    """Append one subscriber-growth snapshot for the user: the scraped subscriber_count (NULL when
    the page couldn't be read) and how many connections were invited on this run. One row per
    tracking run so growth can be charted over time (issue #400).
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO newsletter_subscriber_stats (user_id, subscriber_count, invites_sent) "
                "VALUES (%s, %s, %s)",
                (user_id, subscriber_count, int(invites_sent or 0)))
            return cursor.rowcount == 1
    except mysql.connector.Error as err:
        log_error("Could not record newsletter subscriber stat", exc=err, user_id=user_id)
        return False
def get_newsletter_subscriber_stats(user_id: int, limit: int = 52) -> list:
    """Return the user's subscriber-growth snapshots, most recent first (default last 52 runs — a
    year of weekly tracking). Each item: subscriber_count, invites_sent, captured_at.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT subscriber_count, invites_sent, captured_at FROM newsletter_subscriber_stats "
                "WHERE user_id = %s ORDER BY captured_at DESC, id DESC LIMIT %s", (user_id, limit))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get newsletter subscriber stats", exc=err, user_id=user_id)
        return []
def get_latest_newsletter_subscriber_count(user_id: int) -> "int | None":
    """Most recent non-NULL subscriber_count for the user, or None if never captured."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT subscriber_count FROM newsletter_subscriber_stats "
                "WHERE user_id = %s AND subscriber_count IS NOT NULL "
                "ORDER BY captured_at DESC, id DESC LIMIT 1", (user_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except mysql.connector.Error as err:
        log_error("Could not get latest newsletter subscriber count", exc=err, user_id=user_id)
        return None
def get_newsletter_due_user_ids(now) -> list:
    """User IDs whose newsletter is enabled and due per its cadence (weekly/biweekly/monthly)."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM newsletter_settings WHERE enabled=1 AND ("
                "last_published_at IS NULL "
                "OR (cadence='weekly'   AND last_published_at <= %s - INTERVAL 7 DAY) "
                "OR (cadence='biweekly' AND last_published_at <= %s - INTERVAL 14 DAY) "
                "OR (cadence='monthly'  AND last_published_at <= %s - INTERVAL 1 MONTH))",
                (now, now, now))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get newsletter-due users", exc=err)
        return []
def get_enabled_newsletter_user_ids() -> list:
    """User IDs whose newsletter is enabled (regardless of cadence timing)."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT user_id FROM newsletter_settings WHERE enabled=1")
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get enabled newsletter users", exc=err)
        return []
def create_newsletter_edition(user_id: int, title: str, subtitle: str, body: str,
                              scheduled_for, subject: str = None, edition_format: str = None,
                              hook_style: str = None, opening_line: str = None,
                              blueprint: dict = None) -> int:
    """Insert a draft newsletter edition (status defaults to 'draft'). Returns its id. `subject` is
    the planned topic/angle; `edition_format`/`hook_style`/`opening_line`/`blueprint` record the
    edition's assigned SHAPE, so the planner can rotate formats/hooks/openers (not just subjects)
    against prior editions across runs.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO newsletter_editions (user_id, title, subtitle, subject, `format`, "
                "hook_style, opening_line, blueprint, body, scheduled_for) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (user_id, title, subtitle, subject, edition_format, hook_style, opening_line,
                 json.dumps(blueprint) if blueprint else None, body, to_naive_utc(scheduled_for)))
            return cursor.lastrowid
    except mysql.connector.IntegrityError as err:
        # errno 1062 = ER_DUP_ENTRY: uq_user_slot already covers this user+slot — expected, not an
        # error. Other integrity failures (e.g. FK on user_id) are real problems worth surfacing.
        if getattr(err, "errno", None) != 1062:
            log_error("Could not create newsletter edition", exc=err, user_id=user_id)
        return 0
    except mysql.connector.Error as err:
        log_error("Could not create newsletter edition", exc=err, user_id=user_id)
        return 0
def get_pending_newsletter_edition(user_id: int) -> "dict | None":
    """The most recent edition still under review (status draft/approved) for this user."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, title, subtitle, subject, `format`, hook_style, body, status, scheduled_for "
                "FROM newsletter_editions "
                "WHERE user_id = %s AND status IN ('draft', 'approved') "
                "ORDER BY id DESC LIMIT 1", (user_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not get pending newsletter edition", exc=err, user_id=user_id)
        return None
def count_pending_newsletter_editions(user_id: int) -> int:
    """How many editions are still queued (status draft/approved) for this user."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM newsletter_editions "
                "WHERE user_id = %s AND status IN ('draft', 'approved')", (user_id,))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count pending newsletter editions", exc=err, user_id=user_id)
        return 0
def get_pending_newsletter_editions(user_id: int) -> list:
    """All editions still under review (status draft/approved), soonest slot first — the review queue."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, title, subtitle, subject, `format`, hook_style, body, status, scheduled_for, "
                "cover_image_path, cover_image_source, cover_image_status "
                "FROM newsletter_editions "
                "WHERE user_id = %s AND status IN ('draft', 'approved') "
                "ORDER BY scheduled_for ASC", (user_id,))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get pending newsletter editions", exc=err, user_id=user_id)
        return []
def get_latest_edition_scheduled_for(user_id: int) -> "datetime | None":
    """The latest slot already covered by ANY edition (any status), so the next slot never re-covers it."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT MAX(scheduled_for) FROM newsletter_editions WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        log_error("Could not get latest edition slot", exc=err, user_id=user_id)
        return None
def update_newsletter_edition(edition_id: int, user_id: int, title: str = None,
                              subtitle: str = None, body: str = None,
                              status: str = None, subject: str = None,
                              edition_format: str = None, hook_style: str = None,
                              opening_line: str = None, blueprint: dict = None,
                              scheduled_for=None) -> bool:
    """Update only the provided fields on an edition, scoped to its owner (COALESCE-style)."""
    try:
        with db_cursor(commit=True) as cursor:
            scheduled_for = to_naive_utc(scheduled_for)
            cursor.execute(
                "UPDATE newsletter_editions SET "
                "title = COALESCE(%s, title), subtitle = COALESCE(%s, subtitle), "
                "subject = COALESCE(%s, subject), "
                "`format` = COALESCE(%s, `format`), hook_style = COALESCE(%s, hook_style), "
                "opening_line = COALESCE(%s, opening_line), blueprint = COALESCE(%s, blueprint), "
                "body = COALESCE(%s, body), status = COALESCE(%s, status), "
                "scheduled_for = COALESCE(%s, scheduled_for) "
                "WHERE id = %s AND user_id = %s",
                (title, subtitle, subject, edition_format, hook_style, opening_line,
                 json.dumps(blueprint) if blueprint else None, body, status, scheduled_for,
                 edition_id, user_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not update newsletter edition {edition_id}", exc=err)
        return False
def set_edition_cover_image(edition_id: int, user_id: int, cover_image_path: str,
                            source: str, status: str) -> bool:
    """Attach a cover image to an edition (issue #893), scoped to its owner.

    `source` is 'upload' or 'ai'; `status` is 'approved' or 'pending_review'. The two are set
    together on purpose — a generated cover that arrived without its pending status would be
    indistinguishable from artwork the author chose, and the publish flow only reads `status`.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE newsletter_editions SET cover_image_path=%s, cover_image_source=%s, "
                "cover_image_status=%s WHERE id=%s AND user_id=%s",
                (cover_image_path, source, status, edition_id, user_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not set cover image on edition {edition_id}", exc=err)
        return False
def set_edition_cover_status(edition_id: int, user_id: int, status: str) -> bool:
    """Move an edition's cover between 'pending_review' and 'approved' — the human half of the
    cover gate. Only an edition that HAS a cover can change its status.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE newsletter_editions SET cover_image_status=%s "
                "WHERE id=%s AND user_id=%s AND cover_image_path IS NOT NULL",
                (status, edition_id, user_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not set cover status on edition {edition_id}", exc=err)
        return False
def clear_edition_cover_image(edition_id: int, user_id: int) -> bool:
    """Drop an edition's cover entirely. `update_newsletter_edition` is COALESCE-based and so can
    never null a column — removing a cover needs its own statement.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE newsletter_editions SET cover_image_path=NULL, cover_image_source=NULL, "
                "cover_image_status=NULL WHERE id=%s AND user_id=%s", (edition_id, user_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not clear cover image on edition {edition_id}", exc=err)
        return False
def get_recent_newsletter_subjects(user_id: int, limit: int = 20) -> list:
    """Recent edition SUBJECTS (published, queued draft/approved, AND skipped) for a user — the dedup
    history fed to the topic planner so a new edition never repeats a subject already covered or
    recently rejected. Most-recent first; NULL/blank subjects excluded.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT subject FROM newsletter_editions "
                "WHERE user_id = %s AND subject IS NOT NULL AND subject <> '' "
                "AND status IN ('draft', 'approved', 'published', 'skipped') "
                "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get recent newsletter subjects", exc=err, user_id=user_id)
        return []
def get_recent_newsletter_bodies(user_id: int, limit: int = 20) -> list:
    """Recent edition BODIES (published + queued draft/approved), most-recent first.

    The newsletter-side dedup history, the exact counterpart of `get_recent_post_texts`.

    Added by #1284 because the nightly content-quality pass had no body reader for this surface, so
    every newsletter's self-similarity was recorded as unmeasured. Measured against the real corpus
    it was hiding a lot: ten shipped editions sat at 0.68-0.83 embedding cosine against each other,
    which on the post surface would be a regression alert.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT body FROM newsletter_editions "
                "WHERE user_id = %s AND body IS NOT NULL AND body <> '' "
                "AND status IN ('draft', 'approved', 'published') "
                "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get recent newsletter bodies", exc=err, user_id=user_id)
        return []


def get_recent_newsletter_titles(user_id: int, limit: int = 20) -> list:
    """Recent edition TITLES (published + queued draft/approved), most-recent first.

    The title is the subscriber's subject line, so it is a writing surface of its own: #1284
    measured ten of them at 0.372-0.711 cosine against each other while their bodies sat at
    0.68-0.83. Same status filter and ordering as `get_recent_newsletter_bodies`, so a title row and
    a body row for the same edition are the same edition — the sampler behind #1433 reads both and
    would otherwise be comparing two different corpora.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT title FROM newsletter_editions "
                "WHERE user_id = %s AND title IS NOT NULL AND title <> '' "
                "AND status IN ('draft', 'approved', 'published') "
                "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get recent newsletter titles", exc=err, user_id=user_id)
        return []


def get_recent_newsletter_blueprint_history(user_id: int, limit: int = 12) -> list:
    """Recent editions' SHAPE history — {subject, format, hook_style, opening_line} dicts, most-recent
    first, across queued/published/skipped editions. Fed to the planner and regenerator so new
    editions rotate away from recently used formats, hook styles, AND actual opening lines (the
    'every edition opens the same way' bug), not just subjects.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT subject, `format`, hook_style, opening_line FROM newsletter_editions "
                "WHERE user_id = %s AND status IN ('draft', 'approved', 'published', 'skipped') "
                "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get newsletter blueprint history", exc=err, user_id=user_id)
        return []
def get_editions_due_to_publish(now) -> list:
    """Editions whose scheduled slot has arrived and are still awaiting publish (draft/approved)."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, title, subtitle, body FROM newsletter_editions "
                "WHERE scheduled_for <= %s AND status IN ('draft', 'approved')", (now,))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        log_error("Could not get editions due to publish", exc=err)
        return []
def get_newsletter_edition(edition_id: int) -> "dict | None":
    """Fetch a single newsletter edition by id."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, title, subtitle, subject, `format`, hook_style, opening_line, "
                "body, status, scheduled_for, published_url, "
                "cover_image_path, cover_image_source, cover_image_status "
                "FROM newsletter_editions WHERE id = %s", (edition_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get newsletter edition {edition_id}", exc=err)
        return None
def mark_edition_published(edition_id: int, url: str) -> bool:
    """Mark an edition published and roll the user's newsletter cadence forward."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE newsletter_editions SET status='published', published_at=NOW(), published_url=%s "
                "WHERE id=%s", (url, edition_id))
            cursor.execute(
                "UPDATE newsletter_settings SET last_published_at=NOW() "
                "WHERE user_id = (SELECT user_id FROM newsletter_editions WHERE id=%s)", (edition_id,))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not mark edition {edition_id} published", exc=err)
        return False
def mark_edition_failed(edition_id: int) -> bool:
    """Mark an edition as failed to publish."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE newsletter_editions SET status='failed' WHERE id=%s", (edition_id,))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        log_error(f"Could not mark edition {edition_id} failed", exc=err)
        return False
def get_shipped_notice_by_issue(github_issue_number: int) -> Optional[dict]:
    """The changelog notice already recorded for this issue, or None."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, github_issue_number, pr_number, title, changelog_line, shipped_at "
                "FROM shipped_notices WHERE github_issue_number = %s", (int(github_issue_number),))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get shipped notice for issue {github_issue_number}", exc=err)
        return None
def record_shipped_notice(github_issue_number: int, changelog_line: str, pr_number: int = None,
                          title: str = None) -> Optional[int]:
    """Record the changelog line for a shipped issue and return its notice id. One notice per issue
    (the UNIQUE key), so a re-run of the notify pass re-uses the existing row instead of writing a
    second changelog entry for the same fix.
    """
    if not github_issue_number or not (changelog_line or "").strip():
        return None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO shipped_notices "
                "(github_issue_number, pr_number, title, changelog_line) VALUES (%s,%s,%s,%s)",
                (int(github_issue_number), int(pr_number) if pr_number else None,
                 str(title)[:255] if title else None, str(changelog_line)[:512]))
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            cursor.execute("SELECT id FROM shipped_notices WHERE github_issue_number = %s",
                           (int(github_issue_number),))
            row = cursor.fetchone()
            return int(row[0]) if row else None
    except mysql.connector.Error as err:
        log_error(f"Could not record shipped notice for issue {github_issue_number}", exc=err)
        return None
def record_shipped_notice_recipient(notice_id: int, user_id: int) -> bool:
    """Attach a reporter to a shipped notice. Returns True ONLY the first time — that is what makes
    "notified once" true no matter how often the notify pass runs.
    """
    if not notice_id or not user_id:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO shipped_notice_recipients (notice_id, user_id) VALUES (%s,%s)",
                (int(notice_id), int(user_id)))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not record shipped notice recipient for notice {notice_id}", exc=err,
                  user_id=user_id)
        return False
def get_shipped_notice_recipient_ids(notice_id: int) -> list:
    """Who has already been told about this shipped fix. Read BEFORE sending, so a re-run of the
    notify pass never re-emails a reporter — the recipient PK only stops the duplicate ROW.
    """
    if not notice_id:
        return []
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT user_id FROM shipped_notice_recipients WHERE notice_id = %s",
                           (int(notice_id),))
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error(f"Could not get shipped notice recipients for notice {notice_id}", exc=err)
        return []
def get_unseen_shipped_notices(user_id: int, delay_hours: int = 0, limit: int = 5) -> list:
    """Shipped fixes this user asked for and hasn't acknowledged in-app yet, oldest first.

    `delay_hours` is what SCHEDULES the micro-CSAT: a notice is only surfaced once the user has had
    that long with the fix, so "did this fix it?" is asked after they could have used it rather than
    in the same minute the email went out.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT n.id, n.github_issue_number, n.pr_number, n.title, n.changelog_line, "
                "       n.shipped_at, r.notified_at "
                "FROM shipped_notice_recipients r JOIN shipped_notices n ON n.id = r.notice_id "
                "WHERE r.user_id = %s AND r.seen_at IS NULL "
                "  AND r.notified_at <= (NOW() - INTERVAL %s HOUR) "
                "ORDER BY r.notified_at ASC LIMIT %s",
                (int(user_id), max(0, int(delay_hours)), int(limit)))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get unseen shipped notices", exc=err, user_id=user_id)
        return []
def mark_shipped_notice_seen(notice_id: int, user_id: int) -> bool:
    """The user answered or dismissed the notice — stop surfacing it. Idempotent: only the first
    acknowledgement writes a timestamp.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE shipped_notice_recipients SET seen_at = NOW() "
                "WHERE notice_id = %s AND user_id = %s AND seen_at IS NULL",
                (int(notice_id), int(user_id)))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not mark shipped notice {notice_id} seen", exc=err, user_id=user_id)
        return False
def get_recent_shipped_notices(limit: int = 10) -> list:
    """The user-facing changelog: what shipped, newest first."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, github_issue_number, pr_number, title, changelog_line, shipped_at "
                "FROM shipped_notices ORDER BY shipped_at DESC, id DESC LIMIT %s", (int(limit),))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get recent shipped notices", exc=err)
        return []
