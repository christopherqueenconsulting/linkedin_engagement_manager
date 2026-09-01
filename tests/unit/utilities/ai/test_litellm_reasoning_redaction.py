"""Guards for the reasoning scrub in `.litellm/posthog_redaction_guard.py` (issue #1831).

`turn_off_message_logging: true` redacts `content` and `reasoning_content`, but LiteLLM's redaction
walk never names `provider_specific_fields` — where several providers hand back the model's verbatim
reasoning — so it reached `$ai_generation` with the config claiming otherwise.

Nothing here can run the proxy, so these assertions are on the two halves this suite CAN hold: the
scrub is a pure function and is tested as one, and the wiring that loads it is read out of
`config.yaml` as text, the same way `test_litellm_posthog_config.py` reads the settings it pins.
Whether the container actually installed the patch is a container-log check, named in the module
docstring — a guard that fails open has to be observable somewhere, and it cannot be here.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
GUARD_PATH = REPO_ROOT / ".litellm" / "posthog_redaction_guard.py"
CONFIG = (REPO_ROOT / ".litellm" / "config.yaml").read_text()


def _load_guard():
    """Import the guard from its path.

    `.litellm/` is a bind-mount for a container, not a package on any import path, so there is no
    importable name for it. Importing runs `install()`, which finds no LiteLLM in this venv and
    declines — which is itself one of the behaviours asserted below.
    """
    spec = importlib.util.spec_from_file_location("lem_posthog_redaction_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _event_properties():
    """A `$ai_generation` property dict in the shape PostHogLogger builds it.

    `$ai_output_choices` is `standard_logging_object["response"]` — i.e. the ModelResponse dict AFTER
    LiteLLM's own redaction has run, which is why `content` and `reasoning_content` already read
    `[redacted by litellm]` while `provider_specific_fields` still carries the real text.
    """
    return {
        "$ai_model": "gpt-5-mini",
        "$ai_provider": "openai",
        "$ai_input": [{"role": "user", "content": "[redacted by litellm]"}],
        "$ai_output_choices": [
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "[redacted by litellm]",
                    "reasoning_content": "[redacted by litellm]",
                    "provider_specific_fields": {
                        "reasoning": "We logged 1,200 errors per week until we added observability.",
                        "native_finish_reason": "stop",
                    },
                },
            }
        ],
        "$ai_input_tokens": 812,
        "$ai_output_tokens": 240,
        "$ai_total_cost_usd": 0.0031,
        "$ai_latency": 1.44,
        "$ai_trace_id": "trace-1",
        "feature": "comment_generation",
    }


def _flatten(value):
    """Every scalar reachable in `value`, so a leak anywhere in the tree is one assertion."""
    if isinstance(value, dict):
        return [item for child in value.values() for item in _flatten(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _flatten(child)]
    return [value]


LEAKED = "We logged 1,200 errors per week until we added observability."


class TestScrubWhenRedactionIsOn:
    def test_provider_specific_fields_does_not_survive(self):
        """The measured leak: verbatim reasoning read off a live event while the config said true."""
        scrubbed = guard.scrub_event_properties(_event_properties(), redaction_in_force=True)

        message = scrubbed["$ai_output_choices"][0]["message"]
        assert message["provider_specific_fields"] == "<redacted by lem: provider_specific_fields>"
        assert LEAKED not in _flatten(scrubbed)

    def test_the_analytics_the_event_exists_for_are_untouched(self):
        """Scrubbing content must not cost the cost/latency/model numbers — those ARE the event."""
        scrubbed = guard.scrub_event_properties(_event_properties(), redaction_in_force=True)

        assert scrubbed["$ai_model"] == "gpt-5-mini"
        assert scrubbed["$ai_total_cost_usd"] == 0.0031
        assert scrubbed["$ai_latency"] == 1.44
        assert scrubbed["$ai_output_tokens"] == 240
        assert scrubbed["feature"] == "comment_generation"
        assert scrubbed["$ai_trace_id"] == "trace-1"

    def test_the_caller_s_properties_are_not_mutated(self):
        """Scrubbing is copy-on-write.

        `$ai_output_choices` IS `standard_logging_object["response"]`, which other callbacks are
        handed as well, so scrubbing in place would decide what a sibling callback sees.
        """
        properties = _event_properties()

        guard.scrub_event_properties(properties, redaction_in_force=True)

        original = properties["$ai_output_choices"][0]["message"]["provider_specific_fields"]
        assert original["reasoning"] == LEAKED

    @pytest.mark.parametrize(
        "key", ["reasoning", "reasoning_content", "reasoning_items", "thinking", "thinking_blocks"]
    )
    def test_the_other_reasoning_carriers_go_too(self, key):
        """The other reasoning carriers go too, at any depth and in any shape.

        LiteLLM redacts these on the response shapes it recognises, but
        `_redact_standard_logging_object` only walks a dict with `choices` or `output`, so any other
        shape carries them straight through.
        """
        properties = {"$ai_output_choices": {"whatever": {key: LEAKED}}}

        scrubbed = guard.scrub_event_properties(properties, redaction_in_force=True)

        assert scrubbed["$ai_output_choices"]["whatever"][key] == f"<redacted by lem: {key}>"

    def test_a_cycle_is_truncated_rather_than_raising(self):
        """A cycle is truncated, not raised on.

        A `RecursionError` would land on the exception path, and the exception path is the one that
        leaks — so the depth cap is what keeps a pathological payload from becoming a disclosure.
        """
        cyclic = {"reasoning_holder": {}}
        cyclic["reasoning_holder"]["self"] = cyclic

        scrubbed = guard.scrub_event_properties({"$ai_output_choices": cyclic}, True)

        assert "<redacted by lem: nesting>" in _flatten(scrubbed)

    def test_a_non_dict_payload_is_returned_as_is(self):
        """Never raise in the logging path of an LLM call, whatever LiteLLM hands over."""
        assert guard.scrub_event_properties(None, redaction_in_force=True) is None


class TestNoScrubWhenRedactionIsOff:
    def test_an_allowlisted_call_keeps_its_reasoning(self):
        """An allowlisted call keeps its reasoning, and the object identity proves nothing was copied.

        Un-redaction is a per-request owner decision (#1832, the `LiteLLM-Disable-Message-Redaction`
        header). Grading a drafter on a scrubbed answer would be grading nothing, so the guard must
        not second-guess an explicit allowlist.
        """
        properties = _event_properties()

        result = guard.scrub_event_properties(properties, redaction_in_force=False)

        assert result is properties
        message = result["$ai_output_choices"][0]["message"]
        assert message["provider_specific_fields"]["reasoning"] == LEAKED


class TestTheGateFailsClosed:
    def test_an_unreadable_redaction_state_scrubs(self):
        """An unreadable redaction state scrubs.

        There is no LiteLLM in this venv, so the import inside `redaction_in_force` fails here
        exactly as it would if LiteLLM moved the symbol. A data-egress control answers that with
        `True` — `utilities/flags.py` fails open to its default and this deliberately does not.
        """
        assert guard.redaction_in_force({"litellm_params": {}}) is True

    def test_the_gate_is_litellm_s_own_call(self):
        """The gate is LiteLLM's own call, not a copy of it.

        Reimplementing the header/dynamic-param/global precedence here would drift on the next image
        pull and silently disagree with what LiteLLM actually redacted.
        """
        source = GUARD_PATH.read_text()
        assert "should_redact_message_logging" in source


class TestInstallFailsOpen:
    def test_install_declines_without_litellm_instead_of_raising(self):
        """install() declines rather than raising when the internals it patches are not there.

        `main-latest` is a floating tag, so those internals can move on an image pull with no commit
        here. Declining leaves analytics at today's behaviour; raising would stop the proxy booting,
        which is worse than the leak.
        """
        assert guard.install() is False


@pytest.fixture
def fake_litellm(monkeypatch):
    """Stand LiteLLM up far enough to install the patch and run one event through it.

    LiteLLM ships as a container image and is not a dependency of this venv, so the WRAPPER — the
    half of the guard that actually runs in production — is otherwise unreachable from any lane.
    These two modules are the exact surface `install()` and `redaction_in_force()` reach for, named
    the way LiteLLM names them, so a rename upstream shows up here as a failing install rather than
    as a test that quietly stopped covering anything.

    Returns the logger class (whose method `install()` will replace) and a mutable `redact` switch
    standing in for `should_redact_message_logging`.
    """
    state = {"redact": True}

    class PostHogLogger:
        def create_posthog_event_payload(self, kwargs):
            return {
                "event": "$ai_generation",
                "distinct_id": "1",
                "properties": _event_properties(),
            }

    posthog_module = types.ModuleType("litellm.integrations.posthog")
    posthog_module.PostHogLogger = PostHogLogger

    redact_module = types.ModuleType("litellm.litellm_core_utils.redact_messages")
    redact_module.should_redact_message_logging = lambda details: state["redact"]

    modules = {
        "litellm": types.ModuleType("litellm"),
        "litellm.integrations": types.ModuleType("litellm.integrations"),
        "litellm.integrations.posthog": posthog_module,
        "litellm.litellm_core_utils": types.ModuleType("litellm.litellm_core_utils"),
        "litellm.litellm_core_utils.redact_messages": redact_module,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    return types.SimpleNamespace(logger=PostHogLogger, state=state)


class TestTheInstalledWrapper:
    def test_an_emitted_event_carries_no_reasoning(self, fake_litellm):
        """The end-to-end claim: what `$ai_generation` leaves the proxy with."""
        assert guard.install() is True

        payload = fake_litellm.logger().create_posthog_event_payload({"litellm_params": {}})

        assert LEAKED not in _flatten(payload)
        assert payload["properties"]["$ai_total_cost_usd"] == 0.0031

    def test_an_allowlisted_call_is_emitted_whole(self, fake_litellm):
        """The gate is read per event, not once at install: flipping LiteLLM's answer flips this."""
        guard.install()
        fake_litellm.state["redact"] = False

        payload = fake_litellm.logger().create_posthog_event_payload({"litellm_params": {}})

        assert LEAKED in _flatten(payload)

    def test_installing_twice_does_not_double_wrap(self, fake_litellm):
        """Installing twice does not double-wrap.

        Both guard entries in `callbacks` patch the same class, so a second import must be a no-op
        rather than a second layer of scrub around the first.
        """
        assert guard.install() is True
        wrapped = fake_litellm.logger.create_posthog_event_payload

        assert guard.install() is True
        assert fake_litellm.logger.create_posthog_event_payload is wrapped

    def test_a_scrub_that_throws_drops_the_content_properties(self, fake_litellm, monkeypatch):
        """The fail-CLOSED half.

        There is no unscrubbed fallback: if the walk itself fails, the two properties that can carry
        content go, and the event still leaves with the cost and latency numbers it exists for.
        """
        guard.install()

        def _boom(*_args, **_kwargs):
            raise RuntimeError("scrub failed")

        monkeypatch.setattr(guard, "scrub_event_properties", _boom)

        payload = fake_litellm.logger().create_posthog_event_payload({"litellm_params": {}})

        assert "$ai_output_choices" not in payload["properties"]
        assert "$ai_input" not in payload["properties"]
        assert payload["properties"]["$ai_total_cost_usd"] == 0.0031

    def test_the_gate_delegates_to_litellm(self, fake_litellm):
        """`redaction_in_force` answers whatever LiteLLM answers, in both directions."""
        assert guard.redaction_in_force({"litellm_params": {}}) is True

        fake_litellm.state["redact"] = False
        assert guard.redaction_in_force({"litellm_params": {}}) is False

    def test_a_throwing_redaction_check_scrubs_and_warns_once(self, fake_litellm, caplog):
        """A hook that RAISES is the third failure mode, and it fails closed like the other two.

        Warned once, not once per completion: this sits in the logging path of every LLM call, so a
        persistent fault would otherwise bury its own signal under its own volume.
        """
        def _boom(_details):
            raise RuntimeError("internals moved")

        redact = sys.modules["litellm.litellm_core_utils.redact_messages"]
        redact.should_redact_message_logging = _boom
        guard._warned.clear()

        with caplog.at_level("WARNING"):
            assert guard.redaction_in_force({"litellm_params": {}}) is True
            assert guard.redaction_in_force({"litellm_params": {}}) is True

        assert len([r for r in caplog.records if "redaction guard" in r.getMessage()]) == 1

    def test_install_declines_when_the_hook_is_renamed(self, fake_litellm, monkeypatch):
        """A rename upstream declines rather than raising — `main-latest` is a floating tag."""
        monkeypatch.delattr(fake_litellm.logger, "create_posthog_event_payload")

        assert guard.install() is False


class TestTheGuardIsWiredIn:
    def test_the_config_loads_it_alongside_the_payload_guard(self):
        """A guard nothing loads is not a guard — and until #1880 nothing loaded either of them.

        Both sat under `custom_callbacks`, a key LiteLLM does not read, so this module was never
        imported and #1831's leak stayed open the whole time. The live key is `callbacks`, and it
        takes `module.attribute`, never a file path.

        This is the half of the wiring a unit test can see; the other half is the `__pycache__`
        check after a deploy, then the INFO line the module logs when the patch actually takes.
        """
        uncommented = "\n".join(
            line for line in CONFIG.splitlines() if not line.strip().startswith("#")
        )
        assert "posthog_redaction_guard.proxy_handler_instance" in uncommented
        assert "posthog_payload_guard.proxy_handler_instance" in uncommented
        assert "custom_callbacks:" not in uncommented
