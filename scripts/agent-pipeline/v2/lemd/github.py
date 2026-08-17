"""The daemon's only door to GitHub — every read goes through here.

Shells out to `gh` rather than speaking HTTP: `gh` already resolves the App installation token from
`GH_TOKEN`, handles pagination and retries, and — more importantly — it is the same client v1 uses,
so a permission or auth change breaks both worlds identically instead of one silently.

Two rules this module enforces so callers cannot get them wrong:

* **Unreadable is never "fine".** Every accessor returns a sentinel the caller must handle, never a
  cheerful default. v1's most expensive incidents came from a failed read being indistinguishable
  from a healthy answer — an unreadable merge state that read as "not queued" produced 154
  re-enqueues, and an unreadable check rollup that read as "no failures" would merge red code.
* **Reads are bounded.** Every call has a timeout, because a hung `gh` inside the scheduler loop
  would stall the whole daemon, and the watchdog would then restart a daemon that was merely
  waiting on a socket.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

LOG = logging.getLogger("lemd.github")

#: Required contexts from branch protection. Mirrors tick.sh's REQUIRED_CHECKS_JQ deliberately —
#: verify with: gh api repos/:owner/:repo/branches/main/protection --jq '.required_status_checks.contexts'
REQUIRED_CHECKS = (
    "Unit Tests (Python 3.12)",
    "Integration Tests",
    "GitGuardian Scan",
    "UI Build",
    "Migration Versions",
    "CodeQL PR Quality Gate",
)

FAILED_CONCLUSIONS = frozenset({"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE"})
#: The ONLY conclusions that count as "this check is done and did not fail". Everything else —
#: PENDING/QUEUED/IN_PROGRESS, and equally the ones nobody thinks about (`ACTION_REQUIRED` on a
#: workflow awaiting approval, `STALE`) — counts as NOT REPORTED YET. Classifying by an explicit
#: pass list rather than by a pending list is the difference between an unknown conclusion holding
#: a PR and an unknown conclusion reading as green.
PASSED_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

#: Reviewer identities and the marker MODE=selfreview posts, pinned to the same strings tick.sh
#: uses (`COPILOT`, `CLAUDE_REVIEW_MARKER`). During migration both runners must agree on what counts
#: as a review, or v2's shadow decisions diverge from v1's for a reason that is not a defect.
COPILOT_LOGIN = "copilot-pull-request-reviewer"
CLAUDE_REVIEW_MARKER = "🔎 Claude adversarial review"
#: The ASCII half of the marker — what detection keys on. The decorated marker opens with a non-BMP
#: character that does not survive the round-trip through the agent posting it (it lands as U+FFFD),
#: so matching the whole marker finds nothing and the agent re-reviews forever: five identical
#: comments on PR #1273. The phrase must still OPEN the comment — an unbounded search would count a
#: Decision Comment that merely mentions the review (which a selfreview escalation posts INSTEAD of
#: a marker) as evidence the review happened. Leading non-letters are stripped first: exactly the
#: room the decoration needs (`🔎`, its four U+FFFD, `#`, `**`), none for a word in front of it.
CLAUDE_REVIEW_MARKER_TEXT = "Claude adversarial review"

#: Every text that counts as this pipeline's own review evidence, undecorated.
#:
#: Two entries because the dispatcher and this gate drifted apart and nothing noticed for a day.
#: `agent_run.sh` prompts MODE=selfreview with `MARKER='🤖 Claude self-review'` whenever
#: `CLAUDE_REVIEW_MARKER` is unset — and it IS unset, because that variable lives in v1's `tick.sh`
#: and never reaches a v2 action's environment. So every v2 self-review posted a marker this gate
#: could not see: `review_fresh` stayed False no matter how many passed, `decide` re-dispatched
#: selfreview until the budget of 2 was spent, and the PR parked `selfreview_exhausted`. Un-parking
#: reset the ledger and bought exactly two more runs, so it was a treadmill, not progress — seven
#: open PRs were on it, one of them holding a PASSING self-review posted 13 seconds after its head.
#:
#: The self-review text stays accepted rather than being retired: it IS this pipeline's review
#: evidence, produced by the lane the gate is asking about, and dropping it would throw away the
#: markers already sitting on those seven PRs.
REVIEW_MARKER_TEXTS = (CLAUDE_REVIEW_MARKER_TEXT, "Claude self-review")
MARKER_DECORATION_RE = re.compile(r"^[^A-Za-z]*")

#: The phase-scope declaration MODE=selfreview writes, and MODE=phasefix clears (#1396).
#:
#: v2 deliberately has NO gate that judges acceptance-criteria coverage from a diff — reading a diff
#: and concluding "this closes an issue whose later phase is untracked" is exactly the call an LLM
#: gets confidently wrong, and a wrong hold costs a human decision every time it fires. So the
#: JUDGEMENT stays where it already happens with the full context (the self-review pass, which has
#: read the issue, the diff and the tests), and what lands here is only that verdict's residue.
#:
#: Detection keys on the ASCII phrase, not on the decoration, for the reason `CLAUDE_REVIEW_MARKER`
#: does: a non-BMP character does not survive the round-trip through the agent that posts it. It is
#: NOT anchored to the start of the comment — the declaration rides inside the review marker comment
#: rather than replacing it, because a self-review that found a scope gap has usually found other
#: findings too, and posting two comments to say one thing invites exactly half of it being posted.
#:
#: Fail-OPEN: no declaration means no hold. A PR nobody said anything about merges as it does today.
PHASE_GAP_MARKER = "🧩 phase-gap"
PHASE_GAP_OPEN_RE = re.compile(r"phase-gap:\s*(?!cleared\b|none\b|ok\b)\S", re.I)
#: What retires a declaration. Written by MODE=phasefix once the follow-up is filed and linked (or
#: the closing keyword dropped) — never by time passing and never by a new commit: the gap is in the
#: PR's SCOPE claim, which a push does not touch.
#:
#: ANCHORED TO A LINE, and the asymmetry with the open pattern above is the point. The two mistakes
#: do not cost the same: a comment that merely TALKS about this mechanism and is read as a
#: declaration causes one phasefix run, which finds nothing and clears itself (`phasefix.md` step 4
#: posts the clearing line unconditionally). A comment that merely talks about it and is read as a
#: CLEARING retires a real hold, and the PR merges with the scope lost — #548's failure, which is
#: the one this whole mechanism exists to stop. Prose quotes it mid-sentence ("...clears the
#: declaration with `🧩 phase-gap: cleared`" is how three docs in this repo phrase it); it does not
#: put it at the head of a line. `re.M` so a clearing comment may still carry a heading above it —
#: agents write those, and requiring the whole comment to start with the phrase would wedge the lane.
PHASE_GAP_CLEARED_RE = re.compile(r"^[^A-Za-z\n]*phase-gap:\s*(?:cleared|none|ok)\b", re.I | re.M)


class GitHubUnavailable(RuntimeError):
    """A read failed. Callers must treat this as "I do not know", never as "nothing to do"."""


@dataclass(frozen=True)
class ChecksState:
    """The required-check rollup for one head."""

    failed: int
    pending: int
    total: int
    names_failed: tuple[str, ...] = field(default=())
    names_pending: tuple[str, ...] = field(default=())

    @property
    def green(self) -> bool:
        """True only when every required check reported and none failed.

        `total == 0` is deliberately NOT green: a head with no required checks yet is one where CI
        has not started, and treating that as success would merge unbuilt code.
        """
        return self.total > 0 and self.failed == 0 and self.pending == 0


def run_gh(args: list[str], *, timeout: int = 30) -> str:
    """Run a `gh` command and return stdout.

    Raises:
        GitHubUnavailable: On non-zero exit, timeout, or a missing binary — every failure mode
            collapses to one exception so no caller can accidentally treat stderr as data.
    """
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise GitHubUnavailable(f"gh {' '.join(args[:3])}: {exc}") from exc
    if proc.returncode != 0:
        raise GitHubUnavailable(f"gh {' '.join(args[:3])} rc={proc.returncode}: {proc.stderr[:200]}")
    return proc.stdout


def gh_json(args: list[str], *, timeout: int = 30) -> Any:
    """Run a `gh` command expected to emit JSON."""
    raw = run_gh(args, timeout=timeout)
    try:
        return json.loads(raw or "null")
    except json.JSONDecodeError as exc:
        raise GitHubUnavailable(f"gh {' '.join(args[:3])}: unparseable JSON ({exc})") from exc


def checks_for(slug: str, pr: int, *, timeout: int = 30) -> ChecksState:
    """Rollup of the REQUIRED checks only.

    Non-required noise (CodeQL Security Analysis, E2E, the docstring gate) is filtered out for the
    same reason v1 filters it: those can be red on a PR that is legitimately mergeable, and gating
    on them would park healthy work.

    A required context that is ABSENT from the rollup counts as pending, not as absent-and-therefore
    fine. Every workflow behind the six required contexts triggers unconditionally on
    `pull_request`, so a head listing only three of them is a head where the other three have not
    been created yet — and `total > 0 and failed == 0 and pending == 0` would call that green and
    arm auto-merge before CI had reported. The `total == 0` guard alone only catches the instant
    before the FIRST check appears.
    """
    data = gh_json(
        ["pr", "view", str(pr), "--repo", slug, "--json", "statusCheckRollup"], timeout=timeout
    )
    rollup = (data or {}).get("statusCheckRollup") or []
    failed: list[str] = []
    pending: list[str] = []
    reported: set[str] = set()
    for c in rollup:
        name = c.get("name") or c.get("context") or ""
        if name not in REQUIRED_CHECKS:
            continue
        reported.add(name)
        state = (c.get("conclusion") or c.get("state") or "").upper()
        if state in FAILED_CONCLUSIONS:
            failed.append(name)
        elif state not in PASSED_CONCLUSIONS:
            pending.append(name)
    pending.extend(n for n in REQUIRED_CHECKS if n not in reported)
    return ChecksState(
        failed=len(failed),
        pending=len(pending),
        total=len(reported),
        names_failed=tuple(sorted(failed)),
        names_pending=tuple(sorted(pending)),
    )


def pr_facts(slug: str, pr: int, *, timeout: int = 30) -> dict[str, Any]:
    """The PR fields the state machine reads, in one call."""
    # `autoMergeRequest` is load-bearing, not diagnostic: without it the state machine cannot tell
    # "this PR needs auto-merge armed" from "this PR is armed and waiting", and re-decides the
    # former every pass. That is the #1120 shape — 45 enqueue requests against a budget of 12 —
    # reproduced in a scheduler built to prevent it.
    fields = (
        "number,state,isDraft,mergeStateStatus,headRefName,headRefOid,labels,author,"
        "headRepositoryOwner,updatedAt,mergedAt,autoMergeRequest"
    )
    return gh_json(
        ["pr", "view", str(pr), "--repo", slug, "--json", fields],
        timeout=timeout,
    ) or {}


def branch_exists(slug: str, branch: str, *, timeout: int = 30) -> bool | None:
    """Does this branch exist on the REMOTE?

    Returns:
        True / False, or None when the answer could not be read.

    Deliberately asks GitHub rather than the local checkout. A `start` run works in its own worktree
    and the answer that decides whether it accomplished anything is "did the work leave this box" —
    a local branch proves only that `git checkout -b` ran, which every attempt does before it does
    anything useful. The three-valued return matters as much: an unreadable answer must not read as
    "nothing was produced", because that is the reading that re-dispatches a 12-minute run on top of
    work that already exists.
    """
    owner, _, name = slug.partition("/")
    try:
        gh_json(["api", f"repos/{owner}/{name}/git/ref/heads/{branch}"], timeout=timeout)
        return True
    except GitHubUnavailable as exc:
        # `gh api` exits non-zero for BOTH "no such ref" and "GitHub is unreachable", and those are
        # opposite answers here. 404 is a real, readable NO; anything else is a shrug.
        if "404" in str(exc) or "Not Found" in str(exc):
            return False
        LOG.warning("branch %s existence unreadable: %s", branch, exc)
        return None


def open_pr_for_branch(slug: str, branch: str, *, timeout: int = 30) -> bool | None:
    """Is there an OPEN pull request whose head is this branch?

    The question `_work_exists_for_issue` could not previously ask, and the difference between two
    states it was collapsing: a branch WITH an open PR is work in flight and re-dispatching would
    fork it, while a branch with NO PR is a run that pushed and then died before opening one. The
    second is resumable, and treating it as the first is what left four issues waiting for ever.

    Returns:
        True / False, or None when the answer could not be read — which must never read as False,
        because that is the reading that re-dispatches on top of a live PR.
    """
    try:
        rows = gh_json(
            ["pr", "list", "--repo", slug, "--head", branch, "--state", "open",
             "--json", "number"],
            timeout=timeout,
        )
    except GitHubUnavailable as exc:
        LOG.warning("open-PR lookup for %s unreadable: %s", branch, exc)
        return None
    return bool(rows)


def linked_pr_state(slug: str, number: int, *, timeout: int = 30) -> str | None:
    """The state of the NEWEST pull request GitHub says will close this issue.

    `closedByPullRequestsReferences` carries id/number/repository/url and no `state`, so telling an
    open PR from a merged or a closed-unmerged one costs a second read. That is the whole cost of
    this function, and it is why callers gate it on linkage already existing: an issue with nothing
    linked never reaches the `pr view`.

    NEWEST, by PR number, because an issue can accumulate refs — #1091 carries both #1592 and
    #1597 — and only the most recent one describes where the work stands now. An older merged ref
    under a live PR must not read as "shipped".

    Returns:
        `OPEN` / `MERGED` / `CLOSED`; `""` when the linkage was READ and there is nothing linked;
        None when either read failed or the newest ref carries no usable number. `""` and None are
        the same DECISION — neither licenses a park, both wait — and are separate answers anyway
        because they are different READS: `""` tells the caller the refs question is already
        answered, which is what lets one linkage read serve all three of the questions
        `snapshot_issue` asks instead of three.
    """
    try:
        linked = gh_json(
            ["issue", "view", str(number), "--repo", slug, "--json",
             "closedByPullRequestsReferences"],
            timeout=timeout,
        ) or {}
    except GitHubUnavailable as exc:
        LOG.warning("issue #%s PR linkage unreadable: %s", number, exc)
        return None
    raw = linked.get("closedByPullRequestsReferences") or []
    if not raw:
        # READ and empty — not the same answer as a read that failed, even though both wait.
        return ""
    refs = [r for r in raw if isinstance(r, dict) and r.get("number")]
    if not refs:
        # Linked to SOMETHING this cannot address. Not `""`: that would tell the caller there is no
        # linkage, and the caller would then license a re-dispatch off the branch convention alone.
        LOG.warning("issue #%s has %d linked PR ref(s) with no usable number", number, len(raw))
        return None
    newest = max(refs, key=lambda r: int(r["number"]))
    # The ref names its own repository, so a cross-repo link is read where it actually lives rather
    # than looked up under this repo's slug, where the number would resolve to a different PR.
    repo = newest.get("repository") or {}
    owner = ((repo.get("owner") or {}).get("login") or "").strip()
    name = (repo.get("name") or "").strip()
    ref_slug = f"{owner}/{name}" if owner and name else slug
    try:
        facts = gh_json(
            ["pr", "view", str(newest["number"]), "--repo", ref_slug, "--json", "state"],
            timeout=timeout,
        ) or {}
    except GitHubUnavailable as exc:
        LOG.warning("linked PR #%s state unreadable: %s", newest["number"], exc)
        return None
    return (facts.get("state") or "").upper() or None


def merge_queue_state(slug: str, pr: int, *, timeout: int = 30) -> str:
    """The PR's merge-queue entry state, or "" when it holds no entry.

    An empty string means "no live entry" — NOT "unknown". A failed read raises instead, because
    v1's #1082 incident was precisely an unreadable queue state being read as a healthy one.
    """
    owner, _, name = slug.partition("/")
    query = (
        "query=query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){"
        "pullRequest(number:$p){mergeQueueEntry{state}}}}"
    )
    out = run_gh(
        [
            "api", "graphql", "-f", query,
            "-f", f"o={owner}", "-f", f"n={name}", "-F", f"p={pr}",
            "--jq", ".data.repository.pullRequest.mergeQueueEntry.state // \"\"",
        ],
        timeout=timeout,
    )
    return out.strip()


@dataclass(frozen=True)
class ReviewState:
    """A PR's review evidence: is there a review of the CURRENT head, and what is still unresolved."""

    fresh: bool
    unresolved: int
    reviewed_at: str = ""
    head_at: str = ""
    #: An OPEN phase-scope declaration from the self-review pass (#1396) — this PR closes an issue
    #: whose remaining scope is not tracked anywhere. Defaults False so every existing caller, and
    #: every PR predating the convention, behaves exactly as before.
    phase_gap: bool = False


def _epoch(value: str | None) -> float:
    """ISO-8601 to epoch seconds; 0.0 for absent or unparseable — i.e. "no evidence"."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


_REVIEW_QUERY = (
    "query=query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){"
    "pullRequest(number:$p){"
    "commits(last:1){nodes{commit{committedDate}}}"
    "reviews(last:20){nodes{submittedAt author{login}}}"
    # 100 (the connection maximum), not a smaller window: the marker is an ISSUE comment, so on a
    # PR with a long discussion a short window scrolls the review evidence out of sight and the
    # selfreview lane re-dispatches on a PR that was already reviewed.
    "comments(last:100){nodes{createdAt body}}"
    "reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{author{login}}}}}"
    "}}}"
)


def review_state(slug: str, pr: int, *, timeout: int = 30) -> ReviewState:
    """Whether this PR carries review evidence for its current head, and its unresolved-thread count.

    Both facts in one GraphQL call because both are read on every observation of every open PR, and
    they are the two the merge gate actually turns on.

    Freshness is STRICTER than v1's `claude_marker_fresh_p`, which accepts a marker older than the
    head once the head has aged past `REVIEW_GRACE_SECONDS`. Here a review must be at or after the
    head commit, full stop: being wrong in this direction costs one extra selfreview run, being
    wrong in the other direction merges code no reviewer ever saw. A head with no readable commit
    date falls back to "any review counts", because refusing every PR when a date is unreadable
    would wedge the whole gate.

    Only COPILOT-authored threads count as unresolved, mirroring `copilot_unresolved_threads`: a
    human's unresolved nit is not something MODE=review can resolve, so counting it would dispatch
    that lane forever.

    The phase-scope declaration (#1396) is read from the SAME comment list, because it is written
    into the same comment: the self-review marker. Reading it here costs nothing extra, and putting
    it anywhere else would mean a second GraphQL call per observation of every open PR.
    """
    owner, _, name = slug.partition("/")
    data = gh_json(
        ["api", "graphql", "-f", _REVIEW_QUERY,
         "-f", f"o={owner}", "-f", f"n={name}", "-F", f"p={pr}"],
        timeout=timeout,
    ) or {}
    node = (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {})

    commits = ((node.get("commits") or {}).get("nodes") or [])
    head_at = ""
    if commits:
        head_at = ((commits[-1] or {}).get("commit") or {}).get("committedDate") or ""

    stamps: list[str] = []
    phase_gap = False
    for review in ((node.get("reviews") or {}).get("nodes") or []):
        login = ((review or {}).get("author") or {}).get("login") or ""
        if COPILOT_LOGIN in login.lower() and review.get("submittedAt"):
            stamps.append(review["submittedAt"])
    for comment in ((node.get("comments") or {}).get("nodes") or []):
        body = (comment or {}).get("body") or ""
        undecorated = MARKER_DECORATION_RE.sub("", body)
        if undecorated.startswith(REVIEW_MARKER_TEXTS) and comment.get("createdAt"):
            stamps.append(comment["createdAt"])
        # LAST declaration wins, so the walk does not break early: `comments(last:100)` arrives
        # oldest-first, and the thing being asked is "is the gap still open NOW". A `cleared` line
        # that could not overwrite an earlier `gap` line would wedge the PR in the phasefix lane
        # until its budget parked it to the owner — the failure v1's `phasefix_clear` had to have.
        if PHASE_GAP_CLEARED_RE.search(body):
            phase_gap = False
        elif PHASE_GAP_OPEN_RE.search(body):
            phase_gap = True
    reviewed_at = max(stamps, key=_epoch) if stamps else ""

    unresolved = 0
    for thread in ((node.get("reviewThreads") or {}).get("nodes") or []):
        if (thread or {}).get("isResolved"):
            continue
        first = ((thread.get("comments") or {}).get("nodes") or [{}])[0] or {}
        login = (first.get("author") or {}).get("login") or ""
        if COPILOT_LOGIN in login.lower():
            unresolved += 1

    fresh = bool(reviewed_at) and (not head_at or _epoch(reviewed_at) >= _epoch(head_at))
    return ReviewState(fresh=fresh, unresolved=unresolved, reviewed_at=reviewed_at,
                       head_at=head_at, phase_gap=phase_gap)


def list_by_label(slug: str, label: str, kind: str = "pr", *, limit: int = 100,
                  timeout: int = 30) -> list[dict[str, Any]]:
    """Open issues or PRs carrying a label. The reconciler's whole input."""
    cmd = "pr" if kind == "pr" else "issue"
    fields = "number,labels,updatedAt" + (",headRefName,headRefOid,isDraft" if kind == "pr" else "")
    return gh_json(
        [cmd, "list", "--repo", slug, "--state", "open", "--label", label,
         "--limit", str(limit), "--json", fields],
        timeout=timeout,
    ) or []


def label_names(obj: dict[str, Any]) -> set[str]:
    """Label names from a `gh --json labels` payload."""
    return {ll.get("name", "") for ll in (obj.get("labels") or [])}


def post_comment(slug: str, kind: str, number: int, body: str, *, timeout: int = 30) -> None:
    """Post one comment. The only write in this module that is not a merge/label action.

    Raises `GitHubUnavailable` like every other call here; callers that must not let a notification
    failure take down an observation pass (there is nothing to retry into) catch it themselves.
    """
    cmd = "pr" if kind == "pr" else "issue"
    run_gh([cmd, "comment", str(number), "--repo", slug, "--body", body], timeout=timeout)


def is_upstream(facts: dict[str, Any], slug: str) -> bool:
    """True when the PR's head branch lives in THIS repository, not a fork.

    Fail-closed: an unreadable owner returns False. A fork PR that reads as upstream would let
    attacker-controlled code reach a lane that checks out and runs it.
    """
    owner = (facts.get("headRepositoryOwner") or {}).get("login")
    if not owner:
        return False
    return owner.lower() == slug.partition("/")[0].lower()
