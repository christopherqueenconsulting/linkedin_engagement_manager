"""Guards for the LiteLLM→PostHog analytics wiring (issue #647, docs/llm-analytics.md).

None of this is app code, so nothing fails at runtime when it drifts — the proxy just silently
stops emitting `$ai_generation`, or (worse) starts shipping the user's prompts. Only these
assertions notice. Read as text rather than parsed as YAML, matching test_selenium_capacity.py:
these files configure containers this suite can't run, so the assertion is on what ships.
"""
import re
from pathlib import Path

import pytest

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
        cost-aware routing hook (issue #494)."""
        assert "custom_callbacks:" in CONFIG_SETTINGS
        assert "/app/.litellm/complexity_router.py" in CONFIG_SETTINGS

    def test_prompts_and_completions_are_redacted(self):
        """$ai_input/$ai_output_choices would otherwise carry the user's own LinkedIn material —
        the SPA masks exactly this content, and the proxy must not leak it out the back."""
        assert re.search(r"turn_off_message_logging:\s*true", CONFIG_SETTINGS)

    def test_the_proxy_container_gets_the_posthog_credentials(self):
        """The logger reads these two names specifically; POSTHOG_HOST is the app's own var, so both
        halves of the stack report to one project."""
        service = _litellm_service()
        assert "- POSTHOG_API_KEY=${POSTHOG_API_KEY}" in service
        assert "- POSTHOG_API_URL=${POSTHOG_HOST" in service
