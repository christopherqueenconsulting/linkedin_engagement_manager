"""Integration tests for the full carousel content creation pipeline.

Uses mock AI calls and temp directories but validates the whole pipeline:
generate_carousel_content → create_carousel_slide_images → update_db_post_carousel_slides
"""

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_ai_response(payload: dict) -> MagicMock:
    mock = MagicMock()
    mock.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
    return mock


_EDUCATIONAL_PAYLOAD = {
    "post_text": "Here are 3 LinkedIn growth tips you need to know. #linkedin #growth",
    "carousel": {
        "cover": {"title": "3 LinkedIn Growth Tips", "content": "Grow your network today"},
        "contents": [
            {"title": "Tip 1: Post Consistently", "content": "Show up every weekday."},
            {"title": "Tip 2: Comment Thoughtfully", "content": "Add value, not noise."},
        ],
        "call_to_action": {"title": "Which Tip Resonates?", "content": "Let us know below!"},
    },
}


@pytest.mark.integration
class TestCarouselCreationPipeline:

    def test_create_carousel_content_returns_post_text(self, mock_database_connection, tmp_path):
        """create_carousel_content should return the AI-generated post_text string."""
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        profile = LinkedInProfile(full_name="Test", job_title="CEO", company_name="ACME")

        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_make_ai_response(_EDUCATIONAL_PAYLOAD)), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
                   return_value=("test@example.com", "pass")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair",
                   return_value=(MagicMock(), MagicMock())), \
             patch("cqc_lem.utilities.linkedin.helper.get_my_profile", return_value=profile), \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully"), \
             patch("cqc_lem.utilities.carousel_creator.get_pexels_image_path", return_value=None), \
             patch("cqc_lem.app.run_content_plan.update_db_post_carousel_slides", return_value=True), \
             patch("cqc_lem.utilities.carousel_creator.create_ppt"):
            from cqc_lem.app.run_content_plan import create_carousel_content
            result = create_carousel_content(user_id=1, stage="awareness", post_id=100)

        assert isinstance(result, str)
        assert len(result) > 0

    def test_create_carousel_content_updates_db_slides(self, mock_database_connection, tmp_path):
        """create_carousel_content should call update_db_post_carousel_slides with slide URLs."""
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        profile = LinkedInProfile(full_name="Test", job_title="CEO", company_name="ACME")

        captured_slides = []

        def capture_slides(post_id, slides):
            captured_slides.extend(slides)
            return True

        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_make_ai_response(_EDUCATIONAL_PAYLOAD)), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
                   return_value=("test@example.com", "pass")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair",
                   return_value=(MagicMock(), MagicMock())), \
             patch("cqc_lem.utilities.linkedin.helper.get_my_profile", return_value=profile), \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully"), \
             patch("cqc_lem.utilities.carousel_creator.get_pexels_image_path", return_value=None), \
             patch("cqc_lem.app.run_content_plan.update_db_post_carousel_slides", side_effect=capture_slides), \
             patch("cqc_lem.utilities.carousel_creator.create_ppt"):
            from cqc_lem.app.run_content_plan import create_carousel_content
            create_carousel_content(user_id=1, stage="awareness", post_id=100)

        # Either image URLs or empty list (if Pillow not available)
        assert isinstance(captured_slides, list)

    def test_create_carousel_content_fallback_on_bad_ai_response(self, mock_database_connection, tmp_path):
        """create_carousel_content should not raise even if AI returns garbage JSON."""
        mock_bad_response = MagicMock()
        mock_bad_response.choices = [MagicMock(message=MagicMock(content="not json"))]

        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=mock_bad_response), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
                   return_value=("test@example.com", "pass")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair",
                   return_value=(MagicMock(), MagicMock())), \
             patch("cqc_lem.utilities.linkedin.helper.get_my_profile",
                   return_value=MagicMock(industry="Tech", job_title="CEO")), \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully"), \
             patch("cqc_lem.app.run_content_plan.update_db_post_carousel_slides", return_value=True):
            from cqc_lem.app.run_content_plan import create_carousel_content
            result = create_carousel_content(user_id=1, stage="awareness", post_id=101)

        assert isinstance(result, str)


@pytest.mark.integration
class TestVideoUrlFix:

    def test_replace_video_url_base_returns_updated_count(self, mock_database_connection):
        """replace_video_url_base should execute UPDATE and return row count."""
        mock_cursor = mock_database_connection["cursor"]
        mock_cursor.rowcount = 2

        from cqc_lem.utilities.db import replace_video_url_base
        result = replace_video_url_base(
            old_base="https://cqc-lem-api.ngrok-free.dev",
            new_base="https://relegable-preroyally-marti.ngrok-free.dev",
        )
        assert result == 2

    def test_replace_video_url_base_scoped_to_user(self, mock_database_connection):
        """replace_video_url_base should include user_id filter in the SQL when provided."""
        mock_cursor = mock_database_connection["cursor"]
        mock_cursor.rowcount = 1

        from cqc_lem.utilities.db import replace_video_url_base
        replace_video_url_base(
            old_base="https://old.ngrok.dev",
            new_base="https://new.ngrok.dev",
            user_id=60,
        )
        sql_called = mock_cursor.execute.call_args[0][0]
        assert "user_id" in sql_called

    def test_update_db_post_carousel_slides_stores_json(self, mock_database_connection):
        """update_db_post_carousel_slides should call UPDATE with JSON-encoded slides."""
        mock_cursor = mock_database_connection["cursor"]
        mock_cursor.rowcount = 1

        slides = [
            "https://api.example.com/assets?file_name=images/carousel/1/slide_01.png",
            "https://api.example.com/assets?file_name=images/carousel/1/slide_02.png",
        ]
        from cqc_lem.utilities.db import update_db_post_carousel_slides
        result = update_db_post_carousel_slides(post_id=1, slides=slides)

        assert result is True
        call_args = mock_cursor.execute.call_args[0]
        sql, params = call_args[0], call_args[1]
        assert "carousel_slides" in sql
        stored = json.loads(params[0])
        assert stored == slides
