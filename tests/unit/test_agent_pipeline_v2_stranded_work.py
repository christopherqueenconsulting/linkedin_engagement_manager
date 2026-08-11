"""An issue whose `start` pushed a branch and then died must not wait for ever (#1382).

`work_exists` answers "did anything leave the box" — the right question for "must I avoid forking
this", the wrong one for "will anyone ever finish it". Both answers returned the same wait state,
and that wait never ended: the next observation re-read the same branch and got True again.

Measured 2026-08-11: issue #1279 holds commit 696642d2 on `feature/claude-issue-1279`, has no PR
open or closed, and nothing that could ever end its wait. It was orphaned by a daemon restart the
same night (`rc=-9`) — an interrupt is the most common way to reach this state, because a killed
`start` has usually pushed before it dies.

The sample is one issue and not four: the first sweep used `gh pr list --search "... in:head"`,
which is not valid search syntax and returned nothing for every issue. `--head` is the real lookup
and shows the others carry live PRs. That mistake is also why this module tests the LOOKUP as
carefully as the decision.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import db, github, observe  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)


def issue(**kw) -> observe.Snapshot:
    """An `agent:ready` issue whose previous run produced something."""
    base = dict(kind="issue", number=1270, labels=frozenset({"agent:ready"}),
                branch="feature/claude-issue-1270", work_exists=True)
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


def test_a_branch_with_no_pr_is_resumed_not_waited_on():
    """The defect: #1279 sits in this state with nothing that could ever end the wait."""
    got = d(issue(has_open_pr=False))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "start")
    assert got.reason == "stranded_branch_no_pr"


def test_a_branch_with_an_open_pr_is_still_left_alone():
    """The half that was always right — re-dispatching here forks live work."""
    got = d(issue(has_open_pr=True))
    assert got.action == observe.ACT_NONE
    assert (got.next_state, got.wait_reason) == (db.STATE_WAIT_REVIEW, "work_in_flight")


def test_an_unreadable_pr_lookup_waits():
    """Three-valued, and the third value waits.

    Re-dispatching onto a live PR forks the work; one more TTL costs nothing by comparison.
    """
    got = d(issue(has_open_pr=None))
    assert got.action == observe.ACT_NONE
    assert got.wait_reason == "work_in_flight"


def test_a_stranded_working_claim_is_resumed_too():
    """`agent:working` reaches the same question by a different route, and must answer it the same.

    This is the interrupt case exactly: the claim label is still set because the run that set it was
    killed, and the branch it pushed is the work it left behind.
    """
    got = d(issue(labels=frozenset({"agent:working"}), has_open_pr=False))
    assert (got.action, got.mode, got.reason) == (
        observe.ACT_DISPATCH, "start", "stranded_branch_no_pr")


def test_a_working_claim_with_a_live_pr_still_waits():
    """The symmetric half, so the widening cannot fork an in-flight PR from the claim path."""
    got = d(issue(labels=frozenset({"agent:working"}), has_open_pr=True))
    assert got.reason == "working_claim_has_work"


def test_no_work_at_all_still_starts_normally():
    """Untouched: a blank slate is still a plain start."""
    got = d(issue(work_exists=False, has_open_pr=None))
    assert (got.action, got.reason) == (observe.ACT_DISPATCH, "issue_ready")


def test_unreadable_work_state_still_waits():
    """`work_exists=None` outranks everything below it — the asymmetry #1290 paid for."""
    got = d(issue(work_exists=None))
    assert got.action == observe.ACT_NONE
    assert got.reason == "start_work_state_unreadable"


def test_a_hold_still_outranks_a_stranded_branch():
    """Resuming is a dispatch, and the owner's hold outranks every dispatch."""
    got = d(issue(labels=frozenset({"agent:ready", "needs-human"}), has_open_pr=False))
    assert got.action == observe.ACT_NONE


def test_resuming_is_bounded_by_the_start_budget():
    """The dispatch is an ordinary one, so `act()` meters it like any other.

    That is the whole safety argument: a branch that cannot be finished parks and ASKS after two
    attempts, instead of waiting silently for ever. If this ever became a special-cased dispatch
    that skipped the ledger, a genuinely unfinishable issue would spin.
    """
    from lemd import policy
    assert policy.budget_for("start") == 2


@pytest.mark.parametrize("rows,expected", [([], False), ([{"number": 7}], True)])
def test_open_pr_lookup_reads_the_head_branch(monkeypatch, rows, expected):
    """`open_pr_for_branch` is a plain `gh pr list --head --state open`."""
    seen = {}

    def fake(args, **kw):
        seen["args"] = args
        return rows

    monkeypatch.setattr(github, "gh_json", fake)
    assert github.open_pr_for_branch("o/r", "feature/claude-issue-9") is expected
    assert "--head" in seen["args"] and "feature/claude-issue-9" in seen["args"]
    assert "open" in seen["args"]


def test_an_unreadable_lookup_is_none_not_false(monkeypatch):
    """False would re-dispatch onto a live PR. None must survive all the way to `decide`."""
    def boom(*_a, **_k):
        raise github.GitHubUnavailable("gh down")

    monkeypatch.setattr(github, "gh_json", boom)
    assert github.open_pr_for_branch("o/r", "feature/claude-issue-9") is None


def test_the_lookup_is_skipped_when_no_work_exists(monkeypatch):
    """Cost: one extra API call, and only for an issue that already produced something."""
    calls = []
    monkeypatch.setattr(observe, "_work_exists_for_issue", lambda *a: False)
    monkeypatch.setattr(github, "open_pr_for_branch", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: {"number": 1, "state": "OPEN",
                                                            "labels": [{"name": "agent:ready"}]})
    observe.snapshot_issue("o/r", 5)
    assert calls == []


# ------------------------------------------------- the ordering that stops this forking live work


def test_a_linked_pr_outranks_the_branch_convention(monkeypatch):
    """The branch name is a FALLBACK, not the definition — and getting that backwards forks work.

    PR #1302 carries issue #1301 on `feature/claude-probe-access`. A convention-only lookup answers
    "no PR" for an issue whose PR is open and healthy, and this function's False is what licenses a
    re-dispatch. Caught before shipping precisely because #1301 read as stranded.
    """
    calls = []
    monkeypatch.setattr(github, "gh_json",
                        lambda *a, **k: {"closedByPullRequestsReferences": [{"number": 1302}]})
    monkeypatch.setattr(github, "open_pr_for_branch", lambda *a, **k: calls.append(a) or False)
    assert observe._open_pr_for_issue("o/r", 1301) is True
    assert calls == [], "the branch fallback must not even be consulted when a PR is linked"


def test_the_branch_convention_is_used_when_nothing_is_linked(monkeypatch):
    """...and it IS the fallback, or an agent that pushed without opening a PR reads as in flight."""
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: {"closedByPullRequestsReferences": []})
    monkeypatch.setattr(github, "open_pr_for_branch", lambda *a, **k: False)
    assert observe._open_pr_for_issue("o/r", 1279) is False


def test_unreadable_linkage_is_none_not_a_restart(monkeypatch):
    """An unreadable read must never be the thing that licenses a re-dispatch."""
    def boom(*_a, **_k):
        raise github.GitHubUnavailable("gh down")

    monkeypatch.setattr(github, "gh_json", boom)
    assert observe._open_pr_for_issue("o/r", 9) is None
