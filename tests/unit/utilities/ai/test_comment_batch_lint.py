"""Batch structural lint on feed comments (#2665 P3): question rate, opener family, length spread."""

from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import content_framework as cf

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _varied(n=20):
    """A batch with no question endings, distinct openers and a real length spread."""
    openers = ["Queues", "Handoffs", "Capacity", "Routing", "Triage", "Tiers", "Latency", "Backlogs",
               "Staffing", "Alerts"]
    out = []
    for i in range(n):
        body = " ".join(["word"] * (6 + (i % 5) * 9))
        out.append(f"{openers[i % len(openers)]}{i} {body}.")
    return out


def _questions(n=20, rate=0.9):
    asks = int(n * rate)
    return [f"Topic{i} sets the wait here" + " and nothing else does" * (1 + i * 2 % 7)
            + ("?" if i < asks else ".")
            for i in range(n)]


def test_a_batch_with_90_percent_question_endings_fails():
    report = cf.comment_batch_report("Pricing decides this far more than anyone admits?", _questions())
    assert any("end on a question" in h for h in report["hints"])
    assert report["question_rate"] > cf.COMMENT_QUESTION_RATE_MAX


def test_a_varied_batch_passes():
    report = cf.comment_batch_report("Zebras migrate " + "far " * 20 + "away.", _varied())
    assert report["hints"] == []


def test_a_statement_is_not_hinted_even_in_a_question_heavy_batch():
    report = cf.comment_batch_report("Pricing decides this far more than anyone admits.", _questions())
    assert not any("question" in h for h in report["hints"])


def test_repeated_opener_family_is_hinted_and_you_openers_share_one_family():
    batch = ["Your point about queues is fair."] + _varied()
    report = cf.comment_batch_report("You are right about handoffs.", batch)
    assert any("opens 'you..." in h for h in report["hints"])


def test_uniform_lengths_are_hinted_only_for_a_draft_at_that_length():
    batch = [f"Distinct{i} " + "word " * 13 + "end." for i in range(10)]
    same = cf.comment_batch_report("Fresh angle " + "word " * 12 + "end.", batch)
    assert any("same length" in h for h in same["hints"])
    longer = cf.comment_batch_report("Fresh angle " + "word " * 40 + "end.", batch)
    assert not any("same length" in h for h in longer["hints"])


def test_thin_history_is_no_batch():
    assert cf.comment_batch_report("Anything at all?", ["One?", "Two?"]) == {
        "hints": [], "question_rate": None}


def _gate(drafts, history, monkeypatch, surface="comment", attempts="3"):
    from cqc_lem.utilities.ai import ai_helper
    monkeypatch.setenv("COMMENT_CLAIMS_GATE", "off")
    monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", attempts)
    seen = []
    it = iter(drafts)

    def _draft(fix):
        seen.append(fix)
        return next(it)

    post = "Routing adds a handoff at every hop, and each handoff waits in line behind the others."
    with patch(f"{_AI}.track_comment_gate"), \
            patch("cqc_lem.utilities.ai.content_framework.embed_comments", return_value=None):
        out = ai_helper._gated_comment(_draft, post, recent_comments=history, user_id=1,
                                       surface=surface)
    return out, seen


_ASK = ("Each handoff waits in line behind the others, and that routing delay is the real cost. "
        "Capacity decides the wait far more than the tier count does, which the post skips. "
        "Which of the two did you measure first?")
_STATEMENT = ("Each handoff waits in line behind the others, and that routing delay is the real "
              "cost. Which of the two did you measure first, capacity or the tier count? "
              "Measure capacity before you add another tier.")


def test_gate_regenerates_with_the_hint_then_ships_the_varied_draft(monkeypatch):
    out, seen = _gate([_ASK, _STATEMENT], _questions(), monkeypatch)
    assert out == _STATEMENT
    assert "end on a question" in seen[1]


def test_gate_ships_the_last_draft_rather_than_skipping(monkeypatch):
    out, seen = _gate([_ASK, _ASK], _questions(), monkeypatch, attempts="2")
    assert out == _ASK and len(seen) == 2


def test_lint_regeneration_that_fails_a_gate_falls_back_to_the_shipped_draft(monkeypatch):
    out, seen = _gate([_ASK, "Great post!", "Great post!"], _questions(), monkeypatch)
    assert out == _ASK and len(seen) == 3


def test_lint_regeneration_with_no_draft_falls_back_to_the_shipped_draft(monkeypatch):
    out, seen = _gate([_ASK, None], _questions(), monkeypatch)
    assert out == _ASK and len(seen) == 2


def test_other_surfaces_are_not_linted(monkeypatch):
    out, seen = _gate([_ASK], _questions(), monkeypatch, surface="own_post_comment")
    assert out == _ASK and len(seen) == 1
