"""Unit tests for the persistent per-post comment-claim ledger (commented_posts)."""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


def _mock_conn(rowcount=1, fetchone=None, fetchall=None, execute_error=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    if execute_error is not None:
        cur.execute.side_effect = execute_error
    conn.cursor.return_value = cur
    return conn, cur


class TestClaimPostForComment:
    def test_win_returns_true(self):
        conn, cur = _mock_conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_post_for_comment
            assert claim_post_for_comment(1, "feedpost://abc") is True
        sql = cur.execute.call_args[0][0]
        assert "INSERT INTO commented_posts" in sql
        conn.commit.assert_called_once()

    def test_duplicate_key_loses_race(self):
        conn, _ = _mock_conn(execute_error=mysql.connector.IntegrityError("dup"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_post_for_comment
            # Second concurrent/sequential attempt on the same key loses cleanly.
            assert claim_post_for_comment(1, "feedpost://abc") is False

    def test_blank_key_noop(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection") as gc:
            from cqc_lem.utilities.db import claim_post_for_comment
            assert claim_post_for_comment(1, "  ") is False
        gc.assert_not_called()

    def test_db_error_returns_false(self):
        conn, _ = _mock_conn(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_post_for_comment
            assert claim_post_for_comment(1, "feedpost://x") is False


class TestHasCommentedPost:
    def test_true_when_present(self):
        conn, _ = _mock_conn(fetchone=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_commented_post
            assert has_commented_post(1, "feedpost://abc") is True

    def test_false_when_absent(self):
        conn, _ = _mock_conn(fetchone=(0,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_commented_post
            assert has_commented_post(1, "feedpost://abc") is False

    def test_false_when_table_missing(self):
        conn, _ = _mock_conn(execute_error=mysql.connector.Error("no table"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_commented_post
            assert has_commented_post(1, "feedpost://abc") is False


class TestMarkAndRelease:
    def test_mark_commented(self):
        conn, cur = _mock_conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_post_commented
            assert mark_post_commented(1, "feedpost://abc") is True
        assert "status='commented'" in cur.execute.call_args[0][0]

    def test_release_only_claimed(self):
        conn, cur = _mock_conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import release_post_claim
            assert release_post_claim(1, "feedpost://abc") is True
        sql = cur.execute.call_args[0][0]
        assert "DELETE FROM commented_posts" in sql and "status='claimed'" in sql


class TestGetDuplicateCommentPosts:
    def test_returns_rows(self):
        conn, cur = _mock_conn(fetchall=[("https://x/feed/update/1/", 3, "t0", "t1")])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_duplicate_comment_posts
            rows = get_duplicate_comment_posts(1, hours=24)
        assert rows == [("https://x/feed/update/1/", 3, "t0", "t1")]
        sql = cur.execute.call_args[0][0]
        assert "HAVING c > 1" in sql and "GROUP BY post_url" in sql

    def test_empty_on_error(self):
        conn, _ = _mock_conn(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_duplicate_comment_posts
            assert get_duplicate_comment_posts(1) == []


class TestStaleClaimTakeover:
    """A worker SIGKILLed by a deploy leaves a 'claimed' row it can never release; the re-queued
    task (task_acks_late) must be able to take it over or the comment is lost (issue #549).
    """

    def _dup_then(self, update_rowcount):
        conn = MagicMock()
        cur = MagicMock()
        cur.execute.side_effect = [mysql.connector.IntegrityError("dup"), None]
        type(cur).rowcount = property(lambda self: update_rowcount)
        conn.cursor.return_value = cur
        return conn, cur

    def test_takes_over_a_stale_claim(self):
        conn, cur = self._dup_then(update_rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_post_for_comment
            assert claim_post_for_comment(1, "feedpost://abc") is True
        sql, params = cur.execute.call_args[0]
        assert "UPDATE commented_posts SET status='claimed'" in sql
        # Never reclaims a row that already posted, and only past the staleness window.
        assert "status='claimed'" in sql and "INTERVAL %s MINUTE" in sql
        assert params[2] == 60

    def test_fresh_claim_is_not_stolen(self):
        conn, _ = self._dup_then(update_rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_post_for_comment
            assert claim_post_for_comment(1, "feedpost://abc") is False

    def test_custom_window_is_passed_through(self):
        conn, cur = self._dup_then(update_rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_post_for_comment
            assert claim_post_for_comment(1, "feedpost://abc", stale_after_minutes=5) is True
        assert cur.execute.call_args[0][1][2] == 5

    def test_window_floors_at_one_minute(self):
        conn, cur = self._dup_then(update_rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_post_for_comment
            claim_post_for_comment(1, "feedpost://abc", stale_after_minutes=0)
        assert cur.execute.call_args[0][1][2] == 1

    def test_takeover_error_returns_false(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.execute.side_effect = [mysql.connector.IntegrityError("dup"),
                                   mysql.connector.Error("takeover boom")]
        conn.cursor.return_value = cur
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_post_for_comment
            assert claim_post_for_comment(1, "feedpost://abc") is False
