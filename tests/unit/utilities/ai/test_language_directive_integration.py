"""Unit tests ensuring the language directive reaches every comment/reply prompt (issue #2001).

Comments and replies were shipping with no output-language instruction at all, so a fresh feed
comment or reply grounded in someone else's (non-English) post/comment could mirror that language
instead of the user's own. These tests do NOT call the LLM; they assert the prompt string sent to
`_call_llm` carries the user's configured content language.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.linkedin.profile import LinkedInProfile

pytestmark = pytest.mark.unit


def _profile() -> LinkedInProfile:
    return LinkedInProfile(full_name="Test User", skills=["AI Strategy"])


def _prompt_text(mock_llm) -> str:
    content = mock_llm.call_args.kwargs["messages"][1]["content"]
    return content[0]["text"] if isinstance(content, list) else content


class TestLanguageDirectiveReachesEveryCommentPrompt:
    def test_feed_comment_prompt_states_the_configured_language(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.ai.ai_helper.research_topic", return_value={}), \
             patch("cqc_lem.utilities.db.get_user_content_language", return_value="es"):
            from cqc_lem.utilities.ai.ai_helper import generate_ai_response
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Buen punto."))])
            generate_ai_response("Post content", _profile(), user_id=7)
            prompt = _prompt_text(mock_llm)
            assert "write your ENTIRE response in Spanish" in prompt

    def test_feed_reply_to_a_specific_comment_is_labeled_a_reply_not_the_post_author(self):
        # Issue #2001's second complaint: this branch used to tell the model "You are responding
        # as the author of the LinkedIn Content", which is wrong (this function only ever comments
        # on someone else's post) and reads as indistinguishable from a fresh top-level comment.
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.db.get_user_content_language", return_value="en-US"):
            from cqc_lem.utilities.ai.ai_helper import generate_ai_response
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Good point."))])
            generate_ai_response("Post content", _profile(), post_comment="their comment", user_id=7)
            prompt = _prompt_text(mock_llm)
            assert "You are responding as the author of the LinkedIn Content" not in prompt
            assert "This is a REPLY to that specific comment" in prompt
            assert "not its author" in prompt

    def test_seed_comment_prompt_states_the_configured_language(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.ai.ai_helper.lint_repaired", side_effect=lambda x, *a, **k: x), \
             patch("cqc_lem.utilities.db.get_user_content_language", return_value="fr"):
            from cqc_lem.utilities.ai.ai_helper import generate_seed_comment
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Question ?"))])
            generate_seed_comment("Post content", _profile(), user_id=7)
            prompt = _prompt_text(mock_llm)
            assert "write your ENTIRE response in French" in prompt

    def test_second_wave_prompt_states_the_configured_language(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.ai.ai_helper._gated_comment",
                   side_effect=lambda draft, *a, **k: draft()), \
             patch("cqc_lem.utilities.db.get_user_content_language", return_value="de"):
            from cqc_lem.utilities.ai.ai_helper import generate_second_wave_comment
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="More context."))])
            generate_second_wave_comment("Post content", _profile(), user_id=7)
            prompt = _prompt_text(mock_llm)
            assert "write your ENTIRE response in German" in prompt

    def test_thread_reply_prompt_states_the_configured_language(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.ai.ai_helper.lint_repaired", side_effect=lambda x, *a, **k: x), \
             patch("cqc_lem.utilities.db.get_user_content_language", return_value="pt"):
            from cqc_lem.utilities.ai.ai_helper import generate_thread_reply
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Thanks."))])
            generate_thread_reply("Post content", "Comment", _profile(), user_id=7)
            prompt = _prompt_text(mock_llm)
            assert "write your ENTIRE response in Portuguese" in prompt

    def test_comment_reply_followup_prompt_states_the_configured_language(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.ai.ai_helper.lint_repaired", side_effect=lambda x, *a, **k: x), \
             patch("cqc_lem.utilities.db.get_user_content_language", return_value="en-US"):
            from cqc_lem.utilities.ai.ai_helper import generate_comment_reply_followup
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Reply."))])
            generate_comment_reply_followup("Their reply", _profile(), user_id=7)
            prompt = _prompt_text(mock_llm)
            assert "write your ENTIRE response in English" in prompt

    def test_defaults_to_english_when_user_has_no_content_language_configured(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.ai.ai_helper.lint_repaired", side_effect=lambda x, *a, **k: x):
            from cqc_lem.utilities.ai.ai_helper import generate_thread_reply
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Thanks."))])
            generate_thread_reply("Post content", "Comment", _profile(), user_id=None)
            prompt = _prompt_text(mock_llm)
            assert "write your ENTIRE response in English" in prompt
