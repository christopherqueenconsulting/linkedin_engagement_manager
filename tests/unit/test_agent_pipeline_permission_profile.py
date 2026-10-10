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
GH_SAFE = PIPELINE / "lib" / "gh_safe.sh"
GH_SAFE_INSTALLED = "/home/lem/agent-pipeline/lib/gh_safe.sh"
GIT_PUSH = PIPELINE / "lib" / "git_push.sh"
GIT_PUSH_INSTALLED = "/home/lem/agent-pipeline/lib/git_push.sh"
GIT_COMMIT = PIPELINE / "lib" / "git_commit.sh"
GIT_COMMIT_INSTALLED = "/home/lem/agent-pipeline/lib/git_commit.sh"

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
        "Bash(scripts/deploy.sh*)",
        "Bash(git -C /opt/lem *)",
        "Bash(rm -rf *)",
        "Bash(curl *)",
        "Bash(wget *)",
        "Bash(gh pr merge *)",
        "Bash(gh api *)",
        "Bash(env *)",
        "Bash(git push *)",
        "Bash(git push)",
        "Bash(git rebase --ex*)",
        "Bash(git rebase -x*)",
        "Bash(gh issue edit *)",
        "Bash(gh issue create *)",
        "Bash(git fetch *refs/*)",
        "mcp__playwright__browser_run_code_unsafe",
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


@pytest.mark.parametrize("prefix", [
    "Bash(gh api", "Bash(sudo", "Bash(env", "Bash(docker", "Bash(bash",
    # every GitHub write and every commit/push goes through a helper that sees expanded argv
    "Bash(gh pr edit", "Bash(gh pr create", "Bash(gh pr comment", "Bash(gh pr ready", "Bash(gh pr close",
    "Bash(gh issue edit", "Bash(gh issue create", "Bash(gh issue comment", "Bash(gh issue close",
    "Bash(git commit", "Bash(git push",
])
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


def _decide_one(command: str) -> str:
    if any(_matches(r, command) for r in _bash_rules("deny")):
        return "deny"
    if any(_matches(r, command) for r in _bash_rules("allow")):
        return "allow"
    return "deny"


def _decide(command: str) -> str:
    """Documented compound rule: split on shell separators; every part must be allowed.

    The split is deliberately naive (it ignores quoting), which only ever produces MORE parts — so it
    can turn an allow into a deny, never a deny into an allow. Env-var prefixes are NOT stripped:
    Claude Code strips only a fixed set of known-safe variables, and none of the runner's
    (BRANCH, MODE, ISSUE, PR) are in it, so a prefixed helper call must not match its allow rule.
    """
    parts = [p.strip() for p in re.split(r"&&|\|\||;|\||\n", command) if p.strip()]
    verdicts = [_decide_one(p) for p in parts] or ["deny"]
    return "allow" if all(v == "allow" for v in verdicts) else "deny"


def test_no_rule_ends_in_a_colon_wildcard():
    """Claude Code reads a trailing `:*` as ` *`, so `git push *:*` would mean `git push * *`.

    Such a rule does not do what it says: as a deny it blocks every multi-word command, as an allow it
    opens every one. A literal colon must never be the last character before the final `*`.
    """
    perms = _profile()["permissions"]
    assert not [r for r in perms["allow"] + perms["deny"] if r.endswith(":*)")]


def test_matcher_reads_a_trailing_colon_star_as_a_wildcard():
    assert _matches("git push *:*", "git push origin feature/x")      # NOT a colon test
    assert not _matches("git push *:main", "git push origin feature/x")


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
        "git rm src/cqc_lem/old_module.py",
        "git mv compose/local/database/migrations/V20260101000000__a.sql "
        "compose/local/database/migrations/V20261010000000__a.sql",
        GIT_PUSH_INSTALLED + " --force-with-lease",
        # MODE=review / revise
        f"{REVIEW_THREADS_INSTALLED} list 2338",
        f"{REVIEW_THREADS_INSTALLED} resolve 2338 PRRT_kwDOabc123",
        "gh pr view 2338 --json reviews,comments",
        "gh pr diff 2338",
        f"{GH_SAFE_INSTALLED} pr-comment 2338 --body-file tmp/review-reply.md",
        f"{GH_SAFE_INSTALLED} pr-comment 9 --body \"@dependabot rebase\"",
        f"{GH_SAFE_INSTALLED} issue-comment 41 --body-file tmp/x.md",
        "gh pr checks 9",
        "gh run view 123 --log-failed",
        "gh issue view 41 --comments",
        "gh issue list --search \"phase 2\"",
        # MODE=start / escalation / phasefix
        f"{GH_SAFE_INSTALLED} issue-edit 41 --add-label needs-human --add-assignee gitchrisqueen "
        "--remove-label agent:ready",
        f"{GH_SAFE_INSTALLED} issue-edit 41 --add-label agent:blocked --remove-label agent:working",
        f"{GH_SAFE_INSTALLED} pr-edit 9 --add-label agent:working --remove-label agent:revise "
        "--remove-label needs-human",
        f"{GH_SAFE_INSTALLED} issue-create --title \"x — Phase 2 (follow-up of #41)\" "
        "--body-file tmp/f.md --label enhancement",
        f"{GH_SAFE_INSTALLED} pr-create --title \"feat: x (closes #41)\" --body-file tmp/pr-body.md "
        "--label agent:working",
        f"{GH_SAFE_INSTALLED} pr-edit 9 --body-file tmp/pr-body.md",
        f"{GH_SAFE_INSTALLED} pr-edit 9 --remove-label agent:depfix",
        f"{GH_SAFE_INSTALLED} pr-ready 9 --undo",
        f"{GIT_COMMIT_INSTALLED} -m \"feat: x\" -m \"Co-Authored-By: Claude <noreply@anthropic.com>\"",
        f"{GIT_COMMIT_INSTALLED} -F tmp/commit-msg.txt",
        GIT_PUSH_INSTALLED,
        # a PR that touches the deploy script can still be staged and diffed
        "git add scripts/deploy.sh",
        "git diff scripts/deploy.sh",
        "git add scripts/deploy.sh docs/zero-downtime-deploys.md tests/unit/test_deploy.py",
        # MODE=depfix, MODE=docfix
        "git switch -c fix/pexels-401 origin/main",
        "poetry lock",
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
        # every raw push is denied — the helper is the only push path
        "git push",
        "git push -u origin feature/claude-issue-41",
        "git push --force-with-lease",
        "git push origin dependabot/pip/requests-2.33.0",
        GIT_PUSH_INSTALLED + " origin main",
        GIT_PUSH_INSTALLED + " HEAD:main",
        GIT_PUSH_INSTALLED + " --force",
        "git fetch origin x:main",
        "git fetch origin feature/x",
        # force, main, deletes, mirror — including trailing-argument forms
        "git push --force",
        "git push --force origin feature/x",
        "git push origin feature/x --force",
        "git push -f",
        "git push -f origin feature/x",
        "git push -fu origin x",
        "git push -u origin feature/x -f",
        "git push origin +feature/x",
        "git push origin main",
        "git push --force-with-lease origin main",
        "git push --force-with-lease origin feature/x:main",
        "git push origin feature/x:refs/heads/main",
        "git push origin HEAD:main --no-verify",
        "git push --force-with-lease origin x:refs/heads/main -q",
        "git push -u origin feature/x:main -q",
        "git push --mirror",
        "git push origin --delete fix/y",
        "git push origin fix/y --delete",
        "git push origin -d fix/y",
        "git push origin :fix/y",
        "git push -u origin feature/x --repo=https://example.com/other.git",
        "git push --all",
        "git push origin some-other-branch",
        # rebase
        "git rebase --exec 'make' origin/main",
        "git rebase --exe=make origin/main",
        "git rebase -x make origin/main",
        "git rebase origin/main --exec make",
        "git rebase -i origin/main",
        "git rebase origin/feature",
        "git rm -r src",
        "git rm src -r",
        "git mv src/a.py src/b.py",
        # file arguments outside the worktree
        "git log -1 --format=x --output=/home/lem/agent-pipeline/config.env",
        "git diff --no-index /home/lem/agent-pipeline/config.env /dev/null",
        "git show HEAD --output-file=/tmp/x",
        "gh issue comment 1 --body-file /home/lem/agent-pipeline/state/gh-app-token",
        "gh pr comment 1 --body-file ../../../agent-pipeline/config.env",
        "gh pr comment 1 --body-file=/home/lem/agent-pipeline/config.env",
        "gh pr comment 1 --body-file ~/.config/gh/hosts.yml",
        "gh pr comment 1 --body-file \"$HOME/.ssh/id_rsa\"",
        "gh pr comment 1 -F /etc/passwd",
        "gh pr create --title x -F ../x.md",
        "git commit -F /etc/passwd",
        "git commit -m x --file=/etc/passwd",
        "git commit -F ../../secrets.env",
        "git commit -t /etc/passwd",
        # gh issue edit is labels/assignees only, and never ADDS agent:ready
        "gh issue edit 1 --body \"x\"",
        "gh issue edit 1 --body-file tmp/x.md",
        "gh issue edit 1 -b x",
        "gh issue edit 1 -F tmp/x.md",
        "gh issue edit 1 --title x",
        "gh issue edit 1 -t x",
        "gh issue edit 1 --add-label agent:ready",
        "gh issue edit 1 --add-label=agent:ready",
        "gh issue edit 1 --add-label \"agent:ready\"",
        "gh issue edit 1 --add-label 'agent:ready'",
        "gh issue edit 1 --add-label bug,agent:ready",
        "gh issue edit 1 --add-label \"bug, agent:ready\"",
        "gh issue edit 1 --add-label=bug,agent:ready",
        "gh issue create --title x --label agent:ready --body-file tmp/a.md",
        "gh issue create --title x -l agent:ready --body-file tmp/a.md",
        "gh issue create --title x --label bug,agent:ready --body-file tmp/a.md",
        # round 3: issue edit/create only via the helper; no label adds on gh pr; no --repo
        "gh issue edit 41 --add-label needs-human",
        "gh issue edit 41 --remove-label agent:ready",
        "gh issue create --title x --body-file tmp/a.md",
        "gh pr edit 9 --add-label agent:working",
        "gh pr edit 9 --remove-label x --add-label release:now",
        "gh pr create --title x --body-file tmp/b.md --label agent:working",
        "gh pr create --title x --body-file tmp/b.md -l release:now",
        "gh pr view 1 --repo someone/else --json body",
        "gh pr comment 1 -R someone/else --body-file tmp/x.md",
        # push to main by ref resolution or a second refspec; fetch onto local main
        "git push origin feature/x:heads/main",
        "git push origin feature/x HEAD:heads/main",
        "git push --force-with-lease origin feature/x:heads/main",
        "git push origin feature/x refs/heads/main",
        "git fetch origin feature/x:refs/heads/main",
        "git push origin fix/domain-rename",
        # git diff with an outside path silently becomes --no-index
        "git diff /home/lem/agent-pipeline/config.env /dev/null",
        "git diff ../../agent-pipeline/config.env x",
        "git diff HEAD -- ~/.ssh/id_rsa",
        "git diff \"$HOME/x\" y",
        # pathspec from a file
        "git add --pathspec-from-file=/home/lem/agent-pipeline/config.env",
        "git commit -m x --pathspec-from-file /tmp/list",
        "git rm --pathspec-from-file=list.txt",
        # git rm bundles / protected trees, git mv out of migrations
        "git rm -rf src",
        "git rm -fr src",
        "git rm src -rq",
        "git rm .claude/settings.json",
        "git rm .github/workflows/ci.yml",
        "git mv compose/local/database/migrations/V1__a.sql src/x.sql",
        "git mv compose/local/database/migrations/V1__a.sql compose/local/database/migrations/../../x.sql",
        "poetry lock --no-update",
        # round 4: raw commits, abbreviated / bundled file flags
        "git commit -m x",
        "git commit --fil=/home/lem/agent-pipeline/state/gh-app-token",
        "git commit --fil /home/lem/.config/gh/hosts.yml",
        "git commit -aF/home/lem/agent-pipeline/config.env",
        "git commit --templ=/etc/lem/x",
        "git add --pathspec-fr=/home/lem/agent-pipeline/config.env",
        "git rm --pathspec-fr=/x",
        "git log --outp=/tmp/x",
        "git diff --no-ind /home/lem/agent-pipeline/config.env x",
        "git add /home/lem/agent-pipeline/config.env",
        "git add src ~/.ssh/id_rsa",
        "git add ../outside.txt",
        "git rm $HOME/x",
        # round 4: every raw gh write, however the flag is spelled
        "gh pr edit 9 --add-lab''el release:now",
        "gh pr edit 9 --add-lab${X}el agent:ready",
        "gh pr edit 9 --add-labe\\l agent:ready",
        "gh pr edit 9 --body-file tmp/x.md",
        "gh pr create --title t --body-file tmp/b.md --lab''el release:now",
        "gh pr create --title t --body-file tmp/b.md",
        "gh pr comment 1 --body-file tmp/x.md",
        "gh pr comment 1 --re''po other/repo --body x",
        "gh issue comment 1 --body x",
        "gh pr ready --undo 9",
        "gh pr close 9",
        "gh issue close 41",
        # round 4: an env prefix or a prior export cannot steer a helper
        f"BRANCH=main {GIT_PUSH_INSTALLED}",
        f"MODE=depfix {GIT_PUSH_INSTALLED}",
        f"ISSUE=1 {GH_SAFE_INSTALLED} issue-edit 1 --remove-label needs-human",
        f"export MODE=depfix && {GIT_PUSH_INSTALLED}",
        f"PR=5; {GH_SAFE_INSTALLED} pr-edit 5 --remove-label needs-human",
        # executing the deploy script, however spelled
        "scripts/deploy.sh v1.2.3",
        "./scripts/deploy.sh",
        "/opt/lem/scripts/deploy.sh v1.2.3",
        "bash scripts/deploy.sh",
        "bash -n scripts/deploy.sh",
        "sh scripts/deploy.sh",
        ". scripts/deploy.sh",
        # GraphQL, env prefixes, owner-only tools
        "gh api graphql -f query='mutation{x}'",
        "gh api repos/christopherqueenconsulting/linkedin_engagement_manager/pulls/1/comments",
        "env -u GH_TOKEN gh pr view 1 --json title",
        "env -u GH_TOKEN gh api graphql -f query=x",
        "sudo docker exec -i celery_worker_selenium python - --require-debug-node",
        "docker ps",
        "git switch main",
        "git checkout main",
        "bash -c 'gh api graphql'",
        "PYTHONPATH=src poetry run python -c 'import x'",
        "mv src/cqc_lem/ui/dist /tmp/dist",
    ],
)
def test_the_bounding_rules_hold(command):
    assert _decide(command) == "deny", command


def test_a_quoted_repo_flag_only_reaches_read_only_gh():
    """Documents the residual: `--re''po` defeats the `--repo` deny TEXT, so raw gh must be read-only.

    The plain spelling is denied; the quoted one slips past the pattern — and lands only on a READ
    (`gh pr view` of another public repo). Every gh write goes through gh_safe.sh, which pins the repo,
    so the same trick on a write has no raw command to ride on.
    """
    assert _decide("gh pr view 1 --repo other/repo --json body") == "deny"
    assert _decide("gh pr view 1 --re''po other/repo --json body") == "allow"
    raw_gh = [r for r in _bash_rules("allow") if r.startswith("gh ")]
    assert raw_gh and all(r.split()[2] in {"view", "diff", "checks", "list"} for r in raw_gh), raw_gh
    assert _decide("gh pr comment 1 --re''po other/repo --body x") == "deny"


# ---------------------------------------------------------------- runbooks match the profile


#: A line that names a command in order to forbid it is not an instruction to run it.
_FORBIDS = re.compile(r"\bden(y|ies|ied)\b|\bDo not prefix\b|\bnot available\b|\bowner-run\b|\bnever `")


def _runbook_offenders(texts: dict[str, str], pattern: str) -> list[str]:
    rx = re.compile(pattern)
    return [
        f"{name}:{i}" for name, text in sorted(texts.items())
        for i, line in enumerate(text.splitlines(), 1)
        if rx.search(line) and not _FORBIDS.search(line)
    ]


#: Regexes for runbook text that tells an agent to run something the profile denies.
_DENIED_IN_RUNBOOKS = [
    r"gh api graphql",
    r"gh api repos/",
    r"sudo docker",
    r"\| xargs",
    r"env -u GH_TOKEN",
    r"`[A-Z][A-Z0-9_]*=[^`\s]+ [a-z]",             # an env-assignment prefix: `PYTHONPATH=src poetry …`
    r"poetry run python\b",                       # standalone scripts (pytest is `poetry run pytest`)
    r"\bmove any built\b|`mv ",                   # relocating files: no `mv` in the profile
    r"--add-label agent:ready|\+ `agent:ready`|`agent:ready` \+",  # agents never ADD agent:ready
    r"--body \"[^\"]*$",                          # a --body whose text runs onto the next line
    r"review_threads\.sh resolve <",              # resolve without the PR argument
    r"Tick the acceptance boxes",                 # needs `gh issue edit --body`
    r"`git push",                                 # pushes go through lib/git_push.sh
    r"`gh issue (edit|create)|`gh pr edit [^`]*--add-label|--repo \"\$SLUG\"",  # labels via gh_safe.sh
    r"`git commit",                               # commits go through lib/git_commit.sh
    r"`gh (pr|issue) comment|`gh pr (create|edit|ready)",  # every gh write via gh_safe.sh
]


@pytest.mark.parametrize("pattern", _DENIED_IN_RUNBOOKS)
def test_no_runbook_instructs_a_denied_command(pattern):
    """A runbook step the profile denies is a mode that cannot do its job — and reads as a stuck PR."""
    texts = {p.name: p.read_text(encoding="utf-8") for p in RUNBOOK_DIR.glob("*.md")}
    assert len(texts) == len(MODES) + 1  # every mode file + the preamble
    assert not _runbook_offenders(texts, pattern)


#: Lines from the runbooks as they stood before this change, one per pattern.
_OLD_RUNBOOK_LINES = {
    "review.md": "   `gh api graphql -f query='mutation($t:ID!){resolveReviewThread(...)}' -f t=x`",
    "revise.md": "   - Inline review comments: `gh api repos/o/n/pulls/$PR/comments`",
    "_preamble.md": "  `sudo docker exec -i celery_worker_selenium python - --require-debug-node`",
    "docfix.md": "   `git diff --name-only origin/main...HEAD -- '*.py' | xargs -r poetry run ruff check`",
    "start.md": "   `env -u GH_TOKEN gh pr view 1`",
    "_preamble2.md": "  `PYTHONPATH=src poetry run python -c \"import cqc_lem.api.main as m\"` must be",
    "_preamble3.md": "  reproduce CI exactly, drop an empty `.env` into your worktree and move any built `dist` aside.",
    "start2.md": "     the remaining scope, labeled topical + `agent:ready` + a `priority:` (+ `risk:*` if",
    "selfreview.md": "   `gh pr comment $PR --body \"$MARKER — <PASS|FIXED n findings>",
    "review2.md": "     `/home/lem/agent-pipeline/lib/review_threads.sh resolve <thread_id>` (the `PRRT_…` id).",
    "_preamble4.md": "2. If **nothing** remains → normal `Closes #N`. Tick the acceptance boxes in the issue body",
    "fix.md": "4. Commit + `git push`. The push re-triggers CI. STOP.",
    "revise2.md": "   `gh pr edit $PR --add-label agent:working --remove-label agent:revise`.",
    "_preamble5.md": "    A multi-line commit message is `git commit -F tmp/<name>.txt`. A file argument outside your",
    "depfix2.md": "3. **Clear the flag**: `gh pr edit $PR --remove-label agent:depfix`.",
}


@pytest.mark.parametrize("pattern", _DENIED_IN_RUNBOOKS)
def test_runbook_scan_catches_the_commands_it_exists_for(pattern):
    """Guard the guard: every pattern must flag at least one line of the pre-change runbook text."""
    assert _runbook_offenders(_OLD_RUNBOOK_LINES, pattern), pattern


def test_preamble_tells_agents_about_the_profile_and_the_env_prefix():
    text = (RUNBOOK_DIR / "_preamble.md").read_text(encoding="utf-8")
    assert "Do not prefix `env -u GH_TOKEN`" in text
    assert "Live validation is owner-run" in text
    assert "--dangerously-skip-permissions" not in text


# ---------------------------------------------------------------- review_threads.sh

FAKE_GH = """#!/bin/sh
printf '%s\\n' "$@" >> "$GH_ARGV"
echo '--' >> "$GH_ARGV"
case "$*" in
  *resolveReviewThread*) echo MUTATION >> "$GH_ARGV"; echo '{"data":{}}' ;;
  *"node(id"*) [ -n "$FAKE_OWNER" ] && echo "$FAKE_OWNER" || exit 1 ;;
  *) echo '{"data":{}}' ;;
esac
"""


def _threads(tmp_path: Path, *args: str, owner: str = "") -> tuple[subprocess.CompletedProcess, list[str]]:
    binf = tmp_path / "ghbin"
    binf.mkdir(exist_ok=True)
    gh = binf / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    env = {"PATH": f"{binf}:/usr/bin:/bin", "GH_ARGV": str(tmp_path / "gh-argv"), "FAKE_OWNER": owner}
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


def test_review_threads_resolve_checks_the_thread_is_on_the_pr_then_resolves(tmp_path):
    r, argv = _threads(tmp_path, "resolve", "2338", "PRRT_kwDOabc-12_3",
                       owner="christopherqueenconsulting/linkedin_engagement_manager 2338")
    assert r.returncode == 0, r.stderr
    queries = [a for a in argv if a.startswith("query=")]
    assert len(queries) == 2
    assert "node(id:$t)" in queries[0] and "pullRequest{number}" in queries[0] and "mutation" not in queries[0]
    assert queries[1] == "query=mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{id isResolved}}}"
    assert "MUTATION" in argv
    assert argv.count("t=PRRT_kwDOabc-12_3") == 2


@pytest.mark.parametrize(
    "owner",
    [
        "christopherqueenconsulting/linkedin_engagement_manager 2339",   # another PR's thread
        "someone/else 2338",                                             # another repo
        "christopherqueenconsulting/linkedin_engagement_manager 23380",  # prefix is not equality
        "",                                                              # unreadable
    ],
)
def test_review_threads_refuses_to_resolve_a_thread_on_another_pr(tmp_path, owner):
    """One PR's run must not clear another PR's merge gate — and unreadable is a refusal."""
    r, argv = _threads(tmp_path, "resolve", "2338", "PRRT_kwDOabc", owner=owner)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "REFUSING" in r.stderr
    assert "MUTATION" not in argv


@pytest.mark.parametrize(
    "args",
    [
        ("list", "12; rm -rf /"),
        ("list", "abc"),
        ("list", "0"),
        ("resolve", "PRRT_kwDOabc"),                       # the old one-argument form
        ("resolve", "2338", "MDEyOlB1bGxSZXF1ZXN0"),
        ("resolve", "2338", "PRRT_x -f query=mutation{deleteRef}"),
        ("resolve", "abc", "PRRT_x"),
        ("resolve", "2338", "PRRT_x", "-f", "query=mutation{x}"),
        ("list",),
        ("delete", "PRRT_x"),
        (),
    ],
)
def test_review_threads_refuses_anything_else_before_gh_runs(tmp_path, args):
    r, argv = _threads(tmp_path, *args)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert argv == [], "gh must not run on a refused argument"


def test_review_threads_does_not_claim_to_be_a_boundary():
    text = REVIEW_THREADS.read_text(encoding="utf-8")
    assert "no way to edit" not in text
    assert "guard rail, NOT a boundary" in text


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
    assert "run_lane_rc=73" in r.stdout, r.stdout + r.stderr  # EX_SETUP
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
    assert "run_lane_rc=73" in r.stdout, r.stdout + r.stderr  # EX_SETUP
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


# ---------------------------------------------------------------- a refusal never spends budget

AGENT_RUN = PIPELINE / "v2" / "actions" / "agent_run.sh"
LEDGER = PIPELINE / "lib" / "ledger.sh"

#: Stands in for v2/actions/common.sh: everything agent_run.sh needs up to the launch, with the REAL
#: ledger and run_lane libraries — the trust walk and gh calls are what is stubbed, not the budget.
STUB_COMMON = """
BASE="$TEST_BASE"; LOGDIR="$BASE/logs"; mkdir -p "$LOGDIR"; LOG="$LOGDIR/actions.log"
log() { echo "LOG: $*"; }
EX_TRUST=70; EX_BUDGET=71; EX_BUSY=72; EX_SETUP=73
DRY_RUN="${DRY_RUN:-0}"; RUNBOOK="$BASE/RUNBOOK.md"; V2_DIR="$BASE/v2"; SLUG=o/r; ASSIGNEE=o
. "$BASE/lib/ledger.sh"
. "$BASE/lib/run_lane.sh"
v2_paused() { return 1; }; v2_hold_present() { return 1; }; v2_trust_ok() { return 0; }
issue_for_pr() { :; }; claim_branch() { return 0; }
"""


def _agent_run(tmp_path: Path, ship_profile: bool, dry_run: str = "0") -> tuple[subprocess.CompletedProcess, Path]:
    fx = _fixture(tmp_path, "never printed\n", ship_profile=ship_profile)
    actions = tmp_path / "actions"
    actions.mkdir()
    (actions / "agent_run.sh").write_text(AGENT_RUN.read_text(encoding="utf-8"), encoding="utf-8")
    (actions / "common.sh").write_text(STUB_COMMON, encoding="utf-8")
    env = {**fx["env"], "TEST_BASE": str(tmp_path), "DRY_RUN": dry_run}
    r = subprocess.run(["bash", str(actions / "agent_run.sh"), "fix", "pr", "4242", "feature/claude-issue-1"],
                       capture_output=True, text=True, env=env, timeout=60)
    return r, tmp_path / "state" / "ledger" / "pr-4242.tsv"


def test_v2_profile_refusal_exits_ex_setup_without_charging_budget(tmp_path):
    r, ledger = _agent_run(tmp_path, ship_profile=False)
    assert r.returncode == 73, r.stdout + r.stderr
    assert "before charging budget" in r.stdout
    assert not ledger.exists(), "a refused dispatch must not consume a run"
    assert _argv({"base": tmp_path}) == [], "the agent must not run"


def test_v2_harness_does_charge_when_the_profile_is_present(tmp_path):
    """Guard the guard: the same harness charges the budget once the profile exists."""
    r, ledger = _agent_run(tmp_path, ship_profile=True, dry_run="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ledger.read_text(encoding="utf-8").split("\t")[:2] == ["fix", "1"]


def _v1_ledger(tmp_path: Path, ship_profile: bool, mode: str, modes: str = "") -> subprocess.CompletedProcess:
    _fixture(tmp_path, "", ship_profile=ship_profile)
    (tmp_path / "lib" / "ledger.sh").write_text(LEDGER.read_text(encoding="utf-8"), encoding="utf-8")
    script = textwrap.dedent(f"""
        BASE="{tmp_path}"; LOGDIR="$BASE/logs"; mkdir -p "$LOGDIR"
        log() {{ echo "LOG: $*"; }}
        . "$BASE/lib/ledger.sh"; . "$BASE/lib/run_lane.sh"
        guard_ledger_charge_with_profile
        guard_ledger_charge_with_profile   # idempotent: a second call must not wrap the wrapper
        echo "attempt=$(ledger_charge pr 77 {mode})"
        echo "count=$(ledger_count pr 77 {mode})"
    """)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LEM_PERMISSION_PROFILE_MODES": modes}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)


def test_v1_charge_is_skipped_for_a_mode_run_lane_would_refuse(tmp_path):
    r = _v1_ledger(tmp_path, ship_profile=False, mode="depfix")
    assert "attempt=0" in r.stdout and "count=0" in r.stdout, r.stdout + r.stderr
    assert "not charging" in r.stderr


def test_v1_charge_still_counts_when_the_profile_is_present(tmp_path):
    r = _v1_ledger(tmp_path, ship_profile=True, mode="depfix")
    assert "attempt=1" in r.stdout and "count=1" in r.stdout, r.stdout + r.stderr


def test_v1_non_agent_ledgers_pass_straight_through(tmp_path):
    """merge/disarm ledgers have nothing to do with the profile."""
    r = _v1_ledger(tmp_path, ship_profile=False, mode="merge")
    assert "attempt=1" in r.stdout and "count=1" in r.stdout, r.stdout + r.stderr


def test_v1_mode_excluded_by_the_restriction_still_charges(tmp_path):
    """A MODE the restriction leaves on the old flag WILL run, so it is charged like before."""
    r = _v1_ledger(tmp_path, ship_profile=False, mode="depfix", modes="selfreview")
    assert "attempt=1" in r.stdout, r.stdout + r.stderr


def test_tick_sh_installs_the_v1_guard_after_the_ledger_fallback():
    tick = (PIPELINE / "tick.sh").read_text(encoding="utf-8")
    assert tick.index("ledger_charge() { echo 1; }") < tick.index("guard_ledger_charge_with_profile;")


# ---------------------------------------------------------------- status.sh surfaces denials

STATUS = PIPELINE / "status.sh"


def _status_text(tmp_path: Path, rows: list[dict], config: str = "", ship_profile: bool = True) -> str:
    for sub in ("state", "logs", "locks"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.env").write_text(config, encoding="utf-8")
    if ship_profile:
        (tmp_path / "config").mkdir(exist_ok=True)
        shutil.copy(PROFILE, tmp_path / "config" / PROFILE.name)
    (tmp_path / "logs" / "denials.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "BASE": str(tmp_path), "REPO": str(tmp_path),
           "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"), "NO_COLOR": "1",
           "OUTCOMES": str(tmp_path / "logs" / "tick-outcomes.ndjson")}
    r = subprocess.run(["bash", str(STATUS), "--no-gh"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _ts(seconds_ago: int) -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def _attention(text: str) -> str:
    return text[text.index("NEEDS ATTENTION"):] if "NEEDS ATTENTION" in text else ""


def test_status_raises_recent_denials_by_mode(tmp_path):
    text = _status_text(tmp_path, [
        {"ts": _ts(60), "mode": "rebase", "denial_count": 2},
        {"ts": _ts(3600), "mode": "review", "denial_count": 1},
        {"ts": _ts(120), "mode": "fix", "denial_count": 0},
        {"ts": _ts(3 * 86400), "mode": "start", "denial_count": 9},     # outside the window
    ])
    assert "denials (24h): 3, by mode: rebase=2 review=1" in text
    assert "permission denials (24h): 3" in _attention(text)


def test_status_reports_zero_denials_without_raising_them(tmp_path):
    text = _status_text(tmp_path, [{"ts": _ts(60), "mode": "fix", "denial_count": 0}])
    assert "denials (24h): 0" in text
    assert "permission profile: on" in text
    assert "permission" not in _attention(text)


def test_status_counts_unparsed_runs_as_unknown_not_zero(tmp_path):
    text = _status_text(tmp_path, [
        {"ts": _ts(30), "mode": "docfix", "parsed": False, "denial_count": None},
        {"ts": _ts(40), "mode": "fix", "parsed": False, "denial_count": None},
        {"ts": _ts(3 * 86400), "mode": "fix", "parsed": False, "denial_count": None},  # outside
    ])
    assert "denials (24h): 0 (+2 unparsed runs: denials unknown)" in text
    assert "unparseable output (24h): 2" in _attention(text)


@pytest.mark.parametrize(
    ("config", "ship", "line", "raised"),
    [
        ("", True, "permission profile: on", False),
        ("LEM_PERMISSION_PROFILE_MODES=selfreview,review\n", True,
         "permission profile: restricted(selfreview,review)", False),
        ("LEM_PERMISSION_PROFILE=off\n", True, "permission profile: off", True),
        ("", False, "permission profile: MISSING", True),
    ],
)
def test_status_prints_the_permission_posture(tmp_path, config, ship, line, raised):
    text = _status_text(tmp_path, [], config=config, ship_profile=ship)
    assert line in text
    assert ("permission profile" in _attention(text)) is raised


# ---------------------------------------------------------------- gh_safe.sh

REPO_SLUG = "christopherqueenconsulting/linkedin_engagement_manager"

FAKE_GH_SAFE = """#!/bin/sh
printf '%s\\037' "$@" >> "$GH_LOG"; echo >> "$GH_LOG"
prev=""
for a in "$@"; do [ "$prev" = "--body-file" ] && cat "$a" >> "$GH_BODY"; prev="$a"; done
case "$1 $2" in
  "pr view") echo "$FAKE_HEAD" ;;
esac
"""


def _gh_safe(tmp_path: Path, *args: str, shell: str | None = None, env_extra: dict | None = None,
             cwd: Path | None = None) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    binf = tmp_path / "ghbin"
    binf.mkdir(exist_ok=True)
    gh = binf / "gh"
    gh.write_text(FAKE_GH_SAFE, encoding="utf-8")
    gh.chmod(0o755)
    wt = cwd or tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    env = {"PATH": f"{binf}:/usr/bin:/bin", "GH_LOG": str(tmp_path / "gh-log"), "GH_BODY": str(tmp_path / "gh-body"),
           "TMPDIR": str(tmp_path),
           "ISSUE": "41", "PR": "9", "BRANCH": "feature/claude-issue-41", "FAKE_HEAD": "",
           **(env_extra or {})}
    cmd = ["bash", "-c", shell.replace("HELPER", str(GH_SAFE))] if shell else ["bash", str(GH_SAFE), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30, cwd=wt)
    log = tmp_path / "gh-log"
    calls = [line.split("\x1f")[:-1] for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return r, calls


def _writes(calls: list[list[str]]) -> list[list[str]]:
    """The write calls, with the helper's private body-file copy path replaced by `<copy>`."""
    out = []
    for c in calls:
        if c[:2] == ["pr", "view"]:
            continue
        out.append(["<copy>" if i and c[i - 1] == "--body-file" else a for i, a in enumerate(c)])
    return out


def _bodies(tmp_path: Path) -> str:
    f = tmp_path / "gh-body"
    return f.read_text(encoding="utf-8") if f.exists() else ""


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("issue-edit", "41", "--add-label", "needs-human", "--add-assignee", "gitchrisqueen",
          "--remove-label", "agent:ready"),
         ["issue", "edit", "41", "--repo", REPO_SLUG, "--add-label", "needs-human",
          "--add-assignee", "gitchrisqueen", "--remove-label", "agent:ready"]),
        (("issue-edit", "41", "--add-label", "agent:blocked", "--remove-label", "agent:working"),
         ["issue", "edit", "41", "--repo", REPO_SLUG, "--add-label", "agent:blocked",
          "--remove-label", "agent:working"]),
        (("pr-edit", "9", "--add-label", "agent:working", "--remove-label", "agent:revise",
          "--remove-label", "needs-human", "--remove-label", "agent:blocked"),
         ["pr", "edit", "9", "--repo", REPO_SLUG, "--add-label", "agent:working", "--remove-label",
          "agent:revise", "--remove-label", "needs-human", "--remove-label", "agent:blocked"]),
        (("pr-edit", "9", "--add-label", "needs-human", "--add-label", "agent:blocked",
          "--remove-label", "agent:phasefix", "--add-assignee", "gitchrisqueen"),
         ["pr", "edit", "9", "--repo", REPO_SLUG, "--add-label", "needs-human", "--add-label",
          "agent:blocked", "--remove-label", "agent:phasefix", "--add-assignee", "gitchrisqueen"]),
        (("pr-edit", "9", "--add-label", "needs-human", "--add-assignee", "gitchrisqueen",
          "--remove-label", "agent:depfix"),
         ["pr", "edit", "9", "--repo", REPO_SLUG, "--add-label", "needs-human",
          "--add-assignee", "gitchrisqueen", "--remove-label", "agent:depfix"]),
        (("pr-edit", "9", "--add-label=Needs-Human,priority: high"),
         ["pr", "edit", "9", "--repo", REPO_SLUG, "--add-label", "needs-human",
          "--add-label", "priority: high"]),
    ],
)
def test_gh_safe_runs_every_runbook_label_edit(tmp_path, args, expected):
    r, calls = _gh_safe(tmp_path, *args)
    assert r.returncode == 0, r.stderr
    assert _writes(calls) == [expected]


def test_gh_safe_labels_the_pr_mode_start_just_opened(tmp_path):
    """MODE=start has no $PR yet: the PR on its own branch is a valid target."""
    r, calls = _gh_safe(tmp_path, "pr-edit", "120", "--add-label", "agent:working",
                        env_extra={"PR": "", "FAKE_HEAD": "feature/claude-issue-41 false"})
    assert r.returncode == 0, r.stderr
    assert _writes(calls) == [["pr", "edit", "120", "--repo", REPO_SLUG, "--add-label", "agent:working"]]


@pytest.mark.parametrize(
    ("args", "env_extra"),
    [
        (("issue-edit", "42", "--remove-label", "needs-human"), {}),          # another issue's hold
        (("pr-edit", "10", "--remove-label", "needs-human"), {"FAKE_HEAD": "feature/someone-else false"}),
        (("pr-edit", "10", "--remove-label", "needs-human"), {"FAKE_HEAD": ""}),  # unreadable head
        # a fork PR can carry the same head branch name: same-repo only
        (("pr-edit", "10", "--remove-label", "needs-human"), {"FAKE_HEAD": "feature/claude-issue-41 true"}),
        (("pr-comment", "10", "--body", "x"), {"FAKE_HEAD": "feature/claude-issue-41 true"}),
        (("issue-comment", "42", "--body", "x"), {}),
        (("pr-ready", "10", "--undo"), {"FAKE_HEAD": "feature/someone-else false"}),
        (("issue-edit", "41", "--add-label", "x"), {"ISSUE": "", "PR": "", "BRANCH": ""}),
    ],
)
def test_gh_safe_only_edits_this_runs_item(tmp_path, args, env_extra):
    r, calls = _gh_safe(tmp_path, *args, env_extra=env_extra)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "not this run's item" in r.stderr
    assert _writes(calls) == []


@pytest.mark.parametrize(
    "args",
    [
        ("issue-edit", "41", "--add-label", "agent:ready"),
        ("issue-edit", "41", "--add-label", "Agent:Ready"),
        ("issue-edit", "41", "--add-label", "AGENT:READY"),
        ("issue-edit", "41", "--add-label", " agent:ready "),
        ("issue-edit", "41", "--add-label=agent:ready"),
        ("issue-edit", "41", "--add-label", "bug,agent:ready"),
        ("issue-edit", "41", "--add-label", "bug, Agent:Ready"),
        ("issue-edit", "41", "--add-label", "agent:ready​"),           # zero-width tail
        ("issue-edit", "41", "--add-label", "аgent:ready"),            # Cyrillic a
        ("pr-edit", "9", "--add-label", "release:now"),
        ("pr-edit", "9", "--add-label", "Release:Now"),
        ("pr-edit", "9", "--add-label", "agent:revise"),
        ("pr-edit", "9", "--add-label", "agent:depfix"),
        ("pr-edit", "9", "--add-label", "AGENT:DOCFIX"),
        ("pr-edit", "9", "--add-label", "agent:phasefix"),
        ("pr-edit", "9", "--add-label", "needs-human\n"),                  # newline in a label
        ("pr-edit", "9", "--add-label", "x\nagent:ready"),
        ("pr-edit", "9", "--body-file", "/etc/passwd"),
        ("pr-edit", "9", "--body-file", "tmp/link.md"),
        ("pr-create", "--title", "x", "--body-file", "tmp/f.md", "--label", "release:now"),
        ("pr-create", "--title", "x", "--body-file", "tmp/f.md", "--base", "release"),
        ("pr-create", "--title", "x", "--body-file", "tmp/f.md", "--head", "main"),
        ("pr-create", "--title", "x", "--body-file", "tmp/f.md", "--head", "feature/other"),
        ("pr-create", "--title", "x", "--body-file", "tmp/f.md", "--repo", "a/b"),
        ("pr-create", "--title", "x", "--body-file", "tmp/link.md"),
        ("pr-comment", "9", "--body", "two\nlines"),
        ("pr-comment", "9", "--body-file", "tmp/link.md"),
        ("pr-comment", "9", "--body-file", "../x"),
        ("pr-comment", "9", "--body", "x", "--repo", "a/b"),
        ("pr-comment", "9", "-R", "a/b"),
        ("issue-comment", "41", "--body-file", "/home/lem/agent-pipeline/config.env"),
        ("pr-ready", "9", "--repo"),
        ("pr-edit", "9", "--body", "x"),
        ("pr-edit", "9", "--title", "x"),
        ("pr-edit", "9", "--repo", "someone/else"),
        ("pr-edit", "9", "-R", "someone/else"),
        ("pr-edit", "9", "--add-assignee", "x;y"),
        ("pr-edit", "nine", "--add-label", "x"),
        ("pr-edit", "9"),
        ("issue-create", "--title", "x", "--body-file", "tmp/f.md", "--label", "AGENT:READY"),
        ("issue-create", "--title", "x", "--body-file", "tmp/f.md", "-l", "agent:ready"),
        ("issue-create", "--title", "x", "--body-file", "tmp/f.md", "--label=bug,release:now"),
        ("issue-create", "--title", "x", "--body-file", "tmp/f.md", "--repo", "a/b"),
        ("issue-create", "--title", "x", "--body", "inline"),
        ("issue-create", "--title", "x", "--body-file", "/etc/passwd"),
        ("issue-create", "--title", "x", "--body-file", "../config.env"),
        ("issue-create", "--title", "x", "--body-file", "tmp/../x.md"),
        ("issue-create", "--title", "x", "--body-file", "tmp/missing.md"),
        ("issue-create", "--title", "x", "--body-file", "tmp/link.md"),            # symlink out
        ("issue-create", "--title", "", "--body-file", "tmp/f.md"),
        ("issue-create", "--title", "a\nb", "--body-file", "tmp/f.md"),
        ("delete", "41"),
        (),
    ],
)
def test_gh_safe_refuses_evasions_before_any_write(tmp_path, args):
    wt = tmp_path / "wt"
    (wt / "tmp").mkdir(parents=True)
    (wt / "tmp" / "f.md").write_text("body\n", encoding="utf-8")
    secret = tmp_path / "config.env"
    secret.write_text("TOKEN=x\n", encoding="utf-8")
    (wt / "tmp" / "link.md").symlink_to(secret)
    r, calls = _gh_safe(tmp_path, *args, cwd=wt)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert _writes(calls) == [], "gh must not write on a refused argument"


@pytest.mark.parametrize(
    "shell",
    [
        "HELPER issue-edit 41 --add-label agent\\:ready",
        "HELPER issue-edit 41 --add-label agent:read''y",
        "HELPER issue-edit 41 --add-label \"Agent:\"Ready",
        "HELPER issue-create --title x --body-file tmp/f.md -l AGENT:READY",
        "HELPER pr-edit 9 --add-label 'release'':now'",
    ],
)
def test_gh_safe_sees_the_shell_expanded_label(tmp_path, shell):
    """The spellings a glob cannot catch arrive here already expanded — and are refused."""
    wt = tmp_path / "wt"
    (wt / "tmp").mkdir(parents=True)
    (wt / "tmp" / "f.md").write_text("body\n", encoding="utf-8")
    r, calls = _gh_safe(tmp_path, shell=shell, cwd=wt)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "REFUSING to add" in r.stderr
    assert _writes(calls) == []


def test_gh_safe_creates_an_issue_from_a_tmp_body(tmp_path):
    wt = tmp_path / "wt"
    (wt / "tmp").mkdir(parents=True)
    (wt / "tmp" / "followup.md").write_text("## Why\n", encoding="utf-8")
    r, calls = _gh_safe(tmp_path, "issue-create", "--title", "x — Phase 2 (follow-up of #41)",
                        "--body-file", "tmp/followup.md", "--label", "enhancement,priority:medium", cwd=wt)
    assert r.returncode == 0, r.stderr
    assert _writes(calls) == [["issue", "create", "--repo", REPO_SLUG, "--title", "x — Phase 2 (follow-up of #41)",
                               "--body-file", "<copy>", "--label", "enhancement",
                               "--label", "priority:medium"]]
    assert _bodies(tmp_path) == "## Why\n", "gh reads a private COPY of the checked file"


def _wt_with_body(tmp_path: Path, name: str = "b.md", text: str = "body\n") -> Path:
    wt = tmp_path / "wt"
    (wt / "tmp").mkdir(parents=True, exist_ok=True)
    (wt / "tmp" / name).write_text(text, encoding="utf-8")
    return wt


def test_gh_safe_opens_the_run_branch_pr_against_main(tmp_path):
    wt = _wt_with_body(tmp_path, "pr-body.md", "Closes #41\n")
    subprocess.run(["git", "init", "-q", "-b", "feature/claude-issue-41", str(wt)], check=True,
                   env={**_GIT_ENV, "HOME": str(tmp_path)})
    r, calls = _gh_safe(tmp_path, "pr-create", "--title", "feat: x (closes #41)", "--body-file", "tmp/pr-body.md",
                        "--label", "agent:working", cwd=wt)
    assert r.returncode == 0, r.stderr
    assert _writes(calls) == [["pr", "create", "--repo", REPO_SLUG, "--base", "main", "--head",
                               "feature/claude-issue-41", "--title", "feat: x (closes #41)",
                               "--body-file", "<copy>", "--label", "agent:working"]]
    assert _bodies(tmp_path) == "Closes #41\n"


def test_gh_safe_lets_depfix_open_a_fix_pr(tmp_path):
    wt = _wt_with_body(tmp_path)
    r, calls = _gh_safe(tmp_path, "pr-create", "--title", "fix: x", "--body-file", "tmp/b.md",
                        "--head", "fix/pexels-401", "--draft", cwd=wt,
                        env_extra={"MODE": "depfix", "BRANCH": "dependabot/pip/x"})
    assert r.returncode == 0, r.stderr
    assert _writes(calls)[0][:7] == ["pr", "create", "--repo", REPO_SLUG, "--base", "main", "--head"]
    assert "--draft" in _writes(calls)[0]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("pr-comment", "9", "--body", "@dependabot rebase"),
         ["pr", "comment", "9", "--repo", REPO_SLUG, "--body", "@dependabot rebase"]),
        (("pr-comment", "9", "--body-file", "tmp/b.md"),
         ["pr", "comment", "9", "--repo", REPO_SLUG, "--body-file", "<copy>"]),
        (("issue-comment", "41", "--body-file", "tmp/b.md"),
         ["issue", "comment", "41", "--repo", REPO_SLUG, "--body-file", "<copy>"]),
        (("pr-ready", "9", "--undo"), ["pr", "ready", "9", "--repo", REPO_SLUG, "--undo"]),
        (("pr-ready", "9"), ["pr", "ready", "9", "--repo", REPO_SLUG]),
        (("pr-edit", "9", "--body-file", "tmp/b.md"),
         ["pr", "edit", "9", "--repo", REPO_SLUG, "--body-file", "<copy>"]),
    ],
)
def test_gh_safe_comments_and_drafts_on_this_runs_item(tmp_path, args, expected):
    wt = _wt_with_body(tmp_path)
    r, calls = _gh_safe(tmp_path, *args, cwd=wt)
    assert r.returncode == 0, r.stderr
    assert _writes(calls) == [expected]


def test_gh_safe_protects_every_lane_label_v2_maps_to_a_mode():
    """observe.LANE_LABEL_MODES is the list of labels that start a lane; all must be un-addable."""
    sys.path.insert(0, str(PIPELINE / "v2"))
    from lemd import observe

    text = GH_SAFE.read_text(encoding="utf-8")
    protected = re.search(r"^PROTECTED=\(([^)]*)\)", text, re.M).group(1).split()
    assert set(observe.LANE_LABEL_MODES) <= set(protected)
    assert {"agent:ready", "release:now"} <= set(protected)


def test_the_label_helper_is_the_only_issue_write_path():
    allow = _profile()["permissions"]["allow"]
    assert f"Bash({GH_SAFE_INSTALLED} *)" in allow
    assert not [r for r in allow if r.startswith("Bash(gh issue edit") or r.startswith("Bash(gh issue create")]
    assert "no way to edit" not in GH_SAFE.read_text(encoding="utf-8")


# ---------------------------------------------------------------- git_push.sh (real git, local bare remote)

_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"}


def _push_repo(tmp_path: Path, branch: str, remote_has: str = "") -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    wt = tmp_path / "wt"
    env = {**_GIT_ENV, "HOME": str(tmp_path)}
    ident = ["-c", "user.email=a@b", "-c", "user.name=a"]

    def run(*args: str, cwd: Path | None = None) -> None:
        subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True)

    run("git", "init", "-q", "--bare", "-b", "main", str(remote))
    run("git", "init", "-q", "-b", "main", str(wt))
    run("git", *ident, "commit", "-q", "--allow-empty", "-m", "base", cwd=wt)
    run("git", "remote", "add", "origin", str(remote), cwd=wt)
    run("git", "push", "-q", "origin", "main", cwd=wt)
    if remote_has:  # a branch someone else already pushed
        run("git", "push", "-q", "origin", f"main:refs/heads/{remote_has}", cwd=wt)
    if branch != "main":
        run("git", "checkout", "-q", "-b", branch, cwd=wt)
    run("git", *ident, "commit", "-q", "--allow-empty", "-m", "work", cwd=wt)
    return remote, wt


def _git_push(tmp_path: Path, branch: str, *args: str, env: dict,
              remote_has: str = "") -> tuple[subprocess.CompletedProcess, dict]:
    remote, wt = _push_repo(tmp_path, branch, remote_has)
    full = {**_GIT_ENV, "HOME": str(tmp_path), **env}
    before = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=remote, env=full,
                            capture_output=True, text=True).stdout.strip()
    r = subprocess.run(["bash", str(GIT_PUSH), *args], cwd=wt, env=full, capture_output=True, text=True, timeout=60)
    out = subprocess.run(["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"],
                         cwd=remote, env=full, capture_output=True, text=True).stdout.split("\n")
    heads = dict(line.split() for line in out if line.strip())
    heads["_main_moved"] = heads.get("main") != before
    return r, heads


@pytest.mark.parametrize(("args", "mode"), [((), "fix"), (("--force-with-lease",), "rebase")])
def test_git_push_pushes_only_the_run_branch_to_itself(tmp_path, args, mode):
    r, heads = _git_push(tmp_path, "feature/claude-issue-41", *args,
                         env={"BRANCH": "feature/claude-issue-41", "MODE": mode})
    assert r.returncode == 0, r.stderr
    assert set(heads) == {"main", "feature/claude-issue-41", "_main_moved"}
    assert heads["_main_moved"] is False


def test_git_push_lets_depfix_push_its_new_fix_branch(tmp_path):
    r, heads = _git_push(tmp_path, "fix/pexels-401", env={"BRANCH": "dependabot/pip/x", "MODE": "depfix"})
    assert r.returncode == 0, r.stderr
    assert "fix/pexels-401" in heads


@pytest.mark.parametrize(
    ("branch", "args", "env"),
    [
        ("main", (), {"BRANCH": "main", "MODE": "fix"}),                         # never main
        ("feature/other", (), {"BRANCH": "feature/claude-issue-41", "MODE": "fix"}),
        ("fix/x", (), {"BRANCH": "feature/claude-issue-41", "MODE": "fix"}),      # fix/* only in depfix
        ("feature/claude-issue-41", (), {"MODE": "fix"}),                         # no BRANCH
        ("feature/claude-issue-41", ("origin", "HEAD:main"), {"BRANCH": "feature/claude-issue-41"}),
        ("feature/claude-issue-41", ("--force",), {"BRANCH": "feature/claude-issue-41"}),
        ("feature/claude-issue-41", ("--force-with-lease", "origin"), {"BRANCH": "feature/claude-issue-41"}),
        ("feature/claude-issue-41", ("--mirror",), {"BRANCH": "feature/claude-issue-41"}),
        # a lease rewrites history: MODE=rebase only
        ("feature/claude-issue-41", ("--force-with-lease",), {"BRANCH": "feature/claude-issue-41", "MODE": "fix"}),
        ("feature/claude-issue-41", ("--force-with-lease",), {"BRANCH": "feature/claude-issue-41"}),
        ("fix/new", ("--force-with-lease",), {"BRANCH": "dependabot/pip/x", "MODE": "depfix"}),
    ],
)
def test_git_push_refuses_everything_else(tmp_path, branch, args, env):
    r, heads = _git_push(tmp_path, branch, *args, env=env)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert set(heads) == {"main", "_main_moved"} and heads["_main_moved"] is False, "nothing may reach the remote"


@pytest.mark.parametrize("args", [(), ("--force-with-lease",)])
def test_git_push_depfix_never_touches_an_existing_fix_branch(tmp_path, args):
    """MODE=depfix may open a NEW fix/* branch; one already on origin belongs to someone else."""
    r, heads = _git_push(tmp_path, "fix/x", *args, env={"BRANCH": "dependabot/pip/x", "MODE": "depfix"},
                         remote_has="fix/x")
    assert r.returncode == 2, (r.stdout, r.stderr)
    remote_before = heads["main"]                 # fix/x was created at main's commit
    assert heads["fix/x"] == remote_before, "the existing ref must be unchanged"
    assert heads["_main_moved"] is False


# ---------------------------------------------------------------- git_commit.sh (real git)


def _commit_repo(tmp_path: Path) -> tuple[Path, dict]:
    wt = tmp_path / "wt"
    env = {**_GIT_ENV, "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}
    subprocess.run(["git", "init", "-q", "-b", "feature/x", str(wt)], check=True, env=env, capture_output=True)
    (wt / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=wt, check=True, env=env, capture_output=True)
    (wt / "tmp").mkdir()
    return wt, env


def _commit(tmp_path: Path, *args: str, setup=None) -> tuple[subprocess.CompletedProcess, str]:
    wt, env = _commit_repo(tmp_path)
    if setup:
        setup(wt)
    r = subprocess.run(["bash", str(GIT_COMMIT), *args], cwd=wt, env=env, capture_output=True, text=True, timeout=60)
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=wt, env=env, capture_output=True, text=True)
    return r, (log.stdout if log.returncode == 0 else "")


def test_git_commit_takes_messages(tmp_path):
    r, msg = _commit(tmp_path, "-m", "feat: x", "-m", "Co-Authored-By: Claude <noreply@anthropic.com>")
    assert r.returncode == 0, r.stderr
    assert msg.startswith("feat: x\n\nCo-Authored-By: Claude")


def test_git_commit_takes_a_tmp_message_file(tmp_path):
    r, msg = _commit(tmp_path, "-F", "tmp/msg.txt",
                     setup=lambda wt: (wt / "tmp" / "msg.txt").write_text("fix: y\n\nbody\n", encoding="utf-8"))
    assert r.returncode == 0, r.stderr
    assert msg.startswith("fix: y\n\nbody")


def _secret_link(wt: Path) -> None:
    secret = wt.parent / "config.env"
    secret.write_text("TOKEN=s3cret\n", encoding="utf-8")
    (wt / "tmp" / "link.txt").symlink_to(secret)


@pytest.mark.parametrize(
    "args",
    [
        ("-F", "/etc/passwd"),
        ("-F", "../config.env"),
        ("-F", "tmp/../../config.env"),
        ("-F", "tmp/link.txt"),                      # a symlink out of tmp/
        ("-F", "tmp/missing.txt"),
        ("--file=/etc/passwd",),
        ("--fil", "/etc/passwd"),
        ("-aF/etc/passwd",),
        ("--templ=/etc/passwd", "-m", "x"),
        ("--pathspec-fr=/etc/passwd", "-m", "x"),
        ("-m", "x", "--amend"),
        ("-m", "x", "--no-verify"),
        ("-m", "x", "-F", "tmp/link.txt"),
        ("-m", ""),
        ("-m",),
        (),
    ],
)
def test_git_commit_refuses_every_other_shape(tmp_path, args):
    r, msg = _commit(tmp_path, *args, setup=_secret_link)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert msg == "", "nothing may be committed"
    assert "s3cret" not in r.stdout + r.stderr
