"""A failed lane run ends in Celery FAILURE, not SUCCESS with a string (issue #2097).

Each of the six tasks the 2026-09 audit named (finding C-3) is driven through `Task.apply()`, so what
is asserted is the STATE Celery records — the thing Flower and the `celery_task` tile count — and not
only that some exception left the function.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from cqc_lem.app.task_outcome import LaneTaskFailed, TaskOutcome, landed_or_no_op, lane_result

pytestmark = pytest.mark.unit

_NL = "cqc_lem.app.engagement.newsletter"
_OUT = "cqc_lem.app.engagement.outreach"
_POST = "cqc_lem.app.engagement.posting"
_FEED = "cqc_lem.app.engagement.feed"


def _apply(task, patches: list, **kwargs):
    """Run `task` eagerly under `patches`, with QueueOnce's Redis lock backend stubbed.

    `capture_exception` is stubbed too, so a regression that filed a LaneTaskFailed stays offline.
    """
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(patch("cqc_lem.app.queue_once.QueueOnce.once_backend",
                                  new_callable=PropertyMock))
        stack.enter_context(patch("cqc_lem.app.my_celery.capture_exception"))
        return task.apply(kwargs=kwargs)


def _assert_failed(result, match: str) -> None:
    assert result.state == "FAILURE"
    assert isinstance(result.result, LaneTaskFailed)
    assert match in str(result.result)


class TestLaneResult:
    def test_landed_and_no_op_return_the_message(self):
        assert lane_result(TaskOutcome.LANDED, "sent") == "sent"
        assert lane_result(TaskOutcome.NO_OP, "skipped") == "skipped"

    def test_failed_raises_with_the_message_and_the_cause(self):
        cause = RuntimeError("boom")
        with pytest.raises(LaneTaskFailed, match="did not land") as info:
            lane_result(TaskOutcome.FAILED, "did not land", cause=cause)
        assert info.value.__cause__ is cause

    def test_failed_inside_except_keeps_the_implicit_context(self):
        with pytest.raises(LaneTaskFailed) as info:
            try:
                raise ValueError("inner")
            except ValueError:
                lane_result(TaskOutcome.FAILED, "x")
        assert isinstance(info.value.__context__, ValueError)
        assert not info.value.__suppress_context__

    def test_landed_or_no_op(self):
        assert landed_or_no_op(2) is TaskOutcome.LANDED
        assert landed_or_no_op(0) is TaskOutcome.NO_OP


class TestEachAuditedTaskEndsInFailure:
    def test_auto_publish_edition(self):
        from cqc_lem.app.engagement.newsletter import auto_publish_edition
        edition = {"id": 9, "user_id": 1, "status": "approved", "title": "T", "subtitle": None, "body": "B"}
        fail = MagicMock()
        result = _apply(auto_publish_edition, [
            patch(f"{_NL}.get_newsletter_edition", return_value=edition),
            patch(f"{_NL}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())),
            patch(f"{_NL}._fill_and_publish_article", return_value=(None, "article_publish")),
            patch(f"{_NL}.mark_edition_failed", fail),
            patch(f"{_NL}.log_error"),
            patch(f"{_NL}.quit_gracefully"),
        ], edition_id=9)
        _assert_failed(result, "publish flow did not complete")
        fail.assert_called_once_with(9)  # raised outside the try: marked exactly once

    def test_engage_with_profile_viewer(self):
        from cqc_lem.app.engagement.outreach import engage_with_profile_viewer
        result = _apply(engage_with_profile_viewer, [
            patch(f"{_OUT}.has_engaged_url_with_x_days", return_value=False),
            patch(f"{_OUT}.get_current_profile", side_effect=RuntimeError("grid down")),
            patch(f"{_OUT}.log_error"),
        ], user_id=1, viewer_url="https://x/in/v", viewer_name="V")
        _assert_failed(result, "Failed to start profile viewer engagement")

    def test_send_scheduled_dm(self):
        from cqc_lem.app.engagement.outreach import send_scheduled_dm
        dm = {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/j", "message": "hi",
              "status": "approved"}
        result = _apply(send_scheduled_dm, [
            patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=dm),
            patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0),
            patch(f"{_OUT}.get_engagement_preferences", return_value={"max_dms_per_day": 10}),
            patch(f"{_OUT}.send_dm_now", return_value=False),
            patch("cqc_lem.utilities.db.update_scheduled_dm_status"),
        ], dm_id=3)
        _assert_failed(result, "Scheduled DM 3 -> failed")

    def test_auto_scrape_post_stats(self):
        from cqc_lem.app.engagement.posting import auto_scrape_post_stats
        result = _apply(auto_scrape_post_stats, [
            patch(f"{_POST}.get_recent_posted_post_ids", return_value=[1]),
            patch(f"{_POST}._post_stats_backfill_bounds", return_value=(0, 0)),
            patch(f"{_POST}.get_shipped_variant_keys", return_value={}),
            patch(f"{_POST}.get_post_types_for_user", return_value={}),
            patch(f"{_POST}.get_current_profile", side_effect=RuntimeError("grid down")),
            patch(f"{_POST}.log_error"),
        ], user_id=1)
        _assert_failed(result, "Failed: grid down")

    def test_auto_comment_in_groups(self):
        from cqc_lem.app.engagement.feed import auto_comment_in_groups
        result = _apply(auto_comment_in_groups, [
            patch(f"{_FEED}.get_enabled_group_ids", return_value=["123"]),
            patch(f"{_FEED}.get_current_profile", side_effect=RuntimeError("grid down")),
            patch(f"{_FEED}.log_error"),
        ], user_id=1)
        _assert_failed(result, "Failed: grid down")

    def test_auto_weekly_youtube_token_check(self):
        from cqc_lem.app.run_scheduler import auto_weekly_youtube_token_check
        state = {"status": "needs_reauth", "checked_at": "t", "reason": "invalid_grant", "emailed": True}
        result = _apply(auto_weekly_youtube_token_check, [
            patch("cqc_lem.utilities.marketing.youtube_auth.run_health_probe", return_value=state),
        ])
        _assert_failed(result, "YouTube token needs_reauth")


class TestDesignedStopsStaySuccess:
    """The other side of the line: a back-off or an undecided probe is not a failure."""

    @pytest.mark.parametrize("status", ["ok", "unknown", "not_configured"])
    def test_youtube_check_only_fails_on_a_proved_dead_grant(self, status):
        from cqc_lem.app.run_scheduler import auto_weekly_youtube_token_check
        state = {"status": status, "checked_at": "t", "reason": "r", "emailed": False}
        result = _apply(auto_weekly_youtube_token_check, [
            patch("cqc_lem.utilities.marketing.youtube_auth.run_health_probe", return_value=state),
        ])
        assert result.state == "SUCCESS"
        assert f"YouTube token {status}" in result.result

    def test_profile_viewer_rate_limit_is_a_no_op_not_an_error(self):
        from cqc_lem.app.engagement.outreach import engage_with_profile_viewer
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_OUT}.log_error") as log_err:
            result = _apply(engage_with_profile_viewer, [
                patch(f"{_OUT}.has_engaged_url_with_x_days", return_value=False),
                patch(f"{_OUT}.get_current_profile", side_effect=LinkedInRateLimited("429")),
            ], user_id=1, viewer_url="https://x/in/v", viewer_name="V")
        assert result.state == "SUCCESS"
        assert "rate limited" in result.result
        log_err.assert_not_called()
