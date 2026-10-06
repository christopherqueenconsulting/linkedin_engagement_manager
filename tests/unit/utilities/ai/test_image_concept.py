"""Stage 1 of the image engine: the whole piece in, a grounded ImageConcept out (issue #2241)."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai.image_concept import (
    TREATMENTS,
    ImageConcept,
    analyze_content_for_image,
    entity_mentioned,
    parse_concept,
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
            "emotional_beat": "quiet dread", "hook_phrase": "90 days unpaid",
            "treatment": "concrete_scene", "treatment_rationale": "a tangible situation"}


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

    def test_fewer_than_two_surviving_entities_is_weak_but_usable(self):
        payload = dict(_PAYLOAD, specific_entities=["unpaid invoices", "a lighthouse"])
        concept = parse_concept(payload, _SOURCE)
        assert concept is not None and concept.weak

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
                       specific_entities=["unpaid invoices"])
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
