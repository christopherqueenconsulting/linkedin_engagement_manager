"""Deterministic review risk flags (#2625 Phase 2)."""

import pytest

from cqc_lem.utilities import review_flags as rf

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("text,flag", [
    ("We grew revenue 40% last year", rf.FLAG_NUMBER),
    ("Costs fell by $5 per seat", rf.FLAG_NUMBER),
    ("I built this from scratch", rf.FLAG_FIRST_PERSON_CLAIM),
    ("my clients love it", rf.FLAG_FIRST_PERSON_CLAIM),
    ("Loved your talk with Jane Smith today", rf.FLAG_NAMED_ENTITY),
    ("details at https://example.com/x", rf.FLAG_LINK),
    ("Let's chat next week", rf.FLAG_MEETING_ASK),
    ("Happy to book a quick call", rf.FLAG_MEETING_ASK),
])
def test_each_rule_flags(text, flag):
    assert flag in rf.text_flags(text)


def test_clean_comment_has_no_flags():
    assert rf.text_flags("Great point, thanks for sharing this perspective.") == []


def test_confidence_and_unmeasured():
    assert rf.confidence_for([], "ok text") == 100
    assert rf.confidence_for([rf.FLAG_MEETING_ASK], "x") == 75
    assert rf.confidence_for(list(rf.FLAG_WEIGHTS) * 2, "x") == 0
    assert rf.confidence_for([], "  ") is None


def test_annotate_hold_duplicate_and_stale_slot():
    same = "thanks for connecting, looking forward to learning from your posts"
    items = [
        {"lane": "scheduled_dms", "draft_text": same, "hold_reason": None, "age_hours": 60.0},
        {"lane": "connection_requests", "draft_text": same, "hold_reason": None, "age_hours": 60.0},
        {"lane": "posts", "draft_text": "A different draft entirely", "hold_reason": "AI slop",
         "age_hours": None},
        {"lane": "catchup_touches", "draft_text": "Congrats on the new role", "hold_reason": None,
         "age_hours": 1.0},
    ]
    rf.annotate(items)
    assert rf.FLAG_NEAR_DUPLICATE in items[0]["risk_flags"]
    assert rf.FLAG_STALE_SLOT in items[0]["risk_flags"]
    assert rf.FLAG_STALE_SLOT not in items[1]["risk_flags"]
    assert items[2]["risk_flags"] == [rf.FLAG_GATE_NEAR_MISS]
    assert items[3]["risk_flags"] == [] and items[3]["confidence"] == 100
