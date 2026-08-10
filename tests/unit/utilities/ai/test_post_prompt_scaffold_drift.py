"""Drift guard for the TEXT-POST prompts (issue #1138).

CLAUDE.md's stated invariant is that the writer side and the checking side can never drift apart —
`AI_TELL_WORDS` is the ONE wordbank both the humanization pass and the slop lint read. The four
post system prompts predated that core and had drifted: they handed the model canned templates
("In my experience as a [Job Title]…", "One key takeaway for me was…") and words the lint itself
bans ("crucial", "journey"). Nothing failed, because nobody was comparing the two sides.

This is that comparison. It captures the REAL system prompt each generator sends and fails the
build if a banned word or a canned scaffold ever gets written back into one.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai.content_alignment import find_ai_tell_words
from cqc_lem.utilities.ai.content_framework import post_writing_directive
from cqc_lem.utilities.ai.slop_lint import find_canned_scaffolds

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _resp(text: str):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = '{"full_name": "Jane", "job_title": "COO"}'
    return p


def _prompts(generator: str, *args):
    """(system prompt, user prompt) actually sent for one post generator."""
    from cqc_lem.utilities.ai import ai_helper

    with patch(f"{_AI}._call_llm", return_value=_resp("draft")) as call, \
         patch(f"{_AI}.get_industry_trend_analysis_based_on_user_profile",
               return_value={"industry": "Logistics", "analysis": "freight rates"}):
        getattr(ai_helper, generator)(*args)
    messages = call.call_args.kwargs["messages"]
    return messages[0]["content"], messages[1]["content"][0]["text"]


# Every generator `create_text_post` can route to, with the positional args it needs.
GENERATORS = [
    ("get_thought_leadership_post_from_ai", (_profile(), "awareness")),
    ("get_industry_news_post_from_ai", (_profile(), "consideration")),
    ("get_personal_story_post_from_ai", (_profile(), "decision")),
    ("generate_engagement_prompt_post", (_profile(), "awareness")),
    ("get_blog_summary_post_from_ai", ("https://example.com/p", "blog body", _profile(),
                                       "awareness")),
    ("get_website_content_post_from_ai", ("page body", "https://example.com/a", _profile(),
                                          "consideration")),
]


class TestPostPromptsMatchTheCheckingSide:
    @pytest.mark.parametrize("generator,args", GENERATORS)
    def test_system_prompt_carries_no_tier1_tell_word(self, generator, args):
        system, _ = _prompts(generator, *args)
        assert find_ai_tell_words(system) == [], (
            f"{generator}'s system prompt uses words the slop lint bans")

    @pytest.mark.parametrize("generator,args", GENERATORS)
    def test_system_prompt_hands_the_writer_no_canned_scaffold(self, generator, args):
        system, _ = _prompts(generator, *args)
        assert find_canned_scaffolds(system) == [], (
            f"{generator}'s system prompt supplies a template the lint flags in the output")

    @pytest.mark.parametrize("generator,args", GENERATORS)
    def test_user_prompt_carries_no_tell_word_of_its_own(self, generator, args):
        # The shared craft directive QUOTES the banned list, so it is removed before grading —
        # otherwise the ban itself would read as a violation.
        _, user = _prompts(generator, *args)
        assert find_ai_tell_words(user.replace(post_writing_directive(), "")) == []

    @pytest.mark.parametrize("generator,args", GENERATORS)
    def test_every_post_prompt_carries_the_scaffold_ban(self, generator, args):
        _, user = _prompts(generator, *args)
        assert "NEVER reach for scaffolding" in user
        assert "answerable ONLY from something specific in THIS post" in user
