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
        assert c == {"reactions": 5, "comments": 2, "reposts": 0, "impressions": 153, "saves": 0}

    def test_quiet_post_has_only_impressions(self):
        from cqc_lem.app.run_automation import _post_social_counts
        c = _post_social_counts(_Card("Post impressions\n137 impressions"))
        assert c["impressions"] == 137 and c["reactions"] == 0 and c["comments"] == 0


class TestSocialCountsRepostsSavesParsing:
    def test_captures_reposts_and_saves(self):
        from cqc_lem.app.run_automation import _post_social_counts
        c = _post_social_counts(_Card(
            "1,234 impressions\n88 reactions\n12 comments\n7 reposts\n4 saves"))
        assert c == {"reactions": 88, "comments": 12, "reposts": 7,
                     "impressions": 1234, "saves": 4}

    def test_shares_alias_counts_as_reposts(self):
        from cqc_lem.app.run_automation import _post_social_counts
        c = _post_social_counts(_Card("40 reactions\n3 shares"))
        assert c["reposts"] == 3

    def test_abbreviated_magnitudes(self):
        from cqc_lem.app.run_automation import _post_social_counts
        c = _post_social_counts(_Card(
            "12.3K impressions\n1.2K reactions\n2M comments\n5 reposts"))
        assert c["impressions"] == 12300
        assert c["reactions"] == 1200
        assert c["comments"] == 2_000_000
        assert c["reposts"] == 5

    def test_missing_signals_are_zero_not_error(self):
        from cqc_lem.app.run_automation import _post_social_counts
        c = _post_social_counts(_Card("Be the first to comment"))
        assert c == {"reactions": 0, "comments": 0, "reposts": 0,
                     "impressions": 0, "saves": 0}


class TestParseCount:
    def test_variants(self):
        from cqc_lem.app.run_automation import _parse_count
        assert _parse_count("1,234") == 1234
        assert _parse_count("1.2K") == 1200
        assert _parse_count("3M") == 3_000_000
        assert _parse_count("2B") == 2_000_000_000
        assert _parse_count("") == 0
        assert _parse_count("n/a") == 0
        assert _parse_count(None) == 0


class TestGroupFeedSelector:
    def test_selector_covers_classic_group_feed(self):
        from cqc_lem.app.run_automation import _FEED_POST_TEXT_SEL
        # SDUI home feed AND classic group feed containers both covered
        assert "expandable-text-box" in _FEED_POST_TEXT_SEL
        assert "feed-shared-update-v2" in _FEED_POST_TEXT_SEL


class TestScrapeRecordsImpressions:
    def test_impressions_reposts_saves_passed_to_record(self):
        counts = {"reactions": 5, "comments": 2, "reposts": 7, "impressions": 153, "saves": 4}
        with patch(f"{_RA}.time.sleep"), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[9]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_RA}._post_social_counts", return_value=counts), \
             patch(f"{_RA}.record_post_stats") as rec, patch(f"{_RA}.quit_gracefully"):
            from cqc_lem.app.run_automation import auto_scrape_post_stats
            auto_scrape_post_stats.run(user_id=1)
        # record_post_stats(user_id, pid, reactions, comments, reposts=7, impressions=153, saves=4)
        assert rec.call_args.kwargs.get("impressions") == 153
        assert rec.call_args.kwargs.get("reposts") == 7
        assert rec.call_args.kwargs.get("saves") == 4
