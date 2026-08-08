"""Unit tests for the save-optimized post archetypes (issue #619 / G4): the build receipt and the
bookmarkable compendium, their number-led hook constraints, and the no-fabrication guard that keeps
a draft from inventing the specifics those archetypes are made of.
"""

import pytest

from cqc_lem.utilities.ai import content_framework as fw
from cqc_lem.utilities.quality_gates import GATE_FACT_GROUNDING, demoting_findings, fact_grounding_finding

pytestmark = pytest.mark.unit

SAVE_TARGETED = ("build_receipt", "resource_compendium")


class TestArchetypesJoinTheSharedMenu:
    def test_both_archetypes_are_in_the_post_menu(self):
        for key in SAVE_TARGETED:
            assert key in fw.POST_FORMATS
            assert key in fw.MENUS["post"]["formats"]

    def test_they_carry_a_full_structure_like_every_other_post_format(self):
        for key in SAVE_TARGETED:
            meta = fw.POST_FORMATS[key]
            assert meta["label"] and meta["guidance"]
            assert len(meta["structure"]) >= 5
            assert "hook" in meta["structure"][0].lower()
            assert "cta" in meta["structure"][-1].lower()

    def test_build_receipt_structure_covers_the_receipt_beats(self):
        blob = " ".join(fw.POST_FORMATS["build_receipt"]["structure"]).lower()
        for beat in ("stack", "broke", "use case"):
            assert beat in blob

    def test_they_are_marked_save_targeted_and_fact_anchored(self):
        for key in SAVE_TARGETED:
            assert fw.is_save_targeted("post", key)
            assert fw.requires_fact_anchor("post", key)
        assert sorted(fw.save_targeted_formats("post")) == sorted(SAVE_TARGETED)

    def test_other_post_archetypes_are_neither(self):
        for key in ("personal_lesson", "how_to", "contrarian_take"):
            assert not fw.is_save_targeted("post", key)
            assert not fw.requires_fact_anchor("post", key)

    def test_unknown_and_empty_formats_are_safe(self):
        assert not fw.is_save_targeted("post", "no_such_format")
        assert not fw.requires_fact_anchor("post", None)
        assert fw.format_meta("post", "no_such_format") == {}


class TestSelectionAndRotation:
    def test_guidance_can_name_the_new_archetypes(self):
        assert fw.select_blueprint("post", guidance="make it a build receipt")["format"] \
            == "build_receipt"
        assert fw.select_blueprint("post", guidance="resource compendium")["format"] \
            == "resource_compendium"

    def test_the_new_archetypes_participate_in_shape_rotation(self):
        # The V51 shape history is what a fresh post rotates away from — a just-used save-targeted
        # archetype must not come back immediately, exactly like every other format.
        for key in SAVE_TARGETED:
            for _ in range(25):
                assert fw.select_blueprint("post", recent_formats=[key])["format"] != key

    def test_prefer_save_targeted_biases_selection_without_forcing_it(self):
        picks = [fw.select_blueprint("post", prefer_save_targeted=True)["format"]
                 for _ in range(200)]
        assert any(p in SAVE_TARGETED for p in picks)
        assert any(p not in SAVE_TARGETED for p in picks)

    def test_prefer_save_targeted_still_respects_no_consecutive_repeat(self):
        for _ in range(30):
            picked = fw.select_blueprint("post", recent_formats=["build_receipt"],
                                         prefer_save_targeted=True)["format"]
            assert picked != "build_receipt"

    def test_selected_hook_is_always_number_led(self):
        for key in SAVE_TARGETED:
            for _ in range(30):
                bp = fw.select_blueprint("post", guidance=key.replace("_", " "))
                assert bp["format"] == key
                assert bp["hook_style"] in fw.NUMBER_LED_HOOK_STYLES

    def test_enforce_variety_rewrites_an_off_menu_hook_for_these_archetypes(self):
        out = fw.enforce_variety("post", [{"format": "build_receipt", "hook_style": "question"}])
        assert out[0]["format"] == "build_receipt"
        assert out[0]["hook_style"] in fw.NUMBER_LED_HOOK_STYLES

    def test_enforce_variety_leaves_a_normal_format_on_the_full_hook_menu(self):
        out = fw.enforce_variety("post", [{"format": "how_to", "hook_style": "question"}])
        assert out[0]["hook_style"] == "question"

    def test_excluded_formats_are_off_the_menu_entirely(self):
        excluded = fw.fact_anchored_formats("post")
        # The save-targeted pair are fact-anchored, and so are the occasion archetypes (#1074) —
        # a launch or a credential is nothing BUT its specifics. Nothing else is.
        assert sorted(excluded) == sorted(set(SAVE_TARGETED) | set(fw.occasion_formats("post")))
        for _ in range(200):
            bp = fw.select_blueprint("post", prefer_save_targeted=True, exclude_formats=excluded)
            assert bp["format"] not in excluded
        # Even an explicit hint cannot pull an excluded archetype back in.
        assert fw.select_blueprint("post", guidance="build receipt",
                                   exclude_formats=excluded)["format"] not in excluded

    def test_excluding_everything_falls_back_to_the_full_menu(self):
        bp = fw.select_blueprint("post", exclude_formats=list(fw.POST_FORMATS))
        assert bp["format"] in fw.POST_FORMATS

    def test_allowed_hooks_narrows_only_for_the_new_archetypes(self):
        assert set(fw.allowed_hooks("post", "build_receipt")) == set(fw.NUMBER_LED_HOOK_STYLES)
        assert fw.allowed_hooks("post", "how_to") == fw.HOOK_STYLES
        assert fw.allowed_hooks("post", None) == fw.HOOK_STYLES


class TestHookConstraints:
    def test_a_number_led_hook_inside_the_mobile_budget_passes(self):
        report = fw.hook_report("I shipped 20 agents in 3 weeks. Here is what broke.\n\nBody.",
                                format_key="build_receipt")
        assert report["number_led"] and report["within_mobile_budget"] and report["passes"]

    def test_a_hook_without_a_number_fails_a_number_led_archetype(self):
        report = fw.hook_report("Here is what I learned building agents.\n\nBody.",
                                format_key="build_receipt")
        assert not report["number_led"]
        assert not report["passes"]
        assert "does not lead with a number" in report["issues"][0]

    def test_a_hook_over_the_mobile_budget_fails(self):
        long_hook = "I shipped 20 agents and " + ("a very long clause " * 12)
        report = fw.hook_report(long_hook, format_key="build_receipt")
        assert report["chars"] > fw.MOBILE_HOOK_MAX_CHARS
        assert not report["within_mobile_budget"]
        assert not report["passes"]

    def test_a_placeholder_counts_as_the_number_slot(self):
        report = fw.hook_report("I shipped [[COUNT: how many agents]] agents in one sprint.",
                                format_key="build_receipt")
        assert report["number_led"] and report["passes"]

    def test_non_number_led_archetypes_are_not_graded(self):
        report = fw.hook_report("A hook with no number at all.", format_key="how_to")
        assert not report["required"]
        assert report["passes"]

    def test_no_format_means_no_requirement(self):
        assert fw.hook_report("Anything at all.")["passes"]

    def test_the_directive_names_the_mobile_budget(self):
        text = fw.hook_constraint_directive()
        assert str(fw.MOBILE_HOOK_MAX_CHARS) in text
        assert "REAL NUMBER" in text


class TestNoFabricationGuard:
    def test_a_draft_with_invented_specifics_is_blocked(self):
        draft = "I built a 20-agent team in 3 weeks and cut handling time 40%."
        report = fw.fact_grounding_report(draft)
        assert not report["passes"]
        assert report["unverified_values"] == ["20", "3", "40%"]
        assert not fw.meets_fact_grounding(draft)

    def test_the_same_specifics_pass_once_a_verified_fact_backs_them(self):
        draft = "I built a 20-agent team in 3 weeks and cut handling time 40%."
        anchors = ["Shipped a 20-agent research team over 3 weeks",
                   "Handling time dropped 40% after rollout"]
        report = fw.fact_grounding_report(draft, anchors)
        assert report["passes"]
        assert report["unverified"] == []
        assert len(report["verified"]) == 3
        assert fw.meets_fact_grounding(draft, anchors)

    def test_a_placeholder_draft_passes_but_needs_approval(self):
        draft = ("I built a [[COUNT: how many]] agent team in [[DURATION: how long]].\n\n"
                 "It broke on [[FAILURE: what went wrong]].")
        report = fw.fact_grounding_report(draft)
        assert report["passes"]
        assert report["needs_approval"]
        assert report["placeholders"] == ["COUNT: how many", "DURATION: how long",
                                          "FAILURE: what went wrong"]

    def test_a_clean_draft_needs_no_approval(self):
        report = fw.fact_grounding_report("A receipt with no specifics at all.")
        assert report["passes"] and not report["needs_approval"]

    def test_list_numbering_is_structure_not_a_claim(self):
        draft = "What I used:\n\n1. A parser\n2. A queue\n3. A router\n\nStep 2 is the fiddly one."
        assert fw.fact_grounding_report(draft)["passes"]

    def test_a_count_the_draft_proves_on_its_own_page_is_self_verified(self):
        draft = "The 3 checks I run before shipping.\n\n1. Types\n2. Tests\n3. Rollback"
        assert fw.fact_grounding_report(draft)["passes"]

    def test_a_count_that_does_not_match_the_list_is_still_blocked(self):
        draft = "The 9 checks I run before shipping.\n\n1. Types\n2. Tests\n3. Rollback"
        assert not fw.fact_grounding_report(draft)["passes"]

    def test_a_bare_year_is_a_public_fact_not_a_fabrication(self):
        assert fw.fact_grounding_report("Everything changed in 2026 for this stack.")["passes"]

    def test_a_named_stack_is_not_a_pile_of_invented_numbers(self):
        # The build receipt's structure REQUIRES naming the exact stack, so model/version numbers
        # must not read as fabricated metrics — otherwise every honest receipt burns its one
        # regeneration on a directive that mangles the tool names, then gets held anyway.
        draft = ("The stack: GPT-4o for extraction, Claude 3.5 Sonnet for review, Python 3.12, "
                 "Postgres 16, and n8n to glue it together.")
        assert fw.fact_grounding_report(draft)["passes"]

    def test_a_version_number_is_exempt_but_a_metric_beside_it_is_not(self):
        report = fw.fact_grounding_report("Claude 3.5 Sonnet cut review time 40%.")
        assert report["unverified_values"] == ["40%"]

    def test_a_magnitude_suffix_is_a_metric_not_a_build_number(self):
        report = fw.fact_grounding_report("It saved 10k hours of manual triage.")
        assert report["unverified_values"] == ["10"]

    def test_a_money_amount_after_a_product_name_is_still_a_claim(self):
        # Only BARE numbers can be version-like; a unit ($, %, x) makes it a metric regardless.
        assert not fw.fact_grounding_report("Zapier cost us $4,000 last quarter.")["passes"]

    def test_a_trailing_comma_is_not_part_of_the_number(self):
        report = fw.fact_grounding_report("We ran 16, then stopped.")
        assert report["unverified_values"] == ["16"]

    def test_a_year_shaped_price_is_still_a_claim(self):
        assert not fw.fact_grounding_report("The whole build cost $2,024.")["passes"]

    def test_money_and_multipliers_are_graded_as_specifics(self):
        report = fw.fact_grounding_report("It cost $4,000 and ran 2.5x faster.")
        assert not report["passes"]
        assert [c["value"] for c in report["unverified"]] == ["4000", "2.5"]

    def test_unverified_claims_carry_their_sentence_for_the_review_ui(self):
        report = fw.fact_grounding_report("Nothing here.\n\nIt saved 12 hours a week.")
        assert report["unverified"][0]["context"] == "It saved 12 hours a week."

    def test_empty_content_is_not_a_fabrication(self):
        for value in (None, "", "   "):
            assert fw.fact_grounding_report(value)["passes"]

    def test_numeric_claims_ignores_placeholders(self):
        assert fw.numeric_claims("Cut it by [[METRIC: 55 percent]] overall.") == []

    def test_a_placeholder_satisfies_the_first_person_proof_slot(self):
        # Otherwise the proof-slot regeneration would fight the guard: the proof slot wants a real
        # number, the guard forbids stating one without a verified fact.
        assert fw.has_first_person_proof("I lost [[DURATION: how long]] to that dead end.")


class TestWriterDirectives:
    def test_the_blueprint_directive_injects_the_no_fabrication_rules(self):
        bp = fw.select_blueprint("post", guidance="build receipt")
        text = fw.blueprint_directive("post", bp)
        assert "NO-FABRICATION RULE" in text
        assert fw.FACT_PLACEHOLDER_EXAMPLE in text
        assert "HOOK CONSTRAINTS" in text

    def test_verified_facts_are_listed_verbatim_when_supplied(self):
        bp = fw.select_blueprint("post", guidance="build receipt")
        text = fw.blueprint_directive("post", bp, fact_anchors=["Ran 47 scrapes in one night"])
        assert "Ran 47 scrapes in one night" in text
        assert "ONLY verified facts" in text

    def test_anchors_can_ride_on_the_blueprint_itself(self):
        # How the post pipeline gets the story-bank entry's facts through six prompt builders (and
        # the recursive type fallbacks) without threading a new argument through every one of them.
        bp = fw.select_blueprint("post", guidance="build receipt")
        bp["fact_anchors"] = ["Ran 47 scrapes in one night"]
        text = fw.blueprint_directive("post", bp)
        assert "Ran 47 scrapes in one night" in text
        assert "ONLY verified facts" in text

    def test_an_explicit_anchor_argument_wins_over_the_blueprint(self):
        bp = fw.select_blueprint("post", guidance="build receipt")
        bp["fact_anchors"] = ["Ran 47 scrapes in one night"]
        text = fw.blueprint_directive("post", bp, fact_anchors=["Cut the run to 9 minutes"])
        assert "Cut the run to 9 minutes" in text
        assert "47 scrapes" not in text

    def test_blueprint_carried_anchors_are_not_persisted_with_the_shape(self):
        bp = fw.select_blueprint("post", guidance="build receipt")
        bp["fact_anchors"] = ["Ran 47 scrapes in one night"]
        assert "fact_anchors" not in fw.compact_blueprint(bp)

    def test_a_normal_archetype_gets_no_fabrication_block(self):
        text = fw.blueprint_directive("post", fw.select_blueprint("post", guidance="mini how-to"))
        assert "NO-FABRICATION RULE" not in text
        assert "HOOK CONSTRAINTS" not in text

    def test_the_anchorless_directive_demands_placeholders_only(self):
        text = fw.fact_anchor_directive([])
        assert "NO verified project facts" in text
        assert "OVERRIDES" in text

    def test_blank_anchors_are_treated_as_none(self):
        assert fw.fact_anchor_directive(["  ", None]) == fw.fact_anchor_directive(None)

    def test_the_retry_directive_names_the_offending_numbers(self):
        report = fw.fact_grounding_report("We shipped 12 bots in 5 days.")
        text = fw.fact_retry_directive(report)
        assert "12" in text and "5" in text
        assert fw.FACT_PLACEHOLDER_EXAMPLE in text


class TestCarouselWiring:
    def test_the_archetype_maps_onto_the_slides(self):
        text = fw.carousel_blueprint_directive({"format": "build_receipt"})
        assert "Build Receipt" in text
        assert "SLIDES IN THIS ORDER" in text
        assert "HOOK CONSTRAINTS" in text
        assert "NO-FABRICATION RULE" in text

    def test_a_normal_archetype_maps_without_the_fact_rules(self):
        text = fw.carousel_blueprint_directive({"format": "tactical_list"})
        assert "Tactical List" in text
        assert "NO-FABRICATION RULE" not in text

    def test_anchors_reach_the_carousel_writer(self):
        text = fw.carousel_blueprint_directive({"format": "build_receipt"},
                                               fact_anchors=["Cut render time to 90 seconds"])
        assert "Cut render time to 90 seconds" in text

    def test_a_missing_or_unknown_blueprint_is_a_no_op(self):
        assert fw.carousel_blueprint_directive(None) == ""
        assert fw.carousel_blueprint_directive({}) == ""
        assert fw.carousel_blueprint_directive({"format": "not_a_format"}) == ""


class TestFactGroundingFinding:
    def test_invented_specifics_hold_the_post(self):
        finding = fact_grounding_finding(["20", "40%"], [])
        assert finding["gate"] == GATE_FACT_GROUNDING
        assert finding["demoted"]
        assert demoting_findings([finding]) == [finding]
        assert "Unbacked specific: 20" in finding["details"]

    def test_placeholders_hold_the_post_for_the_author_to_fill_in(self):
        finding = fact_grounding_finding([], ["METRIC: hours saved"])
        assert finding["demoted"]
        assert "placeholder" in finding["explanation"].lower()
        assert "Fill in: METRIC: hours saved" in finding["details"]

    def test_invented_specifics_win_over_placeholders_in_the_explanation(self):
        finding = fact_grounding_finding(["7"], ["METRIC: hours saved"])
        assert "no verified fact backs" in finding["explanation"]

    def test_details_are_capped_so_one_finding_cannot_flood_the_review_ui(self):
        finding = fact_grounding_finding([str(n) for n in range(50)], [])
        assert len(finding["details"]) == 10
