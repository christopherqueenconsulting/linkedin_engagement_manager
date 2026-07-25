"""Unit tests for P6 — engagement-bait guardrail + peak-time model + data-driven override."""

import datetime as dt
import pytest
from unittest.mock import patch

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
        from cqc_lem.utilities.utils import get_post_time, get_best_posting_time
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
