"""Unit tests for post_to_linkedin task — post-type branching."""

import pytest
from contextlib import ExitStack
from unittest.mock import patch


BASE_PATCHES = [
    ("cqc_lem.app.run_automation.get_post_status", {"return_value": "approved"}),
    ("cqc_lem.app.run_automation.get_user_password_pair_by_id", {"return_value": ("u@example.com", "pw")}),
    ("cqc_lem.app.run_automation.get_post_content", {"return_value": "Post text"}),
    ("cqc_lem.app.run_automation.insert_new_log", {}),
    ("cqc_lem.app.run_automation.update_db_post_status", {}),
    ("cqc_lem.app.run_automation.get_engagement_preferences", {"return_value": {"reply_check_mode": "event"}}),
    ("cqc_lem.app.run_automation.sweep_reply_comments", {}),
]


@pytest.mark.unit
class TestPostToLinkedinTypeBranching:

    def test_text_post_calls_share_on_linkedin(self):
        from cqc_lem.utilities.db import PostType
        from cqc_lem.app.run_automation import post_to_linkedin

        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.TEXT))
            mock_share = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_on_linkedin", return_value="urn:li:ugcPost:1")
            )
            mock_carousel = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_carousel_on_linkedin")
            )

            post_to_linkedin.run(1, 10)

            mock_share.assert_called_once_with(1, "Post text")
            mock_carousel.assert_not_called()

    def test_video_post_calls_share_on_linkedin_with_url(self):
        from cqc_lem.utilities.db import PostType
        from cqc_lem.app.run_automation import post_to_linkedin

        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.VIDEO))
            stack.enter_context(
                patch("cqc_lem.app.run_automation.get_post_video_url", return_value="https://cdn.example.com/v.mp4")
            )
            mock_share = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_on_linkedin", return_value="urn:li:ugcPost:2")
            )
            mock_carousel = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_carousel_on_linkedin")
            )

            post_to_linkedin.run(1, 20)

            mock_share.assert_called_once_with(1, "Post text", "https://cdn.example.com/v.mp4")
            mock_carousel.assert_not_called()

    def test_carousel_post_calls_share_carousel_on_linkedin(self):
        from cqc_lem.utilities.db import PostType
        from cqc_lem.app.run_automation import post_to_linkedin

        slides = ["Slide one", "Slide two"]
        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.CAROUSEL))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_carousel_slides", return_value=slides))
            mock_carousel = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_carousel_on_linkedin", return_value="urn:li:ugcPost:3")
            )
            mock_share = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_on_linkedin")
            )

            post_to_linkedin.run(1, 30)

            mock_carousel.assert_called_once_with(1, "Post text", slides)
            mock_share.assert_not_called()

    def test_event_mode_schedules_one_golden_hour_sweep(self):
        from cqc_lem.utilities.db import PostType
        from cqc_lem.app.run_automation import post_to_linkedin

        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                if target.endswith("sweep_reply_comments"):
                    continue
                stack.enter_context(patch(target, **kwargs))
            mock_sweep = stack.enter_context(patch("cqc_lem.app.run_automation.sweep_reply_comments"))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.TEXT))
            stack.enter_context(patch("cqc_lem.app.run_automation.share_on_linkedin", return_value="urn:li:ugcPost:1"))

            post_to_linkedin.run(1, 10)

            mock_sweep.apply_async.assert_called_once()
            assert mock_sweep.apply_async.call_args.kwargs["kwargs"] == {"user_id": 1}
            assert mock_sweep.apply_async.call_args.kwargs["countdown"] == 35 * 60

    def test_scheduled_and_off_modes_schedule_no_per_post_sweep(self):
        from cqc_lem.utilities.db import PostType
        from cqc_lem.app.run_automation import post_to_linkedin

        for mode in ("scheduled", "off"):
            with ExitStack() as stack:
                for target, kwargs in BASE_PATCHES:
                    if target.endswith("get_engagement_preferences") or target.endswith("sweep_reply_comments"):
                        continue
                    stack.enter_context(patch(target, **kwargs))
                stack.enter_context(patch("cqc_lem.app.run_automation.get_engagement_preferences",
                                          return_value={"reply_check_mode": mode}))
                mock_sweep = stack.enter_context(patch("cqc_lem.app.run_automation.sweep_reply_comments"))
                stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.TEXT))
                stack.enter_context(patch("cqc_lem.app.run_automation.share_on_linkedin", return_value="urn:li:ugcPost:1"))

                post_to_linkedin.run(1, 10)

                mock_sweep.apply_async.assert_not_called()

    def test_skips_already_posted(self):
        from cqc_lem.app.run_automation import post_to_linkedin

        with patch("cqc_lem.app.run_automation.get_post_status", return_value="posted"), \
             patch("cqc_lem.app.run_automation.share_on_linkedin") as mock_share, \
             patch("cqc_lem.app.run_automation.share_carousel_on_linkedin") as mock_carousel:

            result = post_to_linkedin.run(1, 99)

            assert "already posted" in result.lower()
            mock_share.assert_not_called()
            mock_carousel.assert_not_called()

    def test_carousel_without_real_images_flags_error_and_does_not_post(self):
        """Prod incident: a carousel with no real slide images must be flagged 'error'
        (not posted with placeholder images)."""
        from cqc_lem.utilities.db import PostType, PostStatus
        from cqc_lem.app.run_automation import post_to_linkedin

        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.CAROUSEL))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_carousel_slides",
                                      return_value=["title a", "title b"]))
            stack.enter_context(patch("cqc_lem.app.run_automation.share_carousel_on_linkedin", return_value=None))
            stack.enter_context(patch("cqc_lem.app.run_automation.log_error"))
            mock_status = stack.enter_context(patch("cqc_lem.app.run_automation.update_db_post_status"))

            result = post_to_linkedin.run(1, 10)

        statuses = [c.args[1] for c in mock_status.call_args_list]
        assert PostStatus.ERROR in statuses
        assert PostStatus.POSTED not in statuses
        assert "error" in result.lower()

    def test_carousel_with_no_slides_flags_error(self):
        """Empty slides → don't even call the poster; flag 'error'."""
        from cqc_lem.utilities.db import PostType, PostStatus
        from cqc_lem.app.run_automation import post_to_linkedin

        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.CAROUSEL))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_carousel_slides", return_value=[]))
            stack.enter_context(patch("cqc_lem.app.run_automation.log_error"))
            mock_carousel = stack.enter_context(patch("cqc_lem.app.run_automation.share_carousel_on_linkedin"))
            mock_status = stack.enter_context(patch("cqc_lem.app.run_automation.update_db_post_status"))

            post_to_linkedin.run(1, 10)

        mock_carousel.assert_not_called()
        assert PostStatus.ERROR in [c.args[1] for c in mock_status.call_args_list]
