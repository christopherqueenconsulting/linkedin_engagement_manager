"""Unit tests for the no-post-day standalone commenting run."""

import pytest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RS = "cqc_lem.app.run_scheduler"
_DB = "cqc_lem.utilities.db"


class TestHasScheduledPostToday:
    def _conn(self, count):
        conn = MagicMock(); cur = MagicMock(); cur.fetchone.return_value = (count,)
        conn.cursor.return_value = cur
        return conn

    def test_true_when_post_today(self):
        with patch(f"{_DB}.get_db_connection", return_value=self._conn(1)):
            from cqc_lem.utilities.db import has_scheduled_post_today
            assert has_scheduled_post_today(1) is True

    def test_false_when_none(self):
        with patch(f"{_DB}.get_db_connection", return_value=self._conn(0)):
            from cqc_lem.utilities.db import has_scheduled_post_today
            assert has_scheduled_post_today(1) is False


class TestAutoDailyEngagement:
    def test_dispatches_daily_for_all_connected_users(self):
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        # Runs every day now (no post-day skip); only the no-session user is excluded.
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2, 3]), \
             patch(f"{_RS}.has_linkedin_session", side_effect=lambda u: u != 3), \
             patch(f"{_RS}._stagger_due", return_value=True), \
             patch(f"{_RS}.automate_commenting") as ac:
            result = auto_daily_engagement()
        assert ac.apply_async.call_count == 2                 # users 1 & 2 (3 has no session)
        assert "2/3" in result

    def test_golden_hour_stays_on_the_engage_lane(self):
        """Issue #553 gave the pre-post warm-up its own se_prepost lane by overriding queue= at
        that dispatch site only — the daily loop must keep falling through to the task's own
        se_engage queue, or it would crowd out the deadline-bound warm-ups."""
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_RS}._stagger_due", return_value=True), \
             patch(f"{_RS}.automate_commenting") as ac:
            auto_daily_engagement()
        assert "queue" not in ac.apply_async.call_args[1]

    def test_only_users_whose_slot_is_due_are_dispatched(self):
        """The beat now ticks every 15 minutes (issue #554) — a tick must hand se_engage only the
        users whose staggered slot came up, not the whole fleet."""
        from cqc_lem.app.run_scheduler import auto_daily_engagement, STAGGER_GOLDEN_HOUR
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2, 3]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_RS}._stagger_due", side_effect=lambda u, *_: u == 2) as due, \
             patch(f"{_RS}.automate_commenting") as ac:
            result = auto_daily_engagement()
        assert ac.apply_async.call_count == 1
        assert ac.apply_async.call_args[1]["kwargs"]["user_id"] == 2
        assert due.call_args[0][1] is STAGGER_GOLDEN_HOUR
        assert "1/3" in result

    def test_a_user_without_a_session_does_not_burn_their_slot(self):
        """The claim is once-a-day, so it must not be spent on a user who can't run anyway —
        connecting later in the day still earns that day's golden-hour run."""
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        with patch(f"{_RS}.get_active_user_ids", return_value=[9]), \
             patch(f"{_RS}.has_linkedin_session", return_value=False), \
             patch(f"{_RS}._stagger_due", return_value=True) as due, \
             patch(f"{_RS}.automate_commenting"):
            auto_daily_engagement()
        due.assert_not_called()


class TestStaggerDue:
    """The gate every staggered fan-out shares (issue #554)."""

    def _slot(self, reached=True, in_tick_window=True):
        return SimpleNamespace(reached=reached, in_tick_window=in_tick_window, offset_minutes=42,
                               local_at=datetime(2026, 7, 25, 9, 42),
                               at=datetime(2026, 7, 25, 13, 42))

    def _run(self, slot, claimed, tz="America/New_York"):
        from cqc_lem.app.run_scheduler import _stagger_due, STAGGER_GOLDEN_HOUR
        with patch(f"{_RS}.get_user_timezone", return_value=tz) as get_tz, \
             patch(f"{_RS}.plan_daily_slot", return_value=slot) as plan, \
             patch(f"{_RS}.claim_daily_slot", return_value=claimed) as claim:
            result = _stagger_due(7, STAGGER_GOLDEN_HOUR, "auto_daily_engagement")
        return result, get_tz, plan, claim

    def test_not_due_before_the_slot_and_no_claim_is_spent(self):
        result, _, _, claim = self._run(self._slot(reached=False, in_tick_window=False), True)
        assert result is False
        claim.assert_not_called()

    def test_due_when_the_slot_is_claimed(self):
        result, _, _, claim = self._run(self._slot(), True)
        assert result is True
        claim.assert_called_once()

    def test_already_dispatched_today_is_not_due_again(self):
        result, _, _, _ = self._run(self._slot(in_tick_window=True), False)
        assert result is False

    def test_missed_tick_catches_up_on_the_next_one(self):
        """Beat (or the 429 breaker) down over the slot → the claim is still free, so the run
        happens later that day instead of being lost."""
        result, _, _, _ = self._run(self._slot(in_tick_window=False), True)
        assert result is True

    def test_without_redis_falls_back_to_the_strict_tick_window(self):
        assert self._run(self._slot(in_tick_window=True), None)[0] is True
        assert self._run(self._slot(in_tick_window=False), None)[0] is False

    def test_local_anchor_plans_on_the_users_timezone(self):
        _, get_tz, plan, _ = self._run(self._slot(), True)
        get_tz.assert_called_once_with(7)
        assert plan.call_args[0][2] == "America/New_York"

    def test_utc_anchor_skips_the_timezone_lookup(self, monkeypatch):
        monkeypatch.setenv("GOLDEN_HOUR_ANCHOR_TZ", "utc")
        _, get_tz, plan, _ = self._run(self._slot(), True)
        get_tz.assert_not_called()
        assert plan.call_args[0][2] == "UTC"


class TestStaggeredBeatSchedule:
    """The per-user slot only works if the beat actually ticks at STAGGER_TICK_MINUTES — a
    once-a-day crontab would fire one arbitrary slot and drop everyone else (issue #554)."""

    def _schedule(self):
        from cqc_lem.app.my_celery import app
        return app.conf.beat_schedule

    @pytest.mark.parametrize("entry", ["daily-golden-hour-engagement", "send-appreciation-dms",
                                       "group-engagement"])
    def test_staggered_fanouts_tick_at_the_stagger_cadence(self, entry):
        from cqc_lem.utilities.engagement_window import STAGGER_TICK_MINUTES
        expected = set(range(0, 60, STAGGER_TICK_MINUTES))
        assert self._schedule()[entry]["schedule"].minute == expected
        assert self._schedule()[entry]["schedule"].hour == set(range(24))
