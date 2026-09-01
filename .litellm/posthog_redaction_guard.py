"""Keep the model's own reasoning out of `$ai_generation` while redaction is on (issue #1831).

`config.yaml` sets `litellm_settings.turn_off_message_logging: true`, and the posture that key is
supposed to buy — the one `docs/llm-analytics.md` describes and the one issue #1832 settled on when
it left `LLM_PROMPT_LOGGING_FEATURES` empty — is "prompts and completions do not leave the stack".
That was not true. A hand audit read verbatim first-person claims off a live `$ai_generation` while
the config said `true`.

**The path.** Read off LiteLLM `main` at commit `10631eb834c7802aa61611e807474170b8a4d425`
(2026-08-30), which is what the pinned-to-floating `ghcr.io/berriai/litellm:main-latest` was
serving:

    ModelResponse.model_dump()
      → StandardLoggingPayloadSetup.get_final_response_obj()   litellm_core_utils/litellm_logging.py
      → redact_message_input_output_from_logging()             litellm_core_utils/redact_messages.py
      → perform_redaction() → _redact_model_response_dict_choices()
      → standard_logging_object["response"]
      → PostHogLogger._create_posthog_properties()             integrations/posthog.py
      → properties["$ai_output_choices"]

`_redact_model_response_dict_choices` replaces `content`, `reasoning_content`, `thinking_blocks`,
`audio`, `tool_calls[].function.arguments` and `function_call.arguments`. It never names
`provider_specific_fields` — a declared field on `Message`, `Delta` and `Choices`
(`litellm/types/utils.py`) and the bag several providers hand their verbatim reasoning back in. So

    $ai_output_choices[*].message.provider_specific_fields

rode out to PostHog untouched. Same class of escape as `previous_models`
(`.litellm/posthog_payload_guard.py`, #1310): a field LiteLLM attaches outside the surface its own
redaction walks. This guard is a monkeypatch for the same reason that one is — the redaction walk is
a module-level private function with no hook.

**Where it hooks.** `PostHogLogger.create_posthog_event_payload` is the ONE choke point: both
`log_success_event` (sync) and `_log_async_event` (async success AND failure) call it, and it is the
last place the event is a plain dict before it is queued for `/batch/`.

**What it is gated on.** `should_redact_message_logging(kwargs)` — LiteLLM's OWN condition, not a
copy of it. A future deliberate allowlist therefore still works: a request carrying
`LiteLLM-Disable-Message-Redaction` (which `utilities/ai/client.py` sets only for the features named
in `LLM_PROMPT_LOGGING_FEATURES`) is graded at full fidelity, reasoning included, exactly as the
owner decision in #1832 intends if that list is ever non-empty.

**Two different failure directions, on purpose.**

* `install()` fails **OPEN**. If LiteLLM's internals move, the patch declines and logs; analytics
  degrade to today's behaviour rather than the proxy failing to boot. A guard that raises on an
  image pull is worse than the leak, and `main-latest` is a floating tag.
* Every decision *inside* the installed patch fails **CLOSED**. An unreadable redaction state
  scrubs, and a scrub that somehow throws drops the two content-bearing properties outright. That
  is the `docs/llm-analytics.md` contract: a data-egress control does not fail open.

Which means the guard has exactly one silent mode — declining to install — so it says so at INFO
when it takes. After a deploy, confirm with:

    docker compose logs litellm | grep "posthog redaction guard"

**Stdlib only.** `.litellm/` is bind-mounted into the LiteLLM container, which has no `cqc_lem`
package, so this file may not import one — the same constraint `utilities/routing_policy.py` carries
for the same reason.
"""
import logging
from typing import Any

log = logging.getLogger("lem.posthog_redaction_guard")

#: Every property key whose value is model-authored reasoning rather than analytics.
#:
#: `provider_specific_fields` is the measured leak (#1831). The rest are belt-and-braces: LiteLLM
#: redacts them on the response shapes it recognises, but `_redact_standard_logging_object` only
#: walks a dict with `choices` or `output`, so any other shape carries them through. None of them is
#: ever read by a LEM insight — cost, tokens, latency, model and feature all arrive as their own
#: properties — so scrubbing them costs nothing measurable and closes the shape-drift hole too.
REASONING_KEYS = frozenset(
    {
        "provider_specific_fields",
        "reasoning",
        "reasoning_content",
        "reasoning_items",
        "thinking",
        "thinking_blocks",
    }
)

#: The only two properties that can carry model content at all — the last-resort drop set.
CONTENT_PROPERTIES = ("$ai_input", "$ai_output_choices")

#: Past this nesting level a value is replaced rather than walked. A response is ~6 levels deep, so
#: this only ever fires on a cycle or a pathological payload — and it has to fire, because an
#: unbounded walk would raise `RecursionError`, and the exception path is the one that leaks.
MAX_SCRUB_DEPTH = 20

_warned: set[str] = set()


def _warn_once(reason: str, exc: BaseException) -> None:
    """Log `reason` at WARNING the first time only.

    This sits in the logging path of every LLM call, so a persistent fault (LiteLLM's internals
    moved) would otherwise emit one warning per completion and bury the signal in its own volume.
    """
    if reason in _warned:
        return
    _warned.add(reason)
    log.warning("posthog redaction guard: %s (%s) — scrubbing", reason, exc)


def _marker(key: str) -> str:
    """Return the placeholder left in place of a scrubbed value.

    A marker rather than a deletion, so the field's absence from `$ai_generation` is provable in
    PostHog instead of looking like a provider that simply did not return reasoning.
    """
    return f"<redacted by lem: {key}>"


def _scrub(value: Any, depth: int) -> Any:
    """Return a copy of `value` with every `REASONING_KEYS` entry replaced by a marker.

    Copy-on-write, never in place: `properties["$ai_output_choices"]` IS
    `standard_logging_object["response"]`, an object other callbacks are handed as well, and a
    logging guard must not decide what a sibling callback sees.

    Tuples come back as lists. The event is JSON-serialised on its way to `/batch/`, so the two are
    the same value by the time anything reads it.
    """
    if depth > MAX_SCRUB_DEPTH:
        return _marker("nesting")
    if isinstance(value, dict):
        return {
            key: (_marker(key) if key in REASONING_KEYS else _scrub(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1) for item in value]
    return value


def scrub_event_properties(properties: Any, redaction_in_force: bool) -> Any:
    """Return `properties` with model reasoning removed, or unchanged when redaction is off.

    Args:
        properties: The `$ai_generation` / `$ai_embedding` property dict LiteLLM built.
        redaction_in_force: Whether LiteLLM is redacting this call. Passed in rather than resolved
            here so the scrub itself stays a pure function of its inputs.

    Returns:
        A scrubbed copy when `redaction_in_force`, otherwise the object it was given. Un-redacted is
        a deliberate, per-request allowlist decision (#1832) and this must not second-guess it.
    """
    if not redaction_in_force or not isinstance(properties, dict):
        return properties
    return _scrub(properties, 0)


def redaction_in_force(model_call_details: dict) -> bool:
    """Whether LiteLLM is redacting this call, asked of LiteLLM.

    Delegates to `should_redact_message_logging` so the header-, dynamic-param- and global-setting
    precedence stays in one place — a reimplementation here would drift on the next upgrade and
    silently disagree with what actually got redacted.

    Fails CLOSED: an unreadable answer returns True, so the reasoning is scrubbed. That costs a
    currently-empty allowlist nothing and is the only safe direction for an egress control.
    """
    try:
        from litellm.litellm_core_utils.redact_messages import should_redact_message_logging
    except Exception as exc:
        _warn_once("cannot import LiteLLM's redaction check", exc)
        return True
    try:
        return bool(should_redact_message_logging(model_call_details))
    except Exception as exc:
        _warn_once("LiteLLM's redaction check raised", exc)
        return True


def _drop_content_properties(payload: Any) -> None:
    """Remove every property that can carry model content, in place.

    The last resort when the scrub itself failed. `properties` is built fresh per event by
    `_create_posthog_properties`, so popping from it cannot reach another callback.
    """
    try:
        properties = payload.get("properties") if isinstance(payload, dict) else None
        if isinstance(properties, dict):
            for name in CONTENT_PROPERTIES:
                properties.pop(name, None)
    except Exception:  # nothing left to fall back to; the event still carries cost and latency
        pass


def install() -> bool:
    """Wrap the PostHog logger's event builder. Returns whether the patch took."""
    try:
        from litellm.integrations.posthog import PostHogLogger
    except Exception as exc:
        log.warning("posthog redaction guard not installed (import failed): %s", exc)
        return False

    original = getattr(PostHogLogger, "create_posthog_event_payload", None)
    if original is None:
        log.warning(
            "posthog redaction guard not installed: "
            "PostHogLogger.create_posthog_event_payload is gone"
        )
        return False
    if getattr(original, "_lem_reasoning_guarded", False):
        return True

    def _guarded(self, kwargs):
        payload = original(self, kwargs)
        try:
            if isinstance(payload, dict):
                payload["properties"] = scrub_event_properties(
                    payload.get("properties"), redaction_in_force(kwargs)
                )
            return payload
        except Exception as exc:
            log.warning("posthog redaction guard fell back to dropping content: %s", exc)
            _drop_content_properties(payload)
            return payload

    _guarded._lem_reasoning_guarded = True
    PostHogLogger.create_posthog_event_payload = _guarded
    log.info("posthog redaction guard installed (keys=%s)", sorted(REASONING_KEYS))
    return True


install()
