"""Prompt/completion CONTENT leaves the stack for allowlisted features ONLY (PR #1828).

The proxy redacts every call by default (`turn_off_message_logging: true`), which is what made
output quality ungradable — an online evaluation judging a published comment scored the literal
string `redacted-by-litellm`. The narrow fix is per request: LiteLLM's
`should_redact_message_logging` honours a `LiteLLM-Disable-Message-Redaction` header ahead of the
global setting, and `client.py` sets it for the features named in `LLM_PROMPT_LOGGING_FEATURES`.

Most assertions here are about the CLOSED direction, because that is the one that matters: the
header is the only thing standing between a user's profile synthesis or a draft DM and a
third-party analytics project. Like test_client_attribution.py and test_client_tracing.py, these
build REAL requests through the OpenAI SDK, so an SDK upgrade that moves the injection point fails
CI instead of silently un-scoping the egress.

`_send` asserts the transport actually received a request before anything reads it. Without that, a
send that failed early would leave every assertion inspecting the PREVIOUS request — and the
five-feature loop below would report five clean passes off one stale record.
"""
import json
from typing import Any
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
    def __init__(self) -> None:
        self.requests: list[tuple[httpx.Headers, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.headers, json.loads(request.content)))
        return httpx.Response(200, json=_CHAT_RESPONSE)


def _client() -> tuple[Any, _Recorder]:
    from cqc_lem.utilities.ai.client import AttributedOpenAI
    recorder = _Recorder()
    client = AttributedOpenAI(api_key="k", base_url="http://litellm:4000", max_retries=0,
                              http_client=httpx.Client(transport=recorder))
    return client, recorder


def _record(recorder: _Recorder, before: int) -> tuple[httpx.Headers, Any]:
    """The ONE request this call put on the wire — never a leftover from the last one."""
    assert len(recorder.requests) == before + 1, "the request never reached the transport"
    return recorder.requests[-1]


def _send(client: Any, recorder: _Recorder, model: str = "lem-medium",
          **kwargs: Any) -> tuple[httpx.Headers, Any]:
    before = len(recorder.requests)
    try:
        client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}],
                                       **kwargs)
    except Exception:
        pass  # a stub response body only has to be good enough to build and send the request
    return _record(recorder, before)


def _comment_call(client: Any, recorder: _Recorder,
                  feature: str = "comment") -> tuple[httpx.Headers, Any]:
    return _send(client, recorder, extra_body={"metadata": {"feature": feature, "user_id": 7}})


def _opted_out(sent: tuple[httpx.Headers, Any]) -> bool:
    headers, _ = sent
    return _HEADER in headers


class TestTheAllowlistDecidesPerRequest:
    def test_an_allowlisted_feature_opts_this_request_out_of_redaction(self, monkeypatch) -> None:
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        assert _opted_out(_comment_call(client, recorder))

    def test_every_other_feature_stays_redacted(self, monkeypatch) -> None:
        """The whole point of scoping.

        Grading the comment drafter must not also ship draft DMs and profile synthesis, which are
        the user's private material and have no evaluation waiting on them.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        for feature in ("dm", "content", "newsletter", "system", "marketing"):
            assert not _opted_out(_comment_call(client, recorder, feature=feature)), feature

    def test_an_unset_allowlist_redacts_everything(self, monkeypatch) -> None:
        """It ships EMPTY, so merging the mechanism changes nothing about what leaves the stack.

        Turning it on is an owner decision — issue #1832.
        """
        monkeypatch.delenv(_ENV, raising=False)
        client, recorder = _client()
        assert not _opted_out(_comment_call(client, recorder))

    def test_an_emptied_allowlist_closes_it_again(self, monkeypatch) -> None:
        """The operator's off switch — an `.env` edit, not a file edit and a broken test."""
        monkeypatch.setenv(_ENV, "  ,  ")
        client, recorder = _client()
        assert not _opted_out(_comment_call(client, recorder))

    def test_the_value_is_read_per_call_not_at_import(self, monkeypatch) -> None:
        client, recorder = _client()
        monkeypatch.setenv(_ENV, "comment")
        assert _opted_out(_comment_call(client, recorder))
        monkeypatch.setenv(_ENV, "")
        assert not _opted_out(_comment_call(client, recorder))

    def test_spacing_and_case_in_the_env_value_are_tolerated(self, monkeypatch) -> None:
        """Typed by hand into `.env`; `Comment, dm` must not silently mean nothing is allowlisted."""
        monkeypatch.setenv(_ENV, " Comment , dm ")
        client, recorder = _client()
        assert _opted_out(_comment_call(client, recorder, feature="COMMENT"))


class TestItFailsClosed:
    def test_a_call_with_no_attribution_at_all_stays_redacted(self, monkeypatch) -> None:
        """A request with no attribution can never be allowlisted.

        The ambient hook stamps `feature: system` on an unattributed call and `system` is not a
        content surface anyone would allowlist — but the guard must hold even if that changes.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.current_attribution", return_value=(None, None)):
            assert not _opted_out(_send(client, recorder))

    def test_a_raw_provider_model_is_never_opted_out(self, monkeypatch) -> None:
        """A direct provider call has no analytics leg to buy anything with.

        Only tier aliases go through the proxy's PostHog logger, so on a raw model the header would
        be a bare content disclosure in exchange for nothing.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        sent = _send(client, recorder, model="gpt-4o-mini",
                     extra_body={"metadata": {"feature": "comment"}})
        assert not _opted_out(sent)

    def test_an_image_prompt_is_never_opted_out(self, monkeypatch) -> None:
        """Image generation shares the `content` feature with the post drafter.

        So an allowlist entry aimed at text would otherwise disclose every render prompt too, for a
        reading no evaluation takes — `$ai_output_choices` is a chat shape. The guard is the body
        shape, not an alias blocklist, so a new non-chat alias is excluded the day it is added.
        """
        monkeypatch.setenv(_ENV, "content")
        client, recorder = _client()
        before = len(recorder.requests)
        try:
            client.images.generate(model="lem-image", prompt="a chart",
                                   extra_body={"metadata": {"feature": "content", "user_id": 7}})
        except Exception:
            pass  # the stub answers with a chat body; the request was already built and sent
        assert not _opted_out(_record(recorder, before))

    def test_an_embedding_is_never_opted_out(self, monkeypatch) -> None:
        """Same reason as the image prompt: `input`, not `messages`, and nothing grades it."""
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        before = len(recorder.requests)
        try:
            client.embeddings.create(model="lem-embedding", input=["a"],
                                     extra_body={"metadata": {"feature": "comment", "user_id": 7}})
        except Exception:
            pass  # stub response; the request was already built and sent
        assert not _opted_out(_record(recorder, before))

    def test_a_hook_failure_leaves_redaction_in_place(self, monkeypatch) -> None:
        """A throwing hook must fail TOWARD redacted, and the call must still go out.

        Observability is never a reason to lose a generation, and never a reason to leak one either.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.prompt_logging_features",
                   side_effect=RuntimeError("boom")):
            headers, body = _comment_call(client, recorder)
        assert _HEADER not in headers
        assert body["metadata"]["feature"] == "comment"

    def test_the_trace_header_still_rides_alongside(self, monkeypatch) -> None:
        """Both hooks write `options.headers`; the second must copy the first, not replace it."""
        from cqc_lem.utilities.observability import llm_trace
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.observability.posthog"):
            with llm_trace("comment_generation", user_id=7, feature="comment") as trace_id:
                headers, _ = _comment_call(client, recorder)
        assert headers.get("x-litellm-trace-id") == trace_id
        assert _HEADER in headers


class TestTheEgressIsLogged:
    def test_releasing_content_leaves_a_line_in_the_log(self, monkeypatch) -> None:
        """Otherwise the only way to learn content is leaving the stack is to go and read PostHog."""
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.log_info") as logged:
            _comment_call(client, recorder)
        assert logged.call_count == 1
        assert logged.call_args.kwargs["feature"] == "comment"

    def test_a_redacted_call_logs_nothing(self, monkeypatch) -> None:
        """One line per RELEASE. A redacted call is the norm and would drown the signal."""
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.log_info") as logged:
            _comment_call(client, recorder, feature="dm")
        logged.assert_not_called()
