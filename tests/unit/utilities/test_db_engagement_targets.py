"""Unit tests for the engagement-roster DB helpers (issue #616)."""

import pytest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_all=None):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = fetch_all or []
    cursor.rowcount = 1
    conn.cursor.return_value = cursor
    return conn, cursor


class TestWeekStart:
    def test_monday_is_the_boundary(self):
        from cqc_lem.utilities.db import engagement_week_start
        assert engagement_week_start(date(2026, 7, 26)) == date(2026, 7, 20)   # a Sunday
        assert engagement_week_start(date(2026, 7, 20)) == date(2026, 7, 20)   # the Monday itself


class TestGetEngagementTargets:
    def _row(self, **kw):
        from cqc_lem.utilities.db import engagement_week_start
        row = {"id": 1, "profile_url": "https://x/in/jane", "name": "Jane", "category": "peer",
               "max_comments_per_week": 2, "active": 1, "last_engaged_at": None,
               "comments_this_week": 2, "week_start": engagement_week_start(), "source": "user"}
        row.update(kw)
        return row

    def test_normalizes_active_flag(self):
        conn, _ = _mock_conn(fetch_all=[self._row(active=1)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            rows = get_engagement_targets(1)
        assert rows[0]["active"] is True

    def test_stale_week_counter_reads_as_zero(self):
        # A cap spent LAST week must not keep an author out of this week's rotation.
        from cqc_lem.utilities.db import engagement_week_start
        stale = engagement_week_start() - timedelta(days=7)
        conn, _ = _mock_conn(fetch_all=[self._row(week_start=stale, comments_this_week=2)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            rows = get_engagement_targets(1)
        assert rows[0]["comments_this_week"] == 0

    def test_current_week_counter_is_kept(self):
        conn, _ = _mock_conn(fetch_all=[self._row(comments_this_week=2)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            rows = get_engagement_targets(1)
        assert rows[0]["comments_this_week"] == 2

    def test_a_zero_cap_reads_back_as_zero(self):
        # 0 is the SPA's "pause this account without removing it" — reading it as unset would hand
        # the author the default cap back and show the operator a number they never typed.
        conn, _ = _mock_conn(fetch_all=[self._row(max_comments_per_week=0)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            rows = get_engagement_targets(1)
        assert rows[0]["max_comments_per_week"] == 0

    def test_a_missing_cap_falls_back_to_the_default(self):
        conn, _ = _mock_conn(fetch_all=[self._row(max_comments_per_week=None)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets, ENGAGEMENT_TARGET_WEEKLY_DEFAULT
            rows = get_engagement_targets(1)
        assert rows[0]["max_comments_per_week"] == ENGAGEMENT_TARGET_WEEKLY_DEFAULT

    def test_active_only_filters_in_sql(self):
        conn, cursor = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            get_engagement_targets(1, active_only=True)
        assert "active=1" in cursor.execute.call_args[0][0]

    def test_db_error_returns_empty(self):
        import mysql.connector
        conn, cursor = _mock_conn()
        cursor.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            assert get_engagement_targets(1) == []


class TestUpsertEngagementTargets:
    def test_upserts_and_clamps(self):
        conn, cursor = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engagement_targets
            ok = upsert_engagement_targets(1, [
                {"profile_url": "https://x/in/jane", "category": "bogus",
                 "max_comments_per_week": 99, "source": "elsewhere"}])
        assert ok is True
        row = cursor.executemany.call_args[0][1][0]
        assert row[3] == "peer"     # unknown category -> peer
        assert row[4] == 14         # cap clamped to the max
        assert row[6] == "user"     # unknown source -> user

    def test_blank_url_rows_are_dropped(self):
        conn, cursor = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engagement_targets
            assert upsert_engagement_targets(1, [{"profile_url": "   "}]) is True
        cursor.executemany.assert_not_called()

    def test_non_numeric_cap_falls_back_to_the_default(self):
        conn, cursor = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engagement_targets, ENGAGEMENT_TARGET_WEEKLY_DEFAULT
            upsert_engagement_targets(1, [{"profile_url": "https://x/in/j", "max_comments_per_week": "lots"}])
        assert cursor.executemany.call_args[0][1][0][4] == ENGAGEMENT_TARGET_WEEKLY_DEFAULT

    def test_does_not_write_the_weekly_counter(self):
        # An operator editing the roster must never reset an author's spent cap.
        conn, cursor = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engagement_targets
            upsert_engagement_targets(1, [{"profile_url": "https://x/in/j"}])
        sql = cursor.executemany.call_args[0][0]
        assert "comments_this_week" not in sql and "last_engaged_at" not in sql


class TestRecordTargetEngagement:
    def test_increments_and_stamps(self):
        conn, cursor = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_target_engagement
            assert record_target_engagement(1, "https://x/in/jane") is True
        sql = cursor.execute.call_args[0][0]
        assert "comments_this_week + 1" in sql and "last_engaged_at = NOW()" in sql

    def test_missing_row_is_false(self):
        conn, cursor = _mock_conn()
        cursor.rowcount = 0
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_target_engagement
            assert record_target_engagement(1, "https://x/in/nobody") is False


class TestSuggestEngagementTargets:
    def test_excludes_accounts_already_on_the_roster(self):
        with patch(f"{_DB}.get_engagement_targets",
                   return_value=[{"profile_url": "https://x/in/jane/"}]), \
             patch(f"{_DB}.get_engager_candidates", return_value=[
                 {"person_name": "Jane", "person_profile_url": "https://x/in/jane"},
                 {"person_name": "Bob", "person_profile_url": "https://x/in/bob"}]):
            from cqc_lem.utilities.db import suggest_engagement_targets
            out = suggest_engagement_targets(1)
        assert [s["name"] for s in out] == ["Bob"]
        assert out[0]["source"] == "suggested" and out[0]["category"] == "icp"

    def test_honours_the_limit_and_dedups(self):
        with patch(f"{_DB}.get_engagement_targets", return_value=[]), \
             patch(f"{_DB}.get_engager_candidates", return_value=[
                 {"person_name": "A", "person_profile_url": "https://x/in/a"},
                 {"person_name": "A again", "person_profile_url": "https://x/in/a"},
                 {"person_name": "B", "person_profile_url": "https://x/in/b"},
                 {"person_name": "No URL", "person_profile_url": ""}]):
            from cqc_lem.utilities.db import suggest_engagement_targets
            out = suggest_engagement_targets(1, limit=2)
        assert [s["profile_url"] for s in out] == ["https://x/in/a", "https://x/in/b"]

    def test_non_positive_limit_returns_nothing_without_querying(self):
        with patch(f"{_DB}.get_engagement_targets") as roster, \
             patch(f"{_DB}.get_engager_candidates") as candidates:
            from cqc_lem.utilities.db import suggest_engagement_targets
            assert suggest_engagement_targets(1, limit=0) == []
            assert suggest_engagement_targets(1, limit=-3) == []
        roster.assert_not_called()
        candidates.assert_not_called()
