"""Unit tests for carousel regeneration / stale-slide healing."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_DB = "cqc_lem.utilities.db"


class TestCarouselSlidesAreRealImages:
    def test_real_image_urls(self):
        from cqc_lem.app.run_content_plan import _carousel_slides_are_real_images
        assert _carousel_slides_are_real_images(
            ["https://x/api/assets?file_name=images/carousel/9/slide_01.png"]) is True

    def test_local_png_paths(self):
        from cqc_lem.app.run_content_plan import _carousel_slides_are_real_images
        assert _carousel_slides_are_real_images(["/app/assets/slide_01.png"]) is True

    def test_text_titles_are_not_images(self):
        from cqc_lem.app.run_content_plan import _carousel_slides_are_real_images
        assert _carousel_slides_are_real_images(["How We Scaled Operations", "Revenue Grew"]) is False

    def test_empty(self):
        from cqc_lem.app.run_content_plan import _carousel_slides_are_real_images
        assert _carousel_slides_are_real_images([]) is False
        assert _carousel_slides_are_real_images(None) is False


class TestPostMissingRequiredAssetCarousel:
    def test_text_slides_count_as_missing(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        from cqc_lem.utilities.db import PostType
        with patch(f"{_DB}.get_post_carousel_slides", return_value=["Title A", "Title B"]):
            assert _post_missing_required_asset(5, PostType.CAROUSEL.value, None) is True

    def test_real_image_slides_not_missing(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        from cqc_lem.utilities.db import PostType
        with patch(f"{_DB}.get_post_carousel_slides",
                   return_value=["https://x/api/assets?file_name=images/carousel/5/slide_01.png"]):
            assert _post_missing_required_asset(5, PostType.CAROUSEL.value, None) is False


class TestRegeneratePostCarouselTask:
    def test_heals_error_to_approved_on_real_images(self):
        from cqc_lem.app.run_content_plan import regenerate_post_carousel_task
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_DB}.get_post_user_id", return_value=1), \
             patch(f"{_DB}.get_post_buyer_stage", return_value="awareness"), \
             patch(f"{_DB}.update_db_post_content"), \
             patch(f"{_DB}.get_post_carousel_slides",
                   return_value=["https://x/api/assets?file_name=images/carousel/5/slide_01.png"]), \
             patch(f"{_DB}.get_post_status", return_value="error"), \
             patch(f"{_DB}.update_db_post_status") as mock_status, \
             patch(f"{_RCP}.create_carousel_content", return_value="post text"):
            regenerate_post_carousel_task(5)
        statuses = [c.args[1] for c in mock_status.call_args_list]
        assert PostStatus.APPROVED in statuses

    def test_does_not_approve_when_still_text(self):
        from cqc_lem.app.run_content_plan import regenerate_post_carousel_task
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_DB}.get_post_user_id", return_value=1), \
             patch(f"{_DB}.get_post_buyer_stage", return_value="awareness"), \
             patch(f"{_DB}.update_db_post_content"), \
             patch(f"{_DB}.get_post_carousel_slides", return_value=["Still A Text Title"]), \
             patch(f"{_DB}.get_post_status", return_value="error"), \
             patch(f"{_DB}.update_db_post_status") as mock_status, \
             patch(f"{_RCP}.create_carousel_content", return_value="post text"):
            regenerate_post_carousel_task(5)
        assert PostStatus.APPROVED not in [c.args[1] for c in mock_status.call_args_list]
