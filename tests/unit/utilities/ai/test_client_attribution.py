"""Every request the shared client sends through the LiteLLM proxy carries who/what it is for, so
the proxy-side `$ai_generation` lands on the right PostHog person and the cost-routing hook can find
the call's bucket (issue #647).

These build REAL requests through the OpenAI SDK rather than asserting on a mocked `create()`: the
injection point is an SDK internal, and an SDK upgrade that moves it must fail here rather than
silently drop attribution in production.
"""
import json
from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.unit

# Imported inside the helpers, not at module scope: importing the client module CONSTRUCTS the
# shared client, which needs an API key that the session-scoped env fixture only sets after
# collection.

_CHAT_RESPONSE = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
}


class _Recorder(httpx.BaseTransport):
    def __init__(self):
        self.bodies = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_CHAT_RESPONSE)


def _client() -> tuple:
    from cqc_lem.utilities.ai.client import AttributedOpenAI
    recorder = _Recorder()
    client = AttributedOpenAI(api_key="k", base_url="http://litellm:4000", max_retries=0,
                              http_client=httpx.Client(transport=recorder))
    return client, recorder


def _sent_metadata(model="lem-complex", attribution=(7, "content"), **kwargs):
    client, recorder = _client()
    with patch("cqc_lem.utilities.observability.current_llm_attribution", return_value=attribution):
        try:
            client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}],
                                           **kwargs)
        except Exception:  # a stub response body only has to be good enough to build the request
            pass
    assert recorder.bodies, "no request was sent"
    return recorder.bodies[-1].get("metadata")


class TestAttributionMetadata:
    def test_scope_user_and_feature_ride_along(self):
        assert _sent_metadata() == {"feature": "content", "user_id": 7}

    def test_unattributed_calls_use_the_system_sentinel(self):
        """No user must NOT mean no distinct_id — PostHog would mint an anonymous person per call."""
        assert _sent_metadata(attribution=(None, None)) == {"feature": "system", "user_id": "system"}

    def test_raw_provider_models_are_not_tagged(self):
        assert _sent_metadata(model="gpt-4o-mini") is None

    def test_a_callers_own_metadata_wins(self):
        """_call_llm's explicit _track_user_id/_track_feature arrive this way and must not be lost."""
        sent = _sent_metadata(extra_body={"metadata": {"feature": "newsletter", "user_id": 9}})
        assert sent == {"feature": "newsletter", "user_id": 9}

    def test_a_callers_other_extra_body_fields_survive(self):
        client, recorder = _client()
        with patch("cqc_lem.utilities.observability.current_llm_attribution", return_value=(3, "dm")):
            try:
                client.chat.completions.create(model="lem-simple",
                                               messages=[{"role": "user", "content": "hi"}],
                                               extra_body={"tags": ["a"]})
            except Exception:
                pass  # Expected: mocked network raises; metadata was already stamped.
        body = recorder.bodies[-1]
        assert body["tags"] == ["a"]
        assert body["metadata"] == {"feature": "dm", "user_id": 3}

    def test_attribution_failure_never_breaks_the_call(self):
        client, recorder = _client()
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   side_effect=RuntimeError("boom")):
            try:
                client.chat.completions.create(model="lem-complex",
                                               messages=[{"role": "user", "content": "hi"}])
            except Exception:
                pass  # Expected: mocked network raises; metadata fell back to system sentinel.
        # The generation still went out; it just carries the sentinel instead of a user.
        assert recorder.bodies[-1]["metadata"] == {"feature": "system", "user_id": "system"}

    def test_embeddings_are_attributed_too(self):
        """Embeddings bypass _call_llm entirely (comment dedup, feedback clustering) — the client is
        the only thing standing between them and an anonymous $ai_embedding."""
        client, recorder = _client()
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   return_value=(5, "comment")):
            try:
                client.embeddings.create(model="lem-embedding", input=["a"])
            except Exception:
                pass  # Expected: mocked network raises; metadata was already stamped.
        assert recorder.bodies[-1]["metadata"] == {"feature": "comment", "user_id": 5}


class TestTheSharedClient:
    """The tests above build their own AttributedOpenAI, so they all still pass if the module-level
    `client` — the ONE instance every AI helper imports — is ever rebuilt as a plain `OpenAI()`.
    That regression would silently strip distinct_id + feature off every proxy event and leave no
    other trace, which is exactly the failure this design exists to prevent. Pin it here."""

    def test_the_shared_client_is_the_attributed_one(self):
        from cqc_lem.utilities.ai.client import AttributedOpenAI, client
        assert isinstance(client, AttributedOpenAI)

    def test_the_shared_client_stamps_the_request_it_actually_builds(self):
        """Driven through the real singleton's own request builder — no network, no substitute
        client — so the assertion covers the object production uses."""
        from openai._models import FinalRequestOptions

        from cqc_lem.utilities.ai.client import client
        options = FinalRequestOptions.construct(
            method="post", url="/chat/completions",
            json_data={"model": "lem-medium", "messages": [{"role": "user", "content": "hi"}]},
        )
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   return_value=(11, "newsletter")):
            request = client._build_request(options)
        assert json.loads(request.content)["metadata"] == {"feature": "newsletter", "user_id": 11}


class TestAttributionMetadataShape:
    def test_zero_is_a_user_id_not_a_missing_one(self):
        from cqc_lem.utilities.ai.client import attribution_metadata
        assert attribution_metadata(0, "content")["user_id"] == 0

    def test_the_sentinel_matches_the_server_side_distinct_id_convention(self):
        from cqc_lem.utilities.ai.client import attribution_metadata
        from cqc_lem.utilities.observability import FEATURE_SYSTEM
        meta = attribution_metadata(None, None)
        # observability.py captures system events as distinct_id=str(user_id or "system").
        assert str(meta["user_id"]) == "system"
        assert meta["feature"] == FEATURE_SYSTEM
