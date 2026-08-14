"""Unit tests for the live-sweep selector/parse fixes (groups + post-stats impressions)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# The post-stats sweep moved to `app.engagement.posting` (#1154); every patch below drives
# `auto_scrape_post_stats` / `_post_analytics_counts`, so this is the module whose globals they read.
_POST = "cqc_lem.app.engagement.posting"


class _Card:
    def __init__(self, text): self.text = text


class TestSocialCountsImpressions:
    def test_captures_impressions_and_counts(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        c = _post_social_counts(_Card("Post impressions\n153 impressions\n5 reactions\n2 comments"))
        assert c == {"reactions": 5, "comments": 2, "reposts": 0, "impressions": 153, "saves": 0}

    def test_quiet_post_has_only_impressions(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        c = _post_social_counts(_Card("Post impressions\n137 impressions"))
        assert c["impressions"] == 137 and c["reactions"] == 0 and c["comments"] == 0


class TestSocialCountsRepostsSavesParsing:
    def test_captures_reposts_and_saves(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        c = _post_social_counts(_Card(
            "1,234 impressions\n88 reactions\n12 comments\n7 reposts\n4 saves"))
        assert c == {"reactions": 88, "comments": 12, "reposts": 7,
                     "impressions": 1234, "saves": 4}

    def test_shares_alias_counts_as_reposts(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        c = _post_social_counts(_Card("40 reactions\n3 shares"))
        assert c["reposts"] == 3

    def test_abbreviated_magnitudes(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        c = _post_social_counts(_Card(
            "12.3K impressions\n1.2K reactions\n2M comments\n5 reposts"))
        assert c["impressions"] == 12300
        assert c["reactions"] == 1200
        assert c["comments"] == 2_000_000
        assert c["reposts"] == 5

    def test_missing_signals_are_zero_not_error(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        c = _post_social_counts(_Card("Be the first to comment"))
        assert c == {"reactions": 0, "comments": 0, "reposts": 0,
                     "impressions": 0, "saves": 0}


class TestLiveAnalyticsPageLayout:
    """Fixture text taken verbatim from a real /analytics/post-summary capture (owner grab on
    2026-07-23, PR #427 review): labels and values land on separate lines, and the Discovery block
    stacks value-first while the Engagement block stacks label-first.
    """

    LIVE = ("Discovery\n72\nImpressions\nIn-network (followers and connections)\n21%\n"
            "Out-of-network\n79%\n45\nMembers reached\nVideo performance\n16\nVideo views\n"
            "Watch time\n3m 24s\nProfile activity\n0\nProfile viewers from this post\n"
            "Engagement\n1\nSocial engagements\nReactions\n0\nComments\n1\nReposts\n0\n"
            "Saves\n0\nSends on LinkedIn\n0")

    def test_parses_live_analytics_capture(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        assert _post_social_counts(_Card(self.LIVE)) == {
            "reactions": 0, "comments": 1, "reposts": 0, "impressions": 72, "saves": 0}

    def test_stacked_reposts_and_saves_are_captured(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        c = _post_social_counts(_Card("Reactions\n11\nComments\n4\nReposts\n6\nSaves\n9"))
        assert c["reposts"] == 6 and c["saves"] == 9 and c["reactions"] == 11 and c["comments"] == 4

    def test_a_rows_value_is_not_read_as_the_next_rows(self):
        # "Comments\n1\nReposts\n0" must not yield reposts=1 — the old \s+ regex did exactly that.
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        assert _post_social_counts(_Card("Comments\n1\nReposts\n0"))["reposts"] == 0

    def test_post_body_prose_is_not_mistaken_for_a_count(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        c = _post_social_counts(_Card("Save this checklist for later.\nIt saves 12 hours a week."))
        assert c["saves"] == 0

    def test_stacked_abbreviated_magnitudes(self):
        from cqc_lem.utilities.linkedin.cards import _post_social_counts
        c = _post_social_counts(_Card("2.4K\nImpressions\nReposts\n1.5K\nSaves\n3M"))
        assert c["impressions"] == 2400 and c["reposts"] == 1500 and c["saves"] == 3_000_000


class TestParseCount:
    def test_variants(self):
        from cqc_lem.utilities.linkedin.cards import _parse_count
        assert _parse_count("1,234") == 1234
        assert _parse_count("1.2K") == 1200
        assert _parse_count("3M") == 3_000_000
        assert _parse_count("2B") == 2_000_000_000
        assert _parse_count("") == 0
        assert _parse_count("n/a") == 0
        assert _parse_count(None) == 0


class TestGroupFeedSelector:
    def test_selector_covers_classic_group_feed(self):
        from cqc_lem.utilities.linkedin.cards import _FEED_POST_TEXT_SEL
        # SDUI home feed AND classic group feed containers both covered
        assert "expandable-text-box" in _FEED_POST_TEXT_SEL
        assert "feed-shared-update-v2" in _FEED_POST_TEXT_SEL


class TestScrapeRecordsImpressions:
    def test_impressions_reposts_saves_passed_to_record(self):
        counts = {"reactions": 5, "comments": 2, "reposts": 7, "impressions": 153, "saves": 4}
        with patch(f"{_POST}.time.sleep"), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[9]), \
             patch(f"{_POST}.get_uncaptured_posted_post_ids", return_value=[]), \
             patch(f"{_POST}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_POST}._post_social_counts", return_value=counts), \
             patch(f"{_POST}.get_shipped_variant_keys", return_value={}), \
             patch(f"{_POST}.record_post_stats") as rec, patch(f"{_POST}.quit_gracefully"):
            from cqc_lem.app.engagement.posting import auto_scrape_post_stats
            auto_scrape_post_stats.run(user_id=1)
        # record_post_stats(user_id, pid, reactions, comments, reposts=7, impressions=153, saves=4)
        assert rec.call_args.kwargs.get("impressions") == 153
        assert rec.call_args.kwargs.get("reposts") == 7
        assert rec.call_args.kwargs.get("saves") == 4

    def test_outcome_event_carries_the_variant_the_post_shipped(self):
        """Issue #652: the shipped A/B variant is looked up ONCE per sweep, not per post inside the
        Selenium loop, and rides onto each post's outcome event as its experiment arm.
        """
        counts = {"reactions": 5, "comments": 2, "reposts": 0, "impressions": 100, "saves": 0}
        with patch(f"{_POST}.time.sleep"), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[9, 10]), \
             patch(f"{_POST}.get_uncaptured_posted_post_ids", return_value=[]), \
             patch(f"{_POST}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_POST}._post_social_counts", return_value=counts), \
             patch(f"{_POST}._post_analytics_counts", return_value={}), \
             patch(f"{_POST}.get_shipped_variant_keys", return_value={9: "flux|gen4|1:1"}) as keys, \
             patch(f"{_POST}.record_post_stats"), \
             patch(f"{_POST}.track_post_outcome") as outcome, patch(f"{_POST}.quit_gracefully"):
            from cqc_lem.app.engagement.posting import auto_scrape_post_stats
            auto_scrape_post_stats.run(user_id=1)
        keys.assert_called_once_with(1)
        variants = {call.kwargs["post_id"]: call.kwargs["variant_key"]
                    for call in outcome.call_args_list}
        # A post with no recorded variant is None, never a made-up arm.
        assert variants == {9: "flux|gen4|1:1", 10: None}

    def test_outcome_event_carries_the_format_the_post_shipped(self):
        """The format the post shipped as rides onto its outcome event (issue #1513).

        Without it, "do carousels out-reach text posts?" is unanswerable from telemetry — `saves`
        was already on the event, the split was not.
        """
        counts = {"reactions": 5, "comments": 2, "reposts": 0, "impressions": 100, "saves": 4}
        with patch(f"{_POST}.time.sleep"), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[9, 10]), \
             patch(f"{_POST}.get_uncaptured_posted_post_ids", return_value=[]), \
             patch(f"{_POST}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_POST}._post_social_counts", return_value=counts), \
             patch(f"{_POST}._post_analytics_counts", return_value={}), \
             patch(f"{_POST}.get_shipped_variant_keys", return_value={}), \
             patch(f"{_POST}.get_post_types_for_user", return_value={9: "carousel"}) as types, \
             patch(f"{_POST}.record_post_stats"), \
             patch(f"{_POST}.track_post_outcome") as outcome, patch(f"{_POST}.quit_gracefully"):
            from cqc_lem.app.engagement.posting import auto_scrape_post_stats
            auto_scrape_post_stats.run(user_id=1)
        # ONE query for the sweep, never one per post inside the Selenium loop.
        types.assert_called_once_with(1)
        formats = {call.kwargs["post_id"]: call.kwargs["post_type"]
                   for call in outcome.call_args_list}
        # An unreadable format stays None so those rows drop out of a format breakdown instead of
        # piling onto one.
        assert formats == {9: "carousel", 10: None}

    def test_analytics_page_signals_merge_over_detail_page(self):
        detail = {"reactions": 5, "comments": 2, "reposts": 0, "impressions": 0, "saves": 0}
        analytics = {"reposts": 6, "impressions": 72, "saves": 9}
        with patch(f"{_POST}.time.sleep"), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[9]), \
             patch(f"{_POST}.get_uncaptured_posted_post_ids", return_value=[]), \
             patch(f"{_POST}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_POST}._post_social_counts", return_value=detail), \
             patch(f"{_POST}._post_analytics_counts", return_value=analytics), \
             patch(f"{_POST}.get_shipped_variant_keys", return_value={}), \
             patch(f"{_POST}.record_post_stats") as rec, patch(f"{_POST}.quit_gracefully"):
            from cqc_lem.app.engagement.posting import auto_scrape_post_stats
            auto_scrape_post_stats.run(user_id=1)
        assert rec.call_args.kwargs == {"reposts": 6, "impressions": 72, "saves": 9}
        assert rec.call_args.args[2:] == (5, 2)


class TestScrapeBackfillsNeverCapturedPosts:
    """Issue #809: the sweep walks a short window but the dashboard reads 90 days, so a post whose
    capture was missed while it was fresh stayed unmeasurable forever — and the analytics rendered a
    shrinking subset of the account.
    """

    def _run(self, recent, uncaptured, env=None, counts=None):
        with patch(f"{_POST}.time.sleep"), \
             patch.dict("os.environ", env or {}, clear=False), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=list(recent)), \
             patch(f"{_POST}.get_uncaptured_posted_post_ids", return_value=list(uncaptured)) as back, \
             patch(f"{_POST}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_POST}._post_social_counts",
                   return_value={"reactions": 1, "comments": 0} if counts is None else counts), \
             patch(f"{_POST}._post_analytics_counts", return_value={}), \
             patch(f"{_POST}.get_shipped_variant_keys", return_value={}), \
             patch(f"{_POST}.track_post_outcome"), \
             patch(f"{_POST}.record_post_stats") as rec, patch(f"{_POST}.quit_gracefully"):
            from cqc_lem.app.engagement.posting import auto_scrape_post_stats
            result = auto_scrape_post_stats.run(user_id=1)
        return result, rec, back

    def test_older_uncaptured_posts_are_appended_to_the_sweep(self):
        result, rec, back = self._run(recent=[9], uncaptured=[4, 2])
        assert [c.args[1] for c in rec.call_args_list] == [9, 4, 2]
        assert back.call_args.kwargs == {"days": 90, "limit": 5}
        assert "3" in result

    def test_a_post_already_in_the_recent_sweep_is_not_scraped_twice(self):
        _, rec, _ = self._run(recent=[9, 10], uncaptured=[10, 4])
        assert [c.args[1] for c in rec.call_args_list] == [9, 10, 4]

    def test_backfill_only_posts_still_open_a_session(self):
        """An account whose posts all aged out of the short window still gets measured."""
        _, rec, _ = self._run(recent=[], uncaptured=[4])
        assert [c.args[1] for c in rec.call_args_list] == [4]

    def test_window_and_cap_are_env_tunable(self):
        _, _, back = self._run(recent=[9], uncaptured=[],
                               env={"POST_STATS_BACKFILL_DAYS": "30", "POST_STATS_BACKFILL_MAX": "2"})
        assert back.call_args.kwargs == {"days": 30, "limit": 2}

    def test_cap_of_zero_disables_the_backfill(self):
        _, _, back = self._run(recent=[9], uncaptured=[4],
                               env={"POST_STATS_BACKFILL_MAX": "0"})
        back.assert_not_called()

    def test_unparseable_env_falls_back_to_defaults(self):
        _, _, back = self._run(recent=[9], uncaptured=[],
                               env={"POST_STATS_BACKFILL_DAYS": "ninety", "POST_STATS_BACKFILL_MAX": ""})
        assert back.call_args.kwargs == {"days": 90, "limit": 5}

    def test_an_unreadable_page_records_nothing_instead_of_a_zero_row(self):
        """A page that never rendered (auth wall, 429, dead permalink) parses to NO signals at all —
        writing the zeros would publish a fabricated row, and for a backfilled post it is permanent:
        the stat row retires it from the never-captured queue on a single bad read.
        """
        result, rec, _ = self._run(recent=[], uncaptured=[4], counts={})
        rec.assert_not_called()
        assert "0" in result

    def test_a_genuinely_zero_engagement_post_is_still_recorded(self):
        """A readable page always yields the full zero-filled dict, so 'no engagement' must keep
        being measured — only 'nothing read' is skipped.
        """
        _, rec, _ = self._run(recent=[9], uncaptured=[],
                              counts={"reactions": 0, "comments": 0, "reposts": 0,
                                      "impressions": 0, "saves": 0})
        assert [c.args[1] for c in rec.call_args_list] == [9]


class TestPostAnalyticsCounts:
    def _driver(self, current_url):
        d = MagicMock()
        d.current_url = current_url
        return d

    def test_navigates_to_analytics_for_resolved_activity_urn(self):
        from cqc_lem.app.engagement.posting import _post_analytics_counts
        # The logged permalink holds the share URN; the detail page redirects to the activity URN.
        driver = self._driver("https://www.linkedin.com/feed/update/urn:li:activity:7484970423563022336/")
        with patch(f"{_POST}.time.sleep"), \
             patch(f"{_POST}._post_social_counts", return_value={"reactions": 0, "reposts": 6, "saves": 9}):
            counts = _post_analytics_counts(driver, "https://www.linkedin.com/feed/update/urn:li:share:123/")
        assert driver.get.call_args.args[0] == (
            "https://www.linkedin.com/analytics/post-summary/urn:li:activity:7484970423563022336/")
        # Zero-valued signals are dropped so they can't overwrite a detail-page count on merge.
        assert counts == {"reposts": 6, "saves": 9}

    def test_falls_back_to_logged_url_urn(self):
        from cqc_lem.app.engagement.posting import _post_analytics_counts
        with patch(f"{_POST}.time.sleep"), patch(f"{_POST}._post_social_counts", return_value={"saves": 1}):
            driver = self._driver("https://www.linkedin.com/checkpoint/challenge/")
            _post_analytics_counts(driver, "https://www.linkedin.com/feed/update/urn:li:share:99/")
        assert "urn:li:share:99" in driver.get.call_args.args[0]

    def test_no_urn_means_no_navigation(self):
        from cqc_lem.app.engagement.posting import _post_analytics_counts
        driver = self._driver("https://www.linkedin.com/feed/")
        assert _post_analytics_counts(driver, "https://example.com/nope") == {}
        driver.get.assert_not_called()

    def test_page_failure_is_swallowed(self):
        from cqc_lem.app.engagement.posting import _post_analytics_counts
        driver = self._driver("https://www.linkedin.com/feed/update/urn:li:activity:1/")
        driver.find_element.side_effect = Exception("no main")
        with patch(f"{_POST}.time.sleep"):
            assert _post_analytics_counts(driver, "") == {}
