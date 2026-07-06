"""Unit tests proving POSTS and COMMENTS now run on the same unified framework/research/alignment
core the newsletter uses: archetype blueprints injected into post prompts, per-comment angle
rotation grounded in the target post, shared research routing with the comment-cost gate, and no
resurrection of the deleted one-size-fits-all viral suffix."""

import pytest
from unittest.mock import MagicMock, patch

from cqc_lem.utilities.ai import content_framework as fw

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_CLIENT = "cqc_lem.utilities.ai.client.client"
_DIRECT = "cqc_lem.utilities.ai.tools.search_with_perplexity"


@pytest.fixture(autouse=True)
def _clean_toggles(monkeypatch):
    for name in ("CONTENT_RESEARCH_ENABLED", "NEWSLETTER_RESEARCH_ENABLED",
                 "POST_RESEARCH_ENABLED", "COMMENT_RESEARCH_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = '{"full_name": "Jane", "job_title": "COO"}'
    return p


def _messages(mock_call):
    return mock_call.call_args.kwargs["messages"]


def _user_text(mock_call):
    content = _messages(mock_call)[1]["content"]
    return content[0]["text"] if isinstance(content, list) else content


class TestPostGeneratorsUseUnifiedFramework:
    _TRENDS = {"industry": "Logistics", "analysis": "robots everywhere", "sources": []}

    def _gen(self, blueprint=None):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}.get_industry_trend_analysis_based_on_user_profile",
                   return_value=self._TRENDS), \
             patch(f"{_AI}._call_llm", return_value=_resp("A post")) as m:
            ai_helper.get_thought_leadership_post_from_ai(
                _profile(), "awareness", prefs={"focus_topics": ["3PL"]}, blueprint=blueprint)
        return _user_text(m)

    def test_blueprint_directive_injected(self):
        blueprint = fw.select_blueprint("post", recent_formats=["personal_lesson"])
        text = self._gen(blueprint)
        assert "THIS POST'S ASSIGNED BLUEPRINT" in text
        assert fw.POST_FORMATS[blueprint["format"]]["label"] in text
        assert fw.HOOK_STYLES[blueprint["hook_style"]]["guidance"] in text

    def test_shared_craft_rules_replace_viral_suffix(self):
        text = self._gen(fw.select_blueprint("post"))
        assert "LinkedIn post craft rules" in text
        assert "210 characters" in text
        # The deleted one-shape-fits-all template must never come back.
        assert "Viral Post Creation Framework" not in text
        assert "SUCKS" not in text
        assert "10 relevant hashtags" not in text

    def test_alignment_and_guardrail_still_present(self):
        text = self._gen(fw.select_blueprint("post"))
        assert "Content alignment rules" in text
        assert "self-promotion" in text.lower()
        assert "3PL" in text

    def test_all_text_generators_accept_blueprint(self):
        from cqc_lem.utilities.ai import ai_helper
        bp = {"format": "tactical_list", "hook_style": "surprising_stat", "cta_style": "save_worthy"}
        with patch(f"{_AI}.get_industry_trend_analysis_based_on_user_profile",
                   return_value=self._TRENDS), \
             patch(f"{_AI}._call_llm", return_value=_resp("A post")) as m:
            ai_helper.get_industry_news_post_from_ai(_profile(), "awareness", blueprint=bp)
            ai_helper.get_personal_story_post_from_ai(_profile(), "awareness", blueprint=bp)
            ai_helper.generate_engagement_prompt_post(_profile(), "awareness", blueprint=bp)
            ai_helper.get_blog_summary_post_from_ai("https://b", "content", _profile(), "awareness",
                                                    blueprint=bp)
            ai_helper.get_website_content_post_from_ai("content", "https://w", _profile(),
                                                       "awareness", blueprint=bp)
        for call in m.call_args_list:
            content = call.kwargs["messages"][1]["content"]
            text = content[0]["text"] if isinstance(content, list) else content
            assert "THIS POST'S ASSIGNED BLUEPRINT" in text
            assert fw.POST_FORMATS["tactical_list"]["label"] in text


class TestPostResearchUnified:
    def test_trend_analysis_prefers_shared_research(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}.get_industries_of_profile_from_ai", return_value="Logistics"), \
             patch(f"{_AI}.research_topic",
                   return_value={"findings": "78% of fleets (2026, FreightWaves)...",
                                 "sources": [{"url": "https://fw"}]}) as research, \
             patch(f"{_AI}.search_recent_news") as gnews, \
             patch(f"{_AI}.get_industry_trend_from_ai") as summarize:
            out = ai_helper.get_industry_trend_analysis_based_on_user_profile(_profile())
        research.assert_called_once()
        assert research.call_args.kwargs["content_type"] == "post"
        assert out["analysis"].startswith("78% of fleets")
        assert out["sources"] == [{"url": "https://fw"}]
        gnews.assert_not_called()        # no duplicate news path
        summarize.assert_not_called()    # findings ARE the analysis — no second LLM call

    def test_trend_analysis_falls_back_to_googlenews(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}.get_industries_of_profile_from_ai", return_value="Logistics"), \
             patch(f"{_AI}.research_topic", return_value={"findings": "", "sources": []}), \
             patch(f"{_AI}.search_recent_news",
                   return_value={"articles": [{"title": "t", "date": "d", "link": "l"}]}), \
             patch(f"{_AI}.get_industry_trend_from_ai", return_value="news analysis"):
            out = ai_helper.get_industry_trend_analysis_based_on_user_profile(_profile())
        assert out["analysis"] == "news analysis"
        assert out["sources"] == []


class TestCommentsUseUnifiedFramework:
    def test_comment_gets_an_assigned_angle_by_default(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Sharp take — what changed?")) as m:
            out = ai_helper.generate_ai_response("POST ABOUT ROBOTS", _profile())
        assert out == "Sharp take — what changed?"
        system = _messages(m)[0]["content"]
        assert "THIS COMMENT'S ASSIGNED BLUEPRINT" in system
        assert any(meta["label"] in system for meta in fw.COMMENT_FORMATS.values())

    def test_explicit_blueprint_honored(self):
        from cqc_lem.utilities.ai import ai_helper
        bp = {"format": "storyteller"}
        with patch(f"{_AI}._call_llm", return_value=_resp("ok")) as m:
            ai_helper.generate_ai_response("POST", _profile(), blueprint=bp)
        assert fw.COMMENT_FORMATS["storyteller"]["label"] in _messages(m)[0]["content"]

    def test_target_post_grounding_contract_unchanged(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("ok")) as m:
            ai_helper.generate_ai_response("POST ABOUT WAREHOUSE ROBOTICS", _profile(),
                                           prefs={"focus_topics": ["logistics"]})
        system = _messages(m)[0]["content"]
        text = _user_text(m)
        assert "GROUND EVERYTHING IN THE TARGET POST" in system
        assert "self-promotion" in system.lower()          # hard guardrail stays for comments
        assert "POST ABOUT WAREHOUSE ROBOTICS" in text
        assert "Build a genuine relationship" in text       # LEM relationship purpose
        assert "END with a single, natural, open-ended question" in system

    def test_normal_comment_fires_no_research_call(self):
        """The cost contract end-to-end: default comment generation touches NO research API."""
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("ok")), \
             patch(_CLIENT) as client, patch(_DIRECT) as direct:
            ai_helper.generate_ai_response("A POST", _profile())
        client.chat.completions.create.assert_not_called()
        direct.assert_not_called()

    def test_comment_research_woven_in_when_enabled(self, monkeypatch):
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("ok")) as m, \
             patch(f"{_AI}.research_topic",
                   return_value={"findings": "61% of ops teams (2026, Gartner)", "sources": []}) as research:
            ai_helper.generate_ai_response("A POST", _profile())
        research.assert_called_once()
        assert research.call_args.kwargs["content_type"] == "comment"
        assert "61% of ops teams" in _user_text(m)
        assert "Optional background facts" in _user_text(m)

    def test_reply_to_comment_mode_skips_angle_blueprint(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("ok")) as m:
            ai_helper.generate_ai_response("A POST", _profile(), post_comment="their comment")
        assert "ASSIGNED BLUEPRINT" not in _messages(m)[0]["content"]
