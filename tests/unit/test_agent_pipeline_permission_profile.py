"""The agent permission profile — the default for every pipeline MODE.

`run_lane.sh` launches every agent under `--permission-mode dontAsk --settings
$BASE/config/claude-headless.json`, and logs every denied tool call to `denials.jsonl`.

Properties that matter more than any single rule in the profile:

* UNSET means the profile, for every MODE — not the historical skip-permissions argv.
* The profile never silently degrades — a missing profile (the default path included) refuses the
  dispatch instead of falling back, and the run's text still reaches `$LOG` and the usage-limit
  detector.
* The way back is explicit and loud: `LEM_PERMISSION_PROFILE=off` restores the old flag and logs it
  on every dispatch; `LEM_PERMISSION_PROFILE_MODES` restricts the profile only when set.
* Every runbook step a MODE needs is allowed, and the rules that keep it bounded hold: no `gh api`,
  no `sudo`, no `env` prefix, no plain `git push --force`, no `git rebase --exec`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
PIPELINE = REPO / "scripts" / "agent-pipeline"
RUN_LANE = PIPELINE / "lib" / "run_lane.sh"
CAPACITY = PIPELINE / "lib" / "capacity.sh"
REVIEW_THREADS = PIPELINE / "lib" / "review_threads.sh"
PROFILE = PIPELINE / "config" / "claude-headless.json"
INSTALL = PIPELINE / "install.sh"
RUNBOOK_DIR = PIPELINE / "runbook"
REVIEW_THREADS_INSTALLED = "/home/lem/agent-pipeline/lib/review_threads.sh"

#: Every MODE agent_run.sh / tick.sh dispatches through run_lane.
MODES = ["start", "fix", "review", "selfreview", "rebase", "revise", "depfix", "docfix", "phasefix"]

FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$ARGV_FILE"
printf 'GIT_EDITOR=%s\\n' "${GIT_EDITOR:-}" > "$ENV_FILE"
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
        "Bash(gh api *)",
        "Bash(env *)",
        "Bash(git push --force *)",
        "Bash(git rebase --ex*)",
        "Bash(git rebase -x*)",
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


@pytest.mark.parametrize("prefix", ["Bash(gh api", "Bash(sudo", "Bash(env", "Bash(docker", "Bash(bash"])
def test_no_allow_rule_opens_a_denied_surface(prefix):
    """No allow rule may open a surface the profile exists to keep shut.

    `gh api` is reached only through review_threads.sh; an `env` allow would carry `gh api` past
    the deny (`env -u GH_TOKEN gh api …`), and sudo/docker are owner-only.
    """
    allow = _profile()["permissions"]["allow"]
    assert not [r for r in allow if r.startswith(prefix)], prefix


def test_git_rebase_is_allowed_only_in_the_forms_mode_rebase_uses():
    """Only the four rebase forms MODE=rebase uses are allowed.

    Git accepts abbreviated long options (`--exe` is `--exec`), so no deny list can fence off
    command execution under a `git rebase *` allow. The exact forms are the control.
    """
    rebase = sorted(r for r in _profile()["permissions"]["allow"] if r.startswith("Bash(git rebase"))
    assert rebase == sorted([
        "Bash(git rebase origin/main)",
        "Bash(git rebase --continue)",
        "Bash(git rebase --abort)",
        "Bash(git rebase --skip)",
    ])


def test_the_one_graphql_path_is_the_installed_helper():
    allow = _profile()["permissions"]["allow"]
    assert f"Bash({REVIEW_THREADS_INSTALLED} *)" in allow
    assert REVIEW_THREADS_INSTALLED.endswith("/lib/" + REVIEW_THREADS.name)


def test_installer_ships_the_profile_and_the_helper():
    """A profile the installer never copies cannot be applied — and the default REFUSES without it."""
    text = INSTALL.read_text(encoding="utf-8")
    assert 'for f in "$SRC"/config/*.json' in text
    assert 'for f in "$SRC"/lib/*.sh' in text


# ---------------------------------------------------------------- rule semantics
#
# A matcher implementing Claude Code's documented Bash-rule rules (code.claude.com/docs/en/permissions,
# "Wildcard patterns"): `*` stands in for any text; the space before a trailing `*` is part of the
# rule; a trailing ` *` that is the rule's only wildcard also matches the bare command; `:*` at the end
# equals ` *`. Deny is checked before allow, and in dontAsk anything unmatched is denied.


def _bash_rules(kind: str) -> list[str]:
    rules = _profile()["permissions"][kind]
    return [r[len("Bash("):-1] for r in rules if r.startswith("Bash(") and r.endswith(")")]


def _matches(rule: str, command: str) -> bool:
    if rule.endswith(":*"):
        rule = rule[:-2] + " *"
    pattern = "^" + ".*".join(re.escape(part) for part in rule.split("*")) + "$"
    if re.match(pattern, command, re.S):
        return True
    return rule.count("*") == 1 and rule.endswith(" *") and command == rule[:-2]


def _decide(command: str) -> str:
    if any(_matches(r, command) for r in _bash_rules("deny")):
        return "deny"
    if any(_matches(r, command) for r in _bash_rules("allow")):
        return "allow"
    return "deny"


def test_matcher_follows_the_documented_examples():
    """Guard the guard: the doc's own table, so a matcher bug cannot make the tests below vacuous."""
    assert _matches("ls *", "ls -la") and _matches("ls *", "ls") and not _matches("ls *", "lsof")
    assert _matches("ls*", "lsof")
    assert _matches("git log * main", "git log --oneline main")
    assert not _matches("git log * main", "git log main")
    assert _matches("* --help *", "npm --help x") and not _matches("* --help *", "npm --help")
    assert _matches("ls:*", "ls -la")


@pytest.mark.parametrize(
    "command",
    [
        # MODE=rebase
        "git fetch origin main",
        "git rebase origin/main",
        "git rebase --continue",
        "git rebase --abort",
        "git push --force-with-lease",
        "git push --force-with-lease origin feature/claude-issue-1",
        # MODE=review / revise
        f"{REVIEW_THREADS_INSTALLED} list 2338",
        f"{REVIEW_THREADS_INSTALLED} resolve PRRT_kwDOabc123",
        "gh pr view 2338 --json reviews,comments",
        "gh pr diff 2338",
        "gh pr comment 2338 --body-file tmp/review-reply.md",
        # MODE=start / escalation / phasefix
        "gh issue edit 41 --add-label needs-human --add-assignee gitchrisqueen --remove-label agent:ready",
        "gh issue create --title \"x — Phase 2 (follow-up of #41)\" --label agent:ready --body-file tmp/f.md",
        "gh pr ready --undo 9",
        "git push -u origin feature/claude-issue-41",
        # MODE=depfix (b), MODE=docfix
        "git switch -c fix/pexels-401 origin/main",
        "git diff --name-only origin/main...HEAD -- '*.py'",
        "poetry run ruff check src/a.py tests/b.py",
        # the preamble's venv trap
        "poetry install --with test",
    ],
)
def test_every_documented_runbook_command_is_allowed(command):
    assert _decide(command) == "allow", command


@pytest.mark.parametrize(
    "command",
    [
        "git push --force",
        "git push --force origin feature/x",
        "git push origin feature/x --force",
        "git push -f",
        "git push -f origin feature/x",
        "git push origin +feature/x",
        "git push origin main",
        "git push --force-with-lease origin main",
        "git push --force-with-lease origin feature/x:main",
        "git push origin feature/x:refs/heads/main",
        "git rebase --exec 'make' origin/main",
        "git rebase --exe=make origin/main",
        "git rebase -x make origin/main",
        "git rebase origin/main --exec make",
        "git rebase -i origin/main",
        "git rebase origin/feature",
        "gh api graphql -f query='mutation{x}'",
        "gh api repos/christopherqueenconsulting/linkedin_engagement_manager/pulls/1/comments",
        "env -u GH_TOKEN gh pr view 1 --json title",
        "env -u GH_TOKEN gh api graphql -f query=x",
        "sudo docker exec -i celery_worker_selenium python - --require-debug-node",
        "docker ps",
        "git switch main",
        "git checkout main",
        "bash -c 'gh api graphql'",
    ],
)
def test_the_bounding_rules_hold(command):
    assert _decide(command) == "deny", command


# ---------------------------------------------------------------- runbooks match the profile


#: A line that names a command in order to forbid it is not an instruction to run it.
_FORBIDS = re.compile(r"\bden(y|ies|ied)\b|\bDo not prefix\b")


def _runbook_offenders(texts: dict[str, str], needle: str) -> list[str]:
    return [
        f"{name}:{i}" for name, text in sorted(texts.items())
        for i, line in enumerate(text.splitlines(), 1)
        if needle in line and not _FORBIDS.search(line)
    ]


_DENIED_IN_RUNBOOKS = ["gh api graphql", "gh api repos/", "sudo docker", "| xargs", "env -u GH_TOKEN"]


@pytest.mark.parametrize("needle", _DENIED_IN_RUNBOOKS)
def test_no_runbook_instructs_a_denied_command(needle):
    """A runbook step the profile denies is a mode that cannot do its job — and reads as a stuck PR."""
    texts = {p.name: p.read_text(encoding="utf-8") for p in RUNBOOK_DIR.glob("*.md")}
    assert len(texts) == len(MODES) + 1  # every mode file + the preamble
    assert not _runbook_offenders(texts, needle)


def test_runbook_scan_catches_the_commands_it_exists_for():
    """Guard the guard: the steps this change rewrote must register as offenders."""
    old = {
        "review.md": "   `gh api graphql -f query='mutation($t:ID!){resolveReviewThread(...)}' -f t=x`",
        "revise.md": "   - Inline review comments: `gh api repos/o/n/pulls/$PR/comments`",
        "_preamble.md": "  `sudo docker exec -i celery_worker_selenium python - --require-debug-node`",
        "docfix.md": "   `git diff --name-only origin/main...HEAD -- '*.py' | xargs -r poetry run ruff check`",
        "start.md": "   `env -u GH_TOKEN gh pr view 1`",
    }
    for needle in _DENIED_IN_RUNBOOKS:
        assert _runbook_offenders(old, needle), needle


def test_preamble_tells_agents_about_the_profile_and_the_env_prefix():
    text = (RUNBOOK_DIR / "_preamble.md").read_text(encoding="utf-8")
    assert "Do not prefix `env -u GH_TOKEN`" in text
    assert "Live validation is owner-run" in text
    assert "--dangerously-skip-permissions" not in text


# ---------------------------------------------------------------- review_threads.sh


def _fake_gh(tmp_path: Path) -> dict[str, str]:
    binf = tmp_path / "ghbin"
    binf.mkdir()
    gh = binf / "gh"
    gh.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$GH_ARGV"\necho \'{"data":{}}\'\n', encoding="utf-8")
    gh.chmod(0o755)
    return {"PATH": f"{binf}:/usr/bin:/bin", "GH_ARGV": str(tmp_path / "gh-argv")}


def _threads(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    env = _fake_gh(tmp_path)
    r = subprocess.run(["bash", str(REVIEW_THREADS), *args], capture_output=True, text=True, env=env, timeout=30)
    argv_file = tmp_path / "gh-argv"
    return r, (argv_file.read_text(encoding="utf-8").splitlines() if argv_file.exists() else [])


def test_review_threads_list_runs_the_fixed_query_for_this_repo(tmp_path):
    r, argv = _threads(tmp_path, "list", "2338")
    assert r.returncode == 0, r.stderr
    assert argv[:2] == ["api", "graphql"]
    query = argv[argv.index("-f") + 1]
    assert query.startswith("query=") and "reviewThreads" in query and "mutation" not in query
    assert "o=christopherqueenconsulting" in argv and "n=linkedin_engagement_manager" in argv
    assert "p=2338" in argv


def test_review_threads_resolve_runs_only_the_resolve_mutation(tmp_path):
    r, argv = _threads(tmp_path, "resolve", "PRRT_kwDOabc-12_3")
    assert r.returncode == 0, r.stderr
    queries = [a for a in argv if a.startswith("query=")]
    assert queries == ["query=mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{id isResolved}}}"]
    assert "t=PRRT_kwDOabc-12_3" in argv


@pytest.mark.parametrize(
    "args",
    [
        ("list", "12; rm -rf /"),
        ("list", "abc"),
        ("list", "0"),
        ("resolve", "MDEyOlB1bGxSZXF1ZXN0"),
        ("resolve", "PRRT_x -f query=mutation{deleteRef}"),
        ("resolve", "PRRT_x", "-f", "query=mutation{x}"),
        ("list",),
        ("delete", "PRRT_x"),
        (),
    ],
)
def test_review_threads_refuses_anything_else_before_gh_runs(tmp_path, args):
    r, argv = _threads(tmp_path, *args)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert argv == [], "gh must not run on a refused argument"


# ---------------------------------------------------------------- run_lane, for real


def _fixture(tmp_path: Path, stdout: str, rc: int = 0, mode: str = "fix", ship_profile: bool = True) -> dict:
    lib = tmp_path / "lib"
    lib.mkdir()
    for sh in RUN_LANE.parent.glob("*.sh"):
        (lib / sh.name).write_text(sh.read_text(encoding="utf-8"), encoding="utf-8")
    if ship_profile:
        (tmp_path / "config").mkdir()
        shutil.copy(PROFILE, tmp_path / "config" / PROFILE.name)
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
            "MODE": mode,
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
            "ENV_FILE": str(tmp_path / "agent-env"),
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


def _default_argv(base: Path) -> list[str]:
    return [
        "-p", "the prompt",
        "--permission-mode", "dontAsk", "--settings", str(base / "config" / "claude-headless.json"),
        "--output-format", "json",
        "--add-dir", str(base),
    ]


def _historical_argv(base: Path) -> list[str]:
    return ["-p", "the prompt", "--dangerously-skip-permissions", "--add-dir", str(base)]


def test_unset_defaults_to_the_dont_ask_profile(tmp_path):
    fx = _fixture(tmp_path, json.dumps({"type": "result", "result": "plain agent text"}) + "\n")
    r = _run(fx, None)
    assert "run_lane_rc=0" in r.stdout, r.stdout + r.stderr
    assert _argv(fx) == _default_argv(tmp_path)
    log = (tmp_path / "tick.log").read_text(encoding="utf-8")
    assert "plain agent text" in log
    entry = json.loads((tmp_path / "logs" / "denials.jsonl").read_text(encoding="utf-8"))
    assert entry["profile"] == str(tmp_path / "config" / "claude-headless.json")
    assert entry["parsed"] is True


def test_empty_value_is_the_default_not_the_old_flag(tmp_path):
    """A `LEM_PERMISSION_PROFILE=` line left in config.env must not read as an opt-out."""
    fx = _fixture(tmp_path, json.dumps({"type": "result", "result": "ok"}) + "\n")
    _run(fx, "")
    assert _argv(fx) == _default_argv(tmp_path)


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_gets_the_profile_by_default(tmp_path, mode):
    fx = _fixture(tmp_path, json.dumps({"type": "result", "result": "ok"}) + "\n", mode=mode)
    r = _run(fx, None)
    assert "run_lane_rc=0" in r.stdout, r.stdout + r.stderr
    argv = _argv(fx)
    assert "--dangerously-skip-permissions" not in argv
    assert argv == _default_argv(tmp_path)
    assert json.loads((tmp_path / "logs" / "denials.jsonl").read_text(encoding="utf-8"))["mode"] == mode


def test_missing_default_profile_refuses_instead_of_falling_back(tmp_path):
    """A box whose installer never shipped config/ must stop, not run every lane unrestricted."""
    fx = _fixture(tmp_path, "never printed\n", ship_profile=False)
    r = _run(fx, None)
    assert "run_lane_rc=1" in r.stdout, r.stdout + r.stderr
    assert "REFUSING to dispatch" in r.stdout
    assert _argv(fx) == [], "the agent must not run at all"


def test_off_is_the_loud_emergency_opt_out(tmp_path):
    fx = _fixture(tmp_path, "plain agent text\n")
    r = _run(fx, "off")
    assert "run_lane_rc=0" in r.stdout, r.stdout + r.stderr
    assert _argv(fx) == _historical_argv(tmp_path)
    assert "PERMISSION PROFILE OFF" in r.stdout
    log = (tmp_path / "tick.log").read_text(encoding="utf-8")
    assert log == "plain agent text\nfake stderr line\n"
    assert not (tmp_path / "logs" / "denials.jsonl").exists()


def test_off_needs_no_profile_file(tmp_path):
    """The opt-out is for emergencies — including a broken or missing profile."""
    fx = _fixture(tmp_path, "plain agent text\n", ship_profile=False)
    r = _run(fx, "off")
    assert "run_lane_rc=0" in r.stdout, r.stdout + r.stderr
    assert _argv(fx) == _historical_argv(tmp_path)


def test_headless_agents_get_no_editor(tmp_path):
    """`git rebase --continue` would otherwise wait on an editor until the lane timeout."""
    fx = _fixture(tmp_path, json.dumps({"type": "result", "result": "ok"}) + "\n")
    _run(fx, None)
    assert (tmp_path / "agent-env").read_text(encoding="utf-8") == "GIT_EDITOR=true\n"


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
    assert entry["profile"] == str(profile)
    assert entry["denial_count"] == 2
    assert entry["denials"][0] == {"tool_name": "Bash", "tool_input": {"command": "curl -s example.com"}}
    assert entry["ts"].endswith("Z")


def test_profile_run_with_no_denials_still_logs_a_line(tmp_path):
    """Zero denials is evidence the profile fits the mode — it must be a row, not an absence."""
    fx = _fixture(tmp_path, json.dumps({"type": "result", "result": "ok", "permission_denials": []}))
    _run(fx, None)
    entry = json.loads((tmp_path / "logs" / "denials.jsonl").read_text(encoding="utf-8"))
    assert entry["denial_count"] == 0
    assert entry["denials"] == []


def test_missing_explicit_profile_refuses_instead_of_falling_back(tmp_path):
    fx = _fixture(tmp_path, "never printed\n")
    r = _run(fx, str(tmp_path / "absent.json"))
    assert "run_lane_rc=1" in r.stdout, r.stdout + r.stderr
    assert "REFUSING to dispatch" in r.stdout
    assert _argv(fx) == [], "the agent must not run at all"


def test_unparseable_output_is_kept_and_flagged(tmp_path):
    fx = _fixture(tmp_path, "Traceback: the CLI crashed before printing JSON\n", rc=1)
    _run(fx, None)
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
    r = _run(fx, None)
    assert "pausing claude lane" in r.stdout, r.stdout + r.stderr


def test_mode_restriction_leaves_unlisted_lanes_on_the_old_flag_and_says_so(tmp_path):
    """MODES is a restriction when set — the leftover shadow value the operator must clear."""
    fx = _fixture(tmp_path, "plain agent text\n")
    r = _run(fx, None, modes="selfreview")  # the fixture's MODE is `fix`
    assert "run_lane_rc=0" in r.stdout, r.stdout + r.stderr
    assert _argv(fx) == _historical_argv(tmp_path)
    assert "permission profile NOT applied to MODE=fix" in r.stdout
    assert not (tmp_path / "logs" / "denials.jsonl").exists()


def test_mode_restriction_applies_the_profile_to_a_listed_lane(tmp_path):
    fx = _fixture(tmp_path, json.dumps({"type": "result", "result": "ok"}) + "\n")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    _run(fx, str(profile), modes="review, fix")
    assert "--dangerously-skip-permissions" not in _argv(fx)
    assert "dontAsk" in _argv(fx)
    assert (tmp_path / "logs" / "denials.jsonl").exists()


def test_mode_restriction_with_the_default_profile_applies_to_a_listed_lane(tmp_path):
    fx = _fixture(tmp_path, json.dumps({"type": "result", "result": "ok"}) + "\n", mode="selfreview")
    _run(fx, None, modes="selfreview")
    assert _argv(fx) == _default_argv(tmp_path)


# ---------------------------------------------------------------- the two usage probes


def test_capacity_probe_runs_under_dont_ask(tmp_path):
    binf = tmp_path / "bin"
    binf.mkdir()
    claude = binf / "claude"
    claude.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$ARGV_FILE"\necho OK\n', encoding="utf-8")
    claude.chmod(0o755)
    script = f'BASE="{tmp_path}"; . "{CAPACITY}"; _claude_probe'
    env = {"PATH": f"{binf}:/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "ARGV_FILE": str(tmp_path / "argv")}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.stdout.strip() == "0", r.stdout + r.stderr  # parsed as a healthy probe
    argv = (tmp_path / "argv").read_text(encoding="utf-8").splitlines()
    assert argv == ["-p", "Reply with the single word OK.", "--permission-mode", "dontAsk"]


def test_spend_usage_probe_runs_under_dont_ask(monkeypatch):
    sys.path.insert(0, str(PIPELINE / "v2"))
    from lemd import spend

    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        out = json.dumps({"result": "Current session: 19% used · resets Aug 10, 4:39am (UTC)\n"})
        return type("Proc", (), {"returncode": 0, "stderr": "", "stdout": out})()

    monkeypatch.setattr(spend.subprocess, "run", fake_run)
    usage = spend._run_usage_cli({}, timeout=5)
    assert usage.readable and usage.session_pct == 19  # parsing unchanged
    assert seen == [["claude", "-p", "/usage", "--output-format", "json", "--permission-mode", "dontAsk"]]
    assert "--dangerously-skip-permissions" not in seen[0]
