"""The opt-in agent permission profile.

`LEM_PERMISSION_PROFILE` swaps the default permission flag for a `dontAsk` settings profile on the
lanes the owner opts in, and logs every denied tool call to `denials.jsonl`.

Two properties matter more than any rule in the profile:

* UNSET changes nothing — the agent's argv is exactly the historical one, and no new file appears.
* SET never silently degrades — a missing profile refuses the dispatch instead of falling back to
  the default argv, and the run's text still reaches `$LOG` and the usage-limit detector.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
PIPELINE = REPO / "scripts" / "agent-pipeline"
RUN_LANE = PIPELINE / "lib" / "run_lane.sh"
PROFILE = PIPELINE / "config" / "claude-headless.json"
INSTALL = PIPELINE / "install.sh"

FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$ARGV_FILE"
cat "$FAKE_STDOUT"
echo "fake stderr line" >&2
exit "${FAKE_RC:-0}"
"""


# ---------------------------------------------------------------- the profile itself


def _profile() -> dict:
    return json.loads(PROFILE.read_text(encoding="utf-8"))


def test_profile_is_dont_ask():
    """No human answers a prompt in a headless run; anything not allowed must be denied."""
    assert _profile()["permissions"]["defaultMode"] == "dontAsk"


def test_hooks_never_sit_inside_the_permission_block():
    """A `hooks` key nested in `permissions` voids the whole permission block.

    The profile would then allow nothing it means to and deny nothing it means to, with no error.
    """
    profile = _profile()
    assert "hooks" not in profile["permissions"]
    for key in profile["permissions"]:
        assert key in {"defaultMode", "allow", "deny", "ask", "additionalDirectories"}, key


@pytest.mark.parametrize(
    "rule",
    [
        "Bash(sudo *)",
        "Bash(docker *)",
        "Bash(*deploy.sh*)",
        "Bash(git -C /opt/lem *)",
        "Bash(rm -rf *)",
        "Bash(curl *)",
        "Bash(wget *)",
        "Bash(gh pr merge *)",
        "Read(//opt/lem/.env)",
        "Read(//home/lem/agent-pipeline/secrets.env)",
    ],
)
def test_profile_denies_the_listed_rules(rule):
    assert rule in _profile()["permissions"]["deny"]


def test_file_writes_are_confined_to_lane_worktrees():
    """run_lane runs every agent in $BASE/work/<branch>; nothing outside it is writable."""
    allow = _profile()["permissions"]["allow"]
    writes = [r for r in allow if r.split("(", 1)[0] in {"Edit", "Write", "MultiEdit", "NotebookEdit"}]
    assert writes, "the profile must let an agent edit its own worktree"
    for rule in writes:
        assert rule.endswith("(//home/lem/agent-pipeline/work/**)"), rule


def test_no_blanket_shell_grant():
    allow = _profile()["permissions"]["allow"]
    assert "Bash" not in allow
    assert not [r for r in allow if r in {"Bash(*)", "Bash(:*)"}]


def test_installer_ships_the_profile():
    """A profile the installer never copies cannot be pointed at."""
    assert 'for f in "$SRC"/config/*.json' in INSTALL.read_text(encoding="utf-8")


# ---------------------------------------------------------------- run_lane, for real


def _fixture(tmp_path: Path, stdout: str, rc: int = 0) -> dict:
    lib = tmp_path / "lib"
    lib.mkdir()
    for sh in RUN_LANE.parent.glob("*.sh"):
        (lib / sh.name).write_text(sh.read_text(encoding="utf-8"), encoding="utf-8")
    binf = tmp_path / "bin"
    binf.mkdir()
    claude = binf / "claude"
    claude.write_text(FAKE_CLAUDE, encoding="utf-8")
    claude.chmod(0o755)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    stdout_file = tmp_path / "fake-stdout"
    stdout_file.write_text(stdout, encoding="utf-8")
    return {
        "base": tmp_path,
        "wt": wt,
        "env": {
            "PATH": f"{binf}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "MODE": "fix",
            "ISSUE": "4242",
            "SLOT": "1",
            "WORKER_ID": "1",
            "DRY_RUN": "0",
            "CLAUDE_AVAIL": "0",
            "OLLAMA_AVAIL": "1",
            "CLAUDE_PCT": "80",
            "OLLAMA_PCT": "20",
            "DEGRADED": "0",
            "CLAUDE_TIMEOUT": "30s",
            "ARGV_FILE": str(tmp_path / "argv"),
            "FAKE_STDOUT": str(stdout_file),
            "FAKE_RC": str(rc),
        },
    }


def _run(fx: dict, profile: str | None, modes: str | None = None) -> subprocess.CompletedProcess:
    base = fx["base"]
    script = textwrap.dedent(f"""
        set -uo pipefail
        BASE="{base}"
        LOGDIR="$BASE/logs"; mkdir -p "$LOGDIR"
        LOG="$BASE/tick.log"
        . "$BASE/lib/run_lane.sh"
        log() {{ printf 'LOG: %s\\n' "$*"; }}
        posthog_capture() {{ :; }}
        record_lane_outcome() {{ :; }}
        apply_lane_labels() {{ :; }}
        _emit() {{ :; }}
        run_lane "{fx['wt']}" "the prompt" ""
        echo "run_lane_rc=$?"
    """)
    env = dict(fx["env"])
    if profile is not None:
        env["LEM_PERMISSION_PROFILE"] = profile
    if modes is not None:
        env["LEM_PERMISSION_PROFILE_MODES"] = modes
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)


def _argv(fx: dict) -> list[str]:
    path = fx["base"] / "argv"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_unset_keeps_the_historical_argv_and_output(tmp_path):
    fx = _fixture(tmp_path, "plain agent text\n")
    r = _run(fx, None)
    assert "run_lane_rc=0" in r.stdout, r.stdout + r.stderr
    assert _argv(fx) == [
        "-p", "the prompt", "--dangerously-skip-permissions", "--add-dir", str(tmp_path),
    ]
    log = (tmp_path / "tick.log").read_text(encoding="utf-8")
    assert log == "plain agent text\nfake stderr line\n"
    assert not (tmp_path / "logs" / "denials.jsonl").exists()


def test_profile_swaps_the_flag_and_logs_denials(tmp_path):
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Opened PR #9.",
        "permission_denials": [
            {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "curl -s example.com"}},
            {"tool_name": "Read", "tool_use_id": "t2", "tool_input": {"file_path": "/tmp/example.txt"}},
        ],
    }
    fx = _fixture(tmp_path, json.dumps(result) + "\n")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    r = _run(fx, str(profile))
    assert "run_lane_rc=0" in r.stdout, r.stdout + r.stderr

    argv = _argv(fx)
    assert "--dangerously-skip-permissions" not in argv
    i = argv.index("--permission-mode")
    assert argv[i:i + 6] == [
        "--permission-mode", "dontAsk", "--settings", str(profile), "--output-format", "json",
    ]

    # downstream reads TEXT: the result string, never the JSON envelope
    log = (tmp_path / "tick.log").read_text(encoding="utf-8")
    assert "Opened PR #9." in log
    assert "permission_denials" not in log
    assert "fake stderr line" in log

    lines = (tmp_path / "logs" / "denials.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["lane"] == "claude"
    assert entry["issue"] == "4242"
    assert entry["mode"] == "fix"
    assert entry["rc"] == 0
    assert entry["parsed"] is True
    assert entry["denial_count"] == 2
    assert entry["denials"][0] == {"tool_name": "Bash", "tool_input": {"command": "curl -s example.com"}}
    assert entry["ts"].endswith("Z")


def test_profile_run_with_no_denials_still_logs_a_line(tmp_path):
    """Zero denials is the evidence the shadow is collecting — it must be a row, not an absence."""
    fx = _fixture(tmp_path, json.dumps({"type": "result", "result": "ok", "permission_denials": []}))
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    _run(fx, str(profile))
    entry = json.loads((tmp_path / "logs" / "denials.jsonl").read_text(encoding="utf-8"))
    assert entry["denial_count"] == 0
    assert entry["denials"] == []


def test_missing_profile_refuses_instead_of_falling_back(tmp_path):
    fx = _fixture(tmp_path, "never printed\n")
    r = _run(fx, str(tmp_path / "absent.json"))
    assert "run_lane_rc=1" in r.stdout, r.stdout + r.stderr
    assert "REFUSING to dispatch" in r.stdout
    assert _argv(fx) == [], "the agent must not run at all"


def test_unparseable_output_is_kept_and_flagged(tmp_path):
    fx = _fixture(tmp_path, "Traceback: the CLI crashed before printing JSON\n", rc=1)
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    _run(fx, str(profile))
    log = (tmp_path / "tick.log").read_text(encoding="utf-8")
    assert "the CLI crashed before printing JSON" in log
    entry = json.loads((tmp_path / "logs" / "denials.jsonl").read_text(encoding="utf-8"))
    assert entry["parsed"] is False
    assert entry["denial_count"] is None
    assert entry["rc"] == 1


def test_usage_limit_inside_the_json_result_still_pauses_the_lane(tmp_path):
    """The usage-limit grep reads $out, so the JSON `.result` must be put back as text.

    Otherwise a limited lane would keep being dispatched.
    """
    msg = {"type": "result", "subtype": "success", "is_error": True,
           "result": "Claude AI usage limit reached|1760000000", "permission_denials": []}
    fx = _fixture(tmp_path, json.dumps(msg) + "\n", rc=1)
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    r = _run(fx, str(profile))
    assert "pausing claude lane" in r.stdout, r.stdout + r.stderr


def test_mode_filter_leaves_other_lanes_on_the_default(tmp_path):
    """Shadowing ONE lane must not change the argv of any other lane."""
    fx = _fixture(tmp_path, "plain agent text\n")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    r = _run(fx, str(profile), modes="review")  # the fixture's MODE is `fix`
    assert "run_lane_rc=0" in r.stdout, r.stdout + r.stderr
    assert _argv(fx) == [
        "-p", "the prompt", "--dangerously-skip-permissions", "--add-dir", str(tmp_path),
    ]
    assert not (tmp_path / "logs" / "denials.jsonl").exists()


def test_mode_filter_applies_the_profile_to_a_listed_lane(tmp_path):
    fx = _fixture(tmp_path, json.dumps({"type": "result", "result": "ok"}) + "\n")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    _run(fx, str(profile), modes="review, fix")
    assert "--dangerously-skip-permissions" not in _argv(fx)
    assert "dontAsk" in _argv(fx)
    assert (tmp_path / "logs" / "denials.jsonl").exists()
