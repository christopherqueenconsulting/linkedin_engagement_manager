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
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.unit

_HEADER = "litellm-disable-message-redaction"
_ENV = "LLM_PROMPT_LOGGING_FEATURES"

# One stub per endpoint, keyed on path, so NOTHING here relies on a raised parse error to get past
# the response. A test that swallows exceptions cannot tell an expected stub mismatch from a real
# client-side regression, and this file is a data-egress control's only coverage.
_RESPONSES: dict[str, Any] = {
    "/chat/completions": {
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}],
    },
    "/embeddings": {
        "object": "list", "model": "m",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    },
    "/images/generations": {"created": 0, "data": [{"url": "http://example.invalid/a.png"}]},
}


class _Recorder(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[tuple[httpx.Headers, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.headers, body))
        for path, response in _RESPONSES.items():
            if request.url.path.endswith(path):
                return httpx.Response(200, json=response)
        raise AssertionError(f"no stub response for {request.url.path}")


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the warn-once / announce-once latches between tests.

    They are module globals, so without this the tests couple to each other's ordering — one that
    writes a bad name into the set silences a later assertion, and nobody would remember why.
    """
    from cqc_lem.utilities.ai import client as mod
    for name in ("_UNKNOWN_FEATURES_WARNED", "_ALLOWLISTS_ANNOUNCED", "_HOOK_FAILURES_WARNED",
                 "_HEADER_STRIPPED_WARNED"):
        monkeypatch.setattr(mod, name, set())


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
    client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}],
                                   **kwargs)
    return _record(recorder, before)


def _comment_call(client: Any, recorder: _Recorder,
                  feature: str = "comment") -> tuple[httpx.Headers, Any]:
    return _send(client, recorder, extra_body={"metadata": {"feature": feature, "user_id": 7}})


def _opted_out(sent: tuple[httpx.Headers, Any]) -> bool:
    headers, _ = sent
    return _HEADER in headers


def _release_lines(logged: Any) -> list[Any]:
    """The per-call egress lines only.

    `prompt_logging_features` also announces a non-empty allowlist once per process, and counting
    both together would let either line satisfy an assertion meant for the other.
    """
    return [call for call in logged.call_args_list if "feature" in call.kwargs]


class TestTheAllowlistDecidesPerRequest:
    def test_an_allowlisted_feature_opts_this_request_out_of_redaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        assert _opted_out(_comment_call(client, recorder))

    def test_every_other_feature_stays_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point of scoping.

        Grading the comment drafter must not also ship draft DMs, newsletter editions and post
        drafts, none of which has an evaluation waiting on it.

        Note what allowlisting `comment` DOES disclose, because "scoped by feature" is not the same
        as "scoped by content class": the comment prompt embeds the user's profile synthesis as its
        voice reference AND the target post's full body, which is a third party's text. That is
        written down in docs/llm-analytics.md and on #1832 so the owner decision is made on it.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        for feature in ("dm", "content", "newsletter", "system", "marketing"):
            assert not _opted_out(_comment_call(client, recorder, feature=feature)), feature

    def test_an_unset_allowlist_redacts_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """It ships EMPTY, so merging the mechanism releases no MESSAGES that were not already going.

        Not the same as "nothing leaves the stack": the model's own `reasoning` escapes the proxy's
        redaction entirely and reaches PostHog today (#1831). This control does not widen that, and
        does not close it either. Turning the allowlist on is an owner decision — issue #1832.
        """
        monkeypatch.delenv(_ENV, raising=False)
        client, recorder = _client()
        assert not _opted_out(_comment_call(client, recorder))

    def test_an_emptied_allowlist_closes_it_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The operator's off switch — an `.env` edit, not a file edit and a broken test."""
        monkeypatch.setenv(_ENV, "  ,  ")
        client, recorder = _client()
        assert not _opted_out(_comment_call(client, recorder))

    def test_the_value_is_read_per_call_not_at_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, recorder = _client()
        monkeypatch.setenv(_ENV, "comment")
        assert _opted_out(_comment_call(client, recorder))
        monkeypatch.setenv(_ENV, "")
        assert not _opted_out(_comment_call(client, recorder))

    def test_spacing_and_case_in_the_env_value_are_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Typed by hand into `.env`; `Comment, dm` must not silently mean nothing is allowlisted."""
        monkeypatch.setenv(_ENV, " Comment , dm ")
        client, recorder = _client()
        assert _opted_out(_comment_call(client, recorder, feature="COMMENT"))

    @pytest.mark.parametrize("feature", ["comment", "content", "dm", "newsletter", "marketing"])
    def test_the_mechanism_is_feature_generic(self, feature: str,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing about it is comment-specific — `comment` is just the only one with an evaluation.

        Pinned so that whichever feature #1832 lands on works without a code change, and so that a
        future edit cannot quietly special-case one.
        """
        monkeypatch.setenv(_ENV, feature)
        client, recorder = _client()
        assert _opted_out(_comment_call(client, recorder, feature=feature))

    def test_the_header_value_is_one_the_proxy_reads_as_true(self,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
        """Presence is not enough — pin the value.

        LiteLLM matches `bool(request_headers.get(...))`, so an empty value is falsy and a refactor
        emitting one would pass a presence-only assertion while silently still redacting.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        headers, _ = _comment_call(client, recorder)
        assert headers[_HEADER] == "true"


class TestTheAllowlistIsAuthoritative:
    def test_the_mirrored_feature_names_match_observability(self) -> None:
        """The mirror in `client.py` must not be able to drift.

        That module cannot import observability — it would drag the DB and PostHog into every
        `from ...ai.client import client` — so the feature names are copied. A drifted copy either
        warns about a real feature or drops one that should have been allowlistable.
        """
        from cqc_lem.utilities import observability
        from cqc_lem.utilities.ai.client import _KNOWN_FEATURES
        real = {value for name, value in vars(observability).items()
                if name.startswith("FEATURE_") and isinstance(value, str)}
        assert set(_KNOWN_FEATURES) == real

    def test_an_unrecognised_name_allowlists_nothing_and_is_warned_once(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo must be loud, because its silent version reads as a broken mechanism.

        `comment_generation` is the TRACE name and `comments` is the plural; both are easy to type
        and neither matches anything.
        """
        monkeypatch.setenv(_ENV, "comment_generation, comments")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.log_warning") as warned:
            assert not _opted_out(_comment_call(client, recorder))
            assert not _opted_out(_comment_call(client, recorder))
        assert warned.call_count == 1, "once per name per process, not once per LLM call"
        assert "comment_generation" in warned.call_args.args[0]

    def test_a_good_name_alongside_a_typo_still_works(self,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, "comment,comments")
        client, recorder = _client()
        assert _opted_out(_comment_call(client, recorder))

    def test_a_caller_supplied_value_is_overwritten_and_still_logged(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """There must be NO release path that skips the audit line.

        Leaving a caller's header alone would forward an unnormalised value (LiteLLM reads "false"
        as truthy) and emit nothing — content leaving the stack with no record, on the one branch
        that exists to prevent exactly that.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.log_info") as logged:
            headers, _ = _send(client, recorder, extra_headers={_HEADER: "false"},
                               extra_body={"metadata": {"feature": "comment", "user_id": 7}})
        assert headers[_HEADER] == "true"
        assert len(_release_lines(logged)) == 1

    def test_a_call_site_cannot_opt_itself_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The env allowlist is the control, so a header set at a call site is STRIPPED, not honoured.

        Nothing in LEM sets it today; this is what keeps that true. Warned rather than logged at
        DEBUG because a call site reaching around a data-egress control is a defect.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.log_warning") as warned:
            sent = _send(client, recorder, extra_headers={_HEADER: "true"},
                         extra_body={"metadata": {"feature": "dm", "user_id": 7}})
            _send(client, recorder, extra_headers={_HEADER: "true"},
                  extra_body={"metadata": {"feature": "dm", "user_id": 7}})
        assert not _opted_out(sent)
        assert warned.call_count == 1, "latched like every other warning here — this is a per-call path"


class TestItFailsClosed:
    def test_a_call_with_no_attribution_at_all_stays_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A request with no attribution can never be allowlisted.

        The ambient hook stamps `feature: system` on an unattributed call and `system` is not a
        content surface anyone would allowlist — but the guard must hold even if that changes.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.current_attribution", return_value=(None, None)):
            assert not _opted_out(_send(client, recorder))

    def test_a_raw_provider_model_is_never_opted_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A direct provider call has no analytics leg to buy anything with.

        Only tier aliases go through the proxy's PostHog logger, so on a raw model the header would
        be a bare content disclosure in exchange for nothing.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        sent = _send(client, recorder, model="gpt-4o-mini",
                     extra_body={"metadata": {"feature": "comment"}})
        assert not _opted_out(sent)

    def test_an_image_prompt_is_never_opted_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Image generation shares the `content` feature with the post drafter.

        So an allowlist entry aimed at text would otherwise disclose every render prompt too, for a
        reading no evaluation takes — `$ai_output_choices` is a chat shape. The guard is the body
        shape, not an alias blocklist, so a new non-chat alias is excluded the day it is added.
        """
        monkeypatch.setenv(_ENV, "content")
        client, recorder = _client()
        before = len(recorder.requests)
        client.images.generate(model="lem-image", prompt="a chart",
                               extra_body={"metadata": {"feature": "content", "user_id": 7}})
        assert not _opted_out(_record(recorder, before))

    def test_an_embedding_is_never_opted_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same reason as the image prompt: `input`, not `messages`, and nothing grades it."""
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        before = len(recorder.requests)
        client.embeddings.create(model="lem-embedding", input=["a"],
                                 extra_body={"metadata": {"feature": "comment", "user_id": 7}})
        assert not _opted_out(_record(recorder, before))

    def test_a_multipart_upload_is_never_opted_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A `files` request has no JSON `messages` to grade, and its payload is an uploaded FILE.

        Transcription is the live example. It carries the same `feature` as whatever pipeline is
        running, so without this guard an allowlist aimed at text would ride along with the upload.
        """
        from cqc_lem.utilities.ai.client import _allowlisted_feature
        monkeypatch.setenv(_ENV, "comment")
        options = SimpleNamespace(json_data={"model": "lem-medium",
                                             "messages": [{"role": "user", "content": "hi"}],
                                             "metadata": {"feature": "comment", "user_id": 7}},
                                  files=[("file", b"audio")], headers={})
        assert _allowlisted_feature(options, options.json_data) is None

    def test_a_real_headers_mapping_is_added_to_rather_than_replaced(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller's own headers must survive the one this hook adds.

        Everywhere else in this file `options.headers` is the SDK's falsy `NOT_GIVEN` default, so a
        bug that only handles the empty case would pass. Drive a real `httpx.Headers` through.
        """
        from cqc_lem.utilities.ai.client import _attach_prompt_logging
        monkeypatch.setenv(_ENV, "comment")
        options = SimpleNamespace(json_data={"model": "lem-medium",
                                             "messages": [{"role": "user", "content": "hi"}],
                                             "metadata": {"feature": "comment", "user_id": 7}},
                                  files=None, headers=httpx.Headers({"x-caller": "keep-me"}))
        _attach_prompt_logging(options)
        assert options.headers[_HEADER] == "true"
        assert options.headers["x-caller"] == "keep-me"

    def test_an_unreadable_headers_object_is_left_completely_alone(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If it cannot be READ it must not be WRITTEN: replacing it would drop Authorization.

        The SDK does not produce this today, and that is exactly why it needs pinning — the failure
        would be an unauthenticated call, not a redaction bug, and nothing else would say why.
        """
        from cqc_lem.utilities.ai.client import _attach_prompt_logging
        monkeypatch.setenv(_ENV, "comment")
        sentinel = object()
        options = SimpleNamespace(json_data={"model": "lem-medium",
                                             "messages": [{"role": "user", "content": "hi"}],
                                             "metadata": {"feature": "comment", "user_id": 7}},
                                  files=None, headers=sentinel)
        _attach_prompt_logging(options)
        assert options.headers is sentinel

    def test_a_hook_failure_leaves_redaction_in_place_and_says_so(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A throwing hook must fail TOWARD redacted, the call must still go out, and it must WARN.

        Observability is never a reason to lose a generation, and never a reason to leak one either
        — but a hook that has quietly started throwing produces evaluation scores that stay constant
        forever, which is the failure this whole control exists to end.
        """
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.prompt_logging_features",
                   side_effect=RuntimeError("boom")):
            with patch("cqc_lem.utilities.ai.client.log_warning") as warned:
                headers, body = _comment_call(client, recorder)
                _comment_call(client, recorder)
        assert _HEADER not in headers
        assert body["metadata"]["feature"] == "comment"
        assert warned.call_count == 1, "once per hook per process, not once per LLM call"
        assert "_attach_prompt_logging" in warned.call_args.args[0]

    def test_the_trace_header_still_rides_alongside(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    def test_releasing_content_leaves_a_line_in_the_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Otherwise the only way to learn content is leaving the stack is to go and read PostHog."""
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.log_info") as logged:
            _comment_call(client, recorder)
        released = _release_lines(logged)
        assert len(released) == 1
        assert released[0].kwargs["feature"] == "comment"
        # WHOSE material left, not just that some did — a deletion or subject-access request has to
        # be answerable from these lines alone.
        assert released[0].kwargs["user_id"] == 7

    def test_a_redacted_call_logs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One line per RELEASE. A redacted call is the norm and would drown the signal."""
        monkeypatch.setenv(_ENV, "comment")
        client, recorder = _client()
        with patch("cqc_lem.utilities.ai.client.log_info") as logged:
            _comment_call(client, recorder, feature="dm")
        assert _release_lines(logged) == []

    def test_the_real_logger_accepts_every_kwarg_this_module_sends(self) -> None:
        """Anti-vacuity: every other assertion here PATCHES the logger, which cannot prove a signature.

        A kwarg the real logger rejects would raise inside `_build_request`'s except block — the one
        place an exception escapes and costs the generation.
        """
        from cqc_lem.utilities.logger import log_info, log_warning
        log_info("prompt-logging signature probe", feature="comment", model="lem-medium",
                 user_id=7, api_provider="litellm")
        log_warning("prompt-logging signature probe", exc=RuntimeError("probe"),
                    api_provider="litellm")
