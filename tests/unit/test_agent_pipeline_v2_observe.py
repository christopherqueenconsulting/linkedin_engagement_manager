"""Tests for the v2 state machine.

`decide` is pure, so every transition — including the ones that only occur when GitHub is lying or
unreachable — is testable exactly. The cases below are organised around the v1 incidents each rule
exists to prevent, so a regression fails on a test that names the incident rather than on a vague
invariant.
"""

from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import (  # noqa: E402
    daemon as daemon_mod,  # noqa: E402
    db,
    github,
    observe,
)
from lemd.config import load  # noqa: E402
from lemd.github import ChecksState  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
GREEN = ChecksState(failed=0, pending=0, total=6)
RED = ChecksState(failed=1, pending=0, total=6, names_failed=("Unit Tests (Python 3.12)",))
RUNNING = ChecksState(failed=0, pending=3, total=6)


def pr(**kw) -> observe.Snapshot:
    """A mergeable PR snapshot, overridable per test."""
    base = dict(
        kind="pr", number=1, labels=frozenset({"agent:working"}), state="OPEN",
        branch="feature/x", head_sha="abc", checks=GREEN, review_fresh=True,
    )
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


# ---------------------------------------------------------------- terminal facts


def test_merged_is_terminal_even_with_holds():
    """A merged PR is done; a stale hold label must not resurrect it."""
    got = d(pr(state="MERGED", labels=frozenset({"agent:working", "needs-human"})))
    assert (got.action, got.next_state) == (observe.ACT_CLOSE, db.STATE_MERGED)


def test_closed_unmerged_is_terminal():
    assert d(pr(state="CLOSED")).next_state == db.STATE_CLOSED


# ---------------------------------------------------------------- unreadable


def test_unreadable_never_acts():
    """#1082: an unreadable state read as healthy produced 154 re-enqueues."""
    got = d(pr(readable=False))
    assert got.action == observe.ACT_NONE
    assert got.wake_in == 300  # ask again soon, do nothing now


def test_unknown_checks_do_not_merge():
    """A missing rollup must never be mistaken for a green one."""
    got = d(pr(checks=None))
    assert got.action == observe.ACT_NONE
    assert got.reason == "checks_unknown"


def test_zero_required_checks_is_not_green():
    """A head with no required checks yet is one where CI has not started."""
    got = d(pr(checks=ChecksState(failed=0, pending=0, total=0)))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_WAIT_CI


# ---------------------------------------------------------------- admission


def test_fork_pr_is_never_admitted():
    """Attacker-controlled code must not reach a lane that checks it out and runs it."""
    got = d(pr(upstream=False))
    assert got.action == observe.ACT_NONE
    assert "fork_pr" in got.reason


def test_release_pr_excluded_by_branch_not_author():
    """Arming auto-merge on a release PR would bypass the 4x-daily release windows."""
    got = d(pr(branch="release-please--branches--main"))
    assert got.action == observe.ACT_NONE
    assert "release_pr" in got.reason


def test_release_pr_excluded_by_autorelease_label():
    got = d(pr(labels=frozenset({"agent:working", "autorelease: pending"})))
    assert "release_pr" in got.reason


def test_dependabot_pr_IS_admitted_when_labelled():
    """Dependabot PRs must stay admissible.

    Excluding them by AUTHOR would silently retire the depfix lane, which exists to run an agent ON
    a dependabot PR — the router workflow only applies the label; the agent does the fix.
    """
    got = d(pr(labels=frozenset({"agent:depfix"}), checks=RED))
    assert got.action == observe.ACT_DISPATCH
    assert got.mode == "depfix"


def test_unlabelled_pr_is_not_ours():
    got = d(pr(labels=frozenset()))
    assert got.action == observe.ACT_NONE
    assert "no_agent_label" in got.reason


# ---------------------------------------------------------------- human holds


@pytest.mark.parametrize("hold", ["needs-human", "agent:blocked"])
def test_human_hold_outranks_every_lane(hold):
    """A green, reviewed PR parked by a human must NOT be merged on the next pass."""
    got = d(pr(labels=frozenset({"agent:working", hold})))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_PARKED
    assert got.wake_in == TTLS["ttl_parked"]


def test_hold_beats_even_a_failing_lane():
    got = d(pr(labels=frozenset({"agent:working", "needs-human"}), checks=RED))
    assert got.action == observe.ACT_NONE


# ---------------------------------------------------------------- PR lanes, in priority order


def test_draft_is_parked_not_merged():
    """#1236 sat CLEAN with 21 green checks for hours because nothing noticed it was a draft."""
    got = d(pr(is_draft=True))
    assert got.next_state == db.STATE_PARKED
    assert got.park_reason == "draft"


def test_dirty_goes_to_rebase_before_anything_else():
    got = d(pr(merge_state="DIRTY", checks=RED))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "rebase")


def test_failing_checks_dispatch_fix_with_the_names():
    got = d(pr(checks=RED))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "fix")
    assert got.details["failed"] == ["Unit Tests (Python 3.12)"]


def test_ci_running_is_a_wait_state_not_a_poll():
    """The core v2 economy change: this costs zero scheduler attention until CI reports."""
    got = d(pr(checks=RUNNING))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_WAIT_CI
    assert got.wake_in == TTLS["ttl_ci"]


def test_unresolved_threads_block_the_merge():
    got = d(pr(unresolved_threads=2))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "review")


def test_missing_review_dispatches_selfreview():
    got = d(pr(review_fresh=False))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "selfreview")


def test_green_and_reviewed_arms_the_merge():
    got = d(pr())
    assert got.action == observe.ACT_MERGE
    assert got.next_state == db.STATE_WAIT_QUEUE
    assert got.wake_in == TTLS["ttl_queue"]


def test_already_queued_waits_instead_of_re_arming():
    """Re-arming a PR the queue already holds is how #1067 reached 154 re-enqueues."""
    got = d(pr(queue_state="QUEUED"))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_WAIT_QUEUE
    assert got.details["queue_state"] == "QUEUED"


def test_revise_outranks_selfreview():
    """The owner's feedback is more important than arranging a review of the old head."""
    got = d(pr(labels=frozenset({"agent:working", "agent:revise"}), review_fresh=False))
    assert got.mode == "revise"


# ---------------------------------------------------------------- issues


def test_ready_issue_dispatches_start():
    """A start needs `agent:ready` AND nothing already built for it.

    `work_exists` is three-valued and the third value is why it exists: an issue whose previous run
    already pushed a branch is in flight, and re-dispatching it spends another 9-12 minute model
    session redoing work that exists (#1290 was re-dispatched seven seconds after finishing rc=0).
    Unknown WAITS, because a false negative there costs a whole session and risks two agents on one
    branch, while a false positive costs one TTL.
    """
    ready = observe.Snapshot(kind="issue", number=9, labels=frozenset({"agent:ready"}),
                             work_exists=False)
    got = d(ready)
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "start")

    from dataclasses import replace
    assert d(replace(ready, work_exists=True)).action == observe.ACT_NONE
    assert d(replace(ready, work_exists=None)).action == observe.ACT_NONE


def test_issue_without_ready_is_not_started():
    got = d(observe.Snapshot(kind="issue", number=9, labels=frozenset({"agent:working"})))
    assert got.action == observe.ACT_NONE


def test_held_issue_is_never_started():
    got = d(observe.Snapshot(
        kind="issue", number=9, labels=frozenset({"agent:ready", "needs-human"})))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_PARKED


# ---------------------------------------------------------------- checks helper


def test_checksstate_green_requires_reported_checks():
    assert ChecksState(failed=0, pending=0, total=6).green is True
    assert ChecksState(failed=0, pending=0, total=0).green is False
    assert ChecksState(failed=1, pending=0, total=6).green is False
    assert ChecksState(failed=0, pending=1, total=6).green is False


def test_every_decision_carries_a_reason():
    """`reason` is telemetry, not decoration: it is how a wrong dispatch gets diagnosed later."""
    for snap in (pr(), pr(checks=RED), pr(readable=False), pr(is_draft=True),
                 pr(state="MERGED"), pr(upstream=False)):
        assert d(snap).reason


# ---------------------------------------------------------------- the rollup reader


def _rollup(monkeypatch, entries):
    """Serve one `statusCheckRollup` payload to `checks_for`."""
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: {"statusCheckRollup": entries})


def test_all_required_checks_green_is_green(monkeypatch):
    _rollup(monkeypatch, [{"name": n, "conclusion": "SUCCESS"} for n in github.REQUIRED_CHECKS])
    assert github.checks_for("o/r", 1).green is True


def test_a_required_check_that_has_not_reported_counts_as_pending(monkeypatch):
    """The hazard `total == 0` does not catch: a head where only SOME checks exist yet.

    Every workflow behind the six required contexts triggers on every `pull_request`, so three
    green entries means the other three have not been created — reading that as green armed
    auto-merge before CI had reported.
    """
    _rollup(monkeypatch, [{"name": n, "conclusion": "SUCCESS"}
                          for n in github.REQUIRED_CHECKS[:3]])
    state = github.checks_for("o/r", 1)
    assert state.green is False
    assert state.pending == 3
    assert d(pr(checks=state)).next_state == db.STATE_WAIT_CI


def test_an_unknown_conclusion_is_never_treated_as_success(monkeypatch):
    """`ACTION_REQUIRED` (a workflow awaiting approval) must hold the PR, not pass it."""
    entries = [{"name": n, "conclusion": "SUCCESS"} for n in github.REQUIRED_CHECKS[:-1]]
    entries.append({"name": github.REQUIRED_CHECKS[-1], "conclusion": "ACTION_REQUIRED"})
    _rollup(monkeypatch, entries)
    state = github.checks_for("o/r", 1)
    assert (state.green, state.pending) == (False, 1)
    assert state.names_pending == (github.REQUIRED_CHECKS[-1],)


def test_non_required_noise_is_ignored(monkeypatch):
    entries = [{"name": n, "conclusion": "SUCCESS"} for n in github.REQUIRED_CHECKS]
    entries.append({"name": "CodeQL Security Analysis", "conclusion": "FAILURE"})
    _rollup(monkeypatch, entries)
    assert github.checks_for("o/r", 1).green is True


def test_failed_required_check_is_named(monkeypatch):
    entries = [{"name": n, "conclusion": "SUCCESS"} for n in github.REQUIRED_CHECKS[1:]]
    entries.append({"name": github.REQUIRED_CHECKS[0], "conclusion": "FAILURE"})
    _rollup(monkeypatch, entries)
    state = github.checks_for("o/r", 1)
    assert state.failed == 1 and state.names_failed == (github.REQUIRED_CHECKS[0],)


# ---------------------------------------------------------------- the review reader


def _review_payload(*, head="2026-08-10T10:00:00Z", comments=(), reviews=(), threads=()):
    """A GraphQL response shaped like the one `review_state` reads."""
    return {"data": {"repository": {"pullRequest": {
        "commits": {"nodes": [{"commit": {"committedDate": head}}]},
        "reviews": {"nodes": list(reviews)},
        "comments": {"nodes": list(comments)},
        "reviewThreads": {"nodes": list(threads)},
    }}}}


def _serve(monkeypatch, payload):
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: payload)


def test_marker_after_the_head_commit_is_fresh(monkeypatch):
    _serve(monkeypatch, _review_payload(comments=[
        {"createdAt": "2026-08-10T10:05:00Z", "body": github.CLAUDE_REVIEW_MARKER + " — PASS"},
    ]))
    assert github.review_state("o/r", 1).fresh is True


def test_marker_older_than_the_head_commit_is_stale(monkeypatch):
    """A review of the PREVIOUS head is not a review of this one — a new push must be re-reviewed."""
    _serve(monkeypatch, _review_payload(comments=[
        {"createdAt": "2026-08-10T09:00:00Z", "body": github.CLAUDE_REVIEW_MARKER + " — PASS"},
    ]))
    state = github.review_state("o/r", 1)
    assert state.fresh is False
    assert d(pr(review_fresh=state.fresh)).mode == "selfreview"


def test_a_copilot_review_also_satisfies_freshness(monkeypatch):
    _serve(monkeypatch, _review_payload(reviews=[
        {"submittedAt": "2026-08-10T10:30:00Z",
         "author": {"login": "copilot-pull-request-reviewer[bot]"}},
    ]))
    assert github.review_state("o/r", 1).fresh is True


def test_an_unrelated_comment_is_not_a_review(monkeypatch):
    _serve(monkeypatch, _review_payload(comments=[
        {"createdAt": "2026-08-10T11:00:00Z", "body": "looks good to me"},
    ]))
    assert github.review_state("o/r", 1).fresh is False


def test_only_copilot_threads_count_as_unresolved(monkeypatch):
    """A human's unresolved nit is not something MODE=review can resolve, so it must not dispatch it."""
    _serve(monkeypatch, _review_payload(threads=[
        {"isResolved": False, "comments": {"nodes": [{"author": {"login": "gitchrisqueen"}}]}},
        {"isResolved": False,
         "comments": {"nodes": [{"author": {"login": "copilot-pull-request-reviewer[bot]"}}]}},
        {"isResolved": True,
         "comments": {"nodes": [{"author": {"login": "copilot-pull-request-reviewer[bot]"}}]}},
    ]))
    assert github.review_state("o/r", 1).unresolved == 1


def test_unreadable_head_date_accepts_an_existing_review(monkeypatch):
    """Refusing every PR when a commit date is unreadable would wedge the gate."""
    payload = _review_payload(head="", comments=[
        {"createdAt": "2026-08-01T00:00:00Z", "body": github.CLAUDE_REVIEW_MARKER + " — PASS"},
    ])
    _serve(monkeypatch, payload)
    assert github.review_state("o/r", 1).fresh is True


def test_snapshot_pr_reads_review_evidence_instead_of_defaulting_it(monkeypatch):
    """The defect this covers: review facts arrived as kwargs NO caller passed.

    Every observation therefore asserted "no fresh review, no unresolved threads", so `ACT_MERGE`
    was unreachable from the daemon and every green PR reported as needing a selfreview.
    """
    monkeypatch.setattr(github, "pr_facts", lambda *a, **k: {
        "number": 7, "state": "OPEN", "isDraft": False, "mergeStateStatus": "CLEAN",
        "headRefName": "feature/x", "headRefOid": "abc",
        "labels": [{"name": "agent:working"}],
        "headRepositoryOwner": {"login": "o"},
    })
    monkeypatch.setattr(github, "checks_for", lambda *a, **k: GREEN)
    monkeypatch.setattr(github, "merge_queue_state", lambda *a, **k: "")
    monkeypatch.setattr(github, "review_state", lambda *a, **k: github.ReviewState(
        fresh=True, unresolved=0))

    snap = observe.snapshot_pr("o/r", 7)
    assert (snap.review_fresh, snap.unresolved_threads) == (True, 0)
    assert d(snap).action == observe.ACT_MERGE


# ---------------------------------------------------------------- the loop's use of the decision


@pytest.fixture()
def dmn(tmp_path):
    """A daemon on a throwaway queue database, as it looks just after its first reconcile."""
    d_ = daemon_mod.Daemon(load(tmp_path))
    d_._last_reconcile = time.time()
    yield d_
    d_.conn.close()


def test_a_pr_comment_marks_the_pr_not_a_phantom_issue(dmn):
    """GitHub delivers PR comments as `issue_comment` with only an `issue` object.

    Keying that to kind `issue` meant the owner's reply to a Decision Comment — the whole
    revise-unblock path — marked an item that does not exist and was dropped.
    """
    db.upsert_item(dmn.conn, kind="pr", number=1269, state=db.STATE_PARKED)
    assert dmn._event_target("issue_comment", 1269) == ("pr", 1269)


def test_an_issue_comment_on_a_real_issue_still_targets_the_issue(dmn):
    db.upsert_item(dmn.conn, kind="issue", number=42, state=db.STATE_READY)
    assert dmn._event_target("issue_comment", 42) == ("issue", 42)


def test_an_event_for_an_unknown_item_targets_nothing(dmn):
    assert dmn._event_target("issue_comment", 999) is None


def test_a_pr_only_event_never_resolves_to_an_issue(dmn):
    db.upsert_item(dmn.conn, kind="issue", number=8, state=db.STATE_READY)
    assert dmn._event_target("check_suite", 8) is None


def test_next_sleep_ignores_deadlines_no_sweep_will_ever_return(dmn):
    """A stale `wake_at` on a non-wait item used to pin the loop at the 1-second floor forever."""
    db.upsert_item(dmn.conn, kind="pr", number=5, state=db.STATE_MERGED, wake_at=1)
    assert dmn.next_sleep() > 1.0


def test_next_sleep_still_honours_a_live_wait_deadline(dmn):
    db.upsert_item(dmn.conn, kind="pr", number=6, state=db.STATE_WAIT_CI, wake_at=1)
    assert dmn.next_sleep() == 1.0


def test_merge_decision_is_not_recorded_as_queued_and_clears_its_deadline(tmp_path, monkeypatch):
    """Nothing arms auto-merge yet, so writing `awaiting_queue` would claim a merge that never was.

    It would also re-decide the same ACT_MERGE every `ttl_queue` seconds forever.
    """
    cfg = dataclasses.replace(load(tmp_path), shadow=False)
    dm = daemon_mod.Daemon(cfg)
    try:
        db.upsert_item(dm.conn, kind="pr", number=11, state=db.STATE_WAIT_CI,
                       branch="feature/x", wake_at=1)
        monkeypatch.setattr(observe, "snapshot_pr", lambda *a, **k: pr(number=11))
        dm._observe_one(db.get_item(dm.conn, "pr", 11))
        row = db.get_item(dm.conn, "pr", 11)
        assert row["state"] == db.STATE_READY
        assert row["wake_at"] is None
    finally:
        dm.conn.close()
