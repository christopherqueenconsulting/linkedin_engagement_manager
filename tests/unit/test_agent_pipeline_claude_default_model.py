"""The Claude lane has an explicit default model instead of the host CLI's.

A Claude-lane run with no `agent:model:*` label used to pass no `--model`, so the `claude` CLI fell
back to the pipeline host's `settings.json` default (`opus[1m]`) and every unlabelled run billed
the most expensive model by accident. `dispatch_lane()` now resolves the hint itself:

- an `agent:model:*` hint always wins;
- otherwise `CLAUDE_DEFAULT_MODEL` (default `claude-sonnet-5-5`);
- `depmajor` keeps `CLAUDE_JUDGMENT_MODEL` (default `claude-opus-5-5`), because its verdict arms
  auto-merge;
- `CLAUDE_DEFAULT_MODEL=` (empty) restores the old CLI-default behaviour, the one-line rollback.

The Ollama lane is untouched, and full Claude model ids keep their `ai:claude-subscription:<slug>`
label so dashboards do not lose the model dimension.
"""

import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "agent-pipeline" / "lib"


def _dispatch(tmp_path: Path, hint: str, env: dict) -> dict:
    """Run dispatch_lane with stubbed capacity; return LANE / AGENT_MODEL / AGENT_TIER."""
    script = textwrap.dedent(
        f"""
        set -uo pipefail
        BASE="{tmp_path}"; SLUG="o/n"; LOGDIR="$BASE/logs"; mkdir -p "$LOGDIR"
        log() {{ :; }}; posthog_capture() {{ :; }}
        . "{LIB}/dispatch.sh" 2>/dev/null
        dispatch_lane "{hint}"
        printf '%s|%s|%s\\n' "$LANE" "${{AGENT_MODEL:-}}" "${{AGENT_TIER:-}}"
        """
    )
    base_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CLAUDE_AVAIL": "0",
        "OLLAMA_AVAIL": "1",
        "SLOT": "1",
        "ISSUE": "1",
    }
    base_env.update(env)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=base_env)
    lane, model, tier = out.stdout.strip().splitlines()[-1].split("|")
    return {"lane": lane, "model": model, "tier": tier, "raw": out.stdout + out.stderr}


@pytest.mark.parametrize(
    "hint,mode,env,expected",
    [
        ("", "start", {}, "claude-sonnet-5-5"),
        ("", "fix", {}, "claude-sonnet-5-5"),
        ("", "selfreview", {}, "claude-sonnet-5-5"),
        # A label hint always wins, whatever the default.
        ("opus", "start", {}, "opus"),
        ("haiku", "docfix", {}, "haiku"),
        # depmajor keeps the stronger model.
        ("", "depmajor", {}, "claude-opus-5-5"),
        # ...but an owner label still overrides it.
        ("sonnet", "depmajor", {}, "sonnet"),
        # Both models are configurable.
        ("", "start", {"CLAUDE_DEFAULT_MODEL": "claude-haiku-5-5"}, "claude-haiku-5-5"),
        ("", "depmajor", {"CLAUDE_JUDGMENT_MODEL": "claude-fable-5-2"}, "claude-fable-5-2"),
        # Rollback: empty default means "pass no --model" again.
        ("", "start", {"CLAUDE_DEFAULT_MODEL": ""}, ""),
    ],
)
def test_claude_lane_model(tmp_path, hint, mode, env, expected):
    got = _dispatch(tmp_path, hint, {"MODE": mode, **env})
    assert got["lane"] == "claude", got["raw"]
    assert got["model"] == expected, got["raw"]


def test_ollama_lane_ignores_the_claude_default(tmp_path):
    got = _dispatch(tmp_path, "", {"MODE": "start", "CLAUDE_AVAIL": "1", "OLLAMA_AVAIL": "0"})
    assert got["lane"] == "ollama", got["raw"]
    assert got["model"].startswith("lem-agent-tier"), got["raw"]


def test_measured_override_to_claude_uses_the_default(tmp_path):
    """v2's LEM_LANE_OVERRIDE=claude path reaches the same default."""
    got = _dispatch(tmp_path, "", {"MODE": "start", "LEM_LANE_OVERRIDE": "claude"})
    assert got["lane"] == "claude" and got["model"] == "claude-sonnet-5-5", got["raw"]


@pytest.mark.parametrize(
    "model,labels",
    [
        ("claude-sonnet-5-5", "ai:claude-subscription ai:claude-subscription:sonnet"),
        ("claude-opus-5-5", "ai:claude-subscription ai:claude-subscription:opus"),
        ("claude-haiku-5-5", "ai:claude-subscription ai:claude-subscription:haiku"),
        ("opus", "ai:claude-subscription ai:claude-subscription:opus"),
        ("", "ai:claude-subscription"),
    ],
)
def test_full_model_ids_keep_their_model_label(tmp_path, model, labels):
    script = textwrap.dedent(
        f"""
        BASE="{tmp_path}"; SLUG="o/n"
        . "{LIB}/labels.sh" 2>/dev/null
        lane_labels_for claude "{model}" primary
        """
    )
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert out.stdout.split() == labels.split(), out.stdout + out.stderr
