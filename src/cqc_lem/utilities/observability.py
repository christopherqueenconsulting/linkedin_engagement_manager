import json
import os
import time
from functools import wraps
from typing import Optional, Tuple

import posthog

posthog.api_key = os.getenv("POSTHOG_API_KEY", "")
posthog.host = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")

def _posthog_on_error(e, items) -> None:
    import sys
    print(f"[PostHog] delivery error: {e}", file=sys.stderr)

posthog.on_error = _posthog_on_error

# Disable PostHog when no key configured (local dev without key)
if not posthog.api_key:
    posthog.disabled = True


# Approximate USD cost per 1K tokens as (input, output), keyed by the model string passed to
# track_llm_call — the tier alias (lem-*) in normal operation, or a raw model name. These are
# coarse blended estimates for cost TREND analytics, not billing; override any entry with the
# LLM_COST_PER_1K env var (JSON: {"lem-complex": [0.003, 0.015], ...}).
_DEFAULT_COST_PER_1K = {
    "lem-simple": (0.00015, 0.00060),
    "lem-medium": (0.00060, 0.00240),
    "lem-complex": (0.00300, 0.01500),
    "lem-router": (0.00060, 0.00240),
    "lem-research": (0.00100, 0.00100),
    "lem-image": (0.0, 0.0),
}


# Cache the parsed cost table keyed by the raw LLM_COST_PER_1K value so track_llm_call() — which
# runs on every LLM invocation — doesn't reparse the JSON each call. Rebuilt only when the env var
# changes (sentinel distinguishes "unset" from "" so both are cached).
_UNSET = object()
_cost_table_cache: Optional[dict] = None
_cost_table_raw = _UNSET


def _cost_table() -> dict:
    global _cost_table_cache, _cost_table_raw
    raw = os.getenv("LLM_COST_PER_1K")
    if _cost_table_cache is not None and raw == _cost_table_raw:
        return _cost_table_cache
    table = dict(_DEFAULT_COST_PER_1K)
    if raw:
        try:
            for key, val in json.loads(raw).items():
                if isinstance(val, (list, tuple)) and len(val) == 2:
                    table[key] = (float(val[0]), float(val[1]))
        except (ValueError, TypeError):
            # Malformed override JSON: keep the built-in defaults rather than crash the tracked call.
            pass
    _cost_table_cache = table
    _cost_table_raw = raw
    return table


def estimate_llm_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Coarse USD cost estimate for a completion from a per-1K-token price table (env-overridable
    via LLM_COST_PER_1K). Unknown models fall back to a substring match then the lem-medium rate so
    a real call's cost signal is never silently zero. Returns 0.0 when there are no tokens."""
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    if not prompt and not completion:
        return 0.0
    table = _cost_table()
    rates = table.get(model)
    if rates is None:
        key = next((k for k in table if k != "lem-image" and k in (model or "")), None)
        rates = table[key] if key else table["lem-medium"]
    # No rounding: a few prompt tokens on a cheap tier round to 0.0 at 6dp, which would erase the
    # non-zero cost signal for real calls. PostHog handles display rounding.
    return (prompt / 1000.0) * rates[0] + (completion / 1000.0) * rates[1]


def _extract_token_usage(result) -> Tuple[int, int]:
    """(prompt_tokens, completion_tokens) from an OpenAI-style response's `.usage`, or (0, 0) when
    the wrapped call returned something without usage."""
    usage = getattr(result, "usage", None)
    if usage is None:
        return 0, 0
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    return prompt, completion


def track_llm_call(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    success: bool = True,
    user_id: Optional[int] = None,
) -> None:
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="llm_call",
        properties={
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_usd": estimate_llm_cost_usd(model, prompt_tokens, completion_tokens),
            "latency_ms": latency_ms,
            "success": success,
        },
    )


def track_post_outcome(
    post_id: int,
    reactions: Optional[int],
    comments: Optional[int],
    reposts: Optional[int] = 0,
    impressions: Optional[int] = None,
    saves: Optional[int] = 0,
    user_id: Optional[int] = None,
    **extra,
) -> None:
    """Emit a LinkedIn post-outcome event so content performance (impressions / engagement rate) is
    queryable in PostHog alongside LLM cost. `engagement` / `engagement_rate` are derived with the
    same weighting `post_stats` uses; `engagement_rate` is None when impressions are unknown."""
    from cqc_lem.utilities.post_stats import engagement_score, engagement_rate
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="post_outcome",
        properties={
            "post_id": post_id,
            "reactions": int(reactions or 0),
            "comments": int(comments or 0),
            "reposts": int(reposts or 0),
            "saves": int(saves or 0),
            "impressions": int(impressions) if impressions else None,
            "engagement": engagement_score(reactions, comments, reposts),
            "engagement_rate": engagement_rate(reactions, comments, reposts, impressions),
            **extra,
        },
    )


def track_task(
    task_name: str,
    duration_ms: int,
    success: bool = True,
    user_id: Optional[int] = None,
    **extra,
) -> None:
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="celery_task",
        properties={"task": task_name, "duration_ms": duration_ms, "success": success, **extra},
    )


def track_api_call(
    route: str,
    method: str,
    status_code: int,
    latency_ms: int,
    user_id: Optional[int] = None,
) -> None:
    posthog.capture(
        distinct_id=str(user_id or "anonymous"),
        event="api_call",
        properties={
            "route": route,
            "method": method,
            "status_code": status_code,
            "latency_ms": latency_ms,
        },
    )


def llm_tracked(model_alias: str):
    """Decorator that wraps an LLM call and tracks usage via PostHog."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = fn(*args, **kwargs)
                prompt_tokens, completion_tokens = _extract_token_usage(result)
                track_llm_call(
                    model=model_alias,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=int((time.time() - start) * 1000),
                    success=True,
                )
                return result
            except Exception:
                track_llm_call(
                    model=model_alias,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=int((time.time() - start) * 1000),
                    success=False,
                )
                raise
        return wrapper
    return decorator
