"""Issue #2684 (MCP W1): the pending-only edit and the text-only draft, against a real MySQL.

The unit lane pins the SQL text. Only a real server can show the two facts it rests on: an UPDATE
whose predicate names `status` refuses a row that left pending, and an identical re-save (zero
CHANGED rows, since the connection does not set `CLIENT.FOUND_ROWS`) still reads as a success. The
asset backfill is checked here too, because a machine seat's draft must never be the post it spends
media on.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import mysql.connector
import pytest

from cqc_lem.utilities import db
from cqc_lem.utilities.db import (
    EMAIL_VERIFICATION_REQUIRED_MESSAGE,
    PendingEdit,
    PostStatus,
    PostType,
    attach_email_to_connection_request,
    update_pending_row,
)

pytestmark = pytest.mark.integration

_EMAIL = "queue-only-edits-2684@example.test"
_OTHER_EMAIL = "queue-only-edits-2684-other@example.test"


def _schema_available() -> bool:
    """True when the configured MySQL is reachable and carries the posts table."""
    try:
        connection = mysql.connector.connect(connect_timeout=3, **db._get_mysql_config())
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW TABLES LIKE 'posts'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params: tuple = (), fetch: bool = False) -> Any:
    connection = db.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if fetch else None
        connection.commit()
        return rows
    finally:
        cursor.close()
        connection.close()


def _row(post_id: int) -> tuple:
    return _exec("SELECT status, content, post_type FROM posts WHERE id=%s", (post_id,),
                 fetch=True)[0]


def _tomorrow() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=1)


@pytest.fixture
def users() -> Iterator[tuple[int, int]]:
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available")
    for email in (_EMAIL, _OTHER_EMAIL):
        _exec("DELETE FROM users WHERE email=%s", (email,))  # CASCADE clears the posts
        db.add_user(email, "x")
    uid, other = db.get_user_id(_EMAIL), db.get_user_id(_OTHER_EMAIL)
    assert uid and other, "test users were not created"
    yield uid, other
    for email in (_EMAIL, _OTHER_EMAIL):
        _exec("DELETE FROM users WHERE email=%s", (email,))


def _draft() -> int:
    post_id = db.insert_post_returning_id(_EMAIL, "draft", _tomorrow(), PostType.TEXT,
                                          status=PostStatus.PENDING)
    assert isinstance(post_id, int) and post_id > 0
    return post_id


def test_a_pending_row_is_edited_and_an_identical_resave_still_succeeds(users: tuple) -> None:
    uid, _other = users
    post_id = _draft()
    assert update_pending_row("post", post_id, uid, {"content": "revised"}) is PendingEdit.UPDATED
    assert _row(post_id)[1] == "revised"
    assert update_pending_row("post", post_id, uid, {"content": "revised"}) is PendingEdit.UPDATED


def test_a_row_a_human_approved_is_refused_and_left_untouched(users: tuple) -> None:
    uid, _other = users
    post_id = _draft()
    _exec("UPDATE posts SET status='approved' WHERE id=%s", (post_id,))
    assert update_pending_row("post", post_id, uid, {"content": "hijack"}) \
        is PendingEdit.NOT_PENDING
    assert _row(post_id)[:2] == ("approved", "draft")


def test_another_accounts_row_is_refused(users: tuple) -> None:
    _uid, other = users
    post_id = _draft()
    assert update_pending_row("post", post_id, other, {"content": "x"}) is PendingEdit.NOT_PENDING
    assert _row(post_id)[1] == "draft"


def test_the_asset_backfill_never_selects_a_pending_text_draft(users: tuple) -> None:
    post_id = _draft()
    # The control: a pending VIDEO post with no asset IS selected, so the query is not just empty.
    video_id = db.insert_post_returning_id(_EMAIL, "video", _tomorrow(), PostType.VIDEO,
                                           status=PostStatus.PENDING)
    selected = {row[0] for row in db.get_unposted_posts_missing_assets()}
    assert video_id in selected
    assert post_id not in selected


def test_revising_a_pending_carousel_keeps_its_type_and_slides(users: tuple) -> None:
    """The content-and-slot write a queue-only seat makes never touches the stored media."""
    uid, _other = users
    post_id = db.insert_post_returning_id(_EMAIL, "carousel", _tomorrow(), PostType.CAROUSEL,
                                          carousel_slides=["https://x/api/assets/1.png"],
                                          status=PostStatus.PENDING)
    when = _tomorrow() + timedelta(hours=2)
    assert update_pending_row("post", post_id, uid,
                              {"content": "revised", "scheduled_time": when}) is PendingEdit.UPDATED
    status, content, post_type = _row(post_id)
    slides = _exec("SELECT carousel_slides FROM posts WHERE id=%s", (post_id,), fetch=True)[0][0]
    assert (status, content, post_type) == ("pending", "revised", "carousel")
    assert "1.png" in slides


def test_the_backfill_carve_out_attaches_only_to_an_email_challenged_failed_request(
        users: tuple) -> None:
    uid, _other = users
    challenged = db.insert_connection_request(uid, "https://www.linkedin.com/in/w1-challenged/")
    declined = db.insert_connection_request(uid, "https://www.linkedin.com/in/w1-declined/")
    _exec("UPDATE connection_requests SET status='failed', failure_reason=%s WHERE id=%s",
          (EMAIL_VERIFICATION_REQUIRED_MESSAGE, challenged))
    _exec("UPDATE connection_requests SET status='failed', failure_reason='no connect' "
          "WHERE id=%s", (declined,))

    assert attach_email_to_connection_request(challenged, uid, "jane@example.test") \
        is PendingEdit.UPDATED
    assert attach_email_to_connection_request(declined, uid, "jane@example.test") \
        is PendingEdit.NOT_PENDING
    rows = {r[0]: (r[1], r[2]) for r in _exec(
        "SELECT id, status, recipient_email FROM connection_requests WHERE id IN (%s, %s)",
        (challenged, declined), fetch=True)}
    assert rows[challenged] == ("failed", "jane@example.test"), "the row stays failed"
    assert rows[declined] == ("failed", None)
