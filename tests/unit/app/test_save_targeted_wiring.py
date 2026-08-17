"""Unit tests for how the save-optimized archetypes (issue #619 / G4) are wired into the content
pipeline: the no-fabrication gate holds a draft that invented specifics, the review gate spends its
one retry on it, and carousels draw their shape from the SAME post menu so a build receipt can land
as a document post.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_RECEIPT_WITH_PLACEHOLDERS = (
    "I shipped a [[COUNT: how many]] agent pipeline in [[DURATION: how long]].\n\n"
    "What it does: triages inbound support mail.\n\n"
    "What broke: [[FAILURE: what went wrong]]."
)
_RECEIPT_WITH_INVENTED_NUMBERS = (
    "I shipped a 20-agent pipeline in 3 weeks.\n\n"
    "What it does: triages inbound support mail.\n\n"
    "It cut first-response time by 62%."
)


class TestFactGroundingGate:
    def _gates(self, content, archetype, **kwargs):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}._post_missing_required_asset", return_value=False):
            return rcp.evaluate_post_gates(7, content, "text", archetype=archetype, **kwargs)

    def test_invented_specifics_hold_a_build_receipt(self):
        findings = self._gates(_RECEIPT_WITH_INVENTED_NUMBERS, "build_receipt")
        assert [f["gate"] for f in findings] == ["fact_grounding"]
        assert findings[0]["demoted"] is True

    def test_placeholders_hold_it_too_so_the_author_fills_them_in(self):
        findings = self._gates(_RECEIPT_WITH_PLACEHOLDERS, "build_receipt")
        assert [f["gate"] for f in findings] == ["fact_grounding"]
        assert any("COUNT: how many" in d for d in findings[0]["details"])

    def test_verified_facts_clear_the_hold(self):
        findings = self._gates(_RECEIPT_WITH_INVENTED_NUMBERS, "build_receipt",
                               fact_anchors=["Shipped a 20-agent pipeline in 3 weeks",
                                             "First-response time fell 62%"])
        assert findings == []

    def test_a_receipt_with_no_specifics_at_all_is_not_held(self):
        assert self._gates("A receipt with nothing quantified in it yet.", "build_receipt") == []

    def test_the_compendium_archetype_is_guarded_the_same_way(self):
        findings = self._gates("I keep 14 tools in this stack.", "resource_compendium")
        assert [f["gate"] for f in findings] == ["fact_grounding"]

    def test_other_archetypes_are_not_guarded(self):
        # An ordinary post's numbers come from research/profile grounding, not a fact anchor —
        # guarding every archetype would hold nearly every post.
        assert self._gates(_RECEIPT_WITH_INVENTED_NUMBERS, "personal_lesson") == []

    def test_an_unknown_or_missing_archetype_is_not_guarded(self):
        assert self._gates(_RECEIPT_WITH_INVENTED_NUMBERS, None) == []
        assert self._gates(_RECEIPT_WITH_INVENTED_NUMBERS, "not_a_format") == []

    def test_the_archetype_is_read_back_off_the_post_for_the_gate_pass(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_post_authenticity_score", return_value=90), \
             patch(f"{_RCP}._engagement_prefs_or_empty", return_value={}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch("cqc_lem.utilities.db.get_post_archetype", return_value="build_receipt"), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[]):
            findings = rcp._gate_findings_for_post(1, 7, _RECEIPT_WITH_INVENTED_NUMBERS, "text")
        assert [f["gate"] for f in findings] == ["fact_grounding"]

    def test_an_author_edit_clears_the_hold_once_the_placeholders_are_filled_in(self):
        # The guard exists to stop the MODEL inventing specifics. On a re-score of human-edited
        # text the author's own numbers ARE the verification — otherwise filling in a placeholder
        # would just swap one hold for another and the post could never publish.
        filled = _RECEIPT_WITH_PLACEHOLDERS.replace("[[COUNT: how many]]", "6") \
            .replace("[[DURATION: how long]]", "9 days") \
            .replace("[[FAILURE: what went wrong]]", "a silent retry loop")
        assert self._gates(filled, "build_receipt") != []
        assert self._gates(filled, "build_receipt", author_edited=True) == []

    def test_an_author_edit_that_left_a_placeholder_behind_is_still_held(self):
        findings = self._gates(_RECEIPT_WITH_PLACEHOLDERS, "build_receipt", author_edited=True)
        assert [f["gate"] for f in findings] == ["fact_grounding"]

    def test_the_rescore_path_treats_the_content_as_author_edited(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_post_content", return_value=_RECEIPT_WITH_INVENTED_NUMBERS), \
             patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_type", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_video_url", return_value=None), \
             patch("cqc_lem.utilities.db.get_post_status", return_value="pending"), \
             patch("cqc_lem.utilities.db.get_post_archetype", return_value="build_receipt"), \
             patch(f"{_RCP}._engagement_prefs_or_empty", return_value={}), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}._score_and_persist_authenticity"), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=90), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}._persist_gate_findings"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
             patch(f"{_RCP}.update_db_post_status") as status:
            result = rcp.rescore_post(7)
        assert result["passed"] is True
        status.assert_called_once()

    def test_an_unreadable_archetype_only_silences_its_own_gate(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch("cqc_lem.utilities.db.get_post_archetype", side_effect=RuntimeError("db down")):
            assert rcp._post_archetype_or_none(7) is None


class TestReviewGateSpendsItsRepairOnFabrication:
    def _review(self, first, second, blueprint, entries=()):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.domain.models import PostDraftContext
        ctx = PostDraftContext(user_id=1, stage="awareness", post_type="thought_leadership",
                               blueprint=blueprint, post_id=7, lead_magnet_cta="")
        with patch(f"{_RCP}.has_first_person_proof", return_value=True), \
             patch(f"{_RCP}._check_post_alignment"), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=list(entries)), \
             patch(f"{_RCP}.humanize_text", side_effect=lambda text, **_: text), \
             patch(f"{_RCP}.update_db_post_gate_reason"), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
             patch(f"{_RCP}.mark_post_gate_demoted"), \
             patch(f"{_RCP}.get_ai_linked_post_refinement", return_value=second) as repair:
            out = rcp._review_generated_post(ctx, first, [])
        return out, repair

    def test_a_fabricating_receipt_is_repaired_once_with_the_offending_numbers_named(self):
        out, repair = self._review(_RECEIPT_WITH_INVENTED_NUMBERS, _RECEIPT_WITH_PLACEHOLDERS,
                                   {"format": "build_receipt"})
        assert out == _RECEIPT_WITH_PLACEHOLDERS
        findings = repair.call_args.kwargs["repair_findings"]
        assert [f["gate"] for f in findings] == ["fact_grounding"]
        details = " ".join(findings[0]["details"])
        assert "20" in details and "62%" in details

    def test_the_same_draft_under_another_archetype_is_left_alone(self):
        out, regen = self._review(_RECEIPT_WITH_INVENTED_NUMBERS, "second draft",
                                  {"format": "personal_lesson"})
        assert out == _RECEIPT_WITH_INVENTED_NUMBERS
        regen.assert_not_called()

    def test_a_placeholder_draft_needs_no_retry(self):
        out, regen = self._review(_RECEIPT_WITH_PLACEHOLDERS, "second draft",
                                  {"format": "build_receipt"})
        assert out == _RECEIPT_WITH_PLACEHOLDERS
        regen.assert_not_called()

    def test_a_still_fabricating_repair_is_kept_and_left_to_the_gate(self):
        out, _ = self._review(_RECEIPT_WITH_INVENTED_NUMBERS, "We ran 90 jobs in 2 hours.",
                              {"format": "build_receipt"})
        assert out == "We ran 90 jobs in 2 hours."

    def test_numbers_the_users_own_story_bank_backs_are_not_treated_as_invented(self):
        # The guard is against the MODEL inventing figures. Once the bank (#620) holds the real
        # ones, calling them fabricated would hold every honest receipt for review.
        out, regen = self._review(
            _RECEIPT_WITH_INVENTED_NUMBERS, "second draft", {"format": "build_receipt"},
            entries=[{"title": "Support triage pipeline",
                      "body": "Shipped 20 agents in 3 weeks; first-response time fell 62%."}])
        assert out == _RECEIPT_WITH_INVENTED_NUMBERS
        regen.assert_not_called()


class TestFactAnchorsComeFromTheStoryBank:
    _ENTRY = {"id": 4, "kind": "artifact", "title": "Scraper rebuild",
              "body": "Ran 47 scrapes in one night.", "happened_at": None}

    def test_every_active_entry_counts_as_verified(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_story_bank_entries", return_value=[self._ENTRY]) as fetch:
            anchors = rcp._fact_anchors(1)
        assert any("47 scrapes" in a for a in anchors)
        assert fetch.call_args.kwargs.get("active_only") is True

    def test_an_empty_bank_leaves_the_guard_in_its_strictest_mode(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_story_bank_entries", return_value=[]):
            assert rcp._fact_anchors(1) == []

    def test_an_unreadable_bank_costs_the_credit_not_the_post(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_story_bank_entries", side_effect=RuntimeError("db down")):
            assert rcp._fact_anchors(1) == []

    def test_an_ordinary_archetype_never_pays_for_the_bank_read(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_story_bank_entries", return_value=[self._ENTRY]) as fetch:
            assert rcp._fact_anchors_for(1, "personal_lesson") == []
            assert rcp._fact_anchors_for(1, None) == []
            fetch.assert_not_called()
            assert rcp._fact_anchors_for(1, "build_receipt")
            fetch.assert_called_once()

    def test_the_writer_may_only_use_the_entry_this_post_was_anchored_to(self):
        # A number from some OTHER bank entry was never in this prompt, so the writer stating one
        # would still be inventing — the writer's allow-list is narrower than the checkers'.
        from unittest.mock import MagicMock

        from cqc_lem.app import run_content_plan as rcp
        other = {"id": 9, "kind": "number", "title": "Unrelated",
                 "body": "Booked 88 calls last quarter.", "happened_at": None}
        captured = {}

        def gen(user_profile, stage, blueprint=None, **kwargs):
            captured["anchors"] = blueprint.get("fact_anchors")
            return "generated post"

        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_lead_magnet_settings",
                   return_value={"enabled": False, "keyword": None, "message": None}), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.record_story_bank_use"), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[self._ENTRY, other]), \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=gen):
            rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                 user_profile=MagicMock(), refine_final_post=False, post_id=3)
        anchors = " ".join(captured["anchors"])
        assert "47 scrapes" in anchors
        assert "88 calls" not in anchors

    def test_no_anchor_entry_means_no_verified_facts_for_the_writer(self):
        from unittest.mock import MagicMock

        from cqc_lem.app import run_content_plan as rcp
        captured = {}

        def gen(user_profile, stage, blueprint=None, **kwargs):
            captured["anchors"] = blueprint.get("fact_anchors")
            return "generated post"

        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_lead_magnet_settings",
                   return_value={"enabled": False, "keyword": None, "message": None}), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=gen):
            rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                 user_profile=MagicMock(), refine_final_post=False, post_id=3)
        assert captured["anchors"] == []


class TestCarouselDrawsFromThePostMenu:
    def _create(self, post_id=None):
        from cqc_lem.app.run_content_plan import create_carousel_content
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.update_db_post_shape") as shape, \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("caption", {"bogus": True})) as gen:
            create_carousel_content(1, "awareness", post_id)
        return gen, shape

    def test_the_carousel_gets_an_assigned_post_archetype(self):
        from cqc_lem.utilities.ai.content_framework import POST_FORMATS
        gen, _ = self._create()
        assert gen.call_args[1]["blueprint"]["format"] in POST_FORMATS

    def test_the_carousel_shape_is_persisted_into_the_same_rotation_history(self):
        gen, shape = self._create(post_id=9)
        blueprint = gen.call_args[1]["blueprint"]
        shape.assert_called_once()
        assert shape.call_args[0] == (9, blueprint["format"], blueprint["hook_style"])

    def test_no_post_id_means_nothing_to_persist(self):
        _, shape = self._create()
        shape.assert_not_called()

    def test_a_failed_archetype_selection_never_blocks_the_carousel(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}._select_post_blueprint", side_effect=RuntimeError("history down")):
            assert rcp._select_carousel_blueprint(1) is None

    def test_carousels_prefer_the_save_targeted_archetypes_once_facts_exist(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}._fact_anchors", return_value=["Rendered 40 slides in one pass"]), \
             patch(f"{_RCP}.select_blueprint", return_value={}) as select:
            rcp._select_carousel_blueprint(1)
        assert select.call_args[1]["prefer_save_targeted"] is True

    def test_without_verified_facts_carousels_do_not_prefer_them(self):
        # A build-receipt carousel with no facts would bake placeholder text into the rendered
        # slide IMAGES, which no re-score can edit — so the bias waits for the story bank.
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
             patch(f"{_RCP}.select_blueprint", return_value={}) as select:
            rcp._select_carousel_blueprint(1)
        assert select.call_args[1]["prefer_save_targeted"] is False

    def test_without_verified_facts_a_carousel_can_never_draw_a_fact_anchored_archetype(self):
        # Un-preferring them is not enough: plain rotation would still hand ~1-in-5 carousels a
        # build receipt, and its placeholders would be rendered into the slide images for good.
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.ai.content_framework import fact_anchored_formats
        with patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[]):
            picks = {rcp._select_carousel_blueprint(1)["format"] for _ in range(200)}
        assert picks
        assert not picks & set(fact_anchored_formats("post"))

    def test_with_verified_facts_the_fact_anchored_archetypes_are_back_on_the_menu(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}._fact_anchors", return_value=["Rendered 40 slides in one pass"]), \
             patch(f"{_RCP}.select_blueprint", return_value={}) as select:
            rcp._select_carousel_blueprint(1)
        assert select.call_args[1]["exclude_formats"] is None

    def test_the_carousel_writer_gets_the_one_selected_story_only(self):
        # Issue #728: the writer's allow-list is the ONE anchored entry, never the whole bank —
        # handing it every active entry is what produced a six-receipt greatest-hits deck.
        from cqc_lem.app.run_content_plan import create_carousel_content
        entries = [{"id": 1, "kind": "artifact", "title": "Slide render",
                    "body": "Rendered 40 slides in one pass", "active": True, "used_count": 0},
                   {"id": 2, "kind": "number", "title": "Release count",
                    "body": "Shipped 160 releases in a year", "active": True, "used_count": 3}]
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=entries), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("caption", {"bogus": True})) as gen:
            create_carousel_content(1, "awareness", None)
        anchors = gen.call_args[1]["fact_anchors"]
        assert any("40 slides" in a for a in anchors)
        assert not any("160 releases" in a for a in anchors)
        # …and the same single entry is what the writer is told to build the proof slot out of.
        assert "Rendered 40 slides in one pass" in gen.call_args[1]["story_directive"]
        assert "160 releases" not in gen.call_args[1]["story_directive"]

    def test_text_posts_do_not_force_the_save_targeted_bias(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.select_blueprint", return_value={}) as select:
            rcp._select_post_blueprint(1)
        assert select.call_args[1]["prefer_save_targeted"] is False

    def test_shape_history_and_performance_failures_degrade_to_plain_rotation(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_recent_post_shape_history", side_effect=RuntimeError("down")), \
             patch(f"{_RCP}.get_shape_performance", side_effect=RuntimeError("down")):
            blueprint = rcp._select_post_blueprint(1)
        assert blueprint["format"] and blueprint["hook_style"]
