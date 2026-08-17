"""Automated native publishing of an occasion draft (issue #1088, Phase 2 of #1074).

Phase 1's whole point is that these rows never reach the REST publish path. Phase 2 gives them a
browser instead — which makes this the only lane in LEM that WRITES a post through Selenium, so the
policy around it carries the weight:

* It is OFF by default and reads the flag at BOTH ends (the dispatcher and the task), so an OFF
  deployment never even queues the message.
* The row is CLAIMED before Chrome opens. An occasion announcement published twice is public and
  un-deletable, and the read that would tell us it already went out is the read that can fail.
* Only a step that provably left nothing on LinkedIn puts the row back on the queue. A Post click
  the feed never confirmed is held for a human, never retried.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.utilities.linkedin import share_composer as sc

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.app.engagement.posting"
_BODY = "Shipped the v2 scheduler tonight after four weeks of nights, and here is what broke."


class TestTheNativePublishQueue:
    def test_it_asks_only_for_approved_manual_publish_rows(self, mock_database_connection):
        """The mirror image of `get_ready_to_post_posts`' `manual_publish = 0`.

        Two queries, so one row can never be handed to both the API path and the browser path.
        """
        from cqc_lem.utilities.db import get_ready_occasion_posts

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            get_ready_occasion_posts()

        sql = " ".join(mock_database_connection["cursor"].execute.call_args[0][0].split())
        assert "manual_publish = 1" in sql
        assert "status = 'approved'" in sql

    def test_a_read_failure_answers_empty(self, mock_database_connection):
        from cqc_lem.utilities.db import get_ready_occasion_posts

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")

            assert get_ready_occasion_posts() == []

    def test_the_abandoned_claim_read_asks_for_the_rows_the_orphan_sweep_excludes(
            self, mock_database_connection):
        """`get_orphaned_scheduled_posts` filters `manual_publish = 0`, so nothing else sees these.

        A claim its worker never resolved is `scheduled` + `manual_publish = 1`, which is a state
        only `auto_publish_occasion_post` writes.
        """
        from cqc_lem.utilities.db import get_orphaned_occasion_claims

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].fetchall.return_value = []

            get_orphaned_occasion_claims()

        sql = " ".join(mock_database_connection["cursor"].execute.call_args[0][0].split())
        assert "manual_publish = 1" in sql
        assert "status = 'scheduled'" in sql

    def test_the_abandoned_claim_read_answers_empty_on_a_read_failure(
            self, mock_database_connection):
        from cqc_lem.utilities.db import get_orphaned_occasion_claims

        with patch("cqc_lem.platform.db.connection.get_db_connection") as mock_conn:
            mock_conn.return_value = mock_database_connection["connection"]
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")

            assert get_orphaned_occasion_claims() == []


class TestTheDispatcher:
    def _run(self, flag_on: bool, due=((7, MagicMock(), 3),), stranded=()):
        from cqc_lem.app.run_scheduler import auto_check_scheduled_posts

        with patch("cqc_lem.app.run_scheduler.get_ready_to_post_posts", return_value=[]), \
             patch("cqc_lem.app.run_scheduler.get_orphaned_scheduled_posts", return_value=[]), \
             patch("cqc_lem.app.run_scheduler.get_active_user_ids", return_value=[3]), \
             patch("cqc_lem.app.run_scheduler.flag_enabled", return_value=flag_on) as flag, \
             patch("cqc_lem.app.run_scheduler.get_ready_occasion_posts",
                   return_value=list(due)) as query, \
             patch("cqc_lem.app.run_scheduler.get_orphaned_occasion_claims",
                   return_value=list(stranded)), \
             patch("cqc_lem.app.run_scheduler.update_db_post_status") as status, \
             patch("cqc_lem.app.run_scheduler.auto_publish_occasion_post") as task:
            result = auto_check_scheduled_posts.run()
        task.status_write = status
        return result, flag, query, task

    def test_the_flag_is_resolved_for_the_ROW_S_owner_not_the_system(self):
        """A %-rollout targets people.

        Gating the whole block on the system identity would switch off every user PostHog had
        switched on.
        """
        _, flag, _, task = self._run(flag_on=False)

        flag.assert_called_once_with("occasion-native-publish-enabled", 3)
        task.apply_async.assert_not_called()

    def test_a_due_draft_is_dispatched_with_its_owner(self):
        result, _, _, task = self._run(flag_on=True)

        task.apply_async.assert_called_once_with(kwargs={"user_id": 3, "post_id": 7})
        assert "1 native occasion post" in result

    def test_an_empty_queue_still_reads_as_nothing_to_do(self):
        result, _, _, task = self._run(flag_on=True, due=())

        task.apply_async.assert_not_called()
        assert result == "No Post to Schedule"

    def test_an_abandoned_claim_is_held_for_a_human_never_re_queued(self):
        """A worker lost mid-composer leaves the row claimed at `scheduled`.

        The orphan sweep excludes `manual_publish` rows on purpose, so without this recovery the
        draft is stranded forever. It must NOT go back on the queue: a dead worker proves nothing
        about whether Post was pressed, and re-running is how one occasion becomes two.
        """
        from cqc_lem.utilities.db import PostStatus

        result, _, _, task = self._run(flag_on=True, due=(), stranded=((9, MagicMock(), 3),))

        task.apply_async.assert_not_called()
        task.status_write.assert_called_once_with(9, PostStatus.ERROR)
        assert "1 abandoned native-publish claim" in result

    def test_an_abandoned_claim_is_recovered_even_with_the_flag_off(self):
        """The claim outlives the flag being switched back off — the row would never resolve."""
        from cqc_lem.utilities.db import PostStatus

        _, _, _, task = self._run(flag_on=False, due=(), stranded=((9, MagicMock(), 3),))

        task.status_write.assert_called_once_with(9, PostStatus.ERROR)


def _publish_result(state=sc.PUBLISHED, reason="published natively", zero_walk=None):
    return sc.OccasionPublishResult(state, reason, zero_walk)


class TestAutoPublishOccasionPost:
    def _run(self, *, flag_on=True, status="approved", manual=True, owner=1,
             archetype="project_launch", content=_BODY, allowed_day=True, lock="token",
             claimed=True, result=None, session_raises=False):
        from cqc_lem.app.engagement.posting import auto_publish_occasion_post

        targets = {
            "flag_enabled": {"return_value": flag_on},
            "get_post_user_id": {"return_value": owner},
            "get_post_manual_publish": {"return_value": manual},
            "get_post_status": {"return_value": status},
            "get_post_archetype": {"return_value": archetype},
            "get_post_content": {"return_value": content},
            "_occasion_publish_day_allowed": {"return_value": allowed_day},
            "acquire_run_lock": {"return_value": lock},
            "release_run_lock": {},
            "update_db_post_status": {"return_value": claimed},
            "insert_new_log": {},
            "quit_gracefully": {},
        }
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch(f"{_MOD}.{name}", **kw))
                     for name, kw in targets.items()}
            profile = stack.enter_context(patch(f"{_MOD}.get_current_profile"))
            if session_raises:
                profile.side_effect = RuntimeError("no chrome")
            else:
                profile.return_value = (MagicMock(), MagicMock(), "u@example.com", MagicMock())
            publish = stack.enter_context(
                patch(f"{_MOD}._occasion.publish_occasion_natively",
                      return_value=result or _publish_result()))
            mocks["publish"] = publish
            mocks["get_current_profile"] = profile
            answer = auto_publish_occasion_post.run(1, 5)
        return answer, mocks

    # --- the gates, each of which must stop BEFORE a browser opens ------------------------------

    def test_the_flag_off_is_an_expected_no_op(self):
        answer, mocks = self._run(flag_on=False)

        assert "off" in answer
        mocks["get_current_profile"].assert_not_called()
        mocks["update_db_post_status"].assert_not_called()

    @pytest.mark.parametrize("kwargs", [
        {"owner": 2},                      # someone else's row
        {"manual": False},                 # publishes through the API
        {"status": "posted"},              # already gone out
        {"archetype": "case_snapshot"},    # no composer mapping
        {"content": "   "},                # nothing to publish
        {"allowed_day": False},            # the user's own cadence bound
    ])
    def test_every_refusal_stops_before_chrome(self, kwargs):
        _, mocks = self._run(**kwargs)

        mocks["get_current_profile"].assert_not_called()
        mocks["publish"].assert_not_called()
        mocks["update_db_post_status"].assert_not_called()

    def test_the_retry_window_is_a_skip_not_a_second_session(self):
        _, mocks = self._run(lock=None)

        mocks["get_current_profile"].assert_not_called()
        mocks["update_db_post_status"].assert_not_called()

    def test_a_claim_that_did_not_land_never_publishes(self):
        """The no-duplicate guarantee rests entirely on that write.

        A row still sitting in the dispatcher's `approved` query must not be published against.
        """
        _, mocks = self._run(claimed=False)

        mocks["get_current_profile"].assert_not_called()
        mocks["publish"].assert_not_called()
        mocks["release_run_lock"].assert_called_once()

    # --- the claim ------------------------------------------------------------------------------

    def test_the_row_is_claimed_before_the_browser_opens(self):
        """`scheduled` is what takes the row out of the dispatcher's `approved` query.

        A second tick then cannot start a duplicate announcement while this one is mid-composer.
        """
        from cqc_lem.utilities.db import PostStatus

        _, mocks = self._run()
        first_write = mocks["update_db_post_status"].call_args_list[0]
        assert first_write.args == (5, PostStatus.SCHEDULED)

    def test_a_session_that_never_opened_releases_the_claim_and_the_lock(self):
        from cqc_lem.utilities.db import PostStatus

        answer, mocks = self._run(session_raises=True)

        assert answer.startswith("Failed:")
        assert mocks["update_db_post_status"].call_args_list[-1].args == (5, PostStatus.APPROVED)
        mocks["release_run_lock"].assert_called_once()

    # --- the outcomes ---------------------------------------------------------------------------

    def test_a_confirmed_publish_marks_the_row_and_logs_the_body(self):
        from cqc_lem.utilities.db import LogResultType, PostStatus

        answer, mocks = self._run()

        assert "published natively" in answer
        assert mocks["update_db_post_status"].call_args_list[-1].args == (5, PostStatus.POSTED)
        # The log message is the post BODY, the convention every downstream reader depends on.
        assert mocks["insert_new_log"].call_args.kwargs["message"] == _BODY
        assert mocks["insert_new_log"].call_args.kwargs["result"] is LogResultType.SUCCESS
        mocks["release_run_lock"].assert_called_once()

    @pytest.mark.parametrize("state", [sc.NO_SHARE_BOX, sc.NO_OCCASION_ENTRY, sc.NO_OCCASION_TYPE,
                                       sc.NO_EDITOR, sc.NO_POST_BUTTON, sc.DRIVER_ERROR])
    def test_a_step_that_left_nothing_behind_goes_back_on_the_queue(self, state):
        from cqc_lem.utilities.db import PostStatus

        _, mocks = self._run(result=_publish_result(state, "blocked", "drift"))

        assert mocks["update_db_post_status"].call_args_list[-1].args == (5, PostStatus.APPROVED)
        # The lock is deliberately HELD: the next 10-minute scan must not re-open Chrome for a
        # composer that has already answered.
        mocks["release_run_lock"].assert_not_called()
        mocks["insert_new_log"].assert_not_called()

    @pytest.mark.parametrize("state,zero_walk,warns", [
        (sc.NO_SHARE_BOX, "drift", False),
        (sc.NO_OCCASION_TYPE, "empty", False),
        (sc.NO_EDITOR, "unknown", False),
        (sc.DRIVER_ERROR, None, True),
    ])
    def test_a_graded_step_does_not_warn_twice_for_one_blocked_run(self, state, zero_walk, warns):
        """`grade_zero_walk` already owns the WARNING for a drift verdict.

        Warning again here would file a SECOND grouped `$exception` for one blocked run, and an
        `empty`/`unknown` verdict grounds nothing and must not warn at all. A browser fault grades
        nothing, so there this line is the only signal there is.
        """
        with patch(f"{_MOD}.log_warning") as warn:
            self._run(result=_publish_result(state, "blocked", zero_walk))

        assert warn.called is warns

    def test_an_unconfirmed_click_is_held_for_a_human_and_never_retried(self):
        """The one outcome that must not go back on the queue.

        The post may well be live, and a retry is how one occasion becomes two.
        """
        from cqc_lem.utilities.db import LogResultType, PostStatus

        answer, mocks = self._run(
            result=_publish_result(sc.UNCONFIRMED, "post not confirmed on the feed", "empty"))

        assert mocks["update_db_post_status"].call_args_list[-1].args == (5, PostStatus.ERROR)
        assert mocks["insert_new_log"].call_args.kwargs["result"] is LogResultType.FAILURE
        assert "error" in answer
        # Released, because the row is resolved — it is out of the `approved` queue for good.
        mocks["release_run_lock"].assert_called_once()

    def test_the_driver_is_always_quit(self):
        _, mocks = self._run(result=_publish_result(sc.NO_SHARE_BOX, "blocked", "drift"))
        mocks["quit_gracefully"].assert_called_once()


class TestThePostingDayBound:
    def _allowed(self, prefs, weekday_now, raises=False):
        from cqc_lem.app.engagement.posting import _occasion_publish_day_allowed

        now = MagicMock()
        now.weekday.return_value = weekday_now
        with patch(f"{_MOD}.get_engagement_preferences",
                   side_effect=RuntimeError("db down") if raises else None,
                   return_value=prefs), \
             patch(f"{_MOD}.get_user_timezone", return_value="UTC"), \
             patch(f"{_MOD}.datetime") as dt:
            dt.now.return_value = now
            return _occasion_publish_day_allowed(1)

    def test_an_allowed_weekday_publishes(self):
        assert self._allowed({"posting_days": [0, 1, 2, 3, 4]}, weekday_now=2) is True

    def test_a_switched_off_weekday_waits(self):
        assert self._allowed({"posting_days": [0, 1, 2, 3, 4]}, weekday_now=5) is False

    def test_no_preference_falls_back_to_the_shipped_default(self):
        assert self._allowed({}, weekday_now=5) is False
        assert self._allowed({}, weekday_now=1) is True

    def test_an_unreadable_preference_fails_open(self):
        """A dated announcement must not be stranded by a DB blip.

        The cost of being wrong the other way is publishing on a day the user would have allowed
        anyway.
        """
        assert self._allowed({}, weekday_now=5, raises=True) is True
