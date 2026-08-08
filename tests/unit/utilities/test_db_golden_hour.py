"""Unit tests for the two DB reads the golden-hour work added (issue #622): the self-comment count
that caps seed + second wave, and the real publish time the latency report is measured from.
"""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_URL = "https://www.linkedin.com/feed/update/urn:li:ugcPost:1/"


def _mock_conn(fetchone=None, execute_error=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    if execute_error is not None:
        cur.execute.side_effect = execute_error
    conn.cursor.return_value = cur
    return conn, cur


class TestCountUserCommentsOnPostUrl:
    def test_counts_successful_comments_only(self):
        from cqc_lem.utilities.db import LogActionType, LogResultType
        conn, cur = _mock_conn(fetchone=(2,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_user_comments_on_post_url
            assert count_user_comments_on_post_url(1, _URL) == 2
        params = cur.execute.call_args[0][1]
        assert params == (1, _URL, LogActionType.COMMENT.value, LogResultType.SUCCESS.value)

    def test_db_error_counts_zero(self):
        conn, _ = _mock_conn(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_user_comments_on_post_url
            assert count_user_comments_on_post_url(1, _URL) == 0

    def test_has_commented_is_the_same_read(self):
        conn, _ = _mock_conn(fetchone=(0,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_user_commented_on_post_url
            assert has_user_commented_on_post_url(1, _URL) is False
        conn, _ = _mock_conn(fetchone=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_user_commented_on_post_url
            assert has_user_commented_on_post_url(1, _URL) is True


class TestGetPostAgeMinutes:
    def test_age_is_computed_in_sql_not_python(self):
        """`logs.created_at` is written in the DB session's zone (TZ, not UTC), so the subtraction
        has to happen against the server's own NOW() or every latency is off by the offset.
        """
        from cqc_lem.utilities.db import LogActionType, LogResultType
        conn, cur = _mock_conn(fetchone=(1320,))     # 22 minutes, in seconds
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_age_minutes
            assert get_post_age_minutes(1, 9) == 22.0
        sql, params = cur.execute.call_args[0]
        assert "TIMESTAMPDIFF(SECOND, created_at, NOW())" in sql and "FROM logs" in sql
        assert params == (1, 9, LogActionType.POST.value, LogResultType.SUCCESS.value)

    def test_clock_skew_never_reports_a_negative_age(self):
        conn, _ = _mock_conn(fetchone=(-30,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_age_minutes
            assert get_post_age_minutes(1, 9) == 0.0

    def test_unpublished_post_is_none(self):
        conn, _ = _mock_conn(fetchone=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_age_minutes
            assert get_post_age_minutes(1, 9) is None

    def test_db_error_is_none_not_zero(self):
        # None means "unknown", which the report renders as out-of-window — a 0 would silently
        # claim every sweep landed instantly.
        conn, _ = _mock_conn(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_age_minutes
            assert get_post_age_minutes(1, 9) is None
