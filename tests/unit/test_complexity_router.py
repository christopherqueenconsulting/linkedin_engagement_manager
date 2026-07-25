"""Unit tests for the LiteLLM pre-call hook `.litellm/complexity_router.py` (issue #494).

The hook lives outside the package (it is mounted into the LiteLLM container, which has no LEM
install), so it is loaded here by path with a stubbed `litellm` — that library is a container
dependency, not one of ours.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROUTER_PATH = Path(__file__).resolve().parents[2] / ".litellm" / "complexity_router.py"


@pytest.fixture
def router_module(monkeypatch):
    """Import the hook fresh with `litellm` stubbed, and with the shared decision core importable
    the same way the container sees it (a bare `routing_policy` next to the hook)."""
    litellm = types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:
        pass

    custom_logger.CustomLogger = CustomLogger
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm.integrations", integrations)
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", custom_logger)

    from cqc_lem.utilities import routing_policy
    monkeypatch.setitem(sys.modules, "routing_policy", routing_policy)

    spec = importlib.util.spec_from_file_location("lem_complexity_router", ROUTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy(cohort_pct=1.0, state="experiment", enabled=True):
    return {"version": 1, "enabled": enabled, "buckets": {
        "content:lem-complex": {"id": "content:lem-complex#1", "feature": "content",
                                "from_tier": "lem-complex", "to_tier": "lem-medium",
                                "state": state, "cohort_pct": cohort_pct}}}


async def _route(router, model, messages=None, metadata=None):
    data = {"model": model, "messages": messages or [{"role": "user", "content": "hi"}]}
    if metadata is not None:
        data["metadata"] = metadata
    return (await router.async_pre_call_hook(None, None, data, "completion"))["model"]


# ── stage 1: complexity routing is unchanged ──

@pytest.mark.parametrize("content,expected", [
    ("write a thought leadership post with a framework", "lem-complex"),
    ("write a personal story", "lem-medium"),
    ("refine this", "lem-simple"),
])
async def test_router_model_resolves_by_complexity(router_module, content, expected):
    router = router_module.LEMComplexityRouter()
    assert await _route(router, "lem-router", [{"role": "user", "content": content}]) == expected


async def test_non_tier_models_are_never_rewritten(router_module):
    router = router_module.LEMComplexityRouter()
    assert await _route(router, "lem-image") == "lem-image"
    assert await _route(router, "gpt-4o") == "gpt-4o"


# ── stage 2: cost-aware down-routing ──

async def test_explicit_tier_is_untouched_while_the_flag_is_off(router_module, monkeypatch):
    monkeypatch.delenv("COST_AWARE_ROUTING_ENABLED", raising=False)
    router = router_module.LEMComplexityRouter()
    router._policy, router._policy_fetched_at = _policy(), float("inf")
    assert await _route(router, "lem-complex", metadata={"feature": "content", "user_id": 1}) \
        == "lem-complex"


async def test_flag_on_downroutes_the_treatment_cohort(router_module, monkeypatch):
    monkeypatch.setenv("COST_AWARE_ROUTING_ENABLED", "true")
    router = router_module.LEMComplexityRouter()
    router._policy, router._policy_fetched_at = _policy(), float("inf")
    assert await _route(router, "lem-complex", metadata={"feature": "content", "user_id": 1}) \
        == "lem-medium"


async def test_flag_on_also_downroutes_the_complexity_result(router_module, monkeypatch):
    monkeypatch.setenv("COST_AWARE_ROUTING_ENABLED", "true")
    router = router_module.LEMComplexityRouter()
    router._policy, router._policy_fetched_at = _policy(), float("inf")
    complex_prompt = [{"role": "user", "content": "thought leadership framework"}]
    assert await _route(router, "lem-router", complex_prompt,
                        metadata={"feature": "content", "user_id": 1}) == "lem-medium"


@pytest.mark.parametrize("metadata", [None, {}, {"feature": "comment", "user_id": 1},
                                      {"feature": "content"}])
async def test_calls_outside_the_bucket_keep_their_tier(router_module, monkeypatch, metadata):
    """No metadata, another feature, or no user id — none of them may join the experiment."""
    monkeypatch.setenv("COST_AWARE_ROUTING_ENABLED", "true")
    router = router_module.LEMComplexityRouter()
    router._policy, router._policy_fetched_at = _policy(), float("inf")
    assert await _route(router, "lem-complex", metadata=metadata) == "lem-complex"


async def test_a_rolled_back_policy_stops_routing_down(router_module, monkeypatch):
    monkeypatch.setenv("COST_AWARE_ROUTING_ENABLED", "true")
    router = router_module.LEMComplexityRouter()
    router._policy, router._policy_fetched_at = _policy(state="rolled_back"), float("inf")
    assert await _route(router, "lem-complex", metadata={"feature": "content", "user_id": 1}) \
        == "lem-complex"


async def test_a_broken_decision_core_fails_open(router_module, monkeypatch):
    monkeypatch.setenv("COST_AWARE_ROUTING_ENABLED", "true")
    router = router_module.LEMComplexityRouter()
    router._policy, router._policy_fetched_at = _policy(), float("inf")
    with patch.object(router_module.routing_policy, "resolve_tier",
                      side_effect=RuntimeError("boom")):
        assert await _route(router, "lem-complex", metadata={"feature": "content", "user_id": 1}) \
            == "lem-complex"


# ── policy fetching ──

def test_policy_is_read_from_redis_and_cached(router_module, monkeypatch):
    monkeypatch.setenv("COST_ROUTING_REDIS_URL", "redis://redis:6379/0")
    client = MagicMock()
    client.get.return_value = json.dumps(_policy()).encode()
    redis_module = types.ModuleType("redis")
    redis_module.Redis = MagicMock(from_url=MagicMock(return_value=client))
    monkeypatch.setitem(sys.modules, "redis", redis_module)

    router = router_module.LEMComplexityRouter()
    assert router._current_policy()["enabled"] is True
    router._current_policy()
    client.get.assert_called_once_with(router_module.routing_policy.POLICY_REDIS_KEY)


@pytest.mark.parametrize("value", [None, b"", b"{not json"])
def test_unreadable_policy_degrades_to_no_policy(router_module, monkeypatch, value):
    client = MagicMock()
    client.get.return_value = value
    redis_module = types.ModuleType("redis")
    redis_module.Redis = MagicMock(from_url=MagicMock(return_value=client))
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    assert router_module.LEMComplexityRouter()._current_policy() == {}


def test_redis_failure_degrades_to_no_policy(router_module, monkeypatch):
    redis_module = types.ModuleType("redis")
    redis_module.Redis = MagicMock(from_url=MagicMock(side_effect=RuntimeError("no redis")))
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    assert router_module.LEMComplexityRouter()._current_policy() == {}


def test_redis_url_prefers_the_shared_override(router_module, monkeypatch):
    monkeypatch.setenv("COST_ROUTING_REDIS_URL", "redis://shared:6379/2")
    monkeypatch.setenv("CELERY_BROKER_URL", "sqs://queue")
    assert router_module._redis_url() == "redis://shared:6379/2"
    monkeypatch.delenv("COST_ROUTING_REDIS_URL")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
    assert router_module._redis_url() == "redis://broker:6379/0"
    monkeypatch.setenv("CELERY_BROKER_URL", "sqs://queue")
    assert router_module._redis_url() == "redis://redis:6379/0"


@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("on", True),
                                            ("false", False), ("", False), ("maybe", False)])
def test_flag_parsing(router_module, monkeypatch, value, expected):
    monkeypatch.setenv("COST_AWARE_ROUTING_ENABLED", value)
    assert router_module._cost_routing_enabled() is expected
