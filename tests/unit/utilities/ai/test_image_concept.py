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


def _engine_calls(create) -> list:
    """Every LLM call except round 12's hook-vs-thesis judge (a separate lem-simple check)."""
    return [c for c in create.call_args_list
            if "Does the headline ASSERT" not in str(c[1].get("messages"))]


def _stage1(create) -> dict:
    """The kwargs of Stage 1's own lem-medium analysis call."""
    return next(c[1] for c in create.call_args_list if c[1].get("model") == "lem-medium")

_SOURCE = ("Our agency nearly missed payroll in March. Three clients sat on unpaid invoices for "
           "90 days while the payroll run came due, and the agency owner covered it from a "
           "personal credit card. Here is the collections process we built afterwards.")

_PAYLOAD = {"thesis": "Late invoices quietly starve a small agency's payroll",
            "audience": "agency owners",
            "specific_entities": ["unpaid invoices", "payroll run", "agency owner",
                                  "personal credit card"],
            "visual_anchors": ["an agency owner", "three late-paying clients",
                               "a tense payroll day"],
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
        kwargs = _stage1(create)
        assert len(_engine_calls(create)) == 1
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
        system = _stage1(create)["messages"][0]["content"]
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
        # The checklist is a prop the brief refuses to draw (round 8), so it is never an anchor.
        assert concept.visual_anchors == ("a marketing lead", "a cluttered meeting room")
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
        system = _stage1(create)["messages"][0]["content"]
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
        user = _stage1(create)["messages"][1]["content"]
        assert "most recent first: concrete_scene, people_scene" in user
        assert "Prefer a different treatment than concrete_scene" in user
        assert "Prefer people_scene" in user and "real faces beat clipart" in user

    def test_other_surfaces_get_no_cover_bias(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE, surface="post_image")
        assert "COVER" not in _stage1(create)["messages"][1]["content"]


_IDEAS = [
    "A copy editor's red pen frozen mid-strike, her face a half-smile of disbelief.",
    "An empty office chair under a warm lamp beside a dark window.",
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
        assert len(_engine_calls(create)) == 2
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
        kwargs = _stage1(create)
        assert kwargs["max_tokens"] >= 6000 and kwargs["reasoning_effort"] == "low"

    def test_a_length_cut_is_retried_once(self):
        cut = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch(_CREATE, side_effect=[cut, _resp(_PAYLOAD)]) as create:
            concept = analyze_content_for_image(_SOURCE)
        assert len(_engine_calls(create)) == 2 and concept is not None

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
        system = _stage1(create)["messages"][0]["content"]
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
        system = _stage1(create)["messages"][0]["content"]
        assert "A numeric hook names its subject" in system
        assert '"45% less engagement on AI posts"' in system


class TestPostImagesFollowTheCoverRecipe:
    """Round 5 (post gauntlet): five dark, moody, hookless post renders; a metal box on a desk."""

    def test_post_guidance_requires_a_hook_and_prefers_people(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)) as create:
            analyze_content_for_image(_SOURCE, surface="post_image")
        user = _stage1(create)["messages"][1]["content"]
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
        # Two split layouts: they alternate, so no layout ever repeats back to back.
        assert all(a != b for a, b in zip(layouts, layouts[1:])), layouts
        assert len({(c["gender"], c["age"]) for c in casts}) >= 4, "the cast varies"
        # Round 12: the setting is the ARTICLE's, never rotated.
        assert {c["setting"] for c in casts} == {""}

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
        assert phrase.endswith(", an agency owner")

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
        assert assign_layout_and_cast(self._concept(), "post_image").layout in (
            "split_top", "split_bottom")

    def test_analyze_takes_the_recent_history(self):
        with patch(_CREATE, return_value=_resp(dict(_PAYLOAD, treatment="people_scene"))):
            concept = analyze_content_for_image(
                _SOURCE, surface="newsletter",
                recent_layouts=["split_left"])
        assert concept.layout == "split_right"


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
        system = _stage1(create)["messages"][0]["content"]
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
            _stage1(create)["messages"][0]["content"]



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
        assert "Rules for kicker" in _stage1(create)["messages"][0]["content"]

    def test_hooks_come_back_sentence_cased(self):
        concept = parse_concept(dict(_PAYLOAD, hook_phrase="payroll first, clients last"), _SOURCE)
        assert concept.hook_phrase == "Payroll first, clients last"

    def test_full_bleed_is_out_of_the_cover_rotation(self):
        from cqc_lem.utilities.ai.image_concept import COVER_LAYOUTS
        assert "full_bleed" not in COVER_LAYOUTS
        # Round 8: only split layouts rotate — type and scene never overlap.
        assert set(COVER_LAYOUTS) == {"split_left", "split_right"}

    @pytest.mark.parametrize("idea", ["A founder holding a proposal on a clipboard.",
                                      "An analyst frowning at code on a monitor.",
                                      "A manager signing a form at her desk."])
    def test_prop_ideas_are_filtered(self, idea):
        from cqc_lem.utilities.ai.image_concept import idea_rejection
        assert "legible words on a surface" in idea_rejection(idea, ())


@pytest.mark.unit
class TestRoundEightAnchors:
    def test_an_anchor_the_brief_would_refuse_is_dropped_at_parse_time(self):
        from cqc_lem.utilities.ai.image_concept import _ground_anchors
        source = ("A marketing lead walks the warehouse floor with a laptop and a printed "
                  "checklist before the audit.")
        anchors = _ground_anchors(["a marketing lead", "a laptop", "a printed checklist",
                                   "the warehouse floor"], source, ())
        assert "a laptop" not in anchors and "a printed checklist" not in anchors
        assert "a marketing lead" in anchors and "the warehouse floor" in anchors


@pytest.mark.unit
class TestRoundNineKickerAndHooks:
    def test_a_failed_kicker_is_derived_title_first(self):
        from cqc_lem.utilities.ai.image_concept import derive_kicker
        source = ("Verification catches errors. Verification costs little. Teams that verify "
                  "their AI output ship fewer errors.")
        assert derive_kicker(source, "Why AI verification pays") == "AI VERIFICATION"

    def test_a_derived_kicker_is_never_empty(self):
        from cqc_lem.utilities.ai.image_concept import derive_kicker
        assert derive_kicker("", None) == "INSIGHT"
        assert derive_kicker("the and of", "") == "INSIGHT"

    def test_a_derived_kicker_keeps_a_lone_word_when_nothing_else_recurs(self):
        from cqc_lem.utilities.ai.image_concept import derive_kicker
        assert derive_kicker("Pricing pricing pricing once twice", None) == "PRICING"

    def test_parse_concept_always_yields_a_kicker(self):
        from cqc_lem.utilities.ai.image_concept import parse_concept
        source = "Content audits find waste. A content audit pays for itself in a month."
        concept = parse_concept({"thesis": "Content audits pay for themselves",
                                 "kicker": "QUANTUM FINANCE"}, source, title="Content audits")
        assert concept is not None and concept.kicker == "CONTENT AUDITS"

    def test_concept_kicker_derives_for_a_caller_concept(self):
        from cqc_lem.utilities.ai.image_concept import ImageConcept, concept_kicker
        concept = ImageConcept(thesis="Payroll audits catch payroll errors", audience="",
                               specific_entities=(), emotional_beat="", hook_phrase="Who pays?",
                               treatment="people_scene", treatment_rationale="", weak=False)
        assert concept_kicker(concept) == "PAYROLL"
        assert concept_kicker(None) == ""

    @pytest.mark.parametrize("hook,vague", [("Verification is much cheaper", "cheaper"),
                                            ("Audits work better", "better"),
                                            ("Fewer meetings now", "fewer"),
                                            ("45% less engagement", None),
                                            ("Cheaper than a hire", None),
                                            ("Hidden buyers cost you deals", None)])
    def test_a_comparative_needs_its_reference(self, hook, vague):
        from cqc_lem.utilities.ai.image_concept import vague_comparative
        assert vague_comparative(hook) == vague

    def test_a_vague_comparative_hook_is_refused(self):
        from cqc_lem.utilities.ai.image_concept import _valid_hook
        source = "verification is much cheaper than a rework"
        assert _valid_hook("Verification is much cheaper", None, source, source) == ""
        assert _valid_hook("Verification is cheaper than rework", None, source, source)


@pytest.mark.unit
class TestRoundTenHookAlways:
    _SOURCE = ("An audit exposed hidden AI tool spend, saving $30K quarterly. The billing review "
               "found unused seats. Audits are much cheaper.")

    def test_a_thesis_is_trimmed_at_its_clause_boundary(self):
        from cqc_lem.utilities.ai.image_concept import derive_hook
        hook = derive_hook("An audit exposed hidden AI tool spend, saving $30K quarterly.",
                           self._SOURCE)
        assert hook == "Audit exposed hidden AI tool spend"

    def test_a_number_and_its_subject_when_the_thesis_cannot_be_a_hook(self):
        from cqc_lem.utilities.ai.image_concept import derive_hook
        # Round 14: the number and its subject share ONE source sentence, or it is never paired.
        source = "The billing review found $30K in unused seats."
        hook = derive_hook("Stop paying!", source, facts=("$30K",),
                           anchors=("the billing review",))
        assert hook == "$30K billing review"
        apart = "Unused seats cost $30K. The billing review found them."
        assert "$30K" not in derive_hook("Stop paying!", apart, facts=("$30K",),
                                         anchors=("the billing review",))

    def test_a_derived_hook_is_never_empty(self):
        from cqc_lem.utilities.ai.image_concept import derive_hook
        assert derive_hook("", "", None)

    def test_hook_rejection_names_the_reason(self):
        from cqc_lem.utilities.ai.image_concept import hook_rejection
        assert "than" in hook_rejection("Verification is much cheaper", None)
        assert hook_rejection("Who buys the audit?", None) == ""

    def _stage1(self, retry_reply):
        payload = {"thesis": "An audit exposed hidden AI tool spend, saving $30K quarterly",
                   "visual_anchors": ["billing review", "unused seats"],
                   "hook_phrase": "Audits are much cheaper", "treatment": "people_scene"}
        replies = [SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop")])]
        if isinstance(retry_reply, Exception):
            replies.append(retry_reply)
        else:
            replies.append(SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(retry_reply)), finish_reason="stop")]))
        replies += [SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="{}"), finish_reason="stop")])] * 3
        return replies

    def test_a_post_whose_hook_failed_is_retried_once_with_the_reason(self):
        from cqc_lem.utilities.ai.image_concept import analyze_content_for_image
        with patch("cqc_lem.utilities.ai.client.client") as client:
            client.chat.completions.create.side_effect = self._stage1(
                {"hook_phrase": "Audit saved $30K quarterly"})
            concept = analyze_content_for_image(self._SOURCE, surface="post_image")
        assert concept.hook_phrase == "Audit saved $30K quarterly"
        retry = client.chat.completions.create.call_args_list[1][1]["messages"][1]["content"]
        assert "Audits are much cheaper" in retry and "needs its reference" in retry

    def test_a_failed_retry_derives_the_hook(self):
        from cqc_lem.utilities.ai.image_concept import analyze_content_for_image
        with patch("cqc_lem.utilities.ai.client.client") as client:
            client.chat.completions.create.side_effect = self._stage1(RuntimeError("down"))
            concept = analyze_content_for_image(self._SOURCE, surface="post_image")
        # Derived from the thesis. Round 12's lead number ($30K) cannot be paired with "billing
        # review": they never share a source sentence (round 14), so the number is dropped.
        assert concept.hook_phrase == "Audit exposed hidden AI tool spend"

    def test_a_carousel_never_pays_for_a_hook_retry(self):
        from cqc_lem.utilities.ai.image_concept import analyze_content_for_image
        with patch("cqc_lem.utilities.ai.client.client") as client:
            client.chat.completions.create.side_effect = self._stage1({"hook_phrase": "x"})
            concept = analyze_content_for_image(self._SOURCE, surface="carousel")
        assert concept.hook_phrase == ""
        prompts = [c[1]["messages"][1]["content"] for c in
                   client.chat.completions.create.call_args_list]
        assert not any("Every hook you offered" in p for p in prompts)


@pytest.mark.unit
class TestRoundElevenTechHardware:
    @pytest.mark.parametrize("anchor", ["smaller model server", "a rack of GPUs",
                                        "network cables", "a laptop showing code",
                                        "the data center floor", "a terminal window"])
    def test_tech_hardware_anchors_are_dropped(self, anchor):
        from cqc_lem.utilities.ai.image_concept import _ground_anchors
        source = f"The team met beside {anchor} on the ops floor with the ops lead."
        anchors = _ground_anchors([anchor, "the ops lead"], source, ())
        assert anchor not in anchors and "the ops lead" in anchors

    def test_usable_anchors_filters_a_caller_concept_too(self):
        from cqc_lem.utilities.ai.image_brief import usable_anchors
        concept = ImageConcept(thesis="Smaller models win", audience="", specific_entities=(),
                               emotional_beat="", hook_phrase="", treatment="people_scene",
                               treatment_rationale="", weak=False,
                               visual_anchors=("smaller model server", "an ops lead"))
        assert usable_anchors(concept) == ["an ops lead"]

    def test_the_cliche_checks_still_apply(self):
        from cqc_lem.utilities.ai.image_brief import cliche_hit
        assert cliche_hit("a server room")


_R12_SOURCE = ("Our AI tool bill hit $30K before anyone looked. At a billing review late on a "
               "Tuesday, the engineering lead found seats nobody used. Routing cut spend by 85% "
               "with the same quality. One caveat: cheap-first routing hurts precision on legal "
               "text.")


@pytest.mark.unit
class TestRoundTwelveSetting:
    def test_the_setting_comes_from_the_article(self):
        concept = parse_concept(dict(_PAYLOAD, setting="a billing review late on a Tuesday"),
                                _R12_SOURCE)
        assert concept.setting == "a billing review late on a Tuesday"

    @pytest.mark.parametrize("setting", ["a warehouse floor", "a server room", "Acme HQ lobby",
                                         "a room full of laptops", ""])
    def test_an_ungrounded_or_unusable_setting_is_dropped(self, setting):
        concept = parse_concept(dict(_PAYLOAD, setting=setting), _R12_SOURCE)
        assert concept.setting == ""

    def test_the_cast_rotates_only_people_and_carries_the_articles_setting(self):
        from cqc_lem.utilities.ai.image_concept import (
            CAST_DIMENSIONS,
            assign_layout_and_cast,
            cast_phrase,
        )
        assert set(CAST_DIMENSIONS) == {"gender", "age", "ethnicity"}
        concept = parse_concept(dict(_PAYLOAD, treatment="people_scene",
                                     setting="a billing review late on a Tuesday"), _R12_SOURCE)
        cast = assign_layout_and_cast(concept, "newsletter").cast
        assert cast["setting"] == "a billing review late on a Tuesday"
        assert cast_phrase(cast).endswith(", at a billing review late on a Tuesday")

    def test_the_prompt_asks_for_the_articles_setting(self):
        from cqc_lem.utilities.ai.image_concept import _SYSTEM_PROMPT
        assert '"setting"' in _SYSTEM_PROMPT and "Rules for setting" in _SYSTEM_PROMPT


@pytest.mark.unit
class TestRoundTwelveLeadNumber:
    def _concept(self, **overrides):
        fields = dict(thesis="Routing cut AI spend 85% with the same quality", audience="",
                      specific_entities=("85%", "$30K"), emotional_beat="relief",
                      hook_phrase="Who pays for idle seats?", treatment="people_scene",
                      treatment_rationale="", weak=False,
                      visual_anchors=("the engineering lead", "a billing review"),
                      hook_shape="question",
                      hook_options={"question": "Who pays for idle seats?",
                                    "number_claim": "85% less spend with routing"})
        fields.update(overrides)
        return ImageConcept(**fields)

    def test_a_thesis_number_is_found_in_the_claim_sentence(self):
        from cqc_lem.utilities.ai.image_concept import thesis_number
        assert thesis_number(self._concept(), _R12_SOURCE) == "85%"

    def test_a_number_only_in_the_tail_is_not_the_lead(self):
        from cqc_lem.utilities.ai.image_concept import thesis_number
        source = ("Routing is about matching the model to the job. It takes care. It takes "
                  "tests. Later, unrelated: 85% of teams skip it.")
        concept = self._concept(thesis="Routing matches the model to the job",
                                specific_entities=("85%",))
        assert thesis_number(concept, source) == ""

    def test_the_lead_number_overrides_shape_rotation(self):
        from cqc_lem.utilities.ai.image_concept import lead_with_number
        concept = lead_with_number(self._concept(), _R12_SOURCE)
        assert concept.hook_shape == "number_claim"
        assert concept.hook_phrase == "85% less spend with routing"

    def test_without_an_offered_number_hook_one_is_built(self):
        from cqc_lem.utilities.ai.image_concept import lead_with_number
        concept = lead_with_number(self._concept(hook_options={"question": "Who pays?"},
                                                 visual_anchors=("routing spend",)),
                                   _R12_SOURCE)
        assert concept.hook_phrase == "85% routing spend" and concept.hook_shape == "number_claim"

    def test_a_built_hook_never_pairs_the_number_with_another_sentences_noun(self):
        from cqc_lem.utilities.ai.image_concept import lead_with_number
        concept = self._concept(hook_options={"question": "Who pays?"})
        assert lead_with_number(concept, _R12_SOURCE).hook_phrase == "Who pays for idle seats?"

    def test_rotation_stays_in_charge_without_a_thesis_number(self):
        from cqc_lem.utilities.ai.image_concept import lead_with_number
        concept = self._concept(thesis="Routing matches the model to the job",
                                specific_entities=())
        assert lead_with_number(concept, "Routing matches the model to the job.") is concept


@pytest.mark.unit
class TestRoundTwelveHookAssertsThesis:
    @pytest.mark.parametrize("hook", ["Cheapest model everywhere vs hybrid routing",
                                      "Routing versus cheap-first", "Humans v. agents"])
    def test_a_versus_hook_is_refused(self, hook):
        from cqc_lem.utilities.ai.image_concept import hook_rejection
        assert "takes no side" in hook_rejection(hook, None)

    def test_a_caveat_hook_is_regenerated_and_valence_follows_the_shipped_hook(self):
        from cqc_lem.utilities.ai.image_concept import _hook_asserts_thesis
        concept = ImageConcept(thesis="Routing cut AI spend 85% with the same quality",
                               audience="", specific_entities=("85%",), emotional_beat="",
                               hook_phrase="Cheap-first hurts precision", treatment="people_scene",
                               treatment_rationale="", weak=False, valence="negative",
                               visual_anchors=("the engineering lead",))
        replies = [_resp({"asserts_thesis": False, "valence": "negative",
                          "reason": "it is the closing caveat"}),
                   _resp({"hook_phrase": "Routing cut spend by 85%"}),
                   _resp({"asserts_thesis": True, "valence": "positive", "reason": "ok"})]
        with patch(_CREATE, side_effect=replies) as create:
            out = _hook_asserts_thesis(concept, {}, _R12_SOURCE, None, "post_image", 1)
        assert out.hook_phrase == "Routing cut spend by 85%"
        assert out.valence == "positive"
        retry = create.call_args_list[1][1]["messages"][1]["content"]
        assert "Cheap-first hurts precision" in retry and "closing caveat" in retry

    def test_an_unreachable_judge_keeps_the_hook(self):
        from cqc_lem.utilities.ai.image_concept import _hook_asserts_thesis
        concept = ImageConcept(thesis="t", audience="", specific_entities=(),
                               emotional_beat="", hook_phrase="Who pays?",
                               treatment="people_scene", treatment_rationale="", weak=False)
        with patch(_CREATE, side_effect=RuntimeError("down")):
            assert _hook_asserts_thesis(concept, {}, "s", None, "post_image", 1) is concept


@pytest.mark.unit
class TestRoundThirteenStats:
    @pytest.mark.parametrize("text,stats", [
        ("Claude Opus 4.5 reviews every draft", []),
        ("GPT-5.2 is the default model", []),
        ("In 2025 we rebuilt the stack", []),
        ("We saved $30K a quarter", ["$30K"]),
        ("Routing cut AI spend by 60%", ["60%"]),
        ("Replies came 3x faster", ["3x"]),
        ("We wrote 45 posts a month", ["45"]),
        ("Saved 30% in March", ["30%"]),
        ("12 hours lost every week", ["12"]),
    ])
    def test_only_stats_are_stats(self, text, stats):
        from cqc_lem.utilities.ai.image_concept import stat_numbers
        assert stat_numbers(text) == stats

    def test_ed17_never_leads_with_a_model_version(self):
        from cqc_lem.utilities.ai.image_concept import thesis_number
        source = ("Claude Opus 4.5 now reviews our content. A content reviewer still catches "
                  "what it misses.")
        concept = ImageConcept(thesis="Claude Opus 4.5 still needs a content reviewer",
                               audience="", specific_entities=("Claude Opus 4.5",),
                               emotional_beat="", hook_phrase="", treatment="people_scene",
                               treatment_rationale="", weak=False)
        assert thesis_number(concept, source) == ""

    def test_the_sources_casing_is_kept(self):
        from cqc_lem.utilities.ai.image_concept import thesis_number
        source = "An audit saved $30K a quarter. Nobody had looked at the bill."
        concept = ImageConcept(thesis="An audit saved $30k a quarter", audience="",
                               specific_entities=("$30k",), emotional_beat="", hook_phrase="",
                               treatment="people_scene", treatment_rationale="", weak=False)
        assert thesis_number(concept, source) == "$30K"


@pytest.mark.unit
class TestRoundThirteenStatSurvives:
    _SOURCE = ("Routing cut our AI spend by 60%. The engineering lead set it up in a week. "
               "Quality held.")

    def _concept(self, hook):
        return ImageConcept(thesis="Routing cut AI spend by 60%", audience="",
                            specific_entities=("60%",), emotional_beat="", hook_phrase=hook,
                            treatment="people_scene", treatment_rationale="", weak=False,
                            visual_anchors=("the engineering lead",),
                            hook_options={"plain_claim": hook})

    def test_a_hook_without_the_stat_is_regenerated_with_the_reason(self):
        from cqc_lem.utilities.ai.image_concept import _enforce_stat
        with patch(_CREATE, return_value=_resp({"hook_phrase": "Routing cut spend by 60%"})) as c:
            out = _enforce_stat(self._concept("Routing saves most spend"), {}, self._SOURCE,
                                None, "post_image", 1)
        assert out.hook_phrase == "Routing cut spend by 60%" and out.hook_shape == "number_claim"
        assert 'must contain the stat "60%" verbatim' in \
            c.call_args_list[0][1]["messages"][1]["content"]

    def test_a_second_miss_falls_back_to_the_stat_and_its_subject(self):
        from cqc_lem.utilities.ai.image_concept import _enforce_stat
        with patch(_CREATE, return_value=_resp({"hook_phrase": "Routing saves most spend"})):
            out = _enforce_stat(self._concept("Routing saves most spend"), {}, self._SOURCE,
                                None, "post_image", 1)
        assert "60%" in out.hook_phrase and out.hook_shape == "number_claim"

    def test_a_hook_already_carrying_the_stat_costs_no_call(self):
        from cqc_lem.utilities.ai.image_concept import _enforce_stat
        with patch(_CREATE) as create:
            out = _enforce_stat(self._concept("Routing cut spend by 60%"), {}, self._SOURCE,
                                None, "post_image", 1)
        assert out.hook_phrase == "Routing cut spend by 60%"
        create.assert_not_called()


@pytest.mark.unit
class TestRoundThirteenHookIsAClaim:
    @pytest.mark.parametrize("hook", ["Routing prompts to models by complexity",
                                      "Hybrid model routing strategy"])
    def test_a_label_is_refused(self, hook):
        from cqc_lem.utilities.ai.image_concept import hook_rejection
        assert "descriptive label" in hook_rejection(hook, None)

    @pytest.mark.parametrize("hook", ["Cheap-first hurts precision", "Payroll eats first",
                                      "Your cheapest model is enough", "Who buys?",
                                      "Audit exposed hidden tool spend", "60% less AI spend"])
    def test_a_claim_a_question_or_a_number_passes(self, hook):
        from cqc_lem.utilities.ai.image_concept import hook_rejection
        assert "descriptive label" not in hook_rejection(hook, None)


@pytest.mark.unit
class TestRoundThirteenShot:
    def test_shots_rotate_least_recently_used(self):
        from cqc_lem.utilities.ai.image_concept import SHOTS, assign_layout_and_cast
        recent: list[str] = []
        for i in range(len(SHOTS)):
            concept = parse_concept(dict(_PAYLOAD, treatment="people_scene",
                                         thesis=f"Late invoices starve payroll {i}"), _SOURCE)
            shot = assign_layout_and_cast(concept, "newsletter", recent_shots=recent).shot
            assert shot in SHOTS and shot not in recent
            recent.insert(0, shot)
        assert set(recent) == set(SHOTS)

    def test_the_shot_is_framing_only_and_the_setting_stays_the_articles(self):
        from cqc_lem.utilities.ai.image_concept import assign_layout_and_cast
        concept = parse_concept(dict(_PAYLOAD, treatment="people_scene",
                                     setting="a tense payroll day"), _SOURCE)
        out = assign_layout_and_cast(concept, "post_image")
        assert out.shot and out.cast["setting"] == "a tense payroll day"

    def test_a_non_people_scene_gets_no_shot(self):
        from cqc_lem.utilities.ai.image_concept import assign_layout_and_cast
        concept = parse_concept(dict(_PAYLOAD, treatment="concrete_scene"), _SOURCE)
        assert assign_layout_and_cast(concept, "newsletter").shot == ""


# ed18's exact title and stat sentences (round 14): 53.7% is a SHARE of posts; 45% is the gap.
_ED18_TITLE = "53.7% of LinkedIn Posts Miss Their Mark - Why"
_ED18_SOURCE = (_ED18_TITLE + "\n\n53.7% of long-form LinkedIn posts in 2025 were probably "
                "AI-generated. Yet they got about 45% less engagement than human-written content.")


@pytest.mark.unit
class TestRoundFourteenFidelity:
    def test_ed18_53_7_with_engagement_fails(self):
        from cqc_lem.utilities.ai.image_concept import hook_rejection, number_claim_mismatch
        hook = "53.7% less engagement than humans"
        assert "never share a source sentence" in number_claim_mismatch(hook, _ED18_SOURCE)
        assert hook_rejection(hook, None, "", _ED18_SOURCE)

    def test_ed18_45_with_engagement_passes(self):
        from cqc_lem.utilities.ai.image_concept import hook_rejection, number_claim_mismatch
        hook = "45% less engagement on AI posts"
        assert number_claim_mismatch(hook, _ED18_SOURCE) == ""
        assert hook_rejection(hook, _ED18_TITLE, "AI posts", _ED18_SOURCE) == ""

    def test_the_lead_stat_is_the_one_whose_sentence_makes_the_claim(self):
        from cqc_lem.utilities.ai.image_concept import thesis_number
        concept = ImageConcept(
            thesis="AI-generated posts get 45% less engagement than human-written ones",
            audience="", specific_entities=("53.7%", "45%"), emotional_beat="", hook_phrase="",
            treatment="people_scene", treatment_rationale="", weak=False)
        assert thesis_number(concept, _ED18_SOURCE, _ED18_TITLE) == "45%"

    @pytest.mark.parametrize("hook,noun", [("45% less engagement on AI posts", "engagement"),
                                           ("Routing cut spend by 60%", "spend"),
                                           ("3x faster replies", "replies")])
    def test_the_claim_noun(self, hook, noun):
        from cqc_lem.utilities.ai.image_concept import _HOOK_NUMBER, claim_noun
        assert claim_noun(hook, _HOOK_NUMBER.search(hook).group(0)) == noun

    def test_the_cited_sentence_is_the_claims(self):
        from cqc_lem.utilities.ai.image_concept import cited_sentence
        assert cited_sentence("45% less engagement on AI posts", _ED18_SOURCE).startswith(
            "Yet they got about 45% less engagement")

    def test_the_stat_fallback_never_pairs_falsely(self):
        from cqc_lem.utilities.ai.image_concept import _enforce_stat
        concept = ImageConcept(thesis="53.7% of LinkedIn posts were AI-generated", audience="",
                               specific_entities=("53.7%",), emotional_beat="",
                               hook_phrase="AI writes most long posts", treatment="people_scene",
                               treatment_rationale="", weak=False,
                               visual_anchors=("an engagement dashboard review",))
        with patch(_CREATE, side_effect=RuntimeError("down")):
            out = _enforce_stat(concept, {}, _ED18_SOURCE, _ED18_TITLE, "post_image", 1)
        assert "53.7%" not in out.hook_phrase or "engagement" not in out.hook_phrase


@pytest.mark.unit
class TestRoundFourteenJudgeAndCap:
    def _concept(self, hook):
        return ImageConcept(thesis="AI posts get 45% less engagement", audience="",
                            specific_entities=("45%",), emotional_beat="", hook_phrase=hook,
                            treatment="people_scene", treatment_rationale="", weak=False,
                            visual_anchors=("a content lead",))

    def test_the_judge_sees_the_cited_sentence_and_the_grammar_question(self):
        from cqc_lem.utilities.ai.image_concept import _hook_asserts_thesis
        with patch(_CREATE, return_value=_resp({"asserts_thesis": True,
                                                "grammatical_and_true": True,
                                                "valence": "negative"})) as create:
            out = _hook_asserts_thesis(self._concept("45% less engagement on AI posts"), {},
                                       _ED18_SOURCE, _ED18_TITLE, "newsletter", 1)
        text = create.call_args[1]["messages"][0]["content"]
        assert "grammatical English, at most 6 words, and literally true" in text
        assert "Yet they got about 45% less engagement than human-written content." in text
        assert out.valence == "negative"

    def test_untrue_twice_takes_the_deterministic_hook(self):
        from cqc_lem.utilities.ai.image_concept import _hook_asserts_thesis
        bad = _resp({"asserts_thesis": True, "grammatical_and_true": False, "valence": "mixed",
                     "reason": "ungrammatical"})
        regen = _resp({"hook_phrase": "Hybrid routing cuts 60% than cheapest"})
        with patch(_CREATE, side_effect=[bad, regen, bad]):
            out = _hook_asserts_thesis(self._concept("Hybrid routing cuts 60% than cheapest"),
                                       {}, _ED18_SOURCE, _ED18_TITLE, "post_image", 1)
        assert out.hook_phrase and "than cheapest" not in out.hook_phrase
        assert len(out.hook_phrase.split()) <= 6

    @pytest.mark.parametrize("hook", [
        "Web search does not guarantee AI-generated answers are right",
        "Ignoring AI's hidden buyers raises deal costs dramatically",
    ])
    def test_a_long_hook_becomes_a_complete_clause_never_a_cut(self, hook):
        # Round 15 replaced round 14's truncating cap: the fallback is built, never cut.
        from cqc_lem.utilities.ai.image_concept import asserts_something, fit_hook
        fitted = fit_hook(hook, hook)
        assert len(fitted.split()) <= 6 and len(fitted) <= 44
        assert asserts_something(fitted)
        assert not fitted.endswith((" deal", " AI-generated"))

    def test_analysis_never_ships_more_than_six_words(self):
        from cqc_lem.utilities.ai.image_concept import analyze_content_for_image
        payload = dict(_PAYLOAD, hook_phrase="Who covers your payroll?")
        long_regen = "Late invoices quietly starve a small agency payroll"
        replies = [_resp(payload),
                   _resp({"asserts_thesis": False, "reason": "caveat"}),
                   _resp({"hook_phrase": long_regen}),
                   _resp({"asserts_thesis": True, "grammatical_and_true": True})]
        with patch(_CREATE, side_effect=replies + [_resp({})] * 4):
            concept = analyze_content_for_image(_SOURCE, surface="post_image")
        assert len(concept.hook_phrase.split()) <= 6


@pytest.mark.unit
class TestRoundFourteenBriefCap:
    def test_a_callers_long_hook_is_capped_in_the_brief(self):
        from cqc_lem.utilities.ai.image_brief import build_image_brief
        concept = ImageConcept(thesis="t", audience="", specific_entities=(), emotional_beat="",
                               hook_phrase="Web search does not guarantee AI-generated answers",
                               treatment="people_scene", treatment_rationale="", weak=False)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("x")):
            brief = build_image_brief("c", surface="newsletter", concept=concept)
        assert len(brief.hook_text.split()) <= 6


# ed19's thesis: round 14's cap shipped "Ignoring AI's hidden buyers raises deal" — "costs" lost.
_ED19_THESIS = "Ignoring AI's hidden buyers raises deal costs dramatically"
_ED19_SOURCE = ("Ignoring AI's hidden buyers raises deal costs dramatically. Procurement officers "
                "and security leads now shape most AI deals.")


@pytest.mark.unit
class TestRoundFifteenNeverTruncate:
    def test_ed19_the_fallback_is_a_complete_six_word_clause(self):
        from cqc_lem.utilities.ai.image_concept import clause_hook, derive_hook
        assert clause_hook(_ED19_THESIS) == "Ignoring AI's hidden buyers raises costs"
        assert derive_hook(_ED19_THESIS, _ED19_SOURCE) == "Ignoring AI's hidden buyers raises costs"

    @pytest.mark.parametrize("thesis,expected", [
        ("Web search does not guarantee AI-generated answers are accurate",
         "Web search does not guarantee answers"),
        ("AI-generated posts get 45% less engagement than human-written ones",
         "AI-generated posts get 45% less engagement"),
        ("Late invoices quietly starve a small agency's payroll",
         "Late invoices starve small agency's payroll"),
    ])
    def test_the_clause_keeps_subject_verb_and_object(self, thesis, expected):
        from cqc_lem.utilities.ai.image_concept import clause_hook
        assert clause_hook(thesis) == expected

    def test_a_long_hook_is_regenerated_with_the_reason_then_built(self):
        from cqc_lem.utilities.ai.image_concept import _final_hook
        concept = ImageConcept(thesis=_ED19_THESIS, audience="", specific_entities=(),
                               emotional_beat="", hook_phrase=_ED19_THESIS,
                               treatment="people_scene", treatment_rationale="", weak=False)
        regen = _resp({"hook_phrase": "Hidden buyers shape most AI deals now, quietly"})
        with patch(_CREATE, return_value=regen) as create:
            out = _final_hook(concept, {}, _ED19_SOURCE, None, "newsletter", 1)
        assert "at most 6 words" in create.call_args_list[0][1]["messages"][1]["content"]
        assert out.hook_phrase == "Ignoring AI's hidden buyers raises costs"

    def test_a_regenerated_hook_that_fits_is_kept(self):
        from cqc_lem.utilities.ai.image_concept import _final_hook
        concept = ImageConcept(thesis=_ED19_THESIS, audience="", specific_entities=(),
                               emotional_beat="", hook_phrase=_ED19_THESIS,
                               treatment="people_scene", treatment_rationale="", weak=False)
        with patch(_CREATE, return_value=_resp({"hook_phrase": "Hidden buyers shape AI deals"})):
            out = _final_hook(concept, {}, _ED19_SOURCE, None, "newsletter", 1)
        assert out.hook_phrase == "Hidden buyers shape AI deals"


@pytest.mark.unit
class TestRoundFifteenNumberCasing:
    _ED16_SOURCE = ("An audit exposed hidden AI tool spend, saving $30K quarterly. Nobody had "
                    "looked at the bill.")

    def test_ed16_the_source_spelling_is_restored(self):
        from cqc_lem.utilities.ai.image_concept import restore_number_casing
        assert restore_number_casing("$30k saved quarterly", self._ED16_SOURCE) == \
            "$30K saved quarterly"

    def test_every_number_token_is_restored(self):
        from cqc_lem.utilities.ai.image_concept import restore_number_casing
        source = "53.7% of posts were AI-written; replies came 3x faster."
        assert restore_number_casing("53.7% of posts, 3X faster", source) == \
            "53.7% of posts, 3x faster"

    def test_the_final_hook_carries_the_source_spelling(self):
        from cqc_lem.utilities.ai.image_concept import _final_hook
        concept = ImageConcept(thesis="An audit saved $30K quarterly", audience="",
                               specific_entities=("$30K",), emotional_beat="",
                               hook_phrase="Audit saved $30k quarterly", treatment="people_scene",
                               treatment_rationale="", weak=False)
        with patch(_CREATE) as create:
            out = _final_hook(concept, {}, self._ED16_SOURCE, None, "newsletter", 1)
        assert out.hook_phrase == "Audit saved $30K quarterly"
        create.assert_not_called()


@pytest.mark.unit
class TestRoundFifteenNoLabelFallback:
    def test_a_label_fallback_gets_its_subject_and_the_theses_verb(self):
        from cqc_lem.utilities.ai.image_concept import asserts_something, clause_hook
        hook = clause_hook("Routing prompts to models by complexity cuts AI spend by 60%")
        assert hook == "Routing prompts cuts AI spend" and asserts_something(hook)

    def test_a_verbless_thesis_still_yields_a_claim(self):
        from cqc_lem.utilities.ai.image_concept import asserts_something, clause_hook
        hook = clause_hook("Routing prompts to models by complexity")
        assert asserts_something(hook) and len(hook.split()) <= 6

    def test_derive_hook_never_returns_a_label(self):
        from cqc_lem.utilities.ai.image_concept import asserts_something, derive_hook
        hook = derive_hook("Routing prompts to models by complexity",
                           "Routing prompts to models by complexity.")
        assert asserts_something(hook)

    def test_a_label_reaching_the_end_of_selection_is_replaced(self):
        from cqc_lem.utilities.ai.image_concept import _final_hook
        concept = ImageConcept(
            thesis="Routing prompts to models by complexity cuts AI spend by 60%", audience="",
            specific_entities=(), emotional_beat="",
            hook_phrase="Routing prompts by complexity", treatment="people_scene",
            treatment_rationale="", weak=False)
        out = _final_hook(concept, {}, "Routing prompts to models by complexity cuts AI spend.",
                          None, "post_image", 1)
        assert out.hook_phrase == "Routing prompts cuts AI spend"
