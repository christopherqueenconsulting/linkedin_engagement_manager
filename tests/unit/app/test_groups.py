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

    def test_bare_bool_payload_only_touches_engagement(self):
        """The pre-#769 SPA bundle still sends {group_id: bool} — that must not reset post_enabled."""
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_groups_enabled
            assert set_groups_enabled(1, {"123": False}) is True
        sql = cur.execute.call_args[0][0]
        assert "enabled=%s" in sql and "post_enabled" not in sql
        assert cur.execute.call_args[0][1] == (0, 1, "123")

    def test_dict_payload_writes_both_flags(self):
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_groups_enabled
            assert set_groups_enabled(1, {"123": {"enabled": True, "post_enabled": False}}) is True
        sql, params = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        assert "enabled=%s" in sql and "post_enabled=%s" in sql
        assert params == (1, 0, 1, "123")

    def test_empty_group_state_writes_nothing(self):
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_groups_enabled
            assert set_groups_enabled(1, {"123": {}}) is True
        cur.execute.assert_not_called()

    def test_next_group_for_post_is_least_recently_posted(self):
        conn, cur = self._conn()
        cur.fetchone.return_value = {"group_id": "456", "group_name": "Sales"}
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_group_for_post
            assert get_next_group_for_post(1) == {"group_id": "456", "group_name": "Sales"}
        sql = cur.execute.call_args[0][0]
        # Posting is gated on post_enabled ALONE — a group can take posts without being commented in.
        assert "post_enabled=1" in sql and "enabled=1" not in sql.replace("post_enabled=1", "")
        # Never-posted groups sort first, then oldest first.
        assert "last_posted_at IS NULL DESC, last_posted_at ASC" in sql

    def test_next_group_for_post_none_when_nothing_opted_in(self):
        conn, cur = self._conn()
        cur.fetchone.return_value = None
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_group_for_post
            assert get_next_group_for_post(1) is None

    def test_record_group_post_stamps_the_row(self):
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_group_post
            assert record_group_post(1, "123") is True
        assert "last_posted_at=NOW()" in cur.execute.call_args[0][0]
        assert cur.execute.call_args[0][1] == (1, "123")


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


class TestPostToGroup:
    def _driver_patches(self):
        return patch(f"{_RA}.get_current_profile",
                     return_value=(MagicMock(), MagicMock(), "e", MagicMock()))

    def test_writes_for_that_group_and_stamps_rotation(self):
        """The post is generated FRESH for the named group (never a copy of a feed post), and only
        a post that actually shipped moves the rotation on."""
        from cqc_lem.app.run_automation import auto_post_to_group
        with self._driver_patches(), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}.generate_group_post", return_value="A useful insight.") as gen, \
             patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=MagicMock()), \
             patch(f"{_RA}.record_group_post") as rec, patch(f"{_RA}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders")
        assert result == "Posted to group"
        assert gen.call_args.kwargs["group_name"] == "AI Leaders"
        rec.assert_called_once_with(1, "123")

    def test_failed_post_leaves_the_group_next_in_line(self):
        from cqc_lem.app.run_automation import auto_post_to_group
        with self._driver_patches(), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}.generate_group_post", return_value="A useful insight."), \
             patch(f"{_RA}.click_first", return_value=None), \
             patch(f"{_RA}.record_group_post") as rec, patch(f"{_RA}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders")
        assert result == "Group share box not found"
        rec.assert_not_called()


class TestGroupDispatchers:
    def test_group_posts_target_the_rotated_group(self):
        from cqc_lem.app.run_scheduler import auto_group_posts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_DB}.get_next_group_for_post",
                   side_effect=lambda u: {"group_id": "g9", "group_name": "Sales"} if u == 1 else None), \
             patch("cqc_lem.app.run_automation.auto_post_to_group") as t:
            result = auto_group_posts()
        t.apply_async.assert_called_once_with(
            kwargs={'user_id': 1, 'group_id': "g9", 'group_name': "Sales"})
        assert "1/2" in result

    def test_group_posts_skip_users_without_a_session(self):
        from cqc_lem.app.run_scheduler import auto_group_posts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=False), \
             patch(f"{_DB}.get_next_group_for_post") as nxt, \
             patch("cqc_lem.app.run_automation.auto_post_to_group") as t:
            result = auto_group_posts()
        nxt.assert_not_called()
        t.apply_async.assert_not_called()
        assert "0/1" in result


    def test_group_engagement_dispatches_connected(self):
        from cqc_lem.app.run_scheduler import auto_group_engagement
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.has_linkedin_session", side_effect=lambda u: u == 1), \
             patch(f"{_RS}._stagger_due", return_value=True), \
             patch("cqc_lem.app.run_automation.auto_comment_in_groups") as t:
            result = auto_group_engagement()
        t.apply_async.assert_called_once()
        assert "1/2" in result

    def test_group_engagement_waits_for_each_users_slot(self):
        """Issue #554: the beat ticks every 15 min, so a connected user still only runs when
        their staggered slot on the single se_content lane comes up."""
        from cqc_lem.app.run_scheduler import auto_group_engagement, STAGGER_GROUP_ENGAGEMENT
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_RS}._stagger_due", side_effect=lambda u, *_: u == 2) as due, \
             patch("cqc_lem.app.run_automation.auto_comment_in_groups") as t:
            result = auto_group_engagement()
        t.apply_async.assert_called_once_with(kwargs={'user_id': 2})
        assert due.call_args[0][1] is STAGGER_GROUP_ENGAGEMENT
        assert "1/2" in result
