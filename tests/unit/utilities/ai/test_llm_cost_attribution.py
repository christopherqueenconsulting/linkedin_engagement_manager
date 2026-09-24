"""Unit tests for the cost-attribution dimensions threaded through ai_helper._call_llm."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_OBS = "cqc_lem.utilities.observability"


def _response(cache_hit: bool = False):
    resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=30, completion_tokens=70))
    if cache_hit:
        resp._hidden_params = {"cache_hit": True}
    return resp


def _call(**kwargs):
    """Call _call_llm with a mocked LiteLLM client; returns (create_mock, track_mock)."""
    from cqc_lem.utilities.ai.ai_helper import _call_llm
    client = MagicMock()
    client.chat.completions.create.return_value = kwargs.pop("_response", _response())
    with patch(f"{_AI}.client", client), patch(f"{_OBS}.track_llm_call") as track:
        _call_llm(**kwargs)
    return client.chat.completions.create, track


class TestCallLlmAttribution:
    def test_explicit_track_kwargs_are_emitted_and_not_forwarded(self):
        create, track = _call(model="lem-medium", messages=[{"role": "user", "content": "hi"}],
                              _track_user_id=5, _track_feature="dm")

        forwarded = create.call_args[1]
        assert "_track_user_id" not in forwarded and "_track_feature" not in forwarded
        assert forwarded["model"] == "lem-medium"
        kwargs = track.call_args[1]
        assert kwargs["user_id"] == 5
        assert kwargs["feature"] == "dm"
        assert kwargs["prompt_tokens"] == 30
        assert kwargs["completion_tokens"] == 70
        assert kwargs["success"] is True

    def test_falls_back_to_the_ambient_attribution_scope(self):
        from cqc_lem.utilities.observability import llm_attribution
        with llm_attribution(user_id=12, feature="comment"):
            _, track = _call(model="lem-simple", messages=[])

        kwargs = track.call_args[1]
        assert kwargs["user_id"] == 12
        assert kwargs["feature"] == "comment"

    def test_explicit_kwargs_win_over_the_ambient_scope(self):
        from cqc_lem.utilities.observability import llm_attribution
        with llm_attribution(user_id=12, feature="comment"):
            _, track = _call(model="lem-simple", messages=[], _track_user_id=99,
                             _track_feature="newsletter")

        kwargs = track.call_args[1]
        assert kwargs["user_id"] == 99
        assert kwargs["feature"] == "newsletter"

    def test_unattributed_call_reports_the_system_feature(self):
        _, track = _call(model="lem-simple", messages=[])
        assert track.call_args[1]["feature"] == "system"
        assert track.call_args[1]["user_id"] is None

    def test_cache_hit_is_reported(self):
        _, track = _call(model="lem-medium", messages=[], _response=_response(cache_hit=True))
        assert track.call_args[1]["cached"] is True

    def test_uncached_call_reports_cached_false(self):
        _, track = _call(model="lem-medium", messages=[])
        assert track.call_args[1]["cached"] is False

    def test_failed_call_still_carries_attribution(self):
        from cqc_lem.utilities.ai.ai_helper import _call_llm
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("provider down")
        with patch(f"{_AI}.client", client), patch(f"{_OBS}.track_llm_call") as track:
            with pytest.raises(RuntimeError):
                _call_llm(model="lem-complex", messages=[], _track_user_id=8,
                          _track_feature="content")

        kwargs = track.call_args[1]
        assert kwargs["success"] is False
        assert kwargs["user_id"] == 8
        assert kwargs["feature"] == "content"

    def test_tracking_failure_never_breaks_the_llm_call(self):
        from cqc_lem.utilities.ai.ai_helper import _call_llm
        client = MagicMock()
        client.chat.completions.create.return_value = _response()
        with patch(f"{_AI}.client", client), \
             patch(f"{_OBS}.track_llm_call", side_effect=RuntimeError("posthog down")):
            assert _call_llm(model="lem-simple", messages=[]) is not None


class TestServingModelCapture:
    def test_serving_model_from_response_model_is_passed_to_tracker(self):
        resp = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=70),
            model="openai/gpt-4o-mini",
        )
        _, track = _call(model="lem-simple", messages=[], _response=resp)

        kwargs = track.call_args[1]
        assert kwargs["serving_model"] == "openai/gpt-4o-mini"
        assert kwargs["model_tier"] == "lem-simple"
        assert kwargs["model"] == "lem-simple"  # requested alias

    def test_serving_model_from_hidden_params_when_response_model_missing(self):
        resp = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=70),
            _hidden_params={"model": "openai/gpt-4o"},
        )
        _, track = _call(model="lem-complex", messages=[], _response=resp)

        kwargs = track.call_args[1]
        assert kwargs["serving_model"] == "openai/gpt-4o"

    def test_down_routed_call_passes_serving_model_separate_from_tier(self):
        """A lem-complex call that fell back to gpt-4o-mini still reports the original tier."""
        resp = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=70),
            model="openai/gpt-4o-mini",
        )
        _, track = _call(model="lem-complex", messages=[], _response=resp)

        kwargs = track.call_args[1]
        assert kwargs["serving_model"] == "openai/gpt-4o-mini"
        assert kwargs["model_tier"] == "lem-complex"

    def test_ollama_serving_model_is_passed_to_tracker(self):
        resp = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=70),
            model="openai/qwen3.5:397b",
        )
        _, track = _call(model="lem-complex", messages=[], _response=resp)

        kwargs = track.call_args[1]
        assert kwargs["serving_model"] == "openai/qwen3.5:397b"
        assert kwargs["model_tier"] == "lem-complex"

    def test_bare_ollama_model_name_is_passed_to_tracker(self):
        resp = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=70),
            model="qwen3.5:397b",
        )
        _, track = _call(model="lem-complex", messages=[], _response=resp)

        kwargs = track.call_args[1]
        assert kwargs["serving_model"] == "qwen3.5:397b"

    def test_serving_model_missing_falls_back_to_requested_alias(self):
        resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=30, completion_tokens=70))
        _, track = _call(model="lem-simple", messages=[], _response=resp)

        kwargs = track.call_args[1]
        assert kwargs["serving_model"] == "lem-simple"
        assert kwargs["model_tier"] == "lem-simple"

    def test_failed_call_reports_requested_alias_as_serving_model(self):
        from cqc_lem.utilities.ai.ai_helper import _call_llm
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("provider down")
        with patch(f"{_AI}.client", client), patch(f"{_OBS}.track_llm_call") as track:
            with pytest.raises(RuntimeError):
                _call_llm(model="lem-complex", messages=[], _track_user_id=8)

        kwargs = track.call_args[1]
        assert kwargs["model"] == "lem-complex"
        # On failure there is no response to extract a serving model from; the function
        # intentionally passes the requested alias as the model argument so cost attribution
        # falls back to the tier rate.
        assert kwargs.get("serving_model", kwargs["model"]) == "lem-complex"


class TestProxyResponseCost:
    """Issue #2131: the proxy's own price reaches the tracker.

    So the ledger books what `$ai_generation` reports instead of the tier alias's list rate.
    """

    def test_hidden_params_response_cost_is_passed_to_tracker(self):
        resp = _response()
        resp._hidden_params = {"response_cost": "0.0"}
        _, track = _call(model="lem-medium", messages=[], _response=resp)
        assert track.call_args[1]["response_cost"] == 0.0

    def test_no_proxy_price_passes_none_so_the_estimate_applies(self):
        _, track = _call(model="lem-medium", messages=[])
        assert track.call_args[1]["response_cost"] is None

    def test_the_header_reaches_the_tracker_through_the_real_client(self):
        """End to end through a REAL `AttributedOpenAI`.

        An SDK upgrade that stops routing responses through `_process_response` would silently
        revert the ledger to alias pricing.
        """
        import httpx

        from cqc_lem.utilities.ai.ai_helper import _call_llm
        from cqc_lem.utilities.ai.client import AttributedOpenAI

        body = {"id": "x", "object": "chat.completion", "created": 0, "model": "lem-medium",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000},
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                             "finish_reason": "stop"}]}
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json=body, headers={"x-litellm-response-cost": "0.0"}))
        real = AttributedOpenAI(api_key="k", base_url="http://litellm:4000", max_retries=0,
                                http_client=httpx.Client(transport=transport))
        with patch(f"{_AI}.client", real), patch(f"{_OBS}.track_llm_call") as track, \
                patch(f"{_OBS}.posthog"):
            _call_llm(model="lem-medium", messages=[{"role": "user", "content": "hi"}])

        kwargs = track.call_args[1]
        assert kwargs["response_cost"] == 0.0
        assert kwargs["model_tier"] == "lem-medium"
