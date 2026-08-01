import os
from collections.abc import Mapping
from typing import Any, Optional, Tuple

from openai import OpenAI

from cqc_lem.utilities.routing_policy import SYSTEM_USER_ID

# Only tier aliases are routable AND priced by the proxy, so nothing is attached to a call that
# named a raw provider model directly.
_TIER_PREFIX = "lem-"

# LiteLLM's own chain id (docs/proxy/request_headers). Whatever arrives here becomes the proxy's
# `litellm_trace_id`, which is what its PostHog logger publishes as `$ai_trace_id` — the ONE property
# PostHog groups an LLM trace by. It cannot be sent as request metadata: the logger sources
# `$ai_trace_id` from its own standard logging payload and would overwrite ours.
_TRACE_HEADER = "x-litellm-trace-id"

# ...whereas the PARENT span is metadata: the logger maps `metadata.parent_run_id` to `$ai_parent_id`
# and keeps the key out of the copied-through properties, so the generation nests under our span.
_PARENT_KEY = "parent_run_id"

# Mirrors observability.FEATURE_SYSTEM. Kept as a literal so this module never imports observability
# eagerly — that would drag the DB and PostHog into every `from ...ai.client import client`.
_SYSTEM_FEATURE = "system"


def current_attribution() -> Tuple[Optional[Any], Optional[str]]:
    """(user_id, feature) for the LLM call happening right now, or (None, None) if attribution is
    unavailable. Imported lazily: observability pulls in the DB and PostHog, and this module is the
    one every AI helper imports first."""
    try:
        from cqc_lem.utilities.observability import current_llm_attribution, FEATURE_SYSTEM
        user_id, feature = current_llm_attribution()
        return user_id, feature or FEATURE_SYSTEM
    except Exception:
        return None, None


def current_trace() -> Tuple[Optional[str], Optional[str]]:
    """(trace_id, span_id) of the multi-call pipeline running right now, or (None, None) when there
    is none. Lazily imported for the same reason `current_attribution` is."""
    try:
        from cqc_lem.utilities.observability import current_llm_trace
        return current_llm_trace()
    except Exception:
        return None, None


def attribution_metadata(user_id: Optional[Any], feature: Optional[str]) -> dict:
    """The `metadata` block a LiteLLM request carries. Two consumers, one shape:

    * the complexity router's cost-aware down-routing reads (feature, user_id) as the experiment
      bucket — the same two dimensions cost is attributed by (issue #494);
    * LiteLLM's PostHog logger uses `user_id` verbatim as the `$ai_generation` distinct_id and
      turns every other key into an event property (issue #647).

    `user_id` therefore falls back to the SYSTEM_USER_ID sentinel rather than being omitted: an
    absent one mints a throwaway anonymous person per call, while the sentinel is exactly what
    observability.py sends server-side, so proxy and app events land on ONE PostHog person.
    """
    return {
        "feature": feature or _SYSTEM_FEATURE,
        "user_id": user_id if user_id is not None else SYSTEM_USER_ID,
    }


def _attach_attribution(options) -> None:
    """Fill in this request's `metadata` from the ambient llm_attribution() scope.

    Runs for every endpoint the client exposes (chat, embeddings, images, speech) because they all
    build one JSON body. A caller that set its own metadata — `_call_llm` does, so an explicit
    `_track_user_id` can beat the ambient scope — always wins.
    """
    body = getattr(options, "json_data", None)
    if not isinstance(body, dict) or options.files:
        return
    if not str(body.get("model") or "").startswith(_TIER_PREFIX):
        return
    extra = getattr(options, "extra_json", None)
    if "metadata" in body or (isinstance(extra, dict) and "metadata" in extra):
        return
    merged = dict(extra) if isinstance(extra, dict) else {}
    merged["metadata"] = attribution_metadata(*current_attribution())
    options.extra_json = merged


def _request_metadata(options) -> Optional[dict]:
    """The metadata dict this request will actually send, whoever put it there — `_call_llm` sets its
    own in `extra_body`, `_attach_attribution` sets the ambient one in `extra_json`."""
    for source in (getattr(options, "json_data", None), getattr(options, "extra_json", None)):
        if isinstance(source, dict) and isinstance(source.get("metadata"), dict):
            return source["metadata"]
    return None


def _attach_trace(options) -> None:
    """Join this request to the pipeline trace open around it, if any (issue #746).

    Separate from `_attach_attribution` on purpose: that one bails out when the caller supplied its
    own metadata, which `_call_llm` always does — so folding tracing into it would silently exclude
    almost every generation LEM makes.
    """
    body = getattr(options, "json_data", None)
    if not isinstance(body, dict) or options.files:
        return
    if not str(body.get("model") or "").startswith(_TIER_PREFIX):
        return
    trace_id, span_id = current_trace()
    if not trace_id:
        return
    headers = getattr(options, "headers", None)
    sent = dict(headers) if isinstance(headers, Mapping) else {}
    # A caller that set the chain id itself owns it — headers are case-insensitive on the wire.
    if not any(str(key).lower() == _TRACE_HEADER for key in sent):
        sent[_TRACE_HEADER] = trace_id
        options.headers = sent
    metadata = _request_metadata(options)
    if span_id and metadata is not None:
        metadata.setdefault(_PARENT_KEY, span_id)


class AttributedOpenAI(OpenAI):
    """The OpenAI client with LEM's who/what — and which pipeline — stamped onto every proxied request.

    Attribution lives here rather than at the ~10 call sites because a call that skips it is
    invisible in cost routing and lands on an anonymous PostHog person — a silent failure nobody
    would notice. Trace ids are here for the same reason: a step that forgot them would drop out of
    its post's trace and nothing would say so. `_build_request` is the one place the SDK funnels
    every endpoint through; tests/unit/utilities/ai/test_client_attribution.py and
    test_client_tracing.py drive a real request build so an SDK upgrade that moves the hook fails CI
    instead of quietly dropping either.
    """

    def _build_request(self, options, **kwargs):
        # Independently guarded: attribution failing must not also cost the trace, or vice versa.
        for hook in (_attach_attribution, _attach_trace):
            try:
                hook(options)
            except Exception:
                pass  # observability is never a reason to lose the generation
        return super()._build_request(options, **kwargs)


client = AttributedOpenAI(
    api_key=os.getenv("LITELLM_MASTER_KEY", os.getenv("OPENAI_API_KEY")),
    base_url=os.getenv("LITELLM_BASE_URL", "http://litellm:4000"),
)
