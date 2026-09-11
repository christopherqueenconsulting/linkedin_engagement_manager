"""An item that ARRIVES already holding `needs-human` never gets a menu, so the hold is permanent (#1736).

Every Decision Comment in v2 is written by `park.sh`, which runs only on `ACT_PARK`. An issue that
was already `needs-human` on first observation — the triage cron's route, a hand-filed issue — took
the `human_hold` branch instead, nothing was ever asked, and the answer lane below it had nothing to
read for ever. Six issues sat like that on 2026-08-29; the issue for this fix was one of them.

The fix adds ONE fact (`Snapshot.menu_posted`) and ONE branch, and the cases below are the four the
issue names: never-asked parks once, already-asked never duplicates, an unreadable thread posts
nothing, and a held PR with auto-merge armed still disarms first.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_V2 = _ROOT / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import answers, daemon, db, github, observe  # noqa: E402
from lemd.config import load  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
OWNER = "gitchrisqueen"
MENU = "🛑 **Human decision needed** — the pipeline has parked this issue (`start_exhausted`)."
GREEN = github.ChecksState(failed=0, pending=0, total=6)


def held_issue(**kw) -> observe.Snapshot:
    """An issue that arrived carrying `needs-human` and nothing the pipeline wrote."""
    base = dict(kind="issue", number=1736, labels=frozenset({"bug", "needs-human"}),
                state="OPEN")
    base.update(kw)
    return observe.Snapshot(**base)


def held_pr(**kw) -> observe.Snapshot:
    """A PR a human held by hand, green underneath."""
    base = dict(kind="pr", number=1, labels=frozenset({"agent:working", "needs-human"}),
                state="OPEN", branch="feature/x", head_sha="abc", merge_state="CLEAN",
                checks=GREEN, review_fresh=True)
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap, **kw):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS, **kw)


def comment(body: str, login: str = OWNER, cid: str = "c1") -> dict:
    """One GitHub comment as `gh --json comments` returns it."""
    return {"id": cid, "body": body, "author": {"login": login}}


# ---------------------------------------------------------------- the four cases, decided


def test_never_asked_parks_once_and_asks():
    """The defect. Held, readable thread, no menu on it: ONE park so `park.sh` posts the menu."""
    got = d(held_issue(menu_posted=False))
    assert got.action == observe.ACT_PARK
    assert (got.reason, got.park_reason, got.mode) == (
        "human_hold_unasked", "human_hold_unasked", "park")
    assert got.next_state == db.STATE_PARKED


def test_already_asked_is_the_ordinary_hold():
    """The second observation — and #1734, parked by the daemon — reads the menu and waits."""
    got = d(held_issue(menu_posted=True))
    assert (got.action, got.reason) == (observe.ACT_NONE, "human_hold")
    assert got.wake_in == TTLS["ttl_parked"]


def test_an_unreadable_thread_posts_nothing():
    """`None` is not `False`. An unreadable thread is never evidence that no menu exists."""
    got = d(held_issue(menu_posted=None))
    assert (got.action, got.reason) == (observe.ACT_NONE, "human_hold")


def test_the_default_is_the_safe_value():
    """A snapshot that never read the thread must decide exactly as it did before #1736."""
    assert observe.Snapshot(kind="issue", number=1).menu_posted is None
    assert d(held_issue()).reason == "human_hold"


def test_an_armed_held_pr_disarms_before_any_menu_logic():
    """Safety ordering unchanged: the arm comes off first, the question waits one observation."""
    got = d(held_pr(auto_merge=True, menu_posted=False))
    assert (got.action, got.reason) == (observe.ACT_DISARM, "human_hold_armed")
    # ...and with the arm gone, the very same snapshot asks.
    assert d(held_pr(auto_merge=False, menu_posted=False)).action == observe.ACT_PARK


def test_a_bare_agent_blocked_is_not_a_question_for_the_owner():
    """`agent:blocked` alone is a dependency hold; only `needs-human` earns a menu."""
    got = d(held_issue(labels=frozenset({"agent:blocked"}), menu_posted=False))
    assert (got.action, got.reason) == (observe.ACT_NONE, "human_hold")


def test_an_answer_is_routed_rather_than_re_asked():
    """The answer rows sit above the unasked row: a reply that exists is never asked again."""
    got = d(held_issue(menu_posted=False, answer=answers.Answer("a1", "answer", "1A")))
    assert got.action == observe.ACT_UNPARK


def test_a_hold_answer_still_outranks_the_unasked_row():
    """`hold`/`question` verdicts are refused by name, not re-asked."""
    got = d(held_issue(menu_posted=False, answer=answers.Answer("a1", "hold", "not yet")))
    assert (got.action, got.reason) == (observe.ACT_NONE, "human_hold:hold")


def test_terminal_and_unreadable_snapshots_still_win():
    """Rows 1-3 are above the hold branch, and nothing here changes that."""
    assert d(held_issue(state="CLOSED", menu_posted=False)).action == observe.ACT_CLOSE
    assert d(held_issue(readable=False, menu_posted=False)).action == observe.ACT_NONE


def test_the_park_is_lap_counted_like_any_other():
    """An owner who keeps releasing a re-applied hold still hits the give-up rule (row 6)."""
    got = d(held_issue(menu_posted=True, parked_reason="human_hold_unasked", park_laps=3,
                       answer=answers.Answer("a1", "answer", "1A")), max_park_laps=3)
    assert (got.action, got.reason) == (observe.ACT_ABANDON, "park_laps_exhausted")


# ---------------------------------------------------------------- the fact, read off the thread


def test_a_decision_comment_by_body_is_the_menu():
    """Recognised by the marker, whoever posted it — the pipeline has had two logins."""
    assert answers.menu_posted([comment(MENU, login="cqc-lem-agent-pipeline")]) is True
    assert answers.menu_posted([comment(MENU, login=OWNER)]) is True


def test_a_thread_without_the_marker_is_unasked():
    """An owner addendum (#1732's shape) is not a menu."""
    assert answers.menu_posted([]) is False
    assert answers.menu_posted([comment("also: check the rate limiter first")]) is False


def test_read_thread_carries_both_halves_from_one_call(monkeypatch):
    """`answer` and `menu_posted` come off the SAME read — no new call on the held path."""
    calls = []

    def gh(args, **_kw):
        calls.append(args)
        return {"comments": [comment(MENU, login="bot", cid="m"), comment("1B", cid="a1")]}

    monkeypatch.setattr(answers.github, "gh_json", gh)
    got = answers.read_thread("o/r", "issue", 1736, OWNER)
    assert got.menu_posted is True
    assert got.answer.comment_id == "a1"
    assert len(calls) == 1


def test_read_thread_is_unreadable_safe(monkeypatch):
    """An unreadable thread is `None` on BOTH halves, never `False`."""
    def boom(*_a, **_k):
        raise github.GitHubUnavailable("gh exploded")
    monkeypatch.setattr(answers.github, "gh_json", boom)
    got = answers.read_thread("o/r", "issue", 1736, OWNER)
    assert got == answers.UNREADABLE
    assert got.menu_posted is None and got.answer is None


def test_newest_is_still_the_answer_half():
    """The pre-#1736 entry point keeps its contract for anything that still calls it."""
    assert answers.newest.__doc__ and "answer half" in answers.newest.__doc__


def test_snapshot_issue_reads_the_menu_for_a_held_issue(monkeypatch):
    """The wiring: a held issue's snapshot carries what the thread says."""
    def gh(args, **_kw):
        if "comments" in args:
            return {"comments": []}
        return {"number": 1736, "state": "OPEN", "labels": [{"name": "needs-human"}]}
    monkeypatch.setattr(github, "gh_json", gh)
    snap = observe.snapshot_issue("o/r", 1736, owner=OWNER)
    assert snap.menu_posted is False
    assert d(snap).action == observe.ACT_PARK


def test_snapshot_issue_reads_nothing_for_an_unheld_issue(monkeypatch):
    """The cost gate holds: no hold label, no comments read, `menu_posted` stays `None`."""
    calls = []

    def gh(args, **_kw):
        calls.append(args)
        return {"number": 5, "state": "OPEN", "labels": [{"name": "bug"}]}
    monkeypatch.setattr(github, "gh_json", gh)
    snap = observe.snapshot_issue("o/r", 5, owner=OWNER)
    assert snap.menu_posted is None
    assert not any("comments" in c for c in calls)


def test_snapshot_pr_reads_the_menu_for_a_held_pr(monkeypatch):
    """The PR builder is wired the same way."""
    monkeypatch.setattr(github, "pr_facts", lambda *a, **k: {
        "state": "OPEN", "labels": [{"name": "needs-human"}], "headRefName": "feature/x",
        "headRefOid": "abc", "mergeStateStatus": "CLEAN"})
    monkeypatch.setattr(github, "checks_for", lambda *a, **k: GREEN)
    monkeypatch.setattr(github, "merge_queue_state", lambda *a, **k: "")
    monkeypatch.setattr(github, "review_state", lambda *a, **k: github.ReviewState(
        fresh=True, unresolved=0))
    monkeypatch.setattr(github, "gh_json", lambda args, **k: {"comments": [comment(MENU)]})
    snap = observe.snapshot_pr("o/r", 1, owner=OWNER)
    assert snap.menu_posted is True


# ---------------------------------------------------------------- it reaches park.sh


def _acting_daemon(tmp_path: Path) -> daemon.Daemon:
    """A daemon allowed to act (not shadow), on a throwaway queue."""
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=0\n"
        "SLUG=christopherqueenconsulting/linkedin_engagement_manager\n"
    )
    return daemon.Daemon(load(tmp_path))


def test_the_daemon_queues_the_park(tmp_path, monkeypatch):
    """`ACT_PARK` must land in the state `act()` dispatches `park.sh` from, with this reason."""
    dmn = _acting_daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=1736, state=db.STATE_PARKED)
    monkeypatch.setattr(observe, "snapshot_issue", lambda *a, **k: held_issue(menu_posted=False))
    dmn._observe_one(db.get_item(dmn.conn, "issue", 1736))
    row = db.get_item(dmn.conn, "issue", 1736)
    assert (row["state"], row["pending_mode"]) == (db.STATE_READY, "park")
    assert row["parked_reason"] == "human_hold_unasked"


def test_the_second_observation_keeps_the_specific_reason(tmp_path, monkeypatch):
    """Once the menu is up the item takes row 10, and the generic reason must not erase this one.

    The un-park routes on `parked_reason`; `needs_human` would route a PR to `agent:revise` with
    no feedback to apply — the treadmill `_keep_specific_park_reason` exists to stop.
    """
    dmn = _acting_daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=1736, state=db.STATE_PARKED,
                   parked_reason="human_hold_unasked")
    monkeypatch.setattr(observe, "snapshot_issue", lambda *a, **k: held_issue(menu_posted=True))
    dmn._observe_one(db.get_item(dmn.conn, "issue", 1736))
    row = db.get_item(dmn.conn, "issue", 1736)
    assert (row["state"], row["pending_mode"]) == (db.STATE_PARKED, None)
    assert row["parked_reason"] == "human_hold_unasked"


def test_the_launched_action_carries_the_reason_and_its_detail(tmp_path, monkeypatch):
    """`park.sh`'s fallback detail says a budget was spent, which this park did not."""
    dmn = _acting_daemon(tmp_path)
    db.upsert_item(dmn.conn, kind="issue", number=1736, state=db.STATE_READY,
                   pending_mode="park", parked_reason="human_hold_unasked")
    seen = {}

    def fake_gh(*, action, kind, number, args, item_id):
        seen.update(action=action, args=args)
        return object()

    monkeypatch.setattr(dmn.sup, "dispatch_gh", fake_gh)
    dmn._launch(db.get_item(dmn.conn, "issue", 1736), "park")
    assert seen["action"] == "park"
    assert seen["args"][:3] == ["issue", "1736", "human_hold_unasked"]
    assert "no budget was spent" in seen["args"][3]


def test_the_detail_is_registered():
    """Derived list, so the reason cannot drift out of `PARK_DETAILS` silently."""
    assert "human_hold_unasked" in observe.PARK_DETAILS
    assert "no question was ever put to you" in observe.PARK_DETAILS["human_hold_unasked"]


# ---------------------------------------------------------------- the action, exactly once


@pytest.fixture
def action_tree(tmp_path: Path):
    """A scratch tree holding `park.sh` next to a STUBBED `common.sh` and a scripted `gh`.

    `common.sh` hard-sets `PATH`, so a fake `gh` on PATH cannot reach the real bootstrap; the stub
    stands in for exactly the names `park.sh` uses. The fake `gh` keeps a JSON thread on disk and
    runs the real `jq` over it, so the marker query itself is exercised — and a posted comment
    lands in that thread, which is what lets the second run see the first.
    """
    actions = tmp_path / "v2" / "actions"
    actions.mkdir(parents=True)
    (tmp_path / "state").mkdir()
    calls = tmp_path / "calls.txt"
    labels = tmp_path / "labels.txt"
    labels.write_text("bug needs-human")
    thread = tmp_path / "thread.json"
    thread.write_text(json.dumps({"comments": []}))

    (actions / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{calls}"\n'
        'JQ=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "--jq" ] && JQ="$a"; prev="$a"; done\n'
        'case "$*" in\n'
        f'  *"--json labels"*) cat "{labels}" ;;\n'
        '  *"--json comments"*)\n'
        f'    [ -f "{tmp_path}/unreadable" ] && exit 1\n'
        f'    jq -r "$JQ" "{thread}" ;;\n'
        '  *" comment "*)\n'
        '    body=""; prev=""\n'
        '    for a in "$@"; do [ "$prev" = "--body" ] && body="$a"; prev="$a"; done\n'
        f'    jq --arg b "$body" \'.comments += [{{"body": $b, "author": {{"login": "bot"}}}}]\' '
        f'"{thread}" > "{thread}.new" && mv "{thread}.new" "{thread}" ;;\n'
        '  *"--json headRefOid"*) echo "abc123def456" ;;\n'
        'esac\nexit 0\n'
    )
    (actions / "gh").chmod(0o755)

    (actions / "common.sh").write_text(
        "set -uo pipefail\n"
        f'BASE="{tmp_path}"\nSLUG="o/r"\nASSIGNEE="{OWNER}"\nDRY_RUN="${{DRY_RUN:-0}}"\n'
        f'export PATH="{actions}:$PATH"\n'
        "EX_TRUST=70; EX_BUDGET=71; EX_BUSY=72; EX_SETUP=73\n"
        'log() { echo "[v2/${V2_ACTION:-action}] $*"; }\n'
        "v2_hold_present() {\n"
        '  local l; l="$(gh "$1" view "$2" --repo "$SLUG" --json labels --jq . 2>/dev/null)"\n'
        '  [ -n "$l" ] || { log "#$2 labels unreadable — treating as held."; return 0; }\n'
        '  case " $l " in *" needs-human "*|*" agent:blocked "*) return 0 ;; esac\n'
        "  return 1\n}\n"
        'issue_for_pr() { echo ""; }\n'
    )
    (actions / "park.sh").write_text((_V2 / "actions" / "park.sh").read_text())
    (actions / "park.sh").chmod(0o755)
    return tmp_path, actions, calls, labels, thread


def _run(tree, *args: str):
    """Run the copied `park.sh` from the scratch tree."""
    tmp_path, actions, _, _, _ = tree
    return subprocess.run(
        ["bash", str(actions / "park.sh"), *args],
        capture_output=True, text=True, timeout=30,
        env={"PATH": f"{actions}:/usr/bin:/bin", "BASE": str(tmp_path), "HOME": str(tmp_path)},
    )


def _menus(tree) -> list[str]:
    """Every comment on the fake thread that carries the marker."""
    body = json.loads(tree[4].read_text())["comments"]
    return [c["body"] for c in body if "human decision needed" in c["body"].lower()]


def test_the_unasked_park_posts_exactly_one_menu(action_tree):
    """Held, no menu: the comment goes up, once, and names the reason and its detail."""
    got = _run(action_tree, "issue", "1736", "human_hold_unasked", "Nothing was attempted.")
    assert got.returncode == 0, got.stdout + got.stderr
    menus = _menus(action_tree)
    assert len(menus) == 1
    assert "`human_hold_unasked`" in menus[0] and "Nothing was attempted." in menus[0]
    assert "**A. Try again as-is**" in menus[0] and "recommendation: `1A`" in menus[0]
    assert (action_tree[0] / "state" / "v2park-issue-1736.sha").read_text().strip() == "none"


def test_a_second_run_never_posts_a_second_menu(action_tree):
    """The thread is the key: the second pass sees the first pass's comment and stops."""
    assert _run(action_tree, "issue", "1736", "human_hold_unasked").returncode == 0
    second = _run(action_tree, "issue", "1736", "human_hold_unasked")
    assert second.returncode == 0
    assert "not asking twice" in second.stdout
    assert len(_menus(action_tree)) == 1


def test_a_menu_the_daemon_posted_is_not_duplicated(action_tree):
    """#1734's shape: parked by the daemon, menu already up, and nothing changes."""
    action_tree[4].write_text(json.dumps({"comments": [comment(MENU, login=OWNER)]}))
    got = _run(action_tree, "issue", "1734", "human_hold_unasked")
    assert got.returncode == 0 and "not asking twice" in got.stdout
    assert len(_menus(action_tree)) == 1


def test_an_unreadable_thread_refuses_to_post(action_tree):
    """Unreadable is never "no menu"."""
    (action_tree[0] / "unreadable").touch()
    got = _run(action_tree, "issue", "1736", "human_hold_unasked")
    assert got.returncode == 0 and "refusing" in got.stdout
    assert " comment " not in action_tree[2].read_text()


def test_a_lifted_hold_asks_nothing(action_tree):
    """The owner removed the label between the decision and the action: nobody is waiting."""
    action_tree[3].write_text("bug agent:ready")
    got = _run(action_tree, "issue", "1736", "human_hold_unasked")
    assert got.returncode == 0 and "nothing to ask" in got.stdout
    assert _menus(action_tree) == []


def test_a_stale_state_file_cannot_silence_the_ask(action_tree):
    """A key left by a menu that never landed must not make this park a permanent no-op."""
    (action_tree[0] / "state" / "v2park-issue-1736.sha").write_text("none\n")
    got = _run(action_tree, "issue", "1736", "human_hold_unasked")
    assert got.returncode == 0
    assert len(_menus(action_tree)) == 1


def test_every_other_reason_still_no_ops_on_a_held_item(action_tree):
    """The pre-#1736 contract for a budget park is untouched: already held means already asked."""
    got = _run(action_tree, "issue", "1736", "start_exhausted")
    assert got.returncode == 0 and "already held" in got.stdout
    assert _menus(action_tree) == []


def test_a_dry_run_posts_nothing(action_tree):
    """`DRY_RUN` is honoured after the thread check, before any write."""
    tmp_path, actions, _, _, _ = action_tree
    got = subprocess.run(
        ["bash", str(actions / "park.sh"), "issue", "1736", "human_hold_unasked"],
        capture_output=True, text=True, timeout=30,
        env={"PATH": f"{actions}:/usr/bin:/bin", "BASE": str(tmp_path), "HOME": str(tmp_path),
             "DRY_RUN": "1"},
    )
    assert got.returncode == 0 and "DRY_RUN" in got.stdout
    assert _menus(action_tree) == []


def test_the_pr_order_is_unchanged_for_the_unasked_case(action_tree):
    """Draft first, then --disable-auto, then labels, then ONE comment — on this path too."""
    got = _run(action_tree, "pr", "42", "human_hold_unasked")
    assert got.returncode == 0, got.stdout + got.stderr
    calls = action_tree[2].read_text()
    assert calls.index("pr ready 42 --repo o/r --undo") < calls.index("--disable-auto") \
        < calls.index('--add-label needs-human') < calls.index("pr comment 42")
    assert calls.count("pr comment 42") == 1


def test_the_stub_matches_the_real_bootstrap():
    """Every name the stubbed `common.sh` provides must exist in the real one."""
    real = (_V2 / "actions" / "common.sh").read_text()
    for name in ("v2_hold_present()", "log()", "EX_TRUST=", "EX_BUDGET=", "EX_BUSY=", "EX_SETUP="):
        assert name in real, f"the stub provides {name}, the real common.sh no longer does"
    assert "issue_for_pr" in (_ROOT / "scripts" / "agent-pipeline" / "lib" / "guards.sh").read_text()


def test_the_marker_agrees_across_all_three_readers():
    """`answers.py`, `common.sh` and `park.sh` must recognise the SAME menu, or one posts twice."""
    assert answers._DECISION.pattern.lower() == "human decision needed"
    assert 'test("Human decision needed"; "i")' in (_V2 / "actions" / "common.sh").read_text()
    park = (_V2 / "actions" / "park.sh").read_text()
    assert 'test("Human decision needed"; "i")' in park
    assert "**Human decision needed**" in park, "the menu park.sh writes must carry the marker"
