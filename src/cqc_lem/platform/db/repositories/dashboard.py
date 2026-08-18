"""Read models the dashboard needs that no single aggregate owns.

`get_planned_tasks` is the one member here and the reason the module exists: the
"Planned Tasks" card merges upcoming POSTS, scheduled DMs and NEWSLETTER editions
into one soonest-first list, so it reads three aggregates and owns none of them.
Putting it in any one repository would give that module a foreign table, which is
the property the split exists to create; putting it in `platform/db/shared.py` is
worse still, because that module does no I/O. Issue #1614.
"""

import mysql.connector

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.enums import (
    PostStatus,
    ScheduledDmStatus,
)
from cqc_lem.utilities.logger import log_error


def get_planned_tasks(user_id: int, limit: int = 10) -> list[dict]:
    """Upcoming (future-dated, non-terminal) work for the dashboard "Planned Tasks" card:
    scheduled/approved/pending POSTS, scheduled DMs, and upcoming NEWSLETTER editions — each
    labeled by `kind` (Post / DM / Newsletter). Terminal states (posted/sent/published/etc.)
    are excluded, results are merged and sorted soonest-first, capped at `limit`.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    tasks: list[dict] = []
    try:
        cursor.execute(
            "SELECT id, content, scheduled_time, status FROM posts "
            "WHERE user_id = %s AND status IN (%s, %s, %s) "
            "AND scheduled_time >= UTC_TIMESTAMP() ORDER BY scheduled_time ASC LIMIT %s",
            (user_id, PostStatus.PENDING.value, PostStatus.APPROVED.value,
             PostStatus.SCHEDULED.value, limit))
        for row in cursor.fetchall():
            tasks.append({
                "kind": "Post",
                "id": row["id"],
                "title": (row.get("content") or "").strip()[:120] or "Scheduled post",
                "scheduled_time": row["scheduled_time"],
                "status": row["status"],
            })

        cursor.execute(
            "SELECT id, recipient_name, message, scheduled_time, status FROM scheduled_dms "
            "WHERE user_id = %s AND status IN (%s, %s, %s) "
            "AND scheduled_time >= UTC_TIMESTAMP() ORDER BY scheduled_time ASC LIMIT %s",
            (user_id, ScheduledDmStatus.PENDING.value, ScheduledDmStatus.APPROVED.value,
             ScheduledDmStatus.SCHEDULED.value, limit))
        for row in cursor.fetchall():
            title = (row.get("recipient_name") or "").strip() or (row.get("message") or "").strip()[:120]
            tasks.append({
                "kind": "DM",
                "id": row["id"],
                "title": title or "Scheduled DM",
                "scheduled_time": row["scheduled_time"],
                "status": row["status"],
            })

        # newsletter_editions has no status enum in code; 'draft'/'approved' are the non-terminal
        # states (mirrors get_pending_newsletter_editions), 'published'/'failed'/'skipped' terminal.
        cursor.execute(
            "SELECT id, title, scheduled_for, status FROM newsletter_editions "
            "WHERE user_id = %s AND status IN ('draft', 'approved') "
            "AND scheduled_for >= UTC_TIMESTAMP() ORDER BY scheduled_for ASC LIMIT %s",
            (user_id, limit))
        for row in cursor.fetchall():
            tasks.append({
                "kind": "Newsletter",
                "id": row["id"],
                "title": (row.get("title") or "").strip() or "Newsletter edition",
                "scheduled_time": row["scheduled_for"],
                "status": row["status"],
            })
    except mysql.connector.Error as err:
        log_error("Could not get planned tasks", exc=err, user_id=user_id)
        return []
    finally:
        cursor.close()
        connection.close()

    tasks.sort(key=lambda t: t["scheduled_time"])
    return tasks[:limit]
