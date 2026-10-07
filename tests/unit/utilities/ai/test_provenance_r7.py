"""Showcase round 7: every figure printed on an image or used in a hook has provenance.

The critic (gauntlet/critic7) found 80% and 5.6h on slot_141 (hook + body, unsourced), 45% on
rhythm_4's image, 53.7% on cover_18 and "Error rates dropped sharply" as slot_130's headline.
docs/content-core.md and docs/image-stack.md, "Showcase round 7".
"""

import dataclasses
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import fact_consistency as fc, image_concept as ic
from cqc_lem.utilities.ai.content_framework import numeric_claims

pytestmark = pytest.mark.unit

SLOT_141 = ("5.6 hours per week – could that reclaimed time transform your small "
            "business? It adds up fast.\n\n80 % of companies now use or plan AI chatbots.\n\n"
            "In a recent test I spent 55 minutes tracking a bug and all 14 tests passed.")
FACTS_141 = ["I spent 55 minutes tracking a bug; 14 tests passed after the AI fix."]


def _concept(**kw) -> ic.ImageConcept:
    base = dict(thesis="Dynamic routing cuts AI costs 45% without hurting performance",
                audience="ops", specific_entities=("45%",), emotional_beat="calm",
                hook_phrase="", treatment="editorial_concept", treatment_rationale="x")
    base.update(kw)
    return ic.ImageConcept(**base)


class TestNumericClaimsDecimalAtLineStart:
    def test_a_line_opening_decimal_is_a_figure_not_numbering(self):
        assert [c["raw"] for c in numeric_claims("5.6 hours per week, gone.")] == ["5.6"]
        assert [c["value"] for c in numeric_claims("53.7% of posts miss.")] == ["53.7"]

    def test_list_numbering_is_still_structure(self):
        assert numeric_claims("1. Route the cheap prompts\n2) Measure") == []


class TestFigureProvenance:
    def test_a_story_bank_fact_vouches(self):
        assert fc.figure_provenance("55", SLOT_141, FACTS_141) == fc.PROVENANCE_FACT

    def test_a_figure_absent_from_the_body_is_never_vouched(self):
        assert fc.figure_provenance("45", "Routing saved money.", ["45% saved"]) == ""

    def test_a_named_source_in_the_sentence_or_the_one_before(self):
        same = "According to Gartner, 60% of firms overspend."
        before = "Originality.ai looked at 3,000 posts. 53.7% of them read as machine-written."
        assert fc.figure_provenance("60", same, []) == fc.PROVENANCE_SOURCE
        assert fc.figure_provenance("53.7", before, []) == fc.PROVENANCE_SOURCE

    def test_first_person_counts_only_without_an_allow_list(self):
        body = "By routing, my client cut AI costs by 45%."
        assert fc.figure_provenance("45", body, None) == fc.PROVENANCE_FIRST_HAND
        assert fc.figure_provenance("45", body, ["routing saved money"]) == ""

    def test_a_pronoun_opening_a_sentence_is_not_a_source(self):
        assert fc.figure_provenance("40", "We found errors fell 40%.", []) == ""

    def test_unprovenanced_figures_dedupes_by_value(self):
        assert fc.unprovenanced_figures("45 % and 45%", "Costs fell 45%.", []) == ["45 %"]
        assert fc.unprovenanced_figures("", "x", []) == []

    def test_prints_any_compares_values(self):
        assert fc.prints_any("cut 53.7 % of waste", ("53.7%",))
        assert not fc.prints_any("cut waste", ("53.7%",))
        assert not fc.prints_any("cut 5%", ())


class TestSourceName:
    @pytest.mark.parametrize("body,name", [
        ("According to Originality.ai, 53.7% of long posts are AI-written.", "Originality.ai"),
        ("Gartner's 2025 survey found 60% of firms overspend.", "Gartner"),
        ("60% of firms overspend (Gartner, 2025).", "Gartner"),
        ("A study by the Stanford HAI lab found 60% of firms overspend.", "Stanford HAI"),
        ("Originality.ai looked at 3,000 posts. 53.7% read as machine-written.", "Originality.ai"),
    ])
    def test_the_name_a_cover_can_print(self, body, name):
        value = numeric_claims(body)[-1]["value"]
        assert fc.source_name_for(value, body) == name

    def test_a_citation_marker_names_nobody(self):
        assert fc.source_name_for("60", "60% of firms overspend [2].") == ""


class TestHook:
    def test_the_hook_is_the_opening_sentence(self):
        assert fc.hook_line("\n\nFirst one. Second one.\nNext line.") == "First one."
        assert fc.hook_line("") == ""

    def test_slot_141_hook_is_flagged(self):
        issues = fc.hook_provenance_issues(SLOT_141, FACTS_141)
        assert len(issues) == 1 and '"5.6"' in issues[0]

    def test_a_backed_hook_passes(self):
        text = "55 minutes on one bug. Then the AI fixed it.\n\nI spent 55 minutes on it."
        assert fc.hook_provenance_issues(text, FACTS_141) == []

    def test_a_figure_the_grounding_gate_already_holds_is_not_reported_twice(self):
        assert fc.hook_provenance_issues(SLOT_141, FACTS_141, already_flagged=["5.6"]) == []

    def test_consistency_report_runs_it_only_with_an_allow_list(self):
        assert fc.consistency_report(SLOT_141)["provenance"] == []
        report = fc.consistency_report(SLOT_141, hook_facts=FACTS_141)
        assert not report["passes"] and report["provenance"] == report["issues"]


class TestVagueResult:
    @pytest.mark.parametrize("text", ["Error rates dropped sharply",
                                      "Satisfaction rose noticeably within three months",
                                      "Costs dramatically decreased"])
    def test_a_numberless_result_is_vague(self, text):
        assert fc.vague_result_claim(text)

    @pytest.mark.parametrize("text", ["Errors dropped 40%", "Routing cuts costs", "", None])
    def test_a_number_or_a_mechanism_is_not(self, text):
        assert fc.vague_result_claim(text) == ""

    def test_the_hook_rules_refuse_it(self):
        assert "claims a result with no figure" in ic.hook_rejection(
            "Error rates dropped sharply", None, "error rates", "Error rates dropped sharply.")


class TestApplyFigureProvenance:
    BODY = ("Imagine steering your AI spend. By routing, my client cut AI costs by 45%. "
            "Simple requests go to a lightweight model.")

    def test_rhythm_4_an_unbacked_hook_figure_is_re_set(self):
        concept = _concept(hook_phrase="Dynamic routing cuts costs 45%")
        out = ic.apply_figure_provenance(concept, None, self.BODY, facts=["routing saved money"])
        assert "45" not in out.hook_phrase
        assert out.hook_verified == out.hook_phrase
        assert out.unsourced_figures == ("45%",)

    def test_a_vouched_hook_is_kept(self):
        concept = _concept(hook_phrase="Dynamic routing cuts costs 45%")
        out = ic.apply_figure_provenance(concept, None, self.BODY, facts=["Client cut costs 45%"])
        assert out is concept

    def test_without_an_allow_list_first_person_vouches(self):
        concept = _concept(hook_phrase="Dynamic routing cuts costs 45%")
        assert ic.apply_figure_provenance(concept, None, self.BODY) is concept

    def test_a_vague_result_hook_is_re_set(self):
        concept = _concept(thesis="Oversight framework guides the AI rollout",
                           hook_phrase="Error rates dropped sharply", specific_entities=())
        out = ic.apply_figure_provenance(concept, None, "The oversight framework guides the "
                                         "AI rollout. Error rates dropped sharply.")
        assert out.hook_phrase != "Error rates dropped sharply"
        assert not fc.vague_result_claim(out.hook_phrase)

    def test_no_clean_clause_means_no_headline(self):
        concept = _concept(thesis="45%", hook_phrase="Costs fell 45%")
        out = ic.apply_figure_provenance(concept, None, "Costs fell 45% last year.", facts=[])
        assert out.hook_phrase == "" and out.hook_options is None

    def test_a_graphic_drawing_an_unbacked_figure_is_dropped(self):
        graphic = {"stat": {"display": "45%", "label": "cost cut", "source_sentence": "45%"},
                   "source_line": ""}
        concept = _concept(graphic=graphic)
        out = ic.apply_figure_provenance(concept, None, self.BODY, facts=[])
        assert "stat" not in (out.graphic or {})

    def test_a_cover_carries_the_editions_named_source(self):
        title = "53.7% of LinkedIn posts miss their mark"
        body = ("Originality.ai looked at 3,000 posts. 53.7% of LinkedIn posts read as "
                "machine-written.")
        graphic = {"stat": {"display": "53.7%", "label": "miss their mark"}, "source_line": ""}
        concept = _concept(hook_phrase="53.7% of posts miss", graphic=graphic)
        out = ic.apply_figure_provenance(concept, title, f"{title}\n\n{body}", facts=[],
                                         surface="newsletter")
        assert out.graphic["source_line"] == "Source: Originality.ai"
        assert out.hook_phrase == "53.7% of posts miss"

    def test_a_cover_figure_the_body_never_sources_records_it(self):
        title = "53.7% of LinkedIn Posts Miss Their Mark"
        concept = _concept(hook_phrase="", graphic=None)
        out = ic.apply_figure_provenance(concept, title, f"{title}\n\nPosts read as robotic.",
                                         facts=[], surface="newsletter")
        assert out.unsourced_figures == ("53.7%",)

    def test_provenance_body_drops_the_title(self):
        assert ic.provenance_body("T 5%", "T 5%\n\nbody") == "body"
        assert ic.provenance_body(None, "body") == "body"

    def test_analysis_runs_it_on_a_video_concept_too(self):
        payload = {"thesis": "Reclaimed time transforms a small business",
                   "audience": "owners", "specific_entities": ["5.6 hours"],
                   "emotional_beat": "hope", "hook_phrase": "Reclaim 5.6 hours weekly",
                   "treatment": "editorial_concept", "treatment_rationale": "x"}
        with patch.object(ic, "parse_concept", return_value=_concept(
                hook_phrase=payload["hook_phrase"], thesis=payload["thesis"])), \
                patch("cqc_lem.utilities.ai.client.client") as client:
            client.chat.completions.create.return_value.choices = [
                type("C", (), {"message": type("M", (), {"content": "{}"})(),
                               "finish_reason": "stop"})()]
            concept = ic.analyze_content_for_image(SLOT_141, surface="video", facts=FACTS_141)
        assert "5.6" not in concept.hook_phrase


class TestFallbacks:
    def test_last_resort_skips_a_sentence_with_an_unvouched_figure(self):
        from cqc_lem.utilities.post_image import last_resort_hook

        text = "80% of companies use chatbots. Pick tools that fit your workflow."
        assert last_resort_hook(None, text) == "Pick tools that fit your workflow."

    def test_last_resort_honours_the_concepts_record(self):
        from cqc_lem.utilities.post_image import last_resort_hook

        concept = dataclasses.replace(_concept(), unsourced_figures=("30%",))
        text = "We cut costs 30%. Then we kept going."
        assert last_resort_hook(concept, text) == "Then we kept going."
        assert last_resort_hook(None, "80% of firms use AI.") == "80% of firms use AI."

    def test_story_facts_for(self):
        from cqc_lem.utilities import post_image

        assert post_image.story_facts_for(None) is None
        with patch("cqc_lem.utilities.db.get_story_bank_entries", side_effect=RuntimeError("db")):
            assert post_image.story_facts_for(1) is None
        entry = {"id": 1, "title": "Bug", "body": "I spent 55 minutes on a bug.", "kind": "story"}
        with patch("cqc_lem.utilities.db.get_story_bank_entries", return_value=[entry, "junk"]):
            facts = post_image.story_facts_for(1)
        assert any("55 minutes" in f for f in facts)

    def test_title_card_hook_skips_an_unvouched_opening(self):
        from cqc_lem.utilities.video_title_card import title_card_hook

        text = "80% of companies use chatbots. Pick tools that fit your workflow."
        assert title_card_hook(text) == "Pick tools that fit your workflow."
        concept = dataclasses.replace(_concept(hook_phrase=""), unsourced_figures=("80%",))
        assert title_card_hook(text, concept) == "Pick tools that fit your workflow."

    def test_title_card_hook_fails_open_when_every_candidate_prints_one(self):
        from cqc_lem.utilities.video_title_card import title_card_hook

        assert title_card_hook("80% of firms use AI.") == "80% of firms use AI."

    def test_quote_candidates_skip_the_cta_and_unvouched_figures(self):
        from cqc_lem.utilities.ai.post_treatment import quote_candidates

        text = ("One static guardrail breaks the day you add a vendor. "
                "Nearly 60% of firms skip the review entirely. "
                "Save this for your next vendor review.")
        got = quote_candidates(text)
        assert got == ["One static guardrail breaks the day you add a vendor."]


class TestCover:
    def test_the_fallback_headline_goes_numberless(self):
        from cqc_lem.utilities.newsletter_cover import cover_headline

        concept = dataclasses.replace(_concept(thesis="LinkedIn posts miss their mark"),
                                      unsourced_figures=("53.7%",))
        assert cover_headline(None, concept, "53.7% of LinkedIn Posts Miss Their Mark", None) \
            == "LinkedIn posts miss their mark"

    def test_a_numberless_subtitle_wins_over_the_thesis(self):
        from cqc_lem.utilities.newsletter_cover import cover_headline

        concept = dataclasses.replace(_concept(), unsourced_figures=("53.7%",))
        assert cover_headline(None, concept, "53.7% miss", "Why posts read as robotic") \
            == "Why posts read as robotic"

    def test_the_title_without_the_figure_is_the_last_word(self):
        from cqc_lem.utilities.newsletter_cover import _without_figures, cover_headline

        concept = dataclasses.replace(_concept(thesis="53.7%"), unsourced_figures=("53.7%",))
        assert cover_headline(None, concept, "53.7% of Posts Miss", None) == "Posts Miss"
        assert _without_figures("53.7%", ("53.7%",)) == "53.7%"

    def test_a_stated_hook_still_wins(self):
        from cqc_lem.utilities.newsletter_cover import cover_headline

        assert cover_headline("Posts read as robotic", None, "53.7% miss", None) \
            == "Posts read as robotic"

    def test_cited_byline(self):
        from cqc_lem.utilities.newsletter_cover import cited_byline

        concept = _concept(graphic={"source_line": "Source: Originality.ai"})
        assert cited_byline("Chris", "53.7% of posts miss", concept) == \
            "Chris · Source: Originality.ai"
        assert cited_byline(None, "53.7% of posts miss", concept) == "Source: Originality.ai"
        assert cited_byline("Chris", "Posts miss their mark", concept) == "Chris"
        stat = dataclasses.replace(concept, archetype="stat_card")
        assert cited_byline("Chris", "53.7% of posts miss", stat) == "Chris"
        assert cited_byline("Chris", "53.7% of posts miss", None) == "Chris"
