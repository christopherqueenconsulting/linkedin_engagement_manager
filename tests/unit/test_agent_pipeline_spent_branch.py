"""A phased issue's next phase starts from main, not from its merged branch.

Phased issues reuse `feature/claude-issue-<N>` for every phase. After Phase K's PR squash-merged,
origin still held Phase K's original commits; `add_worktree` attached to them and the Phase K+1 PR
re-added every Phase K file (migration-version collisions, conflicts, merge-queue ejections — hand
repaired in #2693, #2694, #2706). `restart_spent_branch` (lib/guards.sh) now restarts a SPENT branch
from main and keeps every commit main does not carry.

Everything runs for real: the shipped guards.sh is sourced, git operates on a local bare remote,
and only `gh` is stubbed (it prints the PR list the case needs).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
GUARDS = PIPELINE / "lib" / "guards.sh"
BRANCH = "feature/claude-issue-7"

_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "a",
    "GIT_AUTHOR_EMAIL": "a@b",
    "GIT_COMMITTER_NAME": "a",
    "GIT_COMMITTER_EMAIL": "a@b",
}


class Box:
    """origin (bare), a developer clone that plays GitHub, and the pipeline's REPO clone."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.remote = tmp / "origin.git"
        self.dev = tmp / "dev"
        self.repo = tmp / "repo"
        self.work = tmp / "work"
        self.bin = tmp / "bin"
        self.prs = tmp / "prs.txt"
        self.env = {**_ENV, "HOME": str(tmp), "PATH": f"{self.bin}:/usr/bin:/bin"}
        self.bin.mkdir()
        self.work.mkdir()
        (self.bin / "gh").write_text(
            '#!/usr/bin/env bash\n[ -f "$GH_FAIL" ] && exit 1\ncat "$GH_PRS" 2>/dev/null\n', encoding="utf-8"
        )
        (self.bin / "gh").chmod(0o755)
        self.prs.write_text("", encoding="utf-8")
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=tmp)
        self.git("clone", "-q", str(self.remote), str(self.dev), cwd=tmp)
        self.git("checkout", "-q", "-b", "main", cwd=self.dev)
        self.commit("README", "base\n")
        self.git("push", "-q", "origin", "main", cwd=self.dev)
        self.git("clone", "-q", str(self.remote), str(self.repo), cwd=tmp)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        r = subprocess.run(["git", *args], cwd=cwd or self.dev, env=self.env, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr)
        return r.stdout.strip()

    def commit(self, name: str, text: str, msg: str | None = None) -> str:
        (self.dev / name).write_text(text, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", msg or f"add {name}")
        return self.git("rev-parse", "HEAD")

    def phase_k(self) -> str:
        """Two Phase K commits on the branch, pushed. Returns the branch tip."""
        self.git("checkout", "-q", "-b", BRANCH, "main")
        self.commit("V1__phase_k.sql", "create table k;\n")
        tip = self.commit("phase_k.py", "K = 1\n")
        self.git("push", "-q", "origin", BRANCH)
        return tip

    def squash_merge(self) -> str:
        """GitHub's squash: ONE new commit on main, patch-equivalent to none of the branch's."""
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--squash", BRANCH)
        self.git("commit", "-q", "-m", "Phase K (#1)")
        self.git("push", "-q", "origin", "main")
        return self.git("rev-parse", "HEAD")

    def origin(self, ref: str) -> str:
        return self.git("rev-parse", ref, cwd=self.remote)

    def run(self, prs: str = "", gh_fails: bool = False) -> tuple[str, str, Path]:
        self.prs.write_text(prs, encoding="utf-8")
        fail = self.tmp / "gh-fail"
        if gh_fails:
            fail.write_text("x", encoding="utf-8")
        self.git("fetch", "-q", "origin", cwd=self.repo)
        script = (
            "set -uo pipefail\n"
            f'BASE="{self.tmp}"; REPO="{self.repo}"; WORKROOT="{self.work}"; SLUG=o/r\n'
            'log() { echo "LOG: $*" >&2; }\n'
            f'. "{GUARDS}"\n'
            f'add_worktree "{BRANCH}" origin/main\n'
        )
        env = {**self.env, "GH_PRS": str(self.prs), "GH_FAIL": str(fail)}
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        wt = self.work / BRANCH
        assert r.stdout == f"{wt}\n", "add_worktree's stdout is its return value — exactly the path"
        return r.stdout, r.stderr, wt

    def head(self, wt: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=wt)

    def unique(self, wt: Path) -> list[str]:
        """Commit subjects the worktree's branch carries beyond main."""
        out = self.git("log", "--format=%s", "origin/main..HEAD", cwd=wt)
        return out.splitlines() if out else []


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_a_merged_pr_whose_head_is_the_tip_restarts_from_main(box: Box) -> None:
    """The incident: squash of TWO commits, so `git cherry` sees both as unique — the PR proves it."""
    tip = box.phase_k()
    main = box.squash_merge()
    _, err, wt = box.run(prs=f"MERGED {tip}\n")
    assert box.head(wt) == main
    assert box.unique(wt) == []
    assert box.origin(f"refs/heads/{BRANCH}") == main, "the runner moved origin with a lease"
    assert "was spent (fresh merged-pr-head-is-tip)" in err
    # The agent's later plain push fast-forwards from here.
    upstream = box.git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=wt)
    assert upstream == f"origin/{BRANCH}"


def test_every_commit_patch_equivalent_on_main_restarts_with_no_pr(box: Box) -> None:
    box.git("checkout", "-q", "-b", BRANCH, "main")
    one = box.commit("phase_k.py", "K = 1\n")
    box.git("push", "-q", "origin", BRANCH)
    box.git("checkout", "-q", "main")
    box.git("cherry-pick", one)
    box.git("push", "-q", "origin", "main")
    main = box.git("rev-parse", "HEAD")
    _, err, wt = box.run(prs="")
    assert box.head(wt) == main
    assert "fresh every-commit-on-origin/main" in err


def test_a_unique_commit_with_no_merged_pr_attaches_and_loses_nothing(box: Box) -> None:
    tip = box.phase_k()
    _, err, wt = box.run(prs="")
    assert box.head(wt) == tip
    assert box.unique(wt) == ["add phase_k.py", "add V1__phase_k.sql"]
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "attaching origin/feature/claude-issue-7 (unmerged-commits)" in err


def test_commits_pushed_after_the_merge_are_replayed_onto_main(box: Box) -> None:
    merged_head = box.phase_k()
    box.squash_merge()
    box.git("checkout", "-q", BRANCH)
    box.commit("phase_k1.py", "K1 = 1\n", msg="phase K+1 work")
    box.git("push", "-q", "origin", BRANCH)
    _, err, wt = box.run(prs=f"MERGED {merged_head}\n")
    assert box.unique(wt) == ["phase K+1 work"], "only the post-merge commit, none of Phase K's"
    assert (wt / "phase_k1.py").read_text(encoding="utf-8") == "K1 = 1\n"
    assert box.origin(f"refs/heads/{BRANCH}") == box.head(wt)
    assert f"was spent (replay {merged_head})" in err


def test_post_merge_commits_that_do_not_replay_attach_with_a_warning(box: Box) -> None:
    merged_head = box.phase_k()
    box.squash_merge()
    box.commit("phase_k.py", "K = 2\n", msg="main moves on")  # on main
    box.git("push", "-q", "origin", "main")
    box.git("checkout", "-q", BRANCH)
    old = box.commit("phase_k.py", "K = 3\n", msg="post-merge edit")
    box.git("push", "-q", "origin", BRANCH)
    _, err, wt = box.run(prs=f"MERGED {merged_head}\n")
    assert box.head(wt) == old, "kept today's behaviour: attached, nothing rewritten"
    assert box.git("status", "--porcelain", cwd=wt) == "", "no half-finished rebase left behind"
    assert box.origin(f"refs/heads/{BRANCH}") == old
    assert "WARNING" in err and f"({merged_head}..{old})" in err


def test_post_merge_commits_main_already_has_restart_fresh(box: Box) -> None:
    merged_head = box.phase_k()
    box.squash_merge()
    box.git("checkout", "-q", BRANCH)
    extra = box.commit("extra.py", "E = 1\n", msg="extra")
    box.git("push", "-q", "origin", BRANCH)
    box.git("checkout", "-q", "main")
    box.git("cherry-pick", extra)
    box.git("push", "-q", "origin", "main")
    main = box.git("rev-parse", "HEAD")
    _, err, wt = box.run(prs=f"MERGED {merged_head}\n")
    assert box.head(wt) == main
    assert "fresh post-merge-commits-on-origin/main" in err


def test_an_open_pr_always_attaches_even_when_spent(box: Box) -> None:
    """fix/review/rebase lanes work the OPEN PR's head; rewriting it under them is not ours to do."""
    tip = box.phase_k()
    box.squash_merge()
    _, err, wt = box.run(prs=f"MERGED {tip}\nOPEN {tip}\n")
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(open-pr)" in err


def test_an_unreadable_pr_list_attaches(box: Box) -> None:
    tip = box.phase_k()
    box.squash_merge()
    _, err, wt = box.run(gh_fails=True)
    assert box.head(wt) == tip
    assert "(pr-list-unreadable)" in err


def test_a_merged_head_that_is_not_an_ancestor_attaches(box: Box) -> None:
    """The branch was rewritten after the merge: the PR no longer vouches for what is on it."""
    old_head = box.phase_k()
    box.squash_merge()
    box.git("checkout", "-q", "-B", BRANCH, "main")
    tip = box.commit("rewritten.py", "R = 1\n")
    box.git("push", "-q", "--force", "origin", BRANCH)
    _, err, wt = box.run(prs=f"MERGED {old_head}\n")
    assert box.head(wt) == tip
    assert "(unmerged-commits)" in err


def test_a_lease_refusal_attaches_and_never_overwrites_origin(box: Box, tmp_path: Path) -> None:
    """The lease refuses when origin moved after the fetch, and the newer commit survives."""
    tip = box.phase_k()
    box.squash_merge()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    racer = tmp_path / "racer"
    box.git("clone", "-q", "-b", BRANCH, str(box.remote), str(racer), cwd=tmp_path)
    (racer / "late.py").write_text("L = 1\n", encoding="utf-8")
    box.git("add", "late.py", cwd=racer)
    box.git("commit", "-q", "-m", "late", cwd=racer)
    box.git("push", "-q", "origin", BRANCH, cwd=racer)
    late = box.git("rev-parse", "HEAD", cwd=racer)
    # Run WITHOUT re-fetching, so REPO's origin/<branch> is the stale tip the lease is pinned to.
    box.prs.write_text(f"MERGED {tip}\n", encoding="utf-8")
    script = (
        "set -uo pipefail\n"
        f'BASE="{box.tmp}"; REPO="{box.repo}"; WORKROOT="{box.work}"; SLUG=o/r\n'
        'log() { echo "LOG: $*" >&2; }\n'
        f'. "{GUARDS}"\n'
        f'add_worktree "{BRANCH}" origin/main\n'
    )
    env = {**box.env, "GH_PRS": str(box.prs), "GH_FAIL": str(tmp_path / "none")}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    wt = box.work / BRANCH
    assert r.stdout == f"{wt}\n"
    assert box.origin(f"refs/heads/{BRANCH}") == late, "the lease must protect the newer push"
    assert box.head(wt) == tip
    assert "leased push" in r.stderr and "refused" in r.stderr


def test_no_origin_branch_is_unchanged(box: Box) -> None:
    _, err, wt = box.run(prs="")
    assert box.head(wt) == box.origin("refs/heads/main")
    assert "attaching" not in err and "spent" not in err
