"""Unit tests for Groups engagement (enumeration, commenting, posting, dispatchers)."""

from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import SoftTimeLimitExceeded

pytestmark = pytest.mark.unit

_FEED = "cqc_lem.app.engagement.feed"
_RS = "cqc_lem.app.run_scheduler"
_DB = "cqc_lem.utilities.db"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_FEED}.time.sleep"):
        yield


def _clock(*values):
    """A `time.time` stand-in that walks the given instants and then holds the last one.

    Holding rather than raising StopIteration keeps a test pinned to the behaviour it is asserting,
    instead of failing on however many clock reads the code happens to make after the interesting one.
    """
    remaining = list(values)
    held = {"v": values[-1]}

    def _now():
        if remaining:
            held["v"] = remaining.pop(0)
        return held["v"]

    return _now


class TestUserGroupsDB:
    def test_upsert_and_enabled_and_bulk(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[("123",), ("456",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_enabled_group_ids, set_groups_enabled, upsert_user_group
            assert upsert_user_group(1, "123", "Growth Group") is True
            assert get_enabled_group_ids(1) == ["123", "456"]
            assert set_groups_enabled(1, {"123": False, "456": True}) is True

    def test_bare_bool_payload_only_touches_engagement(self, fake_cursor):
        """The pre-#769 SPA bundle still sends {group_id: bool} — that must not reset post_enabled."""
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_groups_enabled
            assert set_groups_enabled(1, {"123": False}) is True
        sql = cur.execute.call_args[0][0]
        assert "enabled=%s" in sql and "post_enabled" not in sql
        assert cur.execute.call_args[0][1] == (0, 1, "123")

    def test_dict_payload_writes_both_flags(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_groups_enabled
            assert set_groups_enabled(1, {"123": {"enabled": True, "post_enabled": False}}) is True
        sql, params = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        assert "enabled=%s" in sql and "post_enabled=%s" in sql
        assert params == (1, 0, 1, "123")

    def test_empty_group_state_writes_nothing(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_groups_enabled
            assert set_groups_enabled(1, {"123": {}}) is True
        cur.execute.assert_not_called()

    def test_next_group_for_post_is_least_recently_posted(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"group_id": "456", "group_name": "Sales"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_group_for_post
            assert get_next_group_for_post(1) == {"group_id": "456", "group_name": "Sales"}
        sql = cur.execute.call_args[0][0]
        # Posting is gated on post_enabled ALONE — a group can take posts without being commented in.
        assert "post_enabled=1" in sql and "enabled=1" not in sql.replace("post_enabled=1", "")
        # Never-TRIED groups sort first, then oldest first. Ordering is on the run column, not the
        # success column — an unpostable group must not be "next" forever (issue #858).
        assert "COALESCE(last_post_run_at, last_posted_at) IS NULL DESC" in sql
        assert "COALESCE(last_post_run_at, last_posted_at) ASC" in sql

    def test_next_group_for_post_none_when_nothing_opted_in(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_group_for_post
            assert get_next_group_for_post(1) is None

    def test_record_group_post_stamps_the_row(self, fake_cursor):
        """A successful post is also a run, so both columns advance."""
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_group_post
            assert record_group_post(1, "123") is True
        sql = cur.execute.call_args[0][0]
        assert "last_posted_at=NOW()" in sql and "last_post_run_at=NOW()" in sql
        assert cur.execute.call_args[0][1] == (1, "123")

    def test_record_group_post_run_never_claims_a_post(self, fake_cursor):
        """Issue #858: a try advances the rotation without pretending anything shipped."""
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_group_post_run
            assert record_group_post_run(1, "123") is True
        sql = cur.execute.call_args[0][0]
        assert "last_post_run_at=NOW()" in sql and "last_posted_at" not in sql.replace(
            "last_post_run_at", "")
        assert cur.execute.call_args[0][1] == (1, "123")

    def test_record_group_post_run_failure_is_visible(self, fake_cursor):
        """A lost stamp leaves the group least-recently-tried — i.e. it re-creates the starvation —
        and the caller can do nothing with the False, so the failure has to log at ERROR.
        """
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("connection gone")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch("cqc_lem.platform.db.repositories.groups.log_error") as logged:
            from cqc_lem.utilities.db import record_group_post_run
            assert record_group_post_run(1, "123") is False
        logged.assert_called_once()
        assert logged.call_args.kwargs["user_id"] == 1


class TestGroupPostDraftDB:
    def test_create_returns_the_new_id(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_group_post_draft
            assert create_group_post_draft(1, "g1", "  An insight.  ", group_name="AI") == 7
        assert cur.execute.call_args[0][1] == (1, "g1", "AI", "An insight.", "ready")

    def test_create_refuses_empty_text(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_group_post_draft
            assert create_group_post_draft(1, "g1", "   ") is None
        cur.execute.assert_not_called()

    def test_open_draft_reads_only_the_ready_row(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 7, "user_id": 1, "group_id": "g1",
                                          "group_name": "AI", "content": "x", "status": "ready",
                                          "created_at": None, "updated_at": None,
                                          "published_at": None}, lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_open_group_post_draft
            assert get_open_group_post_draft(1)["id"] == 7
        assert cur.execute.call_args[0][1] == (1, "ready")

    def test_open_draft_is_none_when_nothing_is_queued(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None, lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_open_group_post_draft
            assert get_open_group_post_draft(1) is None

    def test_publishing_stamps_the_ship_time_with_the_status(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import GroupPostDraftStatus, update_group_post_draft
            assert update_group_post_draft(7, status=GroupPostDraftStatus.PUBLISHED) is True
        sql = cur.execute.call_args[0][0]
        assert "status = %s" in sql and "published_at = NOW()" in sql
        assert cur.execute.call_args[0][1] == ("published", 7)

    def test_skipping_never_claims_a_ship_time(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import GroupPostDraftStatus, update_group_post_draft
            assert update_group_post_draft(7, status=GroupPostDraftStatus.SKIPPED) is True
        assert "published_at" not in cur.execute.call_args[0][0]

    def test_edit_writes_the_trimmed_text(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_group_post_draft
            assert update_group_post_draft(7, content="  my words  ") is True
        assert cur.execute.call_args[0][1] == ("my words", 7)

    def test_update_with_nothing_to_write_is_a_no_op(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_group_post_draft
            assert update_group_post_draft(7) is False
        cur.execute.assert_not_called()

    def test_attaching_media_writes_the_kind_alongside_the_url(self, fake_cursor):
        """A row can never say "there is media" without saying what it is (issue #1224).

        The publish run reads the kind to decide what it is handing the composer.
        """
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import GroupPostMediaType, update_group_post_draft
            assert update_group_post_draft(7, media_url="http://x/api/assets?file_name=a.png",
                                           media_type=GroupPostMediaType.IMAGE) is True
        assert cur.execute.call_args[0][1] == ("http://x/api/assets?file_name=a.png", "image", 7)

    def test_clearing_media_is_distinct_from_saying_nothing_about_it(self, fake_cursor):
        """The three-valued `media_url`: an explicit None detaches, an OMITTED one changes nothing.

        A text edit that dropped the author's image a minute after they attached it is the bug this
        shape exists to prevent.
        """
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_group_post_draft
            assert update_group_post_draft(7, media_url=None) is True
            assert cur.execute.call_args[0][1] == (None, None, 7)

            cur.execute.reset_mock()
            assert update_group_post_draft(7, content="just the text") is True
        assert "media_url" not in cur.execute.call_args[0][0]

    def test_the_studio_sees_a_skipped_draft_so_it_can_be_restored(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7,
                                fetch_one={"id": 7, "user_id": 1, "group_id": "g1",
                                           "group_name": "AI", "content": "x", "status": "skipped",
                                           "media_url": None, "media_type": None,
                                           "created_at": None, "updated_at": None,
                                           "published_at": None})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_current_group_post_draft
            assert get_current_group_post_draft(1)["status"] == "skipped"
        assert cur.execute.call_args[0][1] == (1, "ready", "skipped", "ready")
        # An open draft outranks a skipped one whatever the ids say, so restoring an old skip can
        # never hide the post that is about to ship.
        assert "ORDER BY status = %s DESC" in cur.execute.call_args[0][0]

    def test_the_studio_never_sees_a_published_or_failed_draft(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7, fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_current_group_post_draft
            assert get_current_group_post_draft(1) is None
        assert "published" not in cur.execute.call_args[0][1]
        assert "failed" not in cur.execute.call_args[0][1]

    def test_only_the_users_own_two_statuses_are_settable_from_the_spa(self):
        from cqc_lem.utilities.db import GroupPostDraftStatus
        assert [str(s) for s in GroupPostDraftStatus.user_settable()] == ["ready", "skipped"]

    def test_post_enabled_ids_read_the_posting_flag_not_the_commenting_one(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7, fetch_all=[("g1",), ("g2",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_enabled_group_ids
            assert get_post_enabled_group_ids(1) == ["g1", "g2"]
        assert "post_enabled=1" in cur.execute.call_args[0][0]

    def test_an_unreadable_switch_answers_unknown_not_opted_out(self, fake_cursor):
        """[] means "no group takes posts", which CANCELS a reviewed draft — a failed read must
        never be able to say that, so it answers None.
        """
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error("db down")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_enabled_group_ids
            assert get_post_enabled_group_ids(1) is None


class TestDraftGroupPost:
    def test_writes_the_draft_for_that_group_without_a_browser(self):
        """Issue #932: the text is written days ahead off the CACHED profile — no Chrome session."""
        from cqc_lem.app.engagement.feed import auto_draft_group_post
        with patch(f"{_FEED}.get_open_group_post_draft", return_value=None), \
             patch(f"{_FEED}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_FEED}.generate_group_post", return_value="A useful insight.") as gen, \
             patch(f"{_FEED}.create_group_post_draft", return_value=11) as create, \
             patch(f"{_FEED}.get_current_profile") as gcp:
            result = auto_draft_group_post.run(user_id=1, group_id="g1", group_name="AI Leaders")
        assert result == "Drafted group post 11"
        gcp.assert_not_called()
        assert gen.call_args.kwargs["group_name"] == "AI Leaders"
        create.assert_called_once_with(1, "g1", "A useful insight.", group_name="AI Leaders")

    def test_an_unpublished_draft_is_never_replaced(self):
        """The waiting draft may already carry the user's edits — a second generation would bin them."""
        from cqc_lem.app.engagement.feed import auto_draft_group_post
        with patch(f"{_FEED}.get_open_group_post_draft", return_value={"id": 3}), \
             patch(f"{_FEED}.generate_group_post") as gen, \
             patch(f"{_FEED}.create_group_post_draft") as create:
            result = auto_draft_group_post.run(user_id=1, group_id="g1")
        assert "already awaiting review" in result
        gen.assert_not_called()
        create.assert_not_called()

    def test_no_cached_profile_skips_quietly(self):
        from cqc_lem.app.engagement.feed import auto_draft_group_post
        with patch(f"{_FEED}.get_open_group_post_draft", return_value=None), \
             patch(f"{_FEED}.load_profile_for_user", return_value=None), \
             patch(f"{_FEED}.generate_group_post") as gen, \
             patch(f"{_FEED}.log_warning") as warned:
            result = auto_draft_group_post.run(user_id=1, group_id="g1")
        assert result == "No cached profile to draft from"
        gen.assert_not_called()
        warned.assert_not_called()

    def test_empty_generation_stores_nothing(self):
        from cqc_lem.app.engagement.feed import auto_draft_group_post
        with patch(f"{_FEED}.get_open_group_post_draft", return_value=None), \
             patch(f"{_FEED}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_FEED}.generate_group_post", return_value="   "), \
             patch(f"{_FEED}.create_group_post_draft") as create:
            result = auto_draft_group_post.run(user_id=1, group_id="g1")
        assert result == "No group post generated"
        create.assert_not_called()


class TestSyncUserGroups:
    def test_upserts_enumerated(self):
        from cqc_lem.app.engagement.feed import auto_sync_user_groups
        with patch(f"{_FEED}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_FEED}._enumerate_joined_groups", return_value=[("1", "A"), ("2", "B")]), \
             patch(f"{_FEED}.upsert_user_group") as up, patch(f"{_FEED}.quit_gracefully"):
            result = auto_sync_user_groups.run(user_id=1)
        assert up.call_count == 2 and "Synced 2" in result


class TestCommentInGroups:
    def test_comments_each_enabled_group(self):
        from cqc_lem.app.engagement.feed import auto_comment_in_groups
        with patch(f"{_FEED}.get_enabled_group_ids", return_value=["1", "2"]), \
             patch(f"{_FEED}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_recent_engagers", return_value=set()), \
             patch(f"{_FEED}.comment_on_feed_inline", return_value=1) as cfi, patch(f"{_FEED}.quit_gracefully"):
            result = auto_comment_in_groups.run(user_id=1)
        assert cfi.call_count == 2 and "across 2 group" in result

    def test_no_enabled_groups(self):
        from cqc_lem.app.engagement.feed import auto_comment_in_groups
        with patch(f"{_FEED}.get_enabled_group_ids", return_value=[]), \
             patch(f"{_FEED}.get_current_profile") as gp:
            result = auto_comment_in_groups.run(user_id=1)
        assert "No enabled groups" in result
        gp.assert_not_called()

    def test_a_session_quit_out_from_under_the_run_ends_it_on_what_shipped(self):
        """Issue #988: a deploy recreates the containers once the drain window is spent, so a group
        walk that outlives it loses its browser. That is a routine release, not a defect — the run
        keeps the comments it already posted, logs INFO, and never raises.
        """
        from selenium.common import InvalidSessionIdException

        from cqc_lem.app.engagement.feed import auto_comment_in_groups
        driver = MagicMock()
        driver.get.side_effect = [None, InvalidSessionIdException("Unable to find session with ID: abc")]
        with patch(f"{_FEED}.get_enabled_group_ids", return_value=["1", "2", "3"]), \
             patch(f"{_FEED}.get_current_profile", return_value=(driver, MagicMock(), "e", MagicMock())), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_recent_engagers", return_value=set()), \
             patch(f"{_FEED}.comment_on_feed_inline", return_value=2) as cfi, \
             patch(f"{_FEED}.log_info") as info, \
             patch(f"{_FEED}.log_error") as err, \
             patch(f"{_FEED}.quit_gracefully") as quit_driver:
            result = auto_comment_in_groups.run(user_id=1)
        assert result == "Commented 2 time(s) before the browser session ended"
        assert cfi.call_count == 1  # the second group is unreachable on a dead session
        assert info.called and not err.called
        quit_driver.assert_called_once_with(driver)

    def test_the_walk_hands_its_deadline_to_the_feed_engine(self):
        """Issue #1198: one slow group must not be able to spend the whole task budget.

        So the deadline goes INTO `comment_on_feed_inline`, not just around it.
        """
        from cqc_lem.app.engagement.feed import auto_comment_in_groups
        with patch(f"{_FEED}.get_enabled_group_ids", return_value=["1"]), \
             patch(f"{_FEED}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_recent_engagers", return_value=set()), \
             patch(f"{_FEED}._group_walk_deadline", return_value=4000.0), \
             patch(f"{_FEED}.time.time", return_value=1000.0), \
             patch(f"{_FEED}.comment_on_feed_inline", return_value=1) as cfi, \
             patch(f"{_FEED}.quit_gracefully"):
            auto_comment_in_groups.run(user_id=1)
        assert cfi.call_args.kwargs["deadline_ts"] == 4000.0

    def test_out_of_time_stops_between_groups_and_keeps_what_shipped(self):
        """Issue #1198: reaching the soft time limit kills the run mid-comment.

        So the walk stops itself first — remaining groups are skipped, the posted comments stand,
        and nothing raises.
        """
        from cqc_lem.app.engagement.feed import auto_comment_in_groups
        driver = MagicMock()
        with patch(f"{_FEED}.get_enabled_group_ids", return_value=["1", "2", "3"]), \
             patch(f"{_FEED}.get_current_profile", return_value=(driver, MagicMock(), "e", MagicMock())), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_recent_engagers", return_value=set()), \
             patch(f"{_FEED}._group_walk_deadline", return_value=4000.0), \
             patch(f"{_FEED}.time.time", side_effect=_clock(1000.0, 1000.0, 9999.0)), \
             patch(f"{_FEED}.comment_on_feed_inline", return_value=2) as cfi, \
             patch(f"{_FEED}.log_warning") as warned, \
             patch(f"{_FEED}.quit_gracefully") as quit_driver:
            result = auto_comment_in_groups.run(user_id=1)
        assert result == "Commented 2 time(s) across 1 group(s) before running out of time"
        assert cfi.call_count == 1  # groups 2 and 3 were never opened
        assert warned.called
        quit_driver.assert_called_once_with(driver)

    def test_the_soft_time_limit_itself_is_absorbed_not_crashed(self):
        """Issue #1198: the backstop for a run whose reserve was not enough.

        SoftTimeLimitExceeded derives from Exception, so the loop's session-lost handler re-raises
        it — it must land on the run's own handler, keep the comments already posted, and quit
        Chrome.
        """
        from cqc_lem.app.engagement.feed import auto_comment_in_groups
        driver = MagicMock()
        with patch(f"{_FEED}.get_enabled_group_ids", return_value=["1", "2"]), \
             patch(f"{_FEED}.get_current_profile", return_value=(driver, MagicMock(), "e", MagicMock())), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_recent_engagers", return_value=set()), \
             patch(f"{_FEED}._group_walk_deadline", return_value=None), \
             patch(f"{_FEED}.comment_on_feed_inline", side_effect=SoftTimeLimitExceeded()), \
             patch(f"{_FEED}.log_warning") as warned, \
             patch(f"{_FEED}.log_error") as err, \
             patch(f"{_FEED}.quit_gracefully") as quit_driver:
            result = auto_comment_in_groups.run(user_id=1)
        assert result == "Commented 0 time(s) before the task time limit"
        assert warned.called and not err.called
        quit_driver.assert_called_once_with(driver)

    def test_a_real_failure_mid_run_still_raises(self):
        """Only a LOST SESSION is absorbed — anything else stays a crash, and a defect."""
        from cqc_lem.app.engagement.feed import auto_comment_in_groups
        driver = MagicMock()
        with patch(f"{_FEED}.get_enabled_group_ids", return_value=["1"]), \
             patch(f"{_FEED}.get_current_profile", return_value=(driver, MagicMock(), "e", MagicMock())), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_recent_engagers", return_value=set()), \
             patch(f"{_FEED}.comment_on_feed_inline", side_effect=RuntimeError("boom")), \
             patch(f"{_FEED}.quit_gracefully"):
            with pytest.raises(RuntimeError):
                auto_comment_in_groups.run(user_id=1)


class TestGroupWalkDeadline:
    """`_group_walk_deadline` — the budget arithmetic behind the #1198 stop."""

    def _task(self, timelimit):
        task = MagicMock()
        task.request.timelimit = timelimit
        return task

    def test_reads_the_soft_limit_off_the_request_first(self):
        """Celery orders the header (hard, soft) — reading index 0 would budget off the wrong one."""
        from cqc_lem.app.engagement.feed import GROUP_WALK_RESERVE_SECONDS, _group_walk_deadline
        assert _group_walk_deadline(self._task((5400, 4800)), 1000.0) == 1000.0 + 4800 - GROUP_WALK_RESERVE_SECONDS

    def test_falls_back_to_the_app_config(self):
        from cqc_lem.app.engagement.feed import (
            GROUP_WALK_RESERVE_SECONDS,
            _group_walk_deadline,
            shared_task,
        )
        soft = shared_task.conf.task_soft_time_limit
        assert soft, "the app must configure a soft time limit for the walk to budget against"
        assert _group_walk_deadline(self._task(None), 1000.0) == 1000.0 + soft - GROUP_WALK_RESERVE_SECONDS

    def test_a_limit_tighter_than_the_reserve_still_leaves_a_budget(self):
        """Otherwise the first deadline check would refuse group one and nothing would be commented.

        A stricter limit must shorten the walk, not cancel it.
        """
        from cqc_lem.app.engagement.feed import GROUP_WALK_MIN_BUDGET_SECONDS, _group_walk_deadline
        assert _group_walk_deadline(self._task((120, 60)), 1000.0) == 1000.0 + GROUP_WALK_MIN_BUDGET_SECONDS

    def test_no_limit_anywhere_leaves_the_walk_unbounded(self):
        from cqc_lem.app.engagement.feed import _group_walk_deadline
        unbounded_app = MagicMock()
        unbounded_app.conf.task_soft_time_limit = None
        with patch(f"{_FEED}.shared_task", unbounded_app):
            assert _group_walk_deadline(self._task(None), 1000.0) is None


_READY_DRAFT = {"id": 11, "user_id": 1, "group_id": "123", "group_name": "AI Leaders",
                "content": "A useful insight.", "status": "ready"}


class TestPostToGroup:
    def _driver_patches(self):
        return patch(f"{_FEED}.get_current_profile",
                     return_value=(MagicMock(), MagicMock(), "e", MagicMock()))

    def _driver_patches_with(self, driver):
        return patch(f"{_FEED}.get_current_profile",
                     return_value=(driver, MagicMock(), "e", MagicMock()))

    def test_publishes_the_reviewed_draft_and_stamps_rotation(self):
        """Issue #932: the published text is the draft the user could read and revise — nothing is
        generated here. Only a post that actually shipped moves the rotation on.
        """
        from cqc_lem.app.engagement.feed import auto_post_to_group
        with self._driver_patches(), \
             patch(f"{_FEED}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_FEED}.generate_group_post") as gen, \
             patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}.find_first", return_value=MagicMock()) as box, \
             patch(f"{_FEED}.record_group_post") as rec, \
             patch(f"{_FEED}.update_group_post_draft") as upd, \
             patch(f"{_FEED}.record_group_post_run") as run, patch(f"{_FEED}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders",
                                            draft_id=11)
        assert result == "Posted to group"
        gen.assert_not_called()
        box.return_value.send_keys.assert_called_once_with("A useful insight.")
        rec.assert_called_once_with(1, "123")
        assert str(upd.call_args.kwargs["status"]) == "published"
        # record_group_post stamps both columns itself — the success path never double-stamps.
        run.assert_not_called()

    def test_media_goes_in_before_the_text(self):
        """Media goes in first because the uploader takes over the composer while it transcodes.

        Text typed first is what the overlay discards (issue #1224). The published result names what
        shipped with it.
        """
        from cqc_lem.app.engagement.feed import auto_post_to_group
        draft = {**_READY_DRAFT, "media_url": "http://x/api/assets?file_name=i.png",
                 "media_type": "image"}
        order = []
        media_input = MagicMock()
        media_input.send_keys.side_effect = lambda *_: order.append("media")
        box = MagicMock()
        box.send_keys.side_effect = lambda *_: order.append("text")
        with self._driver_patches(), \
             patch(f"{_FEED}.get_group_post_draft", return_value=draft), \
             patch(f"{_FEED}.post_image_abs_path", return_value="/assets/i.png"), \
             patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}.find_first", side_effect=[media_input, box]), \
             patch(f"{_FEED}.record_group_post"), patch(f"{_FEED}.update_group_post_draft"), \
             patch(f"{_FEED}.record_group_post_run"), patch(f"{_FEED}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", draft_id=11)
        assert result == "Posted to group with image"
        assert order == ["media", "text"]
        media_input.send_keys.assert_called_once_with("/assets/i.png")

    @pytest.mark.parametrize("failure", ["gone_from_disk", "no_control"])
    def test_media_that_will_not_attach_still_ships_the_post(self, failure):
        """Fail OPEN, like the article cover — text alone is worth more than no post at all.

        The warning is what makes the drift visible (it escalates on repeat).
        """
        from cqc_lem.app.engagement.feed import auto_post_to_group
        draft = {**_READY_DRAFT, "media_url": "http://x/api/assets?file_name=i.png",
                 "media_type": "image"}
        box = MagicMock()
        # gone_from_disk never reaches a lookup; no_control misses the input twice (before and
        # after the media button) and then resolves the editor.
        find_results = [box] if failure == "gone_from_disk" else [None, None, box]
        with self._driver_patches(), \
             patch(f"{_FEED}.get_group_post_draft", return_value=draft), \
             patch(f"{_FEED}.post_image_abs_path",
                   return_value=None if failure == "gone_from_disk" else "/assets/i.png"), \
             patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}.find_first", side_effect=find_results), \
             patch(f"{_FEED}.log_warning") as warned, \
             patch(f"{_FEED}.record_group_post") as rec, \
             patch(f"{_FEED}.update_group_post_draft") as upd, \
             patch(f"{_FEED}.record_group_post_run") as run, patch(f"{_FEED}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", draft_id=11)
        assert result == "Posted to group"
        box.send_keys.assert_called_once_with("A useful insight.")
        rec.assert_called_once_with(1, "123")
        run.assert_not_called()
        assert str(upd.call_args.kwargs["status"]) == "published"
        warned.assert_called_once()

    def test_a_text_only_draft_never_opens_the_media_chain(self):
        from cqc_lem.app.engagement.feed import auto_post_to_group
        with self._driver_patches(), \
             patch(f"{_FEED}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_FEED}.post_image_abs_path") as resolved, \
             patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}.find_first", return_value=MagicMock()), \
             patch(f"{_FEED}.record_group_post"), patch(f"{_FEED}.update_group_post_draft"), \
             patch(f"{_FEED}.record_group_post_run"), patch(f"{_FEED}.quit_gracefully"):
            assert auto_post_to_group.run(user_id=1, group_id="123", draft_id=11) == "Posted to group"
        resolved.assert_not_called()

    def test_the_composers_own_upload_control_is_tried_before_any_other_on_the_page(self):
        """#1012's rule, applied to an upload.

        A group page carries other file inputs — the messaging overlay's declares an image `accept`
        too — and writing the draft's file into one of those uploads the image somewhere the user
        never asked for while the run still reports the media as attached.
        """
        from cqc_lem.app.engagement.feed import (
            _GROUP_MEDIA_CONFIRM_LOCATORS,
            _GROUP_MEDIA_INPUT_LOCATORS,
        )
        for chain in (_GROUP_MEDIA_INPUT_LOCATORS, _GROUP_MEDIA_CONFIRM_LOCATORS):
            scoped = [i for i, (_, value) in enumerate(chain) if "role='dialog'" in value]
            unscoped = [i for i, (_, value) in enumerate(chain) if "role='dialog'" not in value]
            assert scoped, chain
            # Page-wide stays as the last resort (the share box is inline on some variants), but it
            # can never be reached while a composer-scoped control resolves.
            assert max(scoped) < min(unscoped)
            # …and the last resort is not a long shot: the messaging overlay rides EVERY LinkedIn
            # page, so an unscoped locator that could match inside it is the control we would land
            # on the moment the composer's own drifts.
            for i in unscoped:
                assert "msg-overlay" in chain[i][1], chain[i]

    def test_a_media_overlay_we_could_not_close_never_blames_the_group(self):
        """Fail-open has to hold for what the media step LEAVES BEHIND, not just for the upload.

        The uploader's overlay is OURS. An editor or Post button missing after we opened it says the
        overlay is still up — so stamping the draft FAILED and rotating past the group (what
        `_unpostable` does) would cost the week AND blame a healthy group. The draft stays `ready`
        for the next weekly slot.
        """
        from cqc_lem.app.engagement.feed import auto_post_to_group
        draft = {**_READY_DRAFT, "media_url": "http://x/api/assets?file_name=i.png",
                 "media_type": "image"}
        with self._driver_patches(), \
             patch(f"{_FEED}.get_group_post_draft", return_value=draft), \
             patch(f"{_FEED}.post_image_abs_path", return_value="/assets/i.png"), \
             patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}.find_first", side_effect=[MagicMock(), None]), \
             patch(f"{_FEED}.log_warning") as warned, \
             patch(f"{_FEED}.record_group_post") as rec, \
             patch(f"{_FEED}.update_group_post_draft") as upd, \
             patch(f"{_FEED}.record_group_post_run") as run, patch(f"{_FEED}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", draft_id=11)
        assert result == "Group post editor not found"
        upd.assert_not_called()   # the draft is still the user's to publish next slot
        run.assert_not_called()   # the rotation does not move past a group that opened its composer
        rec.assert_not_called()
        assert warned.called

    def test_a_composer_we_never_touched_still_reports_the_group_as_unpostable(self):
        """The reverse: with no media in the draft nothing of ours is on screen.

        A missing editor there is the group refusing member posts, which must still retire the draft
        and rotate past it (issue #858) — the media fix must not swallow that.
        """
        from cqc_lem.app.engagement.feed import auto_post_to_group
        with self._driver_patches(), \
             patch(f"{_FEED}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}.find_first", return_value=None), \
             patch(f"{_FEED}.record_group_post"), \
             patch(f"{_FEED}.update_group_post_draft") as upd, \
             patch(f"{_FEED}.record_group_post_run") as run, patch(f"{_FEED}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", draft_id=11)
        assert result == "Group post editor not found"
        assert str(upd.call_args.kwargs["status"]) == "failed"
        run.assert_called_once_with(1, "123")

    @pytest.mark.parametrize("draft", [
        None,
        {**_READY_DRAFT, "status": "skipped"},
        {**_READY_DRAFT, "user_id": 2},
    ])
    def test_never_publishes_without_a_reviewed_draft(self, draft):
        """A draft that was skipped, belongs to someone else, or is simply gone means NO post —
        falling back to a fresh generation would ship the un-previewed post #932 removed.
        """
        from cqc_lem.app.engagement.feed import auto_post_to_group
        with patch(f"{_FEED}.get_group_post_draft", return_value=draft), \
             patch(f"{_FEED}.get_current_profile") as gcp, \
             patch(f"{_FEED}.generate_group_post") as gen, \
             patch(f"{_FEED}.record_group_post") as rec, \
             patch(f"{_FEED}.record_group_post_run") as run:
            result = auto_post_to_group.run(user_id=1, group_id="123", draft_id=11)
        assert result == "No group post draft to publish"
        gcp.assert_not_called()
        gen.assert_not_called()
        rec.assert_not_called()
        run.assert_not_called()

    def test_a_dispatch_carrying_no_draft_id_publishes_nothing(self):
        """The pre-#932 call shape (no draft) must not resurrect the un-previewed publish path."""
        from cqc_lem.app.engagement.feed import auto_post_to_group
        with patch(f"{_FEED}.get_group_post_draft") as get_draft, \
             patch(f"{_FEED}.get_current_profile") as gcp:
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
        draft was written for THAT group, so it dies with the group's turn (#932).
        """
        from cqc_lem.app.engagement.feed import auto_post_to_group
        clicks = {"share_box": [None], "editor": [MagicMock()],
                  "post_button": [MagicMock(), None]}[miss]
        with self._driver_patches(), \
             patch(f"{_FEED}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_FEED}.click_first", side_effect=clicks), \
             patch(f"{_FEED}.find_first", return_value=None if miss == "editor" else MagicMock()), \
             patch(f"{_FEED}.record_group_post") as rec, \
             patch(f"{_FEED}.update_group_post_draft") as upd, \
             patch(f"{_FEED}.record_group_post_run") as run, patch(f"{_FEED}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders",
                                            draft_id=11)
        assert result == expected
        run.assert_called_once_with(1, "123")
        rec.assert_not_called()
        assert str(upd.call_args.kwargs["status"]) == "failed"

    def test_share_box_drift_warns_when_page_text_still_carries_the_signal(self):
        """Issue #1107: share-box signal visible but the locator chain cannot resolve it.

        When the page plainly renders "Start a post" but the control lookup misses, we are looking
        at selector drift rather than an admin-only group.
        """
        from cqc_lem.app.engagement.feed import auto_post_to_group
        driver = MagicMock()
        body = MagicMock()
        body.text = "Some group header\nStart a post\nRecommended for you"
        driver.find_element.return_value = body
        with self._driver_patches_with(driver), \
             patch(f"{_FEED}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_FEED}.click_first", return_value=None), \
             patch(f"{_FEED}.record_group_post") as rec, \
             patch(f"{_FEED}.update_group_post_draft") as upd, \
             patch(f"{_FEED}.record_group_post_run") as run, \
             patch(f"{_FEED}.log_warning") as warn, \
             patch(f"{_FEED}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders",
                                            draft_id=11)
        assert result == "Group share box control drifted"
        run.assert_called_once_with(1, "123")
        rec.assert_not_called()
        assert str(upd.call_args.kwargs["status"]) == "failed"
        warn.assert_called_once()
        assert "drifted" in warn.call_args[0][0].lower()

    def test_share_box_absent_without_signal_is_not_drift(self):
        """If the page text has no share-box signal at all, the group is simply unpostable."""
        from cqc_lem.app.engagement.feed import auto_post_to_group
        driver = MagicMock()
        body = MagicMock()
        body.text = "Some group header\nRecommended for you"
        driver.find_element.return_value = body
        with self._driver_patches_with(driver), \
             patch(f"{_FEED}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_FEED}.click_first", return_value=None), \
             patch(f"{_FEED}.record_group_post") as rec, \
             patch(f"{_FEED}.update_group_post_draft") as upd, \
             patch(f"{_FEED}.record_group_post_run") as run, \
             patch(f"{_FEED}.log_warning") as warn, \
             patch(f"{_FEED}.quit_gracefully"):
            result = auto_post_to_group.run(user_id=1, group_id="123", group_name="AI Leaders",
                                            draft_id=11)
        assert result == "Group share box not found"
        run.assert_called_once_with(1, "123")
        rec.assert_not_called()
        assert str(upd.call_args.kwargs["status"]) == "failed"
        warn.assert_not_called()

    def test_failure_before_the_group_is_reached_does_not_advance_the_rotation(self):
        """A dead session is transient and not the group's fault — it keeps its turn, and the draft
        stays open so next week's run publishes the text the user already approved.
        """
        from cqc_lem.app.engagement.feed import auto_post_to_group
        with patch(f"{_FEED}.get_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_FEED}.get_current_profile", side_effect=Exception("no session")), \
             patch(f"{_FEED}.record_group_post") as rec, \
             patch(f"{_FEED}.update_group_post_draft") as upd, \
             patch(f"{_FEED}.record_group_post_run") as run:
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
             patch("cqc_lem.app.engagement.feed.auto_draft_group_post") as t:
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
             patch("cqc_lem.app.engagement.feed.auto_draft_group_post") as t:
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
             patch("cqc_lem.app.engagement.feed.auto_post_to_group") as t:
            result = auto_group_posts()
        t.apply_async.assert_called_once_with(
            kwargs={'user_id': 1, 'group_id': "g9", 'group_name': "Sales", 'draft_id': 11})
        assert "1/2" in result

    def test_group_posts_skip_users_without_a_session(self):
        from cqc_lem.app.run_scheduler import auto_group_posts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=False), \
             patch(f"{_DB}.get_open_group_post_draft") as draft, \
             patch("cqc_lem.app.engagement.feed.auto_post_to_group") as t:
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
             patch("cqc_lem.app.engagement.feed.auto_post_to_group") as t:
            result = auto_group_posts()
        t.apply_async.assert_not_called()
        assert str(upd.call_args.kwargs["status"]) == "skipped"
        assert "0/1" in result

    def test_unreadable_post_switches_hold_the_draft_rather_than_cancelling_it(self):
        """A DB hiccup must not be able to cancel a post the user read and approved — the draft
        stays open and ships at the next slot.
        """
        from cqc_lem.app.run_scheduler import auto_group_posts
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_DB}.get_open_group_post_draft", return_value=dict(_READY_DRAFT)), \
             patch(f"{_DB}.get_post_enabled_group_ids", return_value=None), \
             patch(f"{_DB}.update_group_post_draft") as upd, \
             patch("cqc_lem.app.engagement.feed.auto_post_to_group") as t:
            result = auto_group_posts()
        t.apply_async.assert_not_called()
        upd.assert_not_called()
        assert "0/1" in result


    def test_group_engagement_dispatches_connected(self):
        from cqc_lem.app.run_scheduler import auto_group_engagement
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.has_linkedin_session", side_effect=lambda u: u == 1), \
             patch(f"{_RS}._stagger_due", return_value=True), \
             patch("cqc_lem.app.engagement.feed.auto_comment_in_groups") as t:
            result = auto_group_engagement()
        t.apply_async.assert_called_once()
        assert "1/2" in result

    def test_group_engagement_waits_for_each_users_slot(self):
        """Issue #554: the beat ticks every 15 min, so a connected user still only runs when
        their staggered slot on the single se_content lane comes up.
        """
        from cqc_lem.app.run_scheduler import STAGGER_GROUP_ENGAGEMENT, auto_group_engagement
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_RS}._stagger_due", side_effect=lambda u, *_: u == 2) as due, \
             patch("cqc_lem.app.engagement.feed.auto_comment_in_groups") as t:
            result = auto_group_engagement()
        t.apply_async.assert_called_once_with(kwargs={'user_id': 2})
        assert due.call_args[0][1] is STAGGER_GROUP_ENGAGEMENT
        assert "1/2" in result


class TestGroupPostBeats:
    def test_the_draft_beat_lands_before_the_publish_beat(self):
        """The review window IS the feature (#932): a draft written after the publish slot would
        only ever be read once the post it describes had already gone out.
        """
        from cqc_lem.app.my_celery import app
        drafts = app.conf.beat_schedule["group-post-drafts"]
        posts = app.conf.beat_schedule["group-posts"]
        assert drafts["task"] == "cqc_lem.app.run_scheduler.auto_group_post_drafts"
        assert posts["task"] == "cqc_lem.app.run_scheduler.auto_group_posts"
        assert min(drafts["schedule"].day_of_week) < min(posts["schedule"].day_of_week)
