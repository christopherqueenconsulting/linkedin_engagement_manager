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
