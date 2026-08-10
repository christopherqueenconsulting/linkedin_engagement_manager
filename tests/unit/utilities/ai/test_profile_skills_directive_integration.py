"""Unit tests ensuring profile-skills directive reaches post/comment prompts (issue #1075).

These tests do NOT call the LLM; they assert the prompt string carries the directive when a
re-index window is open and omits it when the window is closed.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.linkedin.profile import LinkedInProfile

pytestmark = pytest.mark.unit


def _redis_client_mock():
    """In-memory Redis stand-in."""
    store = {}

    class Client:
        def setex(self, key, seconds, value):
            store[key] = (value, seconds)

        def get(self, key):
            return store.get(key, (None, None))[0]

    return Client(), store


def _profile() -> LinkedInProfile:
    return LinkedInProfile(full_name="Test User", skills=["AI Strategy", "Product Growth"])


class TestPostDirectiveInjection:
    def test_thought_leadership_prompt_contains_skills_during_window(self):
        client, store = _redis_client_mock()
        store["lem:profile_skills_window:7"] = (json.dumps(["ai strategy", "product growth"]), 1209600)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            from cqc_lem.utilities.ai.ai_helper import get_thought_leadership_post_from_ai
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="draft"))])
            get_thought_leadership_post_from_ai(
                _profile(), "awareness", post_id=1, user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo: ai strategy, product growth" in prompt

    def test_thought_leadership_prompt_omits_skills_outside_window(self):
        client, _ = _redis_client_mock()
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            from cqc_lem.utilities.ai.ai_helper import get_thought_leadership_post_from_ai
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="draft"))])
            get_thought_leadership_post_from_ai(
                _profile(), "awareness", post_id=1, user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo" not in prompt

    def test_industry_news_prompt_contains_skills_during_window(self):
        client, store = _redis_client_mock()
        store["lem:profile_skills_window:7"] = (json.dumps(["ai strategy"]), 1209600)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client), \
             patch("cqc_lem.utilities.ai.ai_helper.get_industry_trend_analysis_based_on_user_profile",
                   return_value={"industry": "Technology", "analysis": ""}):
            from cqc_lem.utilities.ai.ai_helper import get_industry_news_post_from_ai
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="draft"))])
            get_industry_news_post_from_ai(_profile(), "awareness", post_id=1, user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo: ai strategy" in prompt

    def test_personal_story_prompt_contains_skills_during_window(self):
        client, store = _redis_client_mock()
        store["lem:profile_skills_window:7"] = (json.dumps(["product growth"]), 1209600)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client), \
             patch("cqc_lem.utilities.ai.ai_helper.get_industry_trend_analysis_based_on_user_profile",
                   return_value={"industry": "Technology", "analysis": ""}):
            from cqc_lem.utilities.ai.ai_helper import get_personal_story_post_from_ai
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="draft"))])
            get_personal_story_post_from_ai(_profile(), "awareness", post_id=1, user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo: product growth" in prompt

    def test_engagement_prompt_contains_skills_during_window(self):
        client, store = _redis_client_mock()
        store["lem:profile_skills_window:7"] = (json.dumps(["b2b sales"]), 1209600)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client), \
             patch("cqc_lem.utilities.ai.ai_helper.get_industry_trend_analysis_based_on_user_profile",
                   return_value={"industry": "Technology", "analysis": ""}):
            from cqc_lem.utilities.ai.ai_helper import generate_engagement_prompt_post
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="draft"))])
            generate_engagement_prompt_post(_profile(), "awareness", post_id=1, user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo: b2b sales" in prompt


class TestCommentDirectiveInjection:
    def test_feed_comment_prompt_contains_skills_during_window(self):
        client, store = _redis_client_mock()
        store["lem:profile_skills_window:7"] = (json.dumps(["ai strategy"]), 1209600)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client), \
             patch("cqc_lem.utilities.ai.ai_helper.research_topic", return_value={}):
            from cqc_lem.utilities.ai.ai_helper import generate_ai_response
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Nice post."))])
            generate_ai_response("Post content", _profile(), user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo: ai strategy" in prompt

    def test_seed_comment_prompt_contains_skills_during_window(self):
        client, store = _redis_client_mock()
        store["lem:profile_skills_window:7"] = (json.dumps(["product growth"]), 1209600)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client), \
             patch("cqc_lem.utilities.ai.ai_helper.lint_repaired", side_effect=lambda x, *a, **k: x):
            from cqc_lem.utilities.ai.ai_helper import generate_seed_comment
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Question?"))])
            generate_seed_comment("Post content", _profile(), user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo: product growth" in prompt

    def test_second_wave_prompt_contains_skills_during_window(self):
        client, store = _redis_client_mock()
        store["lem:profile_skills_window:7"] = (json.dumps(["leadership"]), 1209600)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client), \
             patch("cqc_lem.utilities.ai.ai_helper._gated_comment", side_effect=lambda draft, *a, **k: draft()):
            from cqc_lem.utilities.ai.ai_helper import generate_second_wave_comment
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="More context."))])
            generate_second_wave_comment("Post content", _profile(), user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo: leadership" in prompt

    def test_thread_reply_prompt_contains_skills_during_window(self):
        client, store = _redis_client_mock()
        store["lem:profile_skills_window:7"] = (json.dumps(["ai strategy"]), 1209600)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client), \
             patch("cqc_lem.utilities.ai.ai_helper.lint_repaired", side_effect=lambda x, *a, **k: x):
            from cqc_lem.utilities.ai.ai_helper import generate_thread_reply
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Thanks."))])
            generate_thread_reply("Post content", "Comment", _profile(), user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo: ai strategy" in prompt

    def test_comment_reply_followup_prompt_contains_skills_during_window(self):
        client, store = _redis_client_mock()
        store["lem:profile_skills_window:7"] = (json.dumps(["b2b sales"]), 1209600)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as mock_llm, \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client), \
             patch("cqc_lem.utilities.ai.ai_helper.lint_repaired", side_effect=lambda x, *a, **k: x):
            from cqc_lem.utilities.ai.ai_helper import generate_comment_reply_followup
            mock_llm.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Reply."))])
            generate_comment_reply_followup("Their reply", _profile(), user_id=7)
            content = mock_llm.call_args.kwargs["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            assert "Profile skills to echo: b2b sales" in prompt
