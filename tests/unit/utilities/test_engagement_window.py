"""Unit tests for cqc_lem.utilities.engagement_window (issue #547)."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.engagement_window"

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def mock_redis():
    """A working Redis handle for the marker helpers (the unit lane blocks the real one)."""
    client = MagicMock()
    with patch(f"{_MOD}.shared_redis_client", return_value=client), \
         patch(f"{_MOD}.track_pre_post_engagement"):
        yield client


@pytest.fixture
def mock_track():
    with patch(f"{_MOD}.shared_redis_client", return_value=None), \
         patch(f"{_MOD}.track_pre_post_engagement") as tracker:
        yield tracker


class TestPlanPrePostWindow:
    def test_full_lead_available_starts_lead_minutes_before(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(hours=1), 15, now=_NOW)

        assert window is not None
        assert window.eta == _NOW + timedelta(minutes=45)
        assert window.duration_seconds == 15 * 60
        assert window.clamped is False

    def test_exactly_one_lead_away_is_not_clamped(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(minutes=15), 15, now=_NOW)

        assert window.eta == _NOW
        assert window.duration_seconds == 15 * 60
        assert window.clamped is False

    def test_late_pickup_clamps_eta_to_now(self):
        """The stale-eta case: less than the lead left → fire ASAP, never with an eta in the past."""
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(minutes=6), 15, now=_NOW)

        assert window.eta == _NOW
        assert window.duration_seconds == 6 * 60  # loop ends at the post, not 15 min later
        assert window.clamped is True

    def test_post_at_its_scheduled_time_returns_none(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        assert plan_pre_post_window(_NOW, 15, now=_NOW) is None

    def test_post_past_its_scheduled_time_returns_none(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        assert plan_pre_post_window(_NOW - timedelta(hours=3), 15, now=_NOW) is None

    def test_window_shorter_than_minimum_returns_none(self):
        """60s of runway is a page load, not a warm-up — don't spin up a browser for it."""
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        assert plan_pre_post_window(_NOW + timedelta(seconds=60), 15, now=_NOW) is None

    def test_minimum_window_is_configurable(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(seconds=60), 15, now=_NOW, min_window_seconds=30)

        assert window is not None
        assert window.duration_seconds == 60

    def test_naive_scheduled_time_is_treated_as_utc(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        naive = (_NOW + timedelta(hours=1)).replace(tzinfo=None)
        window = plan_pre_post_window(naive, 15, now=_NOW)

        assert window.eta == _NOW + timedelta(minutes=45)

    def test_naive_now_is_treated_as_utc(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(hours=1), 15, now=_NOW.replace(tzinfo=None))

        assert window.eta == _NOW + timedelta(minutes=45)

    def test_defaults_to_current_time(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(datetime.now(timezone.utc) + timedelta(hours=2), 10)

        assert window is not None
        assert window.duration_seconds == 10 * 60

    def test_viewer_lead_is_shorter_than_comment_lead(self):
        from cqc_lem.utilities.engagement_window import (
            PRE_POST_COMMENT_LEAD_MINUTES, PRE_POST_VIEWER_LEAD_MINUTES)
        assert PRE_POST_VIEWER_LEAD_MINUTES < PRE_POST_COMMENT_LEAD_MINUTES


class TestMarkers:
    def test_scheduled_marker_written_to_redis(self, mock_redis):
        from cqc_lem.utilities.engagement_window import PrePostWindow, record_pre_post_scheduled
        window = PrePostWindow(eta=_NOW, duration_seconds=900, clamped=False)
        record_pre_post_scheduled(11, 7, window)

        key, mapping = mock_redis.hset.call_args[0][0], mock_redis.hset.call_args[1]["mapping"]
        assert key == "engagement:prepost:11"
        assert mapping["status"] == "scheduled"
        assert mapping["window_seconds"] == "900"
        assert mapping["task_name"] == "automate_commenting"
        mock_redis.expire.assert_called_once()

    def test_skipped_marker_records_reason(self, mock_redis):
        from cqc_lem.utilities.engagement_window import record_pre_post_skipped
        record_pre_post_skipped(12, 7, "throttled")

        mapping = mock_redis.hset.call_args[1]["mapping"]
        assert mapping["status"] == "skipped"
        assert mapping["skip_reason"] == "throttled"

    def test_run_marker_accumulates_runs_and_comments(self, mock_redis):
        from cqc_lem.utilities.engagement_window import record_pre_post_run
        record_pre_post_run(13, 7, 3, now=_NOW)

        increments = {c[0][1]: c[0][2] for c in mock_redis.hincrby.call_args_list}
        assert increments == {"runs": 1, "comments": 3}
        assert mock_redis.hset.call_args[1]["mapping"]["last_run_at"] == _NOW.isoformat()

    def test_run_marker_handles_no_comments(self, mock_redis):
        from cqc_lem.utilities.engagement_window import record_pre_post_run
        record_pre_post_run(14, 7, None)

        increments = {c[0][1]: c[0][2] for c in mock_redis.hincrby.call_args_list}
        assert increments == {"runs": 1, "comments": 0}

    def test_markers_fail_open_without_redis(self, mock_track):
        from cqc_lem.utilities.engagement_window import (
            PrePostWindow, record_pre_post_run, record_pre_post_scheduled, record_pre_post_skipped)
        record_pre_post_scheduled(15, 7, PrePostWindow(eta=_NOW, duration_seconds=900, clamped=True))
        record_pre_post_skipped(15, 7, "past_window")
        record_pre_post_run(15, 7, 1)

        # No Redis → no crash, and the PostHog event still carries the observability signal.
        statuses = [c[0][2] for c in mock_track.call_args_list]
        assert statuses == ["scheduled", "skipped", "ran"]

    def test_marker_write_error_is_swallowed(self, mock_redis):
        from cqc_lem.utilities.engagement_window import record_pre_post_skipped
        mock_redis.hset.side_effect = RuntimeError("redis down")

        record_pre_post_skipped(16, 7, "past_window")  # must not raise

    def test_tracks_posthog_event_for_dispatch(self):
        from cqc_lem.utilities.engagement_window import PrePostWindow, record_pre_post_scheduled
        with patch(f"{_MOD}.shared_redis_client", return_value=None), \
             patch(f"{_MOD}.track_pre_post_engagement") as tracker:
            record_pre_post_scheduled(17, 7, PrePostWindow(eta=_NOW, duration_seconds=300, clamped=True))

        args, kwargs = tracker.call_args
        assert args == (17, 7, "scheduled")
        assert kwargs["clamped"] is True
        assert kwargs["window_seconds"] == 300


class TestGetPrePostWindowStat:
    def test_returns_decoded_stat(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.return_value = {
            b"status": b"scheduled", b"runs": b"2", b"comments": b"5",
            b"window_seconds": b"900", b"user_id": b"7", b"clamped": b"0",
        }
        stat = get_pre_post_window_stat(21)

        assert stat["status"] == "scheduled"
        assert stat["runs"] == 2
        assert stat["comments"] == 5
        assert stat["user_id"] == 7

    def test_defaults_counters_when_never_run(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.return_value = {b"status": b"skipped", b"skip_reason": b"throttled"}
        stat = get_pre_post_window_stat(22)

        assert stat["runs"] == 0
        assert stat["comments"] == 0
        assert stat["skip_reason"] == "throttled"

    def test_non_numeric_counter_is_left_as_text(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.return_value = {b"runs": b"n/a"}

        assert get_pre_post_window_stat(23)["runs"] == "n/a"

    def test_unknown_post_returns_empty_dict(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.return_value = {}

        assert get_pre_post_window_stat(24) == {}

    def test_no_redis_returns_empty_dict(self):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        with patch(f"{_MOD}.shared_redis_client", return_value=None):
            assert get_pre_post_window_stat(25) == {}

    def test_read_error_returns_empty_dict(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.side_effect = RuntimeError("redis down")

        assert get_pre_post_window_stat(26) == {}
