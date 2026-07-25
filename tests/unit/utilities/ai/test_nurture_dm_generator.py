"""Unit tests for the DM auto-nurture draft generator (issue #485) — the approval-gated next
message written against what a lead actually replied."""

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


class TestGenerateNurtureDm:
    def test_returns_the_trimmed_draft(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("  Worth a 15 min call?  ")):
            out = ai_helper.generate_nurture_dm("Sounds good", "interested", _profile())
        assert out == "Worth a 15 min call?"

    def test_none_when_the_model_returns_nothing(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(None)):
            assert ai_helper.generate_nurture_dm("Sounds good", "interested", _profile()) is None

    def test_uses_the_dm_budget(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("hi")), \
             patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c) as hum:
            ai_helper.generate_nurture_dm("Sounds good", "interested", _profile())
        assert hum.call_args.kwargs["max_chars"] == 300
        assert hum.call_args.kwargs["content_type"] == "dm"

    def test_intent_guidance_reaches_the_system_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        from cqc_lem.utilities.ai.dm_nurture import nurture_guidance
        with patch(f"{_AI}._call_llm", return_value=_resp("hi")) as call, \
             patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c):
            ai_helper.generate_nurture_dm("Too expensive", "objection", _profile())
        system = call.call_args.kwargs["messages"][0]["content"]
        assert nurture_guidance("objection") in system
        assert "invent prices" in system

    def test_their_reply_name_and_template_hint_reach_the_user_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("hi")) as call, \
             patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c):
            ai_helper.generate_nurture_dm("Next quarter maybe", "not_now", _profile(),
                                          first_name="Jane", template_hint="offer the checklist")
        user = call.call_args.kwargs["messages"][1]["content"]
        assert "Next quarter maybe" in user
        assert "Jane" in user
        assert "offer the checklist" in user

    def test_prior_messages_are_included_so_it_cannot_repeat_itself(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("hi")) as call, \
             patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c):
            ai_helper.generate_nurture_dm("ok", "neutral", _profile(),
                                          history=["one", "two", "three", "four"])
        user = call.call_args.kwargs["messages"][1]["content"]
        # Only the last three touches are carried — enough to avoid repeats without bloating context.
        assert "four" in user and "three" in user and "two" in user
        assert "- one" not in user

    def test_empty_history_adds_no_prior_block(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("hi")) as call, \
             patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c):
            ai_helper.generate_nurture_dm("ok", "neutral", _profile(), history=[None, ""])
        assert "do not repeat it" not in call.call_args.kwargs["messages"][1]["content"]
