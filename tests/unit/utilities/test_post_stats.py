"""Unit tests for post-time recommendation logic."""

import datetime as dt
import pytest

pytestmark = pytest.mark.unit


class TestEngagementScore:
    def test_weights_comments_and_reposts(self):
        from cqc_lem.utilities.post_stats import engagement_score
        assert engagement_score(10, 0, 0) == 10
        assert engagement_score(0, 5, 0) == 10          # comments ×2
        assert engagement_score(0, 0, 3) == 6           # reposts ×2


class TestRecommendPostTimes:
    def _row(self, weekday, hour, reactions, comments):
        # 2026-07-06 is a Monday; add days to hit the desired weekday
        base = dt.datetime(2026, 7, 6, hour, 0)  # Monday
        return (base + dt.timedelta(days=weekday), reactions, comments, 0)

    def test_empty_below_min_posts(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        assert recommend_post_times([self._row(0, 9, 5, 1)], min_posts=3) == []

    def test_ranks_best_bucket_first(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        rows = [
            self._row(2, 16, 50, 10),   # Wed 16:00 — high
            self._row(2, 16, 40, 8),
            self._row(0, 9, 5, 0),      # Mon 09:00 — low
            self._row(0, 9, 6, 1),
        ]
        recs = recommend_post_times(rows, top_n=2, min_posts=3)
        assert recs[0]["weekday"] == "Wednesday" and recs[0]["hour"] == 16
        assert recs[0]["avg_engagement"] > recs[1]["avg_engagement"]

    def test_none_scheduled_time_skipped(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        rows = [(None, 100, 100, 0)] + [self._row(3, 17, 20, 5) for _ in range(3)]
        recs = recommend_post_times(rows, min_posts=3)
        assert recs and recs[0]["weekday"] == "Thursday"


class TestScrapeStatsTask:
    def test_records_each_post(self):
        from unittest.mock import MagicMock, patch
        _RA = "cqc_lem.app.run_automation"
        with patch(f"{_RA}.time.sleep"), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[9, 10]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_RA}._post_social_counts", return_value={"reactions": 12, "comments": 3}), \
             patch(f"{_RA}.record_post_stats") as rec, patch(f"{_RA}.quit_gracefully"):
            from cqc_lem.app.run_automation import auto_scrape_post_stats
            result = auto_scrape_post_stats.run(user_id=1)
        assert rec.call_count == 2 and "Scraped stats for 2" in result
