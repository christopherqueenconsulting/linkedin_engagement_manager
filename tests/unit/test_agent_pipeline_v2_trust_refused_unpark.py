"""A trust-refused owner answer re-dispatched `unpark` on every pass (#2331).

Measured on #2268, a deploy-hold filed by `github-actions[bot]` (CONTRIBUTOR): the owner answered
`1A`, `unpark.sh` exited `EX_TRUST`, and because `last_comment_id` was written only on rc=0 the same
reply decided `owner_answered` again on the next observation — 39 gh-pool runs in six hours. And
the menu that invited the answer could never have worked: the TRUST gate refuses that author.

Two fixes, tested here: a refused un-park spends its answer, and no menu is posted on an issue the
TRUST gate would refuse.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
_V2 = _ROOT / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import answers, daemon, db, dispatch, github, observe  # noqa: E402
from lemd.config import load  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
OWNER = "gitchrisqueen"
MENU = "🛑 **Human decision needed** — the pipeline has parked this issue (`human_hold_unasked`)."


def _daemon(tmp_path: Path) -> daemon.Daemon:
    """A daemon allowed to act (not shadow), on a throwaway queue."""
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=0\n"
        "SLUG=christopherqueenconsulting/linkedin_engagement_manager\n"
    )
    return daemon.Daemon(load(tmp_path))


def held_issue(**kw) -> observe.Snapshot:
    """The #2268 shape: a held issue with the owner's `1A` after the menu."""
    base = dict(kind="issue", number=2268, labels=frozenset({"needs-human"}), state="OPEN",
                menu_posted=True, answer=answers.Answer("a1", "answer", "1A"))
    base.update(kw)
    return observe.Snapshot(**base)


def _observe(dmn: daemon.Daemon, monkeypatch, snap_kw: dict) -> None:
    """One observation of #2268, routed through the real `_observe_one`."""
    def fake_snapshot(slug, number, *, owner=None, answer_routed=None):
        return held_issue(answer_routed=answer_routed, **snap_kw)

    monkeypatch.setattr(observe, "snapshot_issue", fake_snapshot)
    dmn._observe_one(db.get_item(dmn.conn, "issue", 2268))


def _finish(dmn: daemon.Daemon, monkeypatch, rc: int) -> None:
    """Fold one finished `unpark` back in, as `collect` does after a real run."""
    child = SimpleNamespace(kind="issue", number=2268, mode="unpark",
                            started=time.time() - 60, killed=False)
    monkeypatch.setattr(dmn.sup, "reap", lambda: [(child, rc)])
    dmn.collect()


# ---------------------------------------------------------------- the loop


def test_a_trust_refused_answer_is_attempted_once(tmp_path, monkeypatch):
    """The defect: refused, re-observed with the SAME answer, must not un-park again."""
    dmn = _daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=2268, state=db.STATE_PARKED,
                   parked_reason="human_hold_unasked")
    _observe(dmn, monkeypatch, {})
    row = db.get_item(dmn.conn, "issue", 2268)
    assert row["pending_mode"] == "unpark"

    db.force_state(dmn.conn, row["id"], db.STATE_RUNNING)
    _finish(dmn, monkeypatch, dispatch.EX_TRUST)
    row = db.get_item(dmn.conn, "issue", 2268)
    assert (row["state"], row["parked_reason"]) == (db.STATE_PARKED, "trust_refused")
    assert str(row["last_comment_id"]) == "a1"

    _observe(dmn, monkeypatch, {})
    row = db.get_item(dmn.conn, "issue", 2268)
    assert row["pending_mode"] != "unpark"
    assert row["state"] == db.STATE_PARKED


def test_the_re_observation_decides_none(tmp_path, monkeypatch):
    """Stated at the decision: the spent answer reads `answer_already_routed`, i.e. ACT_NONE."""
    got = observe.decide(held_issue(answer_routed="a1"), **TTLS)
    assert (got.action, got.reason) == (observe.ACT_NONE, "human_hold:answer_already_routed")


def test_a_new_owner_comment_after_a_refusal_is_still_read(tmp_path, monkeypatch):
    """Only THAT reply is spent — a later, different owner comment routes again."""
    dmn = _daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=2268, state=db.STATE_PARKED,
                   parked_reason="trust_refused", last_comment_id="a1")
    _observe(dmn, monkeypatch, {"answer": answers.Answer("a2", "answer", "1A please")})
    row = db.get_item(dmn.conn, "issue", 2268)
    assert row["pending_mode"] == "unpark"


def test_one_warning_per_refused_answer(tmp_path, monkeypatch, caplog):
    """One WARNING for the refusal, and none on the re-observations that follow it."""
    dmn = _daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=2268, state=db.STATE_PARKED)
    _observe(dmn, monkeypatch, {})
    db.force_state(dmn.conn, db.get_item(dmn.conn, "issue", 2268)["id"], db.STATE_RUNNING)
    with caplog.at_level(logging.WARNING, logger="lemd"):
        _finish(dmn, monkeypatch, dispatch.EX_TRUST)
        for _ in range(3):
            _observe(dmn, monkeypatch, {})
    refused = [r for r in caplog.records
               if r.levelno == logging.WARNING and "TRUST gate" in r.getMessage()]
    assert len(refused) == 1
    assert "a1" in refused[0].getMessage()


def test_a_refused_non_unpark_action_writes_no_answer(tmp_path, monkeypatch):
    """Only an un-park spends an answer; a refused `start` leaves `last_comment_id` alone."""
    dmn = _daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=2268, state=db.STATE_RUNNING)
    dmn._answer_ids[("issue", 2268)] = "a1"
    child = SimpleNamespace(kind="issue", number=2268, mode="start",
                            started=time.time() - 60, killed=False)
    monkeypatch.setattr(dmn.sup, "reap", lambda: [(child, dispatch.EX_TRUST)])
    dmn.collect()
    row = db.get_item(dmn.conn, "issue", 2268)
    assert row["last_comment_id"] is None
    assert row["parked_reason"] == "trust_refused"


def test_a_successful_unpark_still_spends_its_answer(tmp_path, monkeypatch):
    """The rc=0 branch is unchanged."""
    dmn = _daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=2268, state=db.STATE_PARKED)
    _observe(dmn, monkeypatch, {})
    db.force_state(dmn.conn, db.get_item(dmn.conn, "issue", 2268)["id"], db.STATE_RUNNING)
    _finish(dmn, monkeypatch, 0)
    row = db.get_item(dmn.conn, "issue", 2268)
    assert str(row["last_comment_id"]) == "a1"
    assert row["parked_reason"] is None


# ---------------------------------------------------------------- the dead-end menu


def unasked(**kw) -> observe.Snapshot:
    """Held, readable thread, no menu, no answer — the row 9a shape."""
    base = dict(kind="issue", number=2268, labels=frozenset({"needs-human"}), state="OPEN",
                menu_posted=False)
    base.update(kw)
    return observe.Snapshot(**base)


def test_no_menu_on_an_issue_the_trust_gate_refuses():
    """Row 9b: the owner's answer could never work, so keep the hold and post nothing."""
    got = observe.decide(unasked(author_trusted=False), **TTLS)
    assert got.action == observe.ACT_NONE
    assert got.reason == "human_hold_untrusted_author"
    assert got.next_state == db.STATE_PARKED
    assert got.wake_in == TTLS["ttl_parked"]


def test_a_trusted_or_unknown_author_still_gets_the_menu():
    """Only a literal `False` suppresses it — unreadable is never evidence of distrust."""
    assert observe.decide(unasked(author_trusted=True), **TTLS).action == observe.ACT_PARK
    assert observe.decide(unasked(author_trusted=None), **TTLS).action == observe.ACT_PARK
    assert observe.Snapshot(kind="issue", number=1).author_trusted is None


def _issue_api(monkeypatch, assoc: str, login: str) -> None:
    """Answer the REST issue read with one author."""
    monkeypatch.setattr(github, "gh_json", lambda args, **k: {
        "author_association": assoc, "user": {"login": login}})


def test_author_trusted_mirrors_the_guard(monkeypatch):
    """Same rules as `lib/guards.sh:author_trusted`."""
    monkeypatch.delenv("TRUSTED_ASSOCIATIONS", raising=False)
    monkeypatch.setenv("GH_APP_BOT_LOGIN", "cqc-lem-agent-pipeline[bot]")
    _issue_api(monkeypatch, "OWNER", OWNER)
    assert github.author_trusted("o/r", 1) is True
    _issue_api(monkeypatch, "CONTRIBUTOR", "github-actions[bot]")
    assert github.author_trusted("o/r", 1) is False
    _issue_api(monkeypatch, "NONE", "cqc-lem-agent-pipeline[bot]")
    assert github.author_trusted("o/r", 1) is True
    _issue_api(monkeypatch, "NONE", "stranger")
    assert github.author_trusted("o/r", 1) is False
    _issue_api(monkeypatch, "", "stranger")
    assert github.author_trusted("o/r", 1) is None


def test_author_trusted_fails_toward_asking(monkeypatch):
    """Unreadable, or a bot while the App login is unknown, reads `None` — never `False`."""
    monkeypatch.delenv("GH_APP_BOT_LOGIN", raising=False)
    _issue_api(monkeypatch, "NONE", "cqc-lem-agent-pipeline[bot]")
    assert github.author_trusted("o/r", 1) is None

    def boom(*a, **k):
        raise github.GitHubUnavailable("down")

    monkeypatch.setattr(github, "gh_json", boom)
    assert github.author_trusted("o/r", 1) is None


def test_author_trusted_honours_the_configured_associations(monkeypatch):
    """`TRUSTED_ASSOCIATIONS` narrows the set exactly as it does for the shell guard."""
    monkeypatch.setenv("TRUSTED_ASSOCIATIONS", "OWNER")
    monkeypatch.setenv("GH_APP_BOT_LOGIN", "app[bot]")
    _issue_api(monkeypatch, "COLLABORATOR", "someone")
    assert github.author_trusted("o/r", 1) is False


def _wire_issue(monkeypatch, comments: list[dict], seen: list) -> None:
    """Fake every `gh` read `snapshot_issue` makes for a bot-filed held issue."""
    def fake_gh_json(args, **k):
        if args[0] == "api":
            seen.append(args)
            return {"author_association": "CONTRIBUTOR", "user": {"login": "github-actions[bot]"}}
        if "comments" in args[-1]:
            return {"comments": comments}
        return {"number": 2268, "state": "OPEN", "labels": [{"name": "needs-human"}]}

    monkeypatch.setattr(github, "gh_json", fake_gh_json)
    monkeypatch.setenv("GH_APP_BOT_LOGIN", "app[bot]")


def test_snapshot_issue_reads_the_author_only_when_row_9a_could_fire(monkeypatch):
    """No menu yet: read it. A menu already up: skip the extra call."""
    seen: list = []
    _wire_issue(monkeypatch, [], seen)
    snap = observe.snapshot_issue("o/r", 2268, owner=OWNER)
    assert snap.menu_posted is False
    assert snap.author_trusted is False
    assert len(seen) == 1
    assert observe.decide(snap, **TTLS).reason == "human_hold_untrusted_author"

    seen.clear()
    _wire_issue(monkeypatch, [{"id": "m", "body": MENU, "author": {"login": OWNER}}], seen)
    snap = observe.snapshot_issue("o/r", 2268, owner=OWNER)
    assert snap.menu_posted is True
    assert snap.author_trusted is None
    assert seen == []
