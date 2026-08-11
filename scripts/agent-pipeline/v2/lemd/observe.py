"""The state machine: GitHub facts in, one decision out.

Split deliberately into a PURE function (`decide`) and an impure wrapper (`observe`). Everything
worth arguing about lives in `decide`, which takes a plain snapshot and returns a `Decision` — no
network, no clock, no database — so every transition is testable exactly, including the ones that
only happen when GitHub is lying or unreachable. `observe` does the I/O and applies the result.

That split is also what makes the migration's acceptance criterion possible: v1's per-tick observed
inputs can be replayed through `decide` offline and compared, without a daemon or a webhook.

The rules that shaped this, each from a measured v1 failure:

* **Unreadable is a decision to do NOTHING**, never a decision to proceed. v1 merged on an
  unreadable state once (#1082) and re-enqueued 154 times.
* **Waiting is a state, not a poll.** Every terminal-ish answer sets a `wake_at`, so the item leaves
  the scheduler's attention entirely until an event or that deadline.
* **Nothing dispatches without a fresh trust check.** Admission happens here; the actual dispatch
  re-verifies against the timeline, because webhook ordering is not guaranteed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import answers, db, github

LOG = logging.getLogger("lemd.observe")

# Actions a decision can ask for. The scheduler owns HOW; this module owns WHETHER and WHICH.
ACT_NONE = "none"
ACT_DISPATCH = "dispatch"        # run an agent in some MODE
ACT_MERGE = "merge"              # gh-only: arm auto-merge
ACT_UNPARK = "unpark"            # gh-only: the owner answered — release the hold
ACT_DISARM = "disarm"            # gh-only: a hold appeared on an armed PR — take the arm off
ACT_CLOSE = "close"              # terminal bookkeeping

# There is deliberately no ACT_PARK. Escalation to the owner is not a decision this function makes —
# it happens at DISPATCH, when `act()` finds the ledger spent and queues `park.sh` via `_park()`.
# An `ACT_PARK` constant existed here for months and was never returned, while three branches wrote
# `state=parked` through ACT_NONE and produced no comment, no label and no disarm. If `decide()`
# ever needs to escalate, add it back and wire `_observe_one` to it in the same change.


@dataclass(frozen=True)
class Snapshot:
    """Everything `decide` is allowed to know. Built by `observe`, or by a replay harness."""

    kind: str                       # 'issue' | 'pr'
    number: int
    labels: frozenset[str] = frozenset()
    state: str = "OPEN"             # GitHub's OPEN/CLOSED/MERGED
    is_draft: bool = False
    upstream: bool = True
    branch: str | None = None
    head_sha: str | None = None
    merge_state: str = ""           # GitHub mergeStateStatus
    queue_state: str = ""           # mergeQueueEntry state, "" = no entry
    auto_merge: bool = False        # auto-merge already armed on this PR
    checks: github.ChecksState | None = None
    review_fresh: bool = False      # a review marker at or after the current head
    unresolved_threads: int = 0
    #: For an ISSUE: has a previous run already produced a branch or a PR? Three-valued, and the
    #: third value is the point — `None` means "could not tell", which must never be read as "no".
    work_exists: bool | None = None
    #: For an ISSUE whose work exists: is that work an OPEN PR? Three-valued for the same reason as
    #: `work_exists`. `True` = in flight, leave it alone. `False` = a branch with no PR behind it —
    #: a run that pushed and then died, which is RESUMABLE, not in flight. `None` = could not tell,
    #: which waits.
    has_open_pr: bool | None = None
    #: The owner's newest reply to the latest Decision Comment, when this item is held. Read ONLY
    #: for held items — every other decision is reached without the extra call.
    answer: answers.Answer | None = None
    #: The answer id this item was already un-parked on, from `items.last_comment_id`. What stops
    #: one reply being routed twice: after a successful un-park the labels are gone, but a park that
    #: lands again later must not re-route on the answer to the PREVIOUS question.
    answer_routed: str | None = None
    readable: bool = True           # False when any required read failed


@dataclass(frozen=True)
class Decision:
    """What to do, what state to move to, and why — the 'why' is telemetry, not decoration."""

    action: str
    next_state: str
    reason: str
    mode: str | None = None         # for ACT_DISPATCH
    wait_reason: str | None = None
    wake_in: int | None = None      # seconds; None = no TTL (event-only)
    park_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


#: Human holds. An item carrying either is the owner's, not the pipeline's — v1 learned this when a
#: green PR parked with `needs-human` (but still labelled `agent:working`) was merged on the next tick.
HOLD_LABELS = frozenset({"needs-human", "agent:blocked"})

#: GitHub's `mergeStateStatus`, every value named. Naming them is the point: an unnamed value falls
#: through to the checks ladder, and for `UNKNOWN` that is the #1082 shape — GitHub computes
#: mergeability asynchronously, so an unreadable field was being read as a healthy one.
#:
#: `BEHIND` proceeds deliberately. `main` does NOT require branches to be up to date
#: (`required_status_checks.strict` is false, checked 2026-08-11) and the merge queue builds against
#: the queue head, so being behind is self-healing and dispatching an agent to rebase would spend a
#: model session on something GitHub does for free. `UNSTABLE` proceeds because `checks_for` filters
#: to REQUIRED contexts, so a red non-required check is genuinely mergeable. `BLOCKED` proceeds
#: because it is the normal state of a PR waiting on a required check, and the ladder below reads
#: those directly.
MERGE_STATE_PROCEED = frozenset({"CLEAN", "BLOCKED", "UNSTABLE", "BEHIND", "HAS_HOOKS"})

#: Mergeability GitHub has not finished computing. Not a state to act on.
MERGE_STATE_UNREADABLE = frozenset({"UNKNOWN", ""})

#: Issue labels whose decision turns on "has a previous run left anything behind?". Kept next to
#: `decide` rather than inside `snapshot_issue` so the two cannot drift: a label added to one
#: branch of the state machine and not to this set silently gets `work_exists=None`, which reads as
#: "unreadable" and parks the item in a wait state for ever.
WORK_SENSITIVE_LABELS = frozenset({"agent:ready", "agent:working"})


def admissible(snap: Snapshot) -> tuple[bool, str]:
    """Is this item the pipeline's to act on at all?

    Admission is by LABEL and PROVENANCE, never by author. Excluding "dependabot PRs" by author
    would silently retire the depfix lane, which exists precisely to run an agent ON a Dependabot
    PR — the router workflow only applies the label, the agent does the fix.
    """
    if not snap.readable:
        return False, "unreadable"
    if snap.kind == "pr" and not snap.upstream:
        return False, "fork_pr"
    # Release-please PRs are excluded by BRANCH, not author: arming auto-merge on one would bypass
    # the owner's 4x-daily release windows entirely.
    if snap.branch and snap.branch.startswith("release-please--"):
        return False, "release_pr"
    if "autorelease: pending" in snap.labels:
        return False, "release_pr"
    if not any(ll.startswith("agent:") for ll in snap.labels):
        # ...unless it is HELD. An item carrying a hold is one the pipeline parked, and the only
        # decision available for it is the answer branch below — which is the one thing that can
        # ever release it. Refusing admission here instead made a park unanswerable whenever the
        # `agent:*` label went with it: issues #1284 and #1285 hold `needs-human` and nothing else,
        # so an owner reply on either would have been read, classified, and then discarded one
        # branch later. Admission is not authorisation — `v2_trust_ok` still gates the action.
        if not (snap.labels & HOLD_LABELS):
            return False, "no_agent_label"
    return True, "ok"


def _work_in_flight_or_stranded(snap: Snapshot, ttl_review: int,
                                reason: str = "start_already_produced_work") -> Decision:
    """An issue that has already produced work: is that work MOVING, or abandoned?

    `work_exists` answers "did anything leave the box", which is the right question for "must I
    avoid forking this" and the wrong one for "will anyone ever finish it". A branch with an open PR
    is in flight and the PR carries the item from here. A branch with NO PR is a `start` that pushed
    and then died before opening one — most often because the run was killed, which every daemon
    restart can do.

    That second state used to return the same wait as the first, and the wait never ended:
    `work_exists` re-read the same branch every `ttl_review` and answered True for ever. Measured
    2026-08-11: issues #1270, #1279, #1282 and #1287 each held a pushed branch, no PR, and no
    prospect of either — two of them orphaned by daemon restarts earlier that night.

    Re-dispatching `start` RESUMES: the worktree is created on the existing branch, so the agent
    continues rather than starting over. And it is bounded by the `start` budget like any other
    dispatch, so a branch that cannot be finished parks and ASKS instead of waiting silently.
    """
    if snap.has_open_pr or snap.has_open_pr is None:
        # In flight, or unreadable. Unreadable waits: re-dispatching onto a live PR forks the work,
        # and one more TTL costs nothing by comparison.
        return Decision(ACT_NONE, db.STATE_WAIT_REVIEW, reason,
                        wait_reason="work_in_flight", wake_in=ttl_review)
    return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "stranded_branch_no_pr", mode="start",
                    details={"branch": snap.branch or ""})


def decide(snap: Snapshot, *, ttl_ci: int, ttl_review: int, ttl_queue: int,
           ttl_parked: int) -> Decision:
    """Pure state-machine step. No I/O, no clock — every input is in `snap`.

    Returns the single next action for this item. Order matters and encodes priority: terminal
    facts first, then human holds, then blockers, then progress.
    """
    # ---- terminal facts win over everything, including holds -------------------------------
    if snap.state == "MERGED":
        return Decision(ACT_CLOSE, db.STATE_MERGED, "merged")
    if snap.state == "CLOSED":
        return Decision(ACT_CLOSE, db.STATE_CLOSED, "closed_unmerged")

    # ---- an unreadable snapshot is never a licence to act ------------------------------------
    if not snap.readable:
        # Short TTL: this is "ask again soon", not a wait state with a real event behind it.
        return Decision(ACT_NONE, db.STATE_WAIT_CI, "github_unreadable",
                        wait_reason="unreadable", wake_in=300)

    ok, why = admissible(snap)
    if not ok:
        # IGNORED, not parked. Nobody was asked anything: `decide()` returns ACT_NONE here, so no
        # action runs, no comment is posted, no label is written and no auto-merge is disarmed.
        # Writing `parked` claimed an escalation that had not happened — and for a fork or a
        # release PR the right answer really is silence, since neither is ours to comment on.
        return Decision(ACT_NONE, db.STATE_IGNORED, f"not_admissible:{why}",
                        park_reason=why, wake_in=None)

    # ---- the owner's holds outrank every lane -------------------------------------------------
    if snap.labels & HOLD_LABELS:
        # ...but an ANSWER is the owner lifting their own hold, and it is the only thing that can.
        # Nothing else in v2 clears a hold label, so without this branch every park is permanent:
        # `park.sh` promised un-parking happened "through the existing answer lane" and that lane
        # was never ported off v1, which now stands down whenever the daemon heartbeat is fresh.
        # SAFETY FIRST, above the answer. Only `park.sh` ever ran `--disable-auto`, so a hold a
        # HUMAN applied — the natural "stop, I want to look at this" gesture — was honoured by this
        # function and ignored by GitHub, which merged the PR the moment its gate cleared. The
        # daemon held and the merge happened anyway.
        #
        # This sits above the answer branch deliberately. Un-parking one observation later costs a
        # single pass; leaving an armed hold costs a merge nobody authorised, and it cannot be
        # undone. `disarm.sh` re-checks that the hold is still present, so a hold removed in the
        # meantime is not something we take the arm off for.
        if snap.kind == "pr" and snap.auto_merge:
            return Decision(ACT_DISARM, db.STATE_PARKED, "human_hold_armed", mode="disarm",
                            park_reason="needs_human")
        ans = snap.answer
        if ans is not None:
            if not ans.actionable:
                # `hold` and `question` are recognised so they can be REFUSED by name. Ambiguity
                # never starts a build — an owner who leads with `1B` and then writes "but don't
                # merge until Friday" has not said go.
                return Decision(ACT_NONE, db.STATE_PARKED, f"human_hold:{ans.verdict}",
                                park_reason="needs_human", wake_in=ttl_parked,
                                details={"answer": ans.excerpt})
            if ans.comment_id and ans.comment_id == snap.answer_routed:
                # Already acted on. Reached when an un-park succeeded and the item was parked again
                # later for a NEW reason: the old reply is still the newest comment in the thread,
                # and routing on it would answer a question nobody asked.
                return Decision(ACT_NONE, db.STATE_PARKED, "human_hold:answer_already_routed",
                                park_reason="needs_human", wake_in=ttl_parked)
            return Decision(ACT_UNPARK, db.STATE_READY, "owner_answered", mode="unpark",
                            details={"verdict": ans.verdict, "answer": ans.excerpt})
        # Event-driven (an owner comment or a label removal), with a slow safety re-check so a
        # missed webhook costs hours, not forever.
        return Decision(ACT_NONE, db.STATE_PARKED, "human_hold",
                        park_reason="needs_human", wake_in=ttl_parked)

    if snap.kind == "issue":
        if "agent:ready" in snap.labels:
            # A `start` is only a start when there is nothing to start FROM. If a previous run
            # already pushed a branch or opened a PR, this issue is in flight — re-dispatching it
            # spends another 9-12 minute model session redoing work that exists. Measured: #1290
            # finished rc=0 at 05:43:27 and was re-dispatched at 05:43:34, seven seconds later.
            #
            # `None` (unreadable) is treated as "work may exist" and WAITS. That asymmetry is
            # deliberate: waiting on a false positive costs one TTL, while re-dispatching on a false
            # negative costs a full model session and risks two agents on one branch. An agent that
            # pushed a branch and died before opening a PR is work in progress, not a blank slate.
            if snap.work_exists is None:
                return Decision(ACT_NONE, db.STATE_WAIT_CI, "start_work_state_unreadable",
                                wait_reason="unreadable", wake_in=600)
            if snap.work_exists:
                return _work_in_flight_or_stranded(snap, ttl_review)
            return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "issue_ready", mode="start")
        if "agent:working" in snap.labels:
            # A claim with nothing behind it. v1 marked the ISSUE `agent:working` for the duration
            # of a run and removed it at the end, so a run that died mid-flight — or a cutover that
            # retired the runner underneath it — leaves the label set for ever. Nothing else
            # releases it: the label is on GitHub, not in the queue, so `startup_recover` never
            # sees it.
            #
            # `work_exists` is the whole test, and it is the same three-valued question the ready
            # path asks, with the same asymmetry. Unreadable WAITS rather than restarting, because
            # the cost of guessing wrong here is two agents on one branch.
            if snap.work_exists is None:
                return Decision(ACT_NONE, db.STATE_WAIT_CI, "working_claim_state_unreadable",
                                wait_reason="unreadable", wake_in=600)
            if snap.work_exists:
                # The branch or PR exists, so the claim is honest — but "exists" is two states, and
                # only one of them is in flight. See `_work_in_flight_or_stranded`.
                return _work_in_flight_or_stranded(snap, ttl_review,
                                                   reason="working_claim_has_work")
            return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "working_claim_stranded", mode="start")
        # Also IGNORED: an issue carrying neither `agent:ready` nor `agent:working` was never
        # handed to the pipeline. Silence is correct; pretending it is parked is not.
        return Decision(ACT_NONE, db.STATE_IGNORED, "issue_not_ready", park_reason="not_ready")

    # ---- PR lanes, cheapest-to-unblock first --------------------------------------------------
    if snap.is_draft:
        # A draft cannot enter the queue however green it is. #1236 sat CLEAN with 21 passing
        # checks for hours because nothing noticed it was a draft.
        return Decision(ACT_NONE, db.STATE_PARKED, "pr_is_draft", park_reason="draft",
                        wake_in=ttl_parked)

    if snap.merge_state == "DIRTY":
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "conflicts_with_main", mode="rebase")

    if "agent:revise" in snap.labels:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "owner_requested_changes", mode="revise")
    if "agent:depfix" in snap.labels:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "dependabot_ci_failure", mode="depfix")
    if "agent:docfix" in snap.labels:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "lint_gate_failure", mode="docfix")

    if snap.merge_state in MERGE_STATE_UNREADABLE:
        # Below the lane labels on purpose: `revise`, `depfix` and `docfix` do not merge anything,
        # so there is no reason to hold them on a mergeability GitHub is still computing. Everything
        # from here down is the merge path, and that must not run on a field it cannot read.
        return Decision(ACT_NONE, db.STATE_WAIT_CI, "merge_state_unknown",
                        wait_reason="mergeability_computing", wake_in=120)

    if snap.merge_state not in MERGE_STATE_PROCEED:
        # A value GitHub added, or one gh renamed. The enum is closed and every member of it is
        # named above, so this means the world changed — wait rather than guess which half of the
        # ladder it belongs in.
        return Decision(ACT_NONE, db.STATE_WAIT_CI, "merge_state_unrecognised",
                        wait_reason="mergeability_computing", wake_in=300,
                        details={"merge_state": snap.merge_state})

    if snap.auto_merge:
        # Already armed. GitHub will enqueue it the moment its gate clears, so there is nothing to
        # decide and nothing to ask for — this is a WAIT, and saying so is what stops v2 reproducing
        # the incident it was built to prevent.
        #
        # This sits ABOVE the checks branch on purpose. An armed PR reporting BLOCKED is the normal,
        # healthy state of a PR waiting on required checks; without this, `decide` fell through to
        # "gate satisfied -> ACT_MERGE" on every pass and burned the per-head merge budget in three
        # minutes, one pass from parking a perfectly good PR. Measured live on #1295.
        return Decision(ACT_NONE, db.STATE_WAIT_QUEUE, "auto_merge_armed",
                        wait_reason="merge_queue", wake_in=ttl_queue,
                        details={"merge_state": snap.merge_state})

    checks = snap.checks
    if checks is None:
        return Decision(ACT_NONE, db.STATE_WAIT_CI, "checks_unknown",
                        wait_reason="ci", wake_in=300)

    if checks.failed:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "required_checks_failing", mode="fix",
                        details={"failed": list(checks.names_failed)})

    if checks.pending or checks.total == 0:
        # Waiting on CI is a STATE. v1 spent a whole tick per PR per 5 minutes re-asking this.
        return Decision(ACT_NONE, db.STATE_WAIT_CI, "ci_running",
                        wait_reason="ci", wake_in=ttl_ci)

    if snap.unresolved_threads > 0:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "unresolved_review_threads", mode="review",
                        details={"threads": snap.unresolved_threads})

    if not snap.review_fresh:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "no_fresh_review", mode="selfreview")

    # ---- green, reviewed, threads clear: the queue's problem now ------------------------------
    if snap.queue_state:
        return Decision(ACT_NONE, db.STATE_WAIT_QUEUE, "in_merge_queue",
                        wait_reason="merge_queue", wake_in=ttl_queue,
                        details={"queue_state": snap.queue_state})

    return Decision(ACT_MERGE, db.STATE_WAIT_QUEUE, "gate_satisfied",
                    wait_reason="merge_queue", wake_in=ttl_queue)


def _answer_for(slug: str, kind: str, number: int, labels: frozenset[str],
                owner: str | None) -> answers.Answer | None:
    """Read the owner's newest Decision-Comment reply, but only when it can change the answer.

    Gated on the hold labels for cost, not for correctness: this is one `gh view --json comments`
    call, and asking it for every observation would put it on the hot path of a queue whose whole
    point is that a waiting item costs nothing.
    """
    if not owner or not (labels & HOLD_LABELS):
        return None
    return answers.newest(slug, kind, number, owner)


def snapshot_pr(slug: str, number: int, *, owner: str | None = None,
                answer_routed: str | None = None,
                review: github.ReviewState | None = None) -> Snapshot:
    """Build a `Snapshot` for a PR from live GitHub reads.

    Any failed read yields `readable=False` rather than a partial snapshot, because a snapshot
    that is half-true is worse than one that admits ignorance: `decide` can handle "I don't know"
    and cannot handle "green" that was actually "unreadable".

    Review evidence is READ here, not defaulted. It used to arrive as keyword arguments no caller
    passed, so every observation asserted "no fresh review, no unresolved threads" — which made
    `ACT_MERGE` unreachable from the daemon and reported every green PR as needing a selfreview.
    `review` is injectable only so a replay harness can feed recorded facts.
    """
    try:
        facts = github.pr_facts(slug, number)
        checks = github.checks_for(slug, number)
        queue = github.merge_queue_state(slug, number)
        reviews = review if review is not None else github.review_state(slug, number)
    except github.GitHubUnavailable as exc:
        LOG.warning("PR #%s unreadable: %s", number, exc)
        return Snapshot(kind="pr", number=number, readable=False)

    labels = frozenset(github.label_names(facts))
    return Snapshot(
        kind="pr",
        number=number,
        labels=labels,
        state=(facts.get("state") or "OPEN").upper(),
        is_draft=bool(facts.get("isDraft")),
        upstream=github.is_upstream(facts, slug),
        branch=facts.get("headRefName"),
        head_sha=facts.get("headRefOid"),
        merge_state=(facts.get("mergeStateStatus") or "").upper(),
        queue_state=queue,
        auto_merge=facts.get("autoMergeRequest") is not None,
        checks=checks,
        review_fresh=reviews.fresh,
        unresolved_threads=reviews.unresolved,
        answer=_answer_for(slug, "pr", number, labels, owner),
        answer_routed=answer_routed,
        readable=True,
    )


def snapshot_issue(slug: str, number: int, *, owner: str | None = None,
                   answer_routed: str | None = None) -> Snapshot:
    """Build a `Snapshot` for an issue.

    The answer read is not PR-only, and that is the half v1 got right and is easy to drop: a
    Decision Comment raised before any PR exists has only the issue thread to live on, and an owner
    who answers on the issue out of habit is answering.
    """
    try:
        facts = github.gh_json(
            ["issue", "view", str(number), "--repo", slug, "--json",
             "number,state,labels,updatedAt"]
        ) or {}
    except github.GitHubUnavailable as exc:
        LOG.warning("issue #%s unreadable: %s", number, exc)
        return Snapshot(kind="issue", number=number, readable=False)
    labels = frozenset(github.label_names(facts))
    work = None
    has_open_pr = None
    if labels & WORK_SENSITIVE_LABELS:
        # Only asked when it can change the answer — this is two API calls, and every other issue
        # decision is reached without them. `agent:working` is in the set because a stranded claim
        # is told from an honest one by exactly this question, and asking it is what stops the
        # recovery path forking a branch that already exists.
        work = _work_exists_for_issue(slug, number)
        if work:
            # Only asked when work exists, because it is only then that the answer changes anything.
            has_open_pr = _open_pr_for_issue(slug, number)
    return Snapshot(
        kind="issue",
        number=number,
        labels=labels,
        state=(facts.get("state") or "OPEN").upper(),
        work_exists=work,
        has_open_pr=has_open_pr,
        answer=_answer_for(slug, "issue", number, labels, owner),
        answer_routed=answer_routed,
        readable=True,
    )


def _open_pr_for_issue(slug: str, number: int) -> bool | None:
    """Is this issue's existing work an OPEN pull request?

    LINKED PRs are asked FIRST, and that ordering is the whole correctness of this function. The
    branch-name convention is a fallback, not the definition: PR #1302 carries issue #1301 on
    `feature/claude-probe-access`, so a convention-only lookup answers "no PR" for an issue whose
    PR is open and healthy — and this function's `False` is what licenses a re-dispatch. Getting
    that backwards forks live work, which is the one outcome the wait state exists to prevent.

    Returns:
        True / False, or None when either read failed.
    """
    try:
        linked = github.gh_json(
            ["issue", "view", str(number), "--repo", slug, "--json",
             "closedByPullRequestsReferences"]
        ) or {}
    except github.GitHubUnavailable as exc:
        LOG.warning("issue #%s PR linkage unreadable: %s", number, exc)
        return None
    if linked.get("closedByPullRequestsReferences"):
        # ANY linked PR means "not resumable", and the coarseness is deliberate. GitHub does not
        # return a `state` on these refs (they carry id/number/repository/url only), so telling an
        # open PR from a merged or closed one would cost one extra read per issue per observation.
        # The asymmetry decides it: a merged PR means the work shipped and the issue just needs
        # closing, a closed-unmerged one is an abandoned approach that a human should judge, and an
        # open one must never be forked. None of the three wants an automatic restart.
        return True
    return github.open_pr_for_branch(slug, f"feature/claude-issue-{number}")


def _work_exists_for_issue(slug: str, number: int) -> bool | None:
    """Has a previous run left anything behind for this issue?

    Two sources, cheapest first, and BOTH from GitHub rather than the local checkout: what decides
    this is whether the work left the box. A local branch proves only that `git checkout -b` ran,
    which every attempt does before it does anything useful.

    Returns:
        True on a linked PR or an existing remote branch, False when both are readable and absent,
        and None when either read failed — "I could not tell" is not "there is nothing there".
    """
    try:
        linked = github.gh_json(
            ["issue", "view", str(number), "--repo", slug, "--json",
             "closedByPullRequestsReferences"]
        ) or {}
        if (linked.get("closedByPullRequestsReferences") or []):
            return True
    except github.GitHubUnavailable as exc:
        LOG.warning("issue #%s PR linkage unreadable: %s", number, exc)
        return None
    # The branch convention is the second half: an agent that pushed and then died before opening a
    # PR has no linkage, and is exactly the case that must not be restarted from scratch.
    return github.branch_exists(slug, f"feature/claude-issue-{number}")
