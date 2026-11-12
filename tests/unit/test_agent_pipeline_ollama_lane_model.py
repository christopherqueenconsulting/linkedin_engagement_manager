"""The Ollama lane's `[claude-code:unrecognized_model]` line is a notice, not a failure.

On 2026-11-12 a fallback run logged
`[claude-code:unrecognized_model] {"model":"lem-agent-tier2","query_source":"sdk"}` next to an
`rc=124`, and the pair read as "Claude Code rejects the tier alias". It does not: the CLI prints
that line to stderr once per process for any model name outside its own catalogue, then sends the
request anyway. The depfix run four minutes earlier printed the same line and exited 0 with its
fix pushed. The rc=124 was the docfix 600s budget running out.

These tests run `run_lane()` for real against a fake `claude` and pin three things:

- the Ollama lane still hands the CLI the tier alias and the LiteLLM base URL (the contract the
  notice is about);
- a run that prints the notice and exits 0 is recorded as a success, and the log says the notice
  is non-fatal;
- an rc=124 is logged as the budget running out, not under the generic triple.
"""

import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LANE_SH = REPO_ROOT / "scripts" / "agent-pipeline" / "lib" / "run_lane.sh"
NOTICE = '[claude-code:unrecognized_model] {"model":"lem-agent-tier2","query_source":"sdk"}'


def _install(tmp_path: Path, claude_rc: int, notice: bool) -> dict:
    """Build a self-contained $BASE with a fake `claude` that records how it was launched."""
    lib = tmp_path / "lib"
    lib.mkdir()
    for sh in RUN_LANE_SH.parent.glob("*.sh"):
        (lib / sh.name).write_text(sh.read_text(encoding="utf-8"), encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    profile = RUN_LANE_SH.parent.parent / "config" / "claude-headless.json"
    (config / profile.name).write_text(profile.read_text(encoding="utf-8"), encoding="utf-8")
    binf = tmp_path / "bin"
    binf.mkdir()
    argv_file = tmp_path / "claude.argv"
    emit = f"echo '{NOTICE}' >&2\n" if notice else ""
    claude = binf / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        f'echo "BASE_URL=$ANTHROPIC_BASE_URL" >> "{argv_file}"\n'
        f"{emit}"
        'echo "agent output"\n'
        f"exit {claude_rc}\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    return {"base": str(tmp_path), "bin": str(binf), "wt": str(wt), "argv": argv_file}


def _run_ollama_lane(tmp_path: Path, claude_rc: int, notice: bool = True):
    fx = _install(tmp_path, claude_rc, notice)
    script = textwrap.dedent(f"""
        set -uo pipefail
        BASE="{fx['base']}"
        LOGDIR="$BASE/logs"; mkdir -p "$LOGDIR"
        LOG="$LOGDIR/actions.log"
        . "$BASE/lib/run_lane.sh"
        log() {{ echo "LOG: $*"; }}
        posthog_capture() {{ :; }}
        record_lane_outcome() {{ echo "OUTCOME: lane=$1 ok=$2 ul=$3"; }}
        apply_lane_labels() {{ :; }}
        _emit() {{ :; }}
        run_lane "{fx['wt']}" "prompt" ""
        printf 'rc=%s lane=%s model=%s\\n' "$TICK_AGENT_RC" "$TICK_LANE" "$TICK_MODEL"
    """)
    env = {
        "PATH": fx["bin"] + ":/usr/bin:/bin",
        "HOME": fx["base"],
        "MODE": "docfix",
        "PR": "2532",
        "SLOT": "1",
        "WORKER_ID": "1",
        "DRY_RUN": "0",
        # Claude unavailable, Ollama available -> the fallback lane, as on the live box.
        "CLAUDE_AVAIL": "1",
        "OLLAMA_AVAIL": "0",
        "CLAUDE_PCT": "40",
        "OLLAMA_PCT": "75",
        "DEGRADED": "0",
        "OLLAMA_DEFAULT_TIER": "lem-agent-tier2",
        "OLLAMA_LITELLM_URL": "http://127.0.0.1:4000",
        "LITELLM_MASTER_KEY": "test-not-a-key",
        "CLAUDE_TIMEOUT": "30s",
    }
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
    return out, fx


def test_the_ollama_lane_passes_the_tier_alias_to_the_litellm_endpoint(tmp_path):
    """The alias the notice names is exactly what the lane sends, at LiteLLM — not Anthropic."""
    out, fx = _run_ollama_lane(tmp_path, 0)
    assert "rc=0 lane=ollama model=lem-agent-tier2" in out.stdout, out.stdout + out.stderr
    argv = fx["argv"].read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("--model") + 1] == "lem-agent-tier2"
    assert "BASE_URL=http://127.0.0.1:4000" in argv


def test_a_run_that_prints_the_notice_and_exits_zero_is_a_success(tmp_path):
    """The notice must not turn a working run into a lane failure, and the log says why."""
    out, _ = _run_ollama_lane(tmp_path, 0)
    assert "OUTCOME: lane=ollama ok=1" in out.stdout, out.stdout + out.stderr
    assert "non-fatal notice for a model name" in out.stdout
    assert "lem-agent-tier2" in out.stdout
    assert "exited rc=" not in out.stdout


def test_no_note_when_the_cli_printed_no_notice(tmp_path):
    """The note explains a line that is there; it is not boilerplate on every run."""
    out, _ = _run_ollama_lane(tmp_path, 0, notice=False)
    assert "rc=0 lane=ollama" in out.stdout, out.stdout + out.stderr
    assert "non-fatal notice" not in out.stdout


def test_a_timeout_is_logged_as_the_budget_running_out(tmp_path):
    """rc=124 names CLAUDE_TIMEOUT, so it is not read as a model or endpoint fault."""
    out, _ = _run_ollama_lane(tmp_path, 124)
    assert "rc=124 lane=ollama" in out.stdout, out.stdout + out.stderr
    assert "OUTCOME: lane=ollama ok=0" in out.stdout
    assert "ran out its 30s budget (CLAUDE_TIMEOUT)" in out.stdout
    assert "(timeout/interrupt/limit)" not in out.stdout


def test_other_failures_keep_the_generic_message(tmp_path):
    """Only 124 is a known timeout; rc=1 stays under the generic triple."""
    out, _ = _run_ollama_lane(tmp_path, 1)
    assert "ollama claude exited rc=1 (timeout/interrupt/limit)" in out.stdout, out.stdout + out.stderr
    assert "budget (CLAUDE_TIMEOUT)" not in out.stdout
