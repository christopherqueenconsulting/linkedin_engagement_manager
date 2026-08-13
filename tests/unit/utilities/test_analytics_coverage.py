"""Unit tests for the analytics-coverage reads behind issue #809 — why the Engagement Analytics
panel measures a SUBSET of the account, and the backfill that stops older posts being unmeasurable
forever.

The fallback contract both reads share (a DB error is zero/empty, never a crash) is a
parametrized table (issue #1216); the SQL-shape assertions stay per-read because each
pins a different clause.
"""

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_ZEROS = {"posted_total": 0, "posted_in_window": 0}


class TestFallbacks:
    # (function name, args, the fallback a DB error must produce)
    @pytest.mark.parametrize("fname,args,expected", [
        ("get_post_coverage_counts", (7,), _ZEROS),
        ("get_uncaptured_posted_post_ids", (7,), []),
    ], ids=["get_post_coverage_counts", "get_uncaptured_posted_post_ids"])
    def test_db_error_returns_the_empty_reading(self, mock_database_connection, fname, args,
                                                expected):
        import cqc_lem.utilities.db as db
        mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")
        assert getattr(db, fname)(*args) == expected

    # A missing row is the same "nothing measured yet" reading as an error, not a crash.
    @pytest.mark.parametrize("row,expected", [
        ((30, 11), {"posted_total": 30, "posted_in_window": 11}),
        (None, _ZEROS),
    ], ids=["counts_row", "no_row"])
    def test_coverage_counts_read_off_the_row(self, mock_database_connection, row, expected):
        mock_database_connection["cursor"].fetchone.return_value = row
        from cqc_lem.utilities.db import get_post_coverage_counts
        assert get_post_coverage_counts(7, days=90 if row else 30) == expected


class TestGetPostCoverageCounts:
    def test_windows_only_the_second_count(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.fetchone.return_value = (0, 0)
        from cqc_lem.utilities.db import get_post_coverage_counts
        get_post_coverage_counts(7, days=30)
        sql, params = cursor.execute.call_args[0]
        assert sql.count("INTERVAL %s DAY") == 1
        assert params == ("posted", "posted", 30, 7)


class TestGetUncapturedPostedPostIds:
    def test_selects_posted_posts_with_no_stat_row_newest_first(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.fetchall.return_value = [(41,), (39,)]
        from cqc_lem.utilities.db import get_uncaptured_posted_post_ids
        assert get_uncaptured_posted_post_ids(7, days=90, limit=5) == [41, 39]
        sql, params = cursor.execute.call_args[0]
        assert "LEFT JOIN post_stats" in sql and "s.id IS NULL" in sql
        assert "ORDER BY p.scheduled_time DESC" in sql
        assert params == (7, "posted", 90, "post", "success", 5)

    def test_only_offers_posts_that_have_a_logged_permalink(self, mock_database_connection):
        """A post the sweep can't open never gains a stat row, so it would sit at the head of this
        capped list forever and starve every post behind it.
        """
        cursor = mock_database_connection["cursor"]
        cursor.fetchall.return_value = []
        from cqc_lem.utilities.db import get_uncaptured_posted_post_ids
        get_uncaptured_posted_post_ids(7)
        sql = cursor.execute.call_args[0][0]
        assert "EXISTS (SELECT 1 FROM logs l" in sql
        assert "l.post_url IS NOT NULL AND l.post_url <> ''" in sql

    def test_negative_limit_is_clamped(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.fetchall.return_value = []
        from cqc_lem.utilities.db import get_uncaptured_posted_post_ids
        get_uncaptured_posted_post_ids(7, days=90, limit=-3)
        assert cursor.execute.call_args[0][1][-1] == 0
