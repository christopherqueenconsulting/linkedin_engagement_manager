"""Configurability-audit tests: generation/refinement prompts must be driven by the user's
engagement preferences (use_emojis / use_hashtags / tone) when set. Hashtags follow the shared
2026 policy (issue #393) — OFF unless the user explicitly opted in, in every prompt builder.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _messages_text(mock_llm) -> str:
    return str(mock_llm.call_args.kwargs.get("messages") or mock_llm.call_args.args)


class TestPostRefinementPrefs:
    def _run(self, prefs=None):
        from cqc_lem.utilities.ai.ai_helper import get_ai_linked_post_refinement
        with patch(f"{_AI}._call_llm", return_value=_resp("refined")) as llm:
            get_ai_linked_post_refinement("Draft post", prefs=prefs)
        return _messages_text(llm)

    def test_default_keeps_emoji_line_but_drops_hashtags(self):
        text = self._run(prefs=None)
        assert "Use emojis (✅, 👉, 🔑, 💡)" in text
        assert "Do NOT include any hashtags" in text
        assert "Do NOT use any emojis" not in text

    def test_emojis_off_instructs_removal(self):
        text = self._run(prefs={"use_emojis": False, "use_hashtags": True})
        assert "Do NOT use any emojis" in text
        assert "Use emojis (✅, 👉, 🔑, 💡)" not in text
        assert "at most 3 highly relevant hashtags" in text

    def test_hashtags_off_instructs_removal(self):
        text = self._run(prefs={"use_emojis": True, "use_hashtags": False})
        assert "Do NOT include any hashtags" in text
        assert "at most 3 highly relevant hashtags" not in text
        assert "Use emojis (✅, 👉, 🔑, 💡)" in text

    def test_configured_tone_is_preserved(self):
        text = self._run(prefs={"use_emojis": True, "use_hashtags": True, "tone": "witty"})
        assert "witty tone" in text
        assert "Preserve the author's configured tone" in text

    def test_no_tone_no_preserve_section(self):
        text = self._run(prefs={"use_emojis": True, "use_hashtags": True})
        assert "Preserve the author's configured tone" not in text


class TestOptimizePostHookPrefs:
    def test_no_prefs_no_style_requirements(self):
        from cqc_lem.utilities.ai.ai_helper import optimize_post_hook
        with patch(f"{_AI}._call_llm", return_value=_resp("hooked")) as llm:
            optimize_post_hook("post body")
        assert "Style requirements" not in _messages_text(llm)

    def test_prefs_reach_the_rewrite_prompt(self):
        from cqc_lem.utilities.ai.ai_helper import optimize_post_hook
        prefs = {"tone": "casual", "use_emojis": False, "use_hashtags": False}
        with patch(f"{_AI}._call_llm", return_value=_resp("hooked")) as llm:
            optimize_post_hook("post body", prefs=prefs)
        text = _messages_text(llm)
        assert "Style requirements" in text
        assert "Do not use emojis." in text
        assert "casual tone" in text


class TestSummaryPostHashtagPrefs:
    def _profile(self, sample_linkedin_profile):
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        return LinkedInProfile(**sample_linkedin_profile)

    def test_blog_summary_default_has_no_hashtags(self, sample_linkedin_profile):
        from cqc_lem.utilities.ai.ai_helper import get_blog_summary_post_from_ai
        with patch(f"{_AI}._call_llm", return_value=_resp("post")) as llm:
            get_blog_summary_post_from_ai("https://x.test/a", "blog body",
                                          self._profile(sample_linkedin_profile), "awareness")
        text = _messages_text(llm)
        assert "Do NOT include any hashtags" in text

    def test_blog_summary_hashtags_off(self, sample_linkedin_profile):
        from cqc_lem.utilities.ai.ai_helper import get_blog_summary_post_from_ai
        with patch(f"{_AI}._call_llm", return_value=_resp("post")) as llm:
            get_blog_summary_post_from_ai("https://x.test/a", "blog body",
                                          self._profile(sample_linkedin_profile), "awareness",
                                          prefs={"use_hashtags": False, "use_emojis": False})
        text = _messages_text(llm)
        assert "Do NOT include any hashtags" in text
        assert "Do NOT use any emojis" in text

    def test_website_post_hashtags_off(self, sample_linkedin_profile):
        from cqc_lem.utilities.ai.ai_helper import get_website_content_post_from_ai
        with patch(f"{_AI}._call_llm", return_value=_resp("post")) as llm:
            get_website_content_post_from_ai("site body", "https://x.test",
                                             self._profile(sample_linkedin_profile), "awareness",
                                             prefs={"use_hashtags": False, "use_emojis": True})
        text = _messages_text(llm)
        assert "Do NOT include any hashtags" in text
        assert "You can use emojis" in text

    def test_website_post_default_has_no_hashtags(self, sample_linkedin_profile):
        from cqc_lem.utilities.ai.ai_helper import get_website_content_post_from_ai
        with patch(f"{_AI}._call_llm", return_value=_resp("post")) as llm:
            get_website_content_post_from_ai("site body", "https://x.test",
                                             self._profile(sample_linkedin_profile), "awareness")
        assert "Do NOT include any hashtags" in _messages_text(llm)


class TestCarouselHashtagPref:
    def _payload(self):
        return json.dumps({"post_text": "Carousel intro",
                           "carousel": {"cover": {"title": "T", "content": "C"},
                                        "insights": [{"title": "I", "content": "C"}],
                                        "call_to_action": {"title": "Go", "content": "Now"}}})

    def _run(self, prefs):
        from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
        with patch(f"{_AI}._call_llm", return_value=_resp(self._payload())) as llm, \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
                   side_effect=Exception("no db")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair",
                   side_effect=Exception("no driver")), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None):
            generate_carousel_content(user_id=1, stage="awareness", prefs=prefs)
        return _messages_text(llm)

    def test_default_has_no_hashtags(self):
        text = self._run(prefs=None)
        assert "Do NOT include any hashtags" in text
        assert "End with 5-10 relevant hashtags" not in text

    def test_hashtags_off(self):
        text = self._run(prefs={"use_hashtags": False})
        assert "Do NOT include any hashtags" in text
        assert "End with 5-10 relevant hashtags" not in text

    def test_hashtags_opt_in_is_capped(self):
        text = self._run(prefs={"use_hashtags": True})
        assert "at most 3 highly relevant hashtags" in text

    def test_falls_back_to_cached_profile_before_generic_persona(self):
        from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        cached = LinkedInProfile(full_name="Jane Realtor", job_title="Realtor",
                                 industry="Real Estate")
        with patch(f"{_AI}._call_llm", return_value=_resp(self._payload())) as llm, \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
                   side_effect=Exception("no db")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair",
                   side_effect=Exception("no driver")), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user",
                   return_value=cached):
            generate_carousel_content(user_id=1, stage="awareness")
        text = _messages_text(llm)
        assert "Realtor" in text
        assert "Real Estate" in text


class TestIndustryFallback:
    def test_profile_industry_beats_generic_technology(self, sample_linkedin_profile):
        from cqc_lem.utilities.ai.ai_helper import get_thought_leadership_post_from_ai
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        profile = LinkedInProfile(**{**sample_linkedin_profile, "industry": "Real Estate"})
        with patch(f"{_AI}._call_llm", return_value=_resp("post")) as llm, \
             patch(f"{_AI}.get_industry_trend_analysis_based_on_user_profile",
                   return_value={"industry": "", "analysis": "trend data", "sources": [],
                                 "focus_topic": None}):
            get_thought_leadership_post_from_ai(profile, "awareness",
                                                profile_synthesis="voice brief")
        text = _messages_text(llm)
        assert "the Real Estate industry" in text
        assert "the Technology industry" not in text

    def test_fixed_example_industries_removed_from_system_prompt(self, sample_linkedin_profile):
        from cqc_lem.utilities.ai.ai_helper import get_thought_leadership_post_from_ai
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        profile = LinkedInProfile(**sample_linkedin_profile)
        with patch(f"{_AI}._call_llm", return_value=_resp("post")) as llm, \
             patch(f"{_AI}.get_industry_trend_analysis_based_on_user_profile",
                   return_value={"industry": "Technology", "analysis": "trend data",
                                 "sources": [], "focus_topic": None}):
            get_thought_leadership_post_from_ai(profile, "awareness",
                                                profile_synthesis="voice brief")
        text = _messages_text(llm)
        assert "Healthcare Technology" not in text
        assert "Chief Technology Officer" not in text
