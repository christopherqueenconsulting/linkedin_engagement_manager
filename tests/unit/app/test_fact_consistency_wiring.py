"""Showcase round 6 wiring in the content plan.

The date/timeline gate, the deterministic cleanup and the story-only industry check, each
proven at the seam where `run_content_plan` applies it.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.app import run_content_plan as rcp
from cqc_lem.domain.models import PostDraftContext
from cqc_lem.utilities.ai import content_framework as cf
from cqc_lem.utilities.quality_gates import (
    GATE_FACT_CONSISTENCY,
    GATE_SIMILARITY,
    demoting_findings,
    fact_consistency_finding,
)

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
WRONG_WEEKDAY = "Wednesday, June 22, 2026 the board turned red when our backlog hit 529."
DEADLINE = "On February 14 2026 I offered to rebuild a site for free, aiming to finish in January."


def _ctx(**overrides) -> PostDraftContext:
    fields = dict(user_id=1, stage="awareness", post_type="thought_leadership",
                  user_profile=MagicMock(), prefs={}, profile_synthesis="voice",
                  blueprint={"format": "personal_lesson", "cta_type": cf.CTA_TYPE_QUESTION},
                  post_id=40, history_directive="", story_directive="")
    fields.update(overrides)
    return PostDraftContext(**fields)


class TestTheFinding:
    def test_it_holds_and_names_each_issue(self):
        finding = fact_consistency_finding(["a", " ", "b"])
        assert finding["gate"] == GATE_FACT_CONSISTENCY and finding["demoted"]
        assert finding["details"] == ["a", "b"] and finding["score"] == 2.0
        assert finding["label"] == "Impossible date or timeline"


class TestDeterministicCleanup:
    def test_a_wrong_weekday_and_a_sign_off_are_fixed_without_an_llm(self):
        text = WRONG_WEEKDAY + "\n\nWhat would you automate first?\n\nSenior Applied AI Engineer"
        with patch(f"{_RCP}.log_info") as info:
            out = rcp._deterministic_fact_cleanup(text, user_id=1, post_id=2)
        assert out == ("June 22, 2026 the board turned red when our backlog hit 529.\n\n"
                       "What would you automate first?")
        assert "dropped the wrong weekday" in info.call_args.args[0]
        assert "sign-off" in info.call_args.args[0]

    def test_a_clean_draft_is_untouched_and_quiet(self):
        with patch(f"{_RCP}.log_info") as info:
            assert rcp._deterministic_fact_cleanup("Clean.", user_id=1) == "Clean."
            assert rcp._deterministic_fact_cleanup(None) is None
        info.assert_not_called()

    def test_every_slide_string_is_cleaned(self):
        deck = {"cover": {"title": WRONG_WEEKDAY}, "contents": [{"content": WRONG_WEEKDAY}, 3]}
        out = rcp._fact_cleanup_deck(deck)
        assert out["cover"]["title"].startswith("June 22, 2026")
        assert out["contents"][0]["content"].startswith("June 22, 2026")
        assert out["contents"][1] == 3


class TestTheRecord:
    def test_a_verdict_is_written_beside_the_other_findings(self):
        other = {"gate": GATE_SIMILARITY, "details": []}
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[other]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as write:
            rcp._record_consistency_finding(9, ["impossible"], user_id=1)
        written = write.call_args.args[1]
        assert written[0] == other and written[1]["gate"] == GATE_FACT_CONSISTENCY

    def test_a_clean_draft_clears_a_stale_verdict(self):
        stale = fact_consistency_finding(["old"])
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[stale]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as write:
            rcp._record_consistency_finding(9, [])
        write.assert_called_once_with(9, [])

    def test_a_clean_draft_with_nothing_recorded_never_writes(self):
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as write:
            rcp._record_consistency_finding(9, [])
            rcp._record_consistency_finding(None, ["x"])
        write.assert_not_called()

    def test_a_failed_write_only_logs(self):
        with patch(f"{_RCP}.get_post_gate_reason", side_effect=RuntimeError("db")), \
             patch(f"{_RCP}.log_warning") as warn:
            rcp._record_consistency_finding(9, ["x"])
        warn.assert_called_once()


class TestTheCarry:
    def test_the_recorded_timeline_hold_survives_a_gate_pass(self):
        recorded = fact_consistency_finding(["timeline"])
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[recorded]):
            out = rcp._carry_consistency_hold(9, [])
        assert out == [recorded]

    def test_it_merges_into_a_live_finding(self):
        recorded = fact_consistency_finding(["timeline", "weekday"])
        live = fact_consistency_finding(["weekday"])
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[recorded]):
            out = rcp._carry_consistency_hold(9, [live])
        assert len(out) == 1 and out[0]["details"] == ["weekday", "timeline"]

    def test_nothing_recorded_changes_nothing(self):
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[]):
            assert rcp._carry_consistency_hold(9, ["f"]) == ["f"]

    def test_an_unreadable_record_costs_only_the_hold(self):
        with patch(f"{_RCP}.get_post_gate_reason", side_effect=RuntimeError("db")), \
             patch(f"{_RCP}.log_warning") as warn:
            assert rcp._recorded_consistency_finding(9) == []
        warn.assert_called_once()


class TestTheGatePass:
    def _gates(self, content):
        with patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}._is_affiliate_promo", return_value=False):
            return rcp.evaluate_post_gates(7, content, "text")

    def test_a_deadline_before_the_offer_holds_the_post(self):
        held = demoting_findings(self._gates(DEADLINE))
        assert [f["gate"] for f in held] == [GATE_FACT_CONSISTENCY]

    def test_a_consistent_post_is_not_held_by_it(self):
        findings = self._gates("On Monday, June 22, 2026 the board turned red.")
        assert not any(f["gate"] == GATE_FACT_CONSISTENCY for f in findings)

    def test_the_generation_pass_carries_the_recorded_verdict(self):
        recorded = fact_consistency_finding(["a timeline the story cannot back"])
        with patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch(f"{_RCP}._post_archetype_or_none", return_value=None), \
             patch(f"{_RCP}._engagement_prefs_for_gates", return_value=({}, True)), \
             patch(f"{_RCP}._cta_keyword_for", return_value=None), \
             patch(f"{_RCP}._post_content_mix", return_value=None), \
             patch(f"{_RCP}._fact_anchors_for", return_value=[]), \
             patch(f"{_RCP}._post_material_sources", return_value=[]), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}._is_affiliate_promo", return_value=False), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=[recorded]):
            findings = rcp._gate_findings_for_post(1, 7, "Clean text.", "text")
        assert recorded in findings

    def test_a_failing_gate_pass_still_carries_it(self):
        recorded = fact_consistency_finding(["timeline"])
        with patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch(f"{_RCP}._post_archetype_or_none", return_value=None), \
             patch(f"{_RCP}._engagement_prefs_for_gates", return_value=({}, True)), \
             patch(f"{_RCP}.evaluate_post_gates", side_effect=RuntimeError("boom")), \
             patch(f"{_RCP}.log_warning"), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=[recorded]):
            findings = rcp._gate_findings_for_post(1, 7, "Clean text.", "text")
        assert recorded in findings


class TestTheReviewGate:
    def _review(self, content, repaired, story=None, consistency_story=None):
        with patch(f"{_RCP}._fact_anchors", return_value=["Between June 1 and July 1 I shipped."]), \
             patch(f"{_RCP}._post_material_sources", return_value=[]), \
             patch(f"{_RCP}._repair_draft", return_value=repaired) as repair, \
             patch(f"{_RCP}._persist_gate_findings") as persist, \
             patch(f"{_RCP}._record_post_similarity_finding"), \
             patch(f"{_RCP}._record_consistency_finding") as record, \
             patch(f"{_RCP}._check_post_alignment"), \
             patch(f"{_RCP}.post_similarity_report",
                   return_value={"score": 0.0, "threshold": 1.0, "too_similar": False,
                                 "match": None, "measure": "embedding"}), \
             patch(f"{_RCP}.has_first_person_proof", return_value=True), \
             patch(f"{_RCP}._grades_fact_grounding", return_value=False):
            out = rcp._review_generated_post(_ctx(), content, [], story=story)
        return out, repair, persist, record

    def test_a_timeline_the_story_cannot_back_goes_to_the_editor(self):
        story = {"id": 3, "title": "Fintech", "body": "On Oct 1 I took a fintech engagement.",
                 "happened_at": date.today() - timedelta(days=6)}
        draft = "Within the first month we mapped every touch-point and our costs fell."
        fixed = "In the first week we mapped every touch-point."
        out, repair, persist, _ = self._review(draft, fixed, story=story)
        assert out == fixed and repair.call_count == 1
        brief = repair.call_args.args[2]
        assert any(f["gate"] == GATE_FACT_CONSISTENCY for f in brief)
        # The repaired draft is re-graded: clean, so its recorded findings carry no hold.
        assert not any(f["gate"] == GATE_FACT_CONSISTENCY
                       for f in persist.call_args_list[-1].args[2])

    def test_a_repair_that_keeps_the_impossible_claim_is_recorded_as_a_hold(self):
        out, _, persist, _ = self._review(DEADLINE, DEADLINE)
        assert out == DEADLINE
        assert any(f["gate"] == GATE_FACT_CONSISTENCY for f in persist.call_args_list[-1].args[2])

    def test_the_repair_gets_the_deterministic_cleanup_too(self):
        _out, _, _, _ = self._review(DEADLINE, WRONG_WEEKDAY + "\n\nSenior AI Engineer")
        assert _out.startswith("June 22, 2026") and "Engineer" not in _out

    def test_a_clean_draft_clears_any_stale_verdict(self):
        out, repair, _, record = self._review("A clean post about shipping.", "unused")
        repair.assert_not_called()
        record.assert_called_once_with(40, [], user_id=1)


class TestIndustryClaims:
    def test_the_profile_industry_no_longer_clears_a_claim(self):
        draft = "One e-commerce client taught me this."
        assert rcp._industry_claims(draft, ["I sent 51 cold emails."], ["e-commerce"]) == [
            "e-commerce"]

    def test_profile_facts(self):
        profile = MagicMock()
        profile.model_dump_json.return_value = '{"industry": "E-commerce"}'
        assert rcp._profile_facts(1, {"voice": "plain"}, profile) == [
            '{"voice": "plain"}', '{"industry": "E-commerce"}']
        assert rcp._profile_facts(1, None, None) == []


class _Stop(Exception):
    """Raised by the patched record so the test ends before the deck renders."""


class TestTheDeckRecord:
    def test_the_carousel_records_its_caption_and_slides_against_the_story(self):
        deck = {"cover": {"title": "Cover", "content": "x"},
                "contents": [{"title": "One", "content": "Do one."}],
                "call_to_action": {"title": "Follow", "content": "More soon."}}
        story = {"id": 1, "title": "t", "body": "b", "happened_at": date.today()}
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}._recent_post_records", return_value=[]), \
             patch(f"{_RCP}._resolve_post_audience", return_value=({}, None)), \
             patch(f"{_RCP}._select_story_for_post", return_value=story), \
             patch(f"{_RCP}._select_carousel_blueprint", return_value=None), \
             patch(f"{_RCP}._steer_post_blueprint", return_value=None), \
             patch(f"{_RCP}._fact_anchors", return_value=[]), \
             patch(f"{_RCP}._report_carousel_fact_grounding"), \
             patch(f"{_RCP}._report_carousel_slide_slop"), \
             patch(f"{_RCP}._report_carousel_deck_consistency"), \
             patch(f"{_RCP}._score_carousel_caption_authenticity"), \
             patch(f"{_RCP}.record_story_bank_use"), \
             patch(f"{_RCP}._record_consistency_finding", side_effect=_Stop) as record, \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("Within the first year we grew.", deck)), \
             pytest.raises(_Stop):
            # Stopped at the record: rendering the deck is out of scope here.
            rcp.create_carousel_content(1, "awareness", 5)
        post_id, issues = record.call_args.args
        assert post_id == 5 and len(issues) == 1 and "first year" in issues[0]
