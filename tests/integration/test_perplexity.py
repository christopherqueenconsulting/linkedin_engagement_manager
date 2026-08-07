"""Integration tests for Perplexity Sonar research integration.

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
        """Perplexity sonar returns a non-empty answer for a known query."""
        if not os.environ.get("PERPLEXITY_API_KEY"):
            pytest.skip("PERPLEXITY_API_KEY not set")

        from cqc_lem.utilities.ai.tools import search_with_perplexity

        result = search_with_perplexity("Recent trends in artificial intelligence 2025")

        assert "answer" in result
        assert result["answer"], "Expected a non-empty answer from Perplexity"
        assert "sources" in result
        assert isinstance(result["sources"], list)

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
