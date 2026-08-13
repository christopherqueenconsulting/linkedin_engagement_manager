"""Issue #1215: the publishing workflow's SQL, against a real migrated MySQL.

These three checks came from `tests/e2e/test_post_workflow_e2e.py`, which asked exactly the right
questions and never got to answer them: the e2e lane had no MySQL service, so every one of them
reported SKIPPED on every CI run since it was written, under a workflow marked `continue-on-error`.
The lane is gone (#1215); the questions are not, so they move here, where the lane provisions the
schema and a red result is a red result.

Each one is deliberately about the SQL rather than the Python around it — the unit lane already
mocks the cursor and proves the control flow (`tests/unit/app/test_run_scheduler.py`,
`tests/unit/app/test_run_automation_posting.py`, `tests/unit/utilities/test_db.py`). What only a real
server can answer is whether the columns those queries name still exist and whether the status
filters really partition the rows the way the scheduler assumes.
"""

from datetime import datetime, timedelta, timezone

import mysql.connector
import pytest

from cqc_lem.utilities import db
from cqc_lem.utilities.db import PostStatus

pytestmark = pytest.mark.integration

_EMAIL = "post-workflow-1215@example.test"
_CONTENT = "Publishing-workflow integration post — safe to delete"


def _schema_available() -> bool:
    """True when the configured MySQL is both reachable and migrated.

    A reachable server is not enough: a bare local one skips rather than erroring. CI provisions the
    schema before the suite runs.
    """
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM posts LIKE 'manual_publish'")
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


def _insert_post(user_id: int, status: str, scheduled_time: datetime) -> int:
    _rows, post_id = _exec(
        "INSERT INTO posts (user_id, content, post_type, status, scheduled_time) "
        "VALUES (%s, %s, 'text', %s, %s)",
        (user_id, _CONTENT, status, scheduled_time),
    )
    return post_id


def _post_status(post_id: int) -> str:
    rows, _ = _exec("SELECT status FROM posts WHERE id=%s", (post_id,), fetch=True)
    return rows[0][0] if rows else None


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the publishing-workflow integration test")
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))  # CASCADE clears the posts and logs
    db.add_user(_EMAIL, "x")
    uid = db.get_user_id(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


@pytest.mark.integration
class TestSchedulerPickup:
    """The two SQL halves of the scheduler's hand-off, exercised directly.

    `auto_check_scheduled_posts` itself is not run here on purpose: it would transition every other
    approved post this worker's database happens to hold, and the wiring between the query and the
    status write is already covered with mocks in `tests/unit/app/test_run_scheduler.py`. What is
    NOT covered there is whether the SQL still matches the schema, which is what these assert.
    """

    def test_an_approved_post_is_selected_and_can_be_moved_to_scheduled(self, user_id):
        """The BETWEEN window really selects a just-due post, and 'scheduled' is a writable status.

        A status the ENUM does not accept would fail here and nowhere else — the unit lane's cursor
        accepts anything it is handed.
        """
        scheduled_time = datetime.now(timezone.utc) - timedelta(minutes=1)
        post_id = _insert_post(user_id, "approved", scheduled_time)

        ready = [row[0] for row in db.get_ready_to_post_posts()]
        assert post_id in ready, "a post due one minute ago must be inside the scheduler's window"

        assert db.update_db_post_status(post_id, PostStatus.SCHEDULED) is True
        assert _post_status(post_id) == "scheduled"

    def test_a_manual_publish_post_is_never_picked_up(self, user_id):
        """Issue #1074's guarantee, asserted where it is actually enforced — in the query's WHERE."""
        scheduled_time = datetime.now(timezone.utc) - timedelta(minutes=1)
        post_id = _insert_post(user_id, "approved", scheduled_time)
        _exec("UPDATE posts SET manual_publish = 1 WHERE id=%s", (post_id,))

        ready = [row[0] for row in db.get_ready_to_post_posts()]

        assert post_id not in ready
        assert _post_status(post_id) == "approved", "a manual-publish post must not be transitioned"


@pytest.mark.integration
class TestOrphanRecoveryQuery:
    def test_only_scheduled_rows_past_the_cutoff_are_orphans(self, user_id):
        """`get_orphaned_scheduled_posts` re-queues posts, so what it selects decides what republishes.

        An 'approved' row leaking into this result would be published twice — once here and once by
        the scheduler's own pass — which is why the status filter is worth a real-server assertion.
        """
        stale = datetime.now(timezone.utc) - timedelta(hours=5)
        orphan_id = _insert_post(user_id, "scheduled", stale)
        approved_id = _insert_post(user_id, "approved", stale)
        fresh_id = _insert_post(user_id, "scheduled", datetime.now(timezone.utc))

        found = [row[0] for row in db.get_orphaned_scheduled_posts(lookback_hours=2)]

        assert orphan_id in found
        assert approved_id not in found, "an 'approved' post is not an orphan"
        assert fresh_id not in found, "a post inside the lookback window may still be in flight"


@pytest.mark.integration
class TestAccessTokenColumns:
    def test_a_stored_token_round_trips_for_a_connected_user(self):
        """A stored token comes back out, so the query's column list still matches the schema.

        This is the regression the test was originally written for: `get_user_access_token` once
        named a `token_expiry` column that does not exist, and the MySQL error it raised blocked
        every post.

        Written through the encrypting writer rather than as a raw INSERT, because since #745 the
        column holds ciphertext sealed against `users.id` — a plaintext row would come back None and
        would prove nothing about the query.
        """
        if not _schema_available():
            pytest.skip("no migrated MySQL schema available for the access-token integration test")
        email = "post-workflow-token-1215@example.test"
        _exec("DELETE FROM users WHERE email=%s", (email,))
        try:
            db.add_user_with_access_token(email, "linkedin-sub-1215", "test-token-value", "5184000")
            uid = db.get_user_id(email)
            assert uid, "test user was not created"

            assert db.get_user_access_token(uid) == "test-token-value"
        finally:
            _exec("DELETE FROM users WHERE email=%s", (email,))
