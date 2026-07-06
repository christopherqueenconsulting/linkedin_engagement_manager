"""Unit tests for newsletter edition generation."""

import json
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _resp(text):
    r = MagicMock(); r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


class TestGenerateNewsletterEdition:
    def test_parses_json_with_subtitle(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({
            "title": "5 Levers for Reach",
            "subtitle": "The reach levers most creators ignore — and how to pull them this week.",
            "body": "Hook line\n\nSECTION ONE\n\nA developed paragraph with a real example.",
        })
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof, topic="reach")
        assert out["title"] == "5 Levers for Reach"
        assert out["subtitle"] and len(out["subtitle"]) <= 150
        assert "SECTION ONE" in out["body"]

    def test_subtitle_falls_back_to_topic_when_missing(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "T", "body": "Some body text here."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof, topic="delegation")
        assert out["subtitle"] == "delegation"

    def test_markdown_stripped_from_body(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        body = "# Big Header\n\nHere is **bold** text and a *takeaway*."
        payload = json.dumps({"title": "T", "subtitle": "S", "body": body})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof)
        assert "#" not in out["body"]
        assert "**" not in out["body"]
        assert "Big Header" in out["body"]
        assert "bold" in out["body"] and "takeaway" in out["body"]

    def test_body_keeps_section_spacing(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        body = "Hook\n\nSECTION ONE\n\nParagraph one.\n\nSECTION TWO\n\nParagraph two."
        payload = json.dumps({"title": "T", "subtitle": "S", "body": body})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof)
        assert "\n\n" in out["body"]  # blank lines between sections preserved

    def test_fallback_first_line_is_title(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp("My Title\nBody line one\nBody line two")):
            out = ai_helper.generate_newsletter_edition(prof, topic="growth")
        assert out["title"] == "My Title"
        assert out["body"].startswith("Body line one")
        assert out["subtitle"] == "growth"

    def test_none_on_empty(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp(None)):
            assert ai_helper.generate_newsletter_edition(prof) is None


class TestGenerationUsesSynthesisAndSubject:
    def test_synthesis_replaces_raw_json_and_subject_in_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "RAW_PROFILE_JSON_SHOULD_NOT_APPEAR"
        payload = json.dumps({"title": "T", "subtitle": "S", "subject": "Planned Subject X", "body": "Body."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            out = ai_helper.generate_newsletter_edition(
                prof, topic="desc", subject="Planned Subject X", avoid_subjects=["Avoid Me"],
                profile_synthesis="THE VOICE BRIEF")
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "THE VOICE BRIEF" in prompt
        assert "RAW_PROFILE_JSON_SHOULD_NOT_APPEAR" not in prompt  # raw dump NOT used when synthesis given
        assert "Planned Subject X" in prompt
        assert "Avoid Me" in prompt
        assert out["subject"] == "Planned Subject X"

    def test_falls_back_to_raw_json_without_synthesis(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "RAW_JSON_HERE"
        payload = json.dumps({"title": "T", "subtitle": "S", "body": "Body."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            ai_helper.generate_newsletter_edition(prof, topic="desc")
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "RAW_JSON_HERE" in prompt

    def test_guidance_included_in_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "T", "subtitle": "S", "subject": "S", "body": "B."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            ai_helper.generate_newsletter_edition(prof, profile_synthesis="v", guidance="Make it about hiring")
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "Make it about hiring" in prompt

    def test_subject_defaults_from_passed_subject_when_model_omits(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "T", "subtitle": "S", "body": "B."})  # no subject key
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof, subject="Fallback Subject", profile_synthesis="v")
        assert out["subject"] == "Fallback Subject"


class TestPlanNewsletterTopics:
    def test_returns_distinct_subjects(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [
            {"subject": "Content frameworks that scale", "angle": "foundational"},
            {"subject": "Engagement tactics that compound", "angle": "tactical"},
            {"subject": "Personal-brand positioning", "angle": "advanced"},
        ]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("voice", "desc", {}, [], 3)
        assert len(out) == 3
        assert {o["subject"] for o in out} == {
            "Content frameworks that scale", "Engagement tactics that compound",
            "Personal-brand positioning"}
        assert out[0]["angle"] == "foundational"

    def test_dedups_within_response(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [
            {"subject": "Same Topic", "angle": "a"},
            {"subject": "same topic", "angle": "b"},  # case-insensitive duplicate dropped
            {"subject": "Different", "angle": "c"},
        ]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 3)
        assert [o["subject"] for o in out] == ["Same Topic", "Different"]

    def test_passes_prior_subjects_into_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [{"subject": "Fresh", "angle": "x"}]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)) as call:
            ai_helper.plan_newsletter_topics("v", "d", None, ["Old Subject A", "Old Subject B"], 1)
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "Old Subject A" in prompt and "Old Subject B" in prompt
        assert "AVOID" in prompt

    def test_respects_count_cap(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = json.dumps({"editions": [
            {"subject": "A", "angle": "1"}, {"subject": "B", "angle": "2"},
            {"subject": "C", "angle": "3"}]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 2)
        assert len(out) == 2

    def test_tolerates_json_fences(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = "```json\n" + json.dumps({"editions": [{"subject": "S", "angle": "a"}]}) + "\n```"
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 1)
        assert out and out[0]["subject"] == "S"

    def test_extracts_json_from_surrounding_prose(self):
        from cqc_lem.utilities.ai import ai_helper
        payload = "Here is the plan:\n" + json.dumps({"editions": [{"subject": "S", "angle": "a"}]})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.plan_newsletter_topics("v", "d", None, [], 1)
        assert out and out[0]["subject"] == "S"

    def test_bad_json_falls_back_empty(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("not json at all")):
            assert ai_helper.plan_newsletter_topics("v", "d", None, [], 3) == []

    def test_llm_exception_falls_back_empty(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", side_effect=Exception("boom")):
            assert ai_helper.plan_newsletter_topics("v", "d", None, [], 3) == []
