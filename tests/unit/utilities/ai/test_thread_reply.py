"""Unit tests for the thread-building reply generator (P7)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _resp(text):
    r = MagicMock(); r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


class TestGenerateThreadReply:
    def test_returns_reply(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp("Great point — what led you there?")):
            out = ai_helper.generate_thread_reply("my post", "their comment", prof)
        assert out.endswith("?")

    def test_none_when_empty(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock(); prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp(None)):
            assert ai_helper.generate_thread_reply("p", "c", prof) is None


class TestCommentScrub:
    """Issue #2204: every comment surface ships without an em dash, even when humanize keeps one."""

    def test_thread_reply_has_no_em_dash(self):
        from cqc_lem.utilities.ai import ai_helper
        prof = MagicMock()
        prof.model_dump_json.return_value = "{}"
        with patch(f"{_AI}._call_llm", return_value=_resp("Great point — what led you there?")), \
                patch(f"{_AI}._humanize_text", side_effect=lambda text, **_: text):
            out = ai_helper.generate_thread_reply("my post", "their comment", prof)
        assert out == "Great point, what led you there?"

    def test_humanize_comment_scrubs_what_humanize_returns(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._humanize_text", return_value="One — two -- three") as humanize:
            assert ai_helper._humanize_comment("draft", "synth", {"k": 1}) == "One, two, three"
        humanize.assert_called_once_with("draft", content_type="comment",
                                         profile_synthesis="synth", prefs={"k": 1})
