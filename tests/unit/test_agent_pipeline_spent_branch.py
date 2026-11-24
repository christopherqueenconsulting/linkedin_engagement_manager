"""A phased issue's next phase starts from main, not from its merged branch.

Phased issues reuse `feature/claude-issue-<N>` for every phase. After Phase K's PR squash-merged,
the branch still held Phase K's original commits; `add_worktree` attached to them and the Phase K+1
PR re-added every Phase K file (migration-version collisions, conflicts, merge-queue ejections, hand
repaired in #2693, #2694, #2706). The repo deletes a branch on merge, so the stale branch usually
survives only as a LOCAL ref and/or a stale `refs/remotes/origin/<branch>`.

ONE rule decides (`restart_spent_branch`, lib/guards.sh): the branch is SPENT iff its tip EQUALS the
head of a PR MERGED into main whose merge commit freshly fetched origin/main contains. Everything
else attaches, so no heuristic (`git cherry` patch ids) can read real work as already on main.

Everything runs for real: the shipped guards.sh is sourced, git operates on a local bare remote, and
only `gh` is stubbed. The stub REFUSES any argument list but the one the guard must send, and it
renders the case's PR objects through the guard's own `--jq` with real `jq`, so the field list and
the jq expression are exercised rather than bypassed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
for f in number state headRefOid baseRefName mergeCommit; do
  case ",$json," in *",$f,"*) ;; *) bad "--json lacks $f: $json" ;; esac
done
[ -n "$jq_expr" ] || bad "no --jq"
[ -f "$GH_HOOK" ] && bash "$GH_HOOK"
jq -r "$jq_expr" "$GH_PRS"
"""


def pr(state: str, head: str, base: str = "main", merge: str | None = None, number: int = 1) -> dict:
    """One `gh pr list --json` object, shaped like GitHub's (mergeCommit is null unless merged)."""
    return {
        "number": number,
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

    def delete_on_merge(self, keep_tracking: bool) -> None:
        """delete_branch_on_merge=true: origin loses the branch; REPO keeps a LOCAL ref of it.

        `keep_tracking` leaves REPO's stale refs/remotes/origin/<branch> (v2 never prunes), which
        sends add_worktree down its origin path; without it add_worktree takes the local-only path.
        """
        self.git("fetch", "-q", "origin", cwd=self.repo)
        self.git("branch", "-q", BRANCH, f"origin/{BRANCH}", cwd=self.repo)
        self.git("push", "-q", "origin", "--delete", BRANCH)
        if not keep_tracking:
            self.git("update-ref", "-d", f"refs/remotes/origin/{BRANCH}", cwd=self.repo)

    def origin(self, ref: str) -> str:
        return self.git("rev-parse", ref, cwd=self.remote)

    def origin_has_branch(self) -> bool:
        return bool(self.git("for-each-ref", f"refs/heads/{BRANCH}", cwd=self.remote))

    def has_ref(self, ref: str) -> bool:
        return bool(self.git("for-each-ref", ref, cwd=self.repo))

    def run(
        self,
        prs: list[dict] | None = None,
        gh_fails: bool = False,
        prefetch: bool = True,
        hook: str | None = None,
        refused: bool = False,
        pre: str = "",
    ) -> tuple[str, Path]:
        """Source guards.sh and run add_worktree. Returns (stderr, worktree).

        `refused` expects add_worktree to fail closed: rc 1, nothing on stdout, no worktree built.
        `pre` is bash run after sourcing (e.g. disabling the pre-check to reach git's own refusal).
        """
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
            f"{pre}\n"
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
        assert not self.badargs.exists(), self.badargs.read_text(encoding="utf-8")
        wt = self.work / BRANCH
        if refused:
            assert r.returncode == 1, r.stderr
            assert r.stdout == "", "a refused add_worktree must hand its caller no path"
            assert not wt.exists(), "a refused add_worktree must leave no worktree behind"
            return r.stderr, wt
        assert r.returncode == 0, r.stderr
        assert r.stdout == f"{wt}\n", "add_worktree's stdout is its return value — exactly the path"
        return r.stderr, wt

    def head(self, wt: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=wt)

    def branch_at(self, wt: Path) -> str:
        return self.git("symbolic-ref", "--short", "HEAD", cwd=wt)

    def upstream(self, wt: Path) -> str | None:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=wt, env=self.env, capture_output=True, text=True
        )
        return r.stdout.strip() if r.returncode == 0 else None

    def holders(self) -> list[str]:
        """Every worktree (the main checkout included) whose HEAD is refs/heads/<BRANCH>."""
        out, path = [], ""
        for line in self.git("worktree", "list", "--porcelain", cwd=self.repo).splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
            elif line == f"branch refs/heads/{BRANCH}":
                out.append(path)
        return out

    def unique(self, wt: Path) -> list[str]:
        """Commit subjects the worktree's branch carries beyond main."""
        out = self.git("log", "--format=%s", "origin/main..HEAD", cwd=wt)
        return out.splitlines() if out else []


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


# --- SPENT: tip == a merged-into-main PR's head, merge commit on fresh origin/main ----------------


def test_spent_local_only_branch_with_origin_deleted_restarts_fresh_and_pushes_nothing(box: Box) -> None:
    """The real incident: origin deleted the branch on merge, only the LOCAL ref is left."""
    tip = box.phase_k()
    main = box.squash_merge()
    box.delete_on_merge(keep_tracking=False)
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main, number=41)], prefetch=False)
    assert "reattaching existing" in err, "took add_worktree's local-only path"
    assert box.head(wt) == main
    assert box.branch_at(wt) == BRANCH
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == main
    assert box.unique(wt) == []
    assert not box.origin_has_branch(), "nothing pushed: the lane's first push creates it"
    assert box.upstream(wt) is None, "never origin/main as upstream"
    assert "was spent (merged PR #41" in err and "refs/pull/41/head" in err


def test_spent_with_a_stale_tracking_ref_restarts_fresh_and_drops_the_stale_ref(box: Box) -> None:
    """Local ref plus a never-pruned refs/remotes/origin/<branch>: add_worktree's origin path."""
    tip = box.phase_k()
    main = box.squash_merge()
    box.delete_on_merge(keep_tracking=True)
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main)], prefetch=False)
    assert "reattaching existing" not in err, "took add_worktree's origin path"
    assert box.head(wt) == main
    assert not box.origin_has_branch()
    assert not box.has_ref(f"refs/remotes/origin/{BRANCH}"), "else the next run re-attaches the merged head"
    assert box.upstream(wt) is None
    assert "nothing pushed" in err


def test_spent_with_origin_still_holding_it_restarts_fresh_with_a_leased_push(box: Box) -> None:
    tip = box.phase_k()
    main = box.squash_merge()
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main)])
    assert box.head(wt) == main
    assert box.unique(wt) == []
    assert box.origin(f"refs/heads/{BRANCH}") == main, "the runner moved origin with a lease"
    assert f"lease on {tip}" in err
    # The agent's later plain push fast-forwards from here.
    assert box.upstream(wt) == f"origin/{BRANCH}"


def test_a_reverted_squash_is_still_spent_and_restarts_without_the_phase(box: Box) -> None:
    """Documented: head == tip of a merged PR is spent even if main later reverted it.

    The phase's commits still live in refs/pull/<N>/head, which the log line names.
    """
    tip = box.phase_k()
    squash = box.squash_merge()
    box.git("revert", "--no-edit", squash)
    box.git("push", "-q", "origin", "main")
    main = box.git("rev-parse", "HEAD")
    box.delete_on_merge(keep_tracking=False)
    err, wt = box.run(prs=[pr("MERGED", tip, merge=squash, number=9)], prefetch=False)
    assert box.head(wt) == main
    assert not (wt / "phase_k.py").exists()
    assert "refs/pull/9/head" in err


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


# --- Everything else ATTACHES, exactly as before ---------------------------------------------------


def test_a_commit_pushed_after_the_merge_attaches(box: Box) -> None:
    """The tip is not the merged head, so not spent: post-merge work stays, with no replay heuristic."""
    merged_head = box.phase_k()
    main = box.squash_merge()
    box.git("checkout", "-q", BRANCH)
    tip = box.commit("phase_k1.py", "K1 = 1\n", msg="phase K+1 work")
    box.git("push", "-q", "origin", BRANCH)
    err, wt = box.run(prs=[pr("MERGED", merged_head, merge=main)])
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(tip-is-not-a-merged-pr-head)" in err and "spent" not in err


def test_an_unpushed_local_commit_after_the_merge_attaches(box: Box) -> None:
    """Local-only path: origin deleted the branch, and the LOCAL ref carries a post-merge commit."""
    merged_head = box.phase_k()
    main = box.squash_merge()
    box.delete_on_merge(keep_tracking=False)
    box.git("checkout", "-q", BRANCH)
    box.commit("unpushed.py", "U = 1\n", msg="unpushed")
    box.git("fetch", "-q", str(box.dev), f"+refs/heads/{BRANCH}:refs/heads/{BRANCH}", cwd=box.repo)
    tip = box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo)
    err, wt = box.run(prs=[pr("MERGED", merged_head, merge=main)], prefetch=False)
    assert box.head(wt) == tip
    assert (wt / "unpushed.py").read_text(encoding="utf-8") == "U = 1\n"
    assert "(tip-is-not-a-merged-pr-head)" in err


def test_a_whitespace_variant_on_main_never_reads_as_spent(box: Box) -> None:
    """Round-2 regression: patch ids are whitespace-blind, so `git cherry` called this work landed."""
    box.git("checkout", "-q", "-b", BRANCH, "main")
    tip = box.commit("phase_k.py", "K = 1\n")
    box.git("push", "-q", "origin", BRANCH)
    box.git("checkout", "-q", "main")
    box.commit("phase_k.py", "K=1\n")  # differs ONLY in whitespace
    box.git("push", "-q", "origin", "main")
    assert "+" not in box.git("cherry", "origin/main", tip), "the scenario the old rule read as spent"
    err, wt = box.run(prs=[pr("CLOSED", tip)])
    assert box.head(wt) == tip
    assert (wt / "phase_k.py").read_text(encoding="utf-8") == "K = 1\n"
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(tip-is-not-a-merged-pr-head)" in err


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
    assert "(tip-is-not-a-merged-pr-head)" in err


def test_a_merge_commit_main_does_not_contain_attaches(box: Box) -> None:
    """GitHub says merged into main, but origin/main lacks that merge (GitHub ahead, or stale)."""
    tip = box.phase_k()
    elsewhere = box.squash_merge(push_to="not-main")  # a real commit, just never on main
    box.delete_on_merge(keep_tracking=False)
    err, wt = box.run(prs=[pr("MERGED", tip, merge=elsewhere)], prefetch=False)
    assert box.head(wt) == tip
    assert box.unique(wt) == ["add phase_k.py", "add V1__phase_k.sql"]
    assert "(tip-is-not-a-merged-pr-head)" in err


def test_a_failed_fetch_attaches(box: Box) -> None:
    tip = box.phase_k()
    main = box.squash_merge()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    box.remote.rename(box.tmp / "origin-gone.git")
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main)], prefetch=False)
    assert box.head(wt) == tip
    assert "(fetch-failed)" in err
    assert "spent" not in err


def test_an_open_pr_always_attaches_even_when_spent(box: Box) -> None:
    """fix/review/rebase lanes work the OPEN PR's head; rewriting it under them is not ours to do."""
    tip = box.phase_k()
    main = box.squash_merge()
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main), pr("OPEN", tip, number=2)])
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(open-pr)" in err


def test_an_open_pr_into_another_base_still_attaches(box: Box) -> None:
    """Why the PR list is not `--base main`: an open PR elsewhere is still a live head."""
    tip = box.phase_k()
    main = box.squash_merge()
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main), pr("OPEN", tip, base="release", number=2)])
    assert box.head(wt) == tip
    assert "(open-pr)" in err


def test_an_open_pr_is_seen_past_a_pipe_sized_pr_list(box: Box) -> None:
    """`printf | grep -q` under pipefail: grep's early exit SIGPIPEs printf and reads as no match."""
    tip = box.phase_k()
    main = box.squash_merge()
    filler = [pr("CLOSED", "0" * 40)] * 20000  # far beyond a 64 KiB pipe buffer
    err, wt = box.run(prs=[pr("OPEN", tip), pr("MERGED", tip, merge=main), *filler])
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(open-pr)" in err


def test_an_unreadable_pr_list_attaches(box: Box) -> None:
    tip = box.phase_k()
    box.squash_merge()
    box.delete_on_merge(keep_tracking=False)
    err, wt = box.run(gh_fails=True, prefetch=False)
    assert box.head(wt) == tip
    assert "(pr-list-unreadable)" in err


def test_a_closed_unmerged_pr_attaches(box: Box) -> None:
    """A CLOSED PR never landed: its head vouches for nothing."""
    tip = box.phase_k()
    err, wt = box.run(prs=[pr("CLOSED", tip)])
    assert box.head(wt) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip
    assert "(tip-is-not-a-merged-pr-head)" in err


def test_a_merged_head_the_branch_was_rewritten_away_from_attaches(box: Box) -> None:
    old_head = box.phase_k()
    main = box.squash_merge()
    box.git("checkout", "-q", "-B", BRANCH, "main")
    tip = box.commit("rewritten.py", "R = 1\n")
    box.git("push", "-q", "--force", "origin", BRANCH)
    err, wt = box.run(prs=[pr("MERGED", old_head, merge=main)])
    assert box.head(wt) == tip
    assert "(tip-is-not-a-merged-pr-head)" in err


def test_origin_ahead_of_a_stale_tracking_ref_attaches(box: Box) -> None:
    """The tracking ref is the merged head, but origin has moved on: refs disagree, so attach."""
    tip = box.phase_k()
    main = box.squash_merge()
    box.git("fetch", "-q", "origin", cwd=box.repo)  # REPO's tracking ref = the merged head
    box.git("checkout", "-q", BRANCH)
    later = box.commit("later.py", "L = 1\n", msg="pushed after REPO's last fetch")
    box.git("push", "-q", "origin", BRANCH)
    err, wt = box.run(prs=[pr("MERGED", tip, merge=main)], prefetch=False)
    assert box.head(wt) == tip, "attached to the tracking ref, exactly as before"
    assert box.origin(f"refs/heads/{BRANCH}") == later, "origin's newer work untouched"
    assert "(refs-disagree:" in err and "spent" not in err


def test_an_unpushed_local_commit_on_the_origin_path_is_never_moved_back(box: Box) -> None:
    """Stale tracking ref kept, local ref one commit AHEAD of it: `checkout -B <b> origin/<b>` orphaned it.

    The origin path used to reset refs/heads/<branch> onto the tracking ref unconditionally; a local
    ref that is not an ancestor of the tracking ref is attached as-is instead.
    """
    merged_head = box.phase_k()
    main = box.squash_merge()
    box.delete_on_merge(keep_tracking=True)
    box.git("checkout", "-q", BRANCH)
    box.commit("unpushed.py", "U = 1\n", msg="unpushed")
    box.git("fetch", "-q", str(box.dev), f"+refs/heads/{BRANCH}:refs/heads/{BRANCH}", cwd=box.repo)
    tip = box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo)
    assert box.git("rev-parse", f"refs/remotes/origin/{BRANCH}", cwd=box.repo) == merged_head
    err, wt = box.run(prs=[pr("MERGED", merged_head, merge=main)], prefetch=False)
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == tip, "local ref never moved back"
    assert box.head(wt) == tip
    assert box.branch_at(wt) == BRANCH
    assert (wt / "unpushed.py").read_text(encoding="utf-8") == "U = 1\n"
    assert "attaching local" in err and "was spent" not in err
    assert "reattaching existing" not in err, "took add_worktree's origin path"


# --- A branch another worktree holds is REFUSED (fail closed), never moved or attached ------------
# git 2.43 (prod) lets `checkout -B` move a branch checked out in another worktree; 2.44 refuses.
# These run under the box's own git, so they pin the guard on the version that needs it.


def _hold_elsewhere(box: Box, tmp_path: Path, at: str | None = None) -> Path:
    """Check BRANCH out in another worktree of REPO (creating the local ref at `at` if given)."""
    if at is not None:
        box.git("branch", "-q", "-f", BRANCH, at, cwd=box.repo)
    other = tmp_path / "other-wt"
    box.git("worktree", "add", "-q", str(other), BRANCH, cwd=box.repo)
    return other


def test_s1_local_behind_tracking_held_elsewhere_is_refused(box: Box, tmp_path: Path) -> None:
    """S1: the origin path's `checkout -B <b> origin/<b>` would fast-forward the OTHER worktree's HEAD."""
    behind = box.phase_k()
    box.git("checkout", "-q", BRANCH)
    tip = box.commit("later.py", "L = 1\n", msg="later")
    box.git("push", "-q", "origin", BRANCH)
    box.git("fetch", "-q", "origin", cwd=box.repo)
    other = _hold_elsewhere(box, tmp_path, at=behind)
    assert box.git("rev-parse", f"refs/remotes/origin/{BRANCH}", cwd=box.repo) == tip
    err, _ = box.run(prs=[pr("OPEN", tip)], prefetch=False, refused=True)
    assert box.head(other) == behind, "the other worktree's HEAD moved under it"
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == behind
    assert box.origin(f"refs/heads/{BRANCH}") == tip, "nothing pushed"
    assert box.holders() == [str(other)]
    assert err.count("WARNING") == 1 and "(checked-out-elsewhere)" in err and str(other) in err


def test_s2_local_equal_to_tracking_held_elsewhere_is_refused(box: Box, tmp_path: Path) -> None:
    """S2: a no-op `checkout -B` still put a SECOND worktree on the branch; exactly one may hold it."""
    tip = box.phase_k()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    other = _hold_elsewhere(box, tmp_path, at=f"origin/{BRANCH}")
    err, _ = box.run(prs=[pr("OPEN", tip)], prefetch=False, refused=True)
    assert box.holders() == [str(other)], "exactly one worktree holds the branch"
    assert box.head(other) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip, "nothing pushed"
    assert err.count("WARNING") == 1 and str(other) in err


def test_a_spent_branch_held_elsewhere_is_refused_not_attached(box: Box, tmp_path: Path) -> None:
    """The spent path used to 'attach' here, which still left two worktrees on one branch."""
    tip = box.phase_k()
    main = box.squash_merge()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    other = _hold_elsewhere(box, tmp_path, at=f"origin/{BRANCH}")
    err, _ = box.run(prs=[pr("MERGED", tip, merge=main)], prefetch=False, refused=True)
    assert box.head(other) == tip, "the other worktree's HEAD moved under it"
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip, "nothing pushed over the held branch"
    assert box.holders() == [str(other)]
    assert "(checked-out-elsewhere)" in err and str(other) in err
    assert "restarted from" not in err and "attaching" not in err


def test_a_spent_branch_local_only_held_elsewhere_is_refused(box: Box, tmp_path: Path) -> None:
    """The local-only path (origin deleted the branch on merge) refuses the same way."""
    tip = box.phase_k()
    main = box.squash_merge()
    box.delete_on_merge(keep_tracking=False)
    other = _hold_elsewhere(box, tmp_path)
    err, _ = box.run(prs=[pr("MERGED", tip, merge=main)], prefetch=False, refused=True)
    assert box.head(other) == tip
    assert box.holders() == [str(other)]
    assert "(checked-out-elsewhere)" in err and "restarted from" not in err


def test_a_branch_taken_elsewhere_mid_restart_is_refused_and_torn_down(box: Box, tmp_path: Path) -> None:
    """Another worktree takes the branch during the gh call: the reset re-checks and refuses."""
    tip = box.phase_k()
    main = box.squash_merge()
    other = tmp_path / "late-wt"
    hook = f'git -C "{box.repo}" worktree add -q -f "{other}" {BRANCH} >/dev/null 2>&1\nrm -f "$GH_HOOK"\n'
    err, _ = box.run(prs=[pr("MERGED", tip, merge=main)], hook=hook, refused=True)
    assert box.head(other) == tip, "the reset must not move the late worktree's HEAD"
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip, "nothing pushed"
    assert box.holders() == [str(other)]
    assert "(checked-out-elsewhere)" in err and "restarted from" not in err


def test_an_unreadable_worktree_list_is_refused(box: Box) -> None:
    """Cannot prove the branch is free, so fail closed rather than risk moving it."""
    tip = box.phase_k()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    wrapper = box.bin / "git"
    wrapper.write_text(
        '#!/usr/bin/env bash\nfor a in "$@"; do [ "$a" = list ] && exit 128; done\nexec /usr/bin/git "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    try:
        err, _ = box.run(prs=[pr("OPEN", tip)], prefetch=False, refused=True)
    finally:
        wrapper.unlink()
    assert box.holders() == []
    assert err.count("WARNING") == 1 and "(worktree-list-unreadable)" in err


# --- A worktree mid-rebase / mid-bisect HOLDS the branch, though porcelain says `detached` --------
# git counts it as in use and writes the branch back on --continue, so moving the ref under it
# loses the rebase. Each shape runs twice: with the pre-check (clean message), and with the
# pre-check disabled, so git's own refusal of every `worktree add` form is what fails closed.

NO_PRECHECK = "branch_checked_out_elsewhere() { return 1; }"
PRECHECK = pytest.mark.parametrize("pre", ["", NO_PRECHECK], ids=["pre-check", "git-refusal-only"])


def _rebase_env(box: Box) -> dict[str, str]:
    return {**box.env, "GIT_SEQUENCE_EDITOR": "sed -i 1s/^pick/edit/", "GIT_EDITOR": "true"}


def _start_rebase(box: Box, wt: Path) -> None:
    """Stop `wt` mid `rebase -i` of BRANCH (edit on the first of its two commits)."""
    r = subprocess.run(["git", "rebase", "-i", "HEAD~2"], cwd=wt, env=_rebase_env(box), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (Path(box.git("rev-parse", "--absolute-git-dir", cwd=wt)) / "rebase-merge" / "head-name").exists()
    assert box.holders() == [], "the porcelain entry says `detached`: what the old check missed"


def _rebase_continues(box: Box, wt: Path) -> None:
    r = subprocess.run(["git", "rebase", "--continue"], cwd=wt, env=_rebase_env(box), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert box.branch_at(wt) == BRANCH, "the rebase wrote the branch back"


def _assert_refusal(err: str, pre: str, other: Path) -> None:
    assert err.count("WARNING") == 1, err
    if pre:
        assert "(git-refused)" in err and "already used by worktree" in err
    else:
        assert "(checked-out-elsewhere)" in err and str(other) in err
    assert "restarted from" not in err


@PRECHECK
def test_s1_other_worktree_mid_interactive_rebase_is_refused(box: Box, tmp_path: Path, pre: str) -> None:
    """S1 shape (local behind tracking) with the holder mid `rebase -i`: nothing moves, it can continue."""
    behind = box.phase_k()
    box.git("checkout", "-q", BRANCH)
    tip = box.commit("later.py", "L = 1\n", msg="later")
    box.git("push", "-q", "origin", BRANCH)
    box.git("fetch", "-q", "origin", cwd=box.repo)
    other = _hold_elsewhere(box, tmp_path, at=behind)
    _start_rebase(box, other)
    err, _ = box.run(prs=[pr("OPEN", tip)], prefetch=False, refused=True, pre=pre)
    _assert_refusal(err, pre, other)
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == behind, "branch ref moved"
    assert box.origin(f"refs/heads/{BRANCH}") == tip, "nothing pushed"
    _rebase_continues(box, other)
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == behind


@PRECHECK
def test_spent_with_origin_present_and_another_worktree_mid_rebase_is_refused_with_no_push(
    box: Box, tmp_path: Path, pre: str
) -> None:
    """Spent, origin still at the tip, the holder mid-rebase: refused before any reset or push."""
    tip = box.phase_k()
    main = box.squash_merge()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    other = _hold_elsewhere(box, tmp_path, at=f"origin/{BRANCH}")
    _start_rebase(box, other)
    err, _ = box.run(prs=[pr("MERGED", tip, merge=main)], prefetch=False, refused=True, pre=pre)
    _assert_refusal(err, pre, other)
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip, "NO push: origin unchanged"
    _rebase_continues(box, other)


def test_the_spent_reset_itself_is_refused_by_git_and_never_pushes(box: Box, tmp_path: Path) -> None:
    """A worktree starts a rebase of the branch DURING the gh call, with the pre-check disabled.

    Only the reset's own `worktree add -B` stands between it and the leased push, so this pins that
    the reset is a git-refused form and that the push happens only after it succeeded.
    """
    tip = box.phase_k()
    main = box.squash_merge()
    late = tmp_path / "late-wt"
    hook = (
        f'git -C "{box.repo}" worktree add -q -f "{late}" {BRANCH} >/dev/null 2>&1\n'
        f'GIT_SEQUENCE_EDITOR="sed -i 1s/^pick/edit/" git -C "{late}" rebase -q -i HEAD~2 >/dev/null 2>&1\n'
        'rm -f "$GH_HOOK"\n'
    )
    err, _ = box.run(prs=[pr("MERGED", tip, merge=main)], hook=hook, refused=True, pre=NO_PRECHECK)
    assert err.count("WARNING") == 1 and "(git-refused)" in err and "--no-track -B" in err
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == tip
    assert box.origin(f"refs/heads/{BRANCH}") == tip, "NO push: origin unchanged"
    assert "restarted from" not in err
    _rebase_continues(box, late)


@PRECHECK
def test_another_worktree_mid_bisect_of_the_branch_is_refused(box: Box, tmp_path: Path, pre: str) -> None:
    """BISECT_START records the SHORT branch name; the spent local-only path refuses it."""
    tip = box.phase_k()
    main = box.squash_merge()
    box.delete_on_merge(keep_tracking=False)
    other = _hold_elsewhere(box, tmp_path)
    box.git("bisect", "start", cwd=other)
    box.git("bisect", "bad", cwd=other)
    box.git("bisect", "good", main + "~1", cwd=other)
    assert box.holders() == []
    err, _ = box.run(prs=[pr("MERGED", tip, merge=main)], prefetch=False, refused=True, pre=pre)
    _assert_refusal(err, pre, other)
    assert box.git("rev-parse", f"refs/heads/{BRANCH}", cwd=box.repo) == tip
    box.git("bisect", "reset", cwd=other)
    assert box.branch_at(other) == BRANCH


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_worktree_state_counts_as_a_holder(box: Box, tmp_path: Path) -> None:
    """A rebase state we cannot read cannot prove the branch free: the pre-check refuses."""
    tip = box.phase_k()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    other = tmp_path / "other-wt"
    box.git("worktree", "add", "-q", "--detach", str(other), "main", cwd=box.repo)
    state = Path(box.git("rev-parse", "--absolute-git-dir", cwd=other)) / "rebase-merge"
    state.mkdir()
    (state / "head-name").write_text("refs/heads/something-else\n", encoding="utf-8")
    (state / "head-name").chmod(0)
    try:
        err, _ = box.run(prs=[pr("OPEN", tip)], prefetch=False, refused=True)
    finally:
        (state / "head-name").chmod(0o644)
    assert err.count("WARNING") == 1 and "(checked-out-elsewhere)" in err and str(other) in err


def test_a_stale_prunable_worktree_entry_is_not_a_refusal(box: Box, tmp_path: Path) -> None:
    """The holder's directory was deleted without a prune: listed `prunable`, never a holder for good."""
    tip = box.phase_k()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    gone = _hold_elsewhere(box, tmp_path, at=f"origin/{BRANCH}")
    shutil.rmtree(gone)
    assert "prunable" in box.git("worktree", "list", "--porcelain", cwd=box.repo)
    err, wt = box.run(prs=[pr("OPEN", tip)], prefetch=False)
    assert box.head(wt) == tip and box.branch_at(wt) == BRANCH
    assert "WARNING" not in err


def test_a_lease_refusal_attaches_and_never_overwrites_origin(box: Box, tmp_path: Path) -> None:
    """Origin moves AFTER the guard read it (the gh call is the window); the lease refuses."""
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
    assert box.upstream(wt) == f"origin/{BRANCH}"
    assert "leased push" in err and "refused" in err


def test_no_branch_anywhere_is_unchanged(box: Box) -> None:
    err, wt = box.run(prs=[])
    assert box.head(wt) == box.origin("refs/heads/main")
    assert "attaching" not in err and "spent" not in err


def test_the_heuristic_paths_are_gone() -> None:
    """The single rule only: no patch-id comparison, no post-merge replay."""
    body = "\n".join(
        line for line in GUARDS.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"\bgit\b[^\n]*\bcherry\b", body)
    assert "rebase -q --onto" not in body and "branch_reuse_plan" not in body


def test_guards_move_a_branch_only_through_forms_git_refuses_when_held() -> None:
    """`checkout -B`/`switch`/`reset` move a branch another worktree holds on git 2.43; keep them out.

    `update-ref` never checks either, so it may touch only a remote-tracking ref.
    """
    code = [line for line in GUARDS.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]
    movers = [line for line in code if re.search(r"\bgit\b.*\s(checkout|switch|reset)\s", line)]
    assert movers == []
    assert all("refs/remotes/" in line for line in code if re.search(r"\bgit\b.*\supdate-ref\s", line))


def test_no_grep_q_reads_a_pipe_in_guards() -> None:
    """Pin the here-string rule across the whole file, not just the cases above."""
    piped = [
        line
        for line in GUARDS.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and re.search(r"\|\s*grep\s+-q", line)
    ]
    assert piped == []


# --- agent_run.sh: the refusal is EX_SETUP, BEFORE the budget charge --------------------------------

AGENT_RUN = PIPELINE / "v2" / "actions" / "agent_run.sh"
LEDGER = PIPELINE / "lib" / "ledger.sh"

#: Stands in for v2/actions/common.sh up to the launch, with the REAL ledger and guards libraries;
#: only the trust walk and the branch flock are stubbed.
_STUB_COMMON = f"""
BASE="$TEST_BASE"; REPO="$TEST_REPO"; WORKROOT="$TEST_WORKROOT"; SLUG=o/r; ASSIGNEE=o
LOGDIR="$BASE/logs"; mkdir -p "$LOGDIR" "$BASE/locks"
log() {{ echo "LOG: $*"; }}
EX_TRUST=70; EX_BUDGET=71; EX_BUSY=72; EX_SETUP=73
DRY_RUN="${{DRY_RUN:-0}}"; RUNBOOK="$BASE/RUNBOOK.md"; V2_DIR="$BASE/v2"
. "{LEDGER}"
. "{GUARDS}"
v2_paused() {{ return 1; }}; v2_hold_present() {{ return 1; }}; v2_trust_ok() {{ return 0; }}
issue_for_pr() {{ :; }}; claim_branch() {{ return 0; }}
"""


def _agent_run(box: Box, tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the shipped agent_run.sh (DRY_RUN, so it stops right after the charge)."""
    actions = tmp_path / "actions"
    actions.mkdir()
    (actions / "agent_run.sh").write_text(AGENT_RUN.read_text(encoding="utf-8"), encoding="utf-8")
    (actions / "common.sh").write_text(_STUB_COMMON, encoding="utf-8")
    base = tmp_path / "base"
    env = {
        **box.env,
        "TEST_BASE": str(base),
        "TEST_REPO": str(box.repo),
        "TEST_WORKROOT": str(box.work),
        "DRY_RUN": "1",
    }
    r = subprocess.run(
        ["bash", str(actions / "agent_run.sh"), "fix", "pr", "4242", BRANCH],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return r, base / "state" / "ledger" / "pr-4242.tsv"


def test_agent_run_refuses_a_branch_held_elsewhere_with_ex_setup_before_charging(box: Box, tmp_path: Path) -> None:
    """A retry on the next observation (EX_SETUP), never a budget charge or an exhausted park."""
    tip = box.phase_k()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    other = _hold_elsewhere(box, tmp_path, at=f"origin/{BRANCH}")
    r, ledger = _agent_run(box, tmp_path)
    assert r.returncode == 73, r.stdout + r.stderr
    assert "before charging budget (EX_SETUP)" in r.stdout
    assert str(other) in r.stdout + r.stderr
    assert not ledger.exists(), "a refused dispatch must not consume a run"
    assert box.holders() == [str(other)] and box.head(other) == tip


def test_agent_run_charges_when_the_branch_is_free(box: Box, tmp_path: Path) -> None:
    """Guard the guard: the same harness reaches the charge when nothing else holds the branch."""
    box.phase_k()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    r, ledger = _agent_run(box, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert ledger.read_text(encoding="utf-8").split("\t")[:2] == ["fix", "1"]


def test_agent_run_prunes_a_deleted_worktree_before_the_pre_check(box: Box, tmp_path: Path) -> None:
    """A deleted worktree directory is pruned, so it can never hold the branch (and EX_SETUP) forever."""
    box.phase_k()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    gone = _hold_elsewhere(box, tmp_path, at=f"origin/{BRANCH}")
    shutil.rmtree(gone)
    assert "prunable" in box.git("worktree", "list", "--porcelain", cwd=box.repo)
    r, ledger = _agent_run(box, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert ledger.read_text(encoding="utf-8").split("\t")[:2] == ["fix", "1"]
    assert "prunable" not in box.git("worktree", "list", "--porcelain", cwd=box.repo)


def test_agent_run_refuses_a_branch_another_worktree_is_rebasing(box: Box, tmp_path: Path) -> None:
    """Mid-rebase reads `detached` in porcelain, yet it is EX_SETUP before the charge."""
    tip = box.phase_k()
    box.git("fetch", "-q", "origin", cwd=box.repo)
    other = _hold_elsewhere(box, tmp_path, at=f"origin/{BRANCH}")
    _start_rebase(box, other)
    r, ledger = _agent_run(box, tmp_path)
    assert r.returncode == 73, r.stdout + r.stderr
    assert str(other) in r.stdout + r.stderr
    assert not ledger.exists()
    _rebase_continues(box, other)
    assert box.head(other) == tip
