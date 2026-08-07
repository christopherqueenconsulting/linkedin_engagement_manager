"""Unit tests for P6 — engagement-bait guardrail + peak-time model + data-driven override."""

import datetime as dt
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestStripEngagementBait:
    def test_removes_bait_lines(self):
        from cqc_lem.utilities.linkedin_formatter import strip_engagement_bait
        text = "Great insight here.\nComment YES if you agree!\nTag a friend who needs this.\nReal value line."
        out = strip_engagement_bait(text)
        assert "Great insight here." in out and "Real value line." in out
        assert "YES" not in out and "Tag a friend" not in out

    def test_keeps_lead_magnet_cta(self):
        from cqc_lem.utilities.linkedin_formatter import strip_engagement_bait
        text = "Here's my framework.\nComment GUIDE and I'll send you the full checklist."
        out = strip_engagement_bait(text)
        assert "Comment GUIDE" in out  # lead-magnet CTA preserved

    def test_empty_passthrough(self):
        from cqc_lem.utilities.linkedin_formatter import strip_engagement_bait
        assert strip_engagement_bait("") == ""


class TestPeakTimeModel:
    def test_afternoon_shift(self):
        from cqc_lem.utilities.utils import get_best_posting_times
        bt = get_best_posting_times()
        assert bt[2].hour == 16          # Wednesday peak = 4pm
        assert all(9 <= bt[d].hour <= 20 for d in (0, 1, 2, 3))  # weekday afternoons


class TestGetPostTime:
    def test_falls_back_to_default_without_user(self):
        from cqc_lem.utilities.utils import get_best_posting_time, get_post_time
        d = dt.date(2026, 7, 8)  # Wednesday
        assert get_post_time(d) == get_best_posting_time(d)

    def test_uses_data_driven_hour(self):
        from cqc_lem.utilities import utils
        d = dt.date(2026, 7, 8)  # Wednesday (weekday 2)
        recs = [{"weekday_num": 2, "hour": 19, "avg_engagement": 50, "sample": 4}]
        with patch("cqc_lem.utilities.db.get_post_engagement_rows", return_value=[("x",)] * 4), \
             patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
             patch("cqc_lem.utilities.post_stats.recommend_post_times", return_value=recs):
            assert utils.get_post_time(d, user_id=1).hour == 19


class TestWakingHourBounds:
    """Sane posting hours (issue #621 / G6) — no 03:00 or 05:00 local slots."""

    def test_default_model_is_already_inside_the_window(self):
        from cqc_lem.utilities.utils import POST_HOUR_MAX, POST_HOUR_MIN, get_best_posting_times
        assert all(POST_HOUR_MIN <= t.hour <= POST_HOUR_MAX
                   for t in get_best_posting_times().values())

    def test_clamp_pulls_overnight_hours_into_the_window(self):
        from cqc_lem.utilities.utils import POST_HOUR_MAX, POST_HOUR_MIN, clamp_to_waking_hours
        assert clamp_to_waking_hours(dt.time(3, 30)) == dt.time(POST_HOUR_MIN, 30)
        assert clamp_to_waking_hours(dt.time(5, 0)) == dt.time(POST_HOUR_MIN, 0)
        assert clamp_to_waking_hours(dt.time(23, 15)) == dt.time(POST_HOUR_MAX, 15)
        assert clamp_to_waking_hours(dt.time(16, 0)) == dt.time(16, 0)

    def test_learned_overnight_hour_is_clamped(self):
        from cqc_lem.utilities import utils
        d = dt.date(2026, 7, 8)  # Wednesday
        recs = [{"weekday_num": 2, "hour": 3, "avg_engagement": 50, "sample": 4}]
        with patch("cqc_lem.utilities.db.get_post_engagement_rows", return_value=[("x",)] * 4), \
             patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
             patch("cqc_lem.utilities.post_stats.recommend_post_times", return_value=recs):
            assert utils.get_post_time(d, user_id=1).hour == utils.POST_HOUR_MIN

    def test_operator_override_outside_the_window_is_clamped(self, monkeypatch):
        from cqc_lem.utilities import utils
        monkeypatch.setenv("DEFAULT_POSTING_HOURS", "3,3,3,3,3,3,3")
        assert utils.get_post_time(dt.date(2026, 7, 8)).hour == utils.POST_HOUR_MIN


class TestScheduleJitter:
    def test_offset_is_always_15_to_30_minutes(self):
        from cqc_lem.utilities.utils import (
            SCHEDULE_JITTER_MAX_MINUTES,
            SCHEDULE_JITTER_MIN_MINUTES,
            apply_schedule_jitter,
        )
        base = dt.time(16, 0)
        base_minutes = base.hour * 60 + base.minute
        offsets = set()
        for _ in range(200):
            jittered = apply_schedule_jitter(base)
            offset = jittered.hour * 60 + jittered.minute - base_minutes
            assert SCHEDULE_JITTER_MIN_MINUTES <= abs(offset) <= SCHEDULE_JITTER_MAX_MINUTES
            offsets.add(offset)
        # Both directions get used, and the result is not one machine-exact time.
        assert any(o < 0 for o in offsets) and any(o > 0 for o in offsets)
        assert len(offsets) > 4

    def test_jitter_never_leaves_the_waking_window(self):
        from cqc_lem.utilities.utils import POST_HOUR_MAX, POST_HOUR_MIN, apply_schedule_jitter
        for base in (dt.time(POST_HOUR_MIN, 0), dt.time(POST_HOUR_MAX, 59), dt.time(12, 30)):
            for _ in range(100):
                jittered = apply_schedule_jitter(base)
                assert POST_HOUR_MIN <= jittered.hour <= POST_HOUR_MAX

    def test_accepts_an_injected_rng_for_determinism(self):
        import random

        from cqc_lem.utilities.utils import apply_schedule_jitter
        first = apply_schedule_jitter(dt.time(16, 0), rng=random.Random(7))
        second = apply_schedule_jitter(dt.time(16, 0), rng=random.Random(7))
        assert first == second
