"""Guards for the LiteLLM→PostHog analytics wiring (issue #647, docs/llm-analytics.md).

None of this is app code, so nothing fails at runtime when it drifts — the proxy just silently
stops emitting `$ai_generation`, or (worse) starts shipping the user's prompts. Only these
assertions notice. Read as text rather than parsed as YAML, matching test_selenium_capacity.py:
these files configure containers this suite can't run, so the assertion is on what ships. The one
exception is `turn_off_message_logging`, which is additionally asserted through a real YAML load —
the whole prompt-logging design rests on that key's value and nesting.
"""
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG = (REPO_ROOT / ".litellm" / "config.yaml").read_text()
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()


def _uncommented(text: str) -> str:
    # Every key below is matched as a substring, so prose in the comments must not satisfy it.
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _litellm_service() -> str:
    return _uncommented(re.split(r"\n  (?=\w)", COMPOSE.split("\n  litellm:\n")[1])[0])


CONFIG_SETTINGS = _uncommented(CONFIG)


class TestPostHogCallback:
    def test_success_and_failure_are_both_reported(self):
        """A success-only stream makes the error rate meaningless — a broken model looks like silence."""
        assert re.search(r'success_callback:\s*\[.*"posthog".*\]', CONFIG_SETTINGS)
        assert re.search(r'failure_callback:\s*\[.*"posthog".*\]', CONFIG_SETTINGS)

    def test_the_complexity_router_is_still_mounted(self):
        """success_callback and custom_callbacks are separate keys — adding one must not shadow the
        cost-aware routing hook (issue #494).
        """
        assert "custom_callbacks:" in CONFIG_SETTINGS
        assert "/app/.litellm/complexity_router.py" in CONFIG_SETTINGS

    def test_prompts_and_completions_are_redacted_by_default(self) -> None:
        """The global floor stays `true`: every call is redacted unless it opts out per request.

        Un-redacting is scoped one feature at a time by `utilities/ai/client.py` (the
        `LiteLLM-Disable-Message-Redaction` header), so this key must never become the lever — a
        global `false` ships profile synthesis, draft DMs and image prompts to PostHog to grade one
        drafter. Pinned as a literal rather than `os.environ/` for the same reason: the safe value
        should not be something a typo in `.env` can move.

        Asserted through a real YAML load as well as the text match this file usually uses, because
        this is the one key the whole design rests on and a previous revision of PR #1828 flipped it:
        the parse proves it is the actual `litellm_settings` key at the right nesting and a genuine
        boolean, not a substring that also matches the string "true" or a stray top-level copy.
        """
        assert re.search(r"turn_off_message_logging:\s*true", CONFIG_SETTINGS)
        settings = yaml.safe_load(CONFIG)["litellm_settings"]
        assert settings["turn_off_message_logging"] is True

    def test_the_proxy_container_gets_the_posthog_credentials(self):
        """The logger reads these two names specifically; POSTHOG_HOST is the app's own var, so both
        halves of the stack report to one project.
        """
        service = _litellm_service()
        assert "- POSTHOG_API_KEY=${POSTHOG_API_KEY}" in service
        assert "- POSTHOG_API_URL=${POSTHOG_HOST" in service
