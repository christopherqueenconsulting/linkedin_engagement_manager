"""A phased issue's next phase starts from main, not from its merged branch.

Phased issues reuse `feature/claude-issue-<N>` for every phase. After Phase K's PR squash-merged,
origin still held Phase K's original commits; `add_worktree` attached to them and the Phase K+1 PR
re-added every Phase K file (migration-version collisions, conflicts, merge-queue ejections — hand
repaired in #2693, #2694, #2706). `restart_spent_branch` (lib/guards.sh) now restarts a SPENT branch
from main and keeps every commit main does not carry.

Everything runs for real: the shipped guards.sh is sourced, git operates on a local bare remote, and
only `gh` is stubbed. The stub REFUSES any argument list but the one the guard must send, and it
renders the case's PR objects through the guard's own `--jq` with real `jq`, so the field list and
the jq expression are exercised rather than bypassed.
"""

from __future__ import annotations

import json
import re
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

# The stub accepts exactly the call branch_pr_heads must make. `--base` is deliberately ABSENT: a
# `--base main` list would hide an OPEN PR into another base, which must still attach; the base is
# checked on each PR's baseRefName instead. Any deviation is recorded and answered as a failure.
_GH_STUB = r"""#!/usr/bin/env bash
bad() { printf '%s\n' "$*" >> "$GH_BADARGS"; exit 2; }
[ -f "$GH_FAIL" ] && exit 1
[ "$1 $2" = "pr list" ] || bad "not pr list: $*"
shift 2
repo="" head="" state="" limit="" json="" jq_expr=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --head) head="$2"; shift 2 ;;
    --state) state="$2"; shift 2 ;;
    --limit) limit="$2"; shift 2 ;;
    --json) json="$2"; shift 2 ;;
    --jq) jq_expr="$2"; shift 2 ;;
    *) bad "unexpected argument: $1" ;;
  esac
done
[ "$repo" = "o/r" ] || bad "--repo=$repo"
[ "$head" = "$GH_EXPECT_HEAD" ] || bad "--head=$head"
[ "$state" = "all" ] || bad "--state=$state"
[ "$limit" = "100" ] || bad "--limit=$limit"
for f in state headRefOid baseRefName mergeCommit; do
  case ",$json," in *",$f,"*) ;; *) bad "--json lacks $f: $json" ;; esac
done
[ -n "$jq_expr" ] || bad "no --jq"
[ -f "$GH_HOOK" ] && bash "$GH_HOOK"
jq -r "$jq_expr" "$GH_PRS"
"""


def pr(state: str, head: str, base: str = "main", merge: str | None = None) -> dict:
    """One `gh pr list --json` object, shaped like GitHub's (mergeCommit is null unless merged)."""
    return {
        "state": state,
        "headRefOid": head,
        "baseRefName": base,
        "mergeCommit": {"oid": merge} if merge else None,
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
        self.prs = tmp / "prs.json"
        self.badargs = tmp / "gh-badargs"
        self.hook = tmp / "gh-hook.sh"
        self.env = {**_ENV, "HOME": str(tmp), "PATH": f"{self.bin}:/usr/bin:/bin"}
        self.bin.mkdir()
        self.work.mkdir()
        (self.bin / "gh").write_text(_GH_STUB, encoding="utf-8")
        (self.bin / "gh").chmod(0o755)
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

    def squash_merge(self, push_to: str = "main") -> str:
        """GitHub's squash: ONE new commit on main, patch-equivalent to none of the branch's."""
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--squash", BRANCH)
        self.git("commit", "-q", "-m", "Phase K (#1)")
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{push_to}")
        return self.git("rev-parse", "HEAD")

    def origin(self, ref: str) -> str:
        return self.git("rev-parse", ref, cwd=self.remote)

    def run(
        self,
        prs: list[dict] | None = None,
        gh_fails: bool = False,
        prefetch: bool = True,
        hook: str | None = None,
    ) -> tuple[str, Path]:
        """Source guards.sh and run add_worktree. Returns (stderr, worktree)."""
        self.prs.write_text(json.dumps(prs or []), encoding="utf-8")
        fail = self.tmp / "gh-fail"
        if gh_fails:
            fail.write_text("x", encoding="utf-8")
        if hook is not None:
            self.hook.write_text(hook, encoding="utf-8")
        if prefetch:
            self.git("fetch", "-q", "origin", cwd=self.repo)
        script = (
            "set -uo pipefail\n"
            f'BASE="{self.tmp}"; REPO="{self.repo}"; WORKROOT="{self.work}"; SLUG=o/r\n'
            'log() { echo "LOG: $*" >&2; }\n'
            f'. "{GUARDS}"\n'
            f'add_worktree "{BRANCH}" origin/main\n'
        )
        env = {
            **self.env,
            "GH_PRS": str(self.prs),
            "GH_FAIL": str(fail),
            "GH_BADARGS": str(self.badargs),
            "GH_HOOK": str(self.hook),
            "GH_EXPECT_HEAD": BRANCH,
        }
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        assert not self.badargs.exists(), self.badargs.read_text(encoding="utf-8")
        wt = self.work / BRANCH
        assert r.stdout == f"{wt}\n", "add_worktree's stdout is its return value — exactly the path"
        return r.stderr, wt

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
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main)])
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
    err, wt = box.run(prs=[])
    assert box.head(wt) == main
    assert "fresh every-commit-on-origin/main" in err


def test_a_unique_commit_with_no_merged_pr_attaches_and_loses_nothing(box: Box) -> None:
    tip = box.phase_k()
    err, wt = box.run(prs=[])
    assert box.head(wt) == tip
    assert box.unique(wt) == ["add phase_k.py", "add V1__phase_k.sql"]
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "attaching origin/feature/claude-issue-7 (unmerged-commits)" in err


def test_commits_pushed_after_the_merge_are_replayed_onto_main(box: Box) -> None:
    merged_head = box.phase_k()
    main = box.squash_merge()
    box.git("checkout", "-q", BRANCH)
    box.commit("phase_k1.py", "K1 = 1\n", msg="phase K+1 work")
    box.git("push", "-q", "origin", BRANCH)
    err, wt = box.run(prs=[pr("MERGED", merged_head, merge=main)])
    assert box.unique(wt) == ["phase K+1 work"], "only the post-merge commit, none of Phase K's"
    assert (wt / "phase_k1.py").read_text(encoding="utf-8") == "K1 = 1\n"
    assert box.origin(f"refs/heads/{BRANCH}") == box.head(wt)
    assert f"was spent (replay {merged_head})" in err


def test_post_merge_commits_that_do_not_replay_attach_with_a_warning(box: Box) -> None:
    merged_head = box.phase_k()
    squash = box.squash_merge()
    box.commit("phase_k.py", "K = 2\n", msg="main moves on")  # on main
    box.git("push", "-q", "origin", "main")
    box.git("checkout", "-q", BRANCH)
    old = box.commit("phase_k.py", "K = 3\n", msg="post-merge edit")
    box.git("push", "-q", "origin", BRANCH)
    err, wt = box.run(prs=[pr("MERGED", merged_head, merge=squash)])
    assert box.head(wt) == old, "kept today's behaviour: attached, nothing rewritten"
    assert box.git("status", "--porcelain", cwd=wt) == "", "no half-finished rebase left behind"
    assert box.origin(f"refs/heads/{BRANCH}") == old
    assert "WARNING" in err and f"({merged_head}..{old})" in err


def test_post_merge_commits_main_already_has_restart_fresh(box: Box) -> None:
    merged_head = box.phase_k()
    squash = box.squash_merge()
    box.git("checkout", "-q", BRANCH)
    extra = box.commit("extra.py", "E = 1\n", msg="extra")
    box.git("push", "-q", "origin", BRANCH)
    box.git("checkout", "-q", "main")
    box.git("cherry-pick", extra)
    box.git("push", "-q", "origin", "main")
    main = box.git("rev-parse", "HEAD")
    err, wt = box.run(prs=[pr("MERGED", merged_head, merge=squash)])
    assert box.head(wt) == main
    assert "fresh post-merge-commits-on-origin/main" in err


def test_an_open_pr_always_attaches_even_when_spent(box: Box) -> None:
    """fix/review/rebase lanes work the OPEN PR's head; rewriting it under them is not ours to do."""
    tip = box.phase_k()
    main = box.squash_merge()
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main), pr("OPEN", tip)])
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(open-pr)" in err


def test_an_open_pr_into_another_base_still_attaches(box: Box) -> None:
    """Why the PR list is not `--base main`: an open PR elsewhere is still a live head."""
    tip = box.phase_k()
    main = box.squash_merge()
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main), pr("OPEN", tip, base="release")])
    assert box.head(wt) == tip
    assert "(open-pr)" in err


def test_an_unreadable_pr_list_attaches(box: Box) -> None:
    tip = box.phase_k()
    box.squash_merge()
    err, wt = box.run(gh_fails=True)
    assert box.head(wt) == tip
    assert "(pr-list-unreadable)" in err


def test_a_closed_unmerged_pr_attaches(box: Box) -> None:
    """A CLOSED PR never landed: its head vouches for nothing."""
    tip = box.phase_k()
    err, wt = box.run(prs=[pr("CLOSED", tip)])
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(unmerged-commits)" in err


@pytest.mark.parametrize("main_has_it", [False, True], ids=["release-only", "release-reachable-from-main"])
def test_a_pr_merged_into_another_base_attaches(box: Box, main_has_it: bool) -> None:
    """Merged into `release`, not main: only a PR INTO main is evidence, even if main later has it.

    `release-reachable-from-main` isolates the baseRefName check from the reachability check.
    """
    tip = box.phase_k()
    release = box.squash_merge(push_to="release")
    if not main_has_it:
        box.git("reset", "-q", "--hard", "HEAD~1")  # dev's main back to before the squash
    else:
        box.git("push", "-q", "origin", "main")
    err, wt = box.run(prs=[pr("MERGED", tip, base="release", merge=release)])
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert box.unique(wt) == ["add phase_k.py", "add V1__phase_k.sql"]
    assert "(unmerged-commits)" in err


def test_a_merge_commit_unreachable_from_main_attaches(box: Box) -> None:
    """GitHub says merged into main, but main does not contain that merge: no proof, so attach."""
    tip = box.phase_k()
    elsewhere = box.squash_merge(push_to="not-main")  # a real commit, just never on main
    err, wt = box.run(prs=[pr("MERGED", tip, merge=elsewhere)])
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(unmerged-commits)" in err


def test_a_stale_local_main_is_fetched_before_deciding(box: Box) -> None:
    """REPO last fetched BEFORE the squash. The guard fetches, so it restarts from the REAL main."""
    tip = box.phase_k()
    box.git("fetch", "-q", "origin", cwd=box.repo)  # REPO sees the branch, not the squash
    stale = box.git("rev-parse", "origin/main", cwd=box.repo)
    main = box.squash_merge()
    assert stale != main
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main)], prefetch=False)
    assert box.head(wt) == main, "restarted from the main that HAS the squash, never the stale one"
    assert (wt / "phase_k.py").read_text(encoding="utf-8") == "K = 1\n"
    assert box.origin(f"refs/heads/{BRANCH}") == main


def test_a_failed_fetch_attaches(box: Box) -> None:
    tip = box.phase_k()
    main = box.squash_merge()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    box.remote.rename(box.tmp / "origin-gone.git")
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main)], prefetch=False)
    assert box.head(wt) == tip
    assert "(fetch-failed)" in err
    assert "spent" not in err


def test_unique_work_inside_a_merge_commit_attaches(box: Box) -> None:
    """`git cherry` skips merges, so an evil merge's own change would read as already on main."""
    box.git("checkout", "-q", "-b", BRANCH, "main")
    one = box.commit("phase_k.py", "K = 1\n")
    box.git("checkout", "-q", "-b", "side", "main")
    side = box.commit("side.py", "S = 1\n")
    box.git("checkout", "-q", BRANCH)
    box.git("merge", "-q", "--no-ff", "--no-commit", "side")
    (box.dev / "only_in_merge.py").write_text("M = 1\n", encoding="utf-8")
    box.git("add", "only_in_merge.py")
    box.git("commit", "-q", "-m", "merge side, plus a resolution only the merge carries")
    tip = box.git("rev-parse", "HEAD")
    box.git("push", "-q", "origin", BRANCH)
    box.git("checkout", "-q", "main")
    box.git("cherry-pick", one)
    box.git("cherry-pick", side)
    box.git("push", "-q", "origin", "main")
    # Every NON-merge commit is patch-equivalent on main, so the old rule read this branch as spent.
    assert "+" not in box.git("cherry", "origin/main", tip, cwd=box.dev)
    err, wt = box.run(prs=[pr("MERGED", tip, merge=box.git("rev-parse", "HEAD"))])
    assert box.head(wt) == tip
    assert (wt / "only_in_merge.py").read_text(encoding="utf-8") == "M = 1\n"
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(merge-commits)" in err


def test_an_open_pr_is_seen_past_a_pipe_sized_pr_list(box: Box) -> None:
    """`printf | grep -q` under pipefail: grep's early exit SIGPIPEs printf and reads as no match."""
    tip = box.phase_k()
    main = box.squash_merge()
    filler = [pr("CLOSED", "0" * 40)] * 20000  # far beyond a 64 KiB pipe buffer
    err, wt = box.run(prs=[pr("OPEN", tip), pr("MERGED", tip, merge=main), *filler])
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(open-pr)" in err


def test_no_grep_q_reads_a_pipe_in_guards() -> None:
    """Pin the here-string rule across the whole file, not just the cases above."""
    piped = [
        line
        for line in GUARDS.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and re.search(r"\|\s*grep\s+-q", line)
    ]
    assert piped == []


def test_a_merged_head_that_is_not_an_ancestor_attaches(box: Box) -> None:
    """The branch was rewritten after the merge: the PR no longer vouches for what is on it."""
    old_head = box.phase_k()
    main = box.squash_merge()
    box.git("checkout", "-q", "-B", BRANCH, "main")
    tip = box.commit("rewritten.py", "R = 1\n")
    box.git("push", "-q", "--force", "origin", BRANCH)
    err, wt = box.run(prs=[pr("MERGED", old_head, merge=main)])
    assert box.head(wt) == tip
    assert "(unmerged-commits)" in err


def test_a_lease_refusal_attaches_and_never_overwrites_origin(box: Box, tmp_path: Path) -> None:
    """Origin moves AFTER the guard's own fetch (the gh call is the window); the lease refuses."""
    tip = box.phase_k()
    main = box.squash_merge()
    racer = tmp_path / "racer"
    box.git("clone", "-q", "-b", BRANCH, str(box.remote), str(racer), cwd=tmp_path)
    (racer / "late.py").write_text("L = 1\n", encoding="utf-8")
    box.git("add", "late.py", cwd=racer)
    box.git("commit", "-q", "-m", "late", cwd=racer)
    late = box.git("rev-parse", "HEAD", cwd=racer)
    hook = f'git -C "{racer}" push -q origin {BRANCH} >/dev/null 2>&1\nrm -f "$GH_HOOK"\n'
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main)], hook=hook)
    assert box.origin(f"refs/heads/{BRANCH}") == late, "the lease must protect the newer push"
    assert box.head(wt) == tip
    assert "leased push" in err and "refused" in err


def test_no_origin_branch_is_unchanged(box: Box) -> None:
    err, wt = box.run(prs=[])
    assert box.head(wt) == box.origin("refs/heads/main")
    assert "attaching" not in err and "spent" not in err
