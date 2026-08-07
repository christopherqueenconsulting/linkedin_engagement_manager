"""Unit tests for AI tools utilities (tools.py)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# search_with_perplexity
# ---------------------------------------------------------------------------

class TestSearchWithPerplexity:
    def _make_mock_response(self, answer="AI answer", citations=None):
        if citations is None:
            citations = ["https://example.com/1", "https://example.com/2"]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": answer}}],
            "citations": citations,
        }
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_raises_runtime_error_when_key_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            # ensure key is not present
            import os
            os.environ.pop("PERPLEXITY_API_KEY", None)
            from cqc_lem.utilities.ai.tools import search_with_perplexity

            with pytest.raises(RuntimeError, match="PERPLEXITY_API_KEY is not set"):
                search_with_perplexity("AI trends")

    def test_returns_dict_with_required_keys(self):
        mock_resp = self._make_mock_response()
        with patch.dict("os.environ", {"PERPLEXITY_API_KEY": "test-key"}):
            with patch("cqc_lem.utilities.ai.tools.requests.post", return_value=mock_resp):
                from cqc_lem.utilities.ai.tools import search_with_perplexity

                result = search_with_perplexity("AI trends")

        assert "query" in result
        assert "answer" in result
        assert "sources" in result

    def test_query_echoed_in_result(self):
        mock_resp = self._make_mock_response()
        with patch.dict("os.environ", {"PERPLEXITY_API_KEY": "test-key"}):
            with patch("cqc_lem.utilities.ai.tools.requests.post", return_value=mock_resp):
                from cqc_lem.utilities.ai.tools import search_with_perplexity

                result = search_with_perplexity("machine learning")

        assert result["query"] == "machine learning"

    def test_answer_extracted_from_response(self):
        mock_resp = self._make_mock_response(answer="The answer is 42")
        with patch.dict("os.environ", {"PERPLEXITY_API_KEY": "test-key"}):
            with patch("cqc_lem.utilities.ai.tools.requests.post", return_value=mock_resp):
                from cqc_lem.utilities.ai.tools import search_with_perplexity

                result = search_with_perplexity("test query")

        assert result["answer"] == "The answer is 42"

    def test_sources_truncated_to_max_sources(self):
        many_citations = [f"https://example.com/{i}" for i in range(10)]
        mock_resp = self._make_mock_response(citations=many_citations)
        with patch.dict("os.environ", {"PERPLEXITY_API_KEY": "test-key"}):
            with patch("cqc_lem.utilities.ai.tools.requests.post", return_value=mock_resp):
                from cqc_lem.utilities.ai.tools import search_with_perplexity

                result = search_with_perplexity("query", max_sources=3)

        assert len(result["sources"]) == 3

    def test_sources_are_url_dicts(self):
        mock_resp = self._make_mock_response(citations=["https://a.com", "https://b.com"])
        with patch.dict("os.environ", {"PERPLEXITY_API_KEY": "test-key"}):
            with patch("cqc_lem.utilities.ai.tools.requests.post", return_value=mock_resp):
                from cqc_lem.utilities.ai.tools import search_with_perplexity

                result = search_with_perplexity("query")

        for source in result["sources"]:
            assert "url" in source

    def test_empty_citations_returns_empty_sources(self):
        mock_resp = self._make_mock_response(citations=[])
        with patch.dict("os.environ", {"PERPLEXITY_API_KEY": "test-key"}):
            with patch("cqc_lem.utilities.ai.tools.requests.post", return_value=mock_resp):
                from cqc_lem.utilities.ai.tools import search_with_perplexity

                result = search_with_perplexity("query")

        assert result["sources"] == []

    def test_raise_for_status_is_called(self):
        mock_resp = self._make_mock_response()
        with patch.dict("os.environ", {"PERPLEXITY_API_KEY": "test-key"}):
            with patch("cqc_lem.utilities.ai.tools.requests.post", return_value=mock_resp):
                from cqc_lem.utilities.ai.tools import search_with_perplexity

                search_with_perplexity("query")

        mock_resp.raise_for_status.assert_called_once()


# ---------------------------------------------------------------------------
# search_recent_news
# ---------------------------------------------------------------------------

class TestSearchRecentNews:
    def _make_mock_googlenews(self, articles=None):
        if articles is None:
            articles = [
                {"title": "AI Boom", "date": "2024-01-01", "link": "https://news.com/1"},
                {"title": "ML Update", "date": "2024-01-02", "link": "https://news.com/2"},
            ]
        mock_gn = MagicMock()
        mock_gn.result.return_value = articles
        return mock_gn

    def test_returns_dict_with_required_keys(self):
        mock_gn = self._make_mock_googlenews()
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_gn):
            from cqc_lem.utilities.ai.tools import search_recent_news

            result = search_recent_news("AI", 7)

        assert "industry" in result
        assert "days" in result
        assert "articles" in result

    def test_industry_echoed_in_result(self):
        mock_gn = self._make_mock_googlenews()
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_gn):
            from cqc_lem.utilities.ai.tools import search_recent_news

            result = search_recent_news("Healthcare", 7)

        assert result["industry"] == "Healthcare"

    def test_days_echoed_in_result(self):
        mock_gn = self._make_mock_googlenews()
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_gn):
            from cqc_lem.utilities.ai.tools import search_recent_news

            result = search_recent_news("Finance", 14)

        assert result["days"] == 14

    def test_articles_list_populated(self):
        mock_gn = self._make_mock_googlenews()
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_gn):
            from cqc_lem.utilities.ai.tools import search_recent_news

            result = search_recent_news("Technology", 7)

        assert len(result["articles"]) == 2

    def test_article_fields_present(self):
        mock_gn = self._make_mock_googlenews(articles=[
            {"title": "Story", "date": "2024-03-01", "link": "https://x.com"}
        ])
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_gn):
            from cqc_lem.utilities.ai.tools import search_recent_news

            result = search_recent_news("Tech", 7)

        article = result["articles"][0]
        assert article["title"] == "Story"
        assert article["date"] == "2024-03-01"
        assert article["link"] == "https://x.com"

    def test_empty_articles_case(self):
        mock_gn = self._make_mock_googlenews(articles=[])
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_gn):
            from cqc_lem.utilities.ai.tools import search_recent_news

            result = search_recent_news("Niche", 7)

        assert result["articles"] == []

    def test_missing_article_fields_use_defaults(self):
        mock_gn = self._make_mock_googlenews(articles=[{}])
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_gn):
            from cqc_lem.utilities.ai.tools import search_recent_news

            result = search_recent_news("Tech", 7)

        article = result["articles"][0]
        assert article["title"] == "No title available"
        assert article["date"] == "No date available"
        assert article["link"] == "No link available"


# ---------------------------------------------------------------------------
# news_analysis_prompt
# ---------------------------------------------------------------------------

class TestNewsAnalysisPrompt:
    def test_returns_string(self):
        from cqc_lem.utilities.ai.tools import news_analysis_prompt

        result = news_analysis_prompt("AI", [])

        assert isinstance(result, str)

    def test_contains_industry_name(self):
        from cqc_lem.utilities.ai.tools import news_analysis_prompt

        result = news_analysis_prompt("Blockchain", [])

        assert "Blockchain" in result

    def test_contains_article_title(self):
        from cqc_lem.utilities.ai.tools import news_analysis_prompt

        articles = [{"title": "Rise of Robots", "date": "2024-01-01", "link": "https://x.com"}]
        result = news_analysis_prompt("Robotics", articles)

        assert "Rise of Robots" in result

    def test_handles_empty_articles_list(self):
        from cqc_lem.utilities.ai.tools import news_analysis_prompt

        result = news_analysis_prompt("Finance", [])

        assert isinstance(result, str)
        assert "Finance" in result

    def test_contains_analysis_keywords(self):
        from cqc_lem.utilities.ai.tools import news_analysis_prompt

        result = news_analysis_prompt("Healthcare", [])

        assert "keywords" in result.lower() or "trending" in result.lower() or "Analyze" in result


# ---------------------------------------------------------------------------
# google_news_tool
# ---------------------------------------------------------------------------

class TestGoogleNewsTool:
    def test_returns_dict_with_required_keys(self):
        mock_news = MagicMock()
        mock_news.result.return_value = []
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_news):
            with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
                mock_client.chat.completions.create.return_value.choices[0].message.content = "Analysis here"
                from cqc_lem.utilities.ai.tools import google_news_tool

                result = google_news_tool({"industry": "AI", "days": 7})

        assert "industry" in result
        assert "articles" in result
        assert "analysis" in result

    def test_industry_defaults_to_technology(self):
        mock_news = MagicMock()
        mock_news.result.return_value = []
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_news):
            with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
                mock_client.chat.completions.create.return_value.choices[0].message.content = "Analysis"
                from cqc_lem.utilities.ai.tools import google_news_tool

                result = google_news_tool({})

        assert result["industry"] == "Technology"

    def test_analysis_extracted_from_llm(self):
        mock_news = MagicMock()
        mock_news.result.return_value = []
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_news):
            with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
                mock_client.chat.completions.create.return_value.choices[0].message.content = "  AI is growing  "
                from cqc_lem.utilities.ai.tools import google_news_tool

                result = google_news_tool({"industry": "AI", "days": 7})

        assert result["analysis"] == "AI is growing"

    def test_calls_openai_client(self):
        mock_news = MagicMock()
        mock_news.result.return_value = []
        with patch("cqc_lem.utilities.ai.tools.GoogleNews", return_value=mock_news):
            with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
                mock_client.chat.completions.create.return_value.choices[0].message.content = "Result"
                from cqc_lem.utilities.ai.tools import google_news_tool

                google_news_tool({"industry": "Finance", "days": 3})

        mock_client.chat.completions.create.assert_called_once()


# ---------------------------------------------------------------------------
# chat_with_tools
# ---------------------------------------------------------------------------

class TestChatWithTools:
    def test_returns_string(self):
        with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
            mock_client.chat.completions.create.return_value.choices[0].message.content = "  Tool response  "
            from cqc_lem.utilities.ai.tools import chat_with_tools

            result = chat_with_tools("AI", 7)

        assert result == "Tool response"

    def test_calls_client_with_tools_param(self):
        with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
            mock_client.chat.completions.create.return_value.choices[0].message.content = "Response"
            from cqc_lem.utilities.ai.tools import chat_with_tools

            chat_with_tools("Finance", 14)

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert "tools" in call_kwargs

    def test_industry_appears_in_messages(self):
        with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
            mock_client.chat.completions.create.return_value.choices[0].message.content = "Response"
            from cqc_lem.utilities.ai.tools import chat_with_tools

            chat_with_tools("Blockchain", 7)

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        all_content = str(call_kwargs["messages"])
        assert "Blockchain" in all_content


# ---------------------------------------------------------------------------
# chat_about_news
# ---------------------------------------------------------------------------

class TestChatAboutNews:
    def test_returns_string(self):
        with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
            mock_client.chat.completions.create.return_value.choices[0].message.content = "  News chat response  "
            from cqc_lem.utilities.ai.tools import chat_about_news

            result = chat_about_news({"articles": []}, "Technology")

        assert result == "News chat response"

    def test_calls_client_with_tools_param(self):
        with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
            mock_client.chat.completions.create.return_value.choices[0].message.content = "Response"
            from cqc_lem.utilities.ai.tools import chat_about_news

            chat_about_news({}, "Healthcare")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert "tools" in call_kwargs

    def test_industry_appears_in_messages(self):
        with patch("cqc_lem.utilities.ai.tools.client") as mock_client:
            mock_client.chat.completions.create.return_value.choices[0].message.content = "Response"
            from cqc_lem.utilities.ai.tools import chat_about_news

            chat_about_news({}, "Real Estate")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        all_content = str(call_kwargs["messages"])
        assert "Real Estate" in all_content
