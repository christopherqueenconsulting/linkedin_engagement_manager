import contextvars
import inspect
import json
import os
import time
from contextlib import contextmanager
from functools import wraps
from typing import Iterator, Optional, Tuple

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


def llm_cache_hit(result) -> bool:
    """True when LiteLLM served this completion from its cache — the provider was never called, so
    the tokens carry no spend. Only a real cache hit counts; prompt-cache discounts are still billed."""
    hidden = getattr(result, "_hidden_params", None)
    return bool(hidden.get("cache_hit")) if isinstance(hidden, dict) else False


# Feature buckets used for per-feature cost/margin attribution. Keep this vocabulary stable —
# PostHog breakdowns and the cost plan (docs/cost-performance-margin-plan.md) key off these values.
FEATURE_CONTENT = "content"
FEATURE_COMMENT = "comment"
FEATURE_DM = "dm"
FEATURE_NEWSLETTER = "newsletter"
FEATURE_SYSTEM = "system"

# First match wins, so the order encodes precedence: `dispatch_comment_followups` is comment work,
# and `automate_profile_viewer_engagement` is outreach DM work despite ending in "engagement".
_TASK_FEATURE_RULES = (
    ("newsletter", FEATURE_NEWSLETTER),
    ("edition", FEATURE_NEWSLETTER),
    ("profile_viewer", FEATURE_DM),
    ("comment", FEATURE_COMMENT),
    ("reply", FEATURE_COMMENT),
    ("seed", FEATURE_COMMENT),
    ("engagement", FEATURE_COMMENT),
    ("dm", FEATURE_DM),
    ("appreciat", FEATURE_DM),
    ("followup", FEATURE_DM),
    ("outreach", FEATURE_DM),
    ("connection_request", FEATURE_DM),
    ("lead", FEATURE_DM),
    ("content", FEATURE_CONTENT),
    ("post", FEATURE_CONTENT),
    ("carousel", FEATURE_CONTENT),
    ("video", FEATURE_CONTENT),
)


def feature_from_task_name(task_name: Optional[str]) -> Optional[str]:
    """Map a Celery task name (fully qualified or bare) onto a feature bucket, for LLM calls whose
    caller can't supply one. Returns None when nothing matches, so the caller can decide the default."""
    if not task_name:
        return None
    name = task_name.rsplit(".", 1)[-1].lower()
    for needle, feature in _TASK_FEATURE_RULES:
        if needle in name:
            return feature
    return None


def _current_task_context() -> Tuple[Optional[str], Optional[int]]:
    """(task_name, user_id) of the Celery task executing on this worker, or (None, None) off-worker
    (API/CLI path). Every per-user task in LEM is dispatched with `kwargs={'user_id': ...}`, so the
    request kwargs are a reliable last-resort attribution source for calls no scope covered."""
    try:
        from celery import current_task
        name = getattr(current_task, "name", None)
        if not name:
            return None, None
        kwargs = getattr(getattr(current_task, "request", None), "kwargs", None)
        user_id = kwargs.get("user_id") if isinstance(kwargs, dict) else None
        return name, user_id
    except Exception:
        return None, None


_llm_attribution: contextvars.ContextVar[dict] = contextvars.ContextVar("llm_attribution", default={})


@contextmanager
def llm_attribution(user_id: Optional[int] = None, feature: Optional[str] = None) -> Iterator[None]:
    """Attribute every LLM call made inside this block to a user and/or feature. Task entry points
    wrap their body in it so cost lands on the right user without threading kwargs through the ~40
    ai_helper signatures. Nested scopes inherit the outer values; None never clears an outer value."""
    scope = dict(_llm_attribution.get())
    if user_id is not None:
        scope["user_id"] = user_id
    if feature is not None:
        scope["feature"] = feature
    token = _llm_attribution.set(scope)
    try:
        yield
    finally:
        _llm_attribution.reset(token)


def attribute_llm_cost(feature: str, user_id_arg: str = "user_id"):
    """Decorator form of llm_attribution() for a function that OWNS a feature's LLM work (a Celery
    task, a generator entry point). It reads the user id from the call's own `user_id_arg` argument,
    so cost is attributed the same way no matter which caller — beat, API, or healer — invoked it."""
    def decorator(fn):
        try:
            params = list(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            params = []
        position = params.index(user_id_arg) if user_id_arg in params else None

        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_id = kwargs.get(user_id_arg)
            if user_id is None and position is not None and len(args) > position:
                user_id = args[position]
            with llm_attribution(user_id=user_id, feature=feature):
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def current_llm_attribution() -> Tuple[Optional[int], Optional[str]]:
    """(user_id, feature) for an LLM call happening right now: the innermost llm_attribution() scope
    first, then the running Celery task (its name for the feature, its kwargs for the user)."""
    scope = _llm_attribution.get()
    user_id, feature = scope.get("user_id"), scope.get("feature")
    if user_id is None or feature is None:
        task_name, task_user_id = _current_task_context()
        user_id = user_id if user_id is not None else task_user_id
        feature = feature or feature_from_task_name(task_name)
    return user_id, feature


def _model_tier(model: Optional[str]) -> Optional[str]:
    """The tier alias a call was routed through (lem-simple/medium/complex/...), or None for a call
    that named a raw provider model instead of a tier."""
    return model if model and model.startswith("lem-") else None


def track_llm_call(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    success: bool = True,
    user_id: Optional[int] = None,
    feature: Optional[str] = None,
    model_tier: Optional[str] = None,
    cached: bool = False,
) -> None:
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="llm_call",
        properties={
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            # A cache hit never reached the provider, so it cost nothing — keeping it at the
            # estimated rate would inflate summed spend on every repeated prompt.
            "cost_usd": 0.0 if cached else estimate_llm_cost_usd(model, prompt_tokens, completion_tokens),
            "latency_ms": latency_ms,
            "success": success,
            "user_id": user_id,
            # Floor the bucket here, not just in the callers: a PostHog breakdown on `feature`
            # needs every llm_call to carry one, including direct calls that omit it.
            "feature": feature or FEATURE_SYSTEM,
            "model_tier": model_tier or _model_tier(model),
            "cached": bool(cached),
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


def track_margin_report(report: dict) -> None:
    """Emit the weekly unit-economics scorecard (plan §E.1.4) as one `margin_report` event so the
    PostHog tiles read system margin, cohort margin and LTV:CAC without re-deriving them. Per-user
    financials stay out of the event body — internal-only by policy (plan §E.5) — but the cohort
    aggregates the Margin-by-Cohort dashboard needs ride along."""
    system = dict((report or {}).get("system") or {})
    unit = dict((report or {}).get("unit_economics") or {})
    period = dict((report or {}).get("period") or {})
    posthog.capture(
        distinct_id="system",
        event="margin_report",
        properties={
            "period_start": period.get("start"),
            "period_end": period.get("end"),
            "period_days": period.get("days"),
            "basis": period.get("basis"),
            "ledger_available": (report or {}).get("ledger_available"),
            "cohorts": (report or {}).get("cohorts") or [],
            **{f"system_{key}": value for key, value in system.items()},
            **unit,
        },
    )


def track_cost_alert(alert: dict, day: Optional[str] = None) -> None:
    """Emit one budget/anomaly alert (plan §E.2) as a `cost_alert` event so a breach is queryable
    next to the spend it came from, and a PostHog alert can page off it. Per-user alerts are keyed
    to that user's distinct_id; system-wide ones to "system"."""
    alert = dict(alert or {})
    posthog.capture(
        distinct_id=str(alert.get("user_id") or "system"),
        event="cost_alert",
        properties={"date": day, **alert},
    )


def posthog_hogql_query(sql: str, timeout: int = 30) -> Optional[list]:
    """Run a HogQL query against the PostHog query API and return its result ROWS, or None when the
    read path isn't configured (no personal API key / project) or the call fails. None means
    "unknown" — never zero — so a check reading it reports itself skipped instead of alerting on a
    missing analytics plane. Reads only; the write/provision path lives in scripts/posthog_dashboards.py."""
    api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "")
    project_id = os.getenv("POSTHOG_PROJECT_ID", "")
    if not api_key or not project_id:
        return None
    host = os.getenv("POSTHOG_APP_HOST", "https://us.posthog.com").rstrip("/")
    try:
        import requests
        response = requests.post(
            f"{host}/api/projects/{project_id}/query/",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": {"kind": "HogQLQuery", "query": sql}},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json().get("results") or []
    except Exception as e:
        from cqc_lem.utilities.logger import log_warning
        log_warning("PostHog HogQL query failed", exc=e, api_provider="posthog")
        return None


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
            user_id, feature = current_llm_attribution()
            try:
                result = fn(*args, **kwargs)
                prompt_tokens, completion_tokens = _extract_token_usage(result)
                track_llm_call(
                    model=model_alias,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=int((time.time() - start) * 1000),
                    success=True,
                    user_id=user_id,
                    feature=feature or FEATURE_SYSTEM,
                    cached=llm_cache_hit(result),
                )
                return result
            except Exception:
                track_llm_call(
                    model=model_alias,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=int((time.time() - start) * 1000),
                    success=False,
                    user_id=user_id,
                    feature=feature or FEATURE_SYSTEM,
                )
                raise
        return wrapper
    return decorator
