"""Tests for the v2 owner-answer lane — the way out of a park.

v2 could park an item and never un-park it: nothing cleared a hold label, nothing reset a run
budget, and the lane that used to do both lived only in v1's `tick.sh`, which stands down whenever
the daemon heartbeat is fresh. Measured on #1313 — answered `1B`, still parked six hours later.

The cases below are organised around what must NOT happen: a stale answer re-routing a new park, an
ambiguous reply starting a build, an agent answering its own Decision Comment, and a parked item
falling out of the queue entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import answers, db, observe  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
OWNER = "gitchrisqueen"
DECISION = "🛑 **Human decision needed** — the pipeline has parked this pr (`selfreview_exhausted`)."


def comment(body: str, login: str = OWNER, cid: str = "c1") -> dict:
    """One GitHub comment as `gh --json comments` returns it."""
    return {"id": cid, "body": body, "author": {"login": login}}


def held(**kw) -> observe.Snapshot:
    """A parked PR snapshot, overridable per test."""
    base = dict(kind="pr", number=1, labels=frozenset({"needs-human", "agent:blocked"}),
                state="OPEN", branch="feature/x", head_sha="abc")
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


# ---------------------------------------------------------------- verdict shapes


@pytest.mark.parametrize("body", [
    "1B", "1b", "2 A", "1B — and please rebase first", "ok", "OK", "okay",
    "1A 2C (also research hosted grids) 3A",
])
def test_option_answers_are_answers(body):
    """v1's accepted answer shapes, byte for byte — the owner's habits must keep working."""
    assert answers.verdict_for(body) == "answer"


@pytest.mark.parametrize("body", [
    "@claude rebase this onto main and re-run the gate",
    "decision: drop the caching layer entirely",
    "go: ship it without the migration",
])
def test_off_menu_directives_are_directives(body):
    """An answer the agent never offered is still an answer."""
    assert answers.verdict_for(body) == "directive"


@pytest.mark.parametrize("body", [
    "B",                                   # v1's documented rejection: a bare letter is not an answer
    "2 things I want changed here",        # the [^A-Za-z0-9] guard after the option letter
    "thanks, looking at this now",
    "",
])
def test_non_decisions_are_silent(body):
    """The common case. A comment that is not a decision must read as nothing at all."""
    assert answers.verdict_for(body) is None


@pytest.mark.parametrize("body", [
    "1B but don't merge until the migration lands",
    "ok — hold off until Friday",
    "1A, not yet though",
    "@claude go ahead, but wait for the release window",
])
def test_a_hold_anywhere_outranks_a_leading_token(body):
    """Ambiguity fails toward the human: leading with `1B` does not license a build."""
    assert answers.verdict_for(body) == "hold"


def test_a_directive_ending_in_a_question_is_a_question():
    """Free-form and interrogative is a conversation, not an instruction."""
    assert answers.verdict_for("@claude should we drop the cache instead?") == "question"


def test_an_essay_is_not_a_decision():
    """v1's length bound: prose describing a problem must not be routed to a build."""
    assert answers.verdict_for("1B " + "x" * answers.MAX_BODY) is None


# ---------------------------------------------------------------- thread parsing


def test_only_comments_after_the_latest_decision_count():
    """A re-park must not instantly re-route on the answer to the PREVIOUS question."""
    thread = [comment(DECISION), comment("1B", cid="old"), comment(DECISION, cid="d2")]
    assert answers.parse(thread, OWNER) is None


def test_the_newest_owner_reply_wins():
    """The owner talking past their own earlier answer is what they meant most recently."""
    thread = [comment(DECISION), comment("1A", cid="a1"), comment("1C", cid="a2")]
    got = answers.parse(thread, OWNER)
    assert (got.verdict, got.comment_id) == ("answer", "a2")


def test_a_bot_comment_after_the_owner_cannot_bury_the_answer():
    """Non-owner comments are SKIPPED, never terminal — codecov posts after everything."""
    thread = [comment(DECISION), comment("1B", cid="a1"), comment("## Codecov report", "codecov")]
    assert answers.parse(thread, OWNER).comment_id == "a1"


def test_an_agent_comment_is_never_an_answer():
    """Under the PAT identity agents posted AS the owner; the body trailer is the second guard."""
    thread = [
        comment(DECISION),
        comment("1B\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"),
    ]
    assert answers.parse(thread, OWNER) is None


def test_no_decision_comment_means_no_answer():
    """`1B` on a thread that never asked anything is not an instruction to the pipeline."""
    assert answers.parse([comment("1B")], OWNER) is None


def test_a_thread_the_owner_has_moved_on_from_is_not_an_answer():
    """The newest owner comment decides. An older `1B` they have since talked past is stale."""
    thread = [comment(DECISION), comment("1B", cid="a1"), comment("actually let me look", cid="a2")]
    assert answers.parse(thread, OWNER) is None


# ---------------------------------------------------------------- the decision


def test_an_actionable_answer_un_parks():
    """The whole defect: a held item with an owner answer must leave the hold."""
    got = d(held(answer=answers.Answer("a1", "answer", "1B")))
    assert (got.action, got.mode, got.reason) == (observe.ACT_UNPARK, "unpark", "owner_answered")


def test_a_directive_un_parks_too():
    """An off-menu instruction is an answer — v1 routed it and so must this."""
    assert d(held(answer=answers.Answer("a1", "directive", "@claude rebase"))).action \
        == observe.ACT_UNPARK


@pytest.mark.parametrize("verdict", ["hold", "question"])
def test_an_ambiguous_reply_leaves_the_work_parked(verdict):
    """Recognised so it can be refused BY NAME, not so it can act."""
    got = d(held(answer=answers.Answer("a1", verdict, "…")))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_PARKED
    assert got.reason == f"human_hold:{verdict}"


def test_no_answer_is_still_a_plain_hold():
    """The unanswered park keeps its original reason, so existing telemetry still reads."""
    got = d(held())
    assert (got.action, got.reason) == (observe.ACT_NONE, "human_hold")


def test_the_same_answer_is_never_routed_twice():
    """After a successful un-park the reply stays the newest comment in the thread for ever.

    A LATER park — raised for a different reason entirely — must not consume it a second time.
    """
    got = d(held(answer=answers.Answer("a1", "answer", "1B"), answer_routed="a1"))
    assert got.action == observe.ACT_NONE
    assert got.reason == "human_hold:answer_already_routed"


def test_a_different_answer_after_a_routed_one_does_route():
    """The dedup is per COMMENT, not a one-shot latch — the owner may answer twice."""
    got = d(held(answer=answers.Answer("a2", "answer", "1C"), answer_routed="a1"))
    assert got.action == observe.ACT_UNPARK


def test_an_answer_never_resurrects_a_merged_pr():
    """Terminal facts still outrank everything, holds and answers alike."""
    got = d(held(state="MERGED", answer=answers.Answer("a1", "answer", "1B")))
    assert got.action == observe.ACT_CLOSE


def test_an_unreadable_snapshot_is_never_un_parked():
    """`readable=False` is a decision to do nothing — the rule #1082 was built from."""
    got = d(held(readable=False, answer=answers.Answer("a1", "answer", "1B")))
    assert got.action == observe.ACT_NONE


def test_a_held_issue_is_un_parked_by_an_answer_on_its_own_thread():
    """The issue half, which v1 had: a Decision Comment before any PR lives only on the issue."""
    snap = observe.Snapshot(kind="issue", number=7,
                            labels=frozenset({"needs-human", "agent:blocked"}),
                            answer=answers.Answer("a1", "answer", "ok"))
    assert d(snap).action == observe.ACT_UNPARK


# ---------------------------------------------------------------- the reads around it


def test_the_answer_read_is_skipped_when_nothing_is_held(monkeypatch):
    """Cost gate: one `gh view --json comments` per HELD item, never on the hot path."""
    calls = []
    monkeypatch.setattr(answers, "newest", lambda *a, **k: calls.append(a))
    assert observe._answer_for("slug", "pr", 1, frozenset({"agent:working"}), OWNER) is None
    assert calls == []


def test_the_answer_read_happens_for_a_held_item(monkeypatch):
    """...and it DOES happen when the item is held, or the lane never fires at all."""
    monkeypatch.setattr(answers, "newest",
                        lambda *a, **k: answers.Answer("a1", "answer", "1B"))
    got = observe._answer_for("slug", "pr", 1, frozenset({"needs-human"}), OWNER)
    assert got.verdict == "answer"


def test_an_unreadable_thread_leaves_the_item_parked(monkeypatch):
    """An unreadable read collapses to "no answer", which is the same outcome: stay parked."""
    def boom(*_a, **_k):
        raise answers.github.GitHubUnavailable("gh exploded")
    monkeypatch.setattr(answers.github, "gh_json", boom)
    assert answers.newest("slug", "pr", 1, OWNER) is None


def test_reconcile_queries_both_hold_labels_for_both_kinds():
    """`park.sh` strips `agent:working`, so without these a parked item leaves the queue for ever.

    Asserted against the source because the query list is a literal tuple inside the method: #1274
    was parked with both labels and absent from the queue entirely.
    """
    src = (_V2 / "lemd" / "daemon.py").read_text()
    for label in ("needs-human", "agent:blocked"):
        for kind in ("pr", "issue"):
            assert f'("{label}", "{kind}")' in src


def test_unpark_is_a_gh_pool_action_everywhere_it_is_named():
    """The pool sets in `act()` and in restart adoption must agree, or a restart mis-counts caps."""
    daemon_src = (_V2 / "lemd" / "daemon.py").read_text()
    dispatch_src = (_V2 / "lemd" / "dispatch.py").read_text()
    assert '"gh" if mode in ("merge", "park", "unpark")' in daemon_src
    assert '"gh" if row["mode"] in ("merge_enable", "park", "unpark")' in dispatch_src


def test_the_unpark_action_exists_and_is_executable():
    """The daemon resolves the action by filename; a missing script is a silent no-op."""
    script = _V2 / "actions" / "unpark.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111


def test_the_unpark_action_undrafts_before_it_relabels():
    """#1236's ordering rule: a draft can hold neither auto-merge nor a queue entry."""
    src = (_V2 / "actions" / "unpark.sh").read_text()
    assert src.index("gh pr ready") < src.index("--add-label \"agent:revise\"")


def test_the_unpark_action_resets_the_run_ledger():
    """Without this the six `*_exhausted` parks re-park on their next dispatch, answer or not."""
    assert "ledger_reset" in (_V2 / "actions" / "unpark.sh").read_text()


# ---------------------------------------------------------------- admission of held items


def test_a_hold_admits_an_item_that_lost_its_agent_label():
    """The park is unanswerable otherwise, and that is not hypothetical.

    Issues #1284 and #1285 hold `needs-human` and no `agent:*` label at all, so admission refused
    them one branch BEFORE the answer was considered: the reply would have been read, classified,
    and then discarded.
    """
    snap = observe.Snapshot(kind="issue", number=1284, labels=frozenset({"needs-human"}),
                            answer=answers.Answer("a1", "answer", "1B"))
    assert d(snap).action == observe.ACT_UNPARK


def test_an_unlabelled_item_without_a_hold_is_still_not_ours():
    """Admission widened for HELD items only — a stray issue is still none of the pipeline's."""
    snap = observe.Snapshot(kind="issue", number=9, labels=frozenset({"priority:low"}))
    got = d(snap)
    assert (got.action, got.reason) == (observe.ACT_NONE, "not_admissible:no_agent_label")


def test_a_held_item_without_an_answer_still_just_waits():
    """Widening admission must not turn a hold into work."""
    snap = observe.Snapshot(kind="issue", number=1285, labels=frozenset({"needs-human"}))
    assert d(snap).reason == "human_hold"


def _guard_calls(src: str) -> list[str]:
    """The guard invocations in `unpark.sh`, ignoring the prose that explains them."""
    return [ln.strip() for ln in src.splitlines()
            if ln.strip().startswith("if !") and ("trust" in ln or "author" in ln or "answered" in ln)]


def test_admission_is_not_authorisation():
    """The action re-asks a trust question on BOTH paths; a hold label never grants work itself."""
    src = (_V2 / "actions" / "unpark.sh").read_text()
    calls = _guard_calls(src)
    assert len(calls) == 2, calls          # the PR path and the issue path
    assert 'exit "$EX_TRUST"' in src


def test_a_fork_pr_is_never_un_parked():
    """The fork refusal outranks admission by hold, as it outranks every other lane."""
    snap = held(upstream=False, answer=answers.Answer("a1", "answer", "1B"))
    assert d(snap).action == observe.ACT_NONE


# ---------------------------------------------------------------- and it has to actually run


def test_finishing_work_is_dispatched_before_starting_new_work(tmp_path):
    """An un-park that never reaches a slot has not unblocked anything.

    The backlog is 40+ `agent:ready` issues all stamped with the same, older `ready_since`, so
    ordering by time alone put every one of them ahead of a PR that had just become dispatchable.
    The WIP gate does not save it: that counts PRs IN FLIGHT, and a PR queued behind the backlog is
    not in flight, so the gate reads zero and lets the starts through.
    """
    conn = db.connect(tmp_path / "q.db")
    db.upsert_item(conn, kind="issue", number=100, state=db.STATE_READY, pending_mode="start")
    conn.execute("UPDATE items SET ready_since=1000 WHERE number=100")
    db.upsert_item(conn, kind="pr", number=200, state=db.STATE_READY, pending_mode="revise")
    conn.execute("UPDATE items SET ready_since=9000 WHERE number=200")

    order = [(r["kind"], r["number"]) for r in db.dispatchable(conn)]
    assert order[0] == ("pr", 200), "the newer revise must outrank the older start"


def test_priority_still_orders_within_the_same_lane(tmp_path):
    """Starts sorting last must not flatten the priority ordering among themselves."""
    conn = db.connect(tmp_path / "q.db")
    db.upsert_item(conn, kind="issue", number=1, state=db.STATE_READY, pending_mode="start",
                   priority=5)
    db.upsert_item(conn, kind="issue", number=2, state=db.STATE_READY, pending_mode="start",
                   priority=1)
    assert [r["number"] for r in db.dispatchable(conn)] == [2, 1]


# ---------------------------------------------------------------- the action's half of the gate


def test_the_unpark_asks_about_the_answer_not_about_a_label():
    """`v2_trust_ok issue` asks who applied `agent:ready`. On this path there may never be one.

    Issue #1360 was authored AND answered by the owner, was never in the work queue, and was
    refused with "no readable 'agent:ready' labeler" — so the owner's answer to their own issue did
    nothing, silently, which is the exact failure this lane exists to remove. The label is a proxy
    for "who queued this"; here the authorising act is the answer.
    """
    src = (_V2 / "actions" / "unpark.sh").read_text()
    issue_guard = [c for c in _guard_calls(src) if "$TISS" in c]
    assert len(issue_guard) == 1
    # Asserted on the CALL, not on the file: the comment above it names `v2_trust_ok` to explain
    # what changed and why, and a substring test over prose would pass for the wrong reason.
    assert "v2_owner_answered issue" in issue_guard[0]
    assert "v2_trust_ok" not in issue_guard[0]
    # ...and the other half is still asked: a stranger's issue must not become agent work.
    assert 'author_trusted "$TISS"' in issue_guard[0]


def test_the_authorship_check_cannot_be_buried_by_a_bot():
    """The action and `answers.parse` must agree about the same thread.

    Selecting the newest eligible comment and then comparing its author would let any bot bury the
    answer by commenting after it — codecov posts on every push. The parser skips non-owner
    comments rather than stopping at them, so the shell filter has to select the owner INSIDE the
    query, not after it.
    """
    src = (_V2 / "actions" / "common.sh").read_text()
    body = src[src.index("v2_owner_answered()"):]
    select_owner = body.index('select((.author.login // "") == ')
    take_last = body.index("| (last // empty)")
    assert select_owner < take_last, "the owner filter must run before `last`"


def test_the_authorship_check_refuses_an_unreadable_thread():
    """Unreadable is a refusal here as everywhere else in `common.sh`."""
    src = (_V2 / "actions" / "common.sh").read_text()
    body = src[src.index("v2_owner_answered()"):]
    assert 'refusing.' in body
    assert 'return 1' in body
