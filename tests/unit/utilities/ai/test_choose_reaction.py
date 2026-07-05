"""Unit tests for choose_post_reaction — the fast reaction picker with OpenAI + random fallbacks."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_AH = "cqc_lem.utilities.ai.ai_helper"


def _resp(content):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


class TestChoosePostReaction:
    def test_returns_valid_reaction_from_llm(self):
        from cqc_lem.utilities.ai.ai_helper import choose_post_reaction
        with patch(f"{_AH}.client") as client:
            client.chat.completions.create.return_value = _resp("Celebrate")
            assert choose_post_reaction("We just closed our seed round!", "Congrats!") == "Celebrate"
        assert client.chat.completions.create.call_args[1].get("model") == "lem-simple"

    def test_normalizes_case_and_punctuation(self):
        from cqc_lem.utilities.ai.ai_helper import choose_post_reaction
        with patch(f"{_AH}.client") as client:
            client.chat.completions.create.return_value = _resp("insightful.")
            assert choose_post_reaction("Here's the data", "Good breakdown") == "Insightful"

    def test_invalid_llm_word_falls_back_to_random(self):
        from cqc_lem.utilities.ai.ai_helper import choose_post_reaction, POST_REACTIONS
        with patch(f"{_AH}.client") as client:
            client.chat.completions.create.return_value = _resp("Banana")
            assert choose_post_reaction("p", "c") in POST_REACTIONS

    def test_litellm_failure_uses_openai_fallback(self):
        from cqc_lem.utilities.ai.ai_helper import choose_post_reaction
        with patch(f"{_AH}.client") as client, patch(f"{_AH}.openai") as oa:
            client.chat.completions.create.side_effect = Exception("proxy down")
            oa.OpenAI.return_value.chat.completions.create.return_value = _resp("Support")
            assert choose_post_reaction("Going through a tough layoff", "Hang in there") == "Support"
            assert oa.OpenAI.called

    def test_all_providers_fail_returns_random(self):
        from cqc_lem.utilities.ai.ai_helper import choose_post_reaction, POST_REACTIONS
        with patch(f"{_AH}.client") as client, patch(f"{_AH}.openai") as oa:
            client.chat.completions.create.side_effect = Exception("proxy down")
            oa.OpenAI.return_value.chat.completions.create.side_effect = Exception("openai down")
            assert choose_post_reaction("p", "c") in POST_REACTIONS

    def test_respects_allowed_list(self):
        from cqc_lem.utilities.ai.ai_helper import choose_post_reaction
        with patch(f"{_AH}.client") as client:
            client.chat.completions.create.return_value = _resp("Funny")
            # 'Funny' is excluded by default; explicitly allowing it lets it through
            assert choose_post_reaction("lol", "haha", allowed=["Like", "Funny"]) == "Funny"
