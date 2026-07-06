"""Unit tests for the newsletter research layer (all I/O mocked)."""

import pytest
from unittest.mock import MagicMock, patch

from cqc_lem.utilities.ai import newsletter_research as nr

pytestmark = pytest.mark.unit

_CLIENT = "cqc_lem.utilities.ai.client.client"
_DIRECT = "cqc_lem.utilities.ai.tools.search_with_perplexity"


def _resp(text, citations=None):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    r.citations = citations if citations is not None else []
    return r


class TestLiteLLMRoutePreferred:
    def test_uses_lem_research_alias(self, monkeypatch):
        monkeypatch.delenv("NEWSLETTER_RESEARCH_ENABLED", raising=False)
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.chat.completions.create.return_value = _resp(
                "42% of teams (2026, Acme survey) now...", ["https://acme.example/report"])
            out = nr.research_newsletter_topic("delegation for founders")
        client.chat.completions.create.assert_called_once()
        assert client.chat.completions.create.call_args.kwargs["model"] == "lem-research"
        direct.assert_not_called()  # LiteLLM route preferred; direct helper untouched
        assert out["findings"].startswith("42% of teams")
        assert out["sources"] == [{"url": "https://acme.example/report"}]

    def test_query_includes_subject_angle_and_format_focus(self):
        with patch(_CLIENT) as client:
            client.chat.completions.create.return_value = _resp("findings")
            nr.research_newsletter_topic(
                "pricing strategy", blueprint={"angle": "the psychology angle", "format": "contrarian"},
                newsletter_description="a newsletter for SaaS founders")
        query = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "pricing strategy" in query
        assert "the psychology angle" in query
        assert "conventional wisdom" in query  # contrarian → challenge-the-consensus focus
        assert "SaaS founders" in query

    def test_exactly_one_call_per_invocation(self):
        with patch(_CLIENT) as client:
            client.chat.completions.create.return_value = _resp("findings")
            nr.research_newsletter_topic("subject one")
        assert client.chat.completions.create.call_count == 1


class TestFallbacks:
    def test_falls_back_to_direct_helper_when_litellm_fails(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.chat.completions.create.side_effect = Exception("proxy down")
            direct.return_value = {"query": "q", "answer": "Direct answer with stats.",
                                   "sources": [{"url": "https://s"}]}
            out = nr.research_newsletter_topic("some subject")
        direct.assert_called_once()
        assert out == {"findings": "Direct answer with stats.", "sources": [{"url": "https://s"}]}

    def test_empty_litellm_response_falls_back(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.chat.completions.create.return_value = _resp("")
            direct.return_value = {"query": "q", "answer": "fallback", "sources": []}
            out = nr.research_newsletter_topic("some subject")
        assert out["findings"] == "fallback"

    def test_both_routes_fail_returns_empty_never_raises(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.chat.completions.create.side_effect = Exception("proxy down")
            direct.side_effect = RuntimeError("PERPLEXITY_API_KEY is not set")
            out = nr.research_newsletter_topic("some subject")
        assert out == {"findings": "", "sources": []}

    def test_blank_subject_skips_all_calls(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            out = nr.research_newsletter_topic("   ")
        client.chat.completions.create.assert_not_called()
        direct.assert_not_called()
        assert out == {"findings": "", "sources": []}


class TestToggle:
    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
    def test_disabled_values_skip_research(self, monkeypatch, value):
        monkeypatch.setenv("NEWSLETTER_RESEARCH_ENABLED", value)
        with patch(_CLIENT) as client:
            out = nr.research_newsletter_topic("a subject")
        client.chat.completions.create.assert_not_called()
        assert out == {"findings": "", "sources": []}

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("NEWSLETTER_RESEARCH_ENABLED", raising=False)
        with patch(_CLIENT) as client:
            client.chat.completions.create.return_value = _resp("findings")
            out = nr.research_newsletter_topic("a subject")
        client.chat.completions.create.assert_called_once()
        assert out["findings"] == "findings"

    def test_explicit_true_enables(self, monkeypatch):
        monkeypatch.setenv("NEWSLETTER_RESEARCH_ENABLED", "true")
        with patch(_CLIENT) as client:
            client.chat.completions.create.return_value = _resp("findings")
            assert nr.research_newsletter_topic("a subject")["findings"] == "findings"
