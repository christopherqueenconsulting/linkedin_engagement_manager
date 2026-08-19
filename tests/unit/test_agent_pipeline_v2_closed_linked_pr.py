"""An issue whose only linked PR is merged or closed-unmerged waits for ever (#1405).

`_open_pr_for_issue` returns True for ANY linked PR ref, deliberately: GitHub's
`closedByPullRequestsReferences` carries id/number/repository/url and no `state`, and `False` is what
licenses a re-dispatch, so a ref of unknown state must never produce one. The cost is that the two
states nobody can act on read as "in flight": the issue waits on `ttl_review`, re-reads the same
linkage, gets the same answer, and nothing will ever end it.

Both are genuine ASKS rather than "not ours" — a merged PR means the work shipped and only the issue
is still open, a closed-unmerged one means a human rejected the approach, and restarting a rejected
approach redoes work that was already turned down. So `ACT_PARK` is back, on the terms #1386 removed
it under: returned, wired, and reachable, with the reachability asserted here so the dead-constant
situation cannot recur.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_V2 = _ROOT / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import answers, daemon, db, github, observe  # noqa: E402
from lemd.config import load  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)


def issue(**kw) -> observe.Snapshot:
    """An `agent:ready` issue whose previous run linked a PR."""
    base = dict(kind="issue", number=1405, labels=frozenset({"agent:ready"}),
                branch="feature/claude-issue-1405", work_exists=True, has_open_pr=True)
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


def _acting_daemon(tmp_path: Path) -> daemon.Daemon:
    """A daemon allowed to act (not shadow), on a throwaway queue."""
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=0\n"
        "SLUG=christopherqueenconsulting/linkedin_engagement_manager\n"
    )
    return daemon.Daemon(load(tmp_path))


# ------------------------------------------------------------------ github.linked_pr_state


class _Reads:
    """A scripted `gh_json`, so each test states exactly which two reads it expects."""

    def __init__(self, refs, state="OPEN", refs_raise=False, state_raise=False):
        self.refs, self.state = refs, state
        self.refs_raise, self.state_raise = refs_raise, state_raise
        self.calls: list[list[str]] = []

    def __call__(self, args, **_kw):
        self.calls.append(args)
        if "closedByPullRequestsReferences" in args:
            if self.refs_raise:
                raise github.GitHubUnavailable("gh down")
            return {"closedByPullRequestsReferences": self.refs}
        if args[0] == "issue":
            # `snapshot_issue`'s own facts read. `agent:ready` is what opens the work-state gate.
            return {"number": 1405, "state": "OPEN", "labels": [{"name": "agent:ready"}]}
        if self.state_raise:
            raise github.GitHubUnavailable("gh down")
        return {"state": self.state}


def _ref(number: int, owner="christopherqueenconsulting", name="linkedin_engagement_manager"):
    """One `closedByPullRequestsReferences` entry, in the shape GitHub really returns."""
    return {"id": f"PR_{number}", "number": number,
            "repository": {"name": name, "owner": {"login": owner}},
            "url": f"https://github.com/{owner}/{name}/pull/{number}"}


@pytest.mark.parametrize("state", ["OPEN", "MERGED", "CLOSED"])
def test_the_state_of_the_linked_pr_is_resolved(monkeypatch, state):
    """The whole point: the refs carry no `state`, so it costs a second read to know."""
    reads = _Reads([_ref(1600)], state=state)
    monkeypatch.setattr(github, "gh_json", reads)
    assert github.linked_pr_state("o/r", 1405) == state
    assert reads.calls[0][0] == "issue" and reads.calls[1][0] == "pr"
    assert "1600" in reads.calls[1]


def test_the_newest_ref_decides(monkeypatch):
    """Issue #1091 carries both #1592 and #1597, and only the latest says where the work stands.

    Reading the FIRST ref would let an older merged PR under a live one report `work shipped`, which
    parks an issue whose replacement PR is open and healthy.
    """
    reads = _Reads([_ref(1592), _ref(1597)], state="OPEN")
    monkeypatch.setattr(github, "gh_json", reads)
    assert github.linked_pr_state("o/r", 1091) == "OPEN"
    assert "1597" in reads.calls[1]


def test_the_newest_ref_is_by_number_not_by_position(monkeypatch):
    """The API's ordering is not a documented guarantee, so it is not relied on."""
    reads = _Reads([_ref(1597), _ref(1592)], state="MERGED")
    monkeypatch.setattr(github, "gh_json", reads)
    github.linked_pr_state("o/r", 1091)
    assert "1597" in reads.calls[1]


def test_a_cross_repo_ref_is_read_where_it_lives(monkeypatch):
    """The ref names its own repository; the same number under our slug is a different PR."""
    reads = _Reads([_ref(12, owner="someone", name="elsewhere")], state="MERGED")
    monkeypatch.setattr(github, "gh_json", reads)
    github.linked_pr_state("o/r", 5)
    assert "someone/elsewhere" in reads.calls[1]


def test_no_linkage_reads_nothing_further(monkeypatch):
    """The extra read is bounded to issues that already have linkage.

    `""`, not None: both wait, but they are different READS, and telling them apart is what lets one
    linkage read answer all three of `snapshot_issue`'s questions instead of three.
    """
    reads = _Reads([])
    monkeypatch.setattr(github, "gh_json", reads)
    assert github.linked_pr_state("o/r", 1405) == ""
    assert len(reads.calls) == 1


def test_unreadable_linkage_is_none(monkeypatch):
    """None, never a state — an unreadable read may not license a park."""
    monkeypatch.setattr(github, "gh_json", _Reads([], refs_raise=True))
    assert github.linked_pr_state("o/r", 1405) is None


def test_an_unreadable_pr_state_is_none(monkeypatch):
    """The second read fails on its own, and must not be guessed from the first."""
    monkeypatch.setattr(github, "gh_json", _Reads([_ref(1600)], state_raise=True))
    assert github.linked_pr_state("o/r", 1405) is None


def test_a_ref_with_no_number_is_not_a_ref(monkeypatch):
    """A malformed payload must not become `gh pr view None`.

    None rather than `""`: the issue IS linked to something, and reporting "nothing linked" would
    let the caller license a re-dispatch off the branch convention alone.
    """
    reads = _Reads([{"id": "PR_x"}])
    monkeypatch.setattr(github, "gh_json", reads)
    assert github.linked_pr_state("o/r", 1405) is None
    assert len(reads.calls) == 1


def test_an_empty_state_field_is_none(monkeypatch):
    """`""` is not a fourth state; it is the read failing to say anything."""
    monkeypatch.setattr(github, "gh_json", _Reads([_ref(1600)], state=""))
    assert github.linked_pr_state("o/r", 1405) is None


# ------------------------------------------------------------------ the four states, decided


def test_a_merged_linked_pr_parks_and_asks():
    """The work shipped; the issue is just still open. Nothing in the pipeline moves that."""
    got = d(issue(linked_pr_state="MERGED"))
    assert got.action == observe.ACT_PARK
    assert (got.reason, got.park_reason) == ("work_shipped_needs_close", "work_shipped_needs_close")
    assert (got.next_state, got.mode) == (db.STATE_PARKED, "park")


def test_a_closed_unmerged_linked_pr_parks_and_asks():
    """A human rejected the approach. Restarting would redo work already turned down."""
    got = d(issue(linked_pr_state="CLOSED"))
    assert got.action == observe.ACT_PARK
    assert (got.reason, got.park_reason) == ("approach_rejected", "approach_rejected")


def test_an_open_linked_pr_still_waits():
    """The half that was always right — re-dispatching here forks live work."""
    got = d(issue(linked_pr_state="OPEN"))
    assert got.action == observe.ACT_NONE
    assert (got.wait_reason, got.wake_in) == ("work_in_flight", TTLS["ttl_review"])


def test_an_unreadable_state_waits():
    """None covers "nothing linked" AND "could not read it", and both wait."""
    got = d(issue(linked_pr_state=None))
    assert got.action == observe.ACT_NONE
    assert got.wait_reason == "work_in_flight"


@pytest.mark.parametrize("state,reason", [("MERGED", "work_shipped_needs_close"),
                                          ("CLOSED", "approach_rejected")])
def test_the_working_claim_reaches_the_same_answer(state, reason):
    """`agent:working` gets there by a different route through the same shared helper."""
    got = d(issue(labels=frozenset({"agent:working"}), linked_pr_state=state))
    assert (got.action, got.reason) == (observe.ACT_PARK, reason)


def test_a_stranded_branch_is_still_resumed():
    """No linkage at all is untouched: a pushed branch with no PR is resumable, not an ask."""
    got = d(issue(has_open_pr=False, linked_pr_state=None))
    assert (got.action, got.mode, got.reason) == (
        observe.ACT_DISPATCH, "start", "stranded_branch_no_pr")


def test_a_hold_still_outranks_the_park():
    """The owner's hold outranks every lane, and a park is not an exception to that."""
    got = d(issue(labels=frozenset({"agent:ready", "needs-human"}), linked_pr_state="CLOSED"))
    assert got.action == observe.ACT_NONE


def test_a_closed_issue_is_closed_before_any_of_this():
    """Terminal facts win: the common merged case closes the issue and never reaches a park."""
    got = d(issue(state="CLOSED", linked_pr_state="MERGED"))
    assert (got.action, got.reason) == (observe.ACT_CLOSE, "closed_unmerged")


# ------------------------------------------------------------------ the read is bounded


def test_an_issue_the_pipeline_never_touched_reads_nothing(monkeypatch):
    """The `WORK_SENSITIVE_LABELS` gate is unchanged — no label, no linkage read at all."""
    calls = []
    monkeypatch.setattr(github, "linked_pr_state", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: {"number": 1, "state": "OPEN",
                                                            "labels": [{"name": "bug"}]})
    assert observe.snapshot_issue("o/r", 5).linked_pr_state is None
    assert calls == []


def test_nothing_linked_never_reaches_the_state_read(monkeypatch):
    """The extra `pr view` is bounded to an issue that already has linkage."""
    reads = _Reads([])
    monkeypatch.setattr(github, "gh_json", reads)
    monkeypatch.setattr(github, "branch_exists", lambda *a, **k: False)
    snap = observe.snapshot_issue("o/r", 1405)
    assert snap.work_exists is False and snap.linked_pr_state is None
    assert [c[0] for c in reads.calls] == ["issue", "issue"], (
        "expected the issue facts read and ONE linkage read, and no `pr view`")


def test_the_linkage_read_is_made_once_not_three_times(monkeypatch):
    """All three questions start from the same refs read, so it is taken once and handed on.

    `work_exists`, the linked state and `has_open_pr` each used to ask
    `closedByPullRequestsReferences` for themselves. Adding the state naively would have made that
    three reads for one issue; #1405 budgets ONE extra read per observation, and this holds it there.
    """
    reads = _Reads([_ref(1600)], state="MERGED")
    monkeypatch.setattr(github, "gh_json", reads)
    snap = observe.snapshot_issue("o/r", 1405)
    assert (snap.work_exists, snap.has_open_pr, snap.linked_pr_state) == (True, True, "MERGED")
    linkage = [c for c in reads.calls if "closedByPullRequestsReferences" in c]
    assert len(linkage) == 1, f"linkage read {len(linkage)} times"
    assert [c[0] for c in reads.calls] == ["issue", "issue", "pr"]


def test_a_stranded_branch_still_costs_no_more_than_before(monkeypatch):
    """The `""` answer also spares the open-PR lookup its own repeat of the same read."""
    reads = _Reads([])
    monkeypatch.setattr(github, "gh_json", reads)
    monkeypatch.setattr(github, "branch_exists", lambda *a, **k: True)
    monkeypatch.setattr(github, "open_pr_for_branch", lambda *a, **k: False)
    snap = observe.snapshot_issue("o/r", 1405)
    assert (snap.work_exists, snap.has_open_pr) == (True, False)
    assert len([c for c in reads.calls if "closedByPullRequestsReferences" in c]) == 1


def test_a_resolved_state_still_means_not_resumable():
    """`_open_pr_for_issue` keeps its own contract: any linked ref answers True, whatever its state.

    Which state it is in is `decide`'s question. Keeping that separation is what makes the two park
    rows removable without silently turning a merged PR into a licence to re-dispatch.
    """
    assert observe._open_pr_for_issue("o/r", 1405, linked_state="MERGED") is True
    assert observe._open_pr_for_issue("o/r", 1405, linked_state="CLOSED") is True


# ------------------------------------------------------------------ reachability: it reaches park.sh


def test_the_daemon_queues_a_park_for_the_decision(tmp_path, monkeypatch):
    """`ACT_PARK` must move the item to the state `act()` dispatches `park.sh` from.

    This is the assertion #1386 was missing. A constant that is returned but not wired is the same
    dead branch under a different name.
    """
    dmn = _acting_daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=1405, state=db.STATE_READY)
    monkeypatch.setattr(
        observe, "snapshot_issue",
        lambda *a, **k: issue(linked_pr_state="CLOSED"))
    dmn._observe_one(db.get_item(dmn.conn, "issue", 1405))

    row = db.get_item(dmn.conn, "issue", 1405)
    assert (row["state"], row["pending_mode"]) == (db.STATE_READY, "park")
    assert row["parked_reason"] == "approach_rejected"
    assert [r["number"] for r in db.dispatchable(dmn.conn)] == [1405]


def test_the_queued_park_launches_the_park_action(tmp_path, monkeypatch):
    """…and the action it launches is `park.sh`, on the gh pool, with the reason and its detail."""
    dmn = _acting_daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=1405, state=db.STATE_READY,
                   pending_mode="park", parked_reason="approach_rejected")
    seen = {}

    def fake_gh(*, action, kind, number, args, item_id):
        seen.update(action=action, args=args)
        return object()

    monkeypatch.setattr(dmn.sup, "dispatch_gh", fake_gh)
    dmn._launch(db.get_item(dmn.conn, "issue", 1405), "park")
    assert seen["action"] == "park"
    assert seen["args"][:3] == ["issue", "1405", "approach_rejected"]
    assert "closed without merging" in seen["args"][3]


def test_every_decide_raised_park_reason_carries_a_detail():
    """`park.sh`'s fallback detail says the lane exhausted its budget, which these did not spend.

    An owner picks an option against the reason they were given, so a park `decide()` raises must
    bring its own sentence. Derived from the decision source, so a third reason added without a
    detail fails here instead of shipping a Decision Comment that misstates itself.
    """
    src = (_V2 / "lemd" / "observe.py").read_text()
    body = src[src.index("def _work_in_flight_or_stranded("):src.index("def _queue_withheld_lane(")]
    raised = set(re.findall(r'park_reason="([a-z_]+)"', body))
    assert raised, "the extractor broke, not the code"
    assert raised <= set(observe.PARK_DETAILS)


def test_the_merged_detail_says_the_retry_options_cannot_move_it():
    """`park.sh` offers `1A`/`1B` unconditionally, and for a MERGED PR they are unachievable.

    A budget park is released by the un-park itself — the ledger reset IS the fix. A merged link is
    raised from a GitHub fact the un-park does not touch, so an owner who answers `1A` gets the issue
    back on the queue, re-observed, and parked again on the spot: `LEMD_MAX_PARK_LAPS` round trips
    and then `agent:abandoned`. The options are fixed text in the action, so the honest sentence has
    to ride in the detail — asserted here so it cannot quietly drift back out.
    """
    assert "`1A`/`1B` will not restart it" in observe.PARK_DETAILS["work_shipped_needs_close"]


def test_the_rejected_detail_says_the_retry_options_now_do_restart_it():
    """`approach_rejected` is the ONE park where `1A`/`1B` genuinely restart the work (#1605).

    Unlike a merged PR, a closed-unmerged link is exactly what `unpark.sh` now dismisses on an
    actionable answer — so the detail must say the honest, OPPOSITE thing from the merged case, and
    the two must not silently collapse back onto the same sentence.
    """
    detail = observe.PARK_DETAILS["approach_rejected"]
    assert "DO restart it" in detail
    assert "`1A`/`1B` will not restart it" not in detail


def test_the_park_script_recommends_closing_rather_than_retrying():
    """`1B` (rebase and retry) is the right advice for a spent budget and wrong for both of these.

    A merged PR has already shipped and a rejected approach was turned down on purpose; retrying
    either is the one thing not to do.
    """
    src = (_V2 / "actions" / "park.sh").read_text()
    assert "work_shipped_needs_close|approach_rejected) REC=\"C\"" in src
    assert 'REC="B"' in src, "an unrecognised reason must keep the retry recommendation"


def test_the_detail_reaches_the_script_as_its_fourth_argument():
    """`park.sh` reads `${4:-}`, and an empty fourth argument keeps the old fallback text."""
    src = (_V2 / "actions" / "park.sh").read_text()
    assert 'DETAIL="${4:-}"' in src
    assert "${DETAIL:-The automated lane exhausted its budget" in src


# ------------------------------------------------------------------ the ask has a floor


def test_an_issues_laps_are_counted_per_release_not_per_head(tmp_path):
    """A PR's lap key is its head. An issue has none, so every lap would collapse onto one row.

    That matters here specifically: these parks re-raise deterministically the moment a hold comes
    off, so without a working counter the give-up rule (#1390) could never fire for them and a
    rejected approach really would ask for ever.
    """
    dmn = _acting_daemon(tmp_path)
    row = dict(kind="issue", number=1405, head_sha=None, parked_reason=None)
    for lap in range(3):
        dmn._park(row, "approach_rejected")
        # The 6-hourly re-decision of a standing park is the SAME park, not a new lap.
        dmn._park(row, "approach_rejected")
        assert db.park_laps(dmn.conn, "issue", 1405, "approach_rejected") == lap + 1
        db.record_unpark(dmn.conn, "issue", 1405, "approach_rejected",
                         db.lap_key(dmn.conn, "issue", 1405, "approach_rejected", None))


def test_a_prs_laps_still_key_on_its_head(tmp_path):
    """The PR behaviour #1390 shipped is untouched — a head is a better key when there is one."""
    dmn = _acting_daemon(tmp_path)
    for sha in ("sha-a", "sha-a", "sha-b"):
        dmn._park(dict(kind="pr", number=7, head_sha=sha, parked_reason=None), "fix_exhausted")
    assert db.park_laps(dmn.conn, "pr", 7, "fix_exhausted") == 2


def test_the_park_is_abandoned_once_the_laps_run_out():
    """The floor the escalation relies on, asserted through `decide` rather than assumed."""
    got = observe.decide(
        observe.Snapshot(kind="issue", number=1405, labels=frozenset({"needs-human"}),
                         parked_reason="approach_rejected", park_laps=3,
                         answer=answers.Answer("a1", "answer", "1A")),
        max_park_laps=3, **TTLS)
    assert (got.action, got.reason) == (observe.ACT_ABANDON, "park_laps_exhausted")


# ------------------------------------------------------------------ the decision ledger


def test_the_park_is_reported_as_the_state_it_persists(tmp_path, monkeypatch):
    """`_emit` reports what was WRITTEN, not what `decide` named — `park` persists as `ready`.

    Same rule the dispatch and abandon actions follow: an item queued for an action is `ready` with
    a pending mode, and logging `parked` here would report a hold that has not been applied yet.
    """
    dmn = _acting_daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=1405, state=db.STATE_READY)
    monkeypatch.setattr(observe, "snapshot_issue",
                        lambda *a, **k: issue(linked_pr_state="MERGED"))
    dmn._observe_one(db.get_item(dmn.conn, "issue", 1405))

    rows = [json.loads(ln) for ln
            in (tmp_path / "logs" / "lemd-decisions.ndjson").read_text().splitlines() if ln.strip()]
    park = [r for r in rows if r.get("reason") == "work_shipped_needs_close"]
    assert park and park[-1]["action"] == "park"
    assert park[-1]["to_state"] == db.STATE_READY
    assert park[-1]["intent"] == db.STATE_PARKED
