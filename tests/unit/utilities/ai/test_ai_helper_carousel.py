"""Unit tests for generate_carousel_content() in ai_helper.py."""

import json
import pytest
from unittest.mock import MagicMock, patch


def _make_llm_mock(json_content: dict) -> MagicMock:
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content=json.dumps(json_content)))]
    return mock_response


def _educational_carousel_json():
    return {
        "post_text": "Here are 5 tips to grow faster on LinkedIn. #linkedin #growth",
        "carousel": {
            "cover": {"title": "5 Tips for Growth", "content": "Boost your LinkedIn presence"},
            "contents": [
                {"title": "Tip 1: Post Daily", "content": "Consistency beats perfection."},
                {"title": "Tip 2: Engage Comments", "content": "Reply to every comment you get."},
            ],
            "call_to_action": {"title": "What's Your Tip?", "content": "Share in the comments!"},
        },
    }


@pytest.mark.unit
class TestGenerateCarouselContent:

    def _patch_llm(self, payload):
        return patch(
            "cqc_lem.utilities.ai.ai_helper._call_llm",
            return_value=_make_llm_mock(payload),
        )

    def _patch_profile(self):
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        profile = LinkedInProfile(full_name="Test User", job_title="CTO", company_name="ACME", industry="Technology")
        # generate_carousel_content uses local imports from their source modules
        from contextlib import ExitStack
        import contextlib

        @contextlib.contextmanager
        def _multi():
            with patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
                       return_value=("test@example.com", "pass")), \
                 patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair",
                       return_value=(MagicMock(), MagicMock())), \
                 patch("cqc_lem.utilities.linkedin.helper.get_my_profile", return_value=profile), \
                 patch("cqc_lem.utilities.selenium_util.quit_gracefully"):
                yield

        return _multi()

    def test_returns_tuple_of_post_text_and_dict(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload), self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            post_text, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")
        assert isinstance(post_text, str)
        assert len(post_text) > 0
        assert isinstance(carousel_dict, dict)

    def test_awareness_stage_returns_educational_carousel(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload), self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            _, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")
        assert "cover" in carousel_dict
        assert "contents" in carousel_dict
        assert "call_to_action" in carousel_dict

    def test_post_text_extracted_correctly(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload), self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            post_text, _ = generate_carousel_content(user_id=1, stage="awareness")
        assert post_text == payload["post_text"]

    def test_invalid_json_from_llm_returns_defaults(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="not json at all"))]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=mock_response), \
             self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            post_text, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")
        assert isinstance(post_text, str)
        assert isinstance(carousel_dict, dict)

    def test_guidance_reaches_the_slide_prompt(self):
        """Issue #794: the regenerate flow's free-text guidance must steer the SLIDES, not just the
        caption — and must not license an invented number to satisfy the request."""
        payload = _educational_carousel_json()
        with self._patch_llm(payload) as mock_llm, self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            generate_carousel_content(user_id=1, stage="awareness", guidance="drop the pricing slide")
        prompt = mock_llm.call_args.kwargs["messages"][1]["content"][0]["text"]
        assert "drop the pricing slide" in prompt
        assert "every slide" in prompt
        assert "never invent a number" in prompt

    def test_no_guidance_adds_no_revision_block(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload) as mock_llm, self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            generate_carousel_content(user_id=1, stage="awareness", guidance="   ")
        prompt = mock_llm.call_args.kwargs["messages"][1]["content"][0]["text"]
        assert "AUTHOR'S REVISION REQUEST" not in prompt

    def test_uses_lem_complex_model(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload) as mock_llm, self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            generate_carousel_content(user_id=1, stage="awareness")
        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs.get("model") == "lem-complex"

    def test_profile_fetch_failure_falls_back_gracefully(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", side_effect=Exception("DB down")):
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            post_text, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")
        assert isinstance(post_text, str)


@pytest.mark.unit
def test_wall_of_text_caption_is_reflowed_and_capped():
    """Regression (post 72): a caption that comes back as a long single paragraph must be reflowed
    into scannable paragraphs and hard-capped under the LinkedIn limit."""
    from contextlib import contextmanager
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile

    wall = " ".join(f"Intro sentence number {i} is here." for i in range(150))  # long, no line breaks
    assert "\n\n" not in wall and len(wall) > 3000
    payload = _educational_carousel_json()
    payload["post_text"] = wall

    profile = LinkedInProfile(full_name="Test User", job_title="CTO", company_name="ACME", industry="Technology")

    @contextmanager
    def _env():
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_make_llm_mock(payload)), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", return_value=("t@e.com", "p")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch("cqc_lem.utilities.linkedin.helper.get_my_profile", return_value=profile), \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully"):
            yield

    with _env():
        from cqc_lem.utilities.ai.ai_helper import generate_carousel_content, _CAROUSEL_CAPTION_MAX_CHARS
        post_text, _ = generate_carousel_content(user_id=1, stage="awareness")

    assert len(post_text) <= _CAROUSEL_CAPTION_MAX_CHARS   # hard-capped
    assert "\n\n" in post_text                             # reflowed into paragraphs
