"""Unit tests for the engagement-roster DB helpers (issue #616)."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


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

    def test_normalizes_active_flag(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[self._row(active=1)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            rows = get_engagement_targets(1)
        assert rows[0]["active"] is True

    def test_stale_week_counter_reads_as_zero(self, fake_cursor):
        # A cap spent LAST week must not keep an author out of this week's rotation.
        from cqc_lem.utilities.db import engagement_week_start
        stale = engagement_week_start() - timedelta(days=7)
        conn, _ = fake_cursor(fetch_all=[self._row(week_start=stale, comments_this_week=2)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            rows = get_engagement_targets(1)
        assert rows[0]["comments_this_week"] == 0

    def test_current_week_counter_is_kept(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[self._row(comments_this_week=2)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            rows = get_engagement_targets(1)
        assert rows[0]["comments_this_week"] == 2

    def test_a_zero_cap_reads_back_as_zero(self, fake_cursor):
        # 0 is the SPA's "pause this account without removing it" — reading it as unset would hand
        # the author the default cap back and show the operator a number they never typed.
        conn, _ = fake_cursor(fetch_all=[self._row(max_comments_per_week=0)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            rows = get_engagement_targets(1)
        assert rows[0]["max_comments_per_week"] == 0

    def test_a_missing_cap_falls_back_to_the_default(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[self._row(max_comments_per_week=None)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ENGAGEMENT_TARGET_WEEKLY_DEFAULT, get_engagement_targets
            rows = get_engagement_targets(1)
        assert rows[0]["max_comments_per_week"] == ENGAGEMENT_TARGET_WEEKLY_DEFAULT

    def test_active_only_filters_in_sql(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            get_engagement_targets(1, active_only=True)
        assert "active=1" in cursor.execute.call_args[0][0]

    def test_db_error_returns_empty(self, fake_cursor):
        import mysql.connector
        conn, cursor = fake_cursor()
        cursor.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            assert get_engagement_targets(1) == []


class TestUpsertEngagementTargets:
    def test_upserts_and_clamps(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engagement_targets
            ok = upsert_engagement_targets(1, [
                {"profile_url": "https://x/in/jane", "category": "bogus",
                 "max_comments_per_week": 99, "source": "elsewhere"}])
        assert ok is True
        row = cursor.executemany.call_args[0][1][0]
        assert row[3] == "peer"     # unknown category -> peer
        assert row[4] == 14         # cap clamped to the max
        assert row[6] == "user"     # unknown source -> user

    def test_blank_url_rows_are_dropped(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engagement_targets
            assert upsert_engagement_targets(1, [{"profile_url": "   "}]) is True
        cursor.executemany.assert_not_called()

    def test_non_numeric_cap_falls_back_to_the_default(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ENGAGEMENT_TARGET_WEEKLY_DEFAULT, upsert_engagement_targets
            upsert_engagement_targets(1, [{"profile_url": "https://x/in/j", "max_comments_per_week": "lots"}])
        assert cursor.executemany.call_args[0][1][0][4] == ENGAGEMENT_TARGET_WEEKLY_DEFAULT

    def test_does_not_write_the_weekly_counter(self, fake_cursor):
        # An operator editing the roster must never reset an author's spent cap.
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engagement_targets
            upsert_engagement_targets(1, [{"profile_url": "https://x/in/j"}])
        sql = cursor.executemany.call_args[0][0]
        assert "comments_this_week" not in sql and "last_engaged_at" not in sql


class TestRecordTargetEngagement:
    def test_increments_and_stamps(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_target_engagement
            assert record_target_engagement(1, "https://x/in/jane") is True
        sql = cursor.execute.call_args[0][0]
        assert "comments_this_week + 1" in sql and "last_engaged_at = NOW()" in sql

    def test_missing_row_is_false(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.rowcount = 0
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
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


# --- blocked-comment streak + follow state (issue #962) ------------------------------------------

class TestBlockedAndFollowDefaults:
    def _row(self, **kw):
        from cqc_lem.utilities.db import engagement_week_start
        row = {"id": 1, "profile_url": "https://x/in/jane", "name": "Jane", "category": "peer",
               "max_comments_per_week": 2, "active": 1, "last_engaged_at": None,
               "comments_this_week": 0, "week_start": engagement_week_start(), "source": "user",
               "comment_blocked_streak": None, "last_blocked_at": None, "follow_status": None,
               "followed_at": None, "follow_attempts": None, "connect_status": None,
               "connect_requested_at": None}
        row.update(kw)
        return row

    def test_null_columns_read_as_the_safe_defaults(self, fake_cursor):
        # Rows written before the migration come back with NULLs; the SPA must not see them.
        conn, _ = fake_cursor(fetch_all=[self._row()])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            row = get_engagement_targets(1)[0]
        assert row["comment_blocked_streak"] == 0
        assert row["follow_attempts"] == 0
        assert row["follow_status"] == "unknown"
        assert row["connect_status"] == "unknown"

    def test_an_unrecognised_follow_status_falls_back_to_unknown(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[self._row(follow_status="requested")])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            assert get_engagement_targets(1)[0]["follow_status"] == "unknown"

    def test_an_unrecognised_connect_status_falls_back_to_unknown(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[self._row(connect_status="following")])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            assert get_engagement_targets(1)[0]["connect_status"] == "unknown"

    def test_real_values_survive(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[self._row(comment_blocked_streak=3,
                                                  follow_status="follow_failed",
                                                  follow_attempts=2)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_targets
            row = get_engagement_targets(1)[0]
        assert (row["comment_blocked_streak"], row["follow_status"], row["follow_attempts"]) == \
            (3, "follow_failed", 2)

    def test_the_editable_upsert_never_writes_the_automation_columns(self, fake_cursor):
        # An operator saving the roster in the SPA must not reset a streak or a follow state.
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engagement_targets
            upsert_engagement_targets(1, [{"profile_url": "https://x/in/jane"}])
        sql = cursor.executemany.call_args[0][0]
        for col in ("comment_blocked_streak", "follow_status", "followed_at", "follow_attempts",
                    "connect_status", "connect_requested_at"):
            assert col not in sql


class TestRecordTargetCommentBlocked:
    def test_increments_and_returns_the_new_streak(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.fetchone.return_value = (2, "unknown")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_target_comment_blocked
            assert record_target_comment_blocked(1, "https://x/in/jane").streak == 2
        sql = cursor.execute.call_args_list[0][0][0]
        assert "comment_blocked_streak = LEAST(255, comment_blocked_streak + 1)" in sql
        assert "last_blocked_at = NOW()" in sql

    def test_an_unmatched_target_reports_no_streak(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.rowcount = 0
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_target_comment_blocked
            assert record_target_comment_blocked(1, "https://x/in/nobody").streak == 0

    def test_a_db_error_is_never_read_as_a_streak(self, fake_cursor):
        import mysql.connector

        from cqc_lem.utilities.db import ConnectStatus
        conn, cursor = fake_cursor()
        cursor.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_target_comment_blocked
            visit = record_target_comment_blocked(1, "https://x/in/jane")
        assert visit == (0, ConnectStatus.UNKNOWN.value)


# --- connect escalation (issue #979) -------------------------------------------------------------

class TestConnectEscalation:
    def _sql(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.fetchone.return_value = (2, "needs_connection")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_target_comment_blocked
            visit = record_target_comment_blocked(1, "https://x/in/jane")
        return cursor.execute.call_args_list[0][0], visit

    def test_the_escalation_is_decided_before_the_evidence_is_overwritten(self, fake_cursor):
        # last_blocked_at IS the evidence (the PREVIOUS blocked visit) and this same statement
        # overwrites it, so connect_status must be assigned first — MySQL evaluates SET clauses left
        # to right and a later one already sees the new value.
        (sql, params), _ = self._sql(fake_cursor)
        assert sql.index("connect_status") < sql.index("last_blocked_at = NOW()")
        assert "last_blocked_at > followed_at" in sql
        assert "followed_at IS NOT NULL" in sql

    def test_only_a_followed_target_can_escalate(self, fake_cursor):
        from cqc_lem.utilities.db import ConnectStatus, FollowStatus
        (sql, params), _ = self._sql(fake_cursor)
        # The guard is the parameter list, not prose: unknown -> needs_connection, and only while
        # follow_status is 'following'.
        assert params[:3] == (ConnectStatus.UNKNOWN.value, FollowStatus.FOLLOWING.value,
                              ConnectStatus.NEEDS_CONNECTION.value)

    def test_the_new_connect_state_comes_back_with_the_streak(self, fake_cursor):
        from cqc_lem.utilities.db import ConnectStatus
        _, visit = self._sql(fake_cursor)
        assert visit == (2, ConnectStatus.NEEDS_CONNECTION.value)

    def test_a_landed_comment_stands_a_pending_escalation_back_down(self, fake_cursor):
        # Commenting worked, so "following didn't unlock commenting" is no longer true. Only that
        # one state is cleared — an invite already sent is a fact a comment does not undo.
        from cqc_lem.utilities import db
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert db.record_target_engagement(1, "https://x/in/jane") is True
        sql = cursor.execute.call_args[0][0]
        assert "connect_status = IF(connect_status = 'needs_connection', 'unknown', connect_status)" \
            in sql


class TestConnectStatusEnum:
    def test_the_enum_is_the_migration_s_enum(self):
        # Same contract as FollowStatus: the Python vocabulary and the MySQL ENUM are one list, read
        # from the migration so a member added without one fails HERE and not at 3am.
        import re
        from pathlib import Path

        from cqc_lem.utilities.db import ConnectStatus
        sql = Path("compose/local/database/migrations/"
                   "V20260803083627__add_roster_connect_state.sql").read_text()
        declared = re.search(r"connect_status\s+ENUM\(([^)]*)\)", sql).group(1)
        assert {m.value for m in ConnectStatus} == set(re.findall(r"'([^']+)'", declared))

    def test_one_shot_states_are_terminal_for_automation(self):
        from cqc_lem.utilities.db import ENGAGEMENT_TARGET_CONNECT_TERMINAL, ConnectStatus
        assert "requested" in ENGAGEMENT_TARGET_CONNECT_TERMINAL
        assert "failed" in ENGAGEMENT_TARGET_CONNECT_TERMINAL
        assert "connected" in ENGAGEMENT_TARGET_CONNECT_TERMINAL
        assert ConnectStatus.NEEDS_CONNECTION not in ENGAGEMENT_TARGET_CONNECT_TERMINAL
        assert ConnectStatus.UNKNOWN not in ENGAGEMENT_TARGET_CONNECT_TERMINAL


class TestSetTargetConnectStatus:
    def test_requested_stamps_the_timestamp_once(self, fake_cursor):
        # A later read-only visit that re-observes Pending must not keep moving the date forward.
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_target_connect_status
            assert set_target_connect_status(1, "https://x/in/jane", "requested") is True
        assert "connect_requested_at=COALESCE(connect_requested_at, NOW())" in \
            cursor.execute.call_args[0][0]

    def test_standing_back_down_clears_the_stamp(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_target_connect_status
            set_target_connect_status(1, "https://x/in/jane", "needs_connection")
        assert "connect_requested_at=NULL" in cursor.execute.call_args[0][0]

    def test_other_statuses_leave_the_stamp_alone(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_target_connect_status
            set_target_connect_status(1, "https://x/in/jane", "connected")
        assert "connect_requested_at" not in cursor.execute.call_args[0][0]

    def test_an_unknown_status_is_refused_without_a_query(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_target_connect_status
            assert set_target_connect_status(1, "https://x/in/jane", "following") is False
        cursor.execute.assert_not_called()


class TestFollowStatusEnum:
    def test_the_enum_is_the_migration_s_enum(self):
        # The Python vocabulary and the MySQL ENUM are the same list or a write becomes a runtime
        # MySQL error. Read from the migration so adding a member without one fails HERE.
        import re
        from pathlib import Path

        from cqc_lem.utilities.db import FollowStatus
        sql = Path("compose/local/database/migrations/"
                   "V20260803032507__add_roster_follow_state.sql").read_text()
        declared = re.search(r"follow_status ENUM\(([^)]*)\)", sql).group(1)
        assert {m.value for m in FollowStatus} == set(re.findall(r"'([^']+)'", declared))

    def test_a_raw_column_value_compares_equal_to_a_member(self):
        # StrEnum, so a status read back off the DB needs no conversion at any boundary.
        from cqc_lem.utilities.db import ENGAGEMENT_TARGET_FOLLOW_TERMINAL, FollowStatus
        assert "following" == FollowStatus.FOLLOWING
        assert "following" in ENGAGEMENT_TARGET_FOLLOW_TERMINAL
        assert "follow_failed" in ENGAGEMENT_TARGET_FOLLOW_TERMINAL
        assert "not_following" not in ENGAGEMENT_TARGET_FOLLOW_TERMINAL


class TestSetTargetFollowStatus:
    def test_following_stamps_the_timestamp_and_clears_attempts(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_target_follow_status
            assert set_target_follow_status(1, "https://x/in/jane", "following") is True
        sql = cursor.execute.call_args[0][0]
        assert "followed_at=NOW()" in sql and "follow_attempts=0" in sql

    def test_other_statuses_do_not_stamp_followed_at(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_target_follow_status
            set_target_follow_status(1, "https://x/in/jane", "not_following")
        assert "followed_at" not in cursor.execute.call_args[0][0]

    def test_an_unknown_status_is_refused_without_a_query(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_target_follow_status
            assert set_target_follow_status(1, "https://x/in/jane", "requested") is False
        cursor.execute.assert_not_called()


class TestRecordTargetFollowFailure:
    def test_goes_terminal_at_the_attempt_cap(self, fake_cursor):
        from cqc_lem.utilities.db import ENGAGEMENT_TARGET_FOLLOW_MAX_ATTEMPTS
        conn, cursor = fake_cursor()
        cursor.fetchone.return_value = (2,)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_target_follow_failure
            assert record_target_follow_failure(1, "https://x/in/jane") == 2
        sql, params = cursor.execute.call_args_list[0][0]
        # follow_status is assigned BEFORE the increment, so the test reads the pre-increment count.
        assert sql.index("follow_status") < sql.index("follow_attempts = LEAST")
        assert params[0] == ENGAGEMENT_TARGET_FOLLOW_MAX_ATTEMPTS

    def test_an_unmatched_target_reports_no_attempts(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.rowcount = 0
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_target_follow_failure
            assert record_target_follow_failure(1, "https://x/in/nobody") == 0
