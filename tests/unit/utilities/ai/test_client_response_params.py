"""The shared client keeps what the LiteLLM proxy says about each call (issue #2131).

Behind the proxy the response body's `model` echoes the requested tier alias, so the price the proxy
charged lives only in its response headers. These build REAL requests through the OpenAI SDK, like
test_client_tracing.py, so an SDK upgrade that moves `_process_response` fails CI instead of quietly
reverting the cost ledger to alias pricing.
"""
import httpx
import pytest

pytestmark = pytest.mark.unit

_CHAT_RESPONSE = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "lem-medium",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
}


def _create(headers: dict):
    from cqc_lem.utilities.ai.client import AttributedOpenAI
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_CHAT_RESPONSE,
                                                                   headers=headers))
    client = AttributedOpenAI(api_key="k", base_url="http://litellm:4000", max_retries=0,
                              http_client=httpx.Client(transport=transport))
    return client.chat.completions.create(model="lem-medium",
                                          messages=[{"role": "user", "content": "hi"}])


def test_proxy_headers_land_on_the_parsed_response():
    response = _create({"x-litellm-response-cost": "0.00042",
                        "x-litellm-model-id": "deployment-abc",
                        "x-litellm-model-api-base": "https://ollama.com/v1"})

    assert response._hidden_params == {"response_cost": "0.00042", "model_id": "deployment-abc",
                                       "api_base": "https://ollama.com/v1"}
    # The parsed body is untouched — the headers only add to it.
    assert response.choices[0].message.content == "hi"
    assert response.model == "lem-medium"


def test_a_zero_cost_header_is_kept():
    """$0 is the proxy's real price for a subscription-served Ollama call, not a missing value."""
    response = _create({"x-litellm-response-cost": "0.0"})

    from cqc_lem.utilities.observability import llm_response_cost
    assert llm_response_cost(response) == 0.0


def test_no_proxy_headers_leaves_the_response_alone():
    response = _create({})

    assert getattr(response, "_hidden_params", None) is None
    from cqc_lem.utilities.observability import llm_response_cost
    assert llm_response_cost(response) is None


def test_existing_hidden_params_are_not_overwritten():
    from types import SimpleNamespace

    from cqc_lem.utilities.ai.client import _attach_proxy_response_params
    result = SimpleNamespace(_hidden_params={"cache_hit": True})
    _attach_proxy_response_params(result, httpx.Response(200, headers={"x-litellm-response-cost": "1"}))
    assert result._hidden_params == {"cache_hit": True}


def test_a_header_copy_failure_never_costs_the_generation(monkeypatch):
    from cqc_lem.utilities.ai import client as client_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("renamed SDK attribute")

    monkeypatch.setattr(client_module, "_attach_proxy_response_params", boom)
    response = _create({"x-litellm-response-cost": "0.1"})
    assert response.choices[0].message.content == "hi"
