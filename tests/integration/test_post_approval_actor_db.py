"""Issue #2116: the approval actor, against a real migrated MySQL.

The unit lane proves which actor each caller passes. What only a real server can answer is the SQL
itself: the actor clause reads the row's OLD status because MySQL evaluates single-table UPDATE
assignments left to right, so a transition INTO 'approved' records the actor while a re-save of an
already-approved post, and a publish re-queue that passes none, keep whoever approved it.
"""

from datetime import datetime, timedelta, timezone

import mysql.connector
import pytest

from cqc_lem.utilities import db
from cqc_lem.utilities.db import PostApprover, PostStatus, PostType, user_approver

pytestmark = pytest.mark.integration

_EMAIL = "post-approval-actor-2116@example.test"
_CONTENT = "Approval-actor integration post — safe to delete"


def _schema_available() -> bool:
    """True when the configured MySQL is reachable AND carries this migration's column."""
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM posts LIKE 'approved_by'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=(), fetch: bool = False):
    connection = db.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if fetch else None
        connection.commit()
        return rows, cursor.lastrowid
    finally:
        cursor.close()
        connection.close()


def _insert_post(user_id: int, status: str) -> int:
    _rows, post_id = _exec(
        "INSERT INTO posts (user_id, content, post_type, status, scheduled_time) "
        "VALUES (%s, %s, 'text', %s, %s)",
        (user_id, _CONTENT, status, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)),
    )
    return post_id


def _approval(post_id: int) -> tuple:
    rows, _ = _exec("SELECT status, approved_by, approved_at FROM posts WHERE id=%s", (post_id,),
                    fetch=True)
    return rows[0]


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema with posts.approved_by available")
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))  # CASCADE clears the posts
    db.add_user(_EMAIL, "x")
    uid = db.get_user_id(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


def test_a_transition_into_approved_records_the_actor(user_id):
    post_id = _insert_post(user_id, PostStatus.PENDING.value)

    assert db.update_db_post_status(post_id, PostStatus.APPROVED, approved_by=PostApprover.RESCORE)

    status, approved_by, approved_at = _approval(post_id)
    assert (status, approved_by) == ("approved", "system:rescore")
    assert approved_at is not None


def test_a_requeue_keeps_the_approver_through_scheduled(user_id):
    post_id = _insert_post(user_id, PostStatus.PENDING.value)
    db.bulk_update_posts([post_id], status=PostStatus.APPROVED, user_id=user_id,
                         approved_by=user_approver(user_id))

    db.update_db_post_status(post_id, PostStatus.SCHEDULED)
    db.update_db_post_status(post_id, PostStatus.APPROVED)  # the publish path re-queueing it

    assert _approval(post_id)[:2] == ("approved", f"user:{user_id}")


def test_resaving_an_approved_post_keeps_the_first_approver(user_id):
    post_id = _insert_post(user_id, PostStatus.PENDING.value)
    db.update_db_post_status(post_id, PostStatus.APPROVED, approved_by=PostApprover.AUTO_SCHEDULE)

    db.update_db_post(_CONTENT, None, datetime.now(timezone.utc) + timedelta(days=2), PostType.TEXT,
                      post_id, PostStatus.APPROVED, user_id=user_id,
                      approved_by=user_approver(user_id))

    assert _approval(post_id)[1] == "system:auto_schedule"


def test_a_reapproval_after_review_records_the_new_actor(user_id):
    post_id = _insert_post(user_id, PostStatus.PENDING.value)
    db.update_db_post_status(post_id, PostStatus.APPROVED, approved_by=PostApprover.AUTO_SCHEDULE)
    db.update_db_post_status(post_id, PostStatus.PENDING)

    db.bulk_update_posts([post_id], status=PostStatus.APPROVED, user_id=user_id,
                         approved_by=user_approver(user_id))

    assert _approval(post_id)[1] == f"user:{user_id}"


def test_an_insert_approved_records_the_author(user_id):
    assert db.insert_post(_EMAIL, _CONTENT, datetime.now(timezone.utc) + timedelta(days=1),
                          PostType.TEXT, status=PostStatus.APPROVED,
                          approved_by=user_approver(user_id))

    rows, _ = _exec("SELECT approved_by, approved_at FROM posts WHERE user_id=%s", (user_id,),
                    fetch=True)
    assert rows[0][0] == f"user:{user_id}"
    assert rows[0][1] is not None
