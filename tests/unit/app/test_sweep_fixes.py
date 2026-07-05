"""Unit tests for the live-sweep selector/parse fixes (groups + post-stats impressions)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


class _Card:
    def __init__(self, text): self.text = text


class TestSocialCountsImpressions:
    def test_captures_impressions_and_counts(self):
        from cqc_lem.app.run_automation import _post_social_counts
        c = _post_social_counts(_Card("Post impressions\n153 impressions\n5 reactions\n2 comments"))
        assert c == {"reactions": 5, "comments": 2, "impressions": 153}

    def test_quiet_post_has_only_impressions(self):
        from cqc_lem.app.run_automation import _post_social_counts
        c = _post_social_counts(_Card("Post impressions\n137 impressions"))
        assert c["impressions"] == 137 and c["reactions"] == 0 and c["comments"] == 0


class TestGroupFeedSelector:
    def test_selector_covers_classic_group_feed(self):
        from cqc_lem.app.run_automation import _FEED_POST_TEXT_SEL
        # SDUI home feed AND classic group feed containers both covered
        assert "expandable-text-box" in _FEED_POST_TEXT_SEL
        assert "feed-shared-update-v2" in _FEED_POST_TEXT_SEL


class TestScrapeRecordsImpressions:
    def test_impressions_passed_to_record(self):
        with patch(f"{_RA}.time.sleep"), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[9]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_RA}._post_social_counts", return_value={"reactions": 5, "comments": 2, "impressions": 153}), \
             patch(f"{_RA}.record_post_stats") as rec, patch(f"{_RA}.quit_gracefully"):
            from cqc_lem.app.run_automation import auto_scrape_post_stats
            auto_scrape_post_stats.run(user_id=1)
        # record_post_stats(user_id, pid, reactions, comments, impressions=153)
        assert rec.call_args.kwargs.get("impressions") == 153
