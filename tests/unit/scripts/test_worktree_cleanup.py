"""Tests for scripts/worktree_cleanup.sh — the worktree half of docs/branch-cleanup.md.

The sweep's success path deletes a directory on a weekly timer, so what has to be pinned is the
CLASSIFICATION, not the plumbing: which registrations it would remove and, far more importantly,
which it holds. Every test builds a throwaway repo with real worktrees in each state and reads the
script's own report lines, because those lines are the only thing an operator ever sees.

Two states are regression tests for hazards found while writing it:

* a **detached** worktree has no branch ref to outlive the removal, so "its branch is gone from
  origin" says nothing about it — only ancestry from origin/main does;
* bash `read` collapses a run of tabs, so an empty branch field would shift every later column and
  a detached tree would read as branch ``1``. The awk emitter uses a ``-`` sentinel; a regression
  shows up here as a detached tree landing in the wrong bucket.
"""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "worktree_cleanup.sh"
_SYSTEMD = Path(__file__).resolve().parents[3] / "scripts" / "systemd"

_OLD = "2020-01-01T00:00:00 +0000"


def _proc_cwd_is_readable() -> bool:
    """Whether protection 3's detector can see anything on THIS host.

    The sweep self-tests the same way and holds everything when the answer is no, so on such a
    host there is no removal left to assert — the tests that need one are skipped rather than
    asserting a decision the script deliberately refuses to make.
    """
    try:
        return bool(os.readlink("/proc/self/cwd"))
    except OSError:
        return False


_needs_proc = pytest.mark.skipif(
    not _proc_cwd_is_readable(),
    reason="/proc/*/cwd unreadable — the sweep holds everything here, so there is no removal to test",
)


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
    """The one report line naming this worktree — the operator's whole view of the decision."""
    matches = [ln for ln in output.splitlines() if str(path) in ln and not ln.startswith(" ")]
    assert matches, f"no report line for {path} in:\n{output}"
    return matches[0]


@pytest.fixture(scope="module")
def swept(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Path], str]:
    """One repo built once, swept in the default (report-only) mode, shared by the read-only tests."""
    root = tmp_path_factory.mktemp("worktree-sweep").resolve()
    paths = _build_repo(root)
    result = _sweep(paths["work"])
    assert result.returncode == 0, result.stderr
    return paths, result.stdout + result.stderr


class TestClassification:
    def test_merged_and_branch_gone_is_the_removable_case(self, swept) -> None:
        paths, out = swept
        assert _line_for(out, paths["merged_gone"]).startswith("WOULD REMOVE")

    def test_detached_but_reachable_from_main_is_removable(self, swept) -> None:
        # Nothing to lose: the commits are already on main, so no ref needs to outlive the removal.
        paths, out = swept
        assert _line_for(out, paths["detached_merged"]).startswith("WOULD REMOVE")

    def test_uncommitted_work_is_held(self, swept) -> None:
        paths, out = swept
        assert "HELD  uncommitted" in _line_for(out, paths["dirty"])

    def test_untracked_only_still_counts_as_uncommitted(self, swept) -> None:
        # The dirty tree has NO tracked modifications — only an untracked file. `git status
        # --porcelain` reports it; a `git diff --quiet` check would not, and the file would go.
        paths, _ = swept
        assert (paths["dirty"] / "scratch.txt").exists()

    def test_branch_still_on_origin_and_unmerged_is_held(self, swept) -> None:
        paths, out = swept
        assert "HELD  live branch" in _line_for(out, paths["live"])

    def test_tip_inside_the_grace_window_is_held(self, swept) -> None:
        paths, out = swept
        assert "HELD  within grace" in _line_for(out, paths["fresh"])

    def test_detached_head_not_on_main_is_held(self, swept) -> None:
        # The hazard this exists for: with no branch ref, "gone from origin" proves nothing.
        paths, out = swept
        assert "HELD  detached unmerged" in _line_for(out, paths["detached_unmerged"])

    def test_locked_worktree_is_held(self, swept) -> None:
        paths, out = swept
        assert "HELD  locked" in _line_for(out, paths["locked"])

    def test_the_invoking_tree_is_skipped_out_loud(self, swept) -> None:
        # Silent truncation reads as "swept everything", so even a skip gets a line.
        paths, out = swept
        skips = [ln for ln in out.splitlines() if ln.startswith("SKIP") and str(paths["work"]) in ln]
        assert skips, out

    def test_summary_names_every_held_bucket(self, swept) -> None:
        _, out = swept
        summary = [ln for ln in out.splitlines() if "worktree sweep: mode=" in ln]
        assert summary, out
        assert "would-remove=2" in summary[0]
        assert "uncommitted=1" in summary[0]
        assert "grace=1" in summary[0]
        assert "live-branch=2" in summary[0]  # live branch + detached unmerged
        assert "locked=1" in summary[0]


class TestDryRunIsTheDefault:
    def test_a_bare_invocation_removes_nothing(self, swept) -> None:
        # A destructive script whose no-argument behaviour is destructive is one fat-fingered
        # timer edit away from an incident. Removal needs --apply; the systemd unit carries it.
        paths, out = swept
        assert "WOULD REMOVE" in out
        assert "REMOVED " not in out
        assert paths["merged_gone"].exists()

    def test_dry_run_flag_agrees_with_the_default(self, swept) -> None:
        paths, _ = swept
        result = _sweep(paths["work"], "--dry-run")
        assert result.returncode == 0, result.stderr
        assert "WOULD REMOVE" in result.stdout
        assert paths["merged_gone"].exists()


@_needs_proc
class TestApply:
    def test_apply_removes_only_the_removable_and_keeps_the_branch_ref(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths["work"], "--apply")

        assert result.returncode == 0, result.stderr
        assert not paths["merged_gone"].exists()
        assert not paths["detached_merged"].exists()
        # Everything held keeps its directory, uncommitted work included.
        for held in ("dirty", "live", "fresh", "detached_unmerged", "locked"):
            assert paths[held].exists(), f"{held} was removed"
        assert (paths["dirty"] / "scratch.txt").read_text(encoding="utf-8") == "unsaved work"

        # The claim that committed work survives a removal rests entirely on refs/heads being
        # untouched. `git worktree remove` leaves it alone — assert that, don't assume it.
        branches = _git(paths["work"], "branch", "--format=%(refname:short)")
        assert "merged-gone" in branches.split()

    def test_the_dirty_tree_is_reported_for_a_human(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths["work"], "--apply")
        out = result.stdout + result.stderr
        assert "NEEDS A HUMAN" in out
        assert str(paths["dirty"]) in out
        assert "scratch.txt" in out


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
            assert "HELD  active process" in _line_for(out, paths["merged_gone"])
            assert paths["merged_gone"].exists()
        finally:
            proc.kill()
            proc.wait()

    def test_unknown_argument_refuses_rather_than_sweeping(self, tmp_path: Path) -> None:
        paths = _build_repo(tmp_path.resolve())
        result = _sweep(paths["work"], "--aply")
        assert result.returncode == 2
        assert "unknown argument" in result.stderr


class TestSourceGuards:
    """Cheap ratchets on the two things that would silently break the survivability claim."""

    def test_the_script_never_deletes_a_branch_ref(self) -> None:
        # Comments are allowed to NAME the hazard; no executed line may be it.
        for line in _SCRIPT.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert " branch -d" not in line, line
            assert " branch -D" not in line, line

    def test_worktree_remove_is_never_forced_by_the_script(self) -> None:
        # `--force` appears once, inside an echo that suggests a command to a human. Any executed
        # occurrence would defeat protection 2 — a dirty tree must refuse at the git layer too.
        for line in _SCRIPT.read_text(encoding="utf-8").splitlines():
            if "worktree remove --force" in line:
                assert line.lstrip().startswith("echo"), line


class TestWeeklyWiring:
    """The PR claims a weekly sweep, so the schedule has to be committed, not just described.

    A GitHub Actions runner cannot see worktrees registered on the agent host, so this half cannot
    live next to stale-branches.yml — it is a systemd timer on the box.
    """

    def test_the_timer_runs_weekly_after_the_branch_sweep(self) -> None:
        timer = (_SYSTEMD / "lem-worktree-sweep.timer").read_text(encoding="utf-8")
        assert "OnCalendar=Mon *-*-* 07:00:00 UTC" in timer
        assert "Persistent=true" in timer
        assert "Unit=lem-worktree-sweep.service" in timer

    def test_the_service_carries_apply_and_runs_as_lem(self) -> None:
        service = (_SYSTEMD / "lem-worktree-sweep.service").read_text(encoding="utf-8")
        assert "ExecStart=/home/lem/linkedin_engagement_manager/scripts/worktree_cleanup.sh --apply" in service
        assert "User=lem" in service
        assert "User=root" not in service

    def test_the_service_pins_its_environment(self) -> None:
        # A systemd service inherits an even narrower environment than cron and no login shell, so
        # neither the git that decides removals nor the fetch's prompt behaviour is left to chance:
        # an unpinned PATH could swap the git binary, and a prompting fetch would hang.
        service = (_SYSTEMD / "lem-worktree-sweep.service").read_text(encoding="utf-8")
        assert "WorkingDirectory=/home/lem/linkedin_engagement_manager" in service
        assert "Environment=PATH=/usr/local/bin:/usr/bin:/bin" in service
        assert "Environment=GIT_TERMINAL_PROMPT=0" in service
