"""Unit tests for the hook + save-worthy post pass."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _resp(text):
    r = MagicMock(); r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


class TestOptimizePostHook:
    def test_returns_rewritten(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Bold hook.\n\nBody... Save this.")):
            out = ai_helper.optimize_post_hook("original boring post")
        assert out.startswith("Bold hook.")

    def test_empty_passthrough(self):
        from cqc_lem.utilities.ai import ai_helper
        assert ai_helper.optimize_post_hook("") == ""

    def test_falls_back_to_original_on_error(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", side_effect=RuntimeError("boom")):
            assert ai_helper.optimize_post_hook("keep me") == "keep me"

    def test_none_llm_content_keeps_original(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(None)):
            assert ai_helper.optimize_post_hook("keep me") == "keep me"
