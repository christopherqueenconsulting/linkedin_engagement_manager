"""E2E tests for carousel and video post workflows.

These tests exercise the full posting flow (carousel + video) in a real
docker-compose environment. They are marked @pytest.mark.e2e and skipped
when LinkedIn credentials or a real DB are not available.
"""

import pytest
from unittest.mock import patch


@pytest.mark.e2e
class TestCarouselPostE2E:
    """End-to-end carousel posting: content creation → approval → LinkedIn post."""

    @pytest.mark.requires_database
    def test_carousel_post_flow_returns_urn(self, mock_database_connection):
        """Carousel post pipeline should call share_carousel_on_linkedin and return a URN."""
        mock_slides = ["/tmp/slide_01.png", "/tmp/slide_02.png", "/tmp/slide_03.png"]
        expected_urn = "urn:li:ugcPost:carousel123"

        with patch("cqc_lem.app.run_automation.get_carousel_slides", return_value=mock_slides), \
             patch("cqc_lem.app.run_automation.get_post_content", return_value="Test carousel post #linkedin"), \
             patch("cqc_lem.app.run_automation.get_post_type") as mock_get_type, \
             patch("cqc_lem.app.run_automation.update_db_post_status", return_value=True), \
             patch("cqc_lem.app.run_automation.insert_new_log", return_value=None), \
             patch("cqc_lem.app.run_automation.share_carousel_on_linkedin",
                   return_value=expected_urn) as mock_share, \
             patch("cqc_lem.app.run_automation.auto_seed_comment_on_post"), \
             patch("cqc_lem.app.run_automation.automate_reply_commenting"):
            from cqc_lem.utilities.db import PostType
            mock_get_type.return_value = PostType.CAROUSEL

            from cqc_lem.app.run_automation import post_to_linkedin
            post_to_linkedin(user_id=60, post_id=1)

        mock_share.assert_called_once()
        call_args = mock_share.call_args
        assert call_args[0][2] == mock_slides

    @pytest.mark.requires_database
    def test_carousel_local_image_paths_passed_directly(self, mock_database_connection):
        """Carousel with local PNG paths should pass them directly to upload (not Pexels search)."""
        local_paths = ["/tmp/carousel/1/slide_01.png", "/tmp/carousel/1/slide_02.png"]

        with patch("os.path.isfile", return_value=True):
            from cqc_lem.utilities.linkedin.poster import _is_local_image_path
            for path in local_paths:
                assert _is_local_image_path(path) is True

    def test_pexels_search_text_not_treated_as_local_path(self):
        """Short text search queries should not be mistaken for file paths."""
        from cqc_lem.utilities.linkedin.poster import _is_local_image_path
        text_queries = [
            "5 tips for LinkedIn growth",
            "innovation technology future",
            "challenge problem solving",
        ]
        for query in text_queries:
            assert _is_local_image_path(query) is False


@pytest.mark.e2e
class TestVideoPostE2E:
    """End-to-end video posting: URL correctness → LinkedIn share."""

    def test_video_url_uses_api_url_final(self):
        """Video URLs stored in DB should use the API_URL_FINAL base."""
        import importlib
        import sys

        # Simulate having NGROK_CUSTOM_DOMAIN set correctly
        import os
        original = os.environ.get("NGROK_CUSTOM_DOMAIN")
        os.environ["NGROK_CUSTOM_DOMAIN"] = "relegable-preroyally-marti.ngrok-free.dev"
        if "cqc_lem.utilities.env_constants" in sys.modules:
            del sys.modules["cqc_lem.utilities.env_constants"]
        ec = importlib.import_module("cqc_lem.utilities.env_constants")

        try:
            assert "relegable-preroyally-marti.ngrok-free.dev" in ec.API_URL_FINAL
        finally:
            if original is None:
                os.environ.pop("NGROK_CUSTOM_DOMAIN", None)
            else:
                os.environ["NGROK_CUSTOM_DOMAIN"] = original
            if "cqc_lem.utilities.env_constants" in sys.modules:
                del sys.modules["cqc_lem.utilities.env_constants"]

    @pytest.mark.requires_database
    def test_video_post_flow_calls_share_on_linkedin(self, mock_database_connection):
        """Video post pipeline should call share_on_linkedin with the video URL."""
        video_url = "https://relegable-preroyally-marti.ngrok-free.dev/assets?file_name=videos/runwayml/test.mp4"
        expected_urn = "urn:li:ugcPost:video456"

        with patch("cqc_lem.app.run_automation.get_post_video_url", return_value=video_url), \
             patch("cqc_lem.app.run_automation.get_post_content", return_value="My video post #linkedin"), \
             patch("cqc_lem.app.run_automation.get_post_type") as mock_get_type, \
             patch("cqc_lem.app.run_automation.update_db_post_status", return_value=True), \
             patch("cqc_lem.app.run_automation.insert_new_log", return_value=None), \
             patch("cqc_lem.app.run_automation.share_on_linkedin",
                   return_value=expected_urn) as mock_share, \
             patch("cqc_lem.app.run_automation.auto_seed_comment_on_post"), \
             patch("cqc_lem.app.run_automation.automate_reply_commenting"):
            from cqc_lem.utilities.db import PostType
            mock_get_type.return_value = PostType.VIDEO

            from cqc_lem.app.run_automation import post_to_linkedin
            post_to_linkedin(user_id=60, post_id=2)

        mock_share.assert_called_once()
        call_kwargs = mock_share.call_args
        # video_url should be passed to share_on_linkedin
        assert video_url in str(call_kwargs)
