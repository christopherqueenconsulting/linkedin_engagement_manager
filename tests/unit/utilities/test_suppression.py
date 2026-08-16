"""Unit tests for the suppression tripwire's detection core — issue #629.

The expensive failure this feature exists to prevent is a FALSE trip (a user loses a day of
engagement for nothing) sitting next to a missed real one (60-90 days of suppressed reach), so the
cases below are mostly about the guards: sparse posting, cold start, a thin baseline, an incomplete
impression day, and a recovery reading. Also covers the Redis trip record in `rate_limit`, which is
what makes the pause explainable in the UI and non-self-resuming.
"""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import suppression as sup

pytestmark = pytest.mark.unit

_RL = "cqc_lem.utilities.linkedin.rate_limit"


def _day(offset: int, *, impressions=None, engagement=0, posts=1, start="2026-07-01") -> dict:
    """One `build_engagement_trend` bucket `offset` days after `start`."""
    return {"date": (date.fromisoformat(start) + timedelta(days=offset)).isoformat(),
            "impressions": impressions, "engagement": engagement, "posts": posts}


def _trend(baseline_values, recent_values, *, engagement=False, step=1, posts=1) -> list:
    """A daily series: the baseline days first, then the recent run, `step` days apart."""
    out = []
    for index, value in enumerate([*baseline_values, *recent_values]):
        key = "engagement" if engagement else "impressions"
        day = _day(index * step, posts=posts)
        day[key] = value
        out.append(day)
    return out


@pytest.fixture(autouse=True)
def _default_thresholds(monkeypatch):
    for name in ("SUPPRESSION_TRIPWIRE_ENABLED", "SUPPRESSION_DROP_RATIO",
                 "SUPPRESSION_CONSECUTIVE_DAYS", "SUPPRESSION_BASELINE_DAYS",
                 "SUPPRESSION_MIN_BASELINE_POSTS", "SUPPRESSION_PAUSE_SECONDS",
                 "SUPPRESSION_COMMENT_DAYS"):
        monkeypatch.delenv(name, raising=False)
    yield


def _reach(verdict: dict) -> dict:
    return next(s for s in verdict["signals"] if s["name"] == sup.SIGNAL_REACH)


class TestReachCollapse:
    def test_steady_reach_is_ok(self):
        verdict = sup.evaluate_suppression(_trend([8500] * 10, [8200, 8600, 8400]))
        assert verdict["status"] == sup.STATUS_OK
        assert verdict["tripped"] is False
        assert _reach(verdict)["metric"] == sup.METRIC_IMPRESSIONS

    def test_documented_step_collapse_trips(self):
        verdict = sup.evaluate_suppression(_trend([8500] * 10, [340, 310, 290]))
        assert verdict["tripped"] is True
        assert verdict["status"] == sup.STATUS_TRIPPED
        reach = _reach(verdict)
        assert reach["baseline"] == 8500
        assert reach["max_drop"] > 0.95
        assert "8,500" in reach["reason"]

    def test_one_bad_day_is_only_a_watch(self):
        verdict = sup.evaluate_suppression(_trend([8500] * 10, [300, 8200, 8400]))
        assert verdict["status"] == sup.STATUS_WATCH
        assert verdict["tripped"] is False

    def test_a_drop_short_of_the_threshold_is_ok(self):
        # 50% off the median is a bad week, not a penalty — the tripwire must not fire on it.
        verdict = sup.evaluate_suppression(_trend([8000] * 10, [4000, 4200, 3900]))
        assert verdict["status"] == sup.STATUS_OK

    def test_recovery_reads_as_ok_again(self):
        collapsed = _trend([8500] * 10, [340, 310, 290])
        recovered = collapsed + [_day(13 + i, impressions=8100) for i in range(3)]
        assert sup.evaluate_suppression(collapsed)["tripped"] is True
        assert sup.evaluate_suppression(recovered)["tripped"] is False

    def test_growth_never_trips(self):
        verdict = sup.evaluate_suppression(_trend([500] * 10, [9000, 9500, 9900]))
        assert verdict["status"] == sup.STATUS_OK
        assert _reach(verdict)["max_drop"] < 0


class TestGuards:
    def test_cold_start_is_unknown_not_a_trip(self):
        verdict = sup.evaluate_suppression(_trend([8500], [340, 300]))
        assert verdict["status"] == sup.STATUS_UNKNOWN
        assert "posting day" in _reach(verdict)["reason"]

    def test_a_first_week_account_has_no_baseline(self):
        # Four posting days, but only one post behind the recent run — below min_baseline_posts.
        verdict = sup.evaluate_suppression(_trend([8500], [340, 300, 280]))
        assert verdict["status"] == sup.STATUS_UNKNOWN
        assert _reach(verdict)["baseline_posts"] == 1
        assert "baseline" in _reach(verdict)["reason"]

    def test_sparse_posting_still_scores_on_posting_days(self):
        # Posts every 4th calendar day: the days between simply do not exist in the trend, so the
        # run of 3 POSTING days is what gets compared — a weekend off is not a collapse.
        verdict = sup.evaluate_suppression(_trend([8500] * 4, [340, 310, 300], step=4))
        assert verdict["tripped"] is True
        assert _reach(verdict)["posting_days"] == 7

    def test_baseline_older_than_the_window_is_not_used(self):
        # The high-reach history is 60+ days back; only the (equally low) recent fortnight counts,
        # so today's numbers are normal for this account and nothing trips.
        old = [_day(i, impressions=8500) for i in range(3)]
        recent_window = [_day(60 + i, impressions=320) for i in range(6)]
        verdict = sup.evaluate_suppression(old + recent_window)
        assert verdict["tripped"] is False
        assert _reach(verdict)["baseline"] == 320

    def test_days_without_posts_are_ignored_entirely(self):
        trend = _trend([8500] * 10, [8400, 8300, 8200])
        trend.insert(5, _day(99, impressions=0, posts=0))
        verdict = sup.evaluate_suppression(trend)
        assert verdict["status"] == sup.STATUS_OK
        assert _reach(verdict)["posting_days"] == 13

    def test_zero_baseline_never_trips(self):
        verdict = sup.evaluate_suppression(_trend([0] * 10, [0, 0, 0], engagement=True))
        assert verdict["status"] == sup.STATUS_UNKNOWN
        assert "zero" in _reach(verdict)["reason"]

    def test_empty_trend_is_unknown(self):
        assert sup.evaluate_suppression([])["status"] == sup.STATUS_UNKNOWN
        assert sup.evaluate_suppression(None)["status"] == sup.STATUS_UNKNOWN


class TestMetricSelection:
    def test_falls_back_to_engagement_when_impressions_are_incomplete(self):
        trend = _trend([50] * 10, [2, 1, 2], engagement=True)  # impressions all None
        verdict = sup.evaluate_suppression(trend)
        assert _reach(verdict)["metric"] == sup.METRIC_ENGAGEMENT
        assert verdict["tripped"] is True

    def test_a_single_impressionless_day_switches_the_whole_comparison(self):
        trend = _trend([8500] * 10, [8400, 8300, 8200])
        for day in trend:
            day["engagement"] = 100
        trend[-1]["impressions"] = None
        verdict = sup.evaluate_suppression(trend)
        assert _reach(verdict)["metric"] == sup.METRIC_ENGAGEMENT
        assert verdict["status"] == sup.STATUS_OK

    def test_multi_post_days_are_compared_per_post(self):
        # 2 posts/day at 17000 total is the SAME per-post reach as 1 post at 8500 — a day the user
        # posted twice must not read as a doubling, nor its quiet twin as a collapse.
        trend = _trend([8500] * 10, [17000, 16800, 17200])
        for day in trend[-3:]:
            day["posts"] = 2
        assert sup.evaluate_suppression(trend)["status"] == sup.STATUS_OK


class TestCommentDemotionSignal:
    def _quality(self, status):
        return {"verdict": {"status": status, "reason": "50% of 12 readable comments were demoted",
                            "demotion_rate": 0.5, "visibility_sample": 12}}

    def test_a_comment_hold_trips_even_with_healthy_reach(self):
        verdict = sup.evaluate_suppression(_trend([8500] * 10, [8400, 8300, 8200]),
                                           comment_quality=self._quality("hold"))
        assert verdict["tripped"] is True
        assert "Comment demotion" in verdict["reason"]

    def test_a_comment_watch_does_not_trip(self):
        verdict = sup.evaluate_suppression(_trend([8500] * 10, [8400, 8300, 8200]),
                                           comment_quality=self._quality("watch"))
        assert verdict["status"] == sup.STATUS_WATCH
        assert verdict["tripped"] is False

    def test_healthy_comments_and_healthy_reach_are_ok(self):
        verdict = sup.evaluate_suppression(_trend([8500] * 10, [8400, 8300, 8200]),
                                           comment_quality=self._quality("ok"))
        assert verdict["status"] == sup.STATUS_OK

    def test_missing_comment_quality_is_not_a_signal(self):
        verdict = sup.evaluate_suppression(_trend([8500] * 10, [8400, 8300, 8200]))
        signal = next(s for s in verdict["signals"] if s["name"] == sup.SIGNAL_COMMENT_DEMOTION)
        assert signal["status"] == sup.STATUS_UNKNOWN


class TestThresholdConfig:
    def test_env_overrides_are_honoured(self, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_DROP_RATIO", "0.4")
        monkeypatch.setenv("SUPPRESSION_CONSECUTIVE_DAYS", "2")
        verdict = sup.evaluate_suppression(_trend([8000] * 10, [4000, 3900, 3800]))
        assert verdict["tripped"] is True
        assert verdict["config"] == {"drop_ratio": 0.4, "consecutive_days": 2,
                                     "baseline_days": 14, "min_baseline_posts": 3}

    def test_a_percent_shaped_ratio_cannot_disable_the_tripwire(self, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_DROP_RATIO", "70")  # meant 70%, wrote 70
        assert sup.drop_ratio() == 0.99
        assert sup.evaluate_suppression(_trend([8500] * 10, [1, 1, 1]))["tripped"] is True

    def test_unparseable_values_fall_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_DROP_RATIO", "abc")
        monkeypatch.setenv("SUPPRESSION_CONSECUTIVE_DAYS", "nope")
        monkeypatch.setenv("SUPPRESSION_BASELINE_DAYS", "")
        assert sup.drop_ratio() == sup.DEFAULT_DROP_RATIO
        assert sup.consecutive_days() == sup.DEFAULT_CONSECUTIVE_DAYS
        assert sup.baseline_days() == sup.DEFAULT_BASELINE_DAYS

    def test_history_days_covers_the_baseline_and_the_run(self):
        assert sup.history_days() > sup.baseline_days()

    def test_tripwire_can_be_switched_off(self, monkeypatch):
        assert sup.tripwire_enabled() is True
        monkeypatch.setenv("SUPPRESSION_TRIPWIRE_ENABLED", "false")
        assert sup.tripwire_enabled() is False

    def test_pause_seconds_has_a_floor(self, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_PAUSE_SECONDS", "1")
        assert sup.pause_seconds() == 60


class TestTripRecord:
    def _client(self):
        client = MagicMock()
        store = {}
        client.set.side_effect = lambda key, value, **kw: store.__setitem__(key, value)
        client.get.side_effect = lambda key: store.get(key)
        client.delete.side_effect = lambda key: store.pop(key, None)
        return client, store

    def test_round_trips_the_trip(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        client, store = self._client()
        with patch(f"{_RL}._redis_client", return_value=client):
            assert rl.record_suppression_trip(7, "reach collapse", detail={"status": "tripped"})
            state = rl.suppression_trip_state(7)
            assert rl.is_suppression_tripped(7) is True
        assert state["reason"] == "reach collapse"
        assert state["user_id"] == 7
        assert state["tripped_at"]
        assert json.loads(store["linkedin:suppression_trip:7"])["detail"]["status"] == "tripped"

    def test_the_record_has_no_ttl_so_it_never_self_resumes(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        client, _ = self._client()
        with patch(f"{_RL}._redis_client", return_value=client):
            rl.record_suppression_trip(7, "reach collapse")
        assert "ex" not in client.set.call_args.kwargs

    def test_a_human_can_clear_it(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        client, _ = self._client()
        with patch(f"{_RL}._redis_client", return_value=client):
            rl.record_suppression_trip(7, "reach collapse")
            assert rl.clear_suppression_trip(7) is True
            assert rl.suppression_trip_state(7) is None
            assert rl.is_suppression_tripped(7) is False

    def test_corrupt_json_still_reports_a_trip(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        client = MagicMock()
        client.get.return_value = b"not json"
        with patch(f"{_RL}._redis_client", return_value=client):
            state = rl.suppression_trip_state(7)
        assert state["reason"] == "not json"
        assert state["tripped_at"] is None

    def test_fails_open_without_redis(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        with patch(f"{_RL}._redis_client", return_value=None):
            assert rl.record_suppression_trip(7, "x") is False
            assert rl.clear_suppression_trip(7) is False
            assert rl.suppression_trip_state(7) is None
            assert rl.is_suppression_tripped(7) is False

    def test_redis_errors_never_raise(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        client = MagicMock()
        client.set.side_effect = RuntimeError("down")
        client.get.side_effect = RuntimeError("down")
        client.delete.side_effect = RuntimeError("down")
        with patch(f"{_RL}._redis_client", return_value=client):
            assert rl.record_suppression_trip(7, "x") is False
            assert rl.clear_suppression_trip(7) is False
            assert rl.suppression_trip_state(7) is None

    def test_pause_reason_is_attributable_to_the_tripwire(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        assert rl.suppression_pause_reason(7) == "suppression:7"
        assert rl.is_suppression_pause("suppression:7") is True
        assert rl.is_suppression_pause("maintenance") is False
        assert rl.is_suppression_pause(None) is False


class TestMeasurementExemption:
    """Read-only stat capture has to survive OUR pause — it produces the very readings the tripwire
    re-evaluates, so freezing it would leave the trend stuck at the collapsed numbers and make
    recovery permanently undetectable (and the user's "we keep collecting your analytics" untrue).
    """

    def _paused(self, reason):
        return patch(f"{_RL}.automation_pause_remaining", return_value=600 if reason else 0), \
            patch(f"{_RL}.automation_pause_reason", return_value=reason)

    def test_our_own_pause_does_not_stop_measurement(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        remaining, reason = self._paused("suppression:7")
        with remaining, reason:
            assert rl.is_automation_paused() is True
            assert rl.is_measurement_paused() is False

    def test_every_other_pause_still_stops_measurement(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        for standing in ("maintenance", "manual", "429 cooldown"):
            remaining, reason = self._paused(standing)
            with remaining, reason:
                assert rl.is_measurement_paused() is True, standing

    def test_nothing_paused_stops_nothing(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        remaining, reason = self._paused(None)
        with remaining, reason:
            assert rl.is_measurement_paused() is False


class TestCommentWindow:
    def test_the_comment_signal_reads_days_not_the_reach_baseline_window(self):
        # A month-old demotion episode #628 has already remediated must not trip a 90-day pause, and
        # at a week wide (#1136) a spike is diluted by up to six healthy days on a DAILY beat.
        assert sup.comment_history_days() == 3
        assert sup.comment_history_days() < sup.history_days()

    def test_the_comment_window_is_configurable(self, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_COMMENT_DAYS", "14")
        assert sup.comment_history_days() == 14
        monkeypatch.setenv("SUPPRESSION_COMMENT_DAYS", "0")
        assert sup.comment_history_days() == 1


class TestCommentMinSample:
    """Issue #1136: the readable-comment floor is DERIVED from the window, never its own knob."""

    def test_default_window_scales_the_weekly_floor_down(self):
        # ceil(10 * 3 / 7) = 5 — a 3-day window rarely collects the weekly 10, and a floor it cannot
        # reach is a signal that never fires rather than one that is merely strict.
        assert sup.comment_min_sample() == 5

    def test_it_tracks_the_window_instead_of_drifting(self, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_COMMENT_DAYS", "7")
        assert sup.comment_min_sample() == 10  # back to the weekly window, back to the weekly floor
        monkeypatch.setenv("SUPPRESSION_COMMENT_DAYS", "14")
        assert sup.comment_min_sample() == 20

    def test_it_tracks_the_weekly_floor_too(self, monkeypatch):
        monkeypatch.setenv("COMMENT_QUALITY_MIN_SAMPLE", "20")
        assert sup.comment_min_sample() == 9  # ceil(20 * 3 / 7)

    def test_it_never_falls_below_one(self, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_COMMENT_DAYS", "1")
        monkeypatch.setenv("COMMENT_QUALITY_MIN_SAMPLE", "1")
        assert sup.comment_min_sample() == 1
