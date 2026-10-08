"""Integration tests for the Perplexity research integration (Agent API since #2255).

Sonar Chat Completions was sunset on 2026-09-27; `search_with_perplexity` now calls the Agent API
(`/v1/agent`, `fast` preset) directly, and the proxy's `lem-research` alias reaches the same API via
`/v1/responses` (prove that route with `scripts/probe_research.py`).

Tests that hit the live API are skipped when PERPLEXITY_API_KEY is absent.
Tests that verify fallback/error behavior run without a key.
"""
import os
from unittest.mock import patch

import pytest


@pytest.mark.integration
@pytest.mark.slow
class TestPerplexitySearch:
    def test_search_returns_answer_and_sources(self):
        """The Agent API `fast` preset returns a non-empty, cited answer for a known query."""
        if not os.environ.get("PERPLEXITY_API_KEY"):
            pytest.skip("PERPLEXITY_API_KEY not set")

        import requests

        from cqc_lem.utilities.ai.tools import search_with_perplexity

        try:
            result = search_with_perplexity("Recent trends in artificial intelligence 2026")
        except requests.exceptions.HTTPError as e:
            # Same rule the Pexels probe already follows: a present-but-expired key is
            # functionally "no key" for a live-call test, and turning the nightly red for
            # someone else's dead credential is how a job stops being read. Presence was the only
            # check here, so a 401 failed the run — caught the moment these tests were given a
            # schedule to run on.
            status = getattr(e.response, "status_code", None)
            if status in (401, 403):
                pytest.skip(f"PERPLEXITY_API_KEY unauthorized ({status})")
            raise

        assert "answer" in result
        assert result["answer"], "Expected a non-empty answer from Perplexity"
        assert "sources" in result
        assert isinstance(result["sources"], list)
        # A web-search preset grounds its answer on what it read; every source is a URL.
        assert result["sources"], "Expected at least one citation from a web-search preset"
        assert all(str(s["url"]).startswith("http") for s in result["sources"])

    def test_search_raises_without_api_key(self):
        """search_with_perplexity raises RuntimeError when PERPLEXITY_API_KEY is missing."""
        # Patch both the env and the module to cover both read paths
        with patch.dict(os.environ, {}, clear=False), \
             patch.dict(os.environ, {"PERPLEXITY_API_KEY": ""}):
            from cqc_lem.utilities.ai.tools import search_with_perplexity
            with pytest.raises(RuntimeError, match="PERPLEXITY_API_KEY is not set"):
                search_with_perplexity("any query")

    def test_fallback_to_googlenews_when_key_missing(self):
        """get_industry_trend_analysis_based_on_user_profile falls back to GoogleNews silently."""
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile

        mock_profile = LinkedInProfile(
            full_name="Test User",
            job_title="Software Engineer",
            company_name="Tech Co",
        )

        with patch("cqc_lem.utilities.ai.ai_helper.research_topic",
                   return_value={"findings": "", "sources": []}), \
             patch("cqc_lem.utilities.ai.ai_helper.search_recent_news",
                   return_value={"articles": [{"title": "AI news", "date": "2025-01-01", "link": "https://example.com"}]}), \
             patch("cqc_lem.utilities.ai.ai_helper.get_industries_of_profile_from_ai",
                   return_value="Technology"), \
             patch("cqc_lem.utilities.ai.ai_helper.get_industry_trend_from_ai",
                   return_value="Trend analysis result") as mock_trend:
            from cqc_lem.utilities.ai.ai_helper import get_industry_trend_analysis_based_on_user_profile
            result = get_industry_trend_analysis_based_on_user_profile(mock_profile)

        assert result["analysis"] == "Trend analysis result"
        assert mock_trend.called
