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
    def test_parses_json(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        payload = json.dumps({"title": "5 Levers for Reach", "body": "Intro\n\nSection 1\n..."})
        with patch(f"{_AI}._call_llm", return_value=_resp(payload)):
            out = ai_helper.generate_newsletter_edition(prof, topic="reach")
        assert out["title"] == "5 Levers for Reach" and "Section 1" in out["body"]

    def test_fallback_first_line_is_title(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp("My Title\nBody line one\nBody line two")):
            out = ai_helper.generate_newsletter_edition(prof)
        assert out["title"] == "My Title" and out["body"].startswith("Body line one")

    def test_none_on_empty(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp(None)):
            assert ai_helper.generate_newsletter_edition(prof) is None
