"""A generated carousel's CAPTION is authenticity-judged like any other post (issue #1512).

`_score_and_persist_authenticity` used to be called from `create_text_post` and `rescore_post` only,
so `posts.authenticity_score` stayed NULL for every generated deck and the authenticity gate inside
`evaluate_post_gates` skipped itself by its own `is not None` guard — a deck could only ever be
judged if a human pressed re-score.

These tests pin the caption (never the slides) as what the judge reads, that it is ONE judge call per
deck, that the score reaching the gate now demotes a carousel exactly as it demotes a text post, and
that a judge failure still costs nothing.
"""

from unittest.mock import MagicMock, patch

import pytest

import cqc_lem.app.run_content_plan as rcp
from cqc_lem.utilities.db import PostType
from cqc_lem.utilities.quality_gates import GATE_AUTHENTICITY, demoting_findings

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_DECK = {
    "cover": {"title": "Deploy time", "content": "We cut it from 41 minutes to 9."},
    "insights": [{"title": "Build cache", "content": "One shared cache took 14 minutes off."}],
}


def _create(post_id=7, caption="The deck's caption.", deck=None, prefs=None):
    """Drive `create_carousel_content` with every seam mocked; returns the scorer mock."""
    with patch(f"{_RCP}.get_engagement_preferences", return_value=prefs if prefs is not None else {}), \
         patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
         patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
         patch(f"{_RCP}.get_shape_performance", return_value=None), \
         patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
         patch(f"{_RCP}.record_story_bank_use"), \
         patch(f"{_RCP}.update_db_post_shape"), \
         patch(f"{_RCP}.update_db_post_status"), \
         patch(f"{_RCP}._report_carousel_fact_grounding"), \
         patch(f"{_RCP}._report_carousel_slide_slop"), \
         patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock(name="profile")), \
         patch(f"{_RCP}._score_and_persist_authenticity") as judge, \
         patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
               return_value=(caption, deck if deck is not None else _DECK)):
        rcp.create_carousel_content(1, "awareness", post_id)
    return judge


class TestTheDeckIsJudgedAtGeneration:
    def test_a_generated_deck_scores_its_caption(self):
        judge = _create()
        assert judge.call_count == 1
        assert judge.call_args[0][2] == "The deck's caption."

    def test_the_judge_reads_the_caption_not_the_slide_text(self):
        judge = _create(caption="Caption only.")
        scored = judge.call_args[0][2]
        assert "One shared cache took 14 minutes off." not in scored

    def test_it_is_one_judge_call_per_deck(self):
        assert _create().call_count == 1

    def test_the_users_voice_and_prefs_reach_the_judge(self):
        judge = _create(prefs={"focus_topics": ["devops"]})
        assert judge.call_args[0][4] == "brief"
        assert judge.call_args[0][5] == {"focus_topics": ["devops"]}

    def test_the_logs_name_the_carousel_task_not_the_text_post_one(self):
        judge = _create()
        assert judge.call_args[1]["task_name"] == "create_carousel_content"

    def test_a_deck_with_no_post_row_is_not_judged(self):
        assert _create(post_id=None).call_count == 0

    def test_a_deck_whose_caption_came_back_empty_is_not_judged(self):
        assert _create(caption="   ").call_count == 0


class TestScoringNeverCostsTheDeck:
    def test_a_judge_that_raises_still_returns_the_caption(self):
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
             patch(f"{_RCP}.record_story_bank_use"), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}._report_carousel_fact_grounding"), \
             patch(f"{_RCP}._report_carousel_slide_slop"), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}.authenticity_gate_enabled", return_value=True), \
             patch(f"{_RCP}.score_authenticity", side_effect=RuntimeError("judge down")), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("caption", _DECK)):
            assert rcp.create_carousel_content(1, "awareness", 7) == "caption"

    def test_a_disabled_gate_spends_no_judge_call(self):
        with patch(f"{_RCP}.authenticity_gate_enabled", return_value=False), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}.score_authenticity") as score:
            rcp._score_carousel_caption_authenticity(1, 7, "caption", "brief", {})
        score.assert_not_called()

    def test_the_score_is_persisted_on_the_post(self):
        with patch(f"{_RCP}.authenticity_gate_enabled", return_value=True), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}.score_authenticity",
                   return_value={"score": 44, "reasons": ["generic"], "flagged": True}), \
             patch(f"{_RCP}.update_db_post_authenticity_score") as store:
            rcp._score_carousel_caption_authenticity(1, 7, "caption", "brief", {})
        store.assert_called_once_with(7, 44)


class TestTheGateNowFiresOnADeck:
    """The scoring is only half of it — the point is that the EXISTING gate stops being skipped."""

    def test_a_low_scoring_carousel_is_held_the_way_a_text_post_is(self):
        with patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.authenticity_gate_enabled", return_value=True), \
             patch(f"{_RCP}.authenticity_score_min", return_value=70):
            findings = rcp.evaluate_post_gates(7, "caption", PostType.CAROUSEL.value,
                                               authenticity_score=41)
        held = demoting_findings(findings)
        assert any(f["gate"] == GATE_AUTHENTICITY for f in held)

    def test_an_unscored_carousel_is_still_never_held_by_that_gate(self):
        with patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.authenticity_gate_enabled", return_value=True), \
             patch(f"{_RCP}.authenticity_score_min", return_value=70):
            findings = rcp.evaluate_post_gates(7, "caption", PostType.CAROUSEL.value,
                                               authenticity_score=None)
        assert not any(f["gate"] == GATE_AUTHENTICITY for f in findings)
