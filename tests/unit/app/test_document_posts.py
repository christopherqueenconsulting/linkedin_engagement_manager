"""Unit tests for native document posts in the publish + planning pipelines (issue #390)."""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

BASE_PATCHES = [
    ("cqc_lem.app.run_automation.get_post_status", {"return_value": "approved"}),
    # Issue #1074: the publish task refuses a native-publish draft before anything else.
    ("cqc_lem.app.run_automation.get_post_manual_publish", {"return_value": False}),
    ("cqc_lem.app.run_automation.get_user_password_pair_by_id", {"return_value": ("u@example.com", "pw")}),
    ("cqc_lem.app.run_automation.get_post_content", {"return_value": "Post text"}),
    ("cqc_lem.app.run_automation.insert_new_log", {}),
    ("cqc_lem.app.run_automation.update_db_post_status", {}),
    ("cqc_lem.app.run_automation.get_engagement_preferences", {"return_value": {"reply_check_mode": "event"}}),
    ("cqc_lem.app.run_automation.sweep_reply_comments", {}),
    # post_to_linkedin dispatches the seed first comment itself once the post is live (#392).
    ("cqc_lem.app.run_automation.auto_seed_comment_on_post", {}),
    ("cqc_lem.app.run_automation.auto_second_wave_comment", {}),
]


@pytest.mark.unit
class TestPostToLinkedinDocument:

    def test_document_post_calls_share_document_on_linkedin(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        from cqc_lem.utilities.db import PostType

        slides = ["/assets/images/carousel/40/slide_01.png", "/assets/images/carousel/40/slide_02.png"]
        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.DOCUMENT))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_carousel_slides", return_value=slides))
            mock_document = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_document_on_linkedin", return_value="urn:li:share:40")
            )
            mock_carousel = stack.enter_context(patch("cqc_lem.app.run_automation.share_carousel_on_linkedin"))
            mock_share = stack.enter_context(patch("cqc_lem.app.run_automation.share_on_linkedin"))

            post_to_linkedin.run(1, 40)

            mock_document.assert_called_once_with(1, "Post text", slides, post_id=40)
            mock_carousel.assert_not_called()
            mock_share.assert_not_called()

    def test_document_post_without_slides_is_flagged_error(self):
        from cqc_lem.app.run_automation import post_to_linkedin
        from cqc_lem.utilities.db import PostStatus, PostType

        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                if target.endswith("update_db_post_status"):
                    continue
                stack.enter_context(patch(target, **kwargs))
            mock_status = stack.enter_context(patch("cqc_lem.app.run_automation.update_db_post_status"))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.DOCUMENT))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_carousel_slides", return_value=[]))
            mock_document = stack.enter_context(patch("cqc_lem.app.run_automation.share_document_on_linkedin"))

            result = post_to_linkedin.run(1, 41)

            mock_document.assert_not_called()
            mock_status.assert_called_once_with(41, PostStatus.ERROR)
            assert "error" in result
            assert "no real slide images" in result

    def test_document_publish_failure_does_not_blame_the_slide_images(self):
        """A None URN from a deck that HAD slides may be creds/API — don't send ops after images."""
        from cqc_lem.app.run_automation import post_to_linkedin
        from cqc_lem.utilities.db import PostStatus, PostType

        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                if target.endswith(("update_db_post_status", "insert_new_log")):
                    continue
                stack.enter_context(patch(target, **kwargs))
            mock_status = stack.enter_context(patch("cqc_lem.app.run_automation.update_db_post_status"))
            mock_log = stack.enter_context(patch("cqc_lem.app.run_automation.insert_new_log"))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.DOCUMENT))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_carousel_slides", return_value=["/s1.png"]))
            stack.enter_context(patch("cqc_lem.app.run_automation.share_document_on_linkedin", return_value=None))

            result = post_to_linkedin.run(1, 42)

            mock_status.assert_called_once_with(42, PostStatus.ERROR)
            assert "no real slide images" not in result
            assert "no URN" in result
            assert "no real slide images" not in mock_log.call_args[1]["message"]


@pytest.mark.unit
class TestDocumentContentPlanning:

    def test_document_posts_use_the_carousel_slide_pipeline(self):
        from cqc_lem.app.run_content_plan import create_content

        with patch("cqc_lem.app.run_content_plan.create_carousel_content",
                   return_value="Deck caption") as mock_carousel:
            content, video_url = create_content(user_id=1, post_type="document", stage="awareness", post_id=42)

        assert content == "Deck caption"
        assert video_url is None
        mock_carousel.assert_called_once_with(1, "awareness", 42)

    def test_document_is_balanced_alongside_the_other_types(self):
        from cqc_lem.app.run_content_plan import PLANNED_POST_TYPES
        from cqc_lem.utilities.db import PostType

        assert PostType.DOCUMENT.value in PLANNED_POST_TYPES
        assert set(PLANNED_POST_TYPES) == {t.value for t in PostType}

    def test_document_without_real_slides_is_missing_its_asset(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset

        with patch("cqc_lem.utilities.db.get_post_carousel_slides", return_value=["Just a title"]):
            assert _post_missing_required_asset(42, "document", None) is True

        with patch("cqc_lem.utilities.db.get_post_carousel_slides",
                   return_value=["https://assets.example/slide_01.png"]):
            assert _post_missing_required_asset(42, "document", None) is False
