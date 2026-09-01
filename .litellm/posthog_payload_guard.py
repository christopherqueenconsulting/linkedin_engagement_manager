"""Keep `$ai_generation` small enough for PostHog to accept the batch (issue #1310).

LiteLLM's PostHog logger sends events 100 at a time to `/batch/`. That endpoint rejects an
oversized request with **413**, and a 413 is a size refusal, not a rate limit — the batch is
dropped outright and never retried. So an unknown slice of `$ai_generation` simply never arrived,
while the dashboards kept rendering numbers that looked complete. `docs/llm-analytics.md` makes
`$ai_generation` the provider-priced source of truth for the money question, so every cost figure
for the affected period was a floor, not a total.

Measured on the live project before this guard (12h window, 8,105 events):

    $ai_generation   avg 5.0 KB   max 743 KB

and in the largest event, one property was 95% of it:

        724929  previous_models          <-- the whole retry/fallback history
          2434  user_api_key_auth
           177  requester_metadata

`previous_models` is LiteLLM's record of each earlier attempt, and it carries those attempts'
full request bodies. `turn_off_message_logging` does NOT reach it: that setting redacts `messages`
and `response` inside `standard_logging_object`, whereas this rides in request metadata and is
copied onto the event by `_add_custom_metadata_properties`, whose internal-field exclusion list
does not name it. The agent lane retries against >100k-token prompts, which is why the 413s track
pipeline volume without being caused by the pipeline.

Two guards, because fixing only the known offender leaves the same failure waiting behind the next
large field LiteLLM decides to attach:

1. `previous_models` is dropped outright. It is a debugging aid, and nothing in LEM's analytics
   reads it — cost, tokens, latency and model all arrive as their own properties.
2. Anything else over `MAX_PROPERTY_BYTES` is replaced by a short marker naming the property and
   its real size, so the next offender announces itself in PostHog instead of silently costing us
   a batch.

The guard is a monkeypatch rather than a config setting because LiteLLM exposes no hook here: the
exclusion list is a local inside the method. It is wrapped in a try/except and installs at import;
if the internals move, the patch declines and logs, and analytics degrade to exactly today's
behaviour rather than taking the proxy down.

**How it is loaded (issue #1880).** The work happens at IMPORT, so the only thing config.yaml has
to do is import this file — but LiteLLM's `litellm_settings.callbacks` will only import a module
as `module.attribute`, and refuses at startup anything that is not a `CustomLogger` instance or a
plain callable. `proxy_handler_instance` at the bottom is that handle. It does no per-request work.
Before #1880 this module was listed under `custom_callbacks`, a key LiteLLM never reads, so none of
the above had ever run.
"""
import json
import logging

log = logging.getLogger("lem.posthog_guard")

try:  # The proxy container always has this; the unit lane imports this file with no litellm.
    from litellm.integrations.custom_logger import CustomLogger as _CallbackBase
except Exception:  # pragma: no cover - exercised by the no-litellm import in the unit lane

    class _CallbackBase:  # type: ignore[no-redef]
        """Stand-in base when litellm is absent.

        Callable, so the instance below stays dispatchable either way: LiteLLM accepts a
        `CustomLogger` instance OR a plain callable, and refusing an entry is a startup failure.
        A guard that cannot install must not also be able to stop the proxy booting.
        """

        def __call__(self, *args, **kwargs):
            """No-op: this object exists to be imported, never to be dispatched."""
            return None

#: Above this, a single property is replaced by a marker. 32 KB is far larger than any legitimate
#: analytics value (the whole event averages 5 KB) and far below the point where a 100-event batch
#: can approach PostHog's request ceiling.
MAX_PROPERTY_BYTES = 32 * 1024

#: Never useful in analytics, unbounded in size. Dropped before anything measures it.
DROP_KEYS = frozenset({"previous_models"})


def _sizeof(value) -> int:
    """Serialized size of a property value, or 0 when it cannot be measured.

    A value that will not serialize cannot be the thing blowing the payload budget — PostHog is
    sent JSON — so an unmeasurable value is passed through rather than guessed at.
    """
    try:
        return len(json.dumps(value, default=str))
    except Exception:
        return 0


def prune_metadata(metadata: dict) -> dict:
    """Return `metadata` without the fields that make an event too big to send.

    Pure and total: it never raises, and a non-dict passes straight through, because this sits in
    the logging path of every LLM call and must not be able to fail one.
    """
    if not isinstance(metadata, dict):
        return metadata
    out = {}
    for key, value in metadata.items():
        if key in DROP_KEYS:
            continue
        size = _sizeof(value)
        if size > MAX_PROPERTY_BYTES:
            # Keep the KEY so the next oversized field is visible in PostHog rather than silently
            # eating a batch, which is the failure this whole module exists to end.
            out[key] = f"<dropped by lem: {size} bytes > {MAX_PROPERTY_BYTES}>"
            log.warning("posthog guard truncated oversized property %s (%s bytes)", key, size)
            continue
        out[key] = value
    return out


def install() -> bool:
    """Wrap the PostHog logger's metadata extraction. Returns whether the patch took."""
    try:
        from litellm.integrations.posthog import PostHogLogger
    except Exception as exc:
        log.warning("posthog guard not installed (import failed): %s", exc)
        return False

    original = getattr(PostHogLogger, "_extract_metadata", None)
    if original is None:
        log.warning("posthog guard not installed: PostHogLogger._extract_metadata is gone")
        return False
    if getattr(original, "_lem_guarded", False):
        return True

    def _guarded(self, kwargs):
        try:
            return prune_metadata(original(self, kwargs))
        except Exception as exc:  # analytics must never break a completion
            log.warning("posthog guard passed through after error: %s", exc)
            return original(self, kwargs)

    _guarded._lem_guarded = True
    PostHogLogger._extract_metadata = _guarded
    log.info("posthog payload guard installed (drop=%s, cap=%s bytes)",
             sorted(DROP_KEYS), MAX_PROPERTY_BYTES)
    return True


install()


class _PayloadGuardHandle(_CallbackBase):
    """The object `litellm_settings.callbacks` names, so that this module gets imported.

    Deliberately empty: the guard IS the monkeypatch `install()` ran above, and inheriting
    `CustomLogger` without overriding a hook costs nothing per request — LiteLLM skips the pre-call
    walk for callbacks that do not override it, and the base logging hooks are no-ops.
    """


proxy_handler_instance = _PayloadGuardHandle()
