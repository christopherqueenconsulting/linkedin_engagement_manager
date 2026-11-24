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

    def branch_at(self, wt: Path) -> str:
        return self.git("symbolic-ref", "--short", "HEAD", cwd=wt)

    def upstream(self, wt: Path) -> str | None:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=wt, env=self.env, capture_output=True, text=True
        )
        return r.stdout.strip() if r.returncode == 0 else None

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


def test_no_grep_q_reads_a_pipe_in_guards() -> None:
    """Pin the here-string rule across the whole file, not just the cases above."""
    piped = [
        line
        for line in GUARDS.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and re.search(r"\|\s*grep\s+-q", line)
    ]
    assert piped == []
