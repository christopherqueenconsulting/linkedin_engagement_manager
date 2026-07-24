"""Unit tests for the 2026 hashtag + anti-engagement-bait policy (issue #393 / C4): generated
content is hashtag-free unless the user explicitly opts in, and no CTA the framework can assign
asks for a reflex action the algorithm demotes."""

import pytest

from cqc_lem.utilities.ai import content_framework as cf
from cqc_lem.utilities.linkedin_formatter import contains_engagement_bait

pytestmark = pytest.mark.unit


class TestCtaMenusAreBaitFree:
    @pytest.mark.parametrize("menu_name", ["CTA_STYLES", "POST_CTA_STYLES"])
    def test_no_cta_style_asks_for_a_reflex_action(self, menu_name):
        for key, meta in getattr(cf, menu_name).items():
            for field in ("label", "guidance"):
                assert not contains_engagement_bait(meta[field]), \
                    f"{menu_name}[{key}].{field} reads as engagement bait: {meta[field]}"

    def test_every_menu_entry_still_drives_a_real_conversation(self):
        for key, meta in cf.POST_CTA_STYLES.items():
            text = meta["guidance"].lower()
            assert any(word in text for word in ("question", "comment", "reply", "why", "report back")), \
                f"POST_CTA_STYLES[{key}] no longer invites a response"

    def test_format_structures_and_guidance_are_bait_free(self):
        for menu_name in ("NEWSLETTER_FORMATS", "POST_FORMATS", "COMMENT_FORMATS"):
            for key, meta in getattr(cf, menu_name).items():
                assert not contains_engagement_bait(meta["guidance"]), f"{menu_name}[{key}]"
                for step in meta["structure"]:
                    assert not contains_engagement_bait(step), f"{menu_name}[{key}]: {step}"

    def test_poll_cta_demands_the_reason_not_a_bare_vote(self):
        guidance = cf.POST_CTA_STYLES["poll_prompt"]["guidance"].lower()
        assert "why" in guidance and "bait" in guidance


class TestCtaPolicyDirective:
    def test_names_the_banned_patterns_and_is_injected_with_every_cta(self):
        directive = cf.cta_policy_directive()
        assert "NEVER engagement bait" in directive
        assert cf.ENGAGEMENT_BAIT_EXAMPLES[0] in directive
        blueprint = cf.select_blueprint("post")
        assert directive in cf.blueprint_directive("post", blueprint)

    def test_post_craft_rules_carry_the_policy(self):
        rules = cf.post_writing_directive()
        assert "NEVER engagement bait" in rules
        assert "Hashtags are OFF unless" in rules


class TestHashtagDirective:
    @pytest.mark.parametrize("prefs", [None, {}, {"use_hashtags": False}, {"use_hashtags": 0}])
    def test_default_is_no_hashtags(self, prefs):
        assert "Do NOT include any hashtags" in cf.hashtag_directive(prefs)

    def test_opt_in_stays_minimal(self):
        directive = cf.hashtag_directive({"use_hashtags": True})
        assert "Do NOT" not in directive
        assert f"at most {cf.HASHTAG_MAX}" in directive
        assert cf.HASHTAG_MAX <= 3

    def test_style_directive_uses_the_shared_policy(self):
        from cqc_lem.utilities.ai.content_alignment import style_directive
        assert cf.hashtag_directive(None) in style_directive({"tone": "warm"})
        assert cf.hashtag_directive({"use_hashtags": True}) in style_directive(
            {"tone": "warm", "use_hashtags": True})


class TestGeneratorsDefaultToNoHashtags:
    """Unset prefs used to mean 'hashtags ON' in these prompt builders — the opposite of the 2026
    default. Each prompt must now come back hashtag-free unless the user opted in."""

    @staticmethod
    def _prompt_text(mock_llm) -> str:
        parts = []
        for message in mock_llm.call_args.kwargs["messages"]:
            content = message["content"]
            if isinstance(content, list):
                parts += [c.get("text", "") for c in content]
            else:
                parts.append(str(content))
        return "\n".join(parts)

    @pytest.fixture
    def mock_llm(self):
        from unittest.mock import MagicMock, patch
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="post text"))]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=response) as mock:
            yield mock

    def test_post_refinement_strips_hashtags_when_prefs_unset(self, mock_llm):
        from cqc_lem.utilities.ai.ai_helper import get_ai_linked_post_refinement
        get_ai_linked_post_refinement("Some draft post")
        assert "Do NOT include any hashtags" in self._prompt_text(mock_llm)

    def test_post_refinement_honors_opt_in(self, mock_llm):
        from cqc_lem.utilities.ai.ai_helper import get_ai_linked_post_refinement
        get_ai_linked_post_refinement("Some draft post", prefs={"use_hashtags": True})
        prompt = self._prompt_text(mock_llm)
        assert f"at most {cf.HASHTAG_MAX} highly relevant hashtags" in prompt

    def test_blog_summary_prompt_has_no_hashtags_when_prefs_unset(self, mock_llm):
        from cqc_lem.utilities.ai.ai_helper import get_blog_summary_post_from_ai
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        profile = LinkedInProfile(full_name="Test User", job_title="CTO", company_name="ACME",
                                  industry="Technology")
        get_blog_summary_post_from_ai("https://example.com/post", "Blog body", profile, "awareness")
        assert "Do NOT include any hashtags" in self._prompt_text(mock_llm)
