"""Unit tests for the topic-drift fix: when a user declares focus_topics, trend-based post subjects
are ANCHORED to the intersection of their industry and a deterministically ROTATED focus topic —
research queries, the GoogleNews fallback, and the writer prompt all receive the anchor. With no
focus topics, the original profile-industry-only behavior is byte-for-byte unchanged."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"

_FOCUS = ["LLM/AI cost efficiency", "model & complexity routing", "AI reliability & observability"]
_PREFS = {"focus_topics": list(_FOCUS)}


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = '{"full_name": "Jane", "job_title": "CTO"}'
    return p


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


class TestSelectFocusTopic:
    def test_rotates_deterministically_across_topics(self):
        from cqc_lem.utilities.ai.content_alignment import select_focus_topic
        picks = [select_focus_topic(_PREFS, i) for i in range(6)]
        assert picks == _FOCUS + _FOCUS  # full cycle, twice — varied AND reproducible

    def test_same_index_same_topic(self):
        from cqc_lem.utilities.ai.content_alignment import select_focus_topic
        assert select_focus_topic(_PREFS, 42) == select_focus_topic(_PREFS, 42)

    def test_none_when_no_focus_topics(self):
        from cqc_lem.utilities.ai.content_alignment import select_focus_topic
        assert select_focus_topic({}, 1) is None
        assert select_focus_topic(None, 1) is None
        assert select_focus_topic({"focus_topics": ["  ", ""]}, 1) is None

    def test_no_sequence_index_falls_back_to_first_topic_deterministically(self):
        from cqc_lem.utilities.ai.content_alignment import select_focus_topic
        # No stable per-post key → deterministic FIRST topic, never a random pick.
        assert select_focus_topic(_PREFS, None) == _FOCUS[0]
        assert select_focus_topic(_PREFS, None) == select_focus_topic(_PREFS, None)


class TestTrendAnalysisAnchoring:
    def _run(self, prefs=None, sequence_index=None, findings="dated stats"):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}.get_industries_of_profile_from_ai", return_value="Technology"), \
             patch(f"{_AI}.research_topic",
                   return_value={"findings": findings, "sources": []}) as research, \
             patch(f"{_AI}.search_recent_news",
                   return_value={"articles": [{"title": "t", "date": "d", "link": "l"}]}) as gnews, \
             patch(f"{_AI}.get_industry_trend_from_ai", return_value="news analysis") as summarize:
            out = ai_helper.get_industry_trend_analysis_based_on_user_profile(
                _profile(), prefs=prefs, sequence_index=sequence_index)
        return out, research, gnews, summarize

    def test_research_subject_anchored_to_rotated_focus_topic(self):
        out, research, _, _ = self._run(prefs=_PREFS, sequence_index=0)
        subject = research.call_args[0][0]
        assert _FOCUS[0] in subject
        assert "Technology" in subject  # intersection with the profile industry
        assert research.call_args.kwargs["prefs"] is _PREFS
        assert out["focus_topic"] == _FOCUS[0]

    def test_rotation_varies_subject_across_posts(self):
        subjects = []
        for i in range(3):
            _, research, _, _ = self._run(prefs=_PREFS, sequence_index=i)
            subjects.append(research.call_args[0][0])
        assert len(set(subjects)) == 3
        for i, s in enumerate(subjects):
            assert _FOCUS[i] in s

    def test_no_focus_topics_keeps_original_industry_subject(self):
        out, research, _, _ = self._run(prefs={"focus_topics": []}, sequence_index=1)
        assert research.call_args[0][0] == "Recent trends and news in the Technology industry"
        assert out["focus_topic"] is None

    def test_no_prefs_keeps_original_industry_subject(self):
        out, research, _, _ = self._run(prefs=None)
        assert research.call_args[0][0] == "Recent trends and news in the Technology industry"
        assert out["focus_topic"] is None

    def test_googlenews_fallback_searches_the_focus_topic(self):
        out, _, gnews, summarize = self._run(prefs=_PREFS, sequence_index=1, findings="")
        assert gnews.call_args[0][0] == _FOCUS[1]
        assert summarize.call_args[0][0] == _FOCUS[1]
        assert out["focus_topic"] == _FOCUS[1]

    def test_googlenews_fallback_without_focus_uses_industry(self):
        _, _, gnews, summarize = self._run(prefs=None, findings="")
        assert gnews.call_args[0][0] == "Technology"
        assert summarize.call_args[0][0] == "Technology"


class TestGeneratorsThreadAnchoring:
    _TRENDS = {"industry": "Technology", "analysis": "routing is hot", "sources": [],
               "focus_topic": "model & complexity routing"}

    def _gen(self, fn_name, trends=None, **kwargs):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}.get_industry_trend_analysis_based_on_user_profile",
                   return_value=trends or self._TRENDS) as mock_trends, \
             patch(f"{_AI}._call_llm", return_value=_resp("A post")) as m:
            getattr(ai_helper, fn_name)(_profile(), "awareness", prefs=_PREFS, **kwargs)
        content = m.call_args.kwargs["messages"][1]["content"]
        text = content[0]["text"] if isinstance(content, list) else content
        return mock_trends, text

    @pytest.mark.parametrize("fn", ["get_thought_leadership_post_from_ai",
                                    "get_industry_news_post_from_ai",
                                    "get_personal_story_post_from_ai",
                                    "generate_engagement_prompt_post"])
    def test_trend_call_receives_prefs_and_post_id(self, fn):
        mock_trends, _ = self._gen(fn, post_id=31)
        assert mock_trends.call_args.kwargs["prefs"] is _PREFS
        assert mock_trends.call_args.kwargs["sequence_index"] == 31

    @pytest.mark.parametrize("fn", ["get_thought_leadership_post_from_ai",
                                    "get_industry_news_post_from_ai",
                                    "get_personal_story_post_from_ai",
                                    "generate_engagement_prompt_post"])
    def test_subject_anchor_injected_into_prompt(self, fn):
        _, text = self._gen(fn, post_id=31)
        assert "SUBJECT ANCHOR" in text
        assert "model & complexity routing" in text

    def test_no_anchor_line_when_trends_unanchored(self):
        trends = {"industry": "Technology", "analysis": "stuff", "sources": [], "focus_topic": None}
        _, text = self._gen("get_thought_leadership_post_from_ai", trends=trends, post_id=31)
        assert "SUBJECT ANCHOR" not in text

    def test_history_directive_appended_to_prompt(self):
        _, text = self._gen("get_thought_leadership_post_from_ai", post_id=31,
                            history_directive="\n\nUNIQUENESS RULES — avoid opener X\n")
        assert "UNIQUENESS RULES" in text and "avoid opener X" in text

    def test_blog_and_website_generators_accept_history_directive(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("A post")) as m:
            ai_helper.get_blog_summary_post_from_ai(
                "https://b", "content", _profile(), "awareness",
                history_directive="\n\nUNIQUENESS RULES — blog\n")
            ai_helper.get_website_content_post_from_ai(
                "content", "https://w", _profile(), "awareness",
                history_directive="\n\nUNIQUENESS RULES — site\n")
        for call, marker in zip(m.call_args_list, ["blog", "site"]):
            content = call.kwargs["messages"][1]["content"]
            text = content[0]["text"] if isinstance(content, list) else content
            assert f"UNIQUENESS RULES — {marker}" in text
