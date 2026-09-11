"""Tests for scripts/claude_worktree_cleanup.sh — the opt-in sweep of `.claude/worktrees/` (#2041).

Same shape as ``test_worktree_cleanup.py``: a throwaway repo under ``tmp_path`` with one worktree
per decision, and assertions on the script's own report lines, because those are all an operator
ever sees. What differs is the EVIDENCE: this sweep asks GitHub for the branch's PR state, so ``gh``
is a stub on ``PATH`` that answers from per-branch fixture files — no network, no real repo.

The removal rule is a conjunction (clean AND merged PR AND untouched for 48h), so every test but
one holds a tree for exactly one missing conjunct. The one that removes proves the branch ref
survives.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "claude_worktree_cleanup.sh"
_OLD = "2020-01-01T00:00:00 +0000"
_REPORT = re.compile(r"^(SKIP|HELD|WOULD REMOVE|WOULD PRUNE|REMOVED|FAILED)\b")

_needs_proc = pytest.mark.skipif(
    sys.platform != "linux", reason="the live-process hold reads /proc/*/cwd"
)

Swept = tuple[dict[str, Path], str]

#: The stub answers `gh pr list --repo R --head BRANCH ... --jq '.[].state'` from
#: `$GH_STUB_DIR/<branch with / as _>`; a missing file is "no PR". `$GH_STUB_DIR/FAIL` makes every
#: call fail, which is how the unreadable path is exercised.
_GH_STUB = r'''#!/usr/bin/env bash
echo "$*" >> "$GH_STUB_DIR/calls"
[ -e "$GH_STUB_DIR/FAIL" ] && exit 1
head=""
while [ $# -gt 0 ]; do
  case "$1" in --head) shift; head="$1" ;; esac
  shift
done
f="$GH_STUB_DIR/$(printf '%s' "$head" | tr '/' '_')"
[ -f "$f" ] && cat "$f"
exit 0
'''


def _git_env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "Sweep Test", "GIT_AUTHOR_EMAIL": "sweep@example.com",
        "GIT_COMMITTER_NAME": "Sweep Test", "GIT_COMMITTER_EMAIL": "sweep@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    })
    env.update(overrides)
    return env


def _git(cwd: Path, *args: str, when: str | None = None) -> str:
    env = _git_env()
    if when is not None:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo: Path, name: str, when: str | None = None) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _age(path: Path, seconds: int) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _build_repo(root: Path) -> dict[str, Path]:
    """A main checkout with `.claude/worktrees/` holding one tree per decision.

    Every tree starts from the 2020-dated seed commit and is aged three days, so the 48h grace
    holds exactly the one tree the test touches. Fixture files under ``gh/`` script GitHub's
    answer per branch.
    """
    work = root / "work"
    gh_dir = root / "gh"
    stub_bin = root / "bin"
    work.mkdir()
    gh_dir.mkdir()
    stub_bin.mkdir()
    (stub_bin / "gh").write_text(_GH_STUB, encoding="utf-8")
    (stub_bin / "gh").chmod(0o755)

    _git(work, "init", "-b", "main", ".")
    _git(work, "remote", "add", "origin", "git@github.com:acme/widget.git")
    _commit(work, "seed", _OLD)
    trees = work / ".claude" / "worktrees"
    trees.mkdir(parents=True)

    paths: dict[str, Path] = {"work": work, "gh": gh_dir, "bin": stub_bin}

    def tree(name: str, branch: str, *, states: str | None = None, detached: bool = False) -> Path:
        path = trees / name
        if detached:
            _git(work, "worktree", "add", "--detach", str(path), "main")
        else:
            _git(work, "worktree", "add", "-b", branch, str(path), "main")
            _commit(path, f"{name}-work", _OLD)   # squash-merged or not: never an ancestor of main
        if states is not None:
            (gh_dir / branch.replace("/", "_")).write_text(states, encoding="utf-8")
        _age(path, 3 * 86400)
        paths[name] = path
        return path

    tree("merged_clean", "feature/claude-issue-1", states="MERGED\n")           # removable
    dirty = tree("merged_dirty", "feature/claude-issue-2", states="MERGED\n")   # HELD uncommitted
    (dirty / "scratch.txt").write_text("unsaved work", encoding="utf-8")
    _age(dirty, 3 * 86400)
    tree("open_pr", "feature/claude-issue-3", states="MERGED\nOPEN\n")          # HELD open-pr
    tree("no_pr", "feature/claude-issue-4")                                     # HELD no-merged-pr
    tree("closed_only", "feature/claude-issue-5", states="CLOSED\n")            # HELD no-merged-pr
    fresh = tree("fresh", "feature/claude-issue-6", states="MERGED\n")          # HELD within grace
    _age(fresh, 60)
    tree("detached", "-", detached=True)                                        # HELD detached
    locked = tree("locked", "feature/claude-issue-7", states="MERGED\n")        # HELD locked
    _git(work, "worktree", "lock", str(locked))
    pruned = tree("pruned", "feature/claude-issue-8", states="MERGED\n")        # WOULD PRUNE
    shutil.rmtree(pruned)

    # A merged, clean, old tree that is NOT under .claude/worktrees/: not this sweep's business.
    outside = root / "elsewhere"
    _git(work, "worktree", "add", "-b", "feature/claude-issue-9", str(outside), "main")
    (gh_dir / "feature_claude-issue-9").write_text("MERGED\n", encoding="utf-8")
    _age(outside, 3 * 86400)
    paths["outside"] = outside
    return paths


def _sweep(paths: dict[str, Path], *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    overrides = {"PATH": f"{paths['bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                 "GH_STUB_DIR": str(paths["gh"])}
    overrides.update(env)   # an explicit PATH (the no-gh test) replaces the stub-first default
    run_env = _git_env(**overrides)
    return subprocess.run(["bash", str(_SCRIPT), *args], cwd=paths["work"], env=run_env,
                          capture_output=True, text=True)


def _line_for(output: str, path: Path) -> str:
    matches = [ln for ln in output.splitlines() if _REPORT.match(ln) and str(path) in ln]
    assert len(matches) == 1, f"{len(matches)} report lines for {path} in:\n{output}"
    return matches[0]


def _held_as(line: str) -> str:
    match = re.match(r"^HELD\s+(\S+(?:\s\S+)?)\s+/", line)
    assert match, line
    return match.group(1)


def _summary(output: str) -> str:
    lines = [ln for ln in output.splitlines() if "claude-worktree sweep:" in ln]
    assert len(lines) == 1, output
    return lines[0]


@pytest.fixture(scope="module")
def swept(tmp_path_factory: pytest.TempPathFactory) -> Swept:
    root = tmp_path_factory.mktemp("claude-sweep").resolve()
    paths = _build_repo(root)
    result = _sweep(paths)
    assert result.returncode == 0, result.stderr
    return paths, result.stdout + result.stderr


class TestClassification:
    def test_clean_merged_and_old_is_the_only_removable_shape(self, swept: Swept) -> None:
        paths, out = swept
        assert _line_for(out, paths["merged_clean"]).startswith("WOULD REMOVE")

    def test_a_merged_pr_does_not_excuse_uncommitted_work(self, swept: Swept) -> None:
        # The tree has ONLY an untracked file — the case a `git diff` check would miss.
        paths, out = swept
        assert _held_as(_line_for(out, paths["merged_dirty"])) == "uncommitted"

    def test_an_open_pr_holds_even_when_an_older_pr_merged(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["open_pr"])) == "open-pr"

    def test_unpushed_commits_with_no_pr_are_held(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["no_pr"])) == "no-merged-pr"

    def test_a_closed_unmerged_pr_is_not_a_merged_one(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["closed_only"])) == "no-merged-pr"

    def test_a_recently_touched_tree_is_held(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["fresh"])) == "within grace"

    def test_a_detached_head_has_no_branch_to_ask_about(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["detached"])) == "detached"

    def test_a_locked_tree_is_held(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["locked"])) == "locked"

    def test_a_registration_whose_directory_is_gone_is_prunable(self, swept: Swept) -> None:
        paths, out = swept
        assert _line_for(out, paths["pruned"]).startswith("WOULD PRUNE")

    def test_trees_outside_claude_worktrees_are_not_examined(self, swept: Swept) -> None:
        # Not even reported per line: the main checkout and the pipeline's `work/` trees belong to
        # other sweeps. The count is in the summary so silence never reads as "swept everything".
        paths, out = swept
        assert not [ln for ln in out.splitlines() if _REPORT.match(ln) and str(paths["outside"]) in ln]
        assert not [ln for ln in out.splitlines() if _REPORT.match(ln) and str(paths["work"]) in ln
                    and "/.claude/worktrees/" not in ln]
        assert "outside-scope=2" in _summary(out)

    def test_the_slug_is_parsed_from_origin_and_asked_per_branch(self, swept: Swept) -> None:
        paths, _ = swept
        calls = (paths["gh"] / "calls").read_text(encoding="utf-8")
        assert "--repo acme/widget" in calls
        assert "--head feature/claude-issue-1" in calls
        assert "--state all" in calls, "open AND merged must both be visible in one answer"

    def test_summary_names_every_bucket(self, swept: Swept) -> None:
        _, out = swept
        summary = _summary(out)
        for token in ("would-remove=1", "prunable=1", "uncommitted=1", "open-pr=1",
                      "no-merged-pr=2", "grace=1", "detached=1", "locked=1",
                      "unreadable=0", "pr-unreadable=0"):
            assert token in summary, summary

    def test_the_grace_window_is_arithmetic(self, swept: Swept) -> None:
        paths, _ = swept
        result = _sweep(paths, GRACE_HOURS="0")
        assert _line_for(result.stdout, paths["fresh"]).startswith("WOULD REMOVE")


class TestDryRunIsTheDefault:
    def test_a_bare_invocation_removes_nothing(self, swept: Swept) -> None:
        paths, out = swept
        assert "WOULD REMOVE" in out
        assert not re.search(r"^REMOVED\b", out, re.MULTILINE)
        assert paths["merged_clean"].exists()

    def test_a_dry_run_does_not_prune_the_registry(self, swept: Swept) -> None:
        paths, _ = swept
        assert str(paths["pruned"]) in _git(paths["work"], "worktree", "list", "--porcelain")

    def test_help_names_the_flag_that_deletes(self) -> None:
        result = subprocess.run([str(_SCRIPT), "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--apply" in result.stdout

    def test_unknown_argument_refuses_rather_than_sweeping(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths, "--app")
        assert result.returncode == 2
        assert "unknown argument" in result.stderr


@pytest.fixture(scope="module")
def applied(tmp_path_factory: pytest.TempPathFactory) -> Swept:
    root = tmp_path_factory.mktemp("claude-sweep-apply").resolve()
    paths = _build_repo(root)
    result = _sweep(paths, "--apply")
    assert result.returncode == 0, result.stderr
    return paths, result.stdout + result.stderr


@_needs_proc
class TestApply:
    def test_only_the_removable_tree_is_removed(self, applied: Swept) -> None:
        paths, out = applied
        assert not paths["merged_clean"].exists()
        assert _line_for(out, paths["merged_clean"]).startswith("REMOVED")
        for held in ("merged_dirty", "open_pr", "no_pr", "closed_only", "fresh", "detached",
                     "locked", "outside"):
            assert paths[held].exists(), f"{held} was removed"
        assert (paths["merged_dirty"] / "scratch.txt").read_text(encoding="utf-8") == "unsaved work"

    def test_the_branch_ref_outlives_the_removal(self, applied: Swept) -> None:
        # The whole safety argument: a squash-merged branch's commits stay reachable locally.
        paths, _ = applied
        assert _git(paths["work"], "log", "-1", "--format=%s", "feature/claude-issue-1") \
            == "merged_clean-work"

    def test_the_dropped_registration_is_pruned(self, applied: Swept) -> None:
        paths, _ = applied
        assert str(paths["pruned"]) not in _git(paths["work"], "worktree", "list", "--porcelain")

    def test_the_dirty_tree_is_reported_for_a_human_and_exits_zero(self, applied: Swept) -> None:
        paths, out = applied
        assert "NEEDS A HUMAN" in out
        assert str(paths["merged_dirty"]) in out

    def test_a_second_apply_is_a_no_op(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        assert _sweep(paths, "--apply").returncode == 0
        second = _sweep(paths, "--apply")
        assert second.returncode == 0
        assert "removed=0" in _summary(second.stdout)


class TestFailClosed:
    def test_a_failed_gh_call_holds_the_tree_rather_than_reading_as_no_pr(self, tmp_path: Path) -> None:
        # "Could not ask" and "no PR" must land in different buckets: the second is a fact about the
        # branch, the first is a fact about the box, and only one of them should ever become a
        # removal once the box recovers.
        paths = _build_repo(tmp_path.resolve())
        (paths["gh"] / "FAIL").write_text("")
        result = _sweep(paths, "--apply")
        out = result.stdout + result.stderr
        assert _held_as(_line_for(out, paths["merged_clean"])) == "pr-unreadable"
        assert paths["merged_clean"].exists()
        assert "no-merged-pr=0" in _summary(out)
        assert result.returncode == 0

    def test_no_gh_on_path_disables_every_removal(self, tmp_path: Path) -> None:
        # A PATH holding every tool the script needs and NOT gh — built from symlinks, because the
        # real gh lives in /usr/bin on the agent host and a bare "/usr/bin:/bin" would find it.
        nogh = tmp_path / "nogh"
        nogh.mkdir()
        for tool in ("bash", "git", "awk", "sed", "grep", "head", "wc", "sort", "readlink",
                     "date", "stat", "tr", "dirname"):
            real = shutil.which(tool)
            assert real, tool
            (nogh / tool).symlink_to(real)
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths, "--apply", PATH=str(nogh))
        out = result.stdout + result.stderr
        assert "HOLD-ALL" in out
        assert paths["merged_clean"].exists()
        assert _held_as(_line_for(out, paths["merged_clean"])) == "pr-unreadable"

    def test_an_unresolvable_repo_disables_every_removal(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        _git(paths["work"], "remote", "set-url", "origin", "/nowhere/local.git")
        result = _sweep(paths, "--apply")
        out = result.stdout + result.stderr
        assert "HOLD-ALL" in out and "--slug" in out
        assert paths["merged_clean"].exists()

    def test_slug_can_be_given_explicitly(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        _git(paths["work"], "remote", "set-url", "origin", "/nowhere/local.git")
        result = _sweep(paths, "--slug", "acme/widget")
        assert "HOLD-ALL" not in result.stdout + result.stderr
        assert _line_for(result.stdout, paths["merged_clean"]).startswith("WOULD REMOVE")

    @_needs_proc
    def test_a_live_process_inside_a_tree_holds_it(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        proc = subprocess.Popen(["sleep", "30"], cwd=paths["merged_clean"])
        try:
            result = _sweep(paths, "--apply")
            out = result.stdout + result.stderr
            assert _held_as(_line_for(out, paths["merged_clean"])) == "active process"
            assert paths["merged_clean"].exists()
        finally:
            proc.kill()
            proc.wait()

    def test_a_blind_process_detector_disables_all_removals(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths, "--apply", PROC_ROOT=str(tmp_path / "no-procfs"))
        out = result.stdout + result.stderr
        assert "HOLD-ALL" in out and "blind" in out
        assert paths["merged_clean"].exists()
        assert result.returncode == 0


class TestSourceGuards:
    """The same ratchet the sibling carries: no executed line may destroy a ref or force a removal."""

    _FORBIDDEN = re.compile(
        r"branch\s+(-[a-zA-Z]*[dD]\b|--delete)"
        r"|update-ref\s+-d"
        r"|push\s+.*--delete"
        r"|worktree\s+remove\s+(-[a-zA-Z]*f\b|--force)"
    )

    def test_no_executed_line_destroys_a_ref_or_forces_a_removal(self) -> None:
        for raw in _SCRIPT.read_text(encoding="utf-8").splitlines():
            stripped = raw.lstrip()
            if stripped.startswith(("#", "echo")):
                continue
            assert not self._FORBIDDEN.search(raw.split("#", 1)[0]), raw

    def test_the_script_is_executable(self) -> None:
        assert os.access(_SCRIPT, os.X_OK)
        assert _SCRIPT.read_text(encoding="utf-8").startswith("#!")

    def test_nothing_schedules_it(self) -> None:
        # Opt-in means opt-in: no systemd unit under scripts/systemd may ExecStart this script.
        systemd = _SCRIPT.parent / "systemd"
        for unit in systemd.glob("*.service"):
            assert _SCRIPT.name not in unit.read_text(encoding="utf-8"), unit
