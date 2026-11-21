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

    def test_fallback_receives_disabled_state_and_respects_remaining_api_cap(self):
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
        first_api_token = scraper.call_args_list[0].kwargs["api_access_token"]
        first_state = scraper.call_args_list[0].kwargs["api_state"]
        assert first_api_token == "cma-token"
        assert first_state == {"disabled": True}
        assert scraper.call_args_list[1].kwargs["api_access_token"] is None
        assert scraper.call_args_list[1].kwargs["api_state"] is first_state


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
