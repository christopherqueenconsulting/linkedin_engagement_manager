"""Unit tests for the daily suppression-tripwire beat task — issue #629.

These drive the task end-to-end from real `post_stats` rows (no stubbed trend) so the wiring the
detection depends on is exercised too, and assert the side effects that make the feature safe: the
pause is tagged as ours, the user is told once, a standing trip is re-armed rather than re-notified,
and nothing ever auto-resumes.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

RS = "cqc_lem.app.run_scheduler"
RL = "cqc_lem.utilities.linkedin.rate_limit"
_START = datetime(2026, 7, 1, 9, 0)


def _post(offset: int, impressions: int) -> dict:
    return {"post_id": 100 + offset, "scheduled_time": _START + timedelta(days=offset),
            "reactions": 10, "comments": 1, "reposts": 0, "saves": 0,
            "impressions": impressions, "archetype": None, "hook_style": None,
            "format": None, "topic": None, "buyer_stage": None}


def _rows(baseline: int, recent: int, *, baseline_days: int = 10, recent_days: int = 3) -> list:
    rows = [_post(i, baseline) for i in range(baseline_days)]
    rows += [_post(baseline_days + i, recent) for i in range(recent_days)]
    return rows


class _Harness:
    """Runs the task with every side effect patched, exposing the calls it made."""

    def __init__(self, es, rows, *, users=(1,), comment_rows=(), existing_trip=None,
                 pause_reason=None, paused=False):
        self.pause = es.enter_context(patch(f"{RL}.pause_automation", return_value=True))
        self.record = es.enter_context(patch(f"{RL}.record_suppression_trip", return_value=True))
        self.state = es.enter_context(
            patch(f"{RL}.suppression_trip_state", return_value=existing_trip))
        es.enter_context(patch(f"{RL}.automation_pause_reason", return_value=pause_reason))
        es.enter_context(patch(f"{RL}.is_automation_paused", return_value=paused))
        es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=list(users)))
        es.enter_context(patch("cqc_lem.utilities.db.get_post_performance_rows",
                               return_value=list(rows)))
        es.enter_context(patch("cqc_lem.utilities.db.get_comment_outcomes",
                               return_value=list(comment_rows)))
        self.notify = es.enter_context(
            patch("cqc_lem.utilities.notifications.notify_suppression_tripwire", return_value=True))
        self.track = es.enter_context(
            patch("cqc_lem.utilities.observability.track_suppression_check"))
        self.critical = es.enter_context(patch("cqc_lem.utilities.logger.log_critical"))

    def run(self):
        from cqc_lem.app.run_scheduler import auto_suppression_tripwire
        return auto_suppression_tripwire()


@pytest.fixture(autouse=True)
def _default_thresholds(monkeypatch):
    for name in ("SUPPRESSION_TRIPWIRE_ENABLED", "SUPPRESSION_DROP_RATIO",
                 "SUPPRESSION_CONSECUTIVE_DAYS", "SUPPRESSION_BASELINE_DAYS",
                 "SUPPRESSION_MIN_BASELINE_POSTS", "SUPPRESSION_PAUSE_SECONDS",
                 "SUPPRESSION_COMMENT_DAYS"):
        monkeypatch.delenv(name, raising=False)
    yield


class TestTrip:
    def test_a_collapse_pauses_records_notifies_and_escalates(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 340))
            result = h.run()
        assert "1 newly tripped" in result
        assert h.pause.called and h.record.called and h.notify.called and h.critical.called
        assert h.pause.call_args.kwargs["reason"] == "suppression:1"
        assert h.pause.call_args.args[0] >= 60
        assert h.track.call_args.kwargs["paused"] is True

    def test_the_recorded_reason_is_what_the_user_is_told(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 340))
            h.run()
        recorded = h.record.call_args.args[1]
        assert recorded and recorded == h.notify.call_args.args[1]

    def test_healthy_reach_changes_nothing(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 8400))
            result = h.run()
        assert "0 newly tripped" in result
        assert not h.pause.called and not h.record.called and not h.notify.called
        assert h.track.called  # every check is reported, not just the trips

    def test_a_thin_history_never_trips(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 340, baseline_days=1))
            result = h.run()
        assert "0 newly tripped" in result and not h.pause.called


class TestStandingTrip:
    _TRIP = {"user_id": 1, "reason": "reach collapse", "tripped_at": "2026-07-14T09:15:00+00:00"}

    def test_a_standing_trip_is_rearmed_but_the_user_is_not_re_emailed(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 340), existing_trip=self._TRIP,
                         paused=True, pause_reason="suppression:1")
            result = h.run()
        assert "1 re-armed" in result and "0 newly tripped" in result
        assert h.pause.called and not h.notify.called and not h.record.called
        assert h.track.call_args.kwargs["rearmed"] is True

    def test_rearming_never_extends_someone_elses_pause(self):
        # A maintenance/admin pause has its own lifecycle — a 90-day tripwire refresh must not
        # silently take it over.
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 340), existing_trip=self._TRIP,
                         paused=True, pause_reason="maintenance")
            result = h.run()
        assert not h.pause.called and "0 re-armed" in result

    def test_an_expired_pause_under_a_standing_trip_is_restored(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 340), existing_trip=self._TRIP,
                         paused=False, pause_reason=None)
            h.run()
        assert h.pause.called

    def test_recovery_is_reported_but_never_auto_cleared(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 8400), existing_trip=self._TRIP)
            result = h.run()
        assert "0 newly tripped" in result
        assert not h.pause.called
        assert h.track.call_args.kwargs["paused"] is True  # still paused until a human acts


class TestCommentDemotionPath:
    def _outcomes(self, count, visible):
        return [{"status": "checked", "reply_count": 0, "like_count": 0, "author_replied": 0,
                 "our_reply_sent": 0, "visible_most_relevant": visible} for _ in range(count)]

    def test_demoted_comments_trip_the_wire_on_their_own(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 8400), comment_rows=self._outcomes(12, 0))
            result = h.run()
        assert "1 newly tripped" in result
        assert "Comment demotion" in h.record.call_args.args[1]

    def test_healthy_comments_do_not_trip(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 8400), comment_rows=self._outcomes(12, 1))
            result = h.run()
        assert "0 newly tripped" in result and not h.pause.called


class TestFleet:
    def test_no_active_users(self):
        from cqc_lem.app.run_scheduler import auto_suppression_tripwire
        with patch(f"{RS}.get_active_user_ids", return_value=[]):
            assert auto_suppression_tripwire() == "No active users"

    def test_every_user_is_checked(self):
        with ExitStack() as es:
            h = _Harness(es, _rows(8500, 8400), users=(1, 2, 3))
            result = h.run()
        assert "3/3" in result and h.track.call_count == 3

    def test_the_feature_can_be_switched_off(self, monkeypatch):
        from cqc_lem.app.run_scheduler import auto_suppression_tripwire
        monkeypatch.setenv("SUPPRESSION_TRIPWIRE_ENABLED", "false")
        with patch(f"{RS}.get_active_user_ids") as users:
            assert auto_suppression_tripwire() == "Suppression tripwire disabled"
        assert not users.called


class TestMeasurementKeepsRunning:
    """The trip must not blind the tripwire. Stat capture is read-only and is the ONLY source of the
    readings the next day's check re-evaluates, so it survives OUR pause — and nothing else's."""

    def _dispatch(self, task_name, pause_reason):
        from cqc_lem.app import run_scheduler as rs
        paused = pause_reason is not None
        with ExitStack() as es:
            es.enter_context(patch(f"{RL}.automation_pause_remaining",
                                   return_value=600 if paused else 0))
            es.enter_context(patch(f"{RL}.automation_pause_reason", return_value=pause_reason))
            es.enter_context(patch(f"{RL}.rate_limit_cooldown_remaining", return_value=0))
            es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=[1]))
            es.enter_context(patch(f"{RS}.has_linkedin_session", return_value=True))
            dispatched = es.enter_context(
                patch(f"cqc_lem.app.run_automation.{task_name}"))
            result = (rs.auto_scrape_stats() if task_name == "auto_scrape_post_stats"
                      else rs.auto_capture_follower_stats())
        return result, dispatched

    @pytest.mark.parametrize("task_name", ["auto_scrape_post_stats", "capture_follower_stats"])
    def test_capture_still_runs_under_a_suppression_pause(self, task_name):
        result, dispatched = self._dispatch(task_name, "suppression:1")
        assert "throttled" not in result
        assert dispatched.apply_async.called

    @pytest.mark.parametrize("task_name", ["auto_scrape_post_stats", "capture_follower_stats"])
    def test_capture_still_stops_for_a_maintenance_pause(self, task_name):
        result, dispatched = self._dispatch(task_name, "maintenance")
        assert result == "Automation throttled"
        assert not dispatched.apply_async.called

    def test_engagement_lanes_are_not_exempted(self):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{RS}.is_automation_paused", return_value=True), \
             patch(f"{RL}.automation_pause_remaining", return_value=600), \
             patch(f"{RL}.automation_pause_reason", return_value="suppression:1"), \
             patch(f"{RL}.rate_limit_cooldown_remaining", return_value=0):
            assert rs._skip_if_throttled("auto_daily_engagement") is True
            assert rs._skip_if_throttled("auto_scrape_stats", measurement_only=True) is False


class TestBeatSchedule:
    def test_the_tripwire_runs_daily(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["suppression-tripwire"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_suppression_tripwire"
        assert entry["schedule"].hour == {9} and entry["schedule"].minute == {15}
