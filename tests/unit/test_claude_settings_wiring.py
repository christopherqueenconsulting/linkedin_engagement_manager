"""Guards on the checked-in .claude/settings.json that graft's session-start refresh rewrites.

graft re-runs its wiring writes in any checkout whose graft/.cache wiring stamp is missing or
older than the installed graft, and that rewrite puts back permission rules and a hook this repo
leaves out on purpose. The rewrite lands as an uncommitted diff, so the failure mode is an agent
committing it; these tests make that a red build instead of a silent permission widening.
"""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SETTINGS = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"

# Rules the refresh writes that this repo does not pre-approve: a separate CLI, an npx fetch of
# an unscoped registry name, and graft's own source-tree dev commands (dist/ is gitignored here).
EXCLUDED_ALLOW_PREFIXES = ("Bash(trail", "Bash(npx ", "Bash(graft-dev", "Bash(node ")


def _settings() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def test_allow_list_has_no_excluded_graft_rules() -> None:
    allow = _settings().get("permissions", {}).get("allow", [])
    leaked = [rule for rule in allow if rule.startswith(EXCLUDED_ALLOW_PREFIXES)]
    assert leaked == [], f"graft refresh rules committed to permissions.allow: {leaked}"


def test_no_session_end_hook() -> None:
    assert "SessionEnd" not in _settings().get("hooks", {})


@pytest.mark.parametrize("rule", ["Bash(trail:*)", "Bash(npx graft:*)", "Bash(node dist/cli.js:*)"])
def test_prefixes_catch_the_rules_the_refresh_writes(rule: str) -> None:
    assert rule.startswith(EXCLUDED_ALLOW_PREFIXES)


# Claude Code reads a hook's `timeout` in SECONDS. graft's wiring writes millisecond-sized values
# (15000 = over four hours), so a hung `graft ask` under the UserPromptSubmit hook blocked a
# pipeline lane for 23 minutes and pushed lem-agent.slice past MemoryHigh, stalling the daemon's
# GitHub reads. Any hook that needs longer than this is a defect, not a setting.
MAX_HOOK_TIMEOUT_SECONDS = 60


def test_hook_timeouts_are_seconds_not_milliseconds() -> None:
    too_long = [
        (event, hook.get("command", "")[:60], hook["timeout"])
        for event, matchers in _settings().get("hooks", {}).items()
        for matcher in matchers
        for hook in matcher.get("hooks", [])
        if "timeout" in hook and hook["timeout"] > MAX_HOOK_TIMEOUT_SECONDS
    ]
    assert too_long == [], f"hook timeouts are seconds; these look like milliseconds: {too_long}"
