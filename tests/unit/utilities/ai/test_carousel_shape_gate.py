"""Unit tests for the carousel deck-shape gate (issue #1666).

A reply that parses but omits the `carousel` key — or a deck missing a required slide — used to
reach `model_cls(**carousel_dict)` and die there as a bare pydantic ValidationError ("3 validation
errors for EducationalContentCarousel – cover field required", 332 occurrences in production). The
shape is now read off the model where the LLM can still be asked again: ONE bounded repair call
naming the exact fields it owes, and a residual failure logs the fields instead of pydantic's own
message.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"

_GOOD_DECK = {
    "cover": {"title": "The 3 checks I run", "content": "The exact stack."},
    "contents": [
        {"title": "1. Pin the tag", "content": "Set IMAGE_TAG to the release tag, never latest."},
        {"title": "2. Migrate first", "content": "Run `flyway migrate` before the app flips."},
    ],
    "call_to_action": {"title": "Save this", "content": "Save it for your next deploy."},
}


def _response(payload: dict) -> MagicMock:
    message = MagicMock()
    message.content = json.dumps(payload)
    response = MagicMock()
    response.choices = [MagicMock(message=message)]
    return response


def _deck_response(deck, caption: str = "Here is the exact stack."):
    """A reply carrying `deck` under the "carousel" key. `deck=None` omits the key entirely."""
    payload = {"post_text": caption}
    if deck is not None:
        payload["carousel"] = deck
    return _response(payload)


class _Harness:
    """Everything `generate_carousel_content` reaches for that is not the shape gate."""

    def __enter__(self):
        self._patches = [
            patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
                  side_effect=RuntimeError("no creds")),
            patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None),
            patch(f"{_AI}._alignment_directive", return_value=""),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def _generate(responses, stage: str = "awareness"):
    from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
    with _Harness(), patch(f"{_AI}._call_llm", side_effect=responses) as call, \
            patch(f"{_AI}.log_error") as log_error:
        text, deck = generate_carousel_content(1, stage)
    return text, deck, call, log_error


class TestMissingCarouselFields:
    """The shared reader — every construction site asks the MODEL what it requires."""

    def test_an_empty_deck_is_missing_every_required_field(self):
        from cqc_lem.utilities.carousel_creator import (
            EducationalContentCarousel,
            missing_carousel_fields,
        )
        assert missing_carousel_fields(EducationalContentCarousel, {}) == [
            "cover", "contents", "call_to_action"]

    def test_a_complete_deck_is_missing_nothing(self):
        from cqc_lem.utilities.carousel_creator import (
            EducationalContentCarousel,
            missing_carousel_fields,
        )
        assert missing_carousel_fields(EducationalContentCarousel, _GOOD_DECK) == []

    @pytest.mark.parametrize("value", [None, {}, [], ""], ids=["null", "empty-obj", "empty-list",
                                                              "empty-str"])
    def test_an_empty_value_counts_as_missing(self, value):
        """An empty value is as unusable as an absent key.

        `contents: []` fails the conlist min_length as hard as a missing key, and a cover of {}
        renders a blank slide — both are the model's problem, not a shipped deck.
        """
        from cqc_lem.utilities.carousel_creator import (
            EducationalContentCarousel,
            missing_carousel_fields,
        )
        deck = dict(_GOOD_DECK, contents=value)
        assert missing_carousel_fields(EducationalContentCarousel, deck) == ["contents"]

    @pytest.mark.parametrize("carousel", [None, "not a deck", 7], ids=["none", "str", "int"])
    def test_a_non_dict_is_missing_everything(self, carousel):
        from cqc_lem.utilities.carousel_creator import (
            EducationalContentCarousel,
            missing_carousel_fields,
        )
        assert missing_carousel_fields(EducationalContentCarousel, carousel) == [
            "cover", "contents", "call_to_action"]

    def test_an_optional_field_is_never_required(self):
        """`CaseStudyCarousel.testimonial` is Optional — a deck without it is complete."""
        from cqc_lem.utilities.carousel_creator import CaseStudyCarousel, missing_carousel_fields
        slide = {"title": "t", "content": "c"}
        deck = {"cover": slide, "challenge": slide, "solution": slide, "results": slide,
                "call_to_action": slide}
        assert missing_carousel_fields(CaseStudyCarousel, deck) == []

    @pytest.mark.parametrize("model_name,slides_field", [
        ("EducationalContentCarousel", "contents"),
        ("CaseStudyCarousel", "challenge"),
        ("ProductDemoCarousel", "main_feature"),
        ("IndustryInsightsCarousel", "insights"),
    ])
    def test_every_generated_deck_type_requires_a_cover(self, model_name, slides_field):
        """The field this issue is named for, on each of the four models a stage maps to."""
        from cqc_lem.utilities import carousel_creator
        model_cls = getattr(carousel_creator, model_name)
        missing = carousel_creator.missing_carousel_fields(model_cls, {slides_field: [{}]})
        assert "cover" in missing


class TestTheShapeGateRepairsTheDeck:
    def test_a_reply_with_no_carousel_key_is_regenerated_and_the_repair_ships(self):
        _, deck, call, log_error = _generate([_deck_response(None), _deck_response(_GOOD_DECK)])
        assert call.call_count == 2
        assert deck == _GOOD_DECK
        assert log_error.call_count == 0

    def test_the_repair_prompt_names_the_missing_fields(self):
        _, _, call, _ = _generate([_deck_response(None), _deck_response(_GOOD_DECK)])
        retry_prompt = call.call_args_list[1][1]["messages"][1]["content"][0]["text"]
        assert "YOUR PREVIOUS RESPONSE WAS REJECTED" in retry_prompt
        for field in ("cover", "contents", "call_to_action"):
            assert field in retry_prompt
        # The schema is repeated so the retry has the full shape in hand, not just the field names.
        assert "EducationalContentCarousel with fields" in retry_prompt

    def test_a_partial_deck_is_repaired_too(self):
        """Not just the empty case: a deck that dropped only `cover` is the production error."""
        partial = {k: v for k, v in _GOOD_DECK.items() if k != "cover"}
        _, deck, call, log_error = _generate([_deck_response(partial),
                                              _deck_response(_GOOD_DECK)])
        assert call.call_count == 2
        assert deck == _GOOD_DECK
        assert log_error.call_count == 0

    def test_the_repaired_caption_travels_with_the_repaired_deck(self):
        """The repaired caption ships with the repaired deck.

        A reply with no deck also fell back to the generic caption, so the retry's text is the one
        that matches the slides that shipped.
        """
        text, deck, _, _ = _generate([
            _deck_response(None),
            _deck_response(_GOOD_DECK, caption="Pin the tag before you migrate."),
        ])
        assert deck == _GOOD_DECK
        assert "Pin the tag" in text

    def test_a_well_shaped_deck_costs_exactly_one_call(self):
        _, deck, call, log_error = _generate([_deck_response(_GOOD_DECK)])
        assert call.call_count == 1
        assert deck == _GOOD_DECK
        assert log_error.call_count == 0

    def test_a_stage_is_graded_against_its_own_model(self):
        """Each stage is graded against the model it builds.

        "decision" builds a ProductDemoCarousel, so the awareness deck's fields do not satisfy it —
        the gate must read the stage's model, not one shape for every deck.
        """
        slide = {"title": "Pin the tag", "content": "Set IMAGE_TAG to the release tag."}
        demo = {"cover": slide, "main_feature": slide, "additional_features": [slide],
                "call_to_action": slide}
        # A caption that promises nothing keeps the reference gate out of the call count.
        _, deck, call, _ = _generate([_deck_response(_GOOD_DECK, caption="Some thoughts."),
                                      _deck_response(demo, caption="Some thoughts.")],
                                     stage="decision")
        assert call.call_count == 2
        assert deck == demo


class TestTheRepairIsBoundedAndFailsLoudly:
    def test_a_second_bad_reply_ends_the_repair_and_logs_the_fields(self):
        _, deck, call, log_error = _generate([_deck_response(None), _deck_response(None)])
        assert call.call_count == 2          # ONE repair, never a loop
        assert deck == {}
        assert log_error.call_count == 1
        message = log_error.call_args[0][0]
        assert "missing required slide field(s)" in message
        assert "cover" in message
        kwargs = log_error.call_args.kwargs
        assert kwargs["user_id"] == 1 and kwargs["task_name"] == "create_carousel_content"

    def test_an_unparseable_reply_still_reads_as_a_parse_failure(self):
        """A residual parse failure still reads as one.

        The message has to say which fault it was — a model that answered with prose is a different
        problem from one that answered with half a deck.
        """
        junk = MagicMock()
        junk.choices = [MagicMock(message=MagicMock(content="I'm sorry, I can't help with that."))]
        _, deck, call, log_error = _generate([junk, junk])
        assert call.call_count == 2
        assert deck == {}
        assert log_error.call_count == 1
        assert "no parseable JSON object" in log_error.call_args[0][0]

    def test_a_repair_that_raises_keeps_the_first_draft(self):
        partial = {k: v for k, v in _GOOD_DECK.items() if k != "cover"}
        _, deck, call, log_error = _generate([_deck_response(partial),
                                              RuntimeError("provider down")])
        assert call.call_count == 2
        assert deck == partial          # nothing was thrown out of the task
        assert log_error.call_count == 1

    def test_a_repair_that_comes_back_worse_keeps_the_first_draft(self):
        partial = {k: v for k, v in _GOOD_DECK.items() if k != "cover"}
        _, deck, call, _ = _generate([_deck_response(partial), _deck_response(None)])
        assert call.call_count == 2
        assert deck == partial


class TestTheDirectiveBuilder:
    def test_nothing_missing_adds_no_directive(self):
        from cqc_lem.utilities.ai.ai_helper import _carousel_shape_directive
        assert _carousel_shape_directive([], "SomeCarousel with fields: cover") == ""
        assert _carousel_shape_directive(None, "SomeCarousel with fields: cover") == ""

    def test_the_directive_demands_both_top_level_keys(self):
        from cqc_lem.utilities.ai.ai_helper import _carousel_shape_directive
        directive = _carousel_shape_directive(["cover"], "SomeCarousel with fields: cover")
        assert '"post_text"' in directive and '"carousel"' in directive
        assert "SomeCarousel with fields: cover" in directive
