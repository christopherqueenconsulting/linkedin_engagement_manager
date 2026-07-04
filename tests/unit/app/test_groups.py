"""Unit tests for Groups engagement (enumeration, commenting, posting, dispatchers)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"
_DB = "cqc_lem.utilities.db"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


class TestUserGroupsDB:
    def _conn(self, fetch_all=None, rowcount=1):
        conn = MagicMock(); cur = MagicMock()
        cur.fetchall.return_value = fetch_all or []
        cur.rowcount = rowcount
        conn.cursor.return_value = cur
        return conn, cur

    def test_upsert_and_enabled_and_bulk(self):
        conn, cur = self._conn(fetch_all=[("123",), ("456",)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_user_group, get_enabled_group_ids, set_groups_enabled
            assert upsert_user_group(1, "123", "Growth Group") is True
            assert get_enabled_group_ids(1) == ["123", "456"]
            assert set_groups_enabled(1, {"123": False, "456": True}) is True


class TestSyncUserGroups:
    def test_upserts_enumerated(self):
        from cqc_lem.app.run_automation import auto_sync_user_groups
        with patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}._enumerate_joined_groups", return_value=[("1", "A"), ("2", "B")]), \
             patch(f"{_RA}.upsert_user_group") as up, patch(f"{_RA}.quit_gracefully"):
            result = auto_sync_user_groups.run(user_id=1)
        assert up.call_count == 2 and "Synced 2" in result


class TestCommentInGroups:
    def test_comments_each_enabled_group(self):
        from cqc_lem.app.run_automation import auto_comment_in_groups
        with patch(f"{_RA}.get_enabled_group_ids", return_value=["1", "2"]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_recent_engagers", return_value=set()), \
             patch(f"{_RA}.comment_on_feed_inline", return_value=1) as cfi, patch(f"{_RA}.quit_gracefully"):
            result = auto_comment_in_groups.run(user_id=1)
        assert cfi.call_count == 2 and "across 2 group" in result

    def test_no_enabled_groups(self):
        from cqc_lem.app.run_automation import auto_comment_in_groups
        with patch(f"{_RA}.get_enabled_group_ids", return_value=[]), \
             patch(f"{_RA}.get_current_profile") as gp:
            result = auto_comment_in_groups.run(user_id=1)
        assert "No enabled groups" in result
        gp.assert_not_called()


class TestGroupDispatchers:
    def test_group_engagement_dispatches_connected(self):
        from cqc_lem.app.run_scheduler import auto_group_engagement
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.has_linkedin_session", side_effect=lambda u: u == 1), \
             patch("cqc_lem.app.run_automation.auto_comment_in_groups") as t:
            result = auto_group_engagement()
        t.apply_async.assert_called_once()
        assert "1/2" in result
