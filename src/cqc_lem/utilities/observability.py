import contextvars
import hashlib
import inspect
import json
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import wraps
from typing import Iterator, Optional, Tuple
from urllib.parse import urlparse

import posthog

from cqc_lem.utilities.logger import log_warning

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
# Outbound/marketing production (video tutorials, issue #505) — spend that acquires users rather
# than serving one, so it must never blend into a user's per-feature content cost.
FEATURE_MARKETING = "marketing"
FEATURE_SYSTEM = "system"

# First match wins, so the order encodes precedence: `dispatch_comment_followups` is comment work,
# and `automate_profile_viewer_engagement` is outreach DM work despite ending in "engagement".
_TASK_FEATURE_RULES = (
    ("tutorial", FEATURE_MARKETING),
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
    cost_usd = 0.0 if cached else estimate_llm_cost_usd(model, prompt_tokens, completion_tokens)
    tier = model_tier or _model_tier(model)
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
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "success": success,
            "user_id": user_id,
            # Floor the bucket here, not just in the callers: a PostHog breakdown on `feature`
            # needs every llm_call to carry one, including direct calls that omit it.
            "feature": feature or FEATURE_SYSTEM,
            "model_tier": tier,
            "cached": bool(cached),
        },
    )
    # One ledger row per call would grow unbounded at LEM's call volume, so spend accumulates in a
    # per-day Redis bucket that the daily rollup task collapses into cost_ledger rows.
    _accrue_llm_cost(cost_usd, (prompt_tokens or 0) + (completion_tokens or 0),
                     user_id, feature, tier or model)


# --- Durable cost ledger (issue #490) ------------------------------------------------------
# PostHog answers "what is spend doing?" fast; the cost_ledger table is the exact, Stripe-joinable
# record the margin report needs. Media lands there per render (spiky, needs per-post accounting);
# LLM spend is accumulated in Redis and flushed once a day (one row per user x feature x tier x day)
# so a high-volume table stays small.

# Public list price for one DALL-E 3 1024x1024 "hd" image; override with IMAGE_COST_PER_IMAGE.
_DEFAULT_IMAGE_COST_USD = 0.08

_LLM_ROLLUP_PREFIX = "lem:cost:llm:"
_LLM_ROLLUP_QTY_SUFFIX = ":qty"
# Long enough that a few failed flushes can still be recovered by a later run.
_LLM_ROLLUP_TTL_SECONDS = 14 * 24 * 60 * 60


def image_cost_usd(count: int = 1) -> float:
    """USD for `count` generated images at the configured per-image rate (IMAGE_COST_PER_IMAGE)."""
    try:
        rate = float(os.getenv("IMAGE_COST_PER_IMAGE") or _DEFAULT_IMAGE_COST_USD)
    except (TypeError, ValueError):
        rate = _DEFAULT_IMAGE_COST_USD
    return rate * max(0, int(count or 0))


def _write_cost_ledger(**kwargs) -> None:
    """Best-effort durable write — cost tracking must never break the work that incurred the cost."""
    try:
        from cqc_lem.utilities.db import insert_cost_ledger_entry
        insert_cost_ledger_entry(**kwargs)
    except Exception as e:
        log_warning("Could not write cost_ledger entry", exc=e)


def track_media_cost(kind: str, provider: str, usd: float, user_id: Optional[int] = None,
                     post_id: Optional[int] = None, feature: str = FEATURE_CONTENT,
                     qty: Optional[float] = None, model: Optional[str] = None,
                     meta: Optional[dict] = None) -> None:
    """Record one media render's cost: a PostHog `media_cost` event AND a durable cost_ledger row.

    `kind` is video|image, `qty` the billed units (seconds rendered, images generated). When the
    caller can't supply `user_id`/`feature`, the active llm_attribution scope fills them in.

    A non-positive cost writes nothing — an unpriced model or a rate deliberately zeroed out
    (IMAGE_COST_PER_IMAGE=0) should stay silent rather than fill the ledger with $0 rows, matching
    the LLM accrual and the monthly fixed-cost accrual.
    """
    usd = float(usd or 0.0)
    if usd <= 0:
        return

    scope_user_id, scope_feature = current_llm_attribution()
    user_id = user_id if user_id is not None else scope_user_id
    feature = feature or scope_feature or FEATURE_CONTENT

    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="media_cost",
        properties={
            "kind": kind,
            "provider": provider,
            "model": model,
            "cost_usd": usd,
            "qty": qty,
            "user_id": user_id,
            "post_id": post_id,
            "feature": feature,
            **(meta or {}),
        },
    )
    _write_cost_ledger(feature=feature, category="media", usd=usd, user_id=user_id,
                       provider=provider, model_tier=model, qty=qty, post_id=post_id,
                       task_name=_current_task_context()[0])


def _rollup_field(user_id: Optional[int], feature: Optional[str], model_tier: Optional[str]) -> str:
    return f"{user_id if user_id is not None else ''}|{feature or FEATURE_SYSTEM}|{model_tier or ''}"


def _accrue_llm_cost(usd: float, tokens: int, user_id: Optional[int], feature: Optional[str],
                     model_tier: Optional[str]) -> None:
    """Add one call's spend to today's Redis rollup bucket (flushed to cost_ledger daily).

    Silent no-op without Redis — PostHog still has the per-call event, and the ledger keeps only
    the durable daily aggregate, so a missing bucket costs precision, never correctness elsewhere.
    """
    if usd <= 0:
        return
    # Same handle the 429 breaker and reply-sweep cadence keys use (Redis is where LEM's runtime
    # state lives, so a rollup bucket survives deploys).
    from cqc_lem.utilities.linkedin.rate_limit import _redis_client
    client = _redis_client()
    if client is None:
        return
    day = datetime.now(timezone.utc).date().isoformat()
    key = f"{_LLM_ROLLUP_PREFIX}{day}"
    field = _rollup_field(user_id, feature, model_tier)
    try:
        client.hincrbyfloat(key, field, usd)
        client.expire(key, _LLM_ROLLUP_TTL_SECONDS)
        if tokens:
            client.hincrbyfloat(f"{key}{_LLM_ROLLUP_QTY_SUFFIX}", field, tokens)
            client.expire(f"{key}{_LLM_ROLLUP_QTY_SUFFIX}", _LLM_ROLLUP_TTL_SECONDS)
    except Exception as e:
        log_warning("Could not accrue LLM cost rollup", exc=e)


def _as_text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def flush_llm_cost_rollup(today: Optional[str] = None) -> int:
    """Write every FINISHED day's accumulated LLM spend into cost_ledger, one row per
    user x feature x tier x day, then drop that day's Redis bucket. Returns rows written.

    `today` is an ISO date string (default: today UTC); its bucket — and any later one — is left
    alone because it is still filling. Every OLDER bucket is flushed, so a day missed by a failed
    run is picked up by the next one rather than lost.
    """
    from cqc_lem.utilities.linkedin.rate_limit import _redis_client
    client = _redis_client()
    if client is None:
        return 0

    today = today or datetime.now(timezone.utc).date().isoformat()
    written = 0
    try:
        keys = [_as_text(k) for k in client.scan_iter(match=f"{_LLM_ROLLUP_PREFIX}*")]
    except Exception as e:
        log_warning("Could not scan LLM cost rollup buckets", exc=e)
        return 0

    for key in sorted(k for k in keys if not k.endswith(_LLM_ROLLUP_QTY_SUFFIX)):
        day = key[len(_LLM_ROLLUP_PREFIX):]
        try:
            incurred_on = date.fromisoformat(day)
        except ValueError:
            continue
        if day >= today:
            continue
        try:
            costs = client.hgetall(key) or {}
            quantities = client.hgetall(f"{key}{_LLM_ROLLUP_QTY_SUFFIX}") or {}
        except Exception as e:
            log_warning(f"Could not read LLM cost rollup bucket {key}", exc=e)
            continue

        for raw_field, raw_usd in costs.items():
            field = _as_text(raw_field)
            try:
                usd = float(_as_text(raw_usd))
            except ValueError:
                continue
            user_part, _, rest = field.partition("|")
            feature, _, model_tier = rest.partition("|")
            tokens = quantities.get(raw_field)
            _write_cost_ledger(
                feature=feature or FEATURE_SYSTEM,
                category="llm",
                usd=usd,
                user_id=int(user_part) if user_part else None,
                provider="litellm",
                model_tier=model_tier or None,
                qty=float(_as_text(tokens)) if tokens is not None else None,
                incurred_on=incurred_on,
            )
            written += 1
        try:
            client.delete(key, f"{key}{_LLM_ROLLUP_QTY_SUFFIX}")
        except Exception as e:
            log_warning(f"Could not clear LLM cost rollup bucket {key}", exc=e)

    return written


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


def track_audience_snapshot(
    user_id: Optional[int],
    follower_count: Optional[int] = None,
    connection_count: Optional[int] = None,
    profile_views: Optional[int] = None,
    search_appearances: Optional[int] = None,
    **extra,
) -> None:
    """Emit one audience-telemetry snapshot (issue #627) so follower growth and profile views are
    queryable in PostHog next to the content outcomes that drove them. Unreadable counts stay None
    (not 0) — a zero would read as a real collapse in a growth chart."""
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="audience_snapshot",
        properties={
            "user_id": user_id,
            "follower_count": follower_count,
            "connection_count": connection_count,
            "profile_views": profile_views,
            "search_appearances": search_appearances,
            **extra,
        },
    )


def track_comment_outcome(
    user_id: Optional[int],
    log_id: Optional[int],
    outcome: Optional[dict] = None,
    **extra,
) -> None:
    """Emit one comment-outcome reading (issue #628) so comment→reply rate and the 'Most relevant'
    demotion signal are queryable next to the post outcomes they were meant to drive.
    `visible_most_relevant` stays None when the read was ambiguous — a boolean there would read as
    a confirmed verdict the DOM never gave us."""
    outcome = dict(outcome or {})
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="comment_outcome",
        properties={
            "user_id": user_id,
            "log_id": log_id,
            "status": outcome.get("status"),
            "skip_reason": outcome.get("skip_reason"),
            "author_replied": bool(outcome.get("author_replied")),
            "reply_count": int(outcome.get("reply_count") or 0),
            "like_count": int(outcome.get("like_count") or 0),
            "visible_most_relevant": outcome.get("visible_most_relevant"),
            "our_reply_sent": bool(outcome.get("our_reply_sent")),
            **extra,
        },
    )


def track_suppression_check(user_id: Optional[int], verdict: Optional[dict] = None,
                            paused: bool = False, **extra) -> None:
    """Emit one daily suppression-tripwire reading (issue #629). Every check is emitted, not just
    the trips: the whole point is to see the reach curve BEFORE the step-collapse, and a series that
    only has trips in it cannot show the run-up."""
    verdict = dict(verdict or {})
    signals = {s.get("name"): s for s in (verdict.get("signals") or []) if isinstance(s, dict)}
    reach = signals.get("reach_collapse") or {}
    comments = signals.get("comment_demotion") or {}
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="suppression_check",
        properties={
            "user_id": user_id,
            "status": verdict.get("status"),
            "tripped": bool(verdict.get("tripped")),
            "reason": verdict.get("reason"),
            "paused": bool(paused),
            "reach_status": reach.get("status"),
            "reach_metric": reach.get("metric"),
            "reach_baseline": reach.get("baseline"),
            "reach_max_drop": reach.get("max_drop"),
            "baseline_posts": reach.get("baseline_posts"),
            "posting_days": reach.get("posting_days"),
            "comment_status": comments.get("status"),
            "comment_demotion_rate": comments.get("demotion_rate"),
            **extra,
        },
    )


def track_comment_quality(user_id: Optional[int], report: Optional[dict] = None, **extra) -> None:
    """Emit the weekly per-user comment-quality scorecard (issue #628) — reply/like rates plus the
    demotion rate and the verdict that gates commenting — as one `comment_quality` event, so a hold
    is queryable next to the rates that caused it and a PostHog alert can page off it."""
    report = dict(report or {})
    verdict = dict(report.get("verdict") or {})
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="comment_quality",
        properties={
            "user_id": user_id,
            "days": report.get("days"),
            "sample_size": report.get("sample_size"),
            "checked": report.get("checked"),
            "skipped": report.get("skipped"),
            "author_reply_rate": report.get("author_reply_rate"),
            "reply_rate": report.get("reply_rate"),
            "like_rate": report.get("like_rate"),
            "demotion_rate": report.get("demotion_rate"),
            "visibility_sample": report.get("visibility_sample"),
            "verdict": verdict.get("status"),
            "verdict_reason": verdict.get("reason"),
            **extra,
        },
    )


def track_pre_post_engagement(post_id: int, user_id: Optional[int], status: str, **extra) -> None:
    """Emit the per-post pre-post engagement-window marker (issue #547) — dispatched, skipped (with
    the reason) or ran (with the comment count) — so a report can confirm the warm-up before a post
    actually fired instead of inferring it from task logs."""
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="pre_post_engagement",
        properties={
            "post_id": post_id,
            "user_id": user_id,
            "status": status,
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


def track_routing_policy(report: dict) -> None:
    """Emit the weekly cost-aware routing decision (plan §D.1(1)) as one `routing_policy` event, so
    a down-route — and especially an auto-rollback — is queryable next to the cost it was meant to
    save and the engagement it was gated on. The full comparison stats stay out of the event body;
    the per-bucket verdict and cohort are what a dashboard needs."""
    report = dict(report or {})
    policy = dict(report.get("policy") or {})
    buckets = policy.get("buckets") or {}
    posthog.capture(
        distinct_id="system",
        event="routing_policy",
        properties={
            "date": report.get("date"),
            "enabled": policy.get("enabled"),
            "window_days": report.get("window_days"),
            "observations": report.get("observations"),
            "change_count": len(report.get("changes") or []),
            "rollback_count": sum(1 for c in report.get("changes") or []
                                  if c.get("action") == "rollback"),
            "changes": [{"bucket": c.get("bucket"), "action": c.get("action"),
                         "reason": c.get("reason")} for c in report.get("changes") or []],
            "buckets": [{"bucket": key, "state": b.get("state"), "to_tier": b.get("to_tier"),
                         "cohort_pct": b.get("cohort_pct")} for key, b in buckets.items()],
            "recommendations": report.get("recommendations") or [],
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


def track_capacity_alert(alert: dict, generated_at: Optional[str] = None) -> None:
    """Emit one Selenium/lane capacity breach (issue #552) as a `capacity_alert` event, so the
    saturation history is queryable next to the task latency it explains and a PostHog alert can page
    off it. Always system-scoped: a full browser pool is an infra limit, not one user's problem."""
    alert = dict(alert or {})
    posthog.capture(
        distinct_id="system",
        event="capacity_alert",
        properties={"generated_at": generated_at, **alert},
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


def track_onboarding_step(
    user_id: int,
    step: str,
    hours_since_start: Optional[float] = None,
    **extra,
) -> None:
    """Emit one activation-funnel event per checklist step (issue #500), the first time that step
    completes — so time-to-aha and per-step drop-off are queryable in PostHog."""
    posthog.capture(
        distinct_id=str(user_id),
        event="onboarding_step",
        properties={"step": step, "hours_since_start": hours_since_start, **extra},
    )


def track_onboarding_nudge(user_id: int, nudge_key: str, **extra) -> None:
    """Emit the stalled-user nudge we sent, so nudge → step-completion is measurable."""
    posthog.capture(
        distinct_id=str(user_id),
        event="onboarding_nudge",
        properties={"nudge": nudge_key, **extra},
    )


def track_survey_prompt(user_id: int, survey_key: str, **extra) -> None:
    """Emit the survey we asked for (issue #501), so ask → response rate is measurable."""
    posthog.capture(
        distinct_id=str(user_id),
        event="survey_prompt",
        properties={"survey": survey_key, **extra},
    )


def track_shipped_notice(user_id: int, issue_number: int, **extra) -> None:
    """Emit the "you asked, we shipped" notice we sent (issue #502), so notice → micro-CSAT response
    is measurable against the GA satisfaction gate."""
    posthog.capture(
        distinct_id=str(user_id),
        event="shipped_notice",
        properties={"issue_number": issue_number, **extra},
    )


def track_survey_response(user_id: int, source: str, **extra) -> None:
    """Emit an NPS/review answer (issue #501) with its score/rating, so NPS and CSAT can be trended
    in PostHog next to the activation funnel."""
    posthog.capture(
        distinct_id=str(user_id),
        event="survey_response",
        properties={"source": source, **extra},
    )


# --- Launch funnel (issue #503, docs/launch-and-marketing-plan.md §C.5 / §D.1) -------------------
# Keep these event names STABLE — the PostHog funnel insights, the WARU north-star tile and the
# per-channel CAC rollup all key off these exact strings.
FUNNEL_SIGNUP_STARTED = "signup_started"
FUNNEL_SIGNUP_COMPLETED = "signup_completed"
FUNNEL_TRIAL_STARTED = "trial_started"
FUNNEL_ONBOARDING_STEP_COMPLETED = "onboarding_step_completed"
FUNNEL_ACTIVATED = "activated"
FUNNEL_SUBSCRIPTION_STARTED = "subscription_started"
FUNNEL_CHURNED = "churned"

FUNNEL_EVENTS = (
    FUNNEL_SIGNUP_STARTED,
    FUNNEL_SIGNUP_COMPLETED,
    FUNNEL_TRIAL_STARTED,
    FUNNEL_ONBOARDING_STEP_COMPLETED,
    FUNNEL_ACTIVATED,
    FUNNEL_SUBSCRIPTION_STARTED,
    FUNNEL_CHURNED,
)

# The acquisition channels every trial is attributed to (plan §C.5). Stable vocabulary — breakdowns
# and CAC-per-channel are grouped on these values.
CHANNEL_LINKEDIN = "linkedin"
CHANNEL_NEWSLETTER = "newsletter"
CHANNEL_SEO = "seo"
CHANNEL_EMAIL = "email"
CHANNEL_REFERRAL = "referral"
CHANNEL_AFFILIATE = "affiliate"
CHANNEL_PAID = "paid"
CHANNEL_DIRECT = "direct"
CHANNEL_OTHER = "other"

CHANNELS = (
    CHANNEL_LINKEDIN,
    CHANNEL_NEWSLETTER,
    CHANNEL_SEO,
    CHANNEL_EMAIL,
    CHANNEL_REFERRAL,
    CHANNEL_AFFILIATE,
    CHANNEL_PAID,
    CHANNEL_DIRECT,
    CHANNEL_OTHER,
)

# Client-supplied attribution is allow-listed: a funnel event's schema is ours, not the caller's.
_ATTRIBUTION_KEYS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "referrer", "landing_page",
)
_ATTRIBUTION_MAX_LEN = 255

# First match wins, so order encodes precedence: a `linkedin_newsletter` source is newsletter work,
# not generic brand-LinkedIn traffic — hence the newsletter needles come before "linkedin".
_SOURCE_CHANNEL_RULES = (
    ("newsletter", CHANNEL_NEWSLETTER),
    ("substack", CHANNEL_NEWSLETTER),
    ("beehiiv", CHANNEL_NEWSLETTER),
    ("affiliate", CHANNEL_AFFILIATE),
    ("partner", CHANNEL_AFFILIATE),
    ("linkedin", CHANNEL_LINKEDIN),
    ("google", CHANNEL_SEO),
    ("bing", CHANNEL_SEO),
    ("duckduckgo", CHANNEL_SEO),
    ("referral", CHANNEL_REFERRAL),
)
_MEDIUM_CHANNEL_RULES = (
    ("affiliate", CHANNEL_AFFILIATE),
    ("newsletter", CHANNEL_NEWSLETTER),
    ("referral", CHANNEL_REFERRAL),
    ("email", CHANNEL_EMAIL),
    ("organic", CHANNEL_SEO),
    ("seo", CHANNEL_SEO),
    ("social", CHANNEL_LINKEDIN),
)
_PAID_MEDIUM_NEEDLES = ("cpc", "ppc", "paid")


def _clean_property(value) -> Optional[str]:
    """A trimmed string for a client-supplied property, or None when it carries no signal."""
    if value is None:
        return None
    text = str(value).strip()
    return text[:_ATTRIBUTION_MAX_LEN] if text else None


def _referrer_host(referrer) -> Optional[str]:
    """The bare host a referrer came from, or None when there's no referrer to read."""
    text = _clean_property(referrer)
    if not text:
        return None
    try:
        host = (urlparse(text).netloc or text).lower()
    except ValueError:
        host = text.lower()   # malformed URL (e.g. an unterminated IPv6 host) — match on the raw value
    return host[4:] if host.startswith("www.") else host


def resolve_channel(attribution: Optional[dict]) -> str:
    """The acquisition channel a visit came from, derived from UTMs then the referrer. An explicit
    `channel` always wins (a link we built already knows) but only when it names one of `CHANNELS` —
    a typo or an unknown value becomes `other` rather than a new bucket the CAC rollup can't group.
    Paid mediums are checked before source so `utm_source=google&utm_medium=cpc` is paid spend, not
    SEO. Anything with UTMs we don't recognise is `other` rather than `direct` — a tagged visit was
    never direct."""
    data = attribution if isinstance(attribution, dict) else {}
    explicit = _clean_property(data.get("channel"))
    if explicit:
        normalized = explicit.lower()
        return normalized if normalized in CHANNELS else CHANNEL_OTHER

    source = (_clean_property(data.get("utm_source")) or "").lower()
    medium = (_clean_property(data.get("utm_medium")) or "").lower()
    if any(needle in medium for needle in _PAID_MEDIUM_NEEDLES):
        return CHANNEL_PAID
    for needle, channel in _SOURCE_CHANNEL_RULES:
        if needle in source:
            return channel
    for needle, channel in _MEDIUM_CHANNEL_RULES:
        if needle in medium:
            return channel
    if source or medium or _clean_property(data.get("utm_campaign")):
        return CHANNEL_OTHER

    host = _referrer_host(data.get("referrer"))
    if host:
        for needle, channel in _SOURCE_CHANNEL_RULES:
            if needle in host:
                return channel
        return CHANNEL_REFERRAL
    return CHANNEL_DIRECT


def normalize_attribution(attribution: Optional[dict]) -> dict:
    """The allow-listed source/UTM properties for a funnel event plus the derived `channel`. Unknown
    keys are dropped so a client can't widen the event schema, and `channel` is always present so a
    PostHog breakdown never has an ungrouped bucket."""
    data = attribution if isinstance(attribution, dict) else {}
    props = {}
    for key in _ATTRIBUTION_KEYS:
        value = _clean_property(data.get(key))
        if value:
            props[key] = value
    props["channel"] = resolve_channel(data)
    return props


def anonymous_distinct_id(email: str) -> str:
    """Stable pseudonymous distinct_id for a visitor who has no user row yet, so `signup_started`
    can be aliased onto the real user id once the account exists. The email is hashed — the funnel
    needs a stable key, not the address itself."""
    normalized = (email or "").strip().lower()
    if not normalized:
        return "anonymous"
    return "anon_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def track_funnel_event(
    event: str,
    user_id: Optional[int] = None,
    distinct_id: Optional[str] = None,
    attribution: Optional[dict] = None,
    alias_from: Optional[str] = None,
    **extra,
) -> None:
    """Emit one acquisition → activation → monetization funnel event (plan §C.5) with its source/UTM
    and `channel` properties.

    First-touch attribution is ALSO written onto the PostHog person via `$set_once`, so the later
    events that cannot know the UTMs — `subscription_started` from a Stripe webhook, `churned` — stay
    attributable to the channel that brought the user in. `alias_from` merges the pre-signup
    anonymous person into the identified one so the funnel joins end to end.

    Never raises: analytics must not fail a signup or a billing webhook."""
    from cqc_lem.utilities.logger import log_warning
    try:
        if event not in FUNNEL_EVENTS:
            # Emit anyway — losing the event is worse than an extra name — but make the typo visible.
            log_warning(f"Unknown funnel event '{event}' — emitting anyway")
        attribution_props = normalize_attribution(attribution)
        resolved_id = str(user_id) if user_id is not None else (distinct_id or "anonymous")
        if alias_from and alias_from != resolved_id:
            posthog.alias(previous_id=alias_from, distinct_id=resolved_id)
        posthog.capture(
            distinct_id=resolved_id,
            event=event,
            properties={
                **attribution_props,
                "user_id": user_id,
                "$set_once": {f"initial_{key}": value for key, value in attribution_props.items()},
                **extra,
            },
        )
    except Exception as e:
        log_warning(f"Could not track funnel event '{event}'", exc=e, user_id=user_id)


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
