"""The ONE place LEM emits an analytics event — every server-side `posthog.capture` lives in here.

Callers hand over a measurement (`track_llm_call`, `track_task`, `track_api_call`,
`track_funnel_event`, `capture_exception`, and the per-surface `track_*` reporters); this module
owns the event names, the property shapes, and the `distinct_id` rule that makes browser, Celery and
proxy activity read as ONE person — `str(user_id)`, falling back to a shared `"system"` /
`"anonymous"` rather than dropping the row, because an unattributed run still has to appear in the
count. A capture written anywhere else is invisible to the dashboards those events feed
(`docs/observability-map.md`).

Two money signals live here and are NEVER summed: `llm_call` carries LEM's OWN token-price estimate
(`estimate_llm_cost_usd`), `$ai_generation` carries the provider's price for the same call. They
answer different questions, and adding them double-counts every request.

With no `POSTHOG_API_KEY` the SDK is disabled at import, so every function here is a no-op in local
dev and under test — a call site should never guard itself on the key.
"""

import contextvars
import hashlib
import inspect
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import wraps
from typing import Iterator, Optional, Tuple
from urllib.parse import urlparse

import posthog

from cqc_lem.utilities.experiments import (
    COMMENT_CONTRACT_PROMPT,
    COST_ROUTING_ARM,
    POST_MEDIA_VARIANT,
)
from cqc_lem.utilities.logger import log_debug, log_warning

posthog.api_key = os.getenv("POSTHOG_API_KEY", "")
posthog.host = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")

def _posthog_on_error(e, items) -> None:
    import sys
    print(f"[PostHog] delivery error: {e}", file=sys.stderr)

posthog.on_error = _posthog_on_error

# Disable PostHog when no key configured (local dev without key)
if not posthog.api_key:
    posthog.disabled = True


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


# Error tracking (issue #648). posthog-python installs its own sys.excepthook / threading hook, so
# an exception that kills a process is captured as a grouped $exception issue without any call site
# knowing about it. It only ever fires for UNCAUGHT exceptions — everything LEM catches reaches
# PostHog through capture_exception() below (from log_error/log_critical, the Celery signals and the
# FastAPI middleware). Read at import so the flag is set before posthog.setup() builds the client.
EXCEPTION_AUTOCAPTURE_ENABLED = bool(posthog.api_key) and _env_flag("POSTHOG_EXCEPTION_AUTOCAPTURE")
posthog.enable_exception_autocapture = EXCEPTION_AUTOCAPTURE_ENABLED

# What posthog-js hands out as a session id (uuid v7-ish). Anything else is not linked (#649).
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,64}")


# Approximate USD cost per 1K tokens as (input, output). Sources, in precedence order:
#   1. LLM_COST_PER_1K env var (JSON override)
#   2. .litellm/model_prices_snapshot.json (vendored LiteLLM map + LEM shadow references)
#   3. Hardcoded tier-alias fallbacks below for models absent from the map.
# The vendored map is keyed by the SERVING model LiteLLM returns (e.g. openai/gpt-4o-mini),
# not the lem-* tier alias, so fallback/down-routed calls are priced by the model that actually ran.
_DEFAULT_COST_PER_1K = {
    "lem-simple": (0.00015, 0.00060),
    "lem-medium": (0.00060, 0.00240),
    "lem-complex": (0.00300, 0.01500),
    "lem-router": (0.00060, 0.00240),
    "lem-research": (0.00100, 0.00100),
    "lem-image": (0.0, 0.0),
}

_LLM_PRICE_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    ".litellm", "model_prices_snapshot.json"
)


# Cache the parsed cost table keyed by the raw LLM_COST_PER_1K value so track_llm_call() — which
# runs on every LLM invocation — doesn't reparse the JSON each call. Rebuilt only when the env var
# changes (sentinel distinguishes "unset" from "" so both are cached).
_UNSET = object()
_cost_table_cache: Optional[dict] = None
_cost_table_raw = _UNSET
_vendored_specs_cache: Optional[dict] = None


def _vendored_specs() -> dict:
    """The vendored price map as {model_id: spec_dict}, or {} when the snapshot is missing.

    The map contains both LiteLLM's metered prices and LEM's hand-picked shadow references for
    subscription-only models (Ollama Cloud). It is read once per process; a missing file is not
    fatal — the hardcoded tier fallbacks take over.
    """
    global _vendored_specs_cache
    if _vendored_specs_cache is not None:
        return _vendored_specs_cache
    try:
        with open(_LLM_PRICE_SNAPSHOT_PATH, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        _vendored_specs_cache = dict((doc or {}).get("models") or {})
    except Exception:
        _vendored_specs_cache = {}
    return _vendored_specs_cache


def _cost_table() -> dict:
    global _cost_table_cache, _cost_table_raw
    raw = os.getenv("LLM_COST_PER_1K")
    if _cost_table_cache is not None and raw == _cost_table_raw:
        return _cost_table_cache
    # Start with vendored map (serving models), then overlay the legacy tier-alias fallbacks.
    table: dict = {}
    for model_id, spec in _vendored_specs().items():
        inp = spec.get("input_cost_per_token")
        out = spec.get("output_cost_per_token")
        if inp is None or out is None:
            continue
        rates = (float(inp) * 1000.0, float(out) * 1000.0)
        table[model_id] = rates
        # Also index by the bare model name so a LiteLLM response like "gpt-oss:20b" resolves when
        # the snapshot key is "openai/gpt-oss:20b".
        if "/" in model_id:
            bare = model_id.split("/", 1)[1]
            if bare and bare not in table:
                table[bare] = rates
    table.update(_DEFAULT_COST_PER_1K)
    if raw:
        try:
            for key, val in json.loads(raw).items():
                if isinstance(val, (list, tuple)) and len(val) == 2:
                    table[key] = (float(val[0]), float(val[1]))
        except (ValueError, TypeError):
            # Malformed override JSON: keep the built-in defaults rather than crash the tracked call.
            pass
    _cost_table_cache = table  # lgtm[py/unused-global-variable]
    _cost_table_raw = raw  # lgtm[py/unused-global-variable]
    return table


def estimate_llm_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Coarse USD cost estimate for a completion from the per-1K-token table. The serving model
    (what LiteLLM actually ran) is looked up first; unknown serving models fall back to a substring
    match and then the lem-medium rate so a real call's cost signal is never silently zero.
    Returns 0.0 when there are no tokens.
    """
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    if not prompt and not completion:
        return 0.0
    table = _cost_table()
    rates = table.get(model)
    if rates is None:
        key = next((k for k in table if k != "lem-image" and (k in (model or "") or
                    ((model or "").endswith(k) if "/" in k else False))), None)
        rates = table[key] if key else table["lem-medium"]
    # No rounding: a few prompt tokens on a cheap tier round to 0.0 at 6dp, which would erase the
    # non-zero cost signal for real calls. PostHog handles display rounding.
    return (prompt / 1000.0) * rates[0] + (completion / 1000.0) * rates[1]


def estimate_shadow_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    """Shadow cost for subscription/zero-priced traffic.

    For models whose own LiteLLM price is 0.0 (Ollama Cloud on a flat-rate subscription), this returns
    what the same tokens would cost at the hand-picked metered reference documented in
    .litellm/model_prices_snapshot.json. For already-metered models it returns None — shadow cost
    is only meaningful where the real marginal cost is hidden by a subscription.
    """
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    if not prompt and not completion:
        return None
    specs = _vendored_specs()
    spec = specs.get(model)
    if spec is None and "/" in (model or ""):
        bare = model.split("/", 1)[1]
        spec = specs.get(bare)
    if not spec:
        return None
    own_in = spec.get("input_cost_per_token")
    own_out = spec.get("output_cost_per_token")
    if own_in is not None and own_out is not None and (float(own_in) > 0 or float(own_out) > 0):
        # Already metered — no shadow needed.
        return None
    shadow_ref = spec.get("shadow_reference")
    if not shadow_ref:
        return None
    shadow_spec = specs.get(shadow_ref)
    if shadow_spec is None and "/" in shadow_ref:
        shadow_spec = specs.get(shadow_ref.split("/", 1)[1])
    if not shadow_spec:
        return None
    ref_in = shadow_spec.get("input_cost_per_token")
    ref_out = shadow_spec.get("output_cost_per_token")
    if ref_in is None or ref_out is None:
        return None
    return prompt * float(ref_in) + completion * float(ref_out)


def _extract_token_usage(result) -> Tuple[int, int]:
    """(prompt_tokens, completion_tokens) from an OpenAI-style response's `.usage`, or (0, 0) when
    the wrapped call returned something without usage.
    """
    usage = getattr(result, "usage", None)
    if usage is None:
        return 0, 0
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    return prompt, completion


def llm_cache_hit(result) -> bool:
    """True when LiteLLM served this completion from its cache — the provider was never called, so
    the tokens carry no spend. Only a real cache hit counts; prompt-cache discounts are still billed.
    """
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
    caller can't supply one. Returns None when nothing matches, so the caller can decide the default.
    """
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
    request kwargs are a reliable last-resort attribution source for calls no scope covered.
    """
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
    ai_helper signatures. Nested scopes inherit the outer values; None never clears an outer value.
    """
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


def _argument_reader(fn, arg_name: str):
    """A `(args, kwargs) -> value` reader for one of `fn`'s own arguments, keyword or positional.
    Bound once at decoration time so the signature isn't introspected on every call.
    """
    try:
        params = list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        params = []
    position = params.index(arg_name) if arg_name in params else None

    def read(args, kwargs):
        value = kwargs.get(arg_name)
        if value is None and position is not None and len(args) > position:
            value = args[position]
        return value
    return read


def attribute_llm_cost(feature: str, user_id_arg: str = "user_id"):
    """Decorator form of llm_attribution() for a function that OWNS a feature's LLM work (a Celery
    task, a generator entry point). It reads the user id from the call's own `user_id_arg` argument,
    so cost is attributed the same way no matter which caller — beat, API, or healer — invoked it.
    """
    def decorator(fn):
        read_user_id = _argument_reader(fn, user_id_arg)

        @wraps(fn)
        def wrapper(*args, **kwargs):
            with llm_attribution(user_id=read_user_id(args, kwargs), feature=feature):
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def current_llm_attribution() -> Tuple[Optional[int], Optional[str]]:
    """(user_id, feature) for an LLM call happening right now: the innermost llm_attribution() scope
    first, then the running Celery task (its name for the feature, its kwargs for the user).
    """
    scope = _llm_attribution.get()
    user_id, feature = scope.get("user_id"), scope.get("feature")
    if user_id is None or feature is None:
        task_name, task_user_id = _current_task_context()
        user_id = user_id if user_id is not None else task_user_id
        feature = feature or feature_from_task_name(task_name)
    return user_id, feature


# --- LLM pipeline traces: $ai_trace / $ai_span (issue #746) --------------------------------
# A post is not one model call. Research -> draft -> refine -> humanize -> authenticity -> review is
# six, and each lands in PostHog as its own isolated $ai_generation, so "what did THIS post cost, end
# to end?" has no answer. Grouping is app-side work: only the app knows where a pipeline starts.
#
# The ids live in a contextvar (no ai_helper signature grows a trace argument) and the SHARED client
# is what puts them on the wire — see utilities/ai/client.py. Two wires, because LiteLLM's PostHog
# logger reads them from two places: the `x-litellm-trace-id` header becomes the proxy event's
# $ai_trace_id, and `metadata.parent_run_id` becomes its $ai_parent_id. The $ai_trace / $ai_span
# events emitted here are the skeleton those generations hang off.
#
# The proxy-side $ai_generation stream itself is untouched: no extra event, no changed property.

_llm_trace: contextvars.ContextVar[dict] = contextvars.ContextVar("llm_trace", default={})


def llm_tracing_enabled() -> bool:
    """Kill switch, read at call time. Off means no trace id is ever minted, so the client attaches
    nothing and the proxy stream is exactly what it was before tracing shipped.
    """
    return _env_flag("LLM_TRACING_ENABLED")


def current_llm_trace() -> Tuple[Optional[str], Optional[str]]:
    """(trace_id, span_id) for the LLM call happening right now — the pipeline it belongs to, and the
    step within it. (None, None) when no pipeline is open, which every untraced call still is.
    """
    scope = _llm_trace.get()
    return scope.get("trace_id"), scope.get("span_id")


def _capture_ai_span(event: str, trace_id: str, span_id: str, name: str, started: float,
                     parent_id: Optional[str], error: Optional[BaseException],
                     user_id: Optional[int], feature: Optional[str],
                     properties: Optional[dict] = None) -> None:
    """Emit one $ai_trace / $ai_span. Best-effort by construction — a post must never fail to
    generate because its telemetry could not be written.
    """
    try:
        props = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_span_name": name,
            # PostHog's LLM analytics reads $ai_latency in SECONDS (llm_call's latency_ms is the
            # other convention and the two must not be confused on one chart).
            "$ai_latency": round(max(0.0, time.time() - started), 3),
            "feature": feature or FEATURE_SYSTEM,
            "user_id": user_id,
        }
        if parent_id:
            props["$ai_parent_id"] = parent_id
        if error is not None:
            props["$ai_is_error"] = True
            props["$ai_error"] = f"{type(error).__name__}: {error}"
        if properties:
            props.update({k: v for k, v in properties.items() if v is not None})
        posthog.capture(
            distinct_id=str(user_id if user_id is not None else "system"),
            event=event,
            properties=props,
        )
    except Exception as e:
        # DEBUG, not WARNING: a telemetry miss is an expected no-op class, and a repeated warning
        # would file a defect for it (see utilities/CLAUDE.md).
        log_debug(f"Could not capture {event}: {e}")


@contextmanager
def llm_trace(name: str, user_id: Optional[int] = None,
              feature: Optional[str] = None) -> Iterator[Optional[str]]:
    """Group every LLM call made inside this block into ONE PostHog trace named `name`, and open the
    matching `llm_attribution()` scope so a pipeline entry point declares who/what once.

    Nesting is deliberate: a trace opened inside an open one becomes a SPAN of the outer trace rather
    than a second trace. `create_text_post` recurses into itself for post-type fallbacks, and two
    half-traces of one post answer nobody's question.
    """
    if _llm_trace.get().get("trace_id"):
        with llm_attribution(user_id=user_id, feature=feature):
            with llm_span(name):
                yield _llm_trace.get().get("trace_id")
        return

    with llm_attribution(user_id=user_id, feature=feature):
        if not llm_tracing_enabled():
            yield None
            return
        # The root span IS the trace: PostHog puts an event in a trace's child list only when its
        # $ai_parent_id equals the $ai_trace_id itself (traces_query_runner.py builds `events` from
        # `toString($ai_parent_id) = toString($ai_trace_id)`, and total_latency sums the same set).
        # Minting a separate root-span uuid would parent every step onto an id no node carries, so
        # the trace would open EMPTY with zero latency — the one thing this feature exists to show.
        trace_id = str(uuid.uuid4())
        span_id = trace_id
        # Resolved once, inside the scope: the finally block runs after llm_attribution has been
        # reset, and a trace attributed to "system" would be worse than no trace at all.
        scope_user_id, scope_feature = current_llm_attribution()
        token = _llm_trace.set({"trace_id": trace_id, "span_id": span_id})
        started, error = time.time(), None
        try:
            yield trace_id
        except BaseException as exc:
            error = exc
            raise
        finally:
            _llm_trace.reset(token)
            _capture_ai_span("$ai_trace", trace_id, span_id, name, started, None, error,
                             scope_user_id, scope_feature)


@contextmanager
def llm_span(name: str, **properties) -> Iterator[Optional[str]]:
    """One step of the pipeline currently open around this block.

    Outside a pipeline this does nothing at all and yields None — a span with no trace is an orphan
    PostHog cannot render, and the work itself must run identically either way.
    """
    scope = _llm_trace.get()
    trace_id, parent_id = scope.get("trace_id"), scope.get("span_id")
    if not trace_id:
        yield None
        return
    span_id = str(uuid.uuid4())
    user_id, feature = current_llm_attribution()
    token = _llm_trace.set({"trace_id": trace_id, "span_id": span_id})
    started, error = time.time(), None
    try:
        yield span_id
    except BaseException as exc:
        error = exc
        raise
    finally:
        _llm_trace.reset(token)
        _capture_ai_span("$ai_span", trace_id, span_id, name, started, parent_id, error,
                         user_id, feature, properties)


def llm_pipeline(name: str, feature: Optional[str] = None, user_id_arg: str = "user_id"):
    """Decorator form of llm_trace() for a function that IS one pipeline (`create_text_post`,
    `generate_newsletter_edition`, `generate_ai_response`). Supersedes `attribute_llm_cost` on such a
    function: it opens the same attribution scope AND the trace, reading the user id off the call's
    own `user_id_arg` the same way.
    """
    def decorator(fn):
        read_user_id = _argument_reader(fn, user_id_arg)

        @wraps(fn)
        def wrapper(*args, **kwargs):
            with llm_trace(name, user_id=read_user_id(args, kwargs), feature=feature):
                return fn(*args, **kwargs)
        wrapper.__llm_trace_name__ = name
        return wrapper
    return decorator


def llm_step(name: str):
    """Decorator form of llm_span() for ONE step of a pipeline.

    It goes on the STEP FUNCTION, not at its call sites: newsletters and comments draw draft,
    research, humanize and authenticity from the same shared content core as posts, so decorating
    the core is what makes every pipeline's trace legible without touching any caller.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with llm_span(name):
                return fn(*args, **kwargs)
        wrapper.__llm_span_name__ = name
        return wrapper
    return decorator


# --- Error tracking (issue #648) -----------------------------------------------------------
# PostHog groups $exception events into ISSUES by fingerprint, so the dedup the old log-grep cron
# hand-rolled is done for us. Every capture below is best-effort: telemetry must never be the reason
# a task or a request fails.

def capture_exception(exc: Optional[BaseException] = None, user_id: Optional[int] = None,
                      fingerprint: Optional[str] = None, **context) -> None:
    """Send one caught exception to PostHog Error Tracking with LEM's context on it.

    `posthog.capture_exception` is idempotent per exception INSTANCE, so a task that logs
    `log_error(exc=e)` and then re-raises produces ONE occurrence, not two — the Celery signal
    handler capturing the same object is a no-op.

    The task name/user default to the running Celery task's, so a call site that knows nothing about
    its context still lands attributed. distinct_id follows the same convention as every other
    event here: the user id, or the `"system"` sentinel.
    """
    if posthog.disabled:
        return
    try:
        task_name, task_user_id = _current_task_context()
        properties = {k: v for k, v in context.items() if v is not None}
        properties.setdefault("task_name", task_name)
        if user_id is None:
            user_id = task_user_id
        properties["user_id"] = user_id
        # Explicit grouping override. PostHog's default fingerprint is exception type + first in-app
        # stack frame, so every escalated warning raised from the same helper (e.g. find_first) would
        # otherwise collapse into ONE issue — "Feed sort control" and "Open reactions menu" are
        # different breakages and have to stay different issues. `$`-prefixed keys aren't valid
        # Python identifiers, hence a named parameter rather than a **context key.
        if fingerprint:
            properties["$exception_fingerprint"] = fingerprint
        posthog.capture_exception(
            exc,
            distinct_id=str(user_id if user_id is not None else "system"),
            properties={k: v for k, v in properties.items() if v is not None},
        )
    except Exception as e:
        # A logger call here would recurse straight back into capture_exception via log_error.
        log_warning(f"Could not capture exception in PostHog: {e}")


def _model_tier(model: Optional[str]) -> Optional[str]:
    """The tier alias a call was routed through (lem-simple/medium/complex/...), or None for a call
    that named a raw provider model instead of a tier.
    """
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
    serving_model: Optional[str] = None,
) -> None:
    """Emit one `llm_call` event and accrue spend to the daily cost rollup.

    `model` is the requested tier alias (or raw model) for backward compatibility.
    `serving_model`, when provided, is the model LiteLLM actually ran — including after fallback
    or cost-aware down-routing. Spend is priced by the SERVING model so a fallback to a paid
    provider is visible in the ledger. The requested alias is preserved as `model_tier` so
    per-tier reporting survives.
    """
    resolved_model = serving_model or model
    tier = model_tier or _model_tier(model)
    cost_usd = 0.0 if cached else estimate_llm_cost_usd(resolved_model, prompt_tokens, completion_tokens)
    shadow_cost_usd = None if cached else estimate_shadow_cost_usd(resolved_model, prompt_tokens, completion_tokens)
    properties = {
        "model": resolved_model,
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
    }
    if shadow_cost_usd is not None:
        # Shadow cost is a separate decision signal: what the same call would cost if the
        # subscription model were billed at the metered reference. It never enters cost_usd.
        properties["shadow_cost_usd"] = shadow_cost_usd
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="llm_call",
        properties=properties,
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

# Per-model list prices for one generated image. gpt-image entries are keyed model:quality
# (1024x1024 square rates — the non-square rates are lower, so square is the safe over-estimate).
# IMAGE_COST_PER_IMAGE overrides EVERYTHING when set — kept as the ops kill/repricing switch.
_IMAGE_COST_BY_MODEL = {
    "black-forest-labs/flux-dev": 0.025,
    "black-forest-labs/flux-1.1-pro": 0.04,
    "gpt-image-2:low": 0.006,
    "gpt-image-2:medium": 0.053,
    "gpt-image-2:high": 0.211,
    "gpt-image-1:low": 0.011,
    "gpt-image-1:medium": 0.042,
    "gpt-image-1:high": 0.167,
    "dall-e-3": 0.08,
}
# Fallback when the model is unknown; historic DALL-E 3 hd list price.
_DEFAULT_IMAGE_COST_USD = 0.08

_LLM_ROLLUP_PREFIX = "lem:cost:llm:"
_LLM_ROLLUP_QTY_SUFFIX = ":qty"
# Long enough that a few failed flushes can still be recovered by a later run.
_LLM_ROLLUP_TTL_SECONDS = 14 * 24 * 60 * 60


def image_cost_usd(count: int = 1, model: Optional[str] = None,
                   quality: Optional[str] = None) -> float:
    """USD for `count` generated images.

    Rate resolution: IMAGE_COST_PER_IMAGE env (global override) > `model:quality` >
    `model` > default. Unknown models bill at the conservative default rather than $0 —
    an unpriced render must not silently vanish from the ledger.
    """
    override = os.getenv("IMAGE_COST_PER_IMAGE")
    if override:
        try:
            return float(override) * max(0, int(count or 0))
        except (TypeError, ValueError):
            pass
    rate = None
    if model:
        if quality:
            rate = _IMAGE_COST_BY_MODEL.get(f"{model}:{quality}")
        if rate is None:
            rate = _IMAGE_COST_BY_MODEL.get(model)
    if rate is None:
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
    # cost_ledger.model_tier is VARCHAR(64); an over-long identifier used to abort the whole
    # ledger write, so the spend vanished rather than being recorded under a truncated name.
    _write_cost_ledger(feature=feature, category="media", usd=usd, user_id=user_id,
                       provider=provider, model_tier=(model or None) and model[:64],
                       qty=qty, post_id=post_id,
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
    same weighting `post_stats` uses; `engagement_rate` is None when impressions are unknown.

    This is the success metric of the cost-routing experiment (issue #652), so the user's arm rides
    along as `$feature/cost-routing-arm` when PostHog enrolled them — `variant_key` (the #396 media
    combo this post shipped, when the caller knows it) becomes the media experiment's arm the same
    way.
    """
    from cqc_lem.utilities.post_stats import engagement_rate, engagement_score
    shipped = extra.pop("variant_key", None)
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="post_outcome",
        properties={
            **experiment_props(user_id, keys=(COST_ROUTING_ARM,),
                               shipped={POST_MEDIA_VARIANT: shipped} if shipped else None),
            "post_id": post_id,
            "variant_key": shipped,
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
    (not 0) — a zero would read as a real collapse in a growth chart.
    """
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


def track_golden_hour_report(
    user_id: Optional[int],
    report: Optional[dict] = None,
    **extra,
) -> None:
    """Emit one golden-hour presence reading (issue #622) — per reply sweep and per second-wave
    self-comment, so "did the amplifier actually fire inside the window?" is a query instead of a
    log grep. `latency_minutes` stays None when the publish time is unknown, and `within_window` is
    then False: an unmeasured sweep must never count as an on-time one.
    """
    report = dict(report or {})
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="golden_hour_report",
        properties={
            "user_id": user_id,
            "phase": report.get("phase"),
            "post_id": report.get("post_id"),
            "sweep_slot": report.get("sweep_slot"),
            "status": report.get("status"),
            "latency_minutes": report.get("latency_minutes"),
            "within_window": bool(report.get("within_window")),
            "window_minutes": report.get("window_minutes"),
            "comments_found": int(report.get("comments_found") or 0),
            "replies_sent": int(report.get("replies_sent") or 0),
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
    a confirmed verdict the DOM never gave us.

    `author_replied` is the metric of the pilot prompt experiment (issue #652), so the user's arm
    rides along. It is resolved HERE, at read time, rather than stored with the comment: PostHog's
    assignment is deterministic per person for the life of the flag, and a per-comment copy would
    still be wrong if the flag were re-rolled — see the attribution caveat in docs/experiments.md.
    """
    outcome = dict(outcome or {})
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="comment_outcome",
        properties={
            **experiment_props(user_id, keys=(COMMENT_CONTRACT_PROMPT,)),
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


# A DOM sample is evidence, not a metric: enough rows to recognise a shape, capped so one rotated
# surface cannot post a page's worth of markup per reading.
_SELECTOR_EVIDENCE_MAX_CANDIDATES = 8


def track_selector_evidence(surface: str, candidates: Optional[list] = None,
                            user_id: Optional[int] = None, **extra) -> None:
    """Emit ONE bounded DOM sample for a Selenium locator chain that resolved nothing (issue #1117).

    Re-grounding a rotated LinkedIn surface needs the page's own shape, and a log line cannot carry
    it to anyone: prod runs `LOG_LEVEL=INFO` with `POSTHOG_LOG_LEVEL=WARNING`, so a DEBUG capture is
    dropped before it leaves the worker, and raising the level to be seen would file a grouped
    `$exception` for a page we are only trying to read. An event is the level-independent product,
    and it is queryable next to the reading the miss starved (`comment_outcome`).

    An EMPTY `candidates` list is still emitted: "the scan found nothing describable" is the reading
    that says the capture itself is blind, and suppressing it is how a surface looks un-drifted.
    """
    candidates = list(candidates or [])[:_SELECTOR_EVIDENCE_MAX_CANDIDATES]
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="sdui_selector_evidence",
        properties={
            "surface": surface,
            "user_id": user_id,
            "candidate_count": len(candidates),
            "candidates": candidates,
            **extra,
        },
    )


def track_suppression_check(user_id: Optional[int], verdict: Optional[dict] = None,
                            paused: bool = False, **extra) -> None:
    """Emit one daily suppression-tripwire reading (issue #629). Every check is emitted, not just
    the trips: the whole point is to see the reach curve BEFORE the step-collapse, and a series that
    only has trips in it cannot show the run-up.
    """
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
    is queryable next to the rates that caused it and a PostHog alert can page off it.
    """
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
            "unreadable_readings": report.get("unreadable_readings"),
            "verdict": verdict.get("status"),
            "verdict_reason": verdict.get("reason"),
            **extra,
        },
    )


def track_content_quality(user_id: Optional[int], score: Optional[dict] = None, **extra) -> None:
    """Emit ONE nightly per-piece content-quality reading (issue #630) — slop score, self-similarity,
    the stored authenticity score, hook length vs the 140-char mobile budget, and engagement per
    impression once stats exist — as a `content_quality` event.

    Every unmeasured dimension stays None: an event that reported 0 for a post with no impressions
    yet would drag every ER average toward zero the night it shipped. Content BODIES are never sent
    (they are the user's own LinkedIn material, redacted everywhere else too) — only the names of the
    slop checks that fired, which is what makes a regression explainable.
    """
    score = dict(score or {})
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="content_quality",
        properties={
            "user_id": user_id,
            "surface": score.get("surface"),
            "ref_id": score.get("ref_id"),
            "shipped_on": str(score.get("shipped_on")) if score.get("shipped_on") else None,
            "chars": score.get("chars"),
            "slop_checked": score.get("slop_checked"),
            "slop_hard": score.get("slop_hard"),
            "slop_warn": score.get("slop_warn"),
            "slop_score": score.get("slop_score"),
            "slop_checks": score.get("slop_checks") or [],
            "similarity": score.get("similarity"),
            "similarity_measure": score.get("similarity_measure"),
            "authenticity_score": score.get("authenticity_score"),
            "hook_chars": score.get("hook_chars"),
            "hook_within_budget": score.get("hook_within_budget"),
            "engagement_rate": score.get("engagement_rate"),
            "impressions": score.get("impressions"),
            "detector_score": score.get("detector_score"),
            "detector_provider": score.get("detector_provider"),
            "video_render_ok": score.get("video_render_ok"),
            "video_model_tier": score.get("video_model_tier"),
            "video_duration_seconds": score.get("video_duration_seconds"),
            "video_aspect_ratio": score.get("video_aspect_ratio"),
            "video_asset_probe": score.get("video_asset_probe"),
            **extra,
        },
    )


def track_content_quality_rollup(user_id: Optional[int], rollup: Optional[dict] = None,
                                 **extra) -> None:
    """Emit the weekly content-quality rollup (issue #630) as one `content_quality_rollup` event:
    this period's summary, the prior period's, the deltas between them, and any regression alert that
    fired. Both periods ride on the SAME event so a dashboard tile (and a PostHog alert) can read the
    regression without joining two time ranges — the comparison is the point of the event.
    """
    rollup = dict(rollup or {})
    current = dict(rollup.get("current") or {})
    prior = dict(rollup.get("prior") or {})
    deltas = dict(rollup.get("deltas") or {})
    alerts = [a for a in (rollup.get("alerts") or []) if isinstance(a, dict)]
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="content_quality_rollup",
        properties={
            "user_id": user_id,
            "days": rollup.get("days"),
            "alert_count": len(alerts),
            "alerts": [a.get("name") for a in alerts],
            "alert_reasons": [a.get("reason") for a in alerts],
            **{f"current_{key}": value for key, value in current.items() if key != "by_surface"},
            **{f"prior_{key}": value for key, value in prior.items() if key != "by_surface"},
            **{f"delta_{key}": value for key, value in deltas.items()},
            "by_surface": current.get("by_surface") or {},
            "config": rollup.get("config") or {},
            **extra,
        },
    )


def track_pre_post_engagement(post_id: int, user_id: Optional[int], status: str, **extra) -> None:
    """Emit the per-post pre-post engagement-window marker (issue #547) — dispatched, skipped (with
    the reason) or ran (with the comment count) — so a report can confirm the warm-up before a post
    actually fired instead of inferring it from task logs.
    """
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


def track_company_page_invite_run(user_id: Optional[int], report: Optional[dict] = None,
                                  **extra) -> None:
    """Emit one company-page invite run (issue #732) — EVERY run, including the ones that sent
    nothing. The lane used to be a once-a-month blast with no volume series at all; a series that
    only carried sends could not distinguish "paced down to zero today" from "silently broken", so
    the skip reason (budget_reached / credits_exhausted / paused / disabled) is the point.
    """
    report = dict(report or {})
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="company_page_invite_run",
        properties={
            "user_id": user_id,
            "status": report.get("status"),
            "invites_sent": int(report.get("invites_sent") or 0),
            "budget": int(report.get("budget") or 0),
            "cap": int(report.get("cap") or 0),
            "sent_today": int(report.get("sent_today") or 0),
            "credits_remaining": report.get("credits_remaining"),
            "credit_spread": report.get("credit_spread"),
            **extra,
        },
    )


def track_stale_invite_run(user_id: Optional[int], report: Optional[dict] = None,
                           **extra) -> None:
    """Emit one stale-invite withdrawal run (issue #969) — EVERY run, including the ones that
    withdraw nothing.

    This lane replaced a beat that had been a no-op stub while LOOKING operational, so a series that
    only carried withdrawals would reproduce exactly the problem it was written to fix. `rows_seen`
    is the tell: zero rows day after day on an account with pending invites means the invitation
    manager's markup moved, not that the account is clean.
    """
    report = dict(report or {})
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="stale_invite_run",
        properties={
            "user_id": user_id,
            "status": report.get("status"),
            "withdrawn": int(report.get("withdrawn") or 0),
            "unverified": int(report.get("unverified") or 0),
            "budget": int(report.get("budget") or 0),
            "cap": int(report.get("cap") or 0),
            "withdrawn_today": int(report.get("withdrawn_today") or 0),
            "threshold_days": report.get("threshold_days"),
            "rows_seen": int(report.get("rows_seen") or 0),
            "stale_seen": int(report.get("stale_seen") or 0),
            "unreadable": int(report.get("unreadable") or 0),
            # Rows refused because the Withdraw control named somebody the row does not (#1006). A
            # PARTIAL mismatch is the label drifting by a row — the #1012 hazard — and without this
            # it is invisible: those rows are dropped before `stale_seen`, so the run reports
            # "nothing old enough" and looks identical to a healthy account.
            "entity_mismatch": int(report.get("entity_mismatch") or 0),
            "expansions": int(report.get("expansions") or 0),
            **extra,
        },
    )


def track_catchup_run(user_id: Optional[int], report: Optional[dict] = None, **extra) -> None:
    """Emit one LinkedIn Catch-up run (issue #792) — EVERY run of BOTH phases, including the ones
    that drafted or sent nothing.

    The lane used to be write-only: a scan that found no milestone, a scan the 429 breaker never let
    start, and a scan whose selectors had drifted all produced the same thing — silence — so a user
    reporting "catch-up never sends anything" could not be answered from telemetry at all. The
    `status` and the per-stage funnel counts ARE the point: they say which stage the moments died at
    (`scanned` -> `classified` -> `enabled` -> after exclusion/dedup/score -> `drafted`).

    Three phases, because a `dispatched` touch is not a sent one: `scan` drafts, `send` is the drip
    that dispatches, and `deliver` is the per-touch terminal outcome. A touch the account-wide DM cap
    defers goes back to 'approved' and is re-dispatched on the next beat, so only the `deliver` phase
    can tell a lane that sends from one that has looped all day without delivering anything.
    """
    report = dict(report or {})
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="catchup_run",
        properties={
            "user_id": user_id,
            "phase": report.get("phase"),
            "status": report.get("status"),
            "moments": int(report.get("moments") or 0),
            "classified": int(report.get("classified") or 0),
            "enabled_type": int(report.get("enabled_type") or 0),
            "excluded": int(report.get("excluded") or 0),
            "duplicate": int(report.get("duplicate") or 0),
            "below_bar": int(report.get("below_bar") or 0),
            "drafted": int(report.get("drafted") or 0),
            "auto_approve": bool(report.get("auto_approve")),
            "message_source": report.get("message_source"),
            "dispatched": int(report.get("dispatched") or 0),
            "capped": int(report.get("capped") or 0),
            "inactive": int(report.get("inactive") or 0),
            "pending": int(report.get("pending") or 0),
            "requeued": int(report.get("requeued") or 0),
            "touch_id": report.get("touch_id"),
            **extra,
        },
    )


def track_feed_scan(user_id: Optional[int], funnel: Optional[dict] = None, **extra) -> None:
    """Emit one feed/roster commenting scan (issue #817) — EVERY scan, including the ones that
    comment on nothing.

    `feed_sort` is the load-bearing property. Issue #622 made the scoring matrix recency-dominant,
    so a scan that ran while the 'Sort by -> Recent' control could not be found ranked a candidate
    pool LinkedIn had already reordered by engagement. That miss was only ever a log line, which
    meant an unsorted scan and a recency-sorted one were indistinguishable in the funnel — and
    #622's effect was being measured against a silent mix of the two. It is a STRING (never a
    boolean) so alert tiles can filter on it; `recent` is the only value that means sorted.
    """
    funnel = dict(funnel or {})
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="feed_scan",
        properties={
            "user_id": user_id,
            "feed_sort": funnel.get("feed_sort"),
            "examined": int(funnel.get("examined") or 0),
            "passed_filters": int(funnel.get("passed_filters") or 0),
            "matched_topics": int(funnel.get("matched_topics") or 0),
            "commented": int(funnel.get("commented") or 0),
            "roster_commented": int(funnel.get("roster_commented") or 0),
            "feed_commented": int(funnel.get("feed_commented") or 0),
            # Roster targets that rendered posts but no comment affordance, and targets followed on
            # this scan (issue #962). Both are roster-only; a rising blocked count with a flat
            # followed count is a roster the user has to fix by connecting, not a broken selector.
            "roster_comment_blocked": int(funnel.get("roster_comment_blocked") or 0),
            "roster_followed": int(funnel.get("roster_followed") or 0),
            "off_topic_skipped": int(funnel.get("off_topic_skipped") or 0),
            "fallback_used": bool(funnel.get("fallback_used")),
            # Group-feed lane only (issue #1084): posts whose composer was not reachable before the
            # LLM generation was spent. Counted on `feed_scan` so the cost saving is measurable.
            "skipped_no_composer": int(funnel.get("skipped_no_composer") or 0),
            **extra,
        },
    )


def track_margin_report(report: dict) -> None:
    """Emit the weekly unit-economics scorecard (plan §E.1.4) as one `margin_report` event so the
    PostHog tiles read system margin, cohort margin and LTV:CAC without re-deriving them. Per-user
    financials stay out of the event body — internal-only by policy (plan §E.5) — but the cohort
    aggregates the Margin-by-Cohort dashboard needs ride along.
    """
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


def track_image_gate_verdict(
    surface: str,
    verdict: str,
    issues: list,
    attempt_count: int,
    checked: bool,
    acceptable: bool,
    user_id: Optional[int] = None,
    post_id: Optional[int] = None,
) -> None:
    """Emit an image gate verdict event for observability at POSTHOG_LOG_LEVEL=WARNING.

    Args:
        surface: the image surface (post_image, carousel, newsletter, video, thumbnail)
        verdict: "accepted", "rejected", or "unchecked"
        issues: list of issue strings from the vision gate
        attempt_count: number of render attempts made
        checked: whether the vision gate ran (True) or failed open (False)
        acceptable: whether the final render was deemed acceptable
        user_id: optional user ID
        post_id: optional post ID
    """
    # Emit as a custom event; will be visible at POSTHOG_LOG_LEVEL=WARNING or lower
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="image_gate_verdict",
        properties={
            "surface": surface,
            "verdict": verdict,
            "issues": issues,
            "attempt_count": attempt_count,
            "checked": checked,
            "acceptable": acceptable,
            "user_id": user_id,
            "post_id": post_id,
        },
    )


def track_routing_policy(report: dict) -> None:
    """Emit the weekly cost-aware routing decision (plan §D.1(1)) as one `routing_policy` event, so
    a down-route — and especially an auto-rollback — is queryable next to the cost it was meant to
    save and the engagement it was gated on. The full comparison stats stay out of the event body;
    the per-bucket verdict and cohort are what a dashboard needs.
    """
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
                         "cohort_pct": b.get("cohort_pct"),
                         "assignment": b.get("assignment")} for key, b in buckets.items()],
            # Whether the PostHog experiment (issue #652) actually cohorted this run, or the hash
            # fallback did — a treatment share of None means nobody was enrolled.
            **{f"cohort_{key}": value for key, value in (report.get("cohort") or {}).items()},
            "recommendations": report.get("recommendations") or [],
        },
    )


def track_experiment_exposure(experiment: str, variant: str, user_id: Optional[int] = None,
                              **extra) -> None:
    """Emit one PostHog experiment EXPOSURE (issue #652).

    The event name and the two `$feature_flag*` properties are not ours to rename: they are what
    PostHog's experiment engine reads to decide which variant a person was in, and every metric
    (post_outcome, comment_outcome, $ai_generation) is attributed to an arm through them. `experiment`
    is carried as a plain property too so a HogQL readout can group without knowing PostHog's
    internals.

    Deduping is the CALLER's job (`experiments.track_exposure`) — this stays a dumb emitter like every
    other tracker here.
    """
    posthog.capture(
        distinct_id=str(user_id if user_id is not None else "system"),
        event="$feature_flag_called",
        properties={
            "$feature_flag": experiment,
            "$feature_flag_response": variant,
            f"$feature/{experiment}": variant,
            "experiment": experiment,
            "variant": variant,
            "user_id": user_id,
            **extra,
        },
    )


def experiment_props(user_id: Optional[int] = None, keys: Optional[tuple] = None,
                     shipped: Optional[dict] = None) -> dict:
    """`$feature/<key>` properties for a metric event, or `{}` when experiments can't be resolved.

    Wrapped here (rather than imported at each tracker) so an experiment plane that is down, missing
    or misconfigured can never stop an outcome event from being recorded — the outcome is the
    valuable half; its experiment label is not.
    """
    try:
        from cqc_lem.utilities.experiments import experiment_properties
        return experiment_properties(user_id, keys=keys, extra=shipped)
    except Exception:
        return {}


def track_cost_alert(alert: dict, day: Optional[str] = None) -> None:
    """Emit one budget/anomaly alert (plan §E.2) as a `cost_alert` event so a breach is queryable
    next to the spend it came from, and a PostHog alert can page off it. Per-user alerts are keyed
    to that user's distinct_id; system-wide ones to "system".
    """
    alert = dict(alert or {})
    posthog.capture(
        distinct_id=str(alert.get("user_id") or "system"),
        event="cost_alert",
        properties={"date": day, **alert},
    )


def track_capacity_alert(alert: dict, generated_at: Optional[str] = None) -> None:
    """Emit one Selenium/lane capacity breach (issue #552) as a `capacity_alert` event, so the
    saturation history is queryable next to the task latency it explains and a PostHog alert can page
    off it. Always system-scoped: a full browser pool is an infra limit, not one user's problem.
    """
    alert = dict(alert or {})
    posthog.capture(
        distinct_id="system",
        event="capacity_alert",
        properties={"generated_at": generated_at, **alert},
    )


def track_youtube_token_check(state: Optional[dict] = None) -> None:
    """Emit one YouTube OAuth refresh-token probe (issue #742) as a `youtube_token_check` event.

    The dated OK line in the logs is the audit trail; this is the queryable series behind it, so
    "when did publishing actually go bad?" is answerable without grepping a year of logs. Always
    system-scoped: one OAuth grant publishes the whole channel, not one user's.
    """
    state = dict(state or {})
    posthog.capture(
        distinct_id="system",
        event="youtube_token_check",
        properties={"status": state.get("status"), "reason": state.get("reason"),
                    "error": state.get("error"), "scope": state.get("scope"),
                    "checked_at": state.get("checked_at"),
                    "http_status": state.get("http_status")},
    )


def track_rate_limit_trip(seconds: int, trips: int, reason: str = "429") -> None:
    """Emit one LinkedIn 429 breaker trip (issue #650) as a `rate_limit_trip` event.

    Until now a trip only produced a WARNING log, which never reaches PostHog at the default
    POSTHOG_LOG_LEVEL — so the one signal that says "LinkedIn is throttling us" was invisible to
    both dashboards and alerts. `trips` is the CONSECUTIVE-trip counter, so an escalating doom loop
    is distinguishable from a single unlucky session. Always system-scoped: LinkedIn rate-limits by
    egress IP, so a trip is an account-wide condition, not one user's.

    Never raises: the breaker must open even when analytics is down.
    """
    try:
        posthog.capture(
            distinct_id="system",
            event="rate_limit_trip",
            properties={"cooldown_seconds": int(seconds), "consecutive_trips": int(trips),
                        "reason": reason or "429"},
        )
    except Exception as e:
        log_debug("Could not capture rate-limit trip event", exc=e)


def session_replay_url(session_id: Optional[str]) -> Optional[str]:
    """The PostHog replay permalink for a browser session id (issue #649), or None when there is no
    id or no project configured — a link that can't be built is simply omitted, never guessed.

    The SPA sends its `posthog_session_id` with every feedback report; this is what turns that
    opaque id into something a human can open. Ids that aren't the SDK's own uuid-ish shape are
    rejected rather than escaped: the result is pasted into GitHub markdown, and a "session id"
    carrying a space or a bracket is not a session id.
    """
    sid = str(session_id or "").strip()
    project_id = (os.getenv("POSTHOG_PROJECT_ID") or "").strip()
    if not sid or not project_id or not _SESSION_ID_RE.fullmatch(sid):
        return None
    host = (os.getenv("POSTHOG_APP_HOST") or "https://us.posthog.com").rstrip("/")
    return f"{host}/project/{project_id}/replay/{sid}"


def posthog_hogql_query(sql: str, timeout: int = 30) -> Optional[list]:
    """Run a HogQL query against the PostHog query API and return its result ROWS, or None when the
    read path isn't configured (no personal API key / project) or the call fails. None means
    "unknown" — never zero — so a check reading it reports itself skipped instead of alerting on a
    missing analytics plane. Reads only; the write/provision path lives in scripts/posthog_dashboards.py.
    """
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
    completes — so time-to-aha and per-step drop-off are queryable in PostHog.
    """
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
    is measurable against the GA satisfaction gate.
    """
    posthog.capture(
        distinct_id=str(user_id),
        event="shipped_notice",
        properties={"issue_number": issue_number, **extra},
    )


def track_survey_response(user_id: int, source: str, **extra) -> None:
    """Emit an NPS/review answer (issue #501) with its score/rating, so NPS and CSAT can be trended
    in PostHog next to the activation funnel.
    """
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
CHANNEL_YOUTUBE = "youtube"
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
    CHANNEL_YOUTUBE,
    CHANNEL_DIRECT,
    CHANNEL_OTHER,
)

# Client-supplied attribution is allow-listed: a funnel event's schema is ours, not the caller's.
# `ref` is the referral link's referrer id (issue #658) — it rides beside the UTMs rather than
# inside utm_content because it names a PERSON, not a creative variant.
_ATTRIBUTION_KEYS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "referrer", "landing_page", "ref",
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
    # The tutorial videos (issue #505) tag `utm_source=youtube`. YouTube passes no usable referrer,
    # so without its own bucket every video-driven signup would land in `other`.
    ("youtube", CHANNEL_YOUTUBE),
    ("youtu.be", CHANNEL_YOUTUBE),
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
    never direct.
    """
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
    # A `ref` with no UTMs is still a referral: our own referral links carry both, but a member who
    # pastes the link into a DM strips query params often enough that this is a real arrival shape.
    if _clean_property(data.get("ref")):
        return CHANNEL_REFERRAL

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
    PostHog breakdown never has an ungrouped bucket.
    """
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
    needs a stable key, not the address itself.
    """
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

    Never raises: analytics must not fail a signup or a billing webhook.
    """
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


AFFILIATE_ENROLLED = "affiliate_enrolled"
AFFILIATE_OPTED_OUT = "affiliate_opted_out"
AFFILIATE_PROMO_CONSENT = "affiliate_promo_consent"
AFFILIATE_REFERRAL_ATTRIBUTED = "affiliate_referral_attributed"
AFFILIATE_REFERRAL_REJECTED = "affiliate_referral_rejected"
AFFILIATE_REFERRAL_CONVERTED = "affiliate_referral_converted"
AFFILIATE_REWARD_GRANTED = "affiliate_reward_granted"
AFFILIATE_REWARD_REVOKED = "affiliate_reward_revoked"
AFFILIATE_DISCLOSURE_BLOCKED = "affiliate_disclosure_blocked"
# The (B) generator's own three moments (issue #770). `blocked` is the GENERATION-time refusal (a
# draft that could not be made compliant), which is a different event from
# `affiliate_disclosure_blocked` — that one is the publish gate catching content that arrived
# undisclosed. Summing them would double-count one piece of content that failed twice.
AFFILIATE_PROMO_GENERATED = "affiliate_promo_generated"
AFFILIATE_PROMO_PUBLISHED = "affiliate_promo_published"
AFFILIATE_PROMO_BLOCKED = "affiliate_promo_blocked"

AFFILIATE_EVENTS = (
    AFFILIATE_ENROLLED,
    AFFILIATE_OPTED_OUT,
    AFFILIATE_PROMO_CONSENT,
    AFFILIATE_REFERRAL_ATTRIBUTED,
    AFFILIATE_REFERRAL_REJECTED,
    AFFILIATE_REFERRAL_CONVERTED,
    AFFILIATE_REWARD_GRANTED,
    AFFILIATE_REWARD_REVOKED,
    AFFILIATE_DISCLOSURE_BLOCKED,
    AFFILIATE_PROMO_GENERATED,
    AFFILIATE_PROMO_PUBLISHED,
    AFFILIATE_PROMO_BLOCKED,
)


def track_affiliate_event(event: str, user_id: Optional[int] = None, **extra) -> None:
    """Emit one affiliate-program event (issue #737) so the marketing arm is measurable on the same
    #650/#658 dashboards as the rest of the funnel.

    Deliberately NOT a `track_funnel_event`: the acquisition funnel is one ordered path per person,
    and an affiliate event is about the REFERRER, not about the person moving through the funnel.
    Emitting them there would put one person's referral conversions inside another person's journey.
    Never raises — analytics must not fail an opt-out.
    """
    from cqc_lem.utilities.logger import log_warning
    try:
        if event not in AFFILIATE_EVENTS:
            log_warning(f"Unknown affiliate event '{event}' — emitting anyway")
        posthog.capture(
            distinct_id=str(user_id) if user_id is not None else "system",
            event=event,
            properties={"user_id": user_id, **extra},
        )
    except Exception as e:
        log_warning(f"Could not track affiliate event '{event}'", exc=e, user_id=user_id)


def track_task(
    task_name: str,
    duration_ms: int,
    success: bool = True,
    user_id: Optional[int] = None,
    **extra,
) -> None:
    """Emit `celery_task` — one row per task run, however it ended.

    Its only caller is `my_celery.on_task_postrun`, which is what makes this event a complete census
    of the queue rather than a sample: capturing it from inside a task as well would double-count
    that task. A task with no user (the scheduler beats) lands on the shared `"system"` person
    instead of being dropped, since an unattributed run still has to show up in the count.
    """
    posthog.capture(
        distinct_id=str(user_id or "system"),
        event="celery_task",
        properties={"task": task_name, "duration_ms": duration_ms, "success": success, **extra},
    )


def track_inbound_email(verdict: str, user_id: Optional[int] = None) -> None:
    """One event per SendGrid Inbound Parse POST, carrying the dispatch verdict (comment_accepted /
    debounced / unknown_reply_token / …). The webhook drops most mail BY DESIGN, so without this a
    broken forwarding chain is indistinguishable from no mail arriving at all — weeks of 100%-ignored
    traffic left no signal anywhere. `verdict` is a STRING prop so alert tiles can filter on it
    (docs/kpi-dashboards.md).
    """
    posthog.capture(
        distinct_id=str(user_id or "anonymous"),
        event="inbound_parse_email",
        properties={"verdict": verdict},
    )


def track_api_call(
    route: str,
    method: str,
    status_code: int,
    latency_ms: int,
    user_id: Optional[int] = None,
) -> None:
    """Emit `api_call` — one row per HTTP request, from the middleware's `finally`.

    `status_code` is what the caller actually received, INCLUDING the 500 an unhandled exception
    became, so this counts failures rather than only the requests that survived. Callers with no
    session land on the shared `"anonymous"` person, which is the only way public-surface traffic
    is visible at all.
    """
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
