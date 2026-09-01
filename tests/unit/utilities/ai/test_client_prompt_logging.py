"""Prompt/completion CONTENT leaves the stack for allowlisted features ONLY (PR #1828).

The proxy redacts every call by default (`turn_off_message_logging: true`), which is what made
output quality ungradable — an online evaluation judging a published comment scored the literal
string `redacted-by-litellm`. The narrow fix is per request: LiteLLM's
`should_redact_message_logging` honours a `LiteLLM-Disable-Message-Redaction` header ahead of the
global setting, and `client.py` sets it for the features named in `LLM_PROMPT_LOGGING_FEATURES`.

Every assertion here is about the CLOSED direction, because that is the one that matters: the
header is the only thing standing between a user's profile synthesis or a draft DM and a
third-party analytics project. Like test_client_attribution.py and test_client_tracing.py, these
build REAL requests through the OpenAI SDK, so an SDK upgrade that moves the injection point fails
CI instead of silently un-scoping the egress.
"""
import json
from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.unit

_CHAT_RESPONSE = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
}

_HEADER = "litellm-disable-message-redaction"
_ENV = "LLM_PROMPT_LOGGING_FEATURES"


class _Recorder(httpx.BaseTransport):
    def __init__(self):
        self.requests = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.headers, json.loads(request.content)))
        return httpx.Response(200, json=_CHAT_RESPONSE)


def _client() -> tuple:
    from cqc_lem.utilities.ai.client import AttributedOpenAI
    recorder = _Recorder()
    client = AttributedOpenAI(api_key="k", base_url="http://litellm:4000", max_retries=0,
                              http_client=httpx.Client(transport=recorder))
    return client, recorder


def _send(client, model="lem-medium", **kwargs):
    try:
        client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}],
                                       **kwargs)
    except Exception:
        pass  # a stub response body only has to be good enough to build and send the request


def _opted_out(recorder) -> bool:
    headers, _ = recorder.requests[-1]
    return _HEADER in headers


def _comment_call(client, feature="comment"):
    _send(client, extra_body={"metadata": {"feature": feature, "user_id": 7}})


class TestTheAllowlistDecidesPerRequest:
    def test_an_allowlisted_feature_opts_this_request_out_of_redaction(self, monkeypatch) -> None:
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        _comment_call(client)
        assert _opted_out(recorder)

    def test_every_other_feature_stays_redacted(self, monkeypatch) -> None:
        """The whole point of scoping.

        Grading the comment drafter must not also ship draft DMs and profile synthesis, which are
        the user's private material and have no evaluation waiting on them.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        for feature in ("dm", "content", "newsletter", "system", "marketing"):
            _comment_call(client, feature=feature)
            assert not _opted_out(recorder), feature

    def test_an_unset_allowlist_redacts_everything(self, monkeypatch) -> None:
        """It ships EMPTY, so merging the mechanism changes nothing about what leaves the stack.

        Turning it on is an owner decision — issue #1832.
        """
        monkeypatch.delenv(_ENV, raising=False)
        client, recorder = _client()
        _comment_call(client)
        assert not _opted_out(recorder)

    def test_an_emptied_allowlist_closes_it_again(self, monkeypatch) -> None:
        """The operator's off switch — an `.env` edit, not a file edit and a broken test."""
        monkeypatch.setenv(_ENV, "  ,  ")
        client, recorder = _client()
        _comment_call(client)
        assert not _opted_out(recorder)

    def test_the_value_is_read_per_call_not_at_import(self, monkeypatch) -> None:
        client, recorder = _client()
        monkeypatch.setenv(_ENV, "comment")
        _comment_call(client)
        assert _opted_out(recorder)
        monkeypatch.setenv(_ENV, "")
        _comment_call(client)
        assert not _opted_out(recorder)

    def test_spacing_and_case_in_the_env_value_are_tolerated(self, monkeypatch) -> None:
        """Typed by hand into `.env`; `Comment, dm` must not silently mean nothing is allowlisted."""
        monkeypatch.setenv(_ENV, " Comment , dm ")
        client, recorder = _client()
        _comment_call(client, feature="COMMENT")
        assert _opted_out(recorder)


class TestItFailsClosed:
    def test_a_call_with_no_attribution_at_all_stays_redacted(self, monkeypatch) -> None:
        """A request with no attribution can never be allowlisted.

        The ambient hook stamps `feature: system` on an unattributed call and `system` is not a
        content surface anyone would allowlist — but the guard must hold even if that changes.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.current_attribution", return_value=(None, None)):
            _send(client)
        assert not _opted_out(recorder)

    def test_a_raw_provider_model_is_never_opted_out(self, monkeypatch) -> None:
        """A direct provider call has no analytics leg to buy anything with.

        Only tier aliases go through the proxy's PostHog logger, so on a raw model the header would
        be a bare content disclosure in exchange for nothing.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        _send(client, model="gpt-4o-mini", extra_body={"metadata": {"feature": "comment"}})
        assert not _opted_out(recorder)

    def test_a_hook_failure_leaves_redaction_in_place(self, monkeypatch) -> None:
        """A throwing hook must fail TOWARD redacted, and the call must still go out.

        Observability is never a reason to lose a generation, and never a reason to leak one either.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.prompt_logging_features",
                   side_effect=RuntimeError("boom")):
            _comment_call(client)
        headers, body = recorder.requests[-1]
        assert _HEADER not in headers
        assert body["metadata"]["feature"] == "comment"

    def test_the_trace_header_still_rides_alongside(self, monkeypatch) -> None:
        """Both hooks write `options.headers`; the second must copy the first rather than replace it."""
        from cqc_lem.utilities.observability import llm_trace
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.observability.posthog"):
            with llm_trace("comment_generation", user_id=7, feature="comment") as trace_id:
                _comment_call(client)
        headers, _ = recorder.requests[-1]
        assert headers.get("x-litellm-trace-id") == trace_id
        assert _HEADER in headers
