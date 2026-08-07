"""Unit tests for AI helper utilities."""

import json
from unittest.mock import MagicMock, call, patch

import pytest


@pytest.mark.unit
class TestGenerateAiResponse:
    def test_calls_api_and_returns_string(self, mock_openai_client, sample_linkedin_profile):
        # The draft has to clear the comment quality contract (issue #617) to be returned at all:
        # grounded in the target post, 2+ sentences, a real value-add, no validation-filler opener.
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            "Your point about AI writing every comment is the one I keep hitting. We tried it on "
            "40 posts last month and replies dropped. What made you write about it now?")
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import generate_ai_response
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            result = generate_ai_response("Write a comment about AI", profile)

            assert mock_openai_client.chat.completions.create.called
            assert isinstance(result, str)

    def test_uses_medium_tier_model(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import generate_ai_response
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            generate_ai_response("Write a comment", profile)

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-medium"

    def test_returns_none_for_empty_response(self, mock_openai_client, sample_linkedin_profile):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = None
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import generate_ai_response
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            result = generate_ai_response("Write a comment", profile)

            assert result is None


@pytest.mark.unit
class TestGetAiMessageRefinement:
    def test_returns_string(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_message_refinement

            result = get_ai_message_refinement("Hey, want to connect?")

            assert isinstance(result, str)
            assert mock_openai_client.chat.completions.create.called

    def test_uses_simple_tier_model(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_message_refinement

            get_ai_message_refinement("Hello!")

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-simple"

    def test_character_limit_in_prompt(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_message_refinement

            get_ai_message_refinement("Hello!", character_limit=150)

            call_args = mock_openai_client.chat.completions.create.call_args
            messages = call_args[1]["messages"]
            all_content = " ".join(str(m.get("content", "")) for m in messages)
            assert "150" in all_content


@pytest.mark.unit
class TestSummarizeRecentActivity:
    def test_returns_none_for_empty_activities(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import summarize_recent_activity
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            main_profile = LinkedInProfile(**sample_linkedin_profile)
            empty_data = {**sample_linkedin_profile, "recent_activities": []}
            activity_profile = LinkedInProfile(**empty_data)

            result = summarize_recent_activity(activity_profile, main_profile)

            assert result is None
            mock_openai_client.chat.completions.create.assert_not_called()

    def test_calls_api_for_profiles_with_activities(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import summarize_recent_activity
            from cqc_lem.utilities.linkedin.profile import LinkedInActivity, LinkedInProfile

            main_profile = LinkedInProfile(**sample_linkedin_profile)
            active_data = {
                **sample_linkedin_profile,
                "recent_activities": [LinkedInActivity(text="Posted about AI")],
            }
            activity_profile = LinkedInProfile(**active_data)

            result = summarize_recent_activity(activity_profile, main_profile)

            assert mock_openai_client.chat.completions.create.called
            assert isinstance(result, str)


@pytest.mark.unit
class TestGetFluxImagePromptFromAi:
    def test_returns_prompt_string(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_flux_image_prompt_from_ai

            result = get_flux_image_prompt_from_ai("Technology innovation post")

            assert isinstance(result, str)

    def test_brief_is_authored_by_medium_tier(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_flux_image_prompt_from_ai

            get_flux_image_prompt_from_ai("content")

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-medium"


@pytest.mark.unit
class TestGetThoughtLeadershipPostFromAi:
    def test_uses_complex_tier_model(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client), \
             patch("cqc_lem.utilities.ai.ai_helper.get_industry_trend_analysis_based_on_user_profile") as mock_trends:
            from cqc_lem.utilities.ai.ai_helper import get_thought_leadership_post_from_ai
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            mock_trends.return_value = {"industry": "Technology", "analysis": "AI is growing"}
            profile = LinkedInProfile(**sample_linkedin_profile)

            get_thought_leadership_post_from_ai(profile, "awareness")

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-complex"


@pytest.mark.unit
class TestModelTierAssignments:
    """Verify all functions use the expected LiteLLM tier aliases."""

    @pytest.mark.parametrize("func_name,expected_model", [
        ("get_ai_message_refinement", "lem-simple"),
        ("get_flux_image_prompt_from_ai", "lem-medium"),
    ])
    def test_simple_tier_functions(self, func_name, expected_model, mock_openai_client, sample_linkedin_profile):
        import cqc_lem.utilities.ai.ai_helper as module
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            fn = getattr(module, func_name)
            try:
                fn("test content")
            except Exception:
                pass  # we only care that model was set correctly
            if mock_openai_client.chat.completions.create.called:
                call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
                assert call_kwargs.get("model") == expected_model


@pytest.mark.unit
class TestGenerateAiResponseTest:
    """Tests for the generate_ai_response_test() standalone demo function."""

    def test_returns_string(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import generate_ai_response_test

            result = generate_ai_response_test()

            assert isinstance(result, str)
            assert mock_openai_client.chat.completions.create.called

    def test_uses_hardcoded_gpt_model(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import generate_ai_response_test

            generate_ai_response_test()

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "gpt-4o-mini"

    def test_raises_on_api_error(self, mock_openai_client):
        mock_openai_client.chat.completions.create.side_effect = Exception("API error")
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import generate_ai_response_test

            with pytest.raises(Exception, match="API error"):
                generate_ai_response_test()


@pytest.mark.unit
class TestGetAiDescriptionOfProfile:
    def test_returns_string(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_description_of_profile
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            result = get_ai_description_of_profile(profile)

            assert isinstance(result, str)
            assert mock_openai_client.chat.completions.create.called

    def test_uses_simple_tier_model(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_description_of_profile
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            get_ai_description_of_profile(profile)

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-simple"

    def test_profile_json_in_prompt(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_description_of_profile
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            get_ai_description_of_profile(profile)

            call_args = mock_openai_client.chat.completions.create.call_args[1]
            all_content = str(call_args["messages"])
            assert "John Doe" in all_content or "Software Engineer" in all_content

    def test_raises_on_api_error(self, mock_openai_client, sample_linkedin_profile):
        mock_openai_client.chat.completions.create.side_effect = Exception("LLM down")
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_description_of_profile
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            with pytest.raises(Exception, match="LLM down"):
                get_ai_description_of_profile(profile)


@pytest.mark.unit
class TestGetIndustriesOfProfileFromAi:
    def test_returns_string(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_industries_of_profile_from_ai
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            result = get_industries_of_profile_from_ai(profile)

            assert isinstance(result, str)
            assert mock_openai_client.chat.completions.create.called

    def test_uses_simple_tier_model(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_industries_of_profile_from_ai
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            get_industries_of_profile_from_ai(profile)

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-simple"

    def test_industry_count_in_prompt(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_industries_of_profile_from_ai
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            get_industries_of_profile_from_ai(profile, industry_count=5)

            call_args = mock_openai_client.chat.completions.create.call_args[1]
            all_content = str(call_args["messages"])
            assert "5" in all_content

    def test_default_industry_count_is_three(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_industries_of_profile_from_ai
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            get_industries_of_profile_from_ai(profile)

            call_args = mock_openai_client.chat.completions.create.call_args[1]
            all_content = str(call_args["messages"])
            assert "3" in all_content

    def test_raises_on_api_error(self, mock_openai_client, sample_linkedin_profile):
        mock_openai_client.chat.completions.create.side_effect = Exception("quota exceeded")
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_industries_of_profile_from_ai
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            with pytest.raises(Exception, match="quota exceeded"):
                get_industries_of_profile_from_ai(profile)


@pytest.mark.unit
class TestGetAiLinkedPostRefinement:
    def test_returns_string(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_linked_post_refinement

            result = get_ai_linked_post_refinement("This is a draft LinkedIn post.")

            assert isinstance(result, str)
            assert mock_openai_client.chat.completions.create.called

    def test_uses_medium_tier_model(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_linked_post_refinement

            get_ai_linked_post_refinement("Draft post content")

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-medium"

    def test_character_limit_in_prompt(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_linked_post_refinement

            get_ai_linked_post_refinement("Draft post content", character_limit=2000)

            call_args = mock_openai_client.chat.completions.create.call_args[1]
            all_content = str(call_args["messages"])
            assert "2000" in all_content

    def test_zero_character_limit_omits_limit_string(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_ai_linked_post_refinement

            get_ai_linked_post_refinement("Draft post content", character_limit=0)

            call_args = mock_openai_client.chat.completions.create.call_args[1]
            all_content = str(call_args["messages"])
            assert "less than or equal to" not in all_content


@pytest.mark.unit
class TestGetVideoContentFromAi:
    def test_returns_string(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_video_content_from_ai
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            result = get_video_content_from_ai(profile, "awareness")

            assert isinstance(result, str)
            assert mock_openai_client.chat.completions.create.called

    def test_uses_complex_tier_model(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_video_content_from_ai
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile

            profile = LinkedInProfile(**sample_linkedin_profile)
            get_video_content_from_ai(profile, "decision")

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-complex"


@pytest.mark.unit
class TestGetIndustryTrendFromAi:
    def test_returns_string(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_industry_trend_from_ai

            articles = [
                {"title": "AI grows rapidly", "date": "2024-01-01", "link": "https://example.com/1"},
                {"title": "ML advances", "date": "2024-01-02", "link": "https://example.com/2"},
            ]
            result = get_industry_trend_from_ai("Technology", articles)

            assert isinstance(result, str)
            assert mock_openai_client.chat.completions.create.called

    def test_uses_medium_tier_model(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_industry_trend_from_ai

            get_industry_trend_from_ai("Finance", [])

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-medium"

    def test_industry_name_in_prompt(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_industry_trend_from_ai

            get_industry_trend_from_ai("Healthcare", [])

            call_args = mock_openai_client.chat.completions.create.call_args[1]
            all_content = str(call_args["messages"])
            assert "Healthcare" in all_content


@pytest.mark.unit
class TestGetRunwayMlVideoPromptFromAi:
    def test_returns_string(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai

            result = get_runway_ml_video_prompt_from_ai(
                "A post about innovation", "A futuristic cityscape"
            )

            assert isinstance(result, str)
            assert mock_openai_client.chat.completions.create.called

    def test_uses_simple_tier_model(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai

            get_runway_ml_video_prompt_from_ai("post content", "image prompt")

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-simple"


@pytest.mark.unit
class TestAiCheckMessageHistory:
    def test_returns_string(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import ai_check_message_history

            result = ai_check_message_history(
                message_history_json='[{"sender": "Alice", "text": "Hello!"}]',
                main_focus="networking",
                message="Looking forward to connecting",
                user_name="Bob",
            )

            assert isinstance(result, str)
            assert mock_openai_client.chat.completions.create.called

    def test_uses_simple_tier_model(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import ai_check_message_history

            ai_check_message_history(
                message_history_json="[]",
                main_focus="sales",
                message="Are you interested?",
            )

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-simple"

    def test_main_focus_in_prompt(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import ai_check_message_history

            ai_check_message_history(
                message_history_json="[]",
                main_focus="cloud computing partnerships",
                message="Let's connect!",
            )

            call_args = mock_openai_client.chat.completions.create.call_args[1]
            all_content = str(call_args["messages"])
            assert "cloud computing partnerships" in all_content


@pytest.mark.unit
class TestGenerateCarouselContent:
    """Tests for generate_carousel_content(). The function lazy-imports db and selenium helpers
    inside its body, so we patch those at their source module paths.
    """

    def _make_carousel_response(self, mock_client, post_text="Check this out!", carousel=None):
        if carousel is None:
            carousel = {
                "cover": {"title": "AI Trends", "content": "Explore what's next."},
                "insights": [{"title": "Key Insight", "content": "AI is transforming industries."}],
                "call_to_action": {"title": "Learn More", "content": "Connect with me."},
            }
        payload = {"post_text": post_text, "carousel": carousel}
        mock_client.chat.completions.create.return_value.choices[0].message.content = json.dumps(payload)

    def _db_patches(self):
        """Context manager stack that prevents db/selenium calls from running."""
        return (
            patch("cqc_lem.utilities.db.get_user_password_pair_by_id", side_effect=Exception("no db")),
            patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", side_effect=Exception("no driver")),
        )

    def test_returns_tuple_of_str_and_dict(self, mock_openai_client):
        self._make_carousel_response(mock_openai_client)
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", side_effect=Exception("no db")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", side_effect=Exception("no driver")):
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content

            post_text, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")

            assert isinstance(post_text, str)
            assert isinstance(carousel_dict, dict)

    def test_uses_complex_tier_model(self, mock_openai_client):
        self._make_carousel_response(mock_openai_client)
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", side_effect=Exception("no db")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", side_effect=Exception("no driver")):
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content

            generate_carousel_content(user_id=1, stage="consideration")

            call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
            assert call_kwargs.get("model") == "lem-complex"

    def test_invalid_json_returns_defaults(self, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = "not-json"
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", side_effect=Exception("no db")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", side_effect=Exception("no driver")):
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content

            post_text, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")

            assert isinstance(post_text, str)
            assert carousel_dict == {}

    @pytest.mark.parametrize("stage,expected_hint_fragment", [
        ("awareness", "EducationalContentCarousel"),
        ("consideration", "CaseStudyCarousel"),
        ("decision", "ProductDemoCarousel"),
        ("other", "IndustryInsightsCarousel"),
    ])
    def test_stage_selects_schema_hint(self, mock_openai_client, stage, expected_hint_fragment):
        self._make_carousel_response(mock_openai_client)
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", side_effect=Exception("no db")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", side_effect=Exception("no driver")):
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content

            generate_carousel_content(user_id=1, stage=stage)

            call_args = mock_openai_client.chat.completions.create.call_args[1]
            all_content = str(call_args["messages"])
            assert expected_hint_fragment in all_content
