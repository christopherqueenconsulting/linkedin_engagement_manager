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
        # Never-TRIED groups sort first, then oldest first. Ordering is on the run column, not the
        # success column — an unpostable group must not be "next" forever (issue #858).
        assert "COALESCE(last_post_run_at, last_posted_at) IS NULL DESC" in sql
        assert "COALESCE(last_post_run_at, last_posted_at) ASC" in sql

    def test_next_group_for_post_none_when_nothing_opted_in(self):
        conn, cur = self._conn()
        cur.fetchone.return_value = None
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_group_for_post
            assert get_next_group_for_post(1) is None

    def test_record_group_post_stamps_the_row(self):
        """A successful post is also a run, so both columns advance."""
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_group_post
            assert record_group_post(1, "123") is True
        sql = cur.execute.call_args[0][0]
        assert "last_posted_at=NOW()" in sql and "last_post_run_at=NOW()" in sql
        assert cur.execute.call_args[0][1] == (1, "123")

    def test_record_group_post_run_never_claims_a_post(self):
        """Issue #858: a try advances the rotation without pretending anything shipped."""
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_group_post_run
            assert record_group_post_run(1, "123") is True
        sql = cur.execute.call_args[0][0]
        assert "last_post_run_at=NOW()" in sql and "last_posted_at" not in sql.replace(
            "last_post_run_at", "")
        assert cur.execute.call_args[0][1] == (1, "123")

    def test_record_group_post_run_failure_is_visible(self):
        """A lost stamp leaves the group least-recently-tried — i.e. it re-creates the starvation —
        and the caller can do nothing with the False, so the failure has to log at ERROR."""
        import mysql.connector
        conn, cur = self._conn()
        cur.execute.side_effect = mysql.connector.Error("connection gone")
        with patch(f"{_DB}.get_db_connection", return_value=conn), \
             patch(f"{_DB}.log_error") as logged:
            from cqc_lem.utilities.db import record_group_post_run
            assert record_group_post_run(1, "123") is False
        logged.assert_called_once()
        assert logged.call_args.kwargs["user_id"] == 1


class TestGroupPostDraftDB:
    def _conn(self, fetch_one=None, lastrowid=7):
        conn = MagicMock(); cur = MagicMock()
        cur.fetchone.return_value = fetch_one
        cur.fetchall.return_value = []
        cur.lastrowid = lastrowid
        conn.cursor.return_value = cur
        return conn, cur

    def test_create_returns_the_new_id(self):
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_group_post_draft
            assert create_group_post_draft(1, "g1", "  An insight.  ", group_name="AI") == 7
        assert cur.execute.call_args[0][1] == (1, "g1", "AI", "An insight.", "ready")

    def test_create_refuses_empty_text(self):
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_group_post_draft
            assert create_group_post_draft(1, "g1", "   ") is None
        cur.execute.assert_not_called()

    def test_open_draft_reads_only_the_ready_row(self):
        conn, cur = self._conn(fetch_one={"id": 7, "user_id": 1, "group_id": "g1",
                                          "group_name": "AI", "content": "x", "status": "ready",
                                          "created_at": None, "updated_at": None,
                                          "published_at": None})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_open_group_post_draft
            assert get_open_group_post_draft(1)["id"] == 7
        assert cur.execute.call_args[0][1] == (1, "ready")

    def test_open_draft_is_none_when_nothing_is_queued(self):
        conn, _ = self._conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_open_group_post_draft
            assert get_open_group_post_draft(1) is None

    def test_publishing_stamps_the_ship_time_with_the_status(self):
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_group_post_draft, GroupPostDraftStatus
            assert update_group_post_draft(7, status=GroupPostDraftStatus.PUBLISHED) is True
        sql = cur.execute.call_args[0][0]
        assert "status = %s" in sql and "published_at = NOW()" in sql
        assert cur.execute.call_args[0][1] == ("published", 7)

    def test_skipping_never_claims_a_ship_time(self):
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_group_post_draft, GroupPostDraftStatus
            assert update_group_post_draft(7, status=GroupPostDraftStatus.SKIPPED) is True
        assert "published_at" not in cur.execute.call_args[0][0]

    def test_edit_writes_the_trimmed_text(self):
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_group_post_draft
            assert update_group_post_draft(7, content="  my words  ") is True
        assert cur.execute.call_args[0][1] == ("my words", 7)

    def test_update_with_nothing_to_write_is_a_no_op(self):
        conn, cur = self._conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_group_post_draft
            assert update_group_post_draft(7) is False
        cur.execute.assert_not_called()

    def test_post_enabled_ids_read_the_posting_flag_not_the_commenting_one(self):
        conn, cur = self._conn()
        cur.fetchall.return_value = [("g1",), ("g2",)]
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_enabled_group_ids
            assert get_post_enabled_group_ids(1) == ["g1", "g2"]
        assert "post_enabled=1" in cur.execute.call_args[0][0]

    def test_an_unreadable_switch_answers_unknown_not_opted_out(self):
        """[] means "no group takes posts", which CANCELS a reviewed draft — a failed read must
        never be able to say that, so it answers None."""
        import mysql.connector
        conn, cur = self._conn()
        cur.execute.side_effect = mysql.connector.Error("db down")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_enabled_group_ids
            assert get_post_enabled_group_ids(1) is None


class TestDraftGroupPost:
    def test_writes_the_draft_for_that_group_without_a_browser(self):
        """Issue #932: the text is written days ahead off the CACHED profile — no Chrome session."""
        from cqc_lem.app.run_automation import auto_draft_group_post
        with patch(f"{_RA}.get_open_group_post_draft", return_value=None), \
             patch(f"{_RA}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}.generate_group_post", return_value="A useful insight.") as gen, \
             patch(f"{_RA}.create_group_post_draft", return_value=11) as create, \
             patch(f"{_RA}.get_current_profile") as gcp:
            result = auto_draft_group_post.run(user_id=1, group_id="g1", group_name="AI Leaders")
        assert result == "Drafted group post 11"
        gcp.assert_not_called()
        assert gen.call_args.kwargs["group_name"] == "AI Leaders"
        create.assert_called_once_with(1, "g1", "A useful insight.", group_name="AI Leaders")

    def test_an_unpublished_draft_is_never_replaced(self):
        """The waiting draft may already carry the user's edits — a second generation would bin them."""
        from cqc_lem.app.run_automation import auto_draft_group_post
        with patch(f"{_RA}.get_open_group_post_draft", return_value={"id": 3}), \
             patch(f"{_RA}.generate_group_post") as gen, \
             patch(f"{_RA}.create_group_post_draft") as create:
            result = auto_draft_group_post.run(user_id=1, group_id="g1")
        assert "already awaiting review" in result
        gen.assert_not_called()
        create.assert_not_called()

    def test_no_cached_profile_skips_quietly(self):
        from cqc_lem.app.run_automation import auto_draft_group_post
        with patch(f"{_RA}.get_open_group_post_draft", return_value=None), \
             patch(f"{_RA}.load_profile_for_user", return_value=None), \
             patch(f"{_RA}.generate_group_post") as gen, \
             patch(f"{_RA}.log_warning") as warned:
            result = auto_draft_group_post.run(user_id=1, group_id="g1")
        assert result == "No cached profile to draft from"
        gen.assert_not_called()
        warned.assert_not_called()

    def test_empty_generation_stores_nothing(self):
        from cqc_lem.app.run_automation import auto_draft_group_post
        with patch(f"{_RA}.get_open_group_post_draft", return_value=None), \
             patch(f"{_RA}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}.generate_group_post", return_value="   "), \
             patch(f"{_RA}.create_group_post_draft") as create:
            result = auto_draft_group_post.run(user_id=1, group_id="g1")
        assert result == "No group post generated"
        create.assert_not_called()


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


_READY_DRAFT = {"id": 11, "user_id": 1, "group_id": "123", "group_name": "AI Leaders",
                "content": "A useful insight.", "status": "ready"}


class TestPostToGroup:
    def _driver_patches(self):
        return patch(f"{_RA}.get_current_profile",
                     return_value=(MagicMock(), MagicMock(), "e", MagicMock()))

    def test_publishes_the_reviewed_draft_and_stamps_rotation(self):
        """Issue #932: the published text is the draft the user could read and revise — nothing is
        generated here. Only a post that actually shipped moves the rotation on."""
        from cqc_lem.app.run_automation import auto_post_to_group
        with self._driver_patches(), \
             patch(f"{_RA}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_RA}.generate_group_post") as gen, \
             patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=MagicMock()) as box, \
             patch(f"{_RA}.record_group_post") as rec, \
             patch(f"{_RA}.update_group_post_draft") as upd, \
             patch(f"{_RA}.record_group_post_run") as run, patch(f"{_RA}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders",
                                            draft_id=11)
        assert result == "Posted to group"
        gen.assert_not_called()
        box.return_value.send_keys.assert_called_once_with("A useful insight.")
        rec.assert_called_once_with(1, "123")
        assert str(upd.call_args.kwargs["status"]) == "published"
        # record_group_post stamps both columns itself — the success path never double-stamps.
        run.assert_not_called()

    @pytest.mark.parametrize("draft", [
        None,
        {**_READY_DRAFT, "status": "skipped"},
        {**_READY_DRAFT, "user_id": 2},
    ])
    def test_never_publishes_without_a_reviewed_draft(self, draft):
        """A draft that was skipped, belongs to someone else, or is simply gone means NO post —
        falling back to a fresh generation would ship the un-previewed post #932 removed."""
        from cqc_lem.app.run_automation import auto_post_to_group
        with patch(f"{_RA}.get_group_post_draft", return_value=draft), \
             patch(f"{_RA}.get_current_profile") as gcp, \
             patch(f"{_RA}.generate_group_post") as gen, \
             patch(f"{_RA}.record_group_post") as rec, \
             patch(f"{_RA}.record_group_post_run") as run:
            result = auto_post_to_group.run(user_id=1, group_id="123", draft_id=11)
        assert result == "No group post draft to publish"
        gcp.assert_not_called()
        gen.assert_not_called()
        rec.assert_not_called()
        run.assert_not_called()

    def test_a_dispatch_carrying_no_draft_id_publishes_nothing(self):
        """The pre-#932 call shape (no draft) must not resurrect the un-previewed publish path."""
        from cqc_lem.app.run_automation import auto_post_to_group
        with patch(f"{_RA}.get_group_post_draft") as get_draft, \
             patch(f"{_RA}.get_current_profile") as gcp:
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders")
        assert result == "No group post draft to publish"
        get_draft.assert_not_called()
        gcp.assert_not_called()

    @pytest.mark.parametrize("miss,expected", [
        ("share_box", "Group share box not found"),
        ("editor", "Group post editor not found"),
        ("post_button", "Group Post button not found"),
    ])
    def test_unpostable_group_advances_the_rotation_without_claiming_a_post(self, miss, expected):
        """Issue #858: a group whose composer never renders (admin-only / announcement) is stamped
        as TRIED so it moves to the back of the queue — but `last_posted_at` stays untouched. The
        draft was written for THAT group, so it dies with the group's turn (#932)."""
        from cqc_lem.app.run_automation import auto_post_to_group
        clicks = {"share_box": [None], "editor": [MagicMock()],
                  "post_button": [MagicMock(), None]}[miss]
        with self._driver_patches(), \
             patch(f"{_RA}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_RA}.click_first", side_effect=clicks), \
             patch(f"{_RA}.find_first", return_value=None if miss == "editor" else MagicMock()), \
             patch(f"{_RA}.record_group_post") as rec, \
             patch(f"{_RA}.update_group_post_draft") as upd, \
             patch(f"{_RA}.record_group_post_run") as run, patch(f"{_RA}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders",
                                            draft_id=11)
        assert result == expected
        run.assert_called_once_with(1, "123")
        rec.assert_not_called()
        assert str(upd.call_args.kwargs["status"]) == "failed"

    def test_failure_before_the_group_is_reached_does_not_advance_the_rotation(self):
        """A dead session is transient and not the group's fault — it keeps its turn, and the draft
        stays open so next week's run publishes the text the user already approved."""
        from cqc_lem.app.run_automation import auto_post_to_group
        with patch(f"{_RA}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_RA}.get_current_profile", side_effect=Exception("no session")), \
             patch(f"{_RA}.record_group_post") as rec, \
             patch(f"{_RA}.update_group_post_draft") as upd, \
             patch(f"{_RA}.record_group_post_run") as run:
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders",
                                            draft_id=11)
        assert "Failed" in result
        rec.assert_not_called()
        run.assert_not_called()
        upd.assert_not_called()


class TestGroupDispatchers:
    def test_drafts_target_the_rotated_group(self):
        from cqc_lem.app.run_scheduler import auto_group_post_drafts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_DB}.get_next_group_for_post",
                   side_effect=lambda u: {"group_id": "g9", "group_name": "Sales"} if u == 1 else None), \
             patch("cqc_lem.app.run_automation.auto_draft_group_post") as t:
            result = auto_group_post_drafts()
        t.apply_async.assert_called_once_with(
            kwargs={'user_id': 1, 'group_id': "g9", 'group_name': "Sales"})
        assert "1/2" in result

    def test_drafts_skip_users_without_a_session(self):
        """Drafting opens no browser, but a user who cannot publish must not cost an LLM call."""
        from cqc_lem.app.run_scheduler import auto_group_post_drafts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=False), \
             patch(f"{_DB}.get_next_group_for_post") as nxt, \
             patch("cqc_lem.app.run_automation.auto_draft_group_post") as t:
            result = auto_group_post_drafts()
        nxt.assert_not_called()
        t.apply_async.assert_not_called()
        assert "0/1" in result

    def test_group_posts_publish_the_reviewed_draft(self):
        from cqc_lem.app.run_scheduler import auto_group_posts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_DB}.get_open_group_post_draft",
                   side_effect=lambda u: dict(_READY_DRAFT, group_id="g9", group_name="Sales") if u == 1 else None), \
             patch(f"{_DB}.get_post_enabled_group_ids", return_value=["g9"]), \
             patch("cqc_lem.app.run_automation.auto_post_to_group") as t:
            result = auto_group_posts()
        t.apply_async.assert_called_once_with(
            kwargs={'user_id': 1, 'group_id': "g9", 'group_name': "Sales", 'draft_id': 11})
        assert "1/2" in result

    def test_group_posts_skip_users_without_a_session(self):
        from cqc_lem.app.run_scheduler import auto_group_posts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=False), \
             patch(f"{_DB}.get_open_group_post_draft") as draft, \
             patch("cqc_lem.app.run_automation.auto_post_to_group") as t:
            result = auto_group_posts()
        draft.assert_not_called()
        t.apply_async.assert_not_called()
        assert "0/1" in result

    def test_a_draft_whose_group_was_switched_off_is_dropped_not_published(self):
        """Turning Post off for a group between the draft and the slot means no post lands there."""
        from cqc_lem.app.run_scheduler import auto_group_posts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_DB}.get_open_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_DB}.get_post_enabled_group_ids", return_value=["other"]), \
             patch(f"{_DB}.update_group_post_draft") as upd, \
             patch("cqc_lem.app.run_automation.auto_post_to_group") as t:
            result = auto_group_posts()
        t.apply_async.assert_not_called()
        assert str(upd.call_args.kwargs["status"]) == "skipped"
        assert "0/1" in result

    def test_unreadable_post_switches_hold_the_draft_rather_than_cancelling_it(self):
        """A DB hiccup must not be able to cancel a post the user read and approved — the draft
        stays open and ships at the next slot."""
        from cqc_lem.app.run_scheduler import auto_group_posts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_DB}.get_open_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_DB}.get_post_enabled_group_ids", return_value=None), \
             patch(f"{_DB}.update_group_post_draft") as upd, \
             patch("cqc_lem.app.run_automation.auto_post_to_group") as t:
            result = auto_group_posts()
        t.apply_async.assert_not_called()
        upd.assert_not_called()
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


class TestGroupPostBeats:
    def test_the_draft_beat_lands_before_the_publish_beat(self):
        """The review window IS the feature (#932): a draft written after the publish slot would
        only ever be read once the post it describes had already gone out."""
        from cqc_lem.app.my_celery import app
        drafts = app.conf.beat_schedule["group-post-drafts"]
        posts = app.conf.beat_schedule["group-posts"]
        assert drafts["task"] == "cqc_lem.app.run_scheduler.auto_group_post_drafts"
        assert posts["task"] == "cqc_lem.app.run_scheduler.auto_group_posts"
        assert min(drafts["schedule"].day_of_week) < min(posts["schedule"].day_of_week)
