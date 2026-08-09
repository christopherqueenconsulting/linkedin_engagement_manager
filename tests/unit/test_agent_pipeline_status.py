"""Regression tests for the operator report in scripts/agent-pipeline/status.sh.

status.sh is the one command an operator runs when the pipeline looks wrong, so the failure that
matters is not a crash — it is the report DISAGREEING with tick.sh while looking authoritative.
Three of the four cases below are exactly that: a cap that omits the degraded clamp, a lane verdict
read past the TTL the pipeline itself honours, and a stall warning silently unreachable offline.
The fourth is a hang on a mistyped flag.

Everything here runs the shipped script for real against a scratch $BASE — it is self-contained and
every path it reads is overridable by env, which is what makes that possible.
"""

import json
import os
import re
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

STATUS_SH = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "status.sh"
SOURCE = STATUS_SH.read_text(encoding="utf-8")

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("bash") is None, reason="status.sh needs bash"),
]


def _base(tmp_path: Path, config: str = "", ollama_state: str = "") -> Path:
    """A scratch $BASE laid out the way the installer leaves one on the box."""
    for sub in ("state", "logs", "locks"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.env").write_text(config, encoding="utf-8")
    if ollama_state:
        (tmp_path / "state" / "ollama.state").write_text(ollama_state, encoding="utf-8")
    return tmp_path


def _run(base: Path, *args: str, outcomes: str = "", timeout: int = 60):
    """Run status.sh against `base`, isolated from the real box."""
    env = {
        **os.environ,
        "BASE": str(base),
        "REPO": str(base),
        "OUTCOMES": outcomes or str(base / "logs" / "tick-outcomes.ndjson"),
        "CLAUDE_PROJECTS_DIR": str(base / "projects"),
        "NO_COLOR": "1",
    }
    return subprocess.run(
        ["bash", str(STATUS_SH), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _json(base: Path, *args: str, outcomes: str = "") -> dict:
    proc = _run(base, "--no-gh", "--json", *args, outcomes=outcomes)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_hours_without_a_value_is_refused_not_hung(tmp_path):
    """`--hours` as the last argument used to spin forever.

    `shift 2` with one argument left fails WITHOUT consuming anything, so the option loop re-read
    the same `$1` for ever — no output, no exit, terminal wedged on a plain typo.
    """
    proc = _run(_base(tmp_path), "--hours", timeout=20)
    assert proc.returncode == 2
    assert "--hours" in proc.stderr


def test_hours_rejects_a_non_numeric_window(tmp_path):
    proc = _run(_base(tmp_path), "--hours", "six", timeout=20)
    assert proc.returncode == 2


def test_cap_applies_the_degraded_clamp_like_tick_sh(tmp_path):
    """Neither lane above the threshold forces CAP=1 in tick.sh, so it must here too.

    Reporting the unclamped cap understates the constraint at exactly the moment an operator is
    reading the report to find out why nothing is moving.
    """
    base = _base(tmp_path, config="MAX_AGENTS=5\nCLAUDE_CAPACITY_PCT=10\nOLLAMA_CAPACITY_PCT=20\n")
    report = _json(base)
    assert report["pipeline"]["cap"] == 1
    assert report["pipeline"]["degraded"] is True

    healthy = _json(_base(tmp_path / "ok", config="MAX_AGENTS=5\nCLAUDE_CAPACITY_PCT=90\n"))
    assert healthy["pipeline"]["degraded"] is False


def test_config_secrets_never_reach_the_output(tmp_path):
    """config.env holds AGENT_GH_TOKEN in plaintext; the report is pasted into issues and chat."""
    base = _base(tmp_path, config="AGENT_GH_TOKEN=ghp_tokenthatmustnotappear\nMAX_AGENTS=2\n")
    proc = _run(base, "--no-gh")
    assert proc.returncode == 0
    assert "ghp_tokenthatmustnotappear" not in proc.stdout + proc.stderr


ALIAS_HELD = re.search(r"\nalias_held\(\) \{.*?\n\}\n", SOURCE, re.S)


def _alias_verdict(alias_ok: str, age_seconds: int) -> str:
    """Run the shipped `alias_held` for real against a stubbed ollama.state.

    Extracted rather than driven through a full run: the lane's earlier branch is a live curl to
    LiteLLM, which is absent in the unit lane, so a whole-script run could never reach this clause.
    """
    assert ALIAS_HELD, "alias_held() not found in status.sh"
    script = textwrap.dedent(f"""
        set -uo pipefail
        NOW=1000000
        ALIAS_TTL=3600
        state_get() {{ case "$2" in
            alias_ok)    printf '%s' '{alias_ok}' ;;
            alias_ok_ts) printf '%s' '{1000000 - age_seconds}' ;;
        esac; }}
        {ALIAS_HELD.group(0)}
        if alias_held; then echo HELD; else echo CLEAR; fi
    """)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=30).stdout.strip()


def test_expired_alias_verdict_is_not_reported_as_a_dead_lane():
    """capacity.sh only trusts ollama.state's `alias_ok` inside OLLAMA_ALIAS_TTL.

    Reading the flag without its timestamp holds the lane `unavailable` — and raises a NEEDS
    ATTENTION warning — off a verdict the pipeline has already stopped believing.
    """
    assert _alias_verdict("0", 60) == "HELD"        # fresh "not served" verdict still stands
    assert _alias_verdict("0", 7200) == "CLEAR"     # past the TTL capacity.sh re-probes on
    assert _alias_verdict("1", 60) == "CLEAR"       # alias is served
    assert _alias_verdict("", 60) == "CLEAR"        # never probed is not a failure

    # And the lane clause has to go through it rather than reading the flag raw again.
    assert "elif alias_held; then" in SOURCE


def test_stall_warning_still_fires_without_github(tmp_path):
    """"Ticks run but nothing dispatches" is gated on the ready count.

    Under --no-gh that count comes from the last tick's own record, so it has to be resolved BEFORE
    the rollup reads it — otherwise the warning is unreachable in the one mode where the operator
    has the least other signal.
    """
    base = _base(tmp_path)
    outcomes = base / "logs" / "o.ndjson"
    stamp = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))  # noqa: E731
    outcomes.write_text(
        json.dumps({"tick_outcome": "dispatched", "mode": "start", "ready_count": 7,
                    "duration_ms": 1000, "ts": stamp(7200)}) + "\n"
        + json.dumps({"tick_outcome": "skipped", "reason": "all_slots_busy",
                      "ready_count": 7, "ts": stamp(120)}) + "\n",
        encoding="utf-8",
    )
    report = _json(base, outcomes=str(outcomes))
    assert any("stalled, not idle" in w for w in report["warnings"])

    # ...and stays quiet when the pipeline dispatched recently — the same file, one fresh row.
    outcomes.write_text(
        json.dumps({"tick_outcome": "dispatched", "mode": "start", "ready_count": 7,
                    "duration_ms": 1000, "ts": stamp(120)}) + "\n",
        encoding="utf-8",
    )
    assert not any("stalled, not idle" in w for w in _json(base, outcomes=str(outcomes))["warnings"])
