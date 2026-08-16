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
dev — a call site should never guard itself on the key. Under pytest it is disabled REGARDLESS of the
key, because a test run can inherit the production key without anyone intending it; see
`logger._running_under_pytest`, which the OTLP logs hop reads too.
"""

import contextvars
import hashlib
import inspect
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import wraps
from typing import Callable, Iterator, NamedTuple, Optional, Tuple
from urllib.parse import urlparse

import posthog

from cqc_lem.utilities.experiments import (
    COMMENT_CONTRACT_PROMPT,
    COST_ROUTING_ARM,
    POST_MEDIA_VARIANT,
)
from cqc_lem.utilities.logger import _running_under_pytest, log_debug, log_warning
from cqc_lem.utilities.posthog_keys import runtime_api_key

posthog.api_key = os.getenv("POSTHOG_API_KEY", "")
posthog.host = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")

def _posthog_on_error(e, items) -> None:
    print(f"[PostHog] delivery error: {e}", file=sys.stderr)

posthog.on_error = _posthog_on_error


# `_running_under_pytest` is imported from `logger` rather than defined twice: the SDK hop here and
# the OTLP logs hop there both read POSTHOG_API_KEY at import, so they answer the same question and
# a second copy could only drift (#1451 fixed this half, #1460 the other). logger imports nothing
# from this module at import time, so the direction is safe.

# Disable PostHog when no key configured (local dev without key), and ALWAYS under pytest.
if not posthog.api_key or _running_under_pytest():
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
#
# Gated on `posthog.disabled` rather than on the key, so it answers the pytest question too — this
# was the one hop in the chain that still only asked "is a key configured?" (#1498). The excepthook
# is installed by the SDK, not by us: an autocapture that stays armed under pytest is a global
# `sys.excepthook` / `threading.excepthook` swap made mid-suite, and the only thing standing between
# it and a real `$exception` from a fixture is the client's own disabled check.
def _exception_autocapture_enabled() -> bool:
    """Whether the SDK's uncaught-exception excepthook may be armed in this process.

    A function rather than an inline expression so both branches are testable: re-importing this
    module to exercise the other one would rebind `posthog.disabled` for the rest of the suite,
    which is the very guard being tested.
    """
    return not posthog.disabled and _env_flag("POSTHOG_EXCEPTION_AUTOCAPTURE")


EXCEPTION_AUTOCAPTURE_ENABLED = _exception_autocapture_enabled()
posthog.enable_exception_autocapture = EXCEPTION_AUTOCAPTURE_ENABLED

# What posthog-js hands out as a session id (uuid v7-ish). Anything else is not linked (#649).
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,64}")


# --- The declarative event spec (issue #1218) ----------------------------------------------
# Every server-side capture below goes through ONE `_emit()`, and what an event CARRIES is data
# rather than code: `EVENTS` is the registry, each `track_*` function a one-liner whose docstring
# says why the event exists. Thirty-six hand-written capture wrappers had thirty-six chances to get
# the distinct_id rule, a coercion or a property name subtly wrong, and no way to state a contract
# that held across all of them.
#
# The coercions are the point, not decoration. `label()` marks a property a dashboard or an ALERT
# filters on and forces it to a string, because PostHog matches a property filter against the
# INGESTED type: a tile filtering `status = "paused"` matches nothing if the rows carry booleans, and
# the alert then silently never fires — worse than having no alert (docs/kpi-dashboards.md). That
# rule used to live as prose in three docstrings and could not be tested; declared per property, one
# test now proves it for every event at once.

DISTINCT_USER = "user"              # str(user_id or "system")
DISTINCT_USER_STRICT = "user_strict"  # str(user_id if user_id is not None else "system")
DISTINCT_REQUIRED = "required"      # str(user_id) — the call site guarantees one
DISTINCT_ANONYMOUS = "anonymous"    # str(user_id or "anonymous") — public, pre-login surfaces
DISTINCT_SYSTEM = "system"          # account-wide condition, never one user's


def _verbatim(value):
    return value


def _count(value) -> int:
    return int(value or 0)


def _count_or_none(value) -> Optional[int]:
    return int(value) if value else None


def _flag(value) -> bool:
    return bool(value)


def _items(value) -> list:
    return list(value or [])


def _string(value) -> Optional[str]:
    return None if value is None else str(value)


class Field(NamedTuple):
    """One property on an event: its name, where to read it, and the shape it lands in.

    `key` is a DOTTED path into the reading the tracker was handed, so a nested value
    (`verdict.status`) needs no unpacking code at the call site.
    """

    name: str
    coerce: Callable = _verbatim
    key: Optional[str] = None
    filtered: bool = False


def prop(name: str, key: Optional[str] = None) -> Field:
    """A property carried verbatim — the reading is already the shape the dashboards want."""
    return Field(name, _verbatim, key)


def count(name: str, key: Optional[str] = None) -> Field:
    """A counter that is 0 when absent: for these, "not reported" and "none happened" are the same."""
    return Field(name, _count, key)


def count_or_none(name: str, key: Optional[str] = None) -> Field:
    """A count whose ABSENCE is the reading (impressions we could not see), so it stays None.

    A 0 here would show up in a growth chart as a real collapse.
    """
    return Field(name, _count_or_none, key)


def flag(name: str, key: Optional[str] = None) -> Field:
    """A real boolean — something the event ASSERTS, never something an alert tile filters on."""
    return Field(name, _flag, key)


def items(name: str, key: Optional[str] = None) -> Field:
    """A list property, empty rather than None so a breakdown never has to handle a null array."""
    return Field(name, _items, key)


def text(name: str, key: Optional[str] = None) -> Field:
    """A string property no dashboard filters on — a rendered date, a free-text reason."""
    return Field(name, _string, key)


def label(name: str, key: Optional[str] = None) -> Field:
    """A string a dashboard tile or a PostHog ALERT filters on.

    Forced to a string on the way out: PostHog evaluates a property filter against the ingested
    type, so one boolean row makes `status = "paused"` match nothing and the alert never fires.
    A numeric property an alert COMPARES (`status_code >= 500`) is the opposite case — that stays
    `prop()`, because stringifying it would break the comparison instead of fixing it.
    """
    return Field(name, _string, key, True)


class EventSpec(NamedTuple):
    """One PostHog event end to end: its name, the properties it carries, whose person it lands on."""

    event: str
    fields: Tuple[Field, ...] = ()
    distinct: str = DISTINCT_USER


def _read(source: dict, path: str):
    """The value at a dotted `path` in a reading, or None as soon as any step is missing."""
    value = source
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _distinct_id(rule: str, user_id) -> str:
    """The person an event lands on.

    An unattributed run still has to appear in the count, so a missing user falls back to a shared
    `"system"` / `"anonymous"` sentinel rather than the row being dropped.
    """
    if rule == DISTINCT_SYSTEM:
        return "system"
    if rule == DISTINCT_REQUIRED:
        return str(user_id)
    if rule == DISTINCT_ANONYMOUS:
        return str(user_id or "anonymous")
    if rule == DISTINCT_USER_STRICT:
        return str(user_id if user_id is not None else "system")
    return str(user_id or "system")


def _emit(spec: EventSpec, source: Optional[dict] = None, extra: Optional[dict] = None,
          event: Optional[str] = None, distinct_id: Optional[str] = None) -> None:
    """Capture ONE event built from `spec` — the single `posthog.capture` every tracker here runs.

    `source` is the reading the tracker was handed (its report dict plus whatever its own arguments
    add, the arguments last so an explicit one always wins). `extra` is the caller's own `**extra`
    and is applied LAST, so a call site can still add a property or override a declared one — the
    behaviour every wrapper had when it spread `**extra` at the end of its literal. `event` and
    `distinct_id` are for the trackers whose name or person is decided per call
    (`track_affiliate_event`, `track_funnel_event`).

    Because `extra` lands after the coercions, a property a dashboard or an ALERT FILTERS on must be
    a declared field fed from `source` — one arriving through `**extra` reaches PostHog untouched
    and the string contract cannot see it (that is why `track_task` names `state` explicitly).
    """
    data = source if isinstance(source, dict) else {}
    properties = {field.name: field.coerce(_read(data, field.key or field.name))
                  for field in spec.fields}
    if extra:
        properties.update(extra)
    posthog.capture(
        distinct_id=distinct_id or _distinct_id(spec.distinct, data.get("user_id")),
        event=event or spec.event,
        properties=properties,
    )


EVENTS = {spec.event: spec for spec in (
    # --- LLM + media cost ---------------------------------------------------------------------
    EventSpec("llm_call", (
        label("model"), prop("prompt_tokens"), prop("completion_tokens"), prop("total_tokens"),
        prop("cost_usd"), prop("latency_ms"), flag("success"), prop("user_id"),
        label("feature"), text("model_tier"), flag("cached"),
    )),
    EventSpec("media_cost", (
        label("kind"), label("provider"), text("model"), prop("cost_usd"), prop("qty"),
        prop("user_id"), prop("post_id"), label("feature"),
    )),
    EventSpec("cost_alert", (prop("date"),)),
    # The pipeline-trace skeleton the proxy's $ai_generation events hang off. `$ai_span_name` is
    # NOT a label(): PostHog's own trace viewer reads these `$ai_*` keys, so their shapes are its
    # contract rather than ours. Emitted under this name and as `$ai_trace` for the root.
    EventSpec("$ai_span", (
        prop("$ai_trace_id"), prop("$ai_span_id"), prop("$ai_span_name"), prop("$ai_latency"),
        label("feature"), prop("user_id"),
    ), DISTINCT_USER_STRICT),
    EventSpec("capacity_alert", (prop("generated_at"),), DISTINCT_SYSTEM),
    EventSpec("margin_report", (
        prop("period_start", "period.start"), prop("period_end", "period.end"),
        prop("period_days", "period.days"), label("basis", "period.basis"),
        prop("ledger_available"), items("cohorts"),
    ), DISTINCT_SYSTEM),
    EventSpec("routing_policy", (
        prop("date"), prop("enabled", "policy.enabled"), prop("window_days"),
        prop("observations"), prop("change_count"), prop("rollback_count"),
        items("changes"), items("buckets"), items("recommendations"),
    ), DISTINCT_SYSTEM),

    # --- Content + engagement outcomes --------------------------------------------------------
    # `post_type` is a label() and not a text(): "do carousels out-reach text posts?" is a BREAKDOWN
    # on this event (issue #1513), and a dashboard filter matches on the ingested type.
    EventSpec("post_outcome", (
        prop("post_id"), label("post_type"), text("variant_key"), count("reactions"),
        count("comments"), count("reposts"), count("saves"), count_or_none("impressions"),
        prop("engagement"), prop("engagement_rate"),
    )),
    EventSpec("audience_snapshot", (
        prop("user_id"), prop("follower_count"), prop("connection_count"),
        prop("profile_views"), prop("search_appearances"),
    )),
    EventSpec("golden_hour_report", (
        prop("user_id"), label("phase"), prop("post_id"), prop("sweep_slot"), label("status"),
        prop("latency_minutes"), flag("within_window"), prop("window_minutes"),
        count("comments_found"), count("replies_sent"),
    )),
    EventSpec("comment_outcome", (
        prop("user_id"), prop("log_id"), label("status"), label("skip_reason"),
        flag("author_replied"), count("reply_count"), count("like_count"),
        # Three-valued on purpose: None means the DOM never gave us a verdict, and excludes the
        # row from the demotion denominator. A bool() here would invent a confirmed reading.
        prop("visible_most_relevant"), flag("our_reply_sent"),
    )),
    EventSpec("comment_quality", (
        prop("user_id"), prop("days"), prop("sample_size"), prop("checked"), prop("skipped"),
        prop("author_reply_rate"), prop("reply_rate"), prop("like_rate"), prop("demotion_rate"),
        prop("visibility_sample"), prop("unreadable_readings"),
        label("verdict", "verdict.status"), text("verdict_reason", "verdict.reason"),
    )),
    EventSpec("content_quality", (
        prop("user_id"), label("surface"), prop("ref_id"), text("shipped_on"), prop("chars"),
        prop("slop_checked"), prop("slop_hard"), prop("slop_warn"), prop("slop_score"),
        items("slop_checks"), prop("similarity"), prop("similarity_measure"),
        prop("authenticity_score"), prop("hook_chars"), prop("hook_within_budget"),
        prop("engagement_rate"), prop("impressions"), prop("detector_score"),
        prop("detector_provider"), prop("video_render_ok"), prop("video_model_tier"),
        prop("video_duration_seconds"), prop("video_aspect_ratio"), prop("video_asset_probe"),
        # Deck dimensions (issue #1513), carried on the reading whose `surface` is "carousel".
        # `deck_probe` and `deck_template` are the two a breakdown splits on — an unread deck has to
        # be filterable OUT of a clipping chart — so both are label(); the counts stay prop() because
        # they are compared, and NULL is the reading for a deck nothing could measure.
        label("deck_probe"), label("deck_template"), prop("deck_slides"),
        prop("deck_body_chars_avg"), prop("deck_body_chars_max"), prop("deck_chars_dropped"),
        prop("deck_slides_clipped"), prop("deck_slides_with_band"),
    )),
    EventSpec("content_quality_rollup", (
        prop("user_id"), prop("days"), prop("alert_count"), items("alerts"),
        items("alert_reasons"), prop("by_surface"), prop("config"),
    )),
    EventSpec("video_asset_probe", (
        prop("post_id"), prop("user_id"), flag("probe_ok"), label("reason"), label("source"),
    )),
    # Avatar likeness on a video source frame (issue #1279). `present` is a label() and stays
    # three-valued: "True"/"False" are the probe's verdict, None means it never ran, and only a
    # string keeps a breakdown on the verdict working while leaving the unchecked rows out of it.
    # `used_avatar` is the RENDERER's reading for the probed frame (`posts.avatar_media` is only the
    # fallback — it is per POST and sticky) and is what splits a checked-negative into its TWO
    # causes (issue #1430): a bad LoRA render, or the base-Flux fallback `generate_image_with_avatar`
    # substitutes when LoRA inference fails — a frame that legitimately carries no likeness. A raw
    # checked-negative rate mixes them, so it can never decide the hold flag. Three-valued as a
    # string ("true"/"false"/"unknown") because an unreadable flag is not a fallback render.
    EventSpec("avatar_likeness_probe", (
        prop("user_id"), prop("post_id"), label("present"), flag("checked"), text("reason"),
        label("used_avatar"),
    )),
    EventSpec("image_gate_verdict", (
        label("surface"), label("verdict"), items("issues"), prop("attempt_count"),
        flag("checked"), flag("acceptable"), prop("user_id"), prop("post_id"),
    )),
    # What ONE steered slop-lint regeneration did (issue #1434). `outcome` is the measurement: the
    # finished corpus only ever keeps the last draft, so without this the clear-rate of a retry —
    # and how often it trades one HARD check for another — is unknowable. Check NAMES only; the
    # violations' evidence is draft text.
    EventSpec("slop_retry", (
        prop("user_id"), label("surface"), label("outcome"), label("kept"), prop("attempt"),
        prop("max_attempts"), items("before_checks"), items("after_checks"), prop("hard_before"),
        prop("hard_after"), prop("warn_before"), prop("warn_after"),
    )),
    EventSpec("motion_prompt_check", (
        prop("user_id"), prop("post_id"), label("surface"), text("model"), label("verdict"),
        flag("enforced"), prop("attempt"), prop("checked"), prop("passes"), prop("chars"),
        items("checks"), prop("hard_count"), prop("warn_count"), items("evidence"),
    )),

    # --- Engagement lanes ---------------------------------------------------------------------
    EventSpec("feed_scan", (
        prop("user_id"), label("feed_sort"), count("examined"), count("passed_filters"),
        count("matched_topics"), count("commented"), count("roster_commented"),
        count("feed_commented"),
        # Roster targets that rendered posts but no comment affordance, and targets followed on this
        # scan (issue #962). Both are roster-only; a rising blocked count with a flat followed count
        # is a roster the user has to fix by connecting, not a broken selector.
        count("roster_comment_blocked"), count("roster_followed"),
        count("off_topic_skipped"), flag("fallback_used"),
        # Group-feed lane only (issue #1084): posts whose composer was not reachable before the LLM
        # generation was spent. Counted on `feed_scan` so the cost saving is measurable.
        count("skipped_no_composer"),
    )),
    EventSpec("pre_post_engagement", (prop("post_id"), prop("user_id"), label("status"))),
    EventSpec("company_page_invite_run", (
        prop("user_id"), label("status"), count("invites_sent"), count("budget"), count("cap"),
        count("sent_today"), prop("credits_remaining"), prop("credit_spread"),
    )),
    EventSpec("stale_invite_run", (
        prop("user_id"), label("status"), count("withdrawn"), count("unverified"),
        count("budget"), count("cap"), count("withdrawn_today"), prop("threshold_days"),
        count("rows_seen"), count("stale_seen"), count("unreadable"),
        # Rows refused because the Withdraw control named somebody the row does not (#1006). A
        # PARTIAL mismatch is the label drifting by a row — the #1012 hazard — and without this it
        # is invisible: those rows are dropped before `stale_seen`, so the run reports "nothing old
        # enough" and looks identical to a healthy account.
        count("entity_mismatch"), count("expansions"),
    )),
    EventSpec("catchup_run", (
        prop("user_id"), label("phase"), label("status"), count("moments"), count("classified"),
        count("enabled_type"), count("excluded"), count("duplicate"), count("below_bar"),
        count("drafted"), flag("auto_approve"), label("message_source"), count("dispatched"),
        count("capped"), count("inactive"), count("pending"), count("requeued"),
        prop("touch_id"),
    )),
    EventSpec("suppression_check", (
        prop("user_id"), label("status"), flag("tripped"), label("reason"), flag("paused"),
        label("reach_status", "reach.status"), prop("reach_metric", "reach.metric"),
        prop("reach_baseline", "reach.baseline"), prop("reach_max_drop", "reach.max_drop"),
        prop("baseline_posts", "reach.baseline_posts"), prop("posting_days", "reach.posting_days"),
        label("comment_status", "comments.status"),
        prop("comment_demotion_rate", "comments.demotion_rate"),
    )),
    EventSpec("sdui_selector_evidence", (
        label("surface"), prop("user_id"), prop("candidate_count"), items("candidates"),
    )),
    EventSpec("rate_limit_trip", (
        prop("cooldown_seconds"), prop("consecutive_trips"), label("reason"),
    ), DISTINCT_SYSTEM),

    # --- Platform + infra ---------------------------------------------------------------------
    # `state` carries the SAME reading as `success` and exists because the failure alert filters on
    # it: it is the one native property filter the provisioned dashboards use
    # (`state = "FAILURE"`, exact), so a boolean there would match nothing and the page never fires.
    EventSpec("celery_task", (
        prop("task", "task_name"), prop("duration_ms"), flag("success"), label("state"),
    )),
    EventSpec("api_call", (
        label("route"), label("method"), prop("status_code"), prop("latency_ms"),
    ), DISTINCT_ANONYMOUS),
    EventSpec("inbound_parse_email", (label("verdict"),), DISTINCT_ANONYMOUS),
    EventSpec("youtube_token_check", (
        label("status"), label("reason"), text("error"), text("scope"), prop("checked_at"),
        prop("http_status"),
    ), DISTINCT_SYSTEM),

    # --- Experiments, activation, lifecycle ---------------------------------------------------
    # The event name and the two `$feature_flag*` properties are not ours to rename — PostHog's
    # experiment engine reads exactly those to decide which variant a person was in.
    EventSpec("$feature_flag_called", (
        prop("$feature_flag", "experiment"), prop("$feature_flag_response", "variant"),
        label("experiment"), label("variant"), prop("user_id"),
    ), DISTINCT_USER_STRICT),
    EventSpec("onboarding_step", (
        label("step"), prop("hours_since_start"),
    ), DISTINCT_REQUIRED),
    EventSpec("onboarding_nudge", (label("nudge", "nudge_key"),), DISTINCT_REQUIRED),
    EventSpec("survey_prompt", (label("survey", "survey_key"),), DISTINCT_REQUIRED),
    EventSpec("shipped_notice", (prop("issue_number"),), DISTINCT_REQUIRED),
    EventSpec("survey_response", (label("source"),), DISTINCT_REQUIRED),
    # Two events whose NAME is chosen per call — the spec carries the shared property shape and
    # `_emit(..., event=...)` supplies the concrete name from FUNNEL_EVENTS / AFFILIATE_EVENTS.
    EventSpec("funnel_event", (prop("user_id"),)),
    EventSpec("affiliate_event", (prop("user_id"),), DISTINCT_USER_STRICT),
)}


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
        extra = {}
        if parent_id:
            extra["$ai_parent_id"] = parent_id
        if error is not None:
            extra["$ai_is_error"] = True
            extra["$ai_error"] = f"{type(error).__name__}: {error}"
        if properties:
            extra.update({k: v for k, v in properties.items() if v is not None})
        _emit(EVENTS["$ai_span"], {
            "$ai_trace_id": trace_id, "$ai_span_id": span_id, "$ai_span_name": name,
            # PostHog's LLM analytics reads $ai_latency in SECONDS (llm_call's latency_ms is the
            # other convention and the two must not be confused on one chart).
            "$ai_latency": round(max(0.0, time.time() - started), 3),
            "feature": feature or FEATURE_SYSTEM, "user_id": user_id,
        }, extra, event=event)
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
    than a second trace. One pipeline entry point re-enters another (a regenerate flow calling
    `create_text_post`), and two half-traces of one post answer nobody's question.
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
    # A cache hit never reached the provider, so it cost nothing — keeping it at the estimated rate
    # would inflate summed spend on every repeated prompt.
    cost_usd = 0.0 if cached else estimate_llm_cost_usd(resolved_model, prompt_tokens, completion_tokens)
    shadow_cost_usd = None if cached else estimate_shadow_cost_usd(resolved_model, prompt_tokens, completion_tokens)
    _emit(EVENTS["llm_call"], {
        "model": resolved_model, "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens, "cost_usd": cost_usd,
        "latency_ms": latency_ms, "success": success, "user_id": user_id,
        # Floor the bucket here, not just in the callers: a PostHog breakdown on `feature` needs
        # every llm_call to carry one, including direct calls that omit it.
        "feature": feature or FEATURE_SYSTEM, "model_tier": tier, "cached": cached,
        # Shadow cost is a separate decision signal: what the same call would cost if the
        # subscription model were billed at the metered reference. It never enters cost_usd.
    }, {"shadow_cost_usd": shadow_cost_usd} if shadow_cost_usd is not None else None)
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


def track_video_asset_probe(post_id: Optional[int] = None, user_id: Optional[int] = None,
                            probe_ok: bool = False, reason: Optional[str] = None,
                            source: Optional[str] = None, **extra) -> None:
    """Emit one video-asset probe result (issue #1280).

    One row per stored video: `probe_ok`, the failure `reason`, and the `source` (runway/pexels)
    so the dashboards can distinguish AI-render failures from stock-fallback problems. A missing
    ffprobe still emits the event with `probe_ok=True` when the head signature passes, so the
    metric tracks real parseability gaps rather than missing tooling.
    """
    _emit(EVENTS["video_asset_probe"], {"post_id": post_id, "user_id": user_id,
                                        "probe_ok": probe_ok, "reason": reason,
                                        "source": source}, extra)


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

    _emit(EVENTS["media_cost"], {"kind": kind, "provider": provider, "model": model,
                                 "cost_usd": usd, "qty": qty, "user_id": user_id,
                                 "post_id": post_id, "feature": feature}, meta)
    # cost_ledger.model_tier is VARCHAR(64); an over-long identifier used to abort the whole
    # ledger write, so the spend vanished rather than being recorded under a truncated name.
    _write_cost_ledger(feature=feature, category="media", usd=usd, user_id=user_id,
                       provider=provider, model_tier=(model or None) and model[:64],
                       qty=qty, post_id=post_id,
                       task_name=_current_task_context()[0])


def track_avatar_likeness_probe(
    user_id: Optional[int],
    post_id: Optional[int],
    verdict: Optional[dict],
    used_avatar: Optional[str] = None,
    **extra,
) -> None:
    """Emit one avatar-likeness probe reading (issue #1279).

    The probe checks whether a generated source frame still depicts the user's declared likeness.
    Telemetry-only by default; the separate ``AVATAR_LIKENESS_VIDEO_HOLD_ENABLED`` flag controls whether
    a failed probe may hold the video. Every probe is emitted, including unchecked ones, so false
    positive/negative rates can be measured before any hold is enabled.

    Args:
        user_id: The account the frame was rendered for.
        post_id: The post the frame belongs to.
        verdict: The ``probe_avatar_likeness`` reading (``present`` / ``checked`` / ``reason``).
        used_avatar: ``"true"`` / ``"false"`` / ``"unknown"`` — whether the PROBED frame is a real
            LoRA render (issue #1430), read from the renderer and only falling back to the sticky
            per-post ``posts.avatar_media`` flag. A checked-negative on a ``"false"`` frame
            is the base-Flux fallback doing its job, not a likeness defect, so the two must never
            share one rate.
        **extra: Extra properties for this one event, applied LAST — so a call site can add a
            property or override a declared one. Nothing a dashboard or alert FILTERS on may
            arrive this way: `_emit` applies it after the coercions, so the string contract
            cannot see it.
    """
    _emit(EVENTS["avatar_likeness_probe"],
          {**dict(verdict or {}), "user_id": user_id, "post_id": post_id,
           "used_avatar": used_avatar}, extra)


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
    post_type: Optional[str] = None,
    **extra,
) -> None:
    """Emit a LinkedIn post-outcome event so content performance (impressions / engagement rate) is
    queryable in PostHog alongside LLM cost. `engagement` / `engagement_rate` are derived with the
    same weighting `post_stats` uses; `engagement_rate` is None when impressions are unknown.

    `post_type` is the FORMAT the post shipped as (text / carousel / video / document, issue #1513).
    Without it `saves` — the metric the save-worthy strategy is built on — could not be read per
    format, so "do carousels out-reach text posts?" was unanswerable from telemetry. It stays None
    when the caller could not read the format, which keeps those rows out of a format breakdown
    instead of piling them onto one.

    This is the success metric of the cost-routing experiment (issue #652), so the user's arm rides
    along as `$feature/cost-routing-arm` when PostHog enrolled them — `variant_key` (the #396 media
    combo this post shipped, when the caller knows it) becomes the media experiment's arm the same
    way.
    """
    from cqc_lem.utilities.post_stats import engagement_rate, engagement_score
    shipped = extra.pop("variant_key", None)
    # The `$feature/*` keys can never collide with a declared property, so riding in `extra` puts
    # them on the event exactly as spreading them first did.
    _emit(EVENTS["post_outcome"], {
        "post_id": post_id, "post_type": post_type, "variant_key": shipped, "reactions": reactions,
        "comments": comments, "reposts": reposts, "saves": saves, "impressions": impressions,
        "user_id": user_id,
        "engagement": engagement_score(reactions, comments, reposts),
        "engagement_rate": engagement_rate(reactions, comments, reposts, impressions),
    }, {**experiment_props(user_id, keys=(COST_ROUTING_ARM,),
                           shipped={POST_MEDIA_VARIANT: shipped} if shipped else None), **extra})


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
    _emit(EVENTS["audience_snapshot"], {"user_id": user_id, "follower_count": follower_count,
                                        "connection_count": connection_count,
                                        "profile_views": profile_views,
                                        "search_appearances": search_appearances}, extra)


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
    _emit(EVENTS["golden_hour_report"], {**dict(report or {}), "user_id": user_id}, extra)


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
    _emit(EVENTS["comment_outcome"], {**dict(outcome or {}), "user_id": user_id, "log_id": log_id},
          {**experiment_props(user_id, keys=(COMMENT_CONTRACT_PROMPT,)), **extra})


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
    _emit(EVENTS["sdui_selector_evidence"], {"surface": surface, "user_id": user_id,
                                             "candidate_count": len(candidates),
                                             "candidates": candidates}, extra)


def track_suppression_check(user_id: Optional[int], verdict: Optional[dict] = None,
                            paused: bool = False, **extra) -> None:
    """Emit one daily suppression-tripwire reading (issue #629). Every check is emitted, not just
    the trips: the whole point is to see the reach curve BEFORE the step-collapse, and a series that
    only has trips in it cannot show the run-up.
    """
    verdict = dict(verdict or {})
    signals = {s.get("name"): s for s in (verdict.get("signals") or []) if isinstance(s, dict)}
    _emit(EVENTS["suppression_check"], {**verdict, "user_id": user_id, "paused": paused,
                                        "reach": signals.get("reach_collapse") or {},
                                        "comments": signals.get("comment_demotion") or {}}, extra)


def track_comment_quality(user_id: Optional[int], report: Optional[dict] = None, **extra) -> None:
    """Emit the weekly per-user comment-quality scorecard (issue #628) — reply/like rates plus the
    demotion rate and the verdict that gates commenting — as one `comment_quality` event, so a hold
    is queryable next to the rates that caused it and a PostHog alert can page off it.
    """
    report = dict(report or {})
    _emit(EVENTS["comment_quality"], {**report, "user_id": user_id,
                                      "verdict": dict(report.get("verdict") or {})}, extra)


def track_content_quality(user_id: Optional[int], score: Optional[dict] = None, **extra) -> None:
    """Emit ONE nightly per-piece content-quality reading (issue #630) — slop score, self-similarity,
    the stored authenticity score, hook length vs the 140-char mobile budget, and engagement per
    impression once stats exist — as a `content_quality` event.

    Every unmeasured dimension stays None: an event that reported 0 for a post with no impressions
    yet would drag every ER average toward zero the night it shipped. Content BODIES are never sent
    (they are the user's own LinkedIn material, redacted everywhere else too) — only the names of the
    slop checks that fired, which is what makes a regression explainable.
    """
    _emit(EVENTS["content_quality"], {**dict(score or {}), "user_id": user_id}, extra)


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
    alerts = [a for a in (rollup.get("alerts") or []) if isinstance(a, dict)]
    # The period prefixes are the caller's own vocabulary, not a fixed schema, so they ride through
    # `extra` — which is also what keeps `by_surface`/`config` declared and them not.
    _emit(EVENTS["content_quality_rollup"], {
        **rollup, "user_id": user_id, "alert_count": len(alerts),
        "alerts": [a.get("name") for a in alerts],
        "alert_reasons": [a.get("reason") for a in alerts],
        "by_surface": current.get("by_surface") or {}, "config": rollup.get("config") or {},
    }, {**{f"current_{key}": value for key, value in current.items() if key != "by_surface"},
        **{f"prior_{key}": value for key, value in prior.items() if key != "by_surface"},
        **{f"delta_{key}": value for key, value in dict(rollup.get("deltas") or {}).items()},
        **extra})


def track_pre_post_engagement(post_id: int, user_id: Optional[int], status: str, **extra) -> None:
    """Emit the per-post pre-post engagement-window marker (issue #547) — dispatched, skipped (with
    the reason) or ran (with the comment count) — so a report can confirm the warm-up before a post
    actually fired instead of inferring it from task logs.
    """
    _emit(EVENTS["pre_post_engagement"],
          {"post_id": post_id, "user_id": user_id, "status": status}, extra)


def track_company_page_invite_run(user_id: Optional[int], report: Optional[dict] = None,
                                  **extra) -> None:
    """Emit one company-page invite run (issue #732) — EVERY run, including the ones that sent
    nothing. The lane used to be a once-a-month blast with no volume series at all; a series that
    only carried sends could not distinguish "paced down to zero today" from "silently broken", so
    the skip reason (budget_reached / credits_exhausted / paused / disabled) is the point.
    """
    _emit(EVENTS["company_page_invite_run"], {**dict(report or {}), "user_id": user_id}, extra)


def track_stale_invite_run(user_id: Optional[int], report: Optional[dict] = None,
                           **extra) -> None:
    """Emit one stale-invite withdrawal run (issue #969) — EVERY run, including the ones that
    withdraw nothing.

    This lane replaced a beat that had done nothing while LOOKING operational, so a series that
    only carried withdrawals would reproduce exactly the problem it was written to fix. `rows_seen`
    is the tell: zero rows day after day on an account with pending invites means the invitation
    manager's markup moved, not that the account is clean.
    """
    _emit(EVENTS["stale_invite_run"], {**dict(report or {}), "user_id": user_id}, extra)


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
    _emit(EVENTS["catchup_run"], {**dict(report or {}), "user_id": user_id}, extra)


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
    _emit(EVENTS["feed_scan"], {**dict(funnel or {}), "user_id": user_id}, extra)


def track_margin_report(report: dict) -> None:
    """Emit the weekly unit-economics scorecard (plan §E.1.4) as one `margin_report` event so the
    PostHog tiles read system margin, cohort margin and LTV:CAC without re-deriving them. Per-user
    financials stay out of the event body — internal-only by policy (plan §E.5) — but the cohort
    aggregates the Margin-by-Cohort dashboard needs ride along.
    """
    report = dict(report or {})
    system = dict(report.get("system") or {})
    _emit(EVENTS["margin_report"], report,
          {**{f"system_{key}": value for key, value in system.items()},
           **dict(report.get("unit_economics") or {})})


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
    _emit(EVENTS["image_gate_verdict"], {"surface": surface, "verdict": verdict, "issues": issues,
                                         "attempt_count": attempt_count, "checked": checked,
                                         "acceptable": acceptable, "user_id": user_id,
                                         "post_id": post_id})


def track_slop_retry(
    surface: str,
    outcome: str,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    attempt: int = 1,
    max_attempts: int = 2,
    kept: bool = False,
    user_id: Optional[int] = None,
) -> None:
    """Emit ONE `slop_retry` event per steered slop-lint regeneration (issue #1434).

    `outcome` is `slop_lint.retry_outcome`'s verdict on the pair of reports, and it is the whole
    reason the event exists: a finished draft only records the checks that were STILL firing when
    the budget ran out, so the clear-rate of the retry — and how often a full-draft rewrite trades
    one HARD check for a different one — cannot be recovered from the corpus afterwards. Grade that
    rate over the STEERED rows (`hard_before > 0`): a loop whose budget is shared with another
    grader can spend a regeneration on a draft that tripped no HARD check at all, and those land as
    `unsteered` rather than flattering `cleared`.

    `kept` is the second half of that reading and is NOT derivable from `outcome`: since a caller
    may now discard a regeneration that came back worse (`slop_lint.keep_retry`), a `persisted` row
    can be either the draft that shipped or one that was thrown away. Whether the call bought
    anything is exactly what an attempt-budget decision turns on, so it travels with the outcome.

    Only check NAMES cross the wire. A violation's `evidence` is an excerpt of the draft, i.e. user
    content, and the counts plus the names answer every question this measurement was raised to ask.
    """
    before, after = dict(before or {}), dict(after or {})
    _emit(EVENTS["slop_retry"], {
        "user_id": user_id, "surface": surface, "outcome": outcome, "attempt": attempt,
        "max_attempts": max_attempts, "kept": bool(kept),
        "before_checks": [v.get("check") for v in before.get("hard") or []],
        "after_checks": [v.get("check") for v in after.get("hard") or []],
        "hard_before": len(before.get("hard") or []), "hard_after": len(after.get("hard") or []),
        "warn_before": len(before.get("warnings") or []),
        "warn_after": len(after.get("warnings") or []),
    })


def track_motion_prompt_check(
    report: Optional[dict] = None,
    verdict: str = "unchecked",
    model: Optional[str] = None,
    attempt: int = 1,
    enforced: bool = False,
    user_id: Optional[int] = None,
    post_id: Optional[int] = None,
    surface: str = "post_video",
) -> None:
    """Emit ONE `motion_prompt_check` event per graded motion prompt (issue #1277).

    What the deterministic linter found before a Runway credit was spent, and what was done about it.

    `enforced` next to `verdict` is the whole point of the event: while the `video-motion-lint-hold`
    flag is off, this is a measurement of how often the lint WOULD have held a render, which is what
    the decision to promote it is made on. The prompt body is never sent — only the names of the
    checks that fired and the banned phrases they matched, both of which come from LEM's own fixed
    lists rather than from user content.
    """
    report = dict(report or {})
    _emit(EVENTS["motion_prompt_check"], {
        **report, "user_id": user_id, "post_id": post_id, "surface": surface,
        "model": model or report.get("model"), "verdict": verdict, "enforced": enforced,
        "attempt": attempt, "hard_count": len(report.get("hard") or []),
        "warn_count": len(report.get("warnings") or []),
        "evidence": [e for v in report.get("violations") or []
                     for e in (v.get("evidence") or [])][:10],
    })


def track_routing_policy(report: dict) -> None:
    """Emit the weekly cost-aware routing decision (plan §D.1(1)) as one `routing_policy` event, so
    a down-route — and especially an auto-rollback — is queryable next to the cost it was meant to
    save and the engagement it was gated on. The full comparison stats stay out of the event body;
    the per-bucket verdict and cohort are what a dashboard needs.
    """
    report = dict(report or {})
    changes = list(report.get("changes") or [])
    buckets = (dict(report.get("policy") or {}).get("buckets") or {}).items()
    _emit(EVENTS["routing_policy"], {
        **report, "change_count": len(changes),
        "rollback_count": sum(1 for c in changes if c.get("action") == "rollback"),
        "changes": [{"bucket": c.get("bucket"), "action": c.get("action"),
                     "reason": c.get("reason")} for c in changes],
        "buckets": [{"bucket": key, "state": b.get("state"), "to_tier": b.get("to_tier"),
                     "cohort_pct": b.get("cohort_pct"),
                     "assignment": b.get("assignment")} for key, b in buckets],
        # Whether the PostHog experiment (issue #652) actually cohorted this run, or the hash
        # fallback did — a treatment share of None means nobody was enrolled.
    }, {f"cohort_{key}": value for key, value in (report.get("cohort") or {}).items()})


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
    _emit(EVENTS["$feature_flag_called"],
          {"experiment": experiment, "variant": variant, "user_id": user_id},
          {f"$feature/{experiment}": variant, **extra})


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
    _emit(EVENTS["cost_alert"], {"date": day, "user_id": alert.get("user_id")}, alert)


def track_capacity_alert(alert: dict, generated_at: Optional[str] = None) -> None:
    """Emit one Selenium/lane capacity breach (issue #552) as a `capacity_alert` event, so the
    saturation history is queryable next to the task latency it explains and a PostHog alert can page
    off it. Always system-scoped: a full browser pool is an infra limit, not one user's problem.
    """
    _emit(EVENTS["capacity_alert"], {"generated_at": generated_at}, dict(alert or {}))


def track_youtube_token_check(state: Optional[dict] = None) -> None:
    """Emit one YouTube OAuth refresh-token probe (issue #742) as a `youtube_token_check` event.

    The dated OK line in the logs is the audit trail; this is the queryable series behind it, so
    "when did publishing actually go bad?" is answerable without grepping a year of logs. Always
    system-scoped: one OAuth grant publishes the whole channel, not one user's.
    """
    _emit(EVENTS["youtube_token_check"], dict(state or {}))


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
        _emit(EVENTS["rate_limit_trip"], {"cooldown_seconds": int(seconds),
                                          "consecutive_trips": int(trips),
                                          "reason": reason or "429"})
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

    This is an app-runtime read, so it uses the RUNTIME key (`posthog_keys.runtime_api_key`), not
    the cron's query key.
    """
    api_key = runtime_api_key()
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
    _emit(EVENTS["onboarding_step"],
          {"user_id": user_id, "step": step, "hours_since_start": hours_since_start}, extra)


def track_onboarding_nudge(user_id: int, nudge_key: str, **extra) -> None:
    """Emit the stalled-user nudge we sent, so nudge → step-completion is measurable."""
    _emit(EVENTS["onboarding_nudge"], {"user_id": user_id, "nudge_key": nudge_key}, extra)


def track_survey_prompt(user_id: int, survey_key: str, **extra) -> None:
    """Emit the survey we asked for (issue #501), so ask → response rate is measurable."""
    _emit(EVENTS["survey_prompt"], {"user_id": user_id, "survey_key": survey_key}, extra)


def track_shipped_notice(user_id: int, issue_number: int, **extra) -> None:
    """Emit the "you asked, we shipped" notice we sent (issue #502), so notice → micro-CSAT response
    is measurable against the GA satisfaction gate.
    """
    _emit(EVENTS["shipped_notice"], {"user_id": user_id, "issue_number": issue_number}, extra)


def track_survey_response(user_id: int, source: str, **extra) -> None:
    """Emit an NPS/review answer (issue #501) with its score/rating, so NPS and CSAT can be trended
    in PostHog next to the activation funnel.
    """
    _emit(EVENTS["survey_response"], {"user_id": user_id, "source": source}, extra)


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
        _emit(
            EVENTS["funnel_event"],
            {"user_id": user_id},
            {**attribution_props,
             "$set_once": {f"initial_{key}": value for key, value in attribution_props.items()},
             **extra},
            event=event,
            distinct_id=resolved_id,
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
        _emit(EVENTS["affiliate_event"], {"user_id": user_id}, extra, event=event)
    except Exception as e:
        log_warning(f"Could not track affiliate event '{event}'", exc=e, user_id=user_id)


def track_task(
    task_name: str,
    duration_ms: int,
    success: bool = True,
    user_id: Optional[int] = None,
    state: Optional[str] = None,
    **extra,
) -> None:
    """Emit `celery_task` — one row per task run, however it ended.

    Its only caller is `my_celery.on_task_postrun`, which is what makes this event a complete census
    of the queue rather than a sample: capturing it from inside a task as well would double-count
    that task. A task with no user (the scheduler beats) lands on the shared `"system"` person
    instead of being dropped, since an unattributed run still has to show up in the count.

    `state` is a NAMED argument rather than one of `**extra` because the Celery-failure alert is the
    one provisioned tile that filters on a property value (`state = "FAILURE"`, exact). Only a
    declared field is coerced to a string; riding in `**extra` it would reach PostHog in whatever
    shape a caller sent, and a non-string row silences that page (docs/kpi-dashboards.md).
    """
    _emit(EVENTS["celery_task"], {"task_name": task_name, "duration_ms": duration_ms,
                                  "success": success, "state": state, "user_id": user_id}, extra)


def track_inbound_email(verdict: str, user_id: Optional[int] = None) -> None:
    """One event per SendGrid Inbound Parse POST, carrying the dispatch verdict (comment_accepted /
    debounced / unknown_reply_token / …). The webhook drops most mail BY DESIGN, so without this a
    broken forwarding chain is indistinguishable from no mail arriving at all — weeks of 100%-ignored
    traffic left no signal anywhere. `verdict` is a STRING prop so alert tiles can filter on it
    (docs/kpi-dashboards.md).
    """
    _emit(EVENTS["inbound_parse_email"], {"verdict": verdict, "user_id": user_id})


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

    Never raises. The middleware calls this from a `finally:`, so an exception escaping here would
    REPLACE the request's own in-flight exception — telemetry must never be able to eat the failure
    it was capturing.
    """
    try:
        _emit(EVENTS["api_call"], {"route": route, "method": method, "status_code": status_code,
                                   "latency_ms": latency_ms, "user_id": user_id})
    except Exception as e:
        from cqc_lem.utilities.logger import log_warning
        log_warning("Could not track api call", exc=e, user_id=user_id,
                    http_status=status_code, route=route)


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
