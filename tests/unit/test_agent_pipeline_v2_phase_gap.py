"""The v2 phase guard: a PR may not close a phased issue and lose the rest (#1396).

v1 asked this question at the merge gate and answered it itself, from the issue body and a prose
regex (`phase_guard_ok`). v2 does not, on purpose: judging acceptance-criteria coverage from a diff
is the call an LLM gets confidently wrong, and a wrong hold costs a human decision every time it
fires. So the judgement is made ONCE, by `MODE=selfreview` — which has already read the issue, the
diff and the tests — and only its VERDICT reaches the daemon, as one line in the review comment:

    🧩 phase-gap: #N — <what remains>

Three properties are the design, and each has a test here:

* **It catches the failure.** A green, reviewed, otherwise-mergeable PR carrying an open declaration
  is NOT merged; it dispatches `phasefix`, which files + links the follow-up. That is #548's failure
  mode — Phase 1 merged with `Closes #548`, Phase 2 never filed — caught at the last moment it can be.
* **It fails open.** No declaration, no hold. A gate that can hold a PR on its own opinion is a gate
  that wedges the queue, which is exactly why the v1 shape was not ported.
* **The two halves cannot drift.** The runbook tells the agent one literal and `github.py` detects
  another is how this breaks — silently, and in the direction that merges. The last block asserts
  the shipped prompts and the shipped regexes agree, on the shipped text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE = _ROOT / "scripts" / "agent-pipeline"
sys.path.insert(0, str(_PIPELINE / "v2"))

from lemd import db, github, observe  # noqa: E402
from lemd.github import ChecksState  # noqa: E402

pytestmark = pytest.mark.unit

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
GREEN = ChecksState(failed=0, pending=0, total=6)

SELFREVIEW = _PIPELINE / "runbook" / "selfreview.md"
PHASEFIX = _PIPELINE / "runbook" / "phasefix.md"

#: What a self-review that found untracked scope actually posts: the marker comment it always posts,
#: with the declaration on a line inside it. Written as one comment because that is the instruction
#: in `selfreview.md` — two comments to say one thing invites exactly half of it being posted.
DECLARED = (
    f"{github.CLAUDE_REVIEW_MARKER} — FIXED 1 finding\n"
    "- missing null guard: added it\n"
    f"{github.PHASE_GAP_MARKER}: #1396 — phase 2 (the v2-native guard) is not tracked\n"
)
CLEARED = f"{github.PHASE_GAP_MARKER}: cleared — Follow-up: #1600 filed and linked"


def pr(**kw) -> observe.Snapshot:
    """An otherwise-mergeable PR: green, reviewed, no threads, nothing else to do."""
    base = dict(
        kind="pr", number=1, labels=frozenset({"agent:working"}), state="OPEN",
        branch="feature/claude-issue-1396", head_sha="abc", checks=GREEN,
        review_fresh=True, merge_state="CLEAN",
    )
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap: observe.Snapshot) -> observe.Decision:
    return observe.decide(snap, **TTLS)


# ------------------------------------------------------------------ the decision


def test_a_declared_gap_holds_the_merge_and_files_the_followup():
    """THE acceptance test: a PR closing a phased issue with untracked scope is caught.

    Everything else about this PR says merge — CI green, review fresh, no unresolved threads, no
    lane label, `CLEAN`. The declaration is the only difference, and it must be the difference
    between `gate_satisfied` and the lane that files the follow-up.
    """
    decision = d(pr(phase_gap=True))
    assert decision.action == observe.ACT_DISPATCH
    assert decision.mode == "phasefix"
    assert decision.reason == "phase_scope_untracked"
    assert decision.next_state == db.STATE_CLAIMED


def test_the_same_pr_without_a_declaration_merges():
    """Fail-open, asserted directly: the hold comes from the reviewer, never from this function."""
    decision = d(pr())
    assert decision.action == observe.ACT_MERGE
    assert decision.reason == "gate_satisfied"


def test_the_gap_outranks_every_merge_row():
    """A gap declared on an already-armed or already-green PR still holds.

    The rows between the lane labels and `gate_satisfied` all WAIT (armed auto-merge, unreadable
    mergeability, CI). If the gap sat below any of them, a PR could sit in one of those waits until
    GitHub merged it with the scope still untracked.
    """
    for kw in ({"auto_merge": True}, {"merge_state": ""}, {"checks": None},
               {"review_fresh": False}, {"unresolved_threads": 2}):
        assert d(pr(phase_gap=True, **kw)).mode == "phasefix", kw


def test_the_owners_own_instruction_still_outranks_the_gap():
    """`agent:revise` carries what the owner asked for; our bookkeeping does not outrank it."""
    snap = pr(phase_gap=True, labels=frozenset({"agent:working", "agent:revise"}))
    assert d(snap).mode == "revise"


def test_a_human_hold_still_outranks_the_gap():
    """A held PR is the owner's. The gap waits for the un-park like every other lane."""
    snap = pr(phase_gap=True, labels=frozenset({"agent:working", "needs-human"}))
    assert d(snap).action == observe.ACT_NONE
    assert d(snap).next_state == db.STATE_PARKED


def test_a_queued_pr_with_a_gap_waits_and_says_which_lane_is_held():
    """The merge-queue gate (#1388) is above every lane, and silence is what an operator cannot read."""
    decision = d(pr(phase_gap=True, queue_state="QUEUED"))
    assert decision.reason == "in_merge_queue"
    assert decision.details["withheld"] == "phasefix"


def test_the_label_entrance_still_works():
    """v1 writes `agent:phasefix` while it runs as the failsafe; both entrances are one lane."""
    snap = pr(labels=frozenset({"agent:working", "agent:phasefix"}))
    assert d(snap).mode == "phasefix"
    assert d(snap).reason == d(pr(phase_gap=True)).reason


# ------------------------------------------------------------------ reading the declaration


def _payload(*, head="2026-08-10T10:00:00Z", comments=()):
    """A GraphQL response shaped like the one `review_state` reads."""
    return {"data": {"repository": {"pullRequest": {
        "commits": {"nodes": [{"commit": {"committedDate": head}}]},
        "reviews": {"nodes": []},
        "comments": {"nodes": list(comments)},
        "reviewThreads": {"nodes": []},
    }}}}


def _state(monkeypatch, *bodies) -> github.ReviewState:
    """Run the real reader over comments posted in the given order (oldest first)."""
    comments = [{"createdAt": f"2026-08-10T10:0{i}:00Z", "body": b} for i, b in enumerate(bodies, 1)]
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: _payload(comments=comments))
    return github.review_state("o/r", 1)


def test_the_declaration_is_read_out_of_the_review_comment(monkeypatch):
    """One comment, two facts: the review is evidence AND it carries the verdict."""
    state = _state(monkeypatch, DECLARED)
    assert state.phase_gap is True
    assert state.fresh is True, "the declaration must not cost the PR its review evidence"


def test_no_declaration_reads_as_no_gap(monkeypatch):
    """Every PR that predates this convention, and every honest close, lands here."""
    assert _state(monkeypatch, f"{github.CLAUDE_REVIEW_MARKER} — PASS").phase_gap is False


def test_phasefix_clears_it(monkeypatch):
    """The lane's release. Without this the daemon re-dispatches until the budget parks the PR."""
    assert _state(monkeypatch, DECLARED, CLEARED).phase_gap is False


def test_a_gap_declared_after_a_clear_reopens_it(monkeypatch):
    """LAST declaration wins — a second phase found on a later pass is a new hold, not a stale one."""
    assert _state(monkeypatch, DECLARED, CLEARED, DECLARED).phase_gap is True


def test_a_mangled_emoji_does_not_lose_the_declaration(monkeypatch):
    """PR #1273's failure, applied to this marker before it can happen twice.

    The decoration is non-BMP and does not survive every round-trip through a model; detection keys
    on the ASCII phrase, so four U+FFFD in front of it changes nothing.
    """
    assert _state(monkeypatch, "���� phase-gap: #1396 — phase 2").phase_gap


def test_prose_about_the_convention_is_not_a_declaration(monkeypatch):
    """This repo is PUBLIC and anyone can comment. The hold needs the shape, not the topic."""
    for body in ("we should think about the phase gap here",
                 "phase-gap handling is documented in the runbook",
                 "no phase gap on this one"):
        assert _state(monkeypatch, body).phase_gap is False, body


def test_a_snapshot_carries_the_declaration_into_the_decision(monkeypatch):
    """The wiring between the two halves above, which is where a field like this normally rots."""
    monkeypatch.setattr(github, "pr_facts", lambda *a, **k: {
        "state": "OPEN", "isDraft": False, "headRefName": "feature/claude-issue-1396",
        "headRefOid": "abc", "mergeStateStatus": "CLEAN", "autoMergeRequest": None,
        "labels": [{"name": "agent:working"}],
        "headRepositoryOwner": {"login": "o"},
    })
    monkeypatch.setattr(github, "checks_for", lambda *a, **k: GREEN)
    monkeypatch.setattr(github, "merge_queue_state", lambda *a, **k: "")
    monkeypatch.setattr(github, "gh_json", lambda *a, **k: _payload(comments=[
        {"createdAt": "2026-08-10T10:05:00Z", "body": DECLARED},
    ]))
    snap = observe.snapshot_pr("o/r", 1)
    assert snap.phase_gap is True
    assert d(snap).mode == "phasefix"


# ------------------------------------------------------------------ prompt/detector agreement


def test_selfreview_tells_the_agent_to_write_what_the_detector_reads():
    """The drift that would break this silently, in the direction that merges.

    The runbook is the ONLY place the literal is specified to the agent that writes it. If the two
    sides disagree, every declaration is invisible and the guard is decoration.
    """
    text = SELFREVIEW.read_text(encoding="utf-8")
    declaration = [ln for ln in text.splitlines() if github.PHASE_GAP_OPEN_RE.search(ln)]
    assert declaration, "selfreview.md never shows the agent a line the detector would match"
    assert not any(github.PHASE_GAP_CLEARED_RE.search(ln) for ln in declaration), (
        "selfreview.md's example matches the CLEARED pattern — the declaration would retire itself"
    )


def test_phasefix_tells_the_agent_how_to_clear_it():
    """The other half. A lane that cannot release its own hold re-dispatches until the budget parks."""
    text = PHASEFIX.read_text(encoding="utf-8")
    assert any(github.PHASE_GAP_CLEARED_RE.search(ln) for ln in text.splitlines()), (
        "phasefix.md never shows the agent a line that clears the declaration"
    )


def test_detection_never_depends_on_the_decoration():
    """Same rule as `CLAUDE_REVIEW_MARKER_TEXT`: the anchor must be BMP-only, ASCII-detectable."""
    assert github.PHASE_GAP_OPEN_RE.search("phase-gap: #1 — x"), "the bare ASCII form must match"
    for ch in github.PHASE_GAP_OPEN_RE.pattern:
        assert ord(ch) <= 0xFFFF, f"{ch!r} is non-BMP and must not gate detection"


def test_the_runbooks_agree_with_the_decorated_marker_too():
    """Agents copy the decorated form; it must contain the phrase detection keys on."""
    assert "phase-gap" in github.PHASE_GAP_MARKER
    assert github.PHASE_GAP_MARKER in SELFREVIEW.read_text(encoding="utf-8")
    assert github.PHASE_GAP_MARKER in PHASEFIX.read_text(encoding="utf-8")
