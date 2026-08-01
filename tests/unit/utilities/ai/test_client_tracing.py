"""The shared client is what puts a pipeline's trace on the wire (issue #746).

`$ai_trace`/`$ai_span` alone would be an empty skeleton: the generations themselves are emitted by
the LiteLLM proxy, and they only join the trace if the request carried the chain id. Two wires do
that, and each is read from a different place by LiteLLM's PostHog logger — the `x-litellm-trace-id`
header becomes `$ai_trace_id`, `metadata.parent_run_id` becomes `$ai_parent_id`.

Like test_client_attribution.py, these build REAL requests through the OpenAI SDK so an SDK upgrade
that moves the injection point fails CI instead of silently splintering every trace.
"""
import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.unit

_CHAT_RESPONSE = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
}

_TRACE_HEADER = "x-litellm-trace-id"


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


@contextmanager
def _silent_posthog():
    """The trace/span events themselves are asserted in test_llm_traces.py — here they'd just be
    network noise from a real PostHog client."""
    with patch("cqc_lem.utilities.observability.posthog"):
        yield


def _send(client, model="lem-complex", **kwargs):
    try:
        client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}],
                                       **kwargs)
    except Exception:
        pass  # a stub response body only has to be good enough to build and send the request


class TestTraceOnTheWire:
    def test_every_call_in_one_pipeline_carries_the_same_chain_id(self):
        from cqc_lem.utilities.observability import llm_span, llm_trace
        client, recorder = _client()
        with _silent_posthog():
            with llm_trace("post_generation", user_id=7, feature="content") as trace_id:
                with llm_span("draft"):
                    _send(client)
                with llm_span("humanize"):
                    _send(client, model="lem-medium")
        sent = [headers.get(_TRACE_HEADER) for headers, _ in recorder.requests]
        assert sent == [trace_id, trace_id]

    def test_each_call_names_the_span_it_happened_under(self):
        from cqc_lem.utilities.observability import llm_span, llm_trace
        client, recorder = _client()
        with _silent_posthog():
            with llm_trace("post_generation", user_id=7, feature="content"):
                with llm_span("draft") as draft_span:
                    _send(client)
                with llm_span("humanize") as humanize_span:
                    _send(client)
        parents = [body["metadata"]["parent_run_id"] for _, body in recorder.requests]
        assert parents == [draft_span, humanize_span]
        assert draft_span != humanize_span

    def test_a_call_directly_under_the_trace_parents_onto_its_root_span(self):
        from cqc_lem.utilities.observability import llm_trace
        client, recorder = _client()
        with _silent_posthog():
            with llm_trace("post_generation", user_id=7, feature="content"):
                _send(client)
        _, body = recorder.requests[-1]
        assert body["metadata"]["parent_run_id"]

    def test_a_callers_own_metadata_still_joins_the_trace(self):
        """`_call_llm` always sets its own metadata, which is why tracing cannot ride inside the
        attribution hook — it bails out on exactly that case."""
        from cqc_lem.utilities.observability import llm_span, llm_trace
        client, recorder = _client()
        with _silent_posthog():
            with llm_trace("post_generation", user_id=7, feature="content") as trace_id:
                with llm_span("draft") as span_id:
                    _send(client, extra_body={"metadata": {"feature": "content", "user_id": 7}})
        headers, body = recorder.requests[-1]
        assert headers.get(_TRACE_HEADER) == trace_id
        assert body["metadata"] == {"feature": "content", "user_id": 7, "parent_run_id": span_id}

    def test_untraced_calls_are_unchanged(self):
        client, recorder = _client()
        with _silent_posthog():
            _send(client)
        headers, body = recorder.requests[-1]
        assert _TRACE_HEADER not in headers
        assert "parent_run_id" not in body["metadata"]

    def test_raw_provider_models_are_never_traced(self):
        """Only tier aliases go through the proxy's PostHog logger — a direct provider call has
        nothing to join."""
        from cqc_lem.utilities.observability import llm_trace
        client, recorder = _client()
        with _silent_posthog():
            with llm_trace("post_generation", user_id=7):
                _send(client, model="gpt-4o-mini")
        headers, _ = recorder.requests[-1]
        assert _TRACE_HEADER not in headers

    def test_embeddings_join_the_trace_too(self):
        from cqc_lem.utilities.observability import llm_span, llm_trace
        client, recorder = _client()
        with _silent_posthog():
            with llm_trace("post_generation", user_id=7, feature="content") as trace_id:
                with llm_span("review"):
                    try:
                        client.embeddings.create(model="lem-embedding", input=["a"])
                    except Exception:
                        pass  # stub response; the request was already built and sent
        headers, body = recorder.requests[-1]
        assert headers.get(_TRACE_HEADER) == trace_id
        assert body["metadata"]["parent_run_id"]

    def test_a_caller_supplied_chain_id_wins(self):
        from cqc_lem.utilities.observability import llm_trace
        client, recorder = _client()
        with _silent_posthog():
            with llm_trace("post_generation", user_id=7):
                _send(client, extra_headers={_TRACE_HEADER: "caller-owned"})
        headers, _ = recorder.requests[-1]
        assert headers.get(_TRACE_HEADER) == "caller-owned"

    def test_a_tracing_failure_never_breaks_the_call(self):
        client, recorder = _client()
        with patch("cqc_lem.utilities.observability.current_llm_trace",
                   side_effect=RuntimeError("boom")):
            _send(client)
        headers, body = recorder.requests[-1]
        assert _TRACE_HEADER not in headers
        # The generation still went out, fully attributed — only the grouping was lost.
        assert body["metadata"] == {"feature": "system", "user_id": "system"}

    def test_attribution_survives_a_tracing_failure_and_vice_versa(self):
        """Both hooks run per request; one throwing must not take the other down with it."""
        from cqc_lem.utilities.observability import llm_trace
        client, recorder = _client()
        with _silent_posthog():
            with patch("cqc_lem.utilities.ai.client._attach_attribution",
                       side_effect=RuntimeError("boom")):
                with llm_trace("post_generation", user_id=7):
                    _send(client)
        headers, _ = recorder.requests[-1]
        assert headers.get(_TRACE_HEADER)
