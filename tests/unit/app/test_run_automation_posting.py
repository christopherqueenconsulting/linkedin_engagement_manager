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
    # post_to_linkedin dispatches the seed first comment itself once the post is live (#392).
    ("cqc_lem.app.run_automation.auto_seed_comment_on_post", {}),
    ("cqc_lem.app.run_automation.auto_second_wave_comment", {}),
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
            stack.enter_context(patch("cqc_lem.utilities.db.get_post_image_url", return_value=None))
            mock_share = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_on_linkedin", return_value="urn:li:ugcPost:1")
            )
            mock_carousel = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_carousel_on_linkedin")
            )

            post_to_linkedin.run(1, 10)

            # No image on the row -> the post publishes bare; a missing image never blocks it.
            mock_share.assert_called_once_with(1, "Post text")
            mock_carousel.assert_not_called()

    def test_text_post_attaches_its_generated_image(self):
        from cqc_lem.utilities.db import PostType
        from cqc_lem.app.run_automation import post_to_linkedin

        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.TEXT))
            stack.enter_context(patch("cqc_lem.utilities.db.get_post_image_url",
                                      return_value="https://api.example.com/api/assets?file_name=images/posts/10/a.png"))
            mock_share = stack.enter_context(
                patch("cqc_lem.app.run_automation.share_on_linkedin", return_value="urn:li:ugcPost:1")
            )

            post_to_linkedin.run(1, 10)

            mock_share.assert_called_once_with(
                1, "Post text", "https://api.example.com/api/assets?file_name=images/posts/10/a.png")

    def test_success_log_stores_actual_post_content_not_status_string(self):
        """Regression: the POST success log must store the real post body — seed comments and
        thread replies read it back to ground the AI. Storing a status string made the model
        write comments about the /posts API instead of the post's subject."""
        from cqc_lem.utilities.db import PostType, LogActionType, LogResultType
        from cqc_lem.app.run_automation import post_to_linkedin

        with ExitStack() as stack:
            for target, kwargs in BASE_PATCHES:
                if target == "cqc_lem.app.run_automation.insert_new_log":
                    continue
                stack.enter_context(patch(target, **kwargs))
            mock_log = stack.enter_context(patch("cqc_lem.app.run_automation.insert_new_log"))
            stack.enter_context(patch("cqc_lem.app.run_automation.get_post_type", return_value=PostType.TEXT))
            stack.enter_context(patch("cqc_lem.app.run_automation.share_on_linkedin", return_value="urn:li:ugcPost:1"))
            stack.enter_context(patch("cqc_lem.app.run_automation.share_carousel_on_linkedin"))

            post_to_linkedin.run(1, 10)

            post_logs = [c for c in mock_log.call_args_list
                         if c.kwargs.get("action_type") == LogActionType.POST
                         and c.kwargs.get("result") == LogResultType.SUCCESS]
            assert post_logs, "expected a POST success log"
            assert post_logs[0].kwargs["message"] == "Post text"

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

    def test_event_mode_schedules_golden_hour_amplifier_sweeps(self):
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

            # Golden-hour amplifier (#401): several sweeps spread across the first hour, not one.
            assert mock_sweep.apply_async.call_count == 3
            countdowns = [c.kwargs["countdown"] for c in mock_sweep.apply_async.call_args_list]
            assert countdowns == [20 * 60, 40 * 60, 60 * 60]
            # Each sweep carries a DISTINCT sweep_slot so celery-once's user_id-keyed lock doesn't
            # drop the 2nd/3rd apply_async as duplicates (which would collapse the amplifier to one run).
            slots = [c.kwargs["kwargs"]["sweep_slot"] for c in mock_sweep.apply_async.call_args_list]
            assert slots == [0, 1, 2]
            for c in mock_sweep.apply_async.call_args_list:
                assert c.kwargs["kwargs"]["user_id"] == 1

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
