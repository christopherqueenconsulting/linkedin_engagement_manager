"""Tests for scripts/worktree_cleanup.sh — the worktree half of docs/branch-cleanup.md.

The sweep's success path deletes a directory on a weekly timer, so what has to be pinned is the
CLASSIFICATION, not the plumbing: which registrations it would remove and, far more importantly,
which it holds. Every test builds a throwaway repo with real worktrees in each state and reads the
script's own report lines, because those lines are the only thing an operator ever sees.

Three states are regression tests for hazards found while writing it:

* a **detached** worktree has no branch ref to outlive the removal, so "its branch is gone from
  origin" says nothing about it — only ancestry from origin/main does;
* **gone from origin but carrying unmerged commits** is the dominant real case, not an edge one:
  this repo squash-merges, so a merged PR's commits are never ancestors of main and
  ``delete_branch_on_merge`` drops the head branch seconds later. Such a tree IS removed, and the
  local ``refs/heads`` ref the removal leaves behind is the whole safety argument — asserted below;
* bash ``read`` collapses a run of tabs, so an empty branch field would shift every later column and
  a detached tree would read as branch ``1``. The awk emitter uses a ``-`` sentinel; a regression
  shows up here as a detached tree landing in the wrong bucket.

These live in the unit lane, matching the other ``tests/unit/scripts/`` shell tests: they touch no
network, no database and no service — a ``git init`` under ``tmp_path`` and one ``sleep`` process —
and the whole module runs in about ten seconds.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "worktree_cleanup.sh"
_SYSTEMD = Path(__file__).resolve().parents[3] / "scripts" / "systemd"

_OLD = "2020-01-01T00:00:00 +0000"

#: Report lines the operator reads; the timestamped header and the indented NEEDS-A-HUMAN block
#: are deliberately not among them.
_REPORT = re.compile(r"^(SKIP|HELD|WOULD REMOVE|WOULD PRUNE|REMOVED|FAILED)\b")

#: Protection 3 walks /proc/*/cwd, and the script self-tests against its own cwd and holds
#: EVERYTHING when that fails — so on a host without procfs there is no removal left to assert.
#: Gated on the platform rather than on a probe: on Linux a process can always read its own cwd
#: link, so a skip here would mean the removal path went untested in the lane that gates the PR.
_needs_proc = pytest.mark.skipif(
    sys.platform != "linux", reason="protection 3 reads /proc/*/cwd; the sweep is inert without it"
)

Swept = tuple[dict[str, Path], str]


def _git_env(**overrides: str) -> dict[str, str]:
    """A git env with no user/global config bleed-through from the host running the suite."""
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "Sweep Test",
        "GIT_AUTHOR_EMAIL": "sweep@example.com",
        "GIT_COMMITTER_NAME": "Sweep Test",
        "GIT_COMMITTER_EMAIL": "sweep@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    })
    env.update(overrides)
    return env


def _git(cwd: Path, *args: str, when: str | None = None) -> str:
    env = _git_env()
    if when is not None:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, when: str | None = None) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _build_repo(root: Path) -> dict[str, Path]:
    """A repo with one worktree per decision branch, plus a file:// origin to fetch from.

    Returns the worktree paths by state name. Everything is dated 2020 except ``fresh``, so the
    48h grace window separates exactly one tree instead of all of them.
    """
    origin = root / "origin.git"
    work = root / "work"
    trees = root / "trees"
    origin.mkdir()
    work.mkdir()
    trees.mkdir()

    _git(origin, "init", "--bare", "-b", "main", ".")
    _git(work, "init", "-b", "main", ".")
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "seed", _OLD)
    _git(work, "push", "-u", "origin", "main")

    paths: dict[str, Path] = {"work": work}

    # Branch gone from origin (never pushed) and already an ancestor of origin/main -> removable.
    paths["merged_gone"] = trees / "merged-gone"
    _git(work, "worktree", "add", "-b", "merged-gone", str(paths["merged_gone"]), "main")

    # Pushed, committed ahead, then deleted on origin — what a squash-merge + delete_branch_on_merge
    # leaves behind, and what most of the 261 really were. Removable; refs/heads keeps the commits.
    paths["gone_unmerged"] = trees / "gone-unmerged"
    _git(work, "worktree", "add", "-b", "gone-unmerged", str(paths["gone_unmerged"]), "main")
    _commit(paths["gone_unmerged"], "squashed-work", _OLD)
    _git(paths["gone_unmerged"], "push", "-u", "origin", "gone-unmerged")
    _git(work, "push", "origin", "--delete", "gone-unmerged")

    # Branch still on origin with an unmerged commit -> HELD, the agent may still be working.
    paths["live"] = trees / "live"
    _git(work, "worktree", "add", "-b", "live-branch", str(paths["live"]), "main")
    unmerged = _commit(paths["live"], "live-work", _OLD)
    _git(paths["live"], "push", "-u", "origin", "live-branch")

    # Otherwise removable, but carries an UNTRACKED file -> HELD, and reported for a human.
    paths["dirty"] = trees / "dirty"
    _git(work, "worktree", "add", "-b", "dirty-branch", str(paths["dirty"]), "main")
    (paths["dirty"] / "scratch.txt").write_text("unsaved work", encoding="utf-8")

    # Tip inside the 48h grace -> HELD regardless of anything else.
    paths["fresh"] = trees / "fresh"
    _git(work, "worktree", "add", "-b", "fresh-branch", str(paths["fresh"]), "main")
    _commit(paths["fresh"], "fresh-work")  # default date == now, inside the grace

    # Detached at an unmerged commit: no branch ref to outlive removal -> HELD.
    paths["detached_unmerged"] = trees / "detached-unmerged"
    _git(work, "worktree", "add", "--detach", str(paths["detached_unmerged"]), unmerged)

    # Detached at a commit that IS an ancestor of origin/main -> removable, nothing is lost.
    paths["detached_merged"] = trees / "detached-merged"
    _git(work, "worktree", "add", "--detach", str(paths["detached_merged"]), "main")

    # Explicitly locked -> HELD, the lock is a human saying "not this one".
    paths["locked"] = trees / "locked"
    _git(work, "worktree", "add", "-b", "locked-branch", str(paths["locked"]), "main")
    _git(work, "worktree", "lock", str(paths["locked"]))

    # Removable, but its path contains a SPACE: an awk field split over the porcelain output
    # would truncate it to ".../has", and the sweep would decide about a path that does not exist.
    paths["spaced"] = trees / "has space"
    _git(work, "worktree", "add", "-b", "spaced-branch", str(paths["spaced"]), "main")

    # Registered but the directory was deleted by hand -> `git worktree prune` territory.
    paths["pruned"] = trees / "pruned"
    _git(work, "worktree", "add", "-b", "pruned-branch", str(paths["pruned"]), "main")
    shutil.rmtree(paths["pruned"])

    return paths


def _sweep(work: Path, *args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=work,
        env=_git_env(**env_overrides),
        capture_output=True,
        text=True,
    )


def _line_for(output: str, path: Path) -> str:
    """The ONE report line naming this worktree — the operator's whole view of the decision.

    Exactly one, deliberately: two lines for one tree is a double-report bug, and taking the first
    hit would hide it.
    """
    matches = [ln for ln in output.splitlines() if _REPORT.match(ln) and str(path) in ln]
    assert len(matches) == 1, f"{len(matches)} report lines for {path} in:\n{output}"
    return matches[0]


def _summary(output: str) -> str:
    lines = [ln for ln in output.splitlines() if "worktree sweep:" in ln]
    assert len(lines) == 1, output
    return lines[0]


def _held_as(line: str) -> str:
    """The hold reason, read as tokens so a cosmetic realignment of the report cannot break it."""
    match = re.match(r"^HELD\s+(\S+(?:\s\S+)?)\s+/", line)
    assert match, line
    return match.group(1)


@pytest.fixture(scope="module")
def swept(tmp_path_factory: pytest.TempPathFactory) -> Swept:
    """One repo built once, swept in the default (report-only) mode, shared by the read-only tests."""
    root = tmp_path_factory.mktemp("worktree-sweep").resolve()
    paths = _build_repo(root)
    result = _sweep(paths["work"])
    assert result.returncode == 0, result.stderr
    return paths, result.stdout + result.stderr


class TestClassification:
    def test_merged_and_branch_gone_is_the_removable_case(self, swept: Swept) -> None:
        paths, out = swept
        assert _line_for(out, paths["merged_gone"]).startswith("WOULD REMOVE")

    def test_branch_gone_from_origin_with_unmerged_work_is_removable(self, swept: Swept) -> None:
        # The squash-merge case. Not conditioned on ancestry, because a squash-merged branch is
        # never an ancestor of main — if it were, this sweep would decline to clean up after the
        # merges it exists for. What makes it safe is the surviving branch ref, asserted in
        # TestApply::test_a_gone_branch_with_unmerged_work_stays_recoverable.
        paths, out = swept
        assert _line_for(out, paths["gone_unmerged"]).startswith("WOULD REMOVE")

    def test_detached_but_reachable_from_main_is_removable(self, swept: Swept) -> None:
        # Nothing to lose: the commits are already on main, so no ref needs to outlive the removal.
        paths, out = swept
        assert _line_for(out, paths["detached_merged"]).startswith("WOULD REMOVE")

    def test_uncommitted_work_is_held(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["dirty"])) == "uncommitted"

    def test_untracked_only_still_counts_as_uncommitted(self, swept: Swept) -> None:
        # The dirty tree has NO tracked modifications — only an untracked file. `git status
        # --porcelain` reports it; a `git diff --quiet` check would not, and the file would go.
        paths, _ = swept
        assert (paths["dirty"] / "scratch.txt").exists()

    def test_branch_still_on_origin_and_unmerged_is_held(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["live"])) == "live branch"

    def test_tip_inside_the_grace_window_is_held(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["fresh"])) == "within grace"

    def test_detached_head_not_on_main_is_held(self, swept: Swept) -> None:
        # The hazard this exists for: with no branch ref, "gone from origin" proves nothing.
        paths, out = swept
        assert _held_as(_line_for(out, paths["detached_unmerged"])) == "detached-unmerged"

    def test_locked_worktree_is_held(self, swept: Swept) -> None:
        paths, out = swept
        assert _held_as(_line_for(out, paths["locked"])) == "locked"

    def test_a_path_with_a_space_is_classified_whole(self, swept: Swept) -> None:
        paths, out = swept
        assert _line_for(out, paths["spaced"]).startswith("WOULD REMOVE")

    def test_a_registration_whose_directory_is_gone_is_prunable(self, swept: Swept) -> None:
        paths, out = swept
        assert _line_for(out, paths["pruned"]).startswith("WOULD PRUNE")

    def test_the_invoking_tree_is_skipped_out_loud(self, swept: Swept) -> None:
        # Silent truncation reads as "swept everything", so even a skip gets a line.
        paths, out = swept
        assert _line_for(out, paths["work"]).startswith("SKIP")

    def test_summary_names_every_bucket(self, swept: Swept) -> None:
        _, out = swept
        summary = _summary(out)
        for token in (
            "would-remove=4", "prunable=1", "uncommitted=1", "grace=1",
            "live-branch=1", "detached-unmerged=1", "locked=1", "unreadable=0",
        ):
            assert token in summary, summary

    def test_the_grace_window_is_arithmetic_not_a_constant(self, swept: Swept) -> None:
        # Same tree, same run, GRACE_HOURS=0: the ONLY thing holding it was the window.
        paths, _ = swept
        result = _sweep(paths["work"], GRACE_HOURS="0")
        assert _line_for(result.stdout, paths["fresh"]).startswith("WOULD REMOVE")


class TestDryRunIsTheDefault:
    def test_a_bare_invocation_removes_nothing(self, swept: Swept) -> None:
        # A destructive script whose no-argument behaviour is destructive is one fat-fingered
        # timer edit away from an incident. Removal needs --apply; the systemd unit carries it.
        paths, out = swept
        assert "WOULD REMOVE" in out
        assert not re.search(r"^REMOVED\b", out, re.MULTILINE)
        assert paths["merged_gone"].exists()

    def test_a_dry_run_does_not_even_prune_the_registry(self, swept: Swept) -> None:
        # "Report only" has to mean it, registration file included — otherwise a dry run is a
        # mutation nobody asked for and the mode's whole contract is a half-truth.
        paths, _ = swept
        listed = _git(paths["work"], "worktree", "list", "--porcelain")
        assert str(paths["pruned"]) in listed

    def test_dry_run_flag_agrees_with_the_default(self, swept: Swept) -> None:
        paths, _ = swept
        result = _sweep(paths["work"], "--dry-run")
        assert result.returncode == 0, result.stderr
        assert "WOULD REMOVE" in result.stdout
        assert paths["merged_gone"].exists()

    def test_help_names_the_flag_that_deletes(self) -> None:
        # Invoked the way ExecStart does — the file itself, not `bash <file>` — so the exec path
        # (mode bit + shebang) is exercised and not just the interpreter path every other test uses.
        result = subprocess.run([str(_SCRIPT), "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--apply" in result.stdout

    def test_a_prefix_of_apply_does_not_satisfy_it(self, tmp_path: Path) -> None:
        # `case` patterns are exact, but this is the argument that deletes: prove `--app` refuses
        # rather than falling through to a mode that removes.
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths["work"], "--app")
        assert result.returncode == 2
        assert "unknown argument" in result.stderr


@pytest.fixture(scope="module")
def applied(tmp_path_factory: pytest.TempPathFactory) -> Swept:
    """A second repo, swept once with --apply — the destructive path, run exactly once."""
    root = tmp_path_factory.mktemp("worktree-apply").resolve()
    paths = _build_repo(root)
    result = _sweep(paths["work"], "--apply")
    assert result.returncode == 0, result.stderr
    return paths, result.stdout + result.stderr


@_needs_proc
class TestApply:
    def test_only_the_removable_are_removed(self, applied: Swept) -> None:
        paths, _ = applied
        assert not paths["merged_gone"].exists()
        assert not paths["detached_merged"].exists()
        assert not paths["gone_unmerged"].exists()
        assert not paths["spaced"].exists()
        for held in ("dirty", "live", "fresh", "detached_unmerged", "locked"):
            assert paths[held].exists(), f"{held} was removed"
        assert (paths["dirty"] / "scratch.txt").read_text(encoding="utf-8") == "unsaved work"

    def test_a_gone_branch_with_unmerged_work_stays_recoverable(self, applied: Swept) -> None:
        # The entire safety argument for removing a squash-merged tree: the working directory goes,
        # `refs/heads/gone-unmerged` and the commit it points at do not.
        paths, _ = applied
        sha = _git(paths["work"], "rev-parse", "gone-unmerged")
        assert _git(paths["work"], "cat-file", "-t", sha) == "commit"
        assert _git(paths["work"], "log", "-1", "--format=%s", sha) == "squashed-work"

    def test_the_dropped_registration_is_pruned(self, applied: Swept) -> None:
        paths, _ = applied
        assert str(paths["pruned"]) not in _git(paths["work"], "worktree", "list", "--porcelain")

    def test_the_dirty_tree_is_reported_for_a_human(self, applied: Swept) -> None:
        paths, out = applied
        assert "NEEDS A HUMAN" in out
        assert str(paths["dirty"]) in out
        assert "scratch.txt" in out

    def test_a_run_that_held_something_still_exits_zero(self, applied: Swept) -> None:
        # Held trees and a NEEDS-A-HUMAN list are true reports, not unit failures. A weekly timer
        # that goes permanently red is a timer nobody reads inside a month. The fixture asserts
        # rc == 0 for this very run; what this pins is that the run DID hold something.
        _, out = applied
        assert "NEEDS A HUMAN" in out

    def test_a_second_apply_is_a_no_op(self, tmp_path: Path) -> None:
        # Its own repo, not the shared fixture: a test that mutates shared state is order- and
        # xdist-dependent, and would read as a sweep regression when it is really a scheduling one.
        paths = _build_repo(tmp_path.resolve())
        assert _sweep(paths["work"], "--apply").returncode == 0
        second = _sweep(paths["work"], "--apply")
        assert second.returncode == 0
        summary = _summary(second.stdout)
        assert "removed=0" in summary
        assert "failed=0" in summary


class TestFailClosed:
    def test_no_fetch_downgrades_the_run_to_report_only(self, tmp_path: Path) -> None:
        # Both removal tests read LOCAL remote refs. Without a fresh fetch those are a stale answer
        # to a live question, so the run reports and deletes nothing even with --apply.
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths["work"], "--apply", "--no-fetch")
        out = result.stdout + result.stderr
        assert "HOLD-ALL" in out
        assert "WOULD REMOVE" in out
        assert paths["merged_gone"].exists()
        assert result.returncode == 0  # a hold-all run is a report, not an error

    def test_a_failed_fetch_disables_removals(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        _git(paths["work"], "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
        result = _sweep(paths["work"], "--apply")
        out = result.stdout + result.stderr
        assert "HOLD-ALL" in out
        assert "fetch" in out
        assert paths["merged_gone"].exists()

    @_needs_proc
    def test_a_live_process_inside_a_worktree_holds_it(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        proc = subprocess.Popen(["sleep", "30"], cwd=paths["merged_gone"])
        try:
            result = _sweep(paths["work"], "--apply")
            out = result.stdout + result.stderr
            assert _held_as(_line_for(out, paths["merged_gone"])) == "active process"
            assert paths["merged_gone"].exists()
        finally:
            proc.kill()
            proc.wait()

    def test_a_blind_process_detector_disables_all_removals(self, tmp_path: Path) -> None:
        # The third hold-all condition, and the one guarding a LIVE agent's working directory.
        # PROC_ROOT exists so this is testable: pointed at nothing, the self-test fails, and a
        # detector that cannot look must never read as "no process found".
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths["work"], "--apply", PROC_ROOT=str(tmp_path / "no-procfs"))
        out = result.stdout + result.stderr
        assert "HOLD-ALL" in out
        assert "blind" in out
        assert paths["merged_gone"].exists()
        assert result.returncode == 0

    def test_git_itself_refuses_to_remove_a_dirty_tree(self, tmp_path: Path) -> None:
        # The second gate behind protection 2, and the reason nothing here passes --force: even if
        # a tree turned dirty between the status check and the removal, git declines. Pinned
        # because the safety argument leans on it, not because we control it.
        paths = _build_repo(tmp_path.resolve())
        result = subprocess.run(
            ["git", "worktree", "remove", str(paths["dirty"])],
            cwd=paths["work"], env=_git_env(), capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert (paths["dirty"] / "scratch.txt").exists()

    def test_unknown_argument_refuses_rather_than_sweeping(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths["work"], "--aply")
        assert result.returncode == 2
        assert "unknown argument" in result.stderr


class TestSourceGuards:
    """Cheap ratchets on the things that would silently break the survivability claim.

    Secondary to the behavioural guard in ``TestApply``, which is what actually proves committed
    work outlives a removal.
    """

    #: Every spelling of "destroy a ref" or "force past the dirty check" that would void it.
    _FORBIDDEN = re.compile(
        r"branch\s+(-[a-zA-Z]*[dD]\b|--delete)"
        r"|update-ref\s+-d"
        r"|push\s+.*--delete"
        r"|worktree\s+remove\s+(-[a-zA-Z]*f\b|--force)"
    )

    def test_no_executed_line_destroys_a_ref_or_forces_a_removal(self) -> None:
        for raw in _SCRIPT.read_text(encoding="utf-8").splitlines():
            stripped = raw.lstrip()
            if stripped.startswith("#"):
                continue  # a comment may NAME the hazard; only executed lines are the ratchet
            if stripped.startswith("echo"):
                continue  # the one --force in the file is text printed for a human to decide on
            assert not self._FORBIDDEN.search(raw.split("#", 1)[0]), raw

    def test_the_ratchet_would_actually_catch_a_regression(self) -> None:
        # A guard that matches nothing passes forever. Prove the pattern bites on each spelling.
        for hazard in (
            'git branch -D "$branch"',
            "git branch --delete $branch",
            'git update-ref -d "refs/heads/$branch"',
            "git push origin --delete $branch",
            'git worktree remove --force "$path"',
            'git worktree remove -f "$path"',
        ):
            assert self._FORBIDDEN.search(hazard), hazard


def _directives(unit: str) -> dict[str, list[str]]:
    """A systemd unit as key -> values, so a reformat or a deployment prefix is not a failure.

    Not configparser: ``Environment=`` legitimately repeats and configparser keeps only the last.
    """
    values: dict[str, list[str]] = {}
    for raw in (_SYSTEMD / unit).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())
    return values


class TestWeeklyWiring:
    """The PR claims a weekly sweep, so the schedule has to be committed, not just described.

    A GitHub Actions runner cannot see worktrees registered on the agent host, so this half cannot
    live next to stale-branches.yml — it is a systemd timer on the box.
    """

    def test_the_script_is_executable_the_way_execstart_runs_it(self) -> None:
        # ExecStart runs the file, not `bash <file>`. Land it 0644 or lose the shebang and the
        # timer fails 203/EXEC every Monday — whose only symptom is the accumulation this stops.
        assert os.access(_SCRIPT, os.X_OK), "ExecStart runs the file directly; needs mode 0755"
        assert _SCRIPT.read_text(encoding="utf-8").startswith("#!")

    def test_the_timer_runs_weekly_after_the_branch_sweep(self) -> None:
        timer = _directives("lem-worktree-sweep.timer")
        schedule = timer["OnCalendar"][0]
        assert schedule.startswith("Mon"), schedule  # after the Mon 06:00 UTC branch sweep
        assert "07:00:00" in schedule and "UTC" in schedule, schedule
        assert timer["Persistent"] == ["true"]  # a box that was off still sweeps on next boot
        assert timer["Unit"] == ["lem-worktree-sweep.service"]

    def test_the_service_carries_apply_and_runs_as_lem(self) -> None:
        service = _directives("lem-worktree-sweep.service")
        exec_start = service["ExecStart"][0]
        assert exec_start.startswith("/"), exec_start  # systemd needs an absolute program
        assert exec_start.endswith(f"{_SCRIPT.name} --apply"), exec_start
        # Never root: a root removal is the one way this could delete something it does not own.
        assert service["User"] == ["lem"]

    def test_the_service_pins_its_environment(self) -> None:
        # A systemd service inherits an even narrower environment than cron and no login shell, so
        # neither the git that decides removals nor the fetch's prompt behaviour is left to chance:
        # an unpinned PATH could swap the git binary, and a prompting fetch would hang to timeout.
        service = _directives("lem-worktree-sweep.service")
        environment = service["Environment"]
        assert any(v.startswith("PATH=") and "/usr/bin" in v for v in environment), environment
        assert "GIT_TERMINAL_PROMPT=0" in environment
        assert service["WorkingDirectory"][0].startswith("/")
