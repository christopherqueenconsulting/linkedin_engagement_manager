"""The engagement-mode decision, its backstop, its storage and its API (issue #2367).

The lane-by-lane "no browser for a suggest account" proof lives in
`tests/unit/app/test_engagement_mode_lanes.py`; this file covers the pieces those lanes stand on:
the fail-closed resolver, the `get_docker_driver` backstop, the suggestion store, the creation
paths that make a new account `suggest`, the dispatchers, the API-driven self-comments that store
a draft instead of publishing, and the two read-only API surfaces.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.utilities import engagement_mode as em
from cqc_lem.utilities.db import EngagementMode, EngagementSuggestionKind

pytestmark = pytest.mark.unit

_MODE = "cqc_lem.utilities.engagement_mode.get_user_engagement_mode"
_CONN = "cqc_lem.platform.db.connection.get_db_connection"


class TestResolverFailsClosed:
    def test_a_stored_automate_is_the_only_way_to_automate(self):
        with patch(_MODE, return_value="automate"):
            assert em.resolve_engagement_mode(1) is EngagementMode.AUTOMATE
            assert em.browser_automation_allowed(1) is True
            assert em.is_suggest_only(1) is False

    @pytest.mark.parametrize("reading", [
        {"return_value": "suggest"},
        {"return_value": None},
        {"return_value": ""},
        {"return_value": "Automate"},
        {"side_effect": TypeError("unset DB port")},
        {"side_effect": RuntimeError("anything")},
    ], ids=["suggest", "none", "empty", "wrong-case", "typeerror", "runtimeerror"])
    def test_every_other_reading_is_suggest(self, reading):
        with patch(_MODE, **reading):
            assert em.resolve_engagement_mode(1) is EngagementMode.SUGGEST
            assert em.browser_automation_allowed(1) is False
            assert em.is_suggest_only(1) is True

    def test_no_account_is_suggest_and_is_never_read(self):
        with patch(_MODE) as read:
            assert em.resolve_engagement_mode(None) is EngagementMode.SUGGEST
        read.assert_not_called()

    def test_an_unreadable_mode_warns_once_where_it_is_detected(self):
        with patch(_MODE, side_effect=TypeError("x")), \
             patch("cqc_lem.utilities.engagement_mode.log_warning") as warn:
            em.resolve_engagement_mode(4)
        warn.assert_called_once()


class TestTheLaneSkipIsAnExpectedNoOp:
    def test_a_skip_logs_debug_never_a_warning(self):
        with patch(_MODE, return_value="suggest"), \
             patch("cqc_lem.utilities.engagement_mode.log_debug") as debug, \
             patch("cqc_lem.utilities.engagement_mode.log_warning") as warn:
            assert em.skip_browser_lane(3, "automate_commenting") is True
        debug.assert_called_once()
        assert debug.call_args.kwargs == {"user_id": 3, "task_name": "automate_commenting"}
        warn.assert_not_called()

    def test_an_automate_account_is_not_skipped_and_logs_nothing(self):
        with patch(_MODE, return_value="automate"), \
             patch("cqc_lem.utilities.engagement_mode.log_debug") as debug:
            assert em.skip_browser_lane(3, "automate_commenting") is False
        debug.assert_not_called()

    def test_require_raises_for_suggest_and_passes_for_automate(self):
        with patch(_MODE, return_value="suggest"), pytest.raises(em.SuggestOnlyEngagement):
            em.require_browser_automation(3, "Post Comment")
        with patch(_MODE, return_value="automate"):
            assert em.require_browser_automation(3, "Post Comment") is None


class _Sentinel(Exception):
    pass


class TestTheDriverBackstop:
    """`get_docker_driver` refuses a suggest account even when a lane forgot its own check."""

    def test_a_suggest_account_is_refused_before_the_grid_is_touched(self):
        from cqc_lem.utilities import selenium_util

        with patch(_MODE, return_value="suggest"), \
             patch.object(selenium_util, "_wait_for_selenium_ready") as ready, \
             patch("selenium.webdriver.Remote") as remote, \
             pytest.raises(em.SuggestOnlyEngagement):
            selenium_util.get_docker_driver(session_name="Post Comment", user_id=9)
        ready.assert_not_called()
        remote.assert_not_called()

    def test_an_unreadable_mode_is_refused_too(self):
        from cqc_lem.utilities import selenium_util

        with patch(_MODE, return_value=None), \
             patch("selenium.webdriver.Remote") as remote, \
             pytest.raises(em.SuggestOnlyEngagement):
            selenium_util.get_driver_wait_pair(session_name="Post Comment", user_id=9)
        remote.assert_not_called()

    def test_an_automate_account_goes_on_to_the_grid(self):
        from cqc_lem.utilities import selenium_util

        with patch(_MODE, return_value="automate"), \
             patch.object(selenium_util, "_wait_for_selenium_ready", side_effect=_Sentinel()), \
             patch.object(selenium_util, "DEVICE_FARM_PROJECT_ARN", None), \
             pytest.raises(_Sentinel):
            selenium_util.get_docker_driver(session_name="Post Comment", user_id=9)

    def test_a_session_with_no_account_is_not_an_engagement_lane(self):
        """Tutorial capture and the load test run with no user — they are not refused here."""
        from cqc_lem.utilities import selenium_util

        with patch(_MODE) as read, \
             patch.object(selenium_util, "_wait_for_selenium_ready", side_effect=_Sentinel()), \
             patch.object(selenium_util, "DEVICE_FARM_PROJECT_ARN", None), \
             pytest.raises(_Sentinel):
            selenium_util.get_docker_driver(session_name="TutorialCapture")
        read.assert_not_called()


class TestSaveSuggestion:
    def test_stores_through_the_repository(self):
        with patch("cqc_lem.utilities.engagement_mode.insert_engagement_suggestion",
                   return_value=True) as store:
            assert em.save_suggestion(1, EngagementSuggestionKind.DM, "send_private_dm", "Hi",
                                      "k", target_url="u") is True
        store.assert_called_once_with(1, EngagementSuggestionKind.DM, "send_private_dm", "Hi", "k",
                                      target_url="u")

    @pytest.mark.parametrize("body", ["", "   ", None])
    def test_an_empty_draft_is_not_stored(self, body):
        with patch("cqc_lem.utilities.engagement_mode.insert_engagement_suggestion") as store:
            assert em.save_suggestion(1, EngagementSuggestionKind.COMMENT, "s", body, "k") is False
        store.assert_not_called()

    def test_dm_key_is_per_recipient_and_message(self):
        a = em.dm_suggestion_key("https://www.linkedin.com/in/a/", "Hi")
        assert a == em.dm_suggestion_key("https://www.linkedin.com/in/a/", " Hi ")
        assert a != em.dm_suggestion_key("https://www.linkedin.com/in/a/", "Hello")
        assert a != em.dm_suggestion_key("https://www.linkedin.com/in/b/", "Hi")

    @pytest.mark.parametrize("stored,expected", [(True, True), (False, False), (None, True)])
    def test_suggestion_exists_treats_an_unreadable_answer_as_present(self, stored, expected):
        with patch("cqc_lem.utilities.engagement_mode.has_engagement_suggestion",
                   return_value=stored):
            assert em.suggestion_exists(1, "seed:3") is expected


class TestRepository:
    def test_get_user_engagement_mode_reads_the_column(self, fake_cursor):
        from cqc_lem.utilities.db import get_user_engagement_mode

        conn, cur = fake_cursor(fetch_one=("automate",))
        with patch(_CONN, return_value=conn):
            assert get_user_engagement_mode(4) == "automate"
        sql, params = cur.execute.call_args.args
        assert "engagement_mode" in sql and params == (4,)

    @pytest.mark.parametrize("row", [None, (None,)])
    def test_get_user_engagement_mode_missing_is_none(self, fake_cursor, row):
        from cqc_lem.utilities.db import get_user_engagement_mode

        conn, _cur = fake_cursor(fetch_one=row)
        with patch(_CONN, return_value=conn):
            assert get_user_engagement_mode(4) is None

    def test_get_user_engagement_mode_db_fault_is_none(self, fake_cursor):
        from cqc_lem.utilities.db import get_user_engagement_mode

        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(_CONN, return_value=conn):
            assert get_user_engagement_mode(4) is None

    def test_insert_is_idempotent_on_the_dedup_key(self, fake_cursor):
        from cqc_lem.utilities.db import insert_engagement_suggestion

        conn, cur = fake_cursor(rowcount=1)
        with patch(_CONN, return_value=conn):
            assert insert_engagement_suggestion(
                1, EngagementSuggestionKind.COMMENT, "auto_seed_comment_on_post", "body",
                "seed:3", target_url="https://x/" + "a" * 2000) is True
        sql, params = cur.execute.call_args.args
        assert sql.startswith("INSERT IGNORE INTO engagement_suggestions")
        assert params[1] == "comment"
        assert len(params[3]) == 1024  # over-long target clipped, never a DB error
        assert params[5] == "seed:3"

        conn, cur = fake_cursor(rowcount=0)
        with patch(_CONN, return_value=conn):
            assert insert_engagement_suggestion(1, EngagementSuggestionKind.DM, "s", "b", "k") is False

    def test_insert_db_fault_is_none(self, fake_cursor):
        from cqc_lem.utilities.db import insert_engagement_suggestion

        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(_CONN, return_value=conn):
            assert insert_engagement_suggestion(1, EngagementSuggestionKind.DM, "s", "b", "k") is None

    @pytest.mark.parametrize("row,expected", [((1,), True), (None, False)])
    def test_has_suggestion(self, fake_cursor, row, expected):
        from cqc_lem.utilities.db import has_engagement_suggestion

        conn, _cur = fake_cursor(fetch_one=row)
        with patch(_CONN, return_value=conn):
            assert has_engagement_suggestion(1, "seed:3") is expected

    def test_has_suggestion_db_fault_is_none(self, fake_cursor):
        from cqc_lem.utilities.db import has_engagement_suggestion

        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(_CONN, return_value=conn):
            assert has_engagement_suggestion(1, "seed:3") is None

    def test_list_reads_newest_first(self, fake_cursor):
        from cqc_lem.utilities.db import get_engagement_suggestions

        when = datetime(2026, 10, 10, 12, 0)
        conn, cur = fake_cursor(fetch_all=[(2, "dm", "send_private_dm", "u", "Hi", when)])
        with patch(_CONN, return_value=conn):
            rows = get_engagement_suggestions(1, limit=10)
        assert rows == [{"id": 2, "kind": "dm", "source": "send_private_dm", "target_url": "u",
                         "body": "Hi", "created_at": when}]
        sql, params = cur.execute.call_args.args
        assert "ORDER BY created_at DESC" in sql and params == (1, 10)

    def test_list_db_fault_is_none_not_empty(self, fake_cursor):
        from cqc_lem.utilities.db import get_engagement_suggestions

        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(_CONN, return_value=conn):
            assert get_engagement_suggestions(1) is None


class TestNewAccountsAreSuggest:
    """Every account-creation path writes `suggest`.

    The column DEFAULT stays `automate` for the rows that already exist.
    """

    def test_add_user_by_email(self, mock_database_connection):
        from cqc_lem.utilities.db import add_user_by_email

        cur = mock_database_connection["cursor"]
        cur.lastrowid = 11
        with patch("cqc_lem.utilities.stripe_util.create_stripe_customer", return_value=None):
            assert add_user_by_email("new@example.com") == 11
        sql, params = cur.execute.call_args_list[0].args
        assert "engagement_mode" in sql
        assert params[-1] == "suggest"

    def test_add_user(self, fake_cursor):
        from cqc_lem.utilities.db import add_user

        conn, cur = fake_cursor(lastrowid=12)
        with patch(_CONN, return_value=conn), \
             patch("cqc_lem.platform.db.repositories.users.encrypt_secret", return_value="sealed"):
            add_user("new@example.com", "pw")
        sql, params = cur.execute.call_args_list[0].args
        assert "engagement_mode" in sql
        assert params == ("new@example.com", "suggest")

    def test_oauth_upsert_sets_suggest_on_insert_only(self, mock_database_connection):
        from cqc_lem.utilities.db import add_user_with_access_token

        cur = mock_database_connection["cursor"]
        cur.lastrowid = 13
        with patch("cqc_lem.platform.db.repositories.users.encrypt_secret", return_value="sealed"):
            add_user_with_access_token("new@example.com", "sub", "tok", "3600")
        sql, params = cur.execute.call_args_list[0].args
        insert_half, update_half = sql.split("ON DUPLICATE KEY UPDATE")
        assert "engagement_mode" in insert_half
        # An existing account re-connecting keeps the mode it has.
        assert "engagement_mode" not in update_half
        assert params[-1] == "suggest"


class TestContentGenerationNeverScrapesForASuggestAccount:
    def test_resolve_user_profile_uses_the_cached_profile(self):
        from cqc_lem.app import run_content_plan

        cached = MagicMock()
        with patch(_MODE, return_value="suggest"), \
             patch.object(run_content_plan, "get_driver_wait_pair") as pair, \
             patch.object(run_content_plan, "get_user_password_pair_by_id") as creds, \
             patch.object(run_content_plan, "load_profile_for_user", return_value=cached):
            assert run_content_plan._resolve_user_profile(5) is cached
        pair.assert_not_called()
        creds.assert_not_called()

    def test_resolve_user_profile_still_scrapes_for_automate(self):
        from cqc_lem.app import run_content_plan

        live = MagicMock()
        with patch(_MODE, return_value="automate"), \
             patch.object(run_content_plan, "get_driver_wait_pair",
                          return_value=(MagicMock(), MagicMock())) as pair, \
             patch.object(run_content_plan, "get_user_password_pair_by_id", return_value=("e", "p")), \
             patch.object(run_content_plan, "get_my_profile", return_value=live), \
             patch.object(run_content_plan, "quit_gracefully"):
            assert run_content_plan._resolve_user_profile(5) is live
        pair.assert_called_once()

    def test_carousel_generation_uses_the_cached_profile_quietly(self):
        from cqc_lem.utilities.ai import ai_helper

        with patch(_MODE, return_value="suggest"), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair") as pair, \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user",
                   side_effect=_Sentinel()) as cached, \
             patch.object(ai_helper, "log_warning") as warn:
            # The cached-profile read is the first thing after the skipped scrape; stopping there
            # proves the order without driving the whole carousel generator.
            with pytest.raises(Exception):
                ai_helper.generate_carousel_content(5, "awareness", {})
        pair.assert_not_called()
        cached.assert_called_once_with(5)
        assert not any("Could not load user profile" in str(c.args[0]) for c in warn.call_args_list)


_SCHED = "cqc_lem.app.run_scheduler"


class TestDispatchers:
    def test_browser_lane_users_drops_suggest_accounts(self):
        from cqc_lem.app.run_scheduler import _browser_lane_users

        with patch(_MODE, side_effect=lambda uid: {1: "automate", 2: "suggest", 3: None}.get(uid)):
            assert _browser_lane_users([1, 2, 3], "auto_daily_engagement") == [1]

    def test_daily_engagement_is_not_queued_for_a_suggest_account(self):
        from cqc_lem.app.run_scheduler import auto_daily_engagement

        with patch(_MODE, side_effect=lambda uid: "automate" if uid == 1 else "suggest"), \
             patch(f"{_SCHED}._skip_if_throttled", return_value=False), \
             patch(f"{_SCHED}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_SCHED}.has_linkedin_session", return_value=True), \
             patch(f"{_SCHED}._stagger_due", return_value=True), \
             patch(f"{_SCHED}.dispatch_jitter_seconds", return_value=0), \
             patch(f"{_SCHED}.dispatch_golden_hour_engagement") as dispatch:
            auto_daily_engagement()
        assert [c.kwargs["kwargs"]["user_id"] for c in dispatch.apply_async.call_args_list] == [1]

    def test_seed_reconciler_does_not_rearm_a_suggest_account(self):
        from cqc_lem.app.run_scheduler import auto_check_scheduled_posts

        with patch(_MODE, return_value="suggest"), \
             patch(f"{_SCHED}.get_ready_to_post_posts", return_value=[]), \
             patch(f"{_SCHED}.get_orphaned_scheduled_posts", return_value=[]), \
             patch(f"{_SCHED}.get_posts_missing_their_seed_comment", return_value=[(3, 2)]), \
             patch(f"{_SCHED}.get_ready_occasion_posts", return_value=[]), \
             patch(f"{_SCHED}.get_orphaned_occasion_claims", return_value=[]), \
             patch(f"{_SCHED}.auto_seed_comment_on_post") as seed, \
             patch(f"{_SCHED}.log_warning") as warn:
            auto_check_scheduled_posts.run()
        seed.apply_async.assert_not_called()
        warn.assert_not_called()

    def test_pre_post_browser_tasks_are_not_queued_but_the_post_still_is(self):
        from datetime import timedelta, timezone

        from cqc_lem.app.run_scheduler import auto_check_scheduled_posts

        slot = datetime.now(timezone.utc) + timedelta(minutes=10)
        with patch(_MODE, return_value="suggest"), \
             patch(f"{_SCHED}.get_ready_to_post_posts", return_value=[(7, slot, 2)]), \
             patch(f"{_SCHED}.get_active_user_ids", return_value=[2]), \
             patch(f"{_SCHED}.update_db_post_status"), \
             patch(f"{_SCHED}.post_to_linkedin") as publish, \
             patch(f"{_SCHED}.automate_commenting") as commenting, \
             patch(f"{_SCHED}.automate_profile_viewer_engagement") as viewer, \
             patch(f"{_SCHED}.record_pre_post_skipped") as skipped, \
             patch(f"{_SCHED}.get_orphaned_scheduled_posts", return_value=[]), \
             patch(f"{_SCHED}.get_posts_missing_their_seed_comment", return_value=[]), \
             patch(f"{_SCHED}.get_ready_occasion_posts", return_value=[]), \
             patch(f"{_SCHED}.get_orphaned_occasion_claims", return_value=[]):
            auto_check_scheduled_posts.run()
        publish.apply_async.assert_called_once()
        commenting.apply_async.assert_not_called()
        viewer.apply_async.assert_not_called()
        skipped.assert_not_called()

    @pytest.mark.parametrize("task,reader,dispatched", [
        ("auto_check_scheduled_dms", "get_due_scheduled_dms", "send_scheduled_dm"),
        ("auto_check_connection_requests", "get_approved_connection_requests",
         "send_connection_request"),
        ("auto_check_catchup_touches", "get_approved_catchup_touches", "send_catchup_touch"),
    ])
    def test_approval_queues_leave_a_suggest_accounts_rows_untouched(self, task, reader, dispatched):
        from datetime import timezone

        from cqc_lem.app import run_scheduler

        rows = ([(5, datetime.now(timezone.utc), 2)] if task == "auto_check_scheduled_dms"
                else [(5, 2)])
        with patch(_MODE, return_value="suggest"), \
             patch(f"{_SCHED}._skip_if_throttled", return_value=False), \
             patch(f"{_SCHED}.{reader}", return_value=rows), \
             patch(f"{_SCHED}.get_active_user_ids", return_value=[2]), \
             patch(f"{_SCHED}.get_user_ids_with_dms_in_flight", return_value=set()), \
             patch(f"{_SCHED}.get_user_ids_with_catchup_touches_in_flight", return_value=set()), \
             patch(f"{_SCHED}.get_orphaned_scheduled_dms", return_value=[]), \
             patch(f"{_SCHED}.get_orphaned_connection_requests", return_value=[]), \
             patch(f"{_SCHED}.get_orphaned_catchup_touches", return_value=[]), \
             patch(f"{_SCHED}.is_invites_held", return_value=False), \
             patch(f"{_SCHED}.report_catchup_run"), \
             patch(f"{_SCHED}.count_pending_catchup_touches", return_value=0), \
             patch(f"{_SCHED}.update_scheduled_dm_status") as dm_status, \
             patch(f"{_SCHED}.update_connection_request_status") as req_status, \
             patch(f"{_SCHED}.update_catchup_touch_status") as touch_status, \
             patch(f"{_SCHED}.{dispatched}") as send:
            getattr(run_scheduler, task).run()
        send.apply_async.assert_not_called()
        dm_status.assert_not_called()
        req_status.assert_not_called()
        touch_status.assert_not_called()


_FEED = "cqc_lem.app.engagement.feed"
_URL = "https://www.linkedin.com/feed/update/urn:li:ugcPost:7479519458164695040/"


class TestOwnPostCommentsBecomeSuggestions:
    """The seed and second-wave self-comments store a draft for a suggest account.

    An automate account still publishes them through the API; a suggest account gets the finished
    draft stored and nothing is published.
    """

    @pytest.fixture
    def _seed_inputs(self):
        with patch(f"{_FEED}.get_post_url_from_log_for_user", return_value=_URL), \
             patch(f"{_FEED}.has_user_commented_on_post_url", return_value=False), \
             patch(f"{_FEED}.get_post_content", return_value="post body"), \
             patch(f"{_FEED}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_or_create_profile_synthesis", return_value="s"), \
             patch(f"{_FEED}.get_post_first_comment_link", return_value=None):
            yield

    def test_seed_is_stored_not_published(self, _seed_inputs):
        from cqc_lem.app.engagement.feed import auto_seed_comment_on_post

        with patch(_MODE, return_value="suggest"), \
             patch("cqc_lem.utilities.engagement_mode.has_engagement_suggestion", return_value=False), \
             patch(f"{_FEED}.generate_seed_comment", return_value="What surprised you most?"), \
             patch("cqc_lem.utilities.engagement_mode.insert_engagement_suggestion",
                   return_value=True) as store, \
             patch(f"{_FEED}.comment_on_linkedin_post") as api, \
             patch(f"{_FEED}.insert_new_log") as log:
            result = auto_seed_comment_on_post.run(user_id=1, post_id=3)
        assert result == em.SUGGESTION_SAVED_MESSAGE
        api.assert_not_called()
        log.assert_not_called()
        args, kwargs = store.call_args
        assert args[3] == "What surprised you most?" and args[4] == "seed:3"
        assert kwargs["target_url"] == _URL

    def test_a_stored_seed_is_not_regenerated(self, _seed_inputs):
        from cqc_lem.app.engagement.feed import auto_seed_comment_on_post

        with patch(_MODE, return_value="suggest"), \
             patch("cqc_lem.utilities.engagement_mode.has_engagement_suggestion", return_value=True), \
             patch(f"{_FEED}.generate_seed_comment") as gen:
            result = auto_seed_comment_on_post.run(user_id=1, post_id=3)
        assert "already suggested" in result
        gen.assert_not_called()

    def test_seed_still_publishes_for_automate(self, _seed_inputs):
        from cqc_lem.app.engagement.feed import auto_seed_comment_on_post

        with patch(_MODE, return_value="automate"), \
             patch(f"{_FEED}.generate_seed_comment", return_value="Q?"), \
             patch(f"{_FEED}.comment_on_linkedin_post", return_value="urn:li:comment:1") as api, \
             patch(f"{_FEED}.insert_new_log"), \
             patch("cqc_lem.utilities.engagement_mode.insert_engagement_suggestion") as store:
            auto_seed_comment_on_post.run(user_id=1, post_id=3)
        api.assert_called_once()
        store.assert_not_called()

    @pytest.fixture
    def _wave_inputs(self, monkeypatch):
        monkeypatch.delenv("SECOND_WAVE_COMMENT_ENABLED", raising=False)
        with patch(f"{_FEED}.is_automation_paused", return_value=False), \
             patch(f"{_FEED}.get_post_age_minutes", return_value=9 * 60), \
             patch(f"{_FEED}.get_post_url_from_log_for_user", return_value=_URL), \
             patch(f"{_FEED}.count_user_comments_on_post_url", return_value=0), \
             patch(f"{_FEED}.get_post_content", return_value="post body"), \
             patch(f"{_FEED}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_FEED}.get_engagement_preferences", return_value={}), \
             patch(f"{_FEED}.get_or_create_profile_synthesis", return_value="s"), \
             patch(f"{_FEED}.get_story_bank_entries", return_value=[]), \
             patch(f"{_FEED}.get_recent_comment_texts", return_value=[]):
            yield

    def test_second_wave_is_stored_not_published(self, _wave_inputs):
        from cqc_lem.app.engagement.feed import auto_second_wave_comment

        with patch(_MODE, return_value="suggest"), \
             patch("cqc_lem.utilities.engagement_mode.has_engagement_suggestion", return_value=False), \
             patch(f"{_FEED}.generate_second_wave_comment", return_value="The number: 40%."), \
             patch("cqc_lem.utilities.engagement_mode.insert_engagement_suggestion",
                   return_value=True) as store, \
             patch(f"{_FEED}.comment_on_linkedin_post") as api, \
             patch(f"{_FEED}._record_golden_hour_report") as report:
            result = auto_second_wave_comment.run(user_id=1, post_id=3)
        assert result == em.SUGGESTION_SAVED_MESSAGE
        api.assert_not_called()
        report.assert_not_called()
        assert store.call_args.args[4] == "second_wave:3"

    def test_a_stored_second_wave_is_not_regenerated(self, _wave_inputs):
        from cqc_lem.app.engagement.feed import auto_second_wave_comment

        with patch(_MODE, return_value="suggest"), \
             patch("cqc_lem.utilities.engagement_mode.has_engagement_suggestion", return_value=True), \
             patch(f"{_FEED}.generate_second_wave_comment") as gen:
            result = auto_second_wave_comment.run(user_id=1, post_id=3)
        assert "already suggested" in result
        gen.assert_not_called()
