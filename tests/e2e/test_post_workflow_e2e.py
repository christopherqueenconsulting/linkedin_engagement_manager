"""End-to-end test for the post-publishing workflow.

Tests the complete path:
  DB (approved post) → auto_check_scheduled_posts → status=scheduled
  → post_to_linkedin → status=posted + activity log + reply commenting dispatched
  → orphaned-post recovery when the task is lost

Requires a running MySQL instance.  When run from the host machine (outside
Docker), set MYSQL_HOST=127.0.0.1 (MySQL is exposed on 0.0.0.0:3306 by
docker-compose).  When run inside the Docker network, the default `mysql_db`
hostname resolves correctly.

    # host-machine run:
    MYSQL_HOST=127.0.0.1 poetry run pytest tests/e2e/test_post_workflow_e2e.py -m e2e -v

    # inside docker network (e.g. CI service container):
    poetry run pytest tests/e2e/test_post_workflow_e2e.py -m e2e -v

Does NOT require a LinkedIn connection — the LinkedIn API call is mocked so no
real posts are created.

Cleanup is handled automatically by the module-scoped fixture.
"""

import os
import socket
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.e2e

# ---------------------------------------------------------------------------
# Allow the test to override MYSQL_HOST so it can be run from the host machine
# (where the Docker-internal hostname 'mysql_db' is not resolvable).
# ---------------------------------------------------------------------------
_E2E_MYSQL_HOST = os.environ.get("MYSQL_HOST_E2E") or os.environ.get("MYSQL_HOST", "127.0.0.1")
_E2E_MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))


def _mysql_reachable() -> bool:
    try:
        with socket.create_connection((_E2E_MYSQL_HOST, _E2E_MYSQL_PORT), timeout=2):
            return True
    except OSError:
        return False


_MYSQL_REACHABLE = _mysql_reachable()
_requires_mysql = pytest.mark.skipif(
    not _MYSQL_REACHABLE,
    reason=f"MySQL not reachable at {_E2E_MYSQL_HOST}:{_E2E_MYSQL_PORT}",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_URN = "urn:li:share:9999999999999"
_TEST_EMAIL = os.environ.get("TEST_LI_EMAIL", "workflow-e2e@test.internal")
_DB_MOD = "cqc_lem.utilities.db"


def _get_db():
    """Return a fresh MySQL connection, overriding the host for host-machine runs."""
    import cqc_lem.utilities.db as _db_mod
    _db_mod.MYSQL_HOST = _E2E_MYSQL_HOST
    _db_mod.MYSQL_PORT = _E2E_MYSQL_PORT
    return _db_mod.get_db_connection()


def _create_test_user(cursor, email: str) -> int:
    """Insert a minimal test user and return its id.  Uses INSERT IGNORE so
    the test is idempotent when the user already exists.
    """
    cursor.execute(
        """INSERT IGNORE INTO users (email, linkedin_connection_status, subscription_status)
           VALUES (%s, 'connected', 'active')""",
        (email,),
    )
    cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
    return cursor.fetchone()[0]


def _insert_approved_post(cursor, user_id: int, scheduled_time: datetime) -> int:
    """Insert a post in 'approved' status ready to be picked up by the scheduler."""
    cursor.execute(
        """INSERT INTO posts (user_id, content, post_type, status, scheduled_time)
           VALUES (%s, %s, 'text', 'approved', %s)""",
        (user_id, "E2E workflow test post — safe to delete", scheduled_time),
    )
    return cursor.lastrowid


def _get_post_status(cursor, post_id: int) -> str:
    cursor.execute("SELECT status FROM posts WHERE id = %s", (post_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def _get_latest_log(cursor, user_id: int, post_id: int):
    cursor.execute(
        "SELECT action_type, result, post_url FROM logs WHERE user_id=%s AND post_id=%s ORDER BY id DESC LIMIT 1",
        (user_id, post_id),
    )
    return cursor.fetchone()


# ---------------------------------------------------------------------------
# Module-scoped fixture — creates test data and cleans up after all tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def workflow_state():
    """Creates a test user + post, yields state dict, then deletes all test data."""
    if not _MYSQL_REACHABLE:
        pytest.skip(f"MySQL not reachable at {_E2E_MYSQL_HOST}:{_E2E_MYSQL_PORT}")
    conn = _get_db()
    cursor = conn.cursor()

    try:
        # Schedule the post 1 minute in the past so the scheduler window includes it
        scheduled_time = datetime.now(timezone.utc) - timedelta(minutes=1)

        user_id = _create_test_user(cursor, _TEST_EMAIL)
        post_id = _insert_approved_post(cursor, user_id, scheduled_time)
        conn.commit()

        yield {
            "user_id": user_id,
            "post_id": post_id,
            "scheduled_time": scheduled_time,
            "fake_urn": _FAKE_URN,
        }

    finally:
        # Cleanup — remove test posts and logs; leave the user row (IGNORE on insert)
        cursor.execute("DELETE FROM logs WHERE user_id = %s AND post_id IN (SELECT id FROM posts WHERE user_id = %s)", (user_id, user_id))
        cursor.execute("DELETE FROM posts WHERE user_id = %s AND content = 'E2E workflow test post — safe to delete'", (user_id,))
        conn.commit()
        cursor.close()
        conn.close()


# ---------------------------------------------------------------------------
# Test 1: scheduler picks up the approved post and marks it scheduled
# ---------------------------------------------------------------------------

class TestSchedulerPicksUpApprovedPost:
    def test_auto_check_transitions_post_to_scheduled(self, workflow_state):
        """auto_check_scheduled_posts must find the approved post and change its
        status to 'scheduled', then dispatch post_to_linkedin via apply_async.
        """
        post_id = workflow_state["post_id"]
        user_id = workflow_state["user_id"]

        mock_post_task = MagicMock()
        mock_post_task.apply_async = MagicMock()
        mock_commenting = MagicMock()
        mock_commenting.apply_async = MagicMock()
        mock_profile = MagicMock()
        mock_profile.apply_async = MagicMock()

        with patch("cqc_lem.app.run_scheduler.post_to_linkedin", mock_post_task), \
             patch("cqc_lem.app.run_scheduler.automate_commenting", mock_commenting), \
             patch("cqc_lem.app.run_scheduler.automate_profile_viewer_engagement", mock_profile), \
             patch("cqc_lem.app.run_scheduler.get_orphaned_scheduled_posts", return_value=[]):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            result = auto_check_scheduled_posts.run()

        # Scheduler must have found our post
        assert "1 post" in result or str(post_id) in result or "post" in result.lower()

        # post_to_linkedin must have been dispatched
        mock_post_task.apply_async.assert_called()
        dispatched_kwargs = mock_post_task.apply_async.call_args[1]["kwargs"]
        assert dispatched_kwargs["post_id"] == post_id
        assert dispatched_kwargs["user_id"] == user_id

        # Pre-post engagement tasks dispatched
        mock_commenting.apply_async.assert_called()
        mock_profile.apply_async.assert_called()

        # DB status updated to 'scheduled'
        conn = _get_db()
        cursor = conn.cursor()
        try:
            status = _get_post_status(cursor, post_id)
        finally:
            cursor.close()
            conn.close()

        assert status == "scheduled", f"Expected 'scheduled', got '{status}'"


# ---------------------------------------------------------------------------
# Test 2: post_to_linkedin posts successfully and updates all state
# ---------------------------------------------------------------------------

class TestPostToLinkedinSuccessPath:
    def test_post_published_updates_db_and_writes_log(self, workflow_state):
        """post_to_linkedin must: update status→posted, write success activity log,
        dispatch automate_reply_commenting.
        """
        post_id = workflow_state["post_id"]
        user_id = workflow_state["user_id"]
        fake_urn = workflow_state["fake_urn"]

        mock_reply_task = MagicMock()
        mock_reply_task.apply_async = MagicMock()

        # Patch share_on_linkedin at the run_automation import site and also
        # at the poster module level to cover both call paths.
        with patch("cqc_lem.app.run_automation.share_on_linkedin", return_value=fake_urn), \
             patch("cqc_lem.utilities.linkedin.poster.share_on_linkedin", return_value=fake_urn), \
             patch("cqc_lem.app.run_automation.automate_reply_commenting", mock_reply_task):
            from cqc_lem.app.run_automation import post_to_linkedin
            result = post_to_linkedin.run(user_id=user_id, post_id=post_id)

        assert "successfully" in result.lower(), f"Expected success, got: {result}"

        # Status must be 'posted'
        conn = _get_db()
        cursor = conn.cursor()
        try:
            status = _get_post_status(cursor, post_id)
            log_row = _get_latest_log(cursor, user_id, post_id)
        finally:
            cursor.close()
            conn.close()

        assert status == "posted", f"Expected 'posted', got '{status}'"

        # Activity log must record success with the LinkedIn URL
        assert log_row is not None, "No activity log row written"
        action_type, result_val, post_url = log_row
        assert action_type == "post"
        assert result_val == "success"
        assert fake_urn in (post_url or ""), f"Expected URN in post_url, got: {post_url}"

        # Reply commenting must be dispatched
        mock_reply_task.apply_async.assert_called_once()
        reply_kwargs = mock_reply_task.apply_async.call_args[1]["kwargs"]
        assert reply_kwargs["user_id"] == user_id
        assert reply_kwargs["post_id"] == post_id

    def test_already_posted_skips_duplicate(self, workflow_state):
        """If post_to_linkedin is called again for an already-posted post, it must
        skip without re-publishing.
        """
        post_id = workflow_state["post_id"]
        user_id = workflow_state["user_id"]

        with patch("cqc_lem.app.run_automation.share_on_linkedin") as mock_share:
            from cqc_lem.app.run_automation import post_to_linkedin
            result = post_to_linkedin.run(user_id=user_id, post_id=post_id)

        assert "already posted" in result.lower()
        mock_share.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: orphaned-post recovery (task lost after container restart)
# ---------------------------------------------------------------------------

class TestOrphanedPostRecovery:
    def test_orphaned_scheduled_post_is_requeued_by_scheduler(self):
        """A post stuck in 'scheduled' status (task lost on restart) must be
        re-dispatched by get_orphaned_scheduled_posts on the next scheduler pass.
        """
        stuck_time = datetime.now(timezone.utc) - timedelta(hours=3)
        orphaned = [(9999, stuck_time, 1)]

        mock_post_task = MagicMock()
        mock_post_task.apply_async = MagicMock()

        with patch("cqc_lem.app.run_scheduler.get_ready_to_post_posts", return_value=[]), \
             patch("cqc_lem.app.run_scheduler.get_orphaned_scheduled_posts", return_value=orphaned), \
             patch("cqc_lem.app.run_scheduler.post_to_linkedin", mock_post_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            result = auto_check_scheduled_posts.run()

        mock_post_task.apply_async.assert_called_once()
        assert "re-queued" in result
        dispatched = mock_post_task.apply_async.call_args[1]["kwargs"]
        assert dispatched["post_id"] == 9999
        assert dispatched["user_id"] == 1

    @_requires_mysql
    def test_get_orphaned_scheduled_posts_uses_correct_status_filter(self):
        """get_orphaned_scheduled_posts must only return posts with status='scheduled'."""
        from cqc_lem.utilities.db import get_orphaned_scheduled_posts

        # Insert a temporary post in 'approved' status — should NOT be returned
        conn = _get_db()
        cursor = conn.cursor()
        try:
            scheduled_time = datetime.now(timezone.utc) - timedelta(hours=5)
            cursor.execute(
                "INSERT INTO posts (user_id, content, post_type, status, scheduled_time) VALUES (1, 'orphan-test', 'text', 'approved', %s)",
                (scheduled_time,),
            )
            approved_id = cursor.lastrowid
            conn.commit()

            results = get_orphaned_scheduled_posts(lookback_hours=2)
            result_ids = [r[0] for r in results]
            assert approved_id not in result_ids, (
                "get_orphaned_scheduled_posts must NOT return 'approved' posts"
            )
        finally:
            cursor.execute("DELETE FROM posts WHERE id = %s", (approved_id,))
            conn.commit()
            cursor.close()
            conn.close()


# ---------------------------------------------------------------------------
# Test 4: get_user_access_token uses correct column names
# ---------------------------------------------------------------------------

class TestAccessTokenSqlCorrectness:
    @_requires_mysql
    def test_access_token_retrieved_for_connected_user(self):
        """Regression: get_user_access_token must NOT reference the non-existent
        'token_expiry' column (which caused a MySQL error blocking all posts).
        """
        from cqc_lem.utilities.db import get_user_access_token

        conn = _get_db()
        cursor = conn.cursor()
        try:
            # Insert a user with an access token and no expiry info
            cursor.execute(
                """INSERT IGNORE INTO users (email, access_token, linkedin_connection_status, subscription_status)
                   VALUES ('token-test-e2e@test.internal', 'test-token-value', 'connected', 'active')""",
            )
            cursor.execute(
                "SELECT id FROM users WHERE email = 'token-test-e2e@test.internal'"
            )
            user_id = cursor.fetchone()[0]
            conn.commit()

            token = get_user_access_token(user_id)
            assert token == "test-token-value", f"Expected token, got: {token}"
        finally:
            cursor.execute("DELETE FROM users WHERE email = 'token-test-e2e@test.internal'")
            conn.commit()
            cursor.close()
            conn.close()
