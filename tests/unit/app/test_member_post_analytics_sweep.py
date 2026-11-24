"""Unit tests for the Community API lane in the post-stats sweep."""

from unittest.mock import Mock, patch

import pytest

from cqc_lem.app.engagement import posting

_POST = "cqc_lem.app.engagement.posting"

pytestmark = pytest.mark.unit


class TestMemberAnalyticsSweep:
    def test_api_only_capture_skips_browser_session(self):
        counts = {"impressions": 30, "reactions": 2, "comments": 1, "reposts": 0, "saves": 0}
        with patch(f"{_POST}.skip_browser_lane", return_value=False), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[41]), \
             patch(f"{_POST}._post_stats_backfill_bounds", return_value=(0, 0)), \
             patch(f"{_POST}.get_shipped_variant_keys", return_value={}), \
             patch(f"{_POST}.get_post_types_for_user", return_value={}), \
             patch(f"{_POST}.member_post_analytics_enabled_for_user", return_value=True), \
             patch(f"{_POST}.member_post_analytics_max_posts_per_sweep", return_value=2), \
             patch(f"{_POST}.get_member_post_analytics_access_token", return_value="cma-token"), \
             patch(f"{_POST}.get_post_url_from_log_for_user",
                   return_value="https://linkedin.test/urn:li:share:41"), \
             patch(f"{_POST}.fetch_member_post_analytics", return_value=(counts, None)) as fetch, \
             patch(f"{_POST}._record_member_api_post_stats", return_value=True), \
             patch(f"{_POST}.get_current_profile") as browser:
            result = posting.auto_scrape_post_stats.run(user_id=1)

        assert "through Community API" in result
        fetch.assert_called_once_with(
            "https://linkedin.test/urn:li:share:41", "cma-token", "202606")
        browser.assert_not_called()

    def test_api_pass_failure_falls_back_to_browser_for_every_post(self):
        scraper = Mock(return_value=True)
        with patch(f"{_POST}.skip_browser_lane", return_value=False), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[41, 42]), \
             patch(f"{_POST}._post_stats_backfill_bounds", return_value=(0, 0)), \
             patch(f"{_POST}.get_shipped_variant_keys", return_value={}), \
             patch(f"{_POST}.get_post_types_for_user", return_value={}), \
             patch(f"{_POST}.member_post_analytics_enabled_for_user", return_value=True), \
             patch(f"{_POST}.member_post_analytics_max_posts_per_sweep", return_value=2), \
             patch(f"{_POST}.get_member_post_analytics_access_token", return_value="cma-token"), \
             patch(f"{_POST}.get_post_url_from_log_for_user", side_effect=[
                 "https://linkedin.test/urn:li:share:41",
                 "https://linkedin.test/urn:li:share:41",
                 "https://linkedin.test/urn:li:share:42",
             ]), \
             patch(f"{_POST}._record_member_api_post_stats") as record_api, \
             patch(f"{_POST}.fetch_member_post_analytics", return_value=({}, "http_403")) as fetch, \
             patch(f"{_POST}.log_member_post_analytics_failure"), \
             patch(f"{_POST}.get_current_profile", return_value=(object(), None, None, None)), \
             patch(f"{_POST}._post_stats_recycle_every", return_value=0), \
             patch(f"{_POST}._scrape_one_post_stats", scraper), \
             patch(f"{_POST}.quit_gracefully"):
            result = posting.auto_scrape_post_stats.run(user_id=1)

        assert "Scraped stats for 2 post(s)" in result
        fetch.assert_called_once()
        assert scraper.call_count == 2
        record_api.assert_not_called()
        for call in scraper.call_args_list:
            assert "api_access_token" not in call.kwargs

    def test_browser_pass_skips_posts_the_api_covered(self):
        scraper = Mock(return_value=True)
        counts = {"impressions": 30, "reactions": 2, "comments": 1, "reposts": 0, "saves": 0}
        with patch(f"{_POST}.skip_browser_lane", return_value=False), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[41, 42]), \
             patch(f"{_POST}._post_stats_backfill_bounds", return_value=(0, 0)), \
             patch(f"{_POST}.get_shipped_variant_keys", return_value={}), \
             patch(f"{_POST}.get_post_types_for_user", return_value={}), \
             patch(f"{_POST}.member_post_analytics_enabled_for_user", return_value=True), \
             patch(f"{_POST}.member_post_analytics_max_posts_per_sweep", return_value=1), \
             patch(f"{_POST}.get_member_post_analytics_access_token", return_value="cma-token"), \
             patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://linkedin.test/p"), \
             patch(f"{_POST}.fetch_member_post_analytics", return_value=(counts, None)), \
             patch(f"{_POST}._record_member_api_post_stats", return_value=True), \
             patch(f"{_POST}.get_current_profile", return_value=(object(), None, None, None)), \
             patch(f"{_POST}._post_stats_recycle_every", return_value=0), \
             patch(f"{_POST}._scrape_one_post_stats", scraper), \
             patch(f"{_POST}.quit_gracefully"):
            posting.auto_scrape_post_stats.run(user_id=1)

        assert scraper.call_count == 1
        assert scraper.call_args.args[3] == "https://linkedin.test/p"
        assert scraper.call_args.args[2] == 42


class TestApiRowRecording:
    """An API row is its own source-tagged row and never reaches PostHog (#2702)."""

    _FULL = {"impressions": 30, "reactions": 2, "comments": 1, "reposts": 0, "saves": 4}

    def test_no_merge_with_prior_scrape_and_no_posthog(self):
        with patch(f"{_POST}.own_comment_count_or_none", return_value=1), \
             patch(f"{_POST}.record_post_stats", return_value=True) as rec, \
             patch(f"{_POST}.track_post_outcome") as track:
            assert posting._record_member_api_post_stats(1, 41, "u", {}, {}, self._FULL)
        assert rec.call_args.kwargs["source"] == "api"
        assert rec.call_args.args[2] == 2  # the API value, not max(api, scrape)
        assert rec.call_args.kwargs["impressions"] == 30
        track.assert_not_called()

    def test_partial_api_totals_are_rejected(self):
        with patch(f"{_POST}.record_post_stats") as rec:
            assert not posting._record_member_api_post_stats(
                1, 41, "u", {}, {}, {**self._FULL, "saves": None})
        rec.assert_not_called()


class TestPostHogSkipsApiValues:
    def test_post_outcome_api_source_emits_nothing(self):
        from cqc_lem.utilities import observability
        with patch.object(observability, "_emit") as emit:
            observability.track_post_outcome(1, 2, 3, source="api")
            emit.assert_not_called()
            observability.track_post_outcome(1, 2, 3)
            emit.assert_called_once()

    def test_audience_snapshot_drops_api_counts(self):
        from cqc_lem.utilities import observability
        with patch.object(observability, "_emit") as emit:
            observability.track_audience_snapshot(
                1, follower_count=10, connection_count=20, follower_source="api")
        props = emit.call_args.args[1]
        assert props["follower_count"] is None
        assert props["connection_count"] == 20


class TestCapDefault:
    def test_default_is_twenty(self):
        import importlib
        import os

        from cqc_lem.utilities import env_constants
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LI_MEMBER_POST_ANALYTICS_MAX_POSTS_PER_SWEEP", None)
            try:
                importlib.reload(env_constants)
                assert env_constants.LI_MEMBER_POST_ANALYTICS_MAX_POSTS_PER_SWEEP == 20
            finally:
                importlib.reload(env_constants)


def _tunnel_error():
    from selenium.common.exceptions import WebDriverException
    return WebDriverException("unknown error: net::ERR_TUNNEL_CONNECTION_FAILED")


def _sweep_patches(profile, scraper=None):
    """Patches for a browser-only sweep over one post (#2605)."""
    p = [patch(f"{_POST}.skip_browser_lane", return_value=False),
         patch(f"{_POST}.get_recent_posted_post_ids", return_value=[41]),
         patch(f"{_POST}._post_stats_backfill_bounds", return_value=(0, 0)),
         patch(f"{_POST}.get_shipped_variant_keys", return_value={}),
         patch(f"{_POST}.get_post_types_for_user", return_value={}),
         patch(f"{_POST}.member_post_analytics_enabled_for_user", return_value=False),
         patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://linkedin.test/p/41"),
         patch(f"{_POST}._post_stats_recycle_every", return_value=0),
         patch(f"{_POST}.quit_gracefully"),
         patch(f"{_POST}.get_current_profile", **profile)]
    if scraper is not None:
        p.append(patch(f"{_POST}._scrape_one_post_stats", side_effect=scraper))
    return p


class TestProxyTunnelFailure:
    """A proxy tunnel refusal is a warning, never a fresh $exception (#2605)."""

    def test_login_tunnel_failure_warns_not_errors(self):
        from contextlib import ExitStack
        with ExitStack() as es:
            for p in _sweep_patches({"side_effect": _tunnel_error()}):
                es.enter_context(p)
            warn = es.enter_context(patch(f"{_POST}.log_warning"))
            err = es.enter_context(patch(f"{_POST}.log_error"))
            posting.auto_scrape_post_stats.run(user_id=1)
        warn.assert_called_once()
        err.assert_not_called()

    def test_scrape_loop_tunnel_failure_warns_and_returns(self):
        from contextlib import ExitStack
        with ExitStack() as es:
            for p in _sweep_patches({"return_value": (object(), None, None, None)},
                                    scraper=_tunnel_error()):
                es.enter_context(p)
            warn = es.enter_context(patch(f"{_POST}.log_warning"))
            result = posting.auto_scrape_post_stats.run(user_id=1)
        warn.assert_called_once()
        assert "proxy tunnel failed" in str(result)
