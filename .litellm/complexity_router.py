"""LiteLLM pre-call hook: resolves the tier a request runs on.

Two stages, in order:
1. **Complexity** (always) — `lem-router` requests are mapped to a tier from prompt shape.
2. **Cost-aware down-routing** (flag-gated, `COST_AWARE_ROUTING_ENABLED`) — the tier from stage 1,
   or an explicitly requested `lem-*` tier, may be routed one tier DOWN for the treatment cohort of
   an active experiment (docs/cost-performance-margin-plan.md §D.1(1), issue #494).

The decision itself lives in `routing_policy.py` — the app module mounted into this container by
docker-compose, and the same code the weekly optimizer uses to evaluate the experiment. The policy
document is published to Redis by `cqc_lem.utilities.cost_routing`; this hook only reads it, caches
it briefly in-process, and FAILS OPEN: any missing module, unreachable Redis, or malformed document
leaves routing exactly as it was before this feature existed.

**How it is loaded (issue #1880).** `config.yaml` names `complexity_router.proxy_handler_instance`
under `litellm_settings.callbacks` — the key LiteLLM actually reads. From #494 until #1880 it was
listed under `custom_callbacks`, which LiteLLM never reads, so NOTHING in this file had ever
executed in production: `lem-router` was served by its own `model_list` entry and no request was
ever re-tiered.
"""
import json
import logging
import os
import sys
import time

from litellm.integrations.custom_logger import CustomLogger

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import routing_policy
except Exception:  # pragma: no cover - container without the mounted module: complexity only
    routing_policy = None

log = logging.getLogger("lem.router")

# Signals kept here as the fallback for a container that has no routing_policy mount, so this hook
# never loses its original behaviour.
SIMPLE_SIGNALS = ["refine", "shorten", "summarize briefly", "comma separated", "short list"]
COMPLEX_SIGNALS = [
    "thought leadership", "viral post", "personal story", "industry news",
    "buyer stage", "comprehensive", "step-by-step", "framework",
]

ROUTER_MODEL = "lem-router"
TIERS = ("lem-simple", "lem-medium", "lem-complex")

# Seconds a fetched policy is reused before Redis is consulted again. The policy changes weekly, so
# this is purely about not paying a Redis round-trip per LLM call; a rollback still takes effect
# within one TTL.
POLICY_TTL_SECONDS = int(os.getenv("COST_ROUTING_POLICY_TTL_SECONDS", "60") or 60)


def _cost_routing_enabled():
    return (os.getenv("COST_AWARE_ROUTING_ENABLED", "false") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _redis_url():
    """Must resolve to the SAME Redis the app publishes to — see COST_ROUTING_REDIS_URL in
    .env.example. The Celery broker is the fallback because that is where LEM's runtime state
    already lives."""
    for env_var in ("COST_ROUTING_REDIS_URL", "CELERY_BROKER_URL"):
        url = (os.getenv(env_var) or "").strip()
        if url.startswith("redis"):
            return url
    return "redis://{}:{}/0".format(os.getenv("REDIS_HOST", "redis"), os.getenv("REDIS_PORT", "6379"))


class LEMComplexityRouter(CustomLogger):
    def __init__(self):
        super().__init__()
        self._policy = None
        self._policy_fetched_at = 0.0

    # ───────────────────────── policy plumbing ─────────────────────────

    def _fetch_policy(self):
        """Read the published policy from Redis. None on any failure — the caller treats that as
        "no policy", i.e. complexity-only routing."""
        try:
            import redis
        except Exception:
            return None
        try:
            client = redis.Redis.from_url(_redis_url(), socket_timeout=2, socket_connect_timeout=2)
            raw = client.get(routing_policy.POLICY_REDIS_KEY)
        except Exception as exc:
            log.debug("cost-aware routing: policy read failed (%s)", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception:
            log.warning("cost-aware routing: policy in Redis is not valid JSON — ignoring")
            return None

    def _current_policy(self):
        now = time.time()
        if self._policy is None or (now - self._policy_fetched_at) >= POLICY_TTL_SECONDS:
            self._policy = self._fetch_policy() or {}
            self._policy_fetched_at = now
        return self._policy

    # ───────────────────────── tier resolution ─────────────────────────

    def _complexity_tier(self, data):
        if routing_policy is not None:
            return routing_policy.complexity_tier(routing_policy.prompt_text(data.get("messages")))
        prompt = " ".join(
            m.get("content", "") for m in data.get("messages", [])
            if isinstance(m.get("content"), str)
        ).lower()
        score = (sum(1 for s in COMPLEX_SIGNALS if s in prompt)
                 - sum(1 for s in SIMPLE_SIGNALS if s in prompt))
        token_estimate = len(prompt.split())
        if score >= 2 or token_estimate > 800:
            return "lem-complex"
        if score >= 1 or token_estimate > 300:
            return "lem-medium"
        return "lem-simple"

    @staticmethod
    def _attribution(data):
        """(feature, user_id) for this request, from the `metadata` block `_call_llm` attaches. The
        same two dimensions cost is attributed by, so an experiment bucket and a spend bucket always
        mean the same thing."""
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            return None, None
        return metadata.get("feature"), metadata.get("user_id")

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        requested = data.get("model")
        if requested == ROUTER_MODEL:
            tier = self._complexity_tier(data)
        elif requested in TIERS:
            tier = requested
        else:
            return data  # image/research/raw provider models are never re-routed

        if routing_policy is not None and _cost_routing_enabled():
            feature, user_id = self._attribution(data)
            try:
                decision = routing_policy.resolve_tier(tier, feature, user_id, self._current_policy())
            except Exception as exc:  # never let the optimizer take an LLM call down with it
                log.warning("cost-aware routing failed, using %s: %s", tier, exc)
            else:
                if decision["applied"]:
                    # DEBUG, not INFO: this fires on every down-routed call (10%+ of ALL LLM traffic
                    # once enabled). INFO here is reserved for state transitions, which the weekly
                    # optimizer logs on the app side.
                    log.debug("cost-aware down-route %s→%s (bucket=%s, feature=%s)",
                              tier, decision["tier"], decision["bucket"], feature)
                    tier = decision["tier"]

        data["model"] = tier
        return data


proxy_handler_instance = LEMComplexityRouter()
