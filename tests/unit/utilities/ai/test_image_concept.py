"""Stage 1 of the image engine: the whole piece in, a grounded ImageConcept out (issue #2241)."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai.image_concept import (
    TREATMENTS,
    ImageConcept,
    analyze_content_for_image,
    anchor_rejection,
    enforce_graphic_cap,
    entity_mentioned,
    is_fact_only,
    parse_concept,
    pick_visual_idea,
)

pytestmark = pytest.mark.unit

_CREATE = "cqc_lem.utilities.ai.client.client.chat.completions.create"

_SOURCE = ("Our agency nearly missed payroll in March. Three clients sat on unpaid invoices for "
           "90 days while the payroll run came due, and the agency owner covered it from a "
           "personal credit card. Here is the collections process we built afterwards.")

_PAYLOAD = {"thesis": "Late invoices quietly starve a small agency's payroll",
            "audience": "agency owners",
            "specific_entities": ["unpaid invoices", "payroll run", "agency owner",
                                  "personal credit card"],
            "visual_anchors": ["an agency owner", "a stack of unpaid invoices",
                               "a payroll run printout"],
            "emotional_beat": "quiet dread", "hook_phrase": "90 days unpaid",
            "treatment": "concrete_scene", "treatment_rationale": "a tangible situation"}


# _SOURCE plus the numbers the numeric-hook tests quote — a hook's numbers must be the piece's.
_NUM_SOURCE = (_SOURCE + " AI posts got 45% less engagement, 30% of AI answers were wrong, and "
               "$30K was wasted on AI tools; 45% fewer invoices were paid on time.")


def _resp(payload) -> SimpleNamespace:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class TestEntityMentioned:
    @pytest.mark.parametrize("entity,text", [
        ("unpaid invoices", "the invoice was unpaid for weeks"),
        ("payroll run", "before the PAYROLL RUN"),
        ("hiring manager", "every hiring decision the manager made"),
        ("90 days", "for 90 days straight"),
    ])
    def test_loose_stems_match(self, entity, text):
        assert entity_mentioned(entity, text)

    @pytest.mark.parametrize("entity,text", [
        ("brass valve", "an article about invoices"),
        ("the and of", "the and of"),
        ("", "anything"),
    ])
    def test_absent_or_empty_entities_do_not(self, entity, text):
        assert not entity_mentioned(entity, text)


class TestParseConcept:
    def test_a_grounded_reply_parses(self):
        concept = parse_concept(_PAYLOAD, _SOURCE, title="How we fixed collections")
        assert isinstance(concept, ImageConcept)
        assert concept.specific_entities == ("unpaid invoices", "payroll run", "agency owner",
                                             "personal credit card")
        assert concept.treatment == "concrete_scene" and not concept.weak
        assert concept.hook_phrase == "90 days unpaid"
        assert concept.to_dict()["thesis"] == _PAYLOAD["thesis"]

    def test_invented_entities_are_dropped(self):
        payload = dict(_PAYLOAD, specific_entities=["unpaid invoices", "a brass valve",
                                                    "a glowing robot", "payroll run"])
        concept = parse_concept(payload, _SOURCE)
        assert concept.specific_entities == ("unpaid invoices", "payroll run")

    def test_fewer_than_two_surviving_anchors_is_weak_but_usable(self):
        payload = dict(_PAYLOAD, visual_anchors=["an agency owner", "a lighthouse keeper"])
        concept = parse_concept(payload, _SOURCE)
        assert concept is not None and concept.weak
        assert concept.visual_anchors == ("an agency owner",)

    def test_duplicate_entities_collapse_and_the_list_is_capped(self):
        payload = dict(_PAYLOAD, specific_entities=["payroll run", "Payroll run"] + [
            "agency"] * 3 + ["clients", "collections process", "March", "credit card",
                             "unpaid invoices"])
        concept = parse_concept(payload, _SOURCE)
        assert len(concept.specific_entities) == 6
        assert concept.specific_entities.count("payroll run") == 1

    def test_no_thesis_is_unusable(self):
        assert parse_concept(dict(_PAYLOAD, thesis="  "), _SOURCE) is None
        assert parse_concept(None, _SOURCE) is None
        assert parse_concept(["not", "a", "dict"], _SOURCE) is None

    @pytest.mark.parametrize("hook", ["one", "a hook phrase that is far too many words long",
                                      "Supercalifragilistic expialidocious x"])
    def test_a_hook_outside_two_to_five_words_or_32_chars_is_dropped(self, hook):
        assert parse_concept(dict(_PAYLOAD, hook_phrase=hook), _SOURCE).hook_phrase == ""

    def test_a_hook_restating_the_title_is_dropped(self):
        concept = parse_concept(dict(_PAYLOAD, hook_phrase="Fixed Collections"), _SOURCE,
                                title="How we fixed collections")
        assert concept.hook_phrase == ""

    def test_quotes_around_a_hook_are_stripped(self):
        concept = parse_concept(dict(_PAYLOAD, hook_phrase='"90 days unpaid"'), _SOURCE)
        assert concept.hook_phrase == "90 days unpaid"

    def test_a_graphic_with_no_usable_hook_becomes_a_concrete_scene(self):
        payload = dict(_PAYLOAD, treatment="editorial_graphic", hook_phrase="x")
        assert parse_concept(payload, _SOURCE).treatment == "concrete_scene"

    def test_a_graphic_with_a_hook_stays_a_graphic(self):
        payload = dict(_PAYLOAD, treatment="editorial_graphic")
        assert parse_concept(payload, _SOURCE).treatment == "editorial_graphic"

    def test_metaphor_is_overruled_when_the_text_is_concrete(self):
        payload = dict(_PAYLOAD, treatment="metaphor_last_resort")
        assert parse_concept(payload, _SOURCE).treatment == "concrete_scene"

    def test_metaphor_survives_when_nothing_concrete_does(self):
        payload = dict(_PAYLOAD, treatment="metaphor_last_resort",
                       visual_anchors=["an agency owner"])
        assert parse_concept(payload, _SOURCE).treatment == "metaphor_last_resort"

    def test_an_unknown_treatment_defaults_to_a_concrete_scene(self):
        assert parse_concept(dict(_PAYLOAD, treatment="collage"), _SOURCE).treatment == \
            "concrete_scene"

    def test_people_scene_is_kept(self):
        assert parse_concept(dict(_PAYLOAD, treatment="People_Scene"), _SOURCE).treatment == \
            "people_scene"

    def test_the_four_treatments(self):
        assert TREATMENTS == ("people_scene", "editorial_graphic", "concrete_scene",
                              "metaphor_last_resort")


class TestAnalyzeContentForImage:
    def test_one_lem_medium_json_call_over_the_full_text(self):
        long_text = _SOURCE + (" filler" * 3000)
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            concept = analyze_content_for_image(long_text, title="Collections", surface="newsletter",
                                                user_id=1)
        assert concept.thesis == _PAYLOAD["thesis"]
        kwargs = create.call_args[1]
        assert create.call_count == 1
        assert kwargs["model"] == "lem-medium"
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["max_tokens"] >= 2000, "lem-medium is a reasoning model"
        user = kwargs["messages"][1]["content"]
        assert "<title>Collections</title>" in user and "Surface: newsletter" in user
        body = user.split("<content>")[1].split("</content>")[0]
        assert len(body) == 12000

    def test_the_system_prompt_forbids_invention_and_names_every_treatment(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE)
        system = create.call_args[1]["messages"][0]["content"]
        assert "Never invent one" in system
        for treatment in TREATMENTS:
            assert treatment in system

    def test_a_fenced_reply_is_accepted(self):
        fenced = "```json\n" + json.dumps(_PAYLOAD) + "\n```"
        with patch(_CREATE, return_value=_resp(fenced)):
            assert analyze_content_for_image(_SOURCE) is not None

    def test_an_outage_returns_none(self):
        with patch(_CREATE, side_effect=RuntimeError("proxy down")):
            assert analyze_content_for_image(_SOURCE) is None

    def test_an_unusable_reply_returns_none(self):
        with patch(_CREATE, return_value=_resp("not json at all")):
            assert analyze_content_for_image(_SOURCE) is None
        with patch(_CREATE, return_value=_resp({"thesis": ""})):
            assert analyze_content_for_image(_SOURCE) is None

    def test_empty_content_never_calls_the_llm(self):
        with patch(_CREATE) as create:
            assert analyze_content_for_image("  ", title=None) is None
        create.assert_not_called()

    def test_it_is_a_traced_llm_step(self):
        assert analyze_content_for_image.__llm_span_name__ == "image_concept"


# Gauntlet round 1 of #2241: the real user-1 editions named their facts this way.
_FACTS_SOURCE = ("Terralogic ran a diagnostic audit of its AI tools. GPT-5.2 and Claude Opus 4.5 "
                 "drafts were compared; Stanford HAI found 53.7% of posts fell flat. A marketing "
                 "lead walked the team through a printed audit checklist in a cluttered meeting "
                 "room, and the consultant saved $30K.")
_FACTS = ("Terralogic", "GPT-5.2", "Stanford HAI", "53.7%", "$30K", "diagnostic audit")


class TestVisualAnchors:
    @pytest.mark.parametrize("anchor", [
        "a box representing GPT-5.2", "a Stanford HAI researcher", "a 2025 report",
        "53.7% of posts", "the Terralogic team", "a Claude Opus dashboard", "$30K in savings",
    ])
    def test_names_and_numbers_are_refused_as_anchors(self, anchor):
        assert anchor_rejection(anchor, _FACTS, _FACTS_SOURCE)

    @pytest.mark.parametrize("anchor", [
        "a marketing lead", "a printed audit checklist", "a cluttered meeting room",
        "a consultant", "an AI drafting session",
    ])
    def test_depictable_anchors_pass(self, anchor):
        assert anchor_rejection(anchor, _FACTS, _FACTS_SOURCE) == ""

    def test_a_capitalised_first_word_the_source_never_lowercases_is_a_name(self):
        assert "name" in anchor_rejection("Terralogic offices", (), _FACTS_SOURCE)
        assert anchor_rejection("Marketing lead", (), _FACTS_SOURCE) == ""

    def test_parse_keeps_only_depictable_grounded_anchors(self):
        payload = {"thesis": "Audits find waste", "specific_entities": list(_FACTS),
                   "visual_anchors": ["a marketing lead", "two boxes representing GPT-5.2",
                                      "a printed audit checklist", "a lighthouse keeper",
                                      "Stanford HAI facade", "a cluttered meeting room"],
                   "treatment": "concrete_scene"}
        concept = parse_concept(payload, _FACTS_SOURCE)
        assert concept.visual_anchors == ("a marketing lead", "a printed audit checklist",
                                          "a cluttered meeting room")
        assert not concept.weak
        assert concept.specific_entities[:2] == ("Terralogic", "GPT-5.2"), "facts are kept"

    def test_anchors_are_capped_at_five(self):
        payload = {"thesis": "t", "visual_anchors": [
            "a marketing lead", "a team", "a printed audit checklist", "a meeting room",
            "a consultant", "the posts"]}
        assert len(parse_concept(payload, _FACTS_SOURCE).visual_anchors) == 5

    @pytest.mark.parametrize("fact,expected", [
        ("Terralogic", True), ("GPT-5.2", True), ("$30K", True), ("53.7%", True),
        ("Stanford HAI", True), ("diagnostic audit", False), ("AI tools", False),
    ])
    def test_is_fact_only(self, fact, expected):
        assert is_fact_only(fact) is expected

    def test_a_capitalised_common_noun_the_source_lowercases_is_normalised(self):
        payload = {"thesis": "t", "specific_entities": ["Diagnostic audit", "Terralogic"]}
        concept = parse_concept(payload, _FACTS_SOURCE)
        assert concept.specific_entities == ("diagnostic audit", "Terralogic")


class TestHookRules:
    @pytest.mark.parametrize("hook", ["Stop wasting your budget!", "Start auditing today",
                                      "Don't trust the dashboard", "Wasted, again!",
                                      "Cut the waste now"])
    def test_shouts_and_orders_are_refused(self, hook):
        assert parse_concept(dict(_PAYLOAD, hook_phrase=hook), _SOURCE).hook_phrase == ""

    @pytest.mark.parametrize("hook", ["Payroll first, clients last", "90 days unpaid"])
    def test_curiosity_gaps_and_contrasts_pass(self, hook):
        assert parse_concept(dict(_PAYLOAD, hook_phrase=hook), _SOURCE).hook_phrase == hook

    def test_the_prompt_states_the_hook_rules(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE)
        system = create.call_args[1]["messages"][0]["content"]
        assert "Never an exclamation, never an order to the reader" in system
        assert "NEVER a brand, product, company or model name" in system


class TestTreatmentVariety:
    def _graphic(self):
        return parse_concept(dict(_PAYLOAD, treatment="editorial_graphic"), _SOURCE)

    @pytest.mark.parametrize("recent", [["editorial_graphic"],
                                        ["people_scene", "editorial_graphic"]])
    def test_a_graphic_within_the_window_is_capped_to_people(self, recent):
        capped = enforce_graphic_cap(self._graphic(), recent, "newsletter")
        assert capped.treatment == "people_scene"
        assert "capped" in capped.treatment_rationale

    @pytest.mark.parametrize("recent", [[], None, ["people_scene", "concrete_scene"],
                                        ["people_scene", "concrete_scene", "editorial_graphic"]])
    def test_a_graphic_outside_the_window_stays(self, recent):
        assert enforce_graphic_cap(self._graphic(), recent, "newsletter").treatment == \
            "editorial_graphic"

    def test_only_covers_are_capped(self):
        assert enforce_graphic_cap(self._graphic(), ["editorial_graphic"], "post_image") \
            .treatment == "editorial_graphic"

    def test_three_consecutive_covers_carry_at_most_one_graphic(self):
        history: list[str] = []
        for _ in range(6):
            with patch(_CREATE, return_value=_resp(dict(_PAYLOAD, treatment="editorial_graphic"))):
                concept = analyze_content_for_image(_SOURCE, surface="newsletter",
                                                    recent_treatments=history[:2])
            history.insert(0, concept.treatment)
        for start in range(len(history) - 2):
            assert history[start:start + 3].count("editorial_graphic") <= 1

    def test_recent_treatments_and_the_cover_bias_reach_the_analyst(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE, surface="newsletter",
                                      recent_treatments=["concrete_scene", "people_scene"])
        user = create.call_args[1]["messages"][1]["content"]
        assert "most recent first: concrete_scene, people_scene" in user
        assert "Prefer a different treatment than concrete_scene" in user
        assert "Prefer people_scene" in user and "real faces beat clipart" in user

    def test_other_surfaces_get_no_cover_bias(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE, surface="post_image")
        assert "COVER" not in create.call_args[1]["messages"][1]["content"]


_IDEAS = [
    "A copy editor's red pen frozen mid-strike, her face a half-smile of disbelief.",
    "An empty office chair under a warm lamp beside a cold cup of tea.",
    "An agency owner wincing at the payroll run ahead in an empty office at dusk.",
]


class TestVisualIdeas:
    """Round 3 of #2241: candidate ideas, filtered deterministically, then one cheap pick."""

    @pytest.mark.parametrize("idea,why", [
        ("A pile of coins beside a calculator on a desk.", "stock symbol"),
        ("A founder typing on a laptop late at night.", "stock symbol"),
        ("A manager with a neutral expression at a desk.", "stock symbol"),
        ("Two boxes representing GPT-5.2 glowing on a desk.", "names or numbers"),
        ("A researcher outside Stanford HAI holding a report.", "names or numbers"),
        ("A tag reading $30K on a desk.", "names or numbers"),
    ])
    def test_cliches_names_and_numbers_are_filtered_before_ranking(self, idea, why):
        from cqc_lem.utilities.ai.image_concept import idea_rejection
        assert why in idea_rejection(idea, ("GPT-5.2", "Stanford HAI", "$30K"))

    def test_a_plain_people_led_idea_passes(self):
        from cqc_lem.utilities.ai.image_concept import idea_rejection, is_people_led
        assert idea_rejection(_IDEAS[0], ("GPT-5.2",)) == ""
        assert is_people_led(_IDEAS[0]) and not is_people_led(_IDEAS[1])

    def test_parse_keeps_at_most_three_surviving_ideas(self):
        payload = dict(_PAYLOAD, visual_ideas=["A founder typing on a laptop."] + _IDEAS + [
            "A fourth idea about an agency owner."])
        concept = parse_concept(payload, _SOURCE)
        assert concept.visual_ideas == tuple(_IDEAS)

    def test_the_ranking_call_picks_the_winner_and_keeps_the_rest(self):
        concept = parse_concept(dict(_PAYLOAD, visual_ideas=_IDEAS), _SOURCE)
        reply = _resp({"ranking": [3, 1, 2], "reason": "most specific"})
        with patch(_CREATE, return_value=reply) as create:
            picked = pick_visual_idea(concept, "newsletter")
        assert picked.chosen_idea == _IDEAS[2]
        assert picked.rejected_ideas == (_IDEAS[0], _IDEAS[1])
        assert picked.idea_pick_reason == "most specific"
        kwargs = create.call_args[1]
        assert kwargs["model"] == "lem-simple"
        text = kwargs["messages"][0]["content"]
        assert "surprise" in text and "400x225" in text and "1. A copy editor" in text

    @pytest.mark.parametrize("reply", [RuntimeError("down"), _resp("not json"),
                                       _resp({"ranking": [9, "x"]})])
    def test_an_unusable_ranking_fails_open_to_the_first_people_led_idea(self, reply):
        concept = parse_concept(dict(_PAYLOAD, visual_ideas=[_IDEAS[1], _IDEAS[0]]), _SOURCE)
        kwargs = {"side_effect": reply} if isinstance(reply, Exception) else {"return_value": reply}
        with patch(_CREATE, **kwargs):
            picked = pick_visual_idea(concept)
        assert picked.chosen_idea == _IDEAS[0] and picked.rejected_ideas == (_IDEAS[1],)

    def test_a_single_idea_needs_no_ranking_call(self):
        concept = parse_concept(dict(_PAYLOAD, visual_ideas=[_IDEAS[1]]), _SOURCE)
        with patch(_CREATE) as create:
            picked = pick_visual_idea(concept)
        create.assert_not_called()
        assert picked.chosen_idea == _IDEAS[1] and picked.rejected_ideas == ()

    def test_no_ideas_leaves_the_concept_unchanged(self):
        concept = parse_concept(_PAYLOAD, _SOURCE)
        assert pick_visual_idea(concept) is concept

    def test_analyze_runs_the_pick_and_the_prompt_asks_for_people_led_ideas(self):
        payload = dict(_PAYLOAD, visual_ideas=_IDEAS)
        with patch(_CREATE, side_effect=[_resp(payload),
                                         _resp({"ranking": [1], "reason": "r"})]) as create:
            concept = analyze_content_for_image(_SOURCE, surface="newsletter")
        assert create.call_count == 2
        assert concept.chosen_idea == _IDEAS[0]
        system = create.call_args_list[0][1]["messages"][0]["content"]
        assert "exactly 3 one-sentence ideas" in system and "At least ONE idea is people-led" in system
        user = create.call_args_list[0][1]["messages"][1]["content"]
        assert "hook_phrase is required" in user

    def test_a_shouted_hook_falls_through_to_a_valid_alternative(self):
        payload = dict(_PAYLOAD, hook_phrase="Stop wasting money!",
                       hook_alternatives=["Fix it now!", "Who covers your payroll?"])
        assert parse_concept(payload, _SOURCE).hook_phrase == "Who covers your payroll?"


class TestRoundFourStageOne:
    """Round 4 of #2241: budgets reasoning models finish inside, gist theses, cleaner hooks."""

    def test_the_budget_fits_a_reasoning_model_and_asks_for_low_effort(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE)
        kwargs = create.call_args[1]
        assert kwargs["max_tokens"] >= 6000 and kwargs["reasoning_effort"] == "low"

    def test_a_length_cut_is_retried_once(self):
        cut = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch(_CREATE, side_effect=[cut, _resp(_PAYLOAD)]) as create:
            concept = analyze_content_for_image(_SOURCE)
        assert create.call_count == 2 and concept is not None

    def test_two_length_cuts_give_up(self):
        cut = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch(_CREATE, side_effect=[cut, cut]) as create:
            assert analyze_content_for_image(_SOURCE) is None
        assert create.call_count == 2

    def test_the_ranker_also_gets_the_budget_and_low_effort(self):
        concept = parse_concept(dict(_PAYLOAD, visual_ideas=_IDEAS), _SOURCE)
        with patch(_CREATE, return_value=_resp({"ranking": [1]})) as create:
            pick_visual_idea(concept)
        assert create.call_args[1]["max_tokens"] >= 6000
        assert create.call_args[1]["reasoning_effort"] == "low"

    @pytest.mark.parametrize("hook", ["When AI Misses the Mark", "Hidden Spend Gets Found"])
    def test_title_case_hooks_are_refused(self, hook):
        assert parse_concept(dict(_PAYLOAD, hook_phrase=hook), _SOURCE).hook_phrase == ""

    @pytest.mark.parametrize("hook", ["45% less engagement on AI posts",
                                      "Who covers your payroll?", "90 days of unpaid invoices"])
    def test_sentence_case_and_numbers_are_welcome(self, hook):
        assert parse_concept(dict(_PAYLOAD, hook_phrase=hook), _NUM_SOURCE).hook_phrase == hook

    def test_a_hook_sharing_most_of_the_title_is_refused(self):
        title = "Unpaid invoices and the payroll run"
        assert parse_concept(dict(_PAYLOAD, hook_phrase="Unpaid invoices, payroll run"),
                             _SOURCE, title=title).hook_phrase == ""
        assert parse_concept(dict(_PAYLOAD, hook_phrase="Who covers your payroll?"), _SOURCE,
                             title=title).hook_phrase == "Who covers your payroll?"

    def test_the_thesis_is_one_gist_claim(self):
        from cqc_lem.utilities.ai.image_concept import gist_thesis
        long = ("Companies waste tens of thousands on redundant AI tools; a quick audit can cut "
                "costs, improve compliance, and boost lead quality.")
        assert gist_thesis(long) == "Companies waste tens of thousands on redundant AI tools"
        assert len(gist_thesis(" ".join(["word"] * 40)).split()) == 20
        concept = parse_concept(dict(_PAYLOAD, thesis=long), _SOURCE)
        assert concept.thesis == "Companies waste tens of thousands on redundant AI tools"

    def test_the_prompt_asks_for_one_short_claim_and_sentence_case(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE)
        system = create.call_args[1]["messages"][0]["content"]
        assert "ONE central claim, at most 20 words" in system
        assert "Sentence case, never Title Case" in system
        assert "comes from the article's OWN words" in system

    @pytest.mark.parametrize("idea", [
        "A colleague points to a handwritten note urging personal storytelling.",
        "An engineer smiling at a printed billing dashboard on the table.",
        "A founder signing a paper check with relief.",
    ])
    def test_ideas_asking_for_legible_documents_are_filtered(self, idea):
        from cqc_lem.utilities.ai.image_concept import idea_rejection
        assert "legible words on a surface" in idea_rejection(idea, ())


class TestRoundFiveHookNamesItsSubject:
    """Round 5 of #2241: "45% less engagement" says how much, never of what."""

    @pytest.mark.parametrize("hook", ["45% less engagement", "The 95% you're missing",
                                      "$30K wasted", "53.7% miss the mark"])
    def test_a_number_with_only_generic_words_is_refused(self, hook):
        assert parse_concept(dict(_PAYLOAD, hook_phrase=hook), _SOURCE).hook_phrase == ""

    @pytest.mark.parametrize("hook", ["45% less engagement on AI posts",
                                      "30% of AI answers wrong", "$30K wasted on AI",
                                      "45% fewer invoices paid"])
    def test_a_number_with_its_subject_passes(self, hook):
        assert parse_concept(dict(_PAYLOAD, hook_phrase=hook), _NUM_SOURCE).hook_phrase == hook

    def test_a_subjectless_number_falls_through_to_an_alternative(self):
        payload = dict(_PAYLOAD, hook_phrase="45% less engagement",
                       hook_alternatives=["45% less engagement on AI posts"])
        assert parse_concept(payload, _NUM_SOURCE).hook_phrase == \
            "45% less engagement on AI posts"

    def test_the_prompt_asks_numeric_hooks_to_name_the_subject(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE)
        system = create.call_args[1]["messages"][0]["content"]
        assert "A numeric hook names its subject" in system
        assert '"45% less engagement on AI posts"' in system


class TestPostImagesFollowTheCoverRecipe:
    """Round 5 (post gauntlet): five dark, moody, hookless post renders; a metal box on a desk."""

    def test_post_guidance_requires_a_hook_and_prefers_people(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE, surface="post_image")
        user = create.call_args[1]["messages"][1]["content"]
        assert "LinkedIn feed POST image (4:5). Prefer people_scene" in user
        assert "hook_phrase is required" in user and "never an object sitting on a desk" in user

    def test_the_ranker_is_told_to_penalise_desk_still_lifes(self):
        concept = parse_concept(dict(_PAYLOAD, visual_ideas=_IDEAS), _SOURCE)
        with patch(_CREATE, return_value=_resp({"ranking": [1]})) as create:
            pick_visual_idea(concept)
        assert "Penalise an object sitting on a desk or table with no person" in \
            create.call_args[1]["messages"][0]["content"]

    def test_a_desk_still_life_ranked_first_is_demoted_for_a_people_led_idea(self):
        still = "A dented metal cash box sitting on a cluttered desk under a lamp."
        concept = parse_concept(dict(_PAYLOAD, visual_ideas=[still, _IDEAS[2]]), _SOURCE)
        with patch(_CREATE, return_value=_resp({"ranking": [1, 2], "reason": "r"})):
            picked = pick_visual_idea(concept)
        assert picked.chosen_idea == _IDEAS[2]
        assert "demoted" in picked.idea_pick_reason

    def test_a_striking_object_idea_with_no_people_alternative_stays(self):
        still = "A dented metal cash box sitting on a cluttered desk under a lamp."
        other = "An empty office chair under a warm lamp beside a cold cup of tea."
        concept = parse_concept(dict(_PAYLOAD, visual_ideas=[still, other]), _SOURCE)
        with patch(_CREATE, return_value=_resp({"ranking": [1, 2]})):
            assert pick_visual_idea(concept).chosen_idea == still


class TestSeriesRotation:
    """Round 6 of #2241: four covers, one composition, one kind of man."""

    def _concept(self, **overrides):
        return parse_concept(dict(_PAYLOAD, treatment="people_scene", **overrides), _SOURCE)

    def test_six_consecutive_covers_never_repeat_a_layout_within_three(self):
        from cqc_lem.utilities.ai.image_concept import assign_layout_and_cast
        layouts, casts = [], []
        for i in range(6):
            concept = self._concept(thesis=f"Late invoices starve payroll, part {i}")
            concept = assign_layout_and_cast(concept, "newsletter", layouts[:4], casts[:4])
            layouts.insert(0, concept.layout)
            casts.insert(0, concept.cast)
        for start in range(len(layouts) - 2):
            window = layouts[start:start + 3]
            assert len(set(window)) == 3, window
        assert len({(c["gender"], c["age"]) for c in casts}) >= 4, "the cast varies"
        assert len({c["setting"] for c in casts}) >= 4

    def test_every_cast_dimension_rotates_least_recently_used(self):
        from cqc_lem.utilities.ai.image_concept import CAST_DIMENSIONS, assign_layout_and_cast
        recent = [{"gender": "man", "age": "30s", "ethnicity": "white", "setting": "office"}]
        cast = assign_layout_and_cast(self._concept(), "newsletter", [], recent).cast
        for dim, values in CAST_DIMENSIONS.items():
            assert cast[dim] != recent[0][dim] and cast[dim] in values

    def test_the_role_comes_from_the_anchors(self):
        from cqc_lem.utilities.ai.image_concept import assign_layout_and_cast, cast_phrase
        cast = assign_layout_and_cast(self._concept(), "newsletter").cast
        assert cast["role"] == "agency owner"
        phrase = cast_phrase(cast)
        assert ", an agency owner, " in phrase

    def test_without_history_the_rotation_is_stable_per_piece_but_varies_across(self):
        from cqc_lem.utilities.ai.image_concept import assign_layout_and_cast
        first = assign_layout_and_cast(self._concept(thesis="One claim"), "newsletter")
        again = assign_layout_and_cast(self._concept(thesis="One claim"), "newsletter")
        assert first.layout == again.layout and first.cast == again.cast
        others = {assign_layout_and_cast(self._concept(thesis=f"Claim {i}"), "newsletter").layout
                  for i in range(12)}
        assert len(others) > 1

    def test_no_cast_off_a_people_scene_and_no_layout_off_covers_and_posts(self):
        from cqc_lem.utilities.ai.image_concept import assign_layout_and_cast
        concrete = assign_layout_and_cast(
            parse_concept(dict(_PAYLOAD, treatment="concrete_scene"), _SOURCE), "newsletter")
        assert concrete.cast is None and concrete.layout
        carousel = assign_layout_and_cast(self._concept(), "carousel")
        assert carousel.layout == "" and carousel.cast is None

    def test_posts_use_the_top_band(self):
        from cqc_lem.utilities.ai.image_concept import assign_layout_and_cast
        assert assign_layout_and_cast(self._concept(), "post_image").layout == "band_top"

    def test_analyze_takes_the_recent_history(self):
        with patch(_CREATE, return_value=_resp(dict(_PAYLOAD, treatment="people_scene"))):
            concept = analyze_content_for_image(
                _SOURCE, surface="newsletter",
                recent_layouts=["panel_left", "panel_right", "full_bleed"])
        assert concept.layout == "lower_third_band"


class TestHookFidelityAndShape:
    """Round 6: the critic caught "reach" for "engagement", "influencers" for "hidden buyers"."""

    _ARTICLE = ("Hidden buyers decide most AI purchases. AI posts got 45% less engagement than "
                "human ones, and teams that ignore hidden buyers lose deals.")

    @pytest.mark.parametrize("hook", ["45% less reach on AI posts",
                                      "Influencers decide your deals",
                                      "52% less engagement on AI posts"])
    def test_a_word_or_number_the_piece_never_says_is_refused(self, hook):
        from cqc_lem.utilities.ai.image_concept import hook_is_faithful
        assert not hook_is_faithful(hook, self._ARTICLE)

    @pytest.mark.parametrize("hook", ["45% less engagement on AI posts",
                                      "Who really buys your AI?", "Hidden buyers cost you deals",
                                      "Search on, still wrong"])
    def test_the_pieces_own_words_pass(self, hook):
        from cqc_lem.utilities.ai.image_concept import hook_is_faithful
        source = self._ARTICLE + " Search was on and the answers were still wrong."
        assert hook_is_faithful(hook, source)

    @pytest.mark.parametrize("hook,shape", [("Who really buys your AI?", "question"),
                                            ("45% less engagement on AI posts", "number_claim"),
                                            ("Search on, still wrong", "contrast"),
                                            ("Your cheapest model is enough", "plain_claim")])
    def test_shapes(self, hook, shape):
        from cqc_lem.utilities.ai.image_concept import hook_shape_of
        assert hook_shape_of(hook) == shape

    def test_the_shape_rotates_least_recently_used(self):
        from cqc_lem.utilities.ai.image_concept import rotate_hook_shape
        payload = dict(_PAYLOAD, hook_phrase="90 days of unpaid invoices", hook_candidates={
            "number_claim": "90 days of unpaid invoices",
            "question": "Who covers your payroll?",
            "plain_claim": "Unpaid invoices cost payroll"})
        concept = parse_concept(payload, _SOURCE)
        assert set(concept.hook_options) == {"number_claim", "question", "plain_claim"}
        rotated = rotate_hook_shape(concept, ["number_claim", "question"])
        assert rotated.hook_shape == "plain_claim"
        assert rotated.hook_phrase == "Unpaid invoices cost payroll"

    def test_six_words_now_fit(self):
        hook = "90 days of unpaid invoices, payroll"
        assert parse_concept(dict(_PAYLOAD, hook_phrase=hook), _SOURCE).hook_phrase == hook

    def test_the_prompt_asks_for_every_shape_and_the_articles_own_words(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE)
        system = create.call_args[1]["messages"][0]["content"]
        assert "Give one hook in EACH shape in hook_candidates" in system
        assert '"hidden buyers" never becomes "influencers"' in system


class TestValence:
    @pytest.mark.parametrize("raw,expected", [("positive", "positive"), ("Negative", "negative"),
                                              ("weird", "mixed"), (None, "mixed")])
    def test_valence_is_parsed(self, raw, expected):
        assert parse_concept(dict(_PAYLOAD, valence=raw), _SOURCE).valence == expected

    def test_the_prompt_ties_valence_to_the_piece(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE)
        assert "positive when the piece is about a saving, a win or relief" in \
            create.call_args[1]["messages"][0]["content"]



class TestRoundSevenStageOne:
    @pytest.mark.parametrize("raw,expected", [
        ("agency payroll", "AGENCY PAYROLL"), ("Payroll", "PAYROLL"), ("AI content audit", ""),
        ("LLM costs", ""), ("UNPAID INVOICES", "UNPAID INVOICES"),
        ("collections", "COLLECTIONS"), ("way too many words here", ""),
        ("hidden influencers", ""), ("", ""),
    ])
    def test_the_kicker_is_grounded_in_the_source(self, raw, expected):
        from cqc_lem.utilities.ai.image_concept import valid_kicker
        assert valid_kicker(raw, _SOURCE) == expected

    def test_acronyms_always_pass_and_hyphens_split(self):
        from cqc_lem.utilities.ai.image_concept import valid_kicker
        assert valid_kicker("B2B buying", "Buyers keep buying.") == "B2B BUYING"
        assert valid_kicker("AI fact-checking", "We fact checked every claim.") == \
            "AI FACT-CHECKING"

    def test_the_kicker_lands_on_the_concept(self):
        concept = parse_concept(dict(_PAYLOAD, kicker="Agency payroll"), _SOURCE)
        assert concept.kicker == "AGENCY PAYROLL"

    def test_the_prompt_asks_for_a_kicker(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE)
        assert "Rules for kicker" in create.call_args[1]["messages"][0]["content"]

    def test_hooks_come_back_sentence_cased(self):
        concept = parse_concept(dict(_PAYLOAD, hook_phrase="payroll first, clients last"), _SOURCE)
        assert concept.hook_phrase == "Payroll first, clients last"

    def test_full_bleed_is_out_of_the_cover_rotation(self):
        from cqc_lem.utilities.ai.image_concept import COVER_LAYOUTS
        assert "full_bleed" not in COVER_LAYOUTS
        assert set(COVER_LAYOUTS) == {"panel_left", "panel_right", "lower_third_band"}

    @pytest.mark.parametrize("idea", ["A founder holding a proposal on a clipboard.",
                                      "An analyst frowning at code on a monitor.",
                                      "A manager signing a form at her desk."])
    def test_prop_ideas_are_filtered(self, idea):
        from cqc_lem.utilities.ai.image_concept import idea_rejection
        assert "legible words on a surface" in idea_rejection(idea, ())
