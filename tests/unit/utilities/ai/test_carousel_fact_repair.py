"""The carousel generator's fact-grounding repair (issue #2231).

A fact-anchored deck whose slides state a number no verified fact backs used to reach the renderer
unchanged, so the only record was `_report_carousel_fact_grounding`'s advisory warning — which
recurred and escalated. Slides are baked into images, so the generator now gives such a deck ONE
regeneration naming the offending numbers, and keeps the retry only when it is strictly better.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_RECEIPT = {"format": "build_receipt"}

_INVENTED = {
    "cover": {"title": "The build receipt", "content": "What it took to ship."},
    "contents": [
        {"title": "1. Pin the tag", "content": "Set IMAGE_TAG to the release tag. It cut rollbacks by 37%."},
        {"title": "2. Migrate first", "content": "Run `flyway migrate` before the app flips."},
    ],
    "call_to_action": {"title": "Save this", "content": "Save it for your next deploy."},
}
_CLEAN = {
    "cover": {"title": "The build receipt", "content": "What it took to ship."},
    "contents": [
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


def _generate(responses, blueprint=_RECEIPT, **kwargs):
    from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
    with patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
               side_effect=RuntimeError("no creds")), \
            patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
            patch(f"{_AI}._alignment_directive", return_value=""), \
            patch(f"{_AI}._call_llm", side_effect=responses) as call:
        text, deck = generate_carousel_content(1, "awareness", blueprint=blueprint, **kwargs)
    return text, deck, call


def _prompt(call, n: int) -> str:
    return call.call_args_list[n][1]["messages"][1]["content"][0]["text"]


class TestCarouselFactRepair:
    def test_an_invented_slide_number_is_regenerated_with_the_number_named(self):
        _, deck, call = _generate([_response(_INVENTED), _response(_CLEAN)])
        assert call.call_count == 2
        assert deck == _CLEAN
        retry = _prompt(call, 1)
        assert "YOUR PREVIOUS DECK INVENTED SPECIFICS" in retry
        assert "37" in retry.split("YOUR PREVIOUS DECK INVENTED SPECIFICS", 1)[1]

    def test_a_number_the_bank_backs_costs_no_retry(self):
        # The CHECKER's anchors are the whole bank, so a number from another entry is not invented.
        _, deck, call = _generate([_response(_INVENTED)],
                                  grounding_anchors=["Pinning the tag cut rollbacks by 37%"])
        assert call.call_count == 1
        assert deck == _INVENTED

    def test_an_ordinary_archetype_is_never_graded(self):
        _, deck, call = _generate([_response(_INVENTED, caption="Some thoughts on shipping.")],
                                  blueprint={"format": "personal_lesson"})
        assert call.call_count == 1
        assert deck == _INVENTED

    def test_a_retry_no_better_keeps_the_first_deck(self):
        worse = {**_INVENTED, "cover": {"title": "The 12 step receipt", "content": "Took 9 days."}}
        _, deck, call = _generate([_response(_INVENTED), _response(worse)])
        assert call.call_count == 2
        assert deck == _INVENTED

    def test_a_retry_that_defers_the_number_to_a_placeholder_keeps_the_first_deck(self):
        # A `[[…]]` on a slide renders as literal brackets, so trading the number for one is no fix.
        deferred = {**_CLEAN, "contents": [
            {"title": "1. Pin the tag",
             "content": "Set IMAGE_TAG to the release tag. It cut rollbacks by [[METRIC: rollback cut]]."},
            _CLEAN["contents"][1],
        ]}
        _, deck, call = _generate([_response(_INVENTED), _response(deferred)])
        assert call.call_count == 2
        assert deck == _INVENTED

    def test_a_retry_missing_a_slide_keeps_the_first_deck(self):
        no_cover = {k: v for k, v in _CLEAN.items() if k != "cover"}
        _, deck, _ = _generate([_response(_INVENTED), _response(no_cover)])
        assert deck == _INVENTED

    def test_a_retry_that_errors_keeps_the_first_deck(self):
        with patch(f"{_AI}.log_warning") as warn:
            _, deck, _ = _generate([_response(_INVENTED), RuntimeError("proxy down")])
        assert deck == _INVENTED
        assert any("fact-grounding retry failed" in c.args[0] for c in warn.call_args_list)

    def test_a_retry_that_breaks_the_reference_gate_keeps_the_first_deck(self):
        narrative = {
            "cover": {"title": "The build receipt", "content": "What it took to ship."},
            "contents": [{"title": "Automation compounds",
                          "content": "Every manual step removed paid for itself."}],
            "call_to_action": {"title": "Save this", "content": "Save it for later."},
        }
        _, deck, _ = _generate([_response(_INVENTED), _response(narrative)])
        assert deck == _INVENTED

    def test_a_grading_fault_never_costs_the_deck(self):
        with patch(f"{_AI}._framework.fact_grounding_report", side_effect=RuntimeError("boom")), \
                patch(f"{_AI}.log_warning") as warn:
            _, deck, call = _generate([_response(_INVENTED)])
        assert call.call_count == 1
        assert deck == _INVENTED
        assert any("Could not grade the carousel deck" in c.args[0] for c in warn.call_args_list)


class TestDeckFactRetryDirective:
    def test_it_names_the_numbers_and_forbids_slide_placeholders(self):
        from cqc_lem.utilities.ai.content_framework import deck_fact_retry_directive
        text = deck_fact_retry_directive({"unverified": [{"raw": "37"}, {"raw": "37"}, {"raw": "9"}]})
        assert "37, 9" in text
        assert "do NOT replace them with [[" in text

    def test_a_report_naming_nothing_produces_no_directive(self):
        from cqc_lem.utilities.ai.content_framework import deck_fact_retry_directive
        assert deck_fact_retry_directive({"unverified": []}) == ""
        assert deck_fact_retry_directive(None) == ""
