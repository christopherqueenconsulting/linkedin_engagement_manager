"""Unit tests for the golden-hour timing + reporting core (issue #622 / G7).

Everything the amplifier and the second wave DECIDE lives here — when the sweeps fire, whether a
sweep that couldn't run is worth retrying, how late is late, and when the second wave lands — so
these tests pin the decisions without touching Celery, Selenium or the DB.
"""

import random

import pytest

from cqc_lem.utilities import golden_hour as g

pytestmark = pytest.mark.unit


@pytest.fixture
def pacing_off(monkeypatch):
    """Jitter off → exact, assertable countdowns."""
    monkeypatch.setenv("HUMAN_PACING_ENABLED", "false")


class TestSweepCountdowns:
    def test_three_sweeps_spread_across_the_hour(self, pacing_off):
        assert g.sweep_countdowns(3) == [20 * 60, 40 * 60, 60 * 60]

    def test_last_sweep_closes_the_window(self, pacing_off):
        for n in (1, 2, 4, 6):
            cds = g.sweep_countdowns(n)
            assert len(cds) == n
            assert cds == sorted(cds)
            assert cds[-1] == g.GOLDEN_HOUR_MINUTES * 60

    def test_non_positive_count_floors_to_one(self, pacing_off):
        assert g.sweep_countdowns(0) == [g.GOLDEN_HOUR_MINUTES * 60]

    def test_oversized_count_is_clamped(self, pacing_off):
        assert len(g.sweep_countdowns(1000)) == g.GOLDEN_HOUR_MAX_SWEEPS

    def test_jitter_is_forward_only_and_never_reorders(self, monkeypatch):
        monkeypatch.setenv("HUMAN_PACING_ENABLED", "true")
        monkeypatch.setenv("PACING_RESPONSIVE_JITTER_MAX_SECONDS", "100000")
        cds = g.sweep_countdowns(3)
        assert cds == sorted(cds) and len(set(cds)) == 3
        for i, cd in enumerate(cds, start=1):
            step = g.GOLDEN_HOUR_MINUTES * 60 / 3
            assert step * i <= cd <= step * i + step / 2


class TestReplySweeps:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("GOLDEN_HOUR_REPLY_SWEEPS", raising=False)
        assert g.reply_sweeps() == g.GOLDEN_HOUR_REPLY_SWEEPS

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("GOLDEN_HOUR_REPLY_SWEEPS", "5")
        assert g.reply_sweeps() == 5

    def test_garbage_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("GOLDEN_HOUR_REPLY_SWEEPS", "lots")
        assert g.reply_sweeps() == g.GOLDEN_HOUR_REPLY_SWEEPS


class TestLatencyMinutes:
    def test_normalizes_a_db_reading(self):
        assert g.latency_minutes(42) == 42.0
        assert g.latency_minutes(12.5) == 12.5

    def test_unknown_age_is_none_not_zero(self):
        # None is the whole point: a 0.0 here would render an unmeasured sweep as a perfect one.
        assert g.latency_minutes(None) is None
        assert g.latency_minutes("42") is None
        assert g.latency_minutes(True) is None

    def test_clock_skew_never_goes_negative(self):
        assert g.latency_minutes(-5) == 0.0


class TestWindows:
    def test_reply_sweep_window_is_ninety_minutes(self):
        assert g.within_golden_window(89.0) is True
        assert g.within_golden_window(91.0) is False

    def test_unknown_latency_is_never_on_time(self):
        assert g.within_golden_window(None) is False

    def test_second_wave_is_graded_against_its_own_window(self):
        """A healthy second wave lands ~7h out — grading it against 90 min would flag every one."""
        assert g.within_golden_window(420.0, g.PHASE_SECOND_WAVE) is True
        assert g.within_golden_window(420.0, g.PHASE_REPLY_SWEEP) is False
        assert g.within_golden_window(12 * 60.0, g.PHASE_SECOND_WAVE) is False

    def test_second_wave_window_tracks_the_env_bounds(self, monkeypatch):
        monkeypatch.setenv("SECOND_WAVE_MAX_HOURS", "10")
        assert g.phase_window_minutes(g.PHASE_SECOND_WAVE) == 10 * 60 + g.SECOND_WAVE_GRACE_MINUTES

    def test_should_report_drops_stale_posts_but_keeps_unknowns(self):
        assert g.should_report(30.0) is True
        assert g.should_report(g.GOLDEN_HOUR_REPORT_MAX_MINUTES + 1) is False
        assert g.should_report(None) is True


class TestSweepRetryCountdown:
    def test_retries_inside_the_window(self, pacing_off):
        assert g.sweep_retry_countdown(20.0, attempt=0) == g.GOLDEN_HOUR_RETRY_MINUTES * 60

    def test_no_retry_once_the_retry_would_land_late(self, pacing_off):
        # 88 min + a 7-min retry lands at 95 — past the window, so the browser slot isn't spent.
        assert g.sweep_retry_countdown(88.0, attempt=0) is None

    def test_attempts_are_bounded(self, pacing_off):
        assert g.sweep_retry_countdown(5.0, attempt=g.GOLDEN_HOUR_MAX_RETRIES) is None

    def test_unknown_latency_never_retries(self, pacing_off):
        assert g.sweep_retry_countdown(None, attempt=0) is None

    def test_jitter_stays_below_half_the_delay(self, monkeypatch):
        monkeypatch.setenv("HUMAN_PACING_ENABLED", "true")
        monkeypatch.setenv("PACING_RESPONSIVE_JITTER_MAX_SECONDS", "100000")
        base = g.GOLDEN_HOUR_RETRY_MINUTES * 60
        cd = g.sweep_retry_countdown(10.0, attempt=0)
        assert base <= cd <= base + base // 2


class TestGoldenHourReport:
    def test_carries_the_counts_and_the_verdict(self):
        report = g.golden_hour_report(7, 33.4, comments_found=4, replies_sent=2, sweep_slot=1)
        assert report == {
            "phase": g.PHASE_REPLY_SWEEP, "post_id": 7, "sweep_slot": 1, "status": "ok",
            "latency_minutes": 33.4, "within_window": True, "window_minutes": 90.0,
            "comments_found": 4, "replies_sent": 2,
        }

    def test_unknown_latency_reports_out_of_window(self):
        report = g.golden_hour_report(7, None)
        assert report["latency_minutes"] is None
        assert report["within_window"] is False

    def test_summary_names_the_window_and_the_numbers(self):
        summary = g.report_summary(g.golden_hour_report(7, 120.0, comments_found=1))
        assert "post 7" in summary and "OUTSIDE" in summary and "90-min" in summary

    def test_summary_survives_a_partial_report(self):
        assert "unknown latency" in g.report_summary({"phase": g.PHASE_REPLY_SWEEP})


class TestSecondWaveSchedule:
    def test_lands_between_six_and_eight_hours(self):
        rng = random.Random(7)
        for _ in range(50):
            hours = g.second_wave_countdown_seconds(rng) / 3600
            assert g.SECOND_WAVE_MIN_HOURS <= hours <= g.SECOND_WAVE_MAX_HOURS

    def test_is_not_a_fixed_offset(self):
        rng = random.Random(11)
        assert len({g.second_wave_countdown_seconds(rng) for _ in range(20)}) > 1

    def test_env_bounds_are_honoured(self, monkeypatch):
        monkeypatch.setenv("SECOND_WAVE_MIN_HOURS", "3")
        monkeypatch.setenv("SECOND_WAVE_MAX_HOURS", "4")
        hours = g.second_wave_countdown_seconds(random.Random(3)) / 3600
        assert 3 <= hours <= 4

    def test_inverted_bounds_are_swapped_not_ignored(self, monkeypatch):
        monkeypatch.setenv("SECOND_WAVE_MIN_HOURS", "9")
        monkeypatch.setenv("SECOND_WAVE_MAX_HOURS", "7")
        hours = g.second_wave_countdown_seconds(random.Random(5)) / 3600
        assert 7 <= hours <= 9

    def test_bounds_are_clamped_to_a_day(self, monkeypatch):
        monkeypatch.setenv("SECOND_WAVE_MIN_HOURS", "0")
        monkeypatch.setenv("SECOND_WAVE_MAX_HOURS", "900")
        assert 0 < g.second_wave_countdown_seconds(random.Random(1)) <= 24 * 3600

    def test_enabled_by_default_and_killable(self, monkeypatch):
        monkeypatch.delenv("SECOND_WAVE_COMMENT_ENABLED", raising=False)
        assert g.second_wave_enabled() is True
        monkeypatch.setenv("SECOND_WAVE_COMMENT_ENABLED", "false")
        assert g.second_wave_enabled() is False


class TestSelfCommentCap:
    def test_default_is_seed_plus_second_wave(self, monkeypatch):
        monkeypatch.delenv("SELF_COMMENT_MAX_PER_POST", raising=False)
        assert g.self_comment_cap() == 2

    def test_never_zero_and_never_thread_stuffing(self, monkeypatch):
        monkeypatch.setenv("SELF_COMMENT_MAX_PER_POST", "0")
        assert g.self_comment_cap() == 1
        monkeypatch.setenv("SELF_COMMENT_MAX_PER_POST", "99")
        assert g.self_comment_cap() == 5

    def test_garbage_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("SELF_COMMENT_MAX_PER_POST", "two")
        assert g.self_comment_cap() == g.SELF_COMMENT_MAX_PER_POST
