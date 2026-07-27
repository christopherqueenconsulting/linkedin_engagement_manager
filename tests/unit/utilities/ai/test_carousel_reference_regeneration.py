"""Unit tests for the carousel generator's reference-value loop (issue #728): a deck whose slides
carry nothing reusable is REGENERATED with a directive naming exactly what was missing, the retry is
bounded, and a passing deck never costs a second call."""

import json
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"

_NARRATIVE = {
    "cover": {"title": "The build receipt", "content": "What it took."},
    "insights": [
        {"title": "Release count is not the goal",
         "content": "Shipping often is a side effect of small reversible changes."},
        {"title": "Automation compounds",
         "content": "Every manual step removed paid for itself as the work grew."},
    ],
    "call_to_action": {"title": "Save this", "content": "Save it for later."},
}
_REFERENCE = {
    "cover": {"title": "The 3 checks I run", "content": "The exact stack."},
    "insights": [
        {"title": "1. Pin the tag", "content": "Set IMAGE_TAG to the release tag, never latest."},
        {"title": "2. Migrate first", "content": "Run `flyway migrate` before the app flips."},
    ],
    "call_to_action": {"title": "Save this", "content": "Save it for your next deploy."},
}


def _response(deck: dict, caption: str = "Here is the exact stack."):
    message = MagicMock()
    message.content = json.dumps({"post_text": caption, "carousel": deck})
    response = MagicMock()
    response.choices = [MagicMock(message=message)]
    return response


class _Harness:
    """Everything `generate_carousel_content` reaches for that is not the deck gate."""

    def __enter__(self):
        # The profile lookup is a Selenium round trip; failing it exercises the cached-profile
        # fallback the generator already has and keeps these tests to the deck gate.
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


def _generate(responses, blueprint=None, **kwargs):
    from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
    with _Harness(), patch(f"{_AI}._call_llm", side_effect=responses) as call:
        text, deck = generate_carousel_content(1, "awareness", blueprint=blueprint, **kwargs)
    return text, deck, call


class TestReferenceGateRegeneration:
    def test_a_narrative_deck_is_regenerated_with_the_missing_artifacts_named(self):
        _, deck, call = _generate([_response(_NARRATIVE), _response(_REFERENCE)],
                                  blueprint={"format": "build_receipt"})
        assert call.call_count == 2
        assert deck == _REFERENCE
        retry_prompt = call.call_args_list[1][1]["messages"][1]["content"][0]["text"]
        assert "YOUR PREVIOUS DECK WAS REJECTED" in retry_prompt
        assert "Release count is not the goal" in retry_prompt

    def test_a_reference_deck_costs_exactly_one_call(self):
        _, deck, call = _generate([_response(_REFERENCE)], blueprint={"format": "build_receipt"})
        assert call.call_count == 1
        assert deck == _REFERENCE

    def test_the_retry_is_bounded_and_the_last_deck_still_ships(self):
        # No review queue sits behind rendered slide images, so a deck that cannot be fixed ships
        # with the reason logged rather than failing the whole post.
        _, deck, call = _generate([_response(_NARRATIVE), _response(_NARRATIVE)],
                                  blueprint={"format": "build_receipt"})
        assert call.call_count == 2
        assert deck == _NARRATIVE

    def test_the_attempt_budget_is_configurable(self, monkeypatch):
        monkeypatch.setenv("DECK_REFERENCE_MAX_ATTEMPTS", "3")
        _, _, call = _generate([_response(_NARRATIVE)] * 3, blueprint={"format": "build_receipt"})
        assert call.call_count == 3

    def test_an_ordinary_archetype_whose_caption_promises_nothing_is_not_regenerated(self):
        _, _, call = _generate([_response(_NARRATIVE, caption="Some thoughts on shipping.")],
                               blueprint={"format": "personal_lesson"})
        assert call.call_count == 1

    def test_an_ordinary_archetype_that_promised_a_stack_is_still_held_to_it(self):
        _, _, call = _generate([_response(_NARRATIVE), _response(_REFERENCE)],
                               blueprint={"format": "personal_lesson"})
        assert call.call_count == 2

    def test_the_gate_can_be_stood_down(self, monkeypatch):
        monkeypatch.setenv("DECK_REFERENCE_ENABLED", "false")
        _, deck, call = _generate([_response(_NARRATIVE)], blueprint={"format": "build_receipt"})
        assert call.call_count == 1
        assert deck == _NARRATIVE


class TestTheRetryNeverCostsUsTheDraftWeHave:
    """The gate's whole posture is 'a narrative deck beats no post'. A second call that errors or
    comes back unparseable must not turn a shippable deck into a failed task or an empty one."""

    def test_a_retry_that_raises_keeps_the_first_deck(self):
        text, deck, call = _generate([_response(_NARRATIVE), RuntimeError("provider down")],
                                     blueprint={"format": "build_receipt"})
        assert call.call_count == 2
        assert deck == _NARRATIVE
        assert "exact stack" in text

    def test_a_retry_that_comes_back_unparseable_keeps_the_first_deck(self):
        bad = MagicMock()
        bad.choices = [MagicMock(message=MagicMock(content="not json at all"))]
        text, deck, call = _generate([_response(_NARRATIVE), bad],
                                     blueprint={"format": "build_receipt"})
        assert call.call_count == 2
        assert deck == _NARRATIVE
        assert "exact stack" in text


class TestStoryDirectiveReachesTheWriter:
    def test_the_one_anchored_story_rides_into_the_carousel_prompt(self):
        directive = "\n\nYOUR STORY BANK ENTRY: rendered 40 slides in one pass.\n"
        _, _, call = _generate([_response(_REFERENCE)], blueprint={"format": "build_receipt"},
                               story_directive=directive)
        prompt = call.call_args_list[0][1]["messages"][1]["content"][0]["text"]
        assert "rendered 40 slides in one pass" in prompt

    def test_a_save_targeted_deck_is_told_the_per_slide_rule_up_front(self):
        _, _, call = _generate([_response(_REFERENCE)], blueprint={"format": "build_receipt"})
        prompt = call.call_args_list[0][1]["messages"][1]["content"][0]["text"]
        assert "EVERY BODY SLIDE MUST CARRY SOMETHING REUSABLE" in prompt
