"""Unit tests for the SHARED research layer (all I/O mocked) used by newsletters, posts, and
comments — including the per-type cost toggles (comment research OFF by default).
"""

import json
import pathlib
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI

from cqc_lem.utilities.ai import content_research as cr

pytestmark = pytest.mark.unit

_CLIENT = "cqc_lem.utilities.ai.client.client"
_DIRECT = "cqc_lem.utilities.ai.tools.search_with_perplexity"

_RESEARCH_ENVS = ("CONTENT_RESEARCH_ENABLED", "NEWSLETTER_RESEARCH_ENABLED",
                  "POST_RESEARCH_ENABLED", "COMMENT_RESEARCH_ENABLED")


_TRACK = "cqc_lem.utilities.observability.track_llm_call"


@pytest.fixture(autouse=True)
def _clean_toggles(monkeypatch):
    for name in _RESEARCH_ENVS + ("RESEARCH_VIA_PROXY",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cr, "_proxy_skip_until", 0.0)  # the in-process fallback deadline
    monkeypatch.setattr(cr, "_shared_redis", lambda: None)  # no Redis unless a test brings one


@pytest.fixture(autouse=True)
def spend():
    """Every direct research call books an llm_call; capture it instead of emitting it."""
    with patch(_TRACK) as track:
        yield track


@pytest.fixture
def proxy_on(monkeypatch):
    """The proxy route, which is opt-in while LiteLLM cannot parse the Agent API's answer."""
    monkeypatch.setenv("RESEARCH_VIA_PROXY", "true")


_FIXTURE = pathlib.Path(__file__).resolve().parents[3] / "fixtures" / "perplexity_agent_response.json"


def _resp(text, citations=None):
    """An Agent API response (the `/v1/responses` shape) carrying `text` and inline citations."""
    return {"object": "response", "status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text,
             "annotations": [{"type": "url_citation", "url": u} for u in citations or []]}]}]}


def _sdk_response(doc):
    """`doc` parsed exactly as the app's OpenAI client parses the proxy's answer."""
    def handler(request):
        return httpx.Response(200, json=doc)
    sdk = OpenAI(api_key="test", base_url="http://proxy.test/v1",
                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return sdk.responses.create(model="lem-research", input="q")


@pytest.mark.usefixtures("proxy_on")
class TestLiteLLMRouteUnderTheFlag:
    def test_uses_lem_research_alias(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.responses.create.return_value = _resp(
                "42% of teams (2026, Acme survey) now...", ["https://acme.example/report"])
            out = cr.research_topic("delegation for founders", content_type="newsletter")
        client.responses.create.assert_called_once()
        assert client.responses.create.call_args.kwargs["model"] == "lem-research"
        direct.assert_not_called()  # LiteLLM route preferred; direct helper untouched
        assert out["findings"].startswith("42% of teams")
        assert out["sources"] == [{"url": "https://acme.example/report"}]

    def test_query_includes_subject_angle_and_format_focus(self):
        with patch(_CLIENT) as client:
            client.responses.create.return_value = _resp("findings")
            cr.research_topic(
                "pricing strategy", content_type="newsletter",
                blueprint={"angle": "the psychology angle", "format": "contrarian"},
                context_description="a newsletter for SaaS founders")
        query = client.responses.create.call_args.kwargs["input"]
        assert "pricing strategy" in query
        assert "the psychology angle" in query
        assert "conventional wisdom" in query  # contrarian → challenge-the-consensus focus
        assert "SaaS founders" in query

    def test_query_focus_for_roundup_and_focus_topics(self):
        with patch(_CLIENT) as client:
            client.responses.create.return_value = _resp("findings")
            cr.research_topic(
                "AI tooling this month", content_type="newsletter",
                blueprint={"format": "roundup"},
                prefs={"focus_topics": ["ai agents", "automation"]})
        query = client.responses.create.call_args.kwargs["input"]
        assert "last few weeks" in query
        assert "ai agents, automation" in query

    def test_query_focus_for_case_study(self):
        with patch(_CLIENT) as client:
            client.responses.create.return_value = _resp("findings")
            cr.research_topic("churn turnarounds", content_type="newsletter",
                              blueprint={"format": "case_study"})
        query = client.responses.create.call_args.kwargs["input"]
        assert "real named examples" in query

    def test_post_type_query_uses_post_menu_focus(self):
        with patch(_CLIENT) as client:
            client.responses.create.return_value = _resp("findings")
            cr.research_topic("logistics automation", content_type="post",
                              blueprint={"format": "industry_observation"})
        query = client.responses.create.call_args.kwargs["input"]
        assert "LinkedIn post" in query
        assert "last few weeks" in query

    def test_exactly_one_call_per_invocation(self):
        with patch(_CLIENT) as client:
            client.responses.create.return_value = _resp("findings")
            cr.research_topic("subject one", content_type="newsletter")
        assert client.responses.create.call_count == 1


@pytest.mark.usefixtures("proxy_on")
class TestAgentApiResponseShape:
    """#2255: `lem-research` answers in Perplexity's Agent API shape, not a chat choice."""

    def test_recorded_response_through_the_sdk_yields_findings_and_citations(self):
        response = _sdk_response(json.loads(_FIXTURE.read_text()))
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.responses.create.return_value = response
            out = cr.research_topic("AI agents for small business", content_type="post")
        direct.assert_not_called()
        assert out["findings"].startswith("41% of small businesses")
        # search results first (what the answer was grounded on), then inline-only citations
        assert out["sources"] == [{"url": "https://research.example.com/smb-ai-2026"},
                                  {"url": "https://survey.example.org/backoffice"},
                                  {"url": "https://news.example.net/agents-ops"}]

    def test_the_call_uses_the_responses_route_with_the_research_brief(self):
        with patch(_CLIENT) as client:
            client.responses.create.return_value = _resp("findings")
            cr.research_topic("pricing strategy", content_type="newsletter")
        kwargs = client.responses.create.call_args.kwargs
        assert kwargs["model"] == "lem-research"
        assert kwargs["instructions"] == cr._RESEARCH_SYSTEM
        assert kwargs["max_output_tokens"] == 900
        client.chat.completions.create.assert_not_called()

    def test_sources_are_capped_and_deduplicated(self):
        doc = json.loads(_FIXTURE.read_text())
        findings, sources = cr.parse_agent_response(doc, max_sources=2)
        assert findings.startswith("41%")
        assert sources == [{"url": "https://research.example.com/smb-ai-2026"},
                           {"url": "https://survey.example.org/backoffice"}]

    def test_legacy_top_level_citations_are_still_read(self):
        doc = {"output_text": "converted sonar answer",
               "citations": ["https://a.example", "not-a-url", "https://a.example"],
               "search_results": [{"url": "https://b.example"}, {"title": "no url"}]}
        assert cr.parse_agent_response(doc) == (
            "converted sonar answer", [{"url": "https://a.example"}, {"url": "https://b.example"}])

    @pytest.mark.parametrize("doc", [
        {}, {"output": None}, {"output": "garbage"},
        {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]},
        {"output": [{"type": "search_results", "results": "oops"}, 7, None]},
    ])
    def test_malformed_or_empty_responses_parse_to_nothing(self, doc):
        findings, _ = cr.parse_agent_response(doc)
        assert findings == ""

    def test_empty_or_malformed_response_falls_open_to_the_direct_route(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.responses.create.return_value = {"output": [{"type": "search_results"}]}
            direct.return_value = {"answer": "direct findings", "sources": []}
            out = cr.research_topic("some subject", content_type="post")
        direct.assert_called_once()
        assert out["findings"] == "direct findings"

    def test_malformed_everywhere_still_returns_empty_and_never_raises(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.responses.create.return_value = {"output": "garbage"}
            direct.side_effect = ValueError("unexpected JSON")
            out = cr.research_topic("some subject", content_type="newsletter")
        assert out == {"findings": "", "sources": []}


@pytest.mark.usefixtures("proxy_on")
class TestFallbacks:
    def test_falls_back_to_direct_helper_when_litellm_fails(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.responses.create.side_effect = Exception("proxy down")
            direct.return_value = {"query": "q", "answer": "Direct answer with stats.",
                                   "sources": [{"url": "https://s"}]}
            out = cr.research_topic("some subject", content_type="newsletter")
        direct.assert_called_once()
        assert out == {"findings": "Direct answer with stats.", "sources": [{"url": "https://s"}]}

    def test_empty_litellm_response_falls_back(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.responses.create.return_value = _resp("")
            direct.return_value = {"query": "q", "answer": "fallback", "sources": []}
            out = cr.research_topic("some subject", content_type="post")
        assert out["findings"] == "fallback"

    def test_both_routes_fail_returns_empty_never_raises(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.responses.create.side_effect = Exception("proxy down")
            direct.side_effect = RuntimeError("PERPLEXITY_API_KEY is not set")
            out = cr.research_topic("some subject", content_type="newsletter")
        assert out == {"findings": "", "sources": []}

    def test_blank_subject_skips_all_calls(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            out = cr.research_topic("   ", content_type="newsletter")
        client.responses.create.assert_not_called()
        direct.assert_not_called()
        assert out == {"findings": "", "sources": []}


class TestPerTypeToggles:
    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
    def test_newsletter_disabled_values_skip_research(self, monkeypatch, value):
        monkeypatch.setenv("NEWSLETTER_RESEARCH_ENABLED", value)
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            out = cr.research_topic("a subject", content_type="newsletter")
        client.responses.create.assert_not_called()
        direct.assert_not_called()
        assert out == {"findings": "", "sources": []}

    def test_newsletter_and_post_enabled_by_default(self):
        assert cr.research_enabled("newsletter") is True
        assert cr.research_enabled("post") is True

    def test_comment_research_off_by_default_makes_no_calls(self):
        """THE cost contract: a normal comment must not fire any research API call."""
        assert cr.research_enabled("comment") is False
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            out = cr.research_topic("a post about supply chains", content_type="comment")
        client.responses.create.assert_not_called()
        direct.assert_not_called()
        assert out == {"findings": "", "sources": []}

    def test_comment_research_opt_in_via_env(self, monkeypatch):
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        with patch(_DIRECT) as direct:
            direct.return_value = {"answer": "comment findings", "sources": []}
            out = cr.research_topic("a post about supply chains", content_type="comment")
        direct.assert_called_once()
        assert out["findings"] == "comment findings"

    def test_master_switch_kills_all_types(self, monkeypatch):
        monkeypatch.setenv("CONTENT_RESEARCH_ENABLED", "false")
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        for content_type in ("newsletter", "post", "comment"):
            assert cr.research_enabled(content_type) is False
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            out = cr.research_topic("a subject", content_type="newsletter")
        client.responses.create.assert_not_called()
        direct.assert_not_called()
        assert out == {"findings": "", "sources": []}

    def test_post_toggle_independent_of_newsletter(self, monkeypatch):
        monkeypatch.setenv("POST_RESEARCH_ENABLED", "false")
        assert cr.research_enabled("post") is False
        assert cr.research_enabled("newsletter") is True

    def test_explicit_true_enables(self, monkeypatch):
        monkeypatch.setenv("NEWSLETTER_RESEARCH_ENABLED", "true")
        with patch(_DIRECT) as direct:
            direct.return_value = {"answer": "findings", "sources": []}
            assert cr.research_topic("a subject", content_type="newsletter")["findings"] == "findings"

    def test_unknown_type_defaults_conservative(self):
        assert cr.research_enabled("carrier_pigeon") is False


# The exact text the live proxy returned on prod v0.183.0 (2026-10-08), 4 of 5 probe runs.
_TRUNCATION_DEFECT = (
    "Error code: 500 - {'error': {'message': \"litellm.InternalServerError: 1 validation error for "
    "ResponsesAPIResponse\\ntruncation\\n  Input should be 'auto' or 'disabled' "
    "[type=literal_error, input_value='', input_type=str]\"}}")

_USAGE = {"input_tokens": 412, "output_tokens": 188, "total_tokens": 600,
          "cost": {"currency": "USD", "request_cost": 0.0025, "total_cost": 0.00281}}


class TestDirectRouteIsPrimary:
    """LiteLLM cannot parse the Agent API's answer, so research goes straight to Perplexity."""

    def test_default_reaches_perplexity_without_touching_the_proxy(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            direct.return_value = {"answer": " direct findings ", "sources": [{"url": "https://s"}],
                                   "usage": _USAGE}
            out = cr.research_topic("delegation for founders", content_type="newsletter")
        client.responses.create.assert_not_called()
        direct.assert_called_once()
        assert out == {"findings": "direct findings", "sources": [{"url": "https://s"}]}

    @pytest.mark.parametrize("value, expected", [
        (None, False), ("", False), ("false", False), ("0", False), ("true", True), ("1", True)])
    def test_the_proxy_flag_defaults_off(self, monkeypatch, value, expected):
        if value is not None:
            monkeypatch.setenv("RESEARCH_VIA_PROXY", value)
        assert cr.research_via_proxy_enabled() is expected

    def test_spend_is_booked_at_perplexitys_own_price(self, spend):
        from cqc_lem.utilities.observability import llm_attribution
        with patch(_DIRECT) as direct, llm_attribution(user_id=7, feature="newsletter"):
            direct.return_value = {"answer": "findings", "sources": [], "usage": _USAGE}
            cr.research_topic("a subject", content_type="newsletter")
        spend.assert_called_once()
        kwargs = spend.call_args.kwargs
        assert kwargs["model"] == "lem-research"
        assert (kwargs["prompt_tokens"], kwargs["completion_tokens"]) == (412, 188)
        assert kwargs["response_cost"] == pytest.approx(0.00281)
        assert kwargs["success"] is True
        assert (kwargs["user_id"], kwargs["feature"]) == (7, "newsletter")

    @pytest.mark.parametrize("usage", [
        None, {}, {"cost": "n/a"}, {"input_tokens": "x", "cost": {"total_cost": -1}},
        {"cost": {"total_cost": float("nan")}}])
    def test_an_unpriced_answer_falls_back_to_the_estimate_never_to_free(self, spend, usage):
        with patch(_DIRECT) as direct:
            direct.return_value = {"answer": "findings", "sources": [], "usage": usage}
            cr.research_topic("a subject", content_type="post")
        kwargs = spend.call_args.kwargs
        assert kwargs["response_cost"] is None  # None, not 0.0: track_llm_call then estimates
        assert kwargs["model"] == "lem-research"

    def test_a_failed_direct_call_is_recorded_and_degrades_to_empty(self, spend):
        with patch(_DIRECT) as direct, patch.object(cr, "log_warning") as warn:
            direct.side_effect = RuntimeError("PERPLEXITY_API_KEY is not set")
            out = cr.research_topic("a subject", content_type="newsletter")
        assert out == {"findings": "", "sources": []}
        assert spend.call_args.kwargs["success"] is False
        warn.assert_called_once()  # no research at all is a real fault, so it stays a warning

    def test_telemetry_failure_never_breaks_research(self, spend):
        spend.side_effect = RuntimeError("posthog down")
        with patch(_DIRECT) as direct:
            direct.return_value = {"answer": "findings", "sources": [], "usage": _USAGE}
            out = cr.research_topic("a subject", content_type="newsletter")
        assert out["findings"] == "findings"


@pytest.mark.usefixtures("proxy_on")
class TestProxyFailureLogLevel:
    """Once is a warning, repeatedly is a defect: the KNOWN parse failure warns once, then backs off."""

    def test_the_known_truncation_defect_warns_once_and_parks_the_proxy(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct, \
                patch.object(cr, "log_warning") as warn:
            client.responses.create.side_effect = Exception(_TRUNCATION_DEFECT)
            direct.return_value = {"answer": "direct findings", "sources": []}
            first = cr.research_topic("a subject", content_type="newsletter")
            second = cr.research_topic("another subject", content_type="post")
        assert first["findings"] == second["findings"] == "direct findings"
        assert client.responses.create.call_count == 1  # the second call never paid the proxy
        warn.assert_called_once()
        assert "RESEARCH_VIA_PROXY is on" in warn.call_args.args[0]
        assert direct.call_count == 2

    def test_the_proxy_is_tried_again_after_the_backoff(self):
        cr._park_proxy_route(now=1000.0)
        assert not cr.proxy_route_open(now=1000.0 + cr.PROXY_DEFECT_BACKOFF_SECONDS - 1)
        assert cr.proxy_route_open(now=1000.0 + cr.PROXY_DEFECT_BACKOFF_SECONDS)

    def test_a_proxy_success_keeps_the_route_open(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct:
            client.responses.create.return_value = _sdk_response(_resp("proxy findings"))
            cr.research_topic("a subject", content_type="newsletter")
            cr.research_topic("a subject", content_type="newsletter")
        assert client.responses.create.call_count == 2
        direct.assert_not_called()

    def test_an_unknown_proxy_failure_still_warns(self):
        with patch(_CLIENT) as client, patch(_DIRECT) as direct, \
                patch.object(cr, "log_warning") as warn:
            client.responses.create.side_effect = Exception("proxy down")
            direct.return_value = {"answer": "direct findings", "sources": []}
            cr.research_topic("a subject", content_type="newsletter")
        warn.assert_called_once()
        assert "trying direct Perplexity" in warn.call_args.args[0]

    @pytest.mark.parametrize("text, known", [
        (_TRUNCATION_DEFECT, True),
        ("1 validation error for ResponsesAPIResponse\nstatus\n  Input should be ...", False),
        ("Connection error.", False),
        ("Empty research response from lem-research", False),
    ])
    def test_only_the_truncation_literal_counts_as_known(self, text, known):
        assert cr.is_known_proxy_parse_defect(Exception(text)) is known


def test_the_proxy_route_stays_closed_without_the_flag():
    assert cr.proxy_route_open() is False


class _FakeRedis:
    """Just the two calls the proxy latch makes: SET NX EX, and EXISTS."""

    def __init__(self, fail: bool = False):
        self.keys: dict = {}
        self.fail = fail

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        if nx and key in self.keys:
            return None
        self.keys[key] = (value, ex)
        return True

    def exists(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return int(key in self.keys)


@pytest.mark.usefixtures("proxy_on")
class TestFleetWideProxyLatch:
    """Celery recycles worker children every few tasks, so the closed route must outlive one."""

    def test_a_fresh_worker_child_sees_the_route_closed(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(cr, "_shared_redis", lambda: fake)

        assert cr._park_proxy_route() is True
        assert fake.keys[cr.PROXY_PARKED_KEY][1] == cr.PROXY_DEFECT_BACKOFF_SECONDS
        monkeypatch.setattr(cr, "_proxy_skip_until", 0.0)  # a new child: no in-process memory
        assert cr.proxy_route_open() is False

    def test_only_the_worker_that_opens_the_window_warns(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(cr, "_shared_redis", lambda: fake)
        fake.keys[cr.PROXY_PARKED_KEY] = ("1", cr.PROXY_DEFECT_BACKOFF_SECONDS)  # another worker
        monkeypatch.setattr(cr, "proxy_route_open", lambda now=None: True)  # it raced past the check

        with patch(_CLIENT) as client, patch(_DIRECT) as direct, \
                patch.object(cr, "log_warning") as warn, patch.object(cr, "log_debug") as debug:
            client.responses.create.side_effect = Exception(_TRUNCATION_DEFECT)
            direct.return_value = {"answer": "direct findings", "sources": []}
            cr.research_topic("a subject", content_type="newsletter")
        warn.assert_not_called()
        assert any("already closed" in c.args[0] for c in debug.call_args_list)

    def test_an_unreachable_redis_falls_back_to_the_process_deadline(self, monkeypatch):
        monkeypatch.setattr(cr, "_shared_redis", lambda: _FakeRedis(fail=True))

        assert cr.proxy_route_open() is True  # unreadable Redis is never "closed"
        assert cr._park_proxy_route() is True  # and the window is still warned once here
        assert cr.proxy_route_open() is False  # the in-process deadline holds


_REAL_SHARED_REDIS = cr._shared_redis  # captured before the autouse stub replaces it


def test_shared_redis_never_raises(monkeypatch):
    import cqc_lem.utilities.linkedin.rate_limit as rl

    def boom():
        raise RuntimeError("redis client unavailable")
    monkeypatch.setattr(rl, "shared_redis_client", boom)
    assert _REAL_SHARED_REDIS() is None
