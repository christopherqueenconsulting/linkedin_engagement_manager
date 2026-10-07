"""Showcase round 6: one regeneration for a deck with template slides.

`ai_helper._repair_carousel_substance` keeps the retry only when it is strictly better.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import ai_helper
from cqc_lem.utilities.carousel_creator import EducationalContentCarousel

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
POST = "I found a real bug by running the take-home; the runbook covered key rotation."
STORY = ["A candidate's runbook covered key rotation and running the code surfaced a bug."]
THIN = {"cover": {"title": "A case", "content": "What happened"},
        "contents": [{"title": "The Initial Challenge", "content": "Both were proficient."},
                     {"title": "Decisive Moves Made", "content": "The approach made a difference."}],
        "call_to_action": {"title": "Follow", "content": "More soon."}}
SOLID = {"cover": {"title": "A case", "content": "What happened"},
         "contents": [{"title": "The runbook", "content": "Key rotation runbook, written first."},
                      {"title": "The bug", "content": "Running the take-home surfaced a bug."}],
         "call_to_action": {"title": "Follow", "content": "More soon."}}
PASSING_REFERENCE = {"required": False, "passes": True}


def _repair(deck, draft, reference=PASSING_REFERENCE):
    return ai_helper._repair_carousel_substance(draft, POST, deck, STORY,
                                                EducationalContentCarousel, reference, False, 1)


class TestSubstanceRepair:
    def test_a_solid_deck_costs_no_call(self):
        draft = MagicMock()
        assert _repair(SOLID, draft) == (POST, SOLID)
        draft.assert_not_called()

    def test_a_thin_deck_is_regenerated_with_its_slides_named(self):
        draft = MagicMock(return_value=("New caption.", SOLID, True))
        assert _repair(THIN, draft) == ("New caption.", SOLID)
        directive = draft.call_args.args[0]
        assert "The Initial Challenge" in directive and "Decisive Moves Made" in directive

    def test_a_retry_no_better_keeps_the_deck_in_hand(self):
        draft = MagicMock(return_value=("New caption.", THIN, True))
        assert _repair(THIN, draft) == (POST, THIN)

    def test_a_retry_missing_a_slide_keeps_the_deck_in_hand(self):
        broken = {k: v for k, v in SOLID.items() if k != "cover"}
        draft = MagicMock(return_value=("New caption.", broken, True))
        assert _repair(THIN, draft) == (POST, THIN)

    def test_a_retry_that_raises_keeps_the_deck_in_hand(self):
        draft = MagicMock(side_effect=RuntimeError("llm down"))
        with patch(f"{_AI}.log_warning") as warn:
            assert _repair(THIN, draft) == (POST, THIN)
        warn.assert_called_once()

    def test_a_retry_that_loses_the_reference_value_is_refused(self):
        draft = MagicMock(return_value=("New caption.", SOLID, True))
        held = {"required": True, "passes": True}
        with patch(f"{_AI}._framework.deck_reference_report", return_value={"passes": False}):
            assert _repair(THIN, draft, held) == (POST, THIN)

    def test_no_deck_is_a_no_op(self):
        assert ai_helper._repair_carousel_substance(MagicMock(), POST, {}, STORY, None, {}, False,
                                                    1) == (POST, {})
