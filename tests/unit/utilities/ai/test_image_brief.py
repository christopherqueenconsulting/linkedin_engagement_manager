"""The ONE image-brief engine: validated lem-medium JSON briefs with a deterministic fallback."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai.image_brief import (ImageBrief, _STYLE_PRESETS, build_image_brief)

_GOOD = {"focal_concept": "a founder reviewing a growth chart",
         "prompt": ("A confident founder stands beside a floor-to-ceiling window in a modern "
                    "office at golden hour, warm rim light, shallow depth of field, a single "
                    "bold teal accent in the scene, photorealistic editorial photograph.")}


def _resp(payload) -> SimpleNamespace:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.mark.unit
class TestBuildImageBrief:
    def test_valid_brief_comes_back_structured(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            brief = build_image_brief("My post about growth", surface="newsletter", ratio="16:9")

        assert isinstance(brief, ImageBrief)
        assert brief.prompt == _GOOD["prompt"]
        assert brief.focal_concept == _GOOD["focal_concept"]
        assert brief.ratio == "16:9" and brief.surface == "newsletter"
        kwargs = llm.call_args[1]
        assert kwargs["model"] == "lem-medium"
        assert kwargs["response_format"] == {"type": "json_object"}
        # The surface preset and the actual content must both reach the author.
        user_msg = kwargs["messages"][1]["content"]
        assert _STYLE_PRESETS["newsletter"] in user_msg
        assert "My post about growth" in user_msg

    def test_no_text_constraint_is_always_in_the_system_prompt(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="post_image")
        system_msg = llm.call_args[1]["messages"][0]["content"]
        assert "NO text, letters, words, numbers, logos" in system_msg

    def test_invalid_json_retries_then_falls_back_deterministically(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp("not json at all")) as llm:
            brief = build_image_brief("Quarterly revenue lessons", surface="post_image")

        assert llm.call_count == 2
        assert "Quarterly revenue lessons" in brief.prompt
        assert "No text" in brief.prompt
        assert brief.focal_concept  # never empty

    def test_refusal_text_is_rejected(self):
        refusal = {"focal_concept": "n/a",
                   "prompt": "I'm sorry, as an AI I cannot generate that image description " * 3}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(refusal)):
            brief = build_image_brief("content here", surface="carousel")
        assert "I'm sorry" not in brief.prompt

    def test_llm_outage_never_raises(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=RuntimeError("proxy down")):
            brief = build_image_brief("A story about mentorship", surface="video", ratio="9:16")
        assert "mentorship" in brief.prompt.lower()
        assert brief.ratio == "9:16"

    def test_unknown_surface_uses_default_preset(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)):
            brief = build_image_brief("content", surface="something_new")
        assert brief.style_preset == "post_image"

    def test_avatar_subject_clause_leads_the_context(self):
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="newsletter", avatar=avatar)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "it IS the author" in user_msg


@pytest.mark.unit
class TestRefusalFilterIsAnchoredToRefusals:
    """Regression: a bare "language model" ban rejected every legitimate brief an AI-focused
    author writes, so their briefs silently fell back to the deterministic template."""

    @pytest.mark.parametrize("prompt_text", [
        ("A photorealistic scene of an engineer studying a wall display of large language model "
         "routing costs, shallow depth of field, dramatic rim light, modern office at dusk."),
        ("A close-up of a founder at a whiteboard mapping an AI language model pipeline, warm "
         "window light, one bold orange accent, editorial photograph."),
    ])
    def test_legitimate_ai_subject_matter_is_accepted(self, prompt_text):
        payload = {"focal_concept": "LLM routing cost risk", "prompt": prompt_text}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(payload)):
            brief = build_image_brief("post about LLM cost routing", surface="post_image")
        assert brief.prompt == prompt_text, "an AI-topic brief must not fall back"

    @pytest.mark.parametrize("refusal", [
        "I'm sorry, I cannot create that image for you because it violates the guidelines here.",
        "As an AI language model, I am unable to generate the requested description at all.",
        "I can't fulfill this request, but here is a generic description of an office scene.",
    ])
    def test_actual_refusals_are_still_rejected(self, refusal):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp({"focal_concept": "n/a", "prompt": refusal * 2})):
            brief = build_image_brief("some content", surface="post_image")
        assert refusal not in brief.prompt


@pytest.mark.unit
class TestReasoningTokenBudget:
    """Regression: lem-medium is a REASONING model. At max_tokens=600 the whole budget went to
    thinking tokens, the response came back EMPTY with finish_reason='length', and every image
    silently rendered from the deterministic template."""

    def test_budget_leaves_room_for_reasoning_plus_json(self):
        from cqc_lem.utilities.ai.image_brief import _BRIEF_MAX_TOKENS
        # A real brief measured ~870 completion tokens including reasoning.
        assert _BRIEF_MAX_TOKENS >= 2000

    def test_the_budget_is_what_is_actually_sent(self):
        from cqc_lem.utilities.ai.image_brief import _BRIEF_MAX_TOKENS
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="post_image")
        assert llm.call_args[1]["max_tokens"] == _BRIEF_MAX_TOKENS

    def test_an_empty_response_retries_rather_than_crashing(self):
        empty = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=[empty, _resp(_GOOD)]) as llm:
            brief = build_image_brief("content", surface="post_image")
        assert llm.call_count == 2
        assert brief.prompt == _GOOD["prompt"], "a retry that succeeds must not fall back"

    def test_the_fallback_warning_says_why(self):
        empty = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=empty), \
             patch("cqc_lem.utilities.ai.image_brief.log_warning") as warn:
            build_image_brief("content", surface="post_image")
        reason = warn.call_args[1].get("reason", "")
        assert "empty response" in reason and "length" in reason


@pytest.mark.unit
class TestAnonymousPersonSteer:
    """A stranger's face on a PERSONAL-brand newsletter is the stock-photo look the engine
    exists to replace; with the author's own likeness, a person IS the point."""

    def test_no_avatar_steers_away_from_an_anonymous_face(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="newsletter", avatar=None)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "do NOT make an anonymous person the focal subject" in user_msg

    def test_with_an_avatar_the_person_is_still_the_point(self):
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="newsletter", avatar=avatar)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "anonymous person" not in user_msg
        assert "it IS the author" in user_msg
