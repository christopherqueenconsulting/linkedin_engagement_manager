"""Regression tests for lane/model capture in tick-outcomes.ndjson (closes #1229).

`tick.sh` snapshots `TICK_LANE`, `TICK_MODEL` and `TICK_ROUTE_REASON` before it calls
`run_lane()`, because the actual routing decision (`dispatch_lane()`) happens inside
`run_lane.sh`. The fix backfills those variables from `LANE` / `AGENT_TIER` /
`AGENT_MODEL` / `ROUTE_REASON` once `dispatch_lane()` has run, so the EXIT trap's
`emit_tick_outcome()` writes real routing dimensions instead of empty strings.

These tests exercise `dispatch_lane()` + the backfill in isolation with stubbed
capacity state, and structurally assert that `run_lane.sh` always populates the
TICK_* variables before returning or launching the agent.
"""

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LANE_SH = REPO_ROOT / "scripts" / "agent-pipeline" / "lib" / "run_lane.sh"
DISPATCH_SH = REPO_ROOT / "scripts" / "agent-pipeline" / "lib" / "dispatch.sh"
TICK_SH = REPO_ROOT / "scripts" / "agent-pipeline" / "tick.sh"

RUN_LANE_SOURCE = RUN_LANE_SH.read_text(encoding="utf-8")


# --- helpers to lift the dispatch + backfill block and run it -----------------


def _dispatch_block() -> str:
    """Return dispatch.sh plus the backfill snippet lifted verbatim from run_lane.sh.

    The snippet is sliced out of the real script rather than restated here: a copy would keep
    passing after run_lane.sh changed, which is the drift this test exists to catch.
    """
    match = re.search(
        r'dispatch_lane "\$hint"\n(.*?)\n  if \[ "\$\{DRY_RUN:-0\}" = "1" \]',
        RUN_LANE_SOURCE,
        re.S,
    )
    assert match, "TICK_* backfill block not found between dispatch_lane() and the DRY_RUN branch"
    return (
        DISPATCH_SH.read_text(encoding="utf-8")
        + "\n_backfill_tick_routing() {\n"
        + match.group(1)
        + "\n}\n"
    )


def _run_dispatch(tmp_path: Path, body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run dispatch_lane() with stubbed capacity and capture the TICK_* exports."""
    script = (
        textwrap.dedent("""
            set -uo pipefail
            BASE="__BASE__"
            SLUG="o/n"
            REPO="o/n"
            LOGDIR="$BASE/logs"
            mkdir -p "$LOGDIR"
            log() { echo "LOG: $*"; }
            posthog_capture() { :; }
            record_lane_outcome() { :; }
            apply_lane_labels() { :; }
        """)
        .replace("__BASE__", str(tmp_path))
        + _dispatch_block()
        + textwrap.dedent(body)
    )
    run_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    run_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=run_env,
    )


# --- functional: routing decision backfills TICK_* correctly ------------------


@pytest.mark.parametrize(
    "claude_pct,ollama_pct,slot,expected_lane,expected_reason",
    [
        # Both lanes healthy: slot 1 = Claude primary, slot 2+ = Ollama parallel.
        (80, 80, 1, "claude", "primary"),
        (80, 80, 2, "ollama", "parallel"),
        # Claude constrained/exhausted, Ollama healthy -> Ollama fallback.
        (20, 80, 1, "ollama", "fallback"),
        # Claude healthy, Ollama constrained -> Claude only.
        (80, 20, 1, "claude", "primary"),
        # Both constrained: degraded, tie goes to Ollama.
        (30, 30, 1, "ollama", "degraded"),
        # Both constrained but Claude ahead.
        (40, 30, 1, "claude", "degraded"),
    ],
)
def test_backfill_matches_dispatch_lane(
    tmp_path, claude_pct, ollama_pct, slot, expected_lane, expected_reason
):
    """The TICK_* vars must reflect the lane dispatch chose, not the pre-dispatch empty values."""
    env = {
        "MODE": "start",
        "ISSUE": "1229",
        "SLOT": str(slot),
        "WORKER_ID": str(slot),
        "EXECUTION_ID": "test-1229",
        "CLAUDE_PCT": str(claude_pct),
        "OLLAMA_PCT": str(ollama_pct),
    }
    # Map percentages to availability: >50% means available (0).
    env["CLAUDE_AVAIL"] = "0" if claude_pct > 50 else "1"
    env["OLLAMA_AVAIL"] = "0" if ollama_pct > 50 else "1"
    env["DEGRADED"] = "1" if env["CLAUDE_AVAIL"] == "1" and env["OLLAMA_AVAIL"] == "1" else "0"

    out = _run_dispatch(
        tmp_path,
        """
        # Pre-dispatch values are empty, mirroring tick.sh before the fix.
        TICK_LANE=""; TICK_MODEL=""; TICK_ROUTE_REASON=""
        dispatch_lane ""
        _backfill_tick_routing
        printf '%s/%s/%s\n' "$TICK_LANE" "$TICK_MODEL" "$TICK_ROUTE_REASON"
        """,
        env,
    )
    lane, model, reason = out.stdout.strip().split("/")
    assert lane == expected_lane, out.stdout + out.stderr
    assert reason == expected_reason, out.stdout + out.stderr
    if expected_lane == "ollama":
        assert model.startswith("lem-agent-tier"), model
    else:
        # Claude lane with no agent:model:* hint runs the CLI default — recorded by name, so an
        # empty `model` in the file always means "no lane ran", never "the default model ran".
        assert model == "default", model


def test_backfill_carries_claude_model_hint(tmp_path):
    """When dispatch chooses the Claude lane with a model hint, TICK_MODEL mirrors it."""
    env = {
        "MODE": "start",
        "ISSUE": "1229",
        "SLOT": "1",
        "WORKER_ID": "1",
        "EXECUTION_ID": "test-1229",
        "CLAUDE_PCT": "80",
        "OLLAMA_PCT": "80",
        "CLAUDE_AVAIL": "0",
        "OLLAMA_AVAIL": "0",
        "DEGRADED": "0",
    }
    out = _run_dispatch(
        tmp_path,
        """
        TICK_LANE=""; TICK_MODEL=""; TICK_ROUTE_REASON=""
        dispatch_lane "sonnet"
        _backfill_tick_routing
        printf '%s/%s/%s\n' "$TICK_LANE" "$TICK_MODEL" "$TICK_ROUTE_REASON"
        """,
        env,
    )
    lane, model, reason = out.stdout.strip().split("/")
    assert lane == "claude"
    assert model == "sonnet"
    assert reason == "primary"


def test_backfill_carries_ollama_tier_label(tmp_path):
    """An issue with an agent:tier:* label should set TICK_MODEL to that tier on Ollama lane."""
    env = {
        "MODE": "start",
        "ISSUE": "1229",
        "SLOT": "2",
        "WORKER_ID": "2",
        "EXECUTION_ID": "test-1229",
        "CLAUDE_PCT": "80",
        "OLLAMA_PCT": "80",
        "CLAUDE_AVAIL": "0",
        "OLLAMA_AVAIL": "0",
        "DEGRADED": "0",
        "ISSUE_LABELS": "agent:tier:3 priority:medium",
    }
    out = _run_dispatch(
        tmp_path,
        """
        TICK_LANE=""; TICK_MODEL=""; TICK_ROUTE_REASON=""
        dispatch_lane ""
        _backfill_tick_routing
        printf '%s/%s\n' "$TICK_LANE" "$TICK_MODEL"
        """,
        env,
    )
    lane, model = out.stdout.strip().split("/")
    assert lane == "ollama"
    assert model == "lem-agent-tier3"


# --- structural: run_lane.sh always populates TICK_* after dispatch -----------


def test_run_lane_backfills_tick_routing_after_dispatch():
    """The fix must live in run_lane.sh, not just a helper, so every MODE gets it."""
    run_block = re.search(
        r"run_lane\(\) \{.*^\}", RUN_LANE_SOURCE, re.S | re.M
    ).group(0)
    # dispatch_lane() is called once in the function body (it may also appear in comments).
    assert len(re.findall(r"^\s*dispatch_lane", run_block, re.M)) == 1
    dispatch_at = run_block.index("dispatch_lane")

    # TICK_LANE/TICK_MODEL/TICK_ROUTE_REASON assignments appear after dispatch_lane().
    for var in ("TICK_LANE", "TICK_MODEL", "TICK_ROUTE_REASON"):
        assert var in run_block, f"{var} backfill missing"
        assert run_block.index(var) > dispatch_at, f"{var} must be assigned after dispatch_lane"

    # The values are exported.
    assert re.search(r"export TICK_LANE TICK_MODEL TICK_ROUTE_REASON", run_block)


def test_backfill_runs_before_agent_or_dry_run_return():
    """Backfill must happen for both DRY_RUN=1 and real runs, before run_lane returns."""
    run_block = re.search(
        r"run_lane\(\) \{.*^\}", RUN_LANE_SOURCE, re.S | re.M
    ).group(0)
    backfill_end = max(
        run_block.index("TICK_LANE"),
        run_block.index("TICK_MODEL"),
        run_block.index("TICK_ROUTE_REASON"),
    )
    dry_run_return = run_block.index('if [ "${DRY_RUN:-0}" = "1" ]; then')
    real_run_marker = run_block.index("_emit \"issue_assigned\"")
    assert backfill_end < dry_run_return < real_run_marker


def test_tick_sh_exports_tick_routing_vars():
    """tick.sh must still declare and export the variables the EXIT trap reads."""
    source = TICK_SH.read_text(encoding="utf-8")
    for var in ("TICK_LANE", "TICK_MODEL", "TICK_ROUTE_REASON", "TICK_AGENT_RC"):
        assert f"{var}=\"" in source or f"{var}=" in source
    export_line = re.search(
        r"export TICK_OUTCOME TICK_REASON.*TICK_ROUTE_REASON", source, re.M
    )
    assert export_line
    assert "TICK_AGENT_RC" in export_line.group(0) or re.search(
        r"export .*TICK_AGENT_RC", source
    )


# --- the agent's exit status is what makes the lane answerable ----------------


def test_tick_outcome_payload_carries_agent_rc():
    """The row must carry the agent's exit status, defaulting to -1 when no agent ran."""
    source = TICK_SH.read_text(encoding="utf-8")
    assert '"agent_rc":' in source, "tick-outcomes rows need an agent_rc field"
    assert re.search(r'"agent_rc":\s*int\(os\.environ\.get\("TICK_AGENT_RC","-1"\) or -1\)', source)
    # tick_outcome must NOT be flipped on a failed agent run: status.sh keys its stall detector on
    # "dispatched" and its per-PR failure counter on "failed".
    run_block = re.search(r"run_lane\(\) \{.*^\}", RUN_LANE_SOURCE, re.S | re.M).group(0)
    assert "TICK_OUTCOME" not in run_block


def _install_lane_fixture(tmp_path: Path, claude_rc: int) -> dict:
    """Build a self-contained $BASE (lib/ + a fake `claude`) so run_lane() can be run for real."""
    lib = tmp_path / "lib"
    lib.mkdir()
    for sh in (RUN_LANE_SH.parent).glob("*.sh"):
        (lib / sh.name).write_text(sh.read_text(encoding="utf-8"), encoding="utf-8")
    binf = tmp_path / "bin"
    binf.mkdir()
    claude = binf / "claude"
    claude.write_text(f'#!/bin/sh\necho "fake agent run"\nexit {claude_rc}\n', encoding="utf-8")
    claude.chmod(0o755)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    return {"base": str(tmp_path), "bin": str(binf), "wt": str(wt)}


@pytest.mark.parametrize("claude_rc", [0, 1, 124])
def test_run_lane_records_the_agent_exit_status(tmp_path, claude_rc):
    """TICK_AGENT_RC carries the real exit status — a timeout (124) is not a success."""
    fx = _install_lane_fixture(tmp_path, claude_rc)
    script = textwrap.dedent(f"""
        set -uo pipefail
        BASE="{fx['base']}"
        LOGDIR="$BASE/logs"; mkdir -p "$LOGDIR"
        LOG=/dev/null
        . "$BASE/lib/run_lane.sh"
        log() {{ :; }}
        posthog_capture() {{ :; }}
        record_lane_outcome() {{ :; }}
        apply_lane_labels() {{ :; }}
        _emit() {{ :; }}
        TICK_AGENT_RC="-1"
        run_lane "{fx['wt']}" "prompt" ""
        printf 'rc=%s lane=%s model=%s\\n' "$TICK_AGENT_RC" "$TICK_LANE" "$TICK_MODEL"
    """)
    env = {
        "PATH": fx["bin"] + ":/usr/bin:/bin",
        "HOME": fx["base"],
        "MODE": "fix",
        "PR": "1242",
        "SLOT": "1",
        "WORKER_ID": "1",
        "DRY_RUN": "0",
        "CLAUDE_AVAIL": "0",
        "OLLAMA_AVAIL": "1",
        "CLAUDE_PCT": "80",
        "OLLAMA_PCT": "20",
        "DEGRADED": "0",
        "CLAUDE_TIMEOUT": "30s",
    }
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert f"rc={claude_rc} lane=claude model=default" in out.stdout, out.stdout + out.stderr


def test_run_lane_exports_agent_rc_after_the_run():
    """The assignment must sit after the agent's rc is captured, and be exported for the trap."""
    run_block = re.search(r"run_lane\(\) \{.*^\}", RUN_LANE_SOURCE, re.S | re.M).group(0)
    assert "TICK_AGENT_RC" in run_block, "run_lane must record the agent exit status"
    assert run_block.index("TICK_AGENT_RC") > run_block.rindex("rc=$?")
    assert re.search(r"export TICK_AGENT_RC", run_block)
