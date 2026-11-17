"""An `agent:ready` issue whose AUTHOR the TRUST gate refuses must not be re-dispatched for ever.

Measured on #2547: filed by the EA's App, labelled `agent:ready` by a trusted labeler, and refused
`TRUST: #2547 authored by NONE` (EX_TRUST, rc=70) by `agent_run.sh` 353 times — `decide()` chose a
`start` without asking who wrote the issue, and the 10-minute reconcile re-marked it dirty after
every refusal, so the `ttl_parked` wake the EX_TRUST branch sets never got a chance to hold.

Two fixes, tested here: `decide()` holds a `start` for a literally-untrusted author (row 20a), and
the reconcile no longer re-dirties an item parked on a TRUST refusal whose head and labels are
unchanged.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_V2 = _ROOT / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import daemon, db, dispatch, github, observe  # noqa: E402
from lemd.config import load  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
OWNER = "gitchrisqueen"


def issue(**kw) -> observe.Snapshot:
    """An `agent:ready` issue with no work behind it — the row 16 `start` shape."""
    base = dict(kind="issue", number=2547, labels=frozenset({"agent:ready"}), state="OPEN",
                work_exists=False)
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap: observe.Snapshot) -> observe.Decision:
    return observe.decide(snap, **TTLS)


# ---------------------------------------------------------------- decide(): row 20a


@pytest.mark.parametrize("kw,held", [
    ({}, "issue_ready"),
    ({"work_exists": True, "has_open_pr": False}, "stranded_branch_no_pr"),
    ({"labels": frozenset({"agent:working"})}, "working_claim_stranded"),
    ({"labels": frozenset({"agent:working"}), "work_exists": True, "has_open_pr": False},
     "stranded_branch_no_pr"),
])
def test_an_untrusted_author_holds_every_start(kw, held):
    """Every route to a `start` is held, and the ledger names which one it held."""
    got = d(issue(author_trusted=False, **kw))
    assert (got.action, got.reason) == (observe.ACT_NONE, "untrusted_author")
    assert got.next_state == db.STATE_PARKED
    assert got.park_reason == "untrusted_author"
    assert got.wake_in == TTLS["ttl_parked"]
    assert got.details == {"held": held}


@pytest.mark.parametrize("trusted", [True, None])
def test_a_trusted_or_unreadable_author_dispatches_as_before(trusted):
    """Only a literal `False` holds: `None` dispatches and the action still re-checks."""
    got = d(issue(author_trusted=trusted))
    assert (got.action, got.mode, got.reason) == (observe.ACT_DISPATCH, "start", "issue_ready")


def test_a_non_dispatch_decision_is_untouched_by_the_author():
    """A wait (work in flight) or a park (merged link) is not a `start`, so nothing changes."""
    wait = d(issue(author_trusted=False, work_exists=True, has_open_pr=True))
    assert wait.reason == "start_already_produced_work"
    park = d(issue(author_trusted=False, work_exists=True, linked_pr_state="MERGED"))
    assert (park.action, park.reason) == (observe.ACT_PARK, "work_shipped_needs_close")


# ---------------------------------------------------------------- snapshot_issue: bounded read


def _wire(monkeypatch, *, labels: list[str], branch: bool = False, linked: str = "",
          open_pr: bool | None = None) -> list:
    """Fake every read `snapshot_issue` makes; returns the list of author reads."""
    calls: list = []

    def fake_author(slug, number, **kw):
        calls.append(kw)
        return False

    monkeypatch.setattr(github, "gh_json", lambda args, **k: {
        "number": 2547, "state": "OPEN", "labels": [{"name": n} for n in labels]})
    monkeypatch.setattr(github, "linked_pr_state", lambda *a, **k: linked)
    monkeypatch.setattr(github, "branch_exists", lambda *a, **k: branch)
    monkeypatch.setattr(github, "open_pr_for_branch", lambda *a, **k: open_pr)
    monkeypatch.setattr(github, "author_trusted", fake_author)
    return calls


def test_the_author_is_read_when_a_start_would_dispatch(monkeypatch):
    calls = _wire(monkeypatch, labels=["agent:ready"])
    listed = frozenset({"cqc-ea[bot]"})
    snap = observe.snapshot_issue("o/r", 2547, owner=OWNER, answer_logins=listed,
                                  associations=("OWNER",), app_login="app[bot]")
    assert snap.author_trusted is False
    assert calls == [{"answer_logins": listed, "associations": ("OWNER",),
                      "app_login": "app[bot]"}], "the daemon's trust config reaches the read"
    assert d(snap).reason == "untrusted_author"


def test_a_stranded_branch_reads_the_author_too(monkeypatch):
    calls = _wire(monkeypatch, labels=["agent:ready"], branch=True, open_pr=False)
    assert observe.snapshot_issue("o/r", 2547).author_trusted is False
    assert len(calls) == 1


@pytest.mark.parametrize("wire", [
    dict(labels=["agent:ready"], linked="OPEN"),        # in flight: waits, never dispatches
    dict(labels=["agent:ready"], linked="MERGED"),      # parks on the merged link
    dict(labels=["bug"]),                                # not the pipeline's
    dict(labels=["agent:ready", "agent:blocked"]),       # held: the hold branch decides
])
def test_no_author_read_where_it_cannot_change_the_answer(monkeypatch, wire):
    calls = _wire(monkeypatch, **wire)
    assert observe.snapshot_issue("o/r", 2547, owner=OWNER).author_trusted is None
    assert calls == []


# ---------------------------------------------------------------- the daemon: no loop


def _daemon(tmp_path: Path) -> daemon.Daemon:
    """A daemon allowed to act (not shadow), on a throwaway queue."""
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=0\n"
        "SLUG=christopherqueenconsulting/linkedin_engagement_manager\n"
    )
    return daemon.Daemon(load(tmp_path))


def _observe(dmn: daemon.Daemon, monkeypatch, **kw) -> None:
    monkeypatch.setattr(observe, "snapshot_issue", lambda *a, **k: issue(**kw))
    dmn._observe_one(db.get_item(dmn.conn, "issue", 2547))


def test_the_daemon_holds_and_warns_once(tmp_path, monkeypatch, caplog):
    """No `start` is queued, and the hold is said ONCE however often it is re-observed."""
    dmn = _daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_READY, dirty=1)
    with caplog.at_level(logging.WARNING, logger="lemd"):
        for _ in range(3):
            _observe(dmn, monkeypatch, author_trusted=False)
    row = db.get_item(dmn.conn, "issue", 2547)
    assert row["pending_mode"] is None
    assert (row["state"], row["parked_reason"]) == (db.STATE_PARKED, "untrusted_author")
    assert row["wake_at"] > time.time() + 3600
    warned = [r for r in caplog.records if "TRUST gate" in r.getMessage()]
    assert len(warned) == 1
    assert db.dispatchable(dmn.conn) == []


def test_the_daemon_hands_config_env_trust_knobs_to_the_snapshot(tmp_path, monkeypatch):
    """config.env's TRUSTED_ASSOCIATIONS / GH_APP_BOT_LOGIN, not the unit's environment, decide."""
    monkeypatch.delenv("TRUSTED_ASSOCIATIONS", raising=False)
    monkeypatch.delenv("GH_APP_BOT_LOGIN", raising=False)
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=0\n"
        'TRUSTED_ASSOCIATIONS="OWNER CONTRIBUTOR"\nGH_APP_BOT_LOGIN=pipe[bot]\n')
    dmn = daemon.Daemon(load(tmp_path))
    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_READY, dirty=1)
    got: dict = {}

    def fake(slug, number, **kw):
        got.update(kw)
        return issue(author_trusted=True)

    monkeypatch.setattr(observe, "snapshot_issue", fake)
    dmn._observe_one(db.get_item(dmn.conn, "issue", 2547))
    assert got["associations"] == ("OWNER", "CONTRIBUTOR")
    assert got["app_login"] == "pipe[bot]"


def test_a_now_trusted_author_dispatches_on_the_next_observation(tmp_path, monkeypatch):
    """The hold is not sticky: list the App (or re-file) and the next look starts it."""
    dmn = _daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_READY, dirty=1)
    _observe(dmn, monkeypatch, author_trusted=False)
    _observe(dmn, monkeypatch, author_trusted=True)
    assert db.get_item(dmn.conn, "issue", 2547)["pending_mode"] == "start"


def _reconcile_with(dmn: daemon.Daemon, monkeypatch, labels: list[str]) -> None:
    """One reconcile pass whose only listed item is #2547, carrying `labels`."""
    obj = {"number": 2547, "labels": [{"name": n} for n in labels]}
    monkeypatch.setattr(github, "list_by_label",
                        lambda slug, label, kind: [obj] if (label, kind) == ("agent:ready", "issue")
                        else [])
    monkeypatch.setattr(github, "open_pr_numbers_by_author", lambda *a, **k: [])
    dmn.reconcile()


@pytest.mark.parametrize("reason", ["trust_refused", "untrusted_author"])
def test_the_reconcile_leaves_a_standing_trust_hold_alone(tmp_path, monkeypatch, reason):
    """The loop's engine: reconcile re-dirtied the refused item every 10 minutes."""
    dmn = _daemon(tmp_path)
    dmn._last_reconcile = time.time() - 600  # not the first pass after a start
    labels = ["agent:ready"]
    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_PARKED,
                   parked_reason=reason, dirty=0, wake_at=int(time.time()) + 21600,
                   labels_json=json.dumps(labels))
    _reconcile_with(dmn, monkeypatch, labels)
    assert db.get_item(dmn.conn, "issue", 2547)["dirty"] == 0


def test_the_first_reconcile_after_a_restart_re_checks_every_trust_hold(tmp_path, monkeypatch):
    """Listing the App or widening the associations takes effect at a restart, not 6h later."""
    dmn = _daemon(tmp_path)
    labels = ["agent:ready"]
    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_PARKED,
                   parked_reason="untrusted_author", dirty=0, wake_at=int(time.time()) + 21600,
                   labels_json=json.dumps(labels))
    assert dmn._last_reconcile == 0.0
    _reconcile_with(dmn, monkeypatch, labels)
    assert db.get_item(dmn.conn, "issue", 2547)["dirty"] == 1
    # Observed and still refused: the hold stands again, and the NEXT pass leaves it alone.
    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_PARKED, dirty=0)
    _reconcile_with(dmn, monkeypatch, labels)
    assert db.get_item(dmn.conn, "issue", 2547)["dirty"] == 0


def test_the_reconcile_still_re_observes_a_changed_or_due_hold(tmp_path, monkeypatch):
    """Changed labels are drift, and a due TTL is the safety net — both look again."""
    dmn = _daemon(tmp_path)
    dmn._last_reconcile = time.time() - 600  # not the first pass after a start
    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_PARKED,
                   parked_reason="trust_refused", dirty=0, wake_at=int(time.time()) + 21600,
                   labels_json=json.dumps(["agent:ready"]))
    _reconcile_with(dmn, monkeypatch, ["agent:ready", "priority:high"])
    assert db.get_item(dmn.conn, "issue", 2547)["dirty"] == 1

    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_PARKED, dirty=0,
                   wake_at=int(time.time()) - 1)
    _reconcile_with(dmn, monkeypatch, ["agent:ready", "priority:high"])
    assert db.get_item(dmn.conn, "issue", 2547)["dirty"] == 1


def test_the_reconcile_still_re_dirties_any_other_park(tmp_path, monkeypatch):
    """Scoped to TRUST holds: an ordinary hold keeps its reconcile backstop."""
    dmn = _daemon(tmp_path)
    dmn._last_reconcile = time.time() - 600  # not the first pass after a start
    labels = ["agent:ready"]
    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_PARKED,
                   parked_reason="needs_human", dirty=0, wake_at=int(time.time()) + 21600,
                   labels_json=json.dumps(labels))
    _reconcile_with(dmn, monkeypatch, labels)
    assert db.get_item(dmn.conn, "issue", 2547)["dirty"] == 1


def test_an_ex_trust_refusal_is_not_redispatched_by_the_next_reconcile(tmp_path, monkeypatch):
    """End to end: refused once, then a reconcile — nothing is queued until the TTL or an event."""
    dmn = _daemon(tmp_path)
    dmn._last_reconcile = time.time() - 600  # not the first pass after a start
    labels = ["agent:ready"]
    db.upsert_item(dmn.conn, kind="issue", number=2547, state=db.STATE_RUNNING,
                   labels_json=json.dumps(labels))
    child = SimpleNamespace(kind="issue", number=2547, mode="start",
                            started=time.time() - 5, killed=False)
    monkeypatch.setattr(dmn.sup, "reap", lambda: [(child, dispatch.EX_TRUST)])
    dmn.collect()
    _reconcile_with(dmn, monkeypatch, labels)
    # Author unreadable (`None`) — the shape row 20a cannot catch — so only the reconcile fix holds.
    monkeypatch.setattr(observe, "snapshot_issue", lambda *a, **k: issue())
    dmn.observe_dirty()
    row = db.get_item(dmn.conn, "issue", 2547)
    assert (row["state"], row["parked_reason"], row["pending_mode"]) == (
        db.STATE_PARKED, "trust_refused", None)

    # An event on the item (the owner re-labelling it) still re-observes it at once.
    db.mark_dirty(dmn.conn, "issue", 2547)
    dmn.observe_dirty()
    assert db.get_item(dmn.conn, "issue", 2547)["pending_mode"] == "start"
