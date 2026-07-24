"""Unit tests for the hot-lead response generator (issue #483) — the approval-gated draft sent to
someone showing inbound buying intent."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _resp(text):
    r = MagicMock(); r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock(); p.model_dump_json.return_value = "{}"
    return p


class TestGenerateLeadResponse:
    def test_returns_the_draft(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("  Depends on scope — what's your timeline?  ")):
            out = ai_helper.generate_lead_response("How much?", _profile())
        assert out == "Depends on scope — what's your timeline?"

    def test_none_when_the_model_returns_nothing(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(None)):
            assert ai_helper.generate_lead_response("How much?", _profile()) is None

    def test_dm_channel_uses_the_dm_budget(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("hi")), \
             patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c) as hum:
            ai_helper.generate_lead_response("How much?", _profile(), channel="dm")
        assert hum.call_args.kwargs["max_chars"] == 300
        assert hum.call_args.kwargs["content_type"] == "dm"

    def test_reply_channel_uses_the_comment_budget(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("hi")), \
             patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c) as hum:
            ai_helper.generate_lead_response("How much?", _profile(), channel="reply")
        assert hum.call_args.kwargs["max_chars"] == 500
        assert hum.call_args.kwargs["content_type"] == "comment"

    def test_prompt_forbids_inventing_prices_and_includes_their_message(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("hi")) as call, \
             patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c):
            ai_helper.generate_lead_response("What does this cost?", _profile(),
                                             context="the post they commented on")
        messages = call.call_args.kwargs["messages"]
        assert "NEVER invent prices" in messages[0]["content"]
        assert "What does this cost?" in messages[1]["content"]
        assert "the post they commented on" in messages[1]["content"]
