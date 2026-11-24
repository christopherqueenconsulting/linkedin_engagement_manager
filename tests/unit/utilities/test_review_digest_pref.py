"""Review-digest opt-out write path and the unreadable-follow-up reader (#2669)."""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_CUR = "cqc_lem.platform.db.repositories.outreach.db_cursor"


class TestReviewDigestPref:
    def test_new_account_defaults_on(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["review_digest_enabled"] is True

    def test_stored_null_reads_as_on(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"review_digest_enabled": None})
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["review_digest_enabled"] is True

    def test_stored_zero_reads_as_off(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"review_digest_enabled": 0})
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["review_digest_enabled"] is False

    def test_opt_out_is_written_as_zero(self):
        from cqc_lem.platform.db.repositories import users
        cur = MagicMock()
        cur.rowcount = 1
        ctx = MagicMock()
        ctx.__enter__.return_value = cur
        with patch.object(users, "_select_engagement_row", return_value=None), \
             patch.object(users, "db_cursor", return_value=ctx):
            assert users.update_engagement_preferences(1, {"review_digest_enabled": False}) is True
        sql, values = cur.execute.call_args[0]
        assert "review_digest_enabled" in sql
        idx = list(users._ENGAGEMENT_COLS).index("review_digest_enabled") + 1
        assert values[idx] == 0

    def test_put_omitting_the_field_keeps_what_is_stored(self):
        from cqc_lem.api.routers.user import EngagementPreferencesRequest
        assert EngagementPreferencesRequest(session_token="t").review_digest_enabled is None


class TestUnreadableFollowupReader:
    def test_selects_only_recent_unreadable_stops(self):
        from cqc_lem.platform.db.repositories import outreach
        cur = MagicMock()
        cur.fetchall.return_value = [{"id": 1}]
        ctx = MagicMock()
        ctx.__enter__.return_value = cur
        with patch(_CUR, return_value=ctx):
            assert outreach.get_recent_unreadable_stopped_followups(7, 48) == [{"id": 1}]
        sql, params = cur.execute.call_args[0]
        assert "stop_reason = %s" in sql and "updated_at >=" in sql
        assert params == (7, "stopped", "unreadable_thread", 48)

    def test_empty_on_db_error(self):
        from cqc_lem.platform.db.repositories import outreach
        with patch(_CUR, side_effect=mysql.connector.Error("boom")):
            assert outreach.get_recent_unreadable_stopped_followups(7, 48) == []
