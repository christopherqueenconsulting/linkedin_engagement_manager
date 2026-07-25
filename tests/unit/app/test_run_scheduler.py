"""Unit tests for cqc_lem.app.run_scheduler Celery tasks."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Module-level patch target strings
# ---------------------------------------------------------------------------

_MOD = "cqc_lem.app.run_scheduler"

_PATCH_GET_USERS_STRIPE = f"{_MOD}.get_users_with_stripe_subscriptions"
_PATCH_UPDATE_SUB = f"{_MOD}.update_subscription_from_stripe"
# sync_stripe_subscriptions imports these lazily inside the function body, so
# we must patch at the source module, not at run_scheduler.
_PATCH_FETCH_SUB = "cqc_lem.utilities.stripe_util.fetch_subscription"
_PATCH_GET_TIER = "cqc_lem.utilities.stripe_util.get_subscription_tier_from_price"
_PATCH_STATUS_TO_DB = "cqc_lem.utilities.stripe_util.stripe_status_to_db"
_PATCH_GET_ACTIVE = f"{_MOD}.get_active_user_ids"
_PATCH_GET_POSTS = f"{_MOD}.get_ready_to_post_posts"
_PATCH_GET_ORPHANED = f"{_MOD}.get_orphaned_scheduled_posts"
_PATCH_UPDATE_POST_STATUS = f"{_MOD}.update_db_post_status"
_PATCH_POST_TO_LINKEDIN = f"{_MOD}.post_to_linkedin"
_PATCH_APPRECIATE = f"{_MOD}.automate_appreciation_dms_for_user"
_PATCH_CLEAN_INVITES = f"{_MOD}.clean_stale_invites"
_PATCH_UPDATE_STALE = f"{_MOD}.update_stale_profile"
_PATCH_AUTOMATE_COMMENTING = f"{_MOD}.automate_commenting"
_PATCH_AUTOMATE_PROFILE_VIEWER = f"{_MOD}.automate_profile_viewer_engagement"
_PATCH_RECORD_SCHEDULED = f"{_MOD}.record_pre_post_scheduled"
_PATCH_RECORD_SKIPPED = f"{_MOD}.record_pre_post_skipped"


@pytest.fixture(autouse=True)
def _not_throttled():
    """Keep the automation throttle (manual pause + 429 breaker) OPEN=off and hermetic by default so
    dispatcher tests never touch Redis. Tests exercising the throttle gate patch these explicitly."""
    with patch(f"{_MOD}.is_automation_paused", return_value=False), \
         patch(f"{_MOD}.rate_limit_cooldown_remaining", return_value=0):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _async_task_mock() -> MagicMock:
    """Return a MagicMock with an apply_async attribute."""
    m = MagicMock()
    m.apply_async = MagicMock()
    return m


def _future_dt(minutes: int = 60) -> datetime:
    """A scheduled time far enough ahead that the pre-post engagement window is still open."""
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# sync_stripe_subscriptions
# ---------------------------------------------------------------------------

class TestSyncStripeSubscriptions:
    """Tests for the sync_stripe_subscriptions Celery task."""

    def test_no_subscribers_returns_early_without_fetching(self):
        with patch(_PATCH_GET_USERS_STRIPE, return_value=[]) as mock_get, \
             patch(_PATCH_FETCH_SUB) as mock_fetch:
            from cqc_lem.app.run_scheduler import sync_stripe_subscriptions
            sync_stripe_subscriptions.run()

        mock_get.assert_called_once()
        mock_fetch.assert_not_called()

    def test_subscriber_with_up_to_date_status_is_not_updated(self):
        """When DB status matches Stripe status and tier, update_subscription_from_stripe is NOT called."""
        row = {
            "id": 1,
            "stripe_subscription_id": "sub_abc",
            "stripe_customer_id": "cus_abc",
            "subscription_status": "active",
            "subscription_tier": "starter",
        }
        sub = {
            "status": "active",
            "items": {"data": [{"price": {"id": "price_starter"}}]},
            "current_period_end": 1700000000,
        }

        # fetch_subscription / stripe_status_to_db / get_subscription_tier_from_price are
        # imported inside sync_stripe_subscriptions's function body — patch at source module.
        with patch(_PATCH_GET_USERS_STRIPE, return_value=[row]), \
             patch(_PATCH_FETCH_SUB, return_value=sub), \
             patch(_PATCH_STATUS_TO_DB, return_value="active"), \
             patch(_PATCH_GET_TIER, return_value="starter"), \
             patch(_PATCH_UPDATE_SUB) as mock_update:
            from cqc_lem.app.run_scheduler import sync_stripe_subscriptions
            sync_stripe_subscriptions.run()

        mock_update.assert_not_called()

    def test_subscriber_with_mismatched_status_calls_update(self):
        """When Stripe status differs from DB status, update_subscription_from_stripe IS called."""
        row = {
            "id": 2,
            "stripe_subscription_id": "sub_xyz",
            "stripe_customer_id": "cus_xyz",
            "subscription_status": "active",   # DB says active
            "subscription_tier": "starter",
        }
        sub = {
            "status": "past_due",               # Stripe says past_due
            "items": {"data": [{"price": {"id": "price_starter"}}]},
            "current_period_end": 1700000000,
        }

        with patch(_PATCH_GET_USERS_STRIPE, return_value=[row]), \
             patch(_PATCH_FETCH_SUB, return_value=sub), \
             patch(_PATCH_STATUS_TO_DB, return_value="past_due"), \
             patch(_PATCH_GET_TIER, return_value="starter"), \
             patch(_PATCH_UPDATE_SUB) as mock_update:
            from cqc_lem.app.run_scheduler import sync_stripe_subscriptions
            sync_stripe_subscriptions.run()

        mock_update.assert_called_once()
        call_args = mock_update.call_args[0]
        assert call_args[0] == "cus_xyz"   # customer_id
        assert call_args[1] == "past_due"  # new db_status

    def test_subscriber_with_mismatched_tier_calls_update(self):
        """When Stripe tier differs from DB tier, update_subscription_from_stripe IS called."""
        row = {
            "id": 3,
            "stripe_subscription_id": "sub_tier",
            "stripe_customer_id": "cus_tier",
            "subscription_status": "active",
            "subscription_tier": "starter",    # DB says starter
        }
        sub = {
            "status": "active",
            "items": {"data": [{"price": {"id": "price_pro"}}]},
            "current_period_end": 1700000000,
        }

        with patch(_PATCH_GET_USERS_STRIPE, return_value=[row]), \
             patch(_PATCH_FETCH_SUB, return_value=sub), \
             patch(_PATCH_STATUS_TO_DB, return_value="active"), \
             patch(_PATCH_GET_TIER, return_value="professional"), \
             patch(_PATCH_UPDATE_SUB) as mock_update:
            from cqc_lem.app.run_scheduler import sync_stripe_subscriptions
            sync_stripe_subscriptions.run()

        mock_update.assert_called_once()

    def test_fetch_subscription_returns_none_skips_subscriber(self):
        """When fetch_subscription returns None, that subscriber is skipped silently."""
        row = {
            "id": 4,
            "stripe_subscription_id": "sub_gone",
            "stripe_customer_id": "cus_gone",
            "subscription_status": "active",
            "subscription_tier": "starter",
        }

        with patch(_PATCH_GET_USERS_STRIPE, return_value=[row]), \
             patch(_PATCH_FETCH_SUB, return_value=None), \
             patch(_PATCH_UPDATE_SUB) as mock_update:
            from cqc_lem.app.run_scheduler import sync_stripe_subscriptions
            sync_stripe_subscriptions.run()

        mock_update.assert_not_called()

    def test_row_without_sub_id_is_skipped(self):
        """Rows missing stripe_subscription_id are silently skipped."""
        row = {
            "id": 5,
            "stripe_subscription_id": None,
            "stripe_customer_id": "cus_no_sub",
            "subscription_status": "trial",
            "subscription_tier": None,
        }

        with patch(_PATCH_GET_USERS_STRIPE, return_value=[row]), \
             patch(_PATCH_FETCH_SUB) as mock_fetch, \
             patch(_PATCH_UPDATE_SUB) as mock_update:
            from cqc_lem.app.run_scheduler import sync_stripe_subscriptions
            sync_stripe_subscriptions.run()

        mock_fetch.assert_not_called()
        mock_update.assert_not_called()

    def test_multiple_subscribers_processed_individually(self):
        """Each subscriber row is checked independently."""
        rows = [
            {
                "id": 10,
                "stripe_subscription_id": "sub_a",
                "stripe_customer_id": "cus_a",
                "subscription_status": "active",
                "subscription_tier": "starter",
            },
            {
                "id": 11,
                "stripe_subscription_id": "sub_b",
                "stripe_customer_id": "cus_b",
                "subscription_status": "active",
                "subscription_tier": "starter",
            },
        ]
        sub = {
            "status": "past_due",
            "items": {"data": [{"price": {"id": "price_starter"}}]},
            "current_period_end": 1700000000,
        }

        with patch(_PATCH_GET_USERS_STRIPE, return_value=rows), \
             patch(_PATCH_FETCH_SUB, return_value=sub), \
             patch(_PATCH_STATUS_TO_DB, return_value="past_due"), \
             patch(_PATCH_GET_TIER, return_value="starter"), \
             patch(_PATCH_UPDATE_SUB) as mock_update:
            from cqc_lem.app.run_scheduler import sync_stripe_subscriptions
            sync_stripe_subscriptions.run()

        # Both subscribers have mismatched status → update called twice
        assert mock_update.call_count == 2


# ---------------------------------------------------------------------------
# auto_check_scheduled_posts
# ---------------------------------------------------------------------------

class TestAutoCheckScheduledPosts:
    def test_no_posts_returns_no_post_to_schedule(self):
        with patch(_PATCH_GET_POSTS, return_value=[]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            result = auto_check_scheduled_posts.run()

        assert result == "No Post to Schedule"

    def test_one_post_schedules_it_and_calls_apply_async(self):
        # Future time: the pre-post warm-up is only dispatched while its window is still open.
        scheduled_dt = _future_dt()
        posts = [(42, scheduled_dt, 7)]  # (post_id, scheduled_time, user_id)

        mock_post_task = _async_task_mock()
        mock_commenting_task = _async_task_mock()
        mock_profile_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[7]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS) as mock_upd, \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, mock_profile_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            result = auto_check_scheduled_posts.run()

        # post status updated to SCHEDULED
        mock_upd.assert_called_once()
        status_arg = mock_upd.call_args[0][1]
        from cqc_lem.utilities.db import PostStatus
        assert status_arg == PostStatus.SCHEDULED

        # post_to_linkedin.apply_async called with correct kwargs
        mock_post_task.apply_async.assert_called_once()
        post_call_kwargs = mock_post_task.apply_async.call_args[1]
        assert post_call_kwargs["kwargs"] == {"user_id": 7, "post_id": 42}
        assert post_call_kwargs["eta"] == scheduled_dt

        # Commenting and profile viewer tasks also scheduled (user 7 is active)
        mock_commenting_task.apply_async.assert_called_once()
        mock_profile_task.apply_async.assert_called_once()

        assert "1 post" in result

    def test_multiple_posts_return_correct_count_message(self):
        scheduled_dt = datetime(2025, 6, 20, 14, 0, 0, tzinfo=timezone.utc)
        posts = [
            (1, scheduled_dt, 7),
            (2, scheduled_dt, 8),
            (3, scheduled_dt, 9),
        ]

        mock_post_task = _async_task_mock()
        mock_commenting_task = _async_task_mock()
        mock_profile_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[7, 8, 9]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, mock_profile_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            result = auto_check_scheduled_posts.run()

        assert "3 post" in result
        assert mock_post_task.apply_async.call_count == 3

    def test_naive_scheduled_time_gets_utc_tzinfo(self):
        """A naive datetime returned from MySQL is treated as UTC before becoming the eta."""
        naive_dt = datetime(2025, 6, 20, 14, 0, 0)  # no tzinfo — simulates MySQL read
        expected_eta = datetime(2025, 6, 20, 14, 0, 0, tzinfo=timezone.utc)
        posts = [(55, naive_dt, 10)]

        mock_post_task = _async_task_mock()
        mock_commenting_task = _async_task_mock()
        mock_profile_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[10]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, mock_profile_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        eta = mock_post_task.apply_async.call_args[1]["eta"]
        assert eta.tzinfo is not None, "eta must be timezone-aware"
        assert eta == expected_eta

    def test_update_db_post_status_called_before_apply_async(self):
        """Status must be set to SCHEDULED before dispatching the Celery task."""
        call_order = []
        scheduled_dt = datetime(2025, 6, 20, 14, 0, 0, tzinfo=timezone.utc)
        posts = [(99, scheduled_dt, 5)]

        mock_post_task = _async_task_mock()
        mock_commenting_task = _async_task_mock()
        mock_profile_task = _async_task_mock()

        def record_update(*args, **kwargs):
            call_order.append("update_status")

        def record_apply(*args, **kwargs):
            call_order.append("apply_async")

        mock_post_task.apply_async.side_effect = record_apply

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[5]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS, side_effect=record_update), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, mock_profile_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        assert call_order[0] == "update_status"
        assert "apply_async" in call_order

    def test_inactive_user_pre_post_tasks_skipped(self):
        """Pre-post Selenium tasks are NOT dispatched for inactive/disconnected users."""
        scheduled_dt = datetime(2025, 6, 20, 14, 0, 0, tzinfo=timezone.utc)
        posts = [(88, scheduled_dt, 70)]  # user 70 is inactive

        mock_post_task = _async_task_mock()
        mock_commenting_task = _async_task_mock()
        mock_profile_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[60]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, mock_profile_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        mock_post_task.apply_async.assert_called_once()  # post_to_linkedin still runs
        mock_commenting_task.apply_async.assert_not_called()
        mock_profile_task.apply_async.assert_not_called()

    def test_post_to_linkedin_dispatched_regardless_of_user_activity(self):
        """post_to_linkedin uses the REST API — it runs even for inactive users."""
        scheduled_dt = datetime(2025, 6, 20, 14, 0, 0, tzinfo=timezone.utc)
        posts = [(77, scheduled_dt, 99)]  # user 99 not in active list

        mock_post_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task), \
             patch(_PATCH_AUTOMATE_COMMENTING, _async_task_mock()), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, _async_task_mock()):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        mock_post_task.apply_async.assert_called_once()
        call_kwargs = mock_post_task.apply_async.call_args[1]
        assert call_kwargs["kwargs"]["user_id"] == 99

    def test_mixed_active_and_inactive_users_filtered_correctly(self):
        """Active users get Selenium tasks; inactive users get only post_to_linkedin."""
        scheduled_dt = _future_dt()
        posts = [
            (10, scheduled_dt, 60),   # active user
            (11, scheduled_dt, 70),   # inactive user
        ]

        mock_post_task = _async_task_mock()
        mock_commenting_task = _async_task_mock()
        mock_profile_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[60]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, mock_profile_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        # post_to_linkedin called for both users
        assert mock_post_task.apply_async.call_count == 2
        # Selenium pre-post tasks only for the active user
        mock_commenting_task.apply_async.assert_called_once()
        mock_profile_task.apply_async.assert_called_once()
        # Verify the Selenium tasks were for the active user (user_id=60)
        assert mock_commenting_task.apply_async.call_args[1]["kwargs"]["user_id"] == 60

    # -- pre-post engagement window (issue #547) ----------------------------------------------

    def test_full_lead_available_schedules_15_minutes_before_post(self):
        """With the whole lead left, commenting starts exactly 15 min before and loops 15 min."""
        scheduled_dt = _future_dt(60)
        posts = [(42, scheduled_dt, 7)]

        mock_commenting_task = _async_task_mock()
        mock_profile_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[7]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, _async_task_mock()), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, mock_profile_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        call = mock_commenting_task.apply_async.call_args[1]
        assert call["eta"] == scheduled_dt - timedelta(minutes=15)
        assert call["kwargs"]["loop_for_duration"] == 15 * 60
        assert call["kwargs"]["post_id"] == 42  # marker key for per-post observability

        viewer_call = mock_profile_task.apply_async.call_args[1]
        assert viewer_call["eta"] == scheduled_dt - timedelta(minutes=10)
        assert viewer_call["kwargs"]["loop_for_duration"] == 10 * 60

    def test_late_pickup_clamps_eta_to_now_and_shortens_loop(self):
        """A post picked up 5 min before its time gets an eta that is NOT in the past, and a loop
        that ends at the post time instead of running past it."""
        scheduled_dt = _future_dt(5)
        posts = [(43, scheduled_dt, 7)]

        mock_commenting_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[7]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, _async_task_mock()), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, _async_task_mock()):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        call = mock_commenting_task.apply_async.call_args[1]
        assert call["eta"] <= scheduled_dt
        assert call["eta"] >= scheduled_dt - timedelta(minutes=15)
        assert 0 < call["kwargs"]["loop_for_duration"] <= 5 * 60

    def test_post_past_its_time_gets_no_pre_post_tasks(self):
        """The stale-eta bug: a post already at/past its scheduled time must NOT trigger a
        'pre'-post warm-up loop that would actually run during/after publication."""
        scheduled_dt = datetime(2025, 6, 20, 14, 0, 0, tzinfo=timezone.utc)
        posts = [(44, scheduled_dt, 7)]

        mock_post_task = _async_task_mock()
        mock_commenting_task = _async_task_mock()
        mock_profile_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[7]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, mock_profile_task), \
             patch(_PATCH_RECORD_SKIPPED) as mock_skipped:
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        mock_post_task.apply_async.assert_called_once()  # the post itself still publishes
        mock_commenting_task.apply_async.assert_not_called()
        mock_profile_task.apply_async.assert_not_called()
        reasons = {c[0][2] for c in mock_skipped.call_args_list}
        assert reasons == {"past_window"}

    def test_scheduled_window_is_recorded_per_post(self):
        """A dispatched window leaves a per-post marker so a report can confirm it fired."""
        scheduled_dt = _future_dt(60)
        posts = [(45, scheduled_dt, 7)]

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[7]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, _async_task_mock()), \
             patch(_PATCH_AUTOMATE_COMMENTING, _async_task_mock()), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, _async_task_mock()), \
             patch(_PATCH_RECORD_SCHEDULED) as mock_scheduled:
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        assert mock_scheduled.call_count == 2  # commenting + profile viewer
        post_ids = {c[0][0] for c in mock_scheduled.call_args_list}
        assert post_ids == {45}

    def test_throttled_skip_is_recorded_with_reason(self):
        """A throttle/pause skip is still gated (correct) but must be visible per post."""
        scheduled_dt = _future_dt(60)
        posts = [(46, scheduled_dt, 7)]

        mock_commenting_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[7]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(f"{_MOD}.rate_limit_cooldown_remaining", return_value=900), \
             patch(_PATCH_POST_TO_LINKEDIN, _async_task_mock()), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, _async_task_mock()), \
             patch(_PATCH_RECORD_SKIPPED) as mock_skipped:
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        mock_commenting_task.apply_async.assert_not_called()
        mock_skipped.assert_called_once()
        assert mock_skipped.call_args[0][2] == "throttled"

    def test_inactive_user_skip_is_recorded_with_reason(self):
        scheduled_dt = _future_dt(60)
        posts = [(47, scheduled_dt, 70)]

        with patch(_PATCH_GET_POSTS, return_value=posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[60]), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, _async_task_mock()), \
             patch(_PATCH_AUTOMATE_COMMENTING, _async_task_mock()), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, _async_task_mock()), \
             patch(_PATCH_RECORD_SKIPPED) as mock_skipped:
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        mock_skipped.assert_called_once()
        assert mock_skipped.call_args[0][2] == "user_inactive"

    def test_orphaned_scheduled_post_is_requeued(self):
        """Posts stuck in 'scheduled' status (task lost on restart) must be re-dispatched."""
        orphaned_dt = datetime(2026, 6, 19, 19, 45, 0, tzinfo=timezone.utc)
        orphaned = [(1485, orphaned_dt, 60)]

        mock_post_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=[]), \
             patch(_PATCH_GET_ORPHANED, return_value=orphaned), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            result = auto_check_scheduled_posts.run()

        mock_post_task.apply_async.assert_called_once()
        call_kwargs = mock_post_task.apply_async.call_args[1]
        assert call_kwargs["kwargs"] == {"user_id": 60, "post_id": 1485}
        assert "re-queued" in result
        assert "1" in result

    def test_orphaned_naive_datetime_gets_utc_tzinfo(self):
        """Naive orphaned scheduled_time is made UTC-aware before re-queuing."""
        naive_orphaned_dt = datetime(2026, 6, 19, 19, 45, 0)  # no tzinfo
        orphaned = [(1485, naive_orphaned_dt, 60)]

        mock_post_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=[]), \
             patch(_PATCH_GET_ORPHANED, return_value=orphaned), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()

        # Task dispatched without error — no assertion needed on eta since
        # orphaned re-queuing dispatches immediately (no eta kwarg)
        mock_post_task.apply_async.assert_called_once()

    def test_both_approved_and_orphaned_processed_together(self):
        """New approved posts and orphaned scheduled posts are both handled in one pass."""
        approved_dt = datetime(2026, 6, 20, 14, 0, 0, tzinfo=timezone.utc)
        orphaned_dt = datetime(2026, 6, 19, 19, 45, 0, tzinfo=timezone.utc)

        new_posts = [(100, approved_dt, 7)]
        orphaned = [(1485, orphaned_dt, 60)]

        mock_post_task = _async_task_mock()
        mock_commenting_task = _async_task_mock()
        mock_profile_task = _async_task_mock()

        with patch(_PATCH_GET_POSTS, return_value=new_posts), \
             patch(_PATCH_GET_ACTIVE, return_value=[7]), \
             patch(_PATCH_GET_ORPHANED, return_value=orphaned), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_POST_TO_LINKEDIN, mock_post_task), \
             patch(_PATCH_AUTOMATE_COMMENTING, mock_commenting_task), \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER, mock_profile_task):
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            result = auto_check_scheduled_posts.run()

        assert mock_post_task.apply_async.call_count == 2  # one new + one orphaned
        assert "1 post" in result
        assert "1 orphaned" in result


# ---------------------------------------------------------------------------
# auto_appreciate_dms
# ---------------------------------------------------------------------------

class TestAutoAppreciateDms:
    def test_no_users_returns_no_active_users(self):
        with patch(_PATCH_GET_ACTIVE, return_value=[]):
            from cqc_lem.app.run_scheduler import auto_appreciate_dms
            result = auto_appreciate_dms.run()

        assert result == "No Active Users"

    def test_one_user_calls_apply_async_with_correct_kwargs(self):
        mock_task = _async_task_mock()

        with patch(_PATCH_GET_ACTIVE, return_value=[42]), \
             patch(_PATCH_APPRECIATE, mock_task):
            from cqc_lem.app.run_scheduler import auto_appreciate_dms
            result = auto_appreciate_dms.run()

        mock_task.apply_async.assert_called_once()
        call_kwargs = mock_task.apply_async.call_args[1]
        assert call_kwargs["kwargs"]["user_id"] == 42
        assert call_kwargs["kwargs"]["loop_for_duration"] == 60 * 5
        assert "1 user" in result

    def test_multiple_users_calls_apply_async_for_each(self):
        mock_task = _async_task_mock()
        users = [1, 2, 3]

        with patch(_PATCH_GET_ACTIVE, return_value=users), \
             patch(_PATCH_APPRECIATE, mock_task):
            from cqc_lem.app.run_scheduler import auto_appreciate_dms
            result = auto_appreciate_dms.run()

        assert mock_task.apply_async.call_count == 3
        assert "3 user" in result

    def test_apply_async_includes_retry_policy(self):
        mock_task = _async_task_mock()

        with patch(_PATCH_GET_ACTIVE, return_value=[10]), \
             patch(_PATCH_APPRECIATE, mock_task):
            from cqc_lem.app.run_scheduler import auto_appreciate_dms
            auto_appreciate_dms.run()

        call_kwargs = mock_task.apply_async.call_args[1]
        assert call_kwargs.get("retry") is True
        assert "retry_policy" in call_kwargs
        assert call_kwargs["retry_policy"]["max_retries"] == 3


# ---------------------------------------------------------------------------
# auto_clean_stale_invites
# ---------------------------------------------------------------------------

class TestAutoCleanStaleInvites:
    def test_no_users_returns_no_active_users(self):
        with patch(_PATCH_GET_ACTIVE, return_value=[]):
            from cqc_lem.app.run_scheduler import auto_clean_stale_invites
            result = auto_clean_stale_invites.run()

        assert result == "No Active Users"

    def test_one_user_calls_clean_stale_invites_apply_async(self):
        mock_task = _async_task_mock()

        with patch(_PATCH_GET_ACTIVE, return_value=[7]), \
             patch(_PATCH_CLEAN_INVITES, mock_task):
            from cqc_lem.app.run_scheduler import auto_clean_stale_invites
            result = auto_clean_stale_invites.run()

        mock_task.apply_async.assert_called_once()
        call_kwargs = mock_task.apply_async.call_args[1]
        assert call_kwargs["kwargs"]["user_id"] == 7
        assert "1 user" in result

    def test_multiple_users_calls_apply_async_for_each(self):
        mock_task = _async_task_mock()
        users = [1, 2, 3, 4]

        with patch(_PATCH_GET_ACTIVE, return_value=users), \
             patch(_PATCH_CLEAN_INVITES, mock_task):
            from cqc_lem.app.run_scheduler import auto_clean_stale_invites
            result = auto_clean_stale_invites.run()

        assert mock_task.apply_async.call_count == 4
        assert "4 user" in result

    def test_apply_async_includes_retry_policy(self):
        mock_task = _async_task_mock()

        with patch(_PATCH_GET_ACTIVE, return_value=[5]), \
             patch(_PATCH_CLEAN_INVITES, mock_task):
            from cqc_lem.app.run_scheduler import auto_clean_stale_invites
            auto_clean_stale_invites.run()

        call_kwargs = mock_task.apply_async.call_args[1]
        assert call_kwargs.get("retry") is True
        assert "retry_policy" in call_kwargs


# ---------------------------------------------------------------------------
# auto_clean_stale_profiles
# ---------------------------------------------------------------------------

class TestAutoCleanStaleProfiles:
    def test_no_users_returns_no_active_users(self):
        with patch(_PATCH_GET_ACTIVE, return_value=[]):
            from cqc_lem.app.run_scheduler import auto_clean_stale_profiles
            result = auto_clean_stale_profiles.run()

        assert result == "No Active Users"

    def test_one_user_calls_update_stale_profile_apply_async(self):
        mock_task = _async_task_mock()

        with patch(_PATCH_GET_ACTIVE, return_value=[15]), \
             patch(_PATCH_UPDATE_STALE, mock_task):
            from cqc_lem.app.run_scheduler import auto_clean_stale_profiles
            result = auto_clean_stale_profiles.run()

        mock_task.apply_async.assert_called_once()
        call_kwargs = mock_task.apply_async.call_args[1]
        assert call_kwargs["kwargs"]["user_id"] == 15
        assert "1 user" in result

    def test_multiple_users_calls_apply_async_for_each(self):
        mock_task = _async_task_mock()
        users = [10, 20, 30]

        with patch(_PATCH_GET_ACTIVE, return_value=users), \
             patch(_PATCH_UPDATE_STALE, mock_task):
            from cqc_lem.app.run_scheduler import auto_clean_stale_profiles
            result = auto_clean_stale_profiles.run()

        assert mock_task.apply_async.call_count == 3
        assert "3 user" in result

    def test_apply_async_includes_retry_policy(self):
        mock_task = _async_task_mock()

        with patch(_PATCH_GET_ACTIVE, return_value=[99]), \
             patch(_PATCH_UPDATE_STALE, mock_task):
            from cqc_lem.app.run_scheduler import auto_clean_stale_profiles
            auto_clean_stale_profiles.run()

        call_kwargs = mock_task.apply_async.call_args[1]
        assert call_kwargs.get("retry") is True
        assert "retry_policy" in call_kwargs
        assert call_kwargs["retry_policy"]["max_retries"] == 3


# ---------------------------------------------------------------------------
# auto_invite_to_company_pages
# ---------------------------------------------------------------------------

_PATCH_AUTOMATE_INVITES = f"{_MOD}.automate_invites_to_company_page_for_user"


class TestAutoInviteToCompanyPages:
    def test_no_active_users_returns_no_active_users(self):
        with patch(_PATCH_GET_ACTIVE, return_value=[]):
            from cqc_lem.app.run_scheduler import auto_invite_to_company_pages
            result = auto_invite_to_company_pages.run()
        assert result == "No Active Users"

    def test_single_user_calls_apply_async_with_user_id(self):
        mock_task = _async_task_mock()
        with patch(_PATCH_GET_ACTIVE, return_value=[7]), \
             patch(_PATCH_AUTOMATE_INVITES, mock_task):
            from cqc_lem.app.run_scheduler import auto_invite_to_company_pages
            result = auto_invite_to_company_pages.run()

        mock_task.apply_async.assert_called_once()
        call_kwargs = mock_task.apply_async.call_args[1]
        assert call_kwargs["kwargs"]["user_id"] == 7
        assert "1 user" in result

    def test_multiple_users_calls_apply_async_for_each(self):
        mock_task = _async_task_mock()
        with patch(_PATCH_GET_ACTIVE, return_value=[1, 2, 3]), \
             patch(_PATCH_AUTOMATE_INVITES, mock_task):
            from cqc_lem.app.run_scheduler import auto_invite_to_company_pages
            result = auto_invite_to_company_pages.run()

        assert mock_task.apply_async.call_count == 3
        assert "3 user" in result

    def test_apply_async_includes_retry_policy_with_max_retries_3(self):
        mock_task = _async_task_mock()
        with patch(_PATCH_GET_ACTIVE, return_value=[99]), \
             patch(_PATCH_AUTOMATE_INVITES, mock_task):
            from cqc_lem.app.run_scheduler import auto_invite_to_company_pages
            auto_invite_to_company_pages.run()

        call_kwargs = mock_task.apply_async.call_args[1]
        assert call_kwargs.get("retry") is True
        assert call_kwargs["retry_policy"]["max_retries"] == 3


# ---------------------------------------------------------------------------
# auto_clean_old_videos
# ---------------------------------------------------------------------------

_PATCH_ORGANIZE = f"{_MOD}.organize_videos_by_name_and_timestamp"
_PATCH_OS_LISTDIR = f"{_MOD}.os.listdir"
_PATCH_OS_ISDIR = f"{_MOD}.os.path.isdir"
_PATCH_OS_GETMTIME = f"{_MOD}.os.path.getmtime"
_PATCH_RMTREE = f"{_MOD}.shutil.rmtree"


class TestAutoCleanOldVideos:
    def test_no_expired_folders_deletes_zero(self):
        # folder with mtime in the future (not expired)
        from datetime import datetime, timedelta
        future_ts = (datetime.now() + timedelta(days=10)).timestamp()

        with patch(_PATCH_OS_LISTDIR, return_value=["fresh_folder"]), \
             patch(_PATCH_OS_ISDIR, return_value=True), \
             patch(_PATCH_OS_GETMTIME, return_value=future_ts), \
             patch(_PATCH_RMTREE) as mock_rm, \
             patch(_PATCH_ORGANIZE, return_value=0):
            from cqc_lem.app.run_scheduler import auto_clean_old_videos
            result = auto_clean_old_videos.run()

        mock_rm.assert_not_called()
        assert "Deleted 0 folders" in result
        assert "Moved 0 videos" in result

    def test_expired_folder_is_deleted(self):
        from datetime import datetime, timedelta
        old_ts = (datetime.now() - timedelta(days=200)).timestamp()

        with patch(_PATCH_OS_LISTDIR, return_value=["old_folder"]), \
             patch(_PATCH_OS_ISDIR, return_value=True), \
             patch(_PATCH_OS_GETMTIME, return_value=old_ts), \
             patch(_PATCH_RMTREE) as mock_rm, \
             patch(_PATCH_ORGANIZE, return_value=0):
            from cqc_lem.app.run_scheduler import auto_clean_old_videos
            result = auto_clean_old_videos.run()

        mock_rm.assert_called_once()
        assert "Deleted 1 folders" in result

    def test_organize_videos_result_reflected_in_return(self):
        from datetime import datetime, timedelta
        future_ts = (datetime.now() + timedelta(days=10)).timestamp()

        with patch(_PATCH_OS_LISTDIR, return_value=[]), \
             patch(_PATCH_OS_ISDIR, return_value=True), \
             patch(_PATCH_OS_GETMTIME, return_value=future_ts), \
             patch(_PATCH_RMTREE), \
             patch(_PATCH_ORGANIZE, return_value=3):
            from cqc_lem.app.run_scheduler import auto_clean_old_videos
            result = auto_clean_old_videos.run()

        assert "Moved 3 videos" in result


# ---------------------------------------------------------------------------
# organize_videos_by_name_and_timestamp
# ---------------------------------------------------------------------------

class TestOrganizeVideosByNameAndTimestamp:
    def test_empty_selenium_folder_returns_zero(self):
        with patch(_PATCH_OS_LISTDIR, return_value=[]):
            from cqc_lem.app.run_scheduler import organize_videos_by_name_and_timestamp
            result = organize_videos_by_name_and_timestamp()
        assert result == 0

    def test_cqc_lem_prefixed_folder_is_skipped(self):
        # Folders starting with "CQC_LEM" should be skipped entirely
        with patch(_PATCH_OS_LISTDIR, return_value=["CQC_LEM_session_abc"]), \
             patch(_PATCH_OS_ISDIR, return_value=True):
            from cqc_lem.app.run_scheduler import organize_videos_by_name_and_timestamp
            result = organize_videos_by_name_and_timestamp()
        assert result == 0

    def test_mp4_file_in_non_cqc_folder_is_moved(self):
        from datetime import datetime
        from unittest.mock import call as mock_call

        fake_mtime = datetime(2024, 1, 15, 10, 30, 0).timestamp()

        # os.listdir called twice: once for the selenium folder listing, once for folder contents
        listdir_returns = [["session_xyz"], ["recording.mp4"]]
        listdir_iter = iter(listdir_returns)

        with patch(_PATCH_OS_LISTDIR, side_effect=lambda _: next(listdir_iter)), \
             patch(_PATCH_OS_ISDIR, return_value=True), \
             patch(_PATCH_OS_GETMTIME, return_value=fake_mtime), \
             patch(f"{_MOD}.os.makedirs") as mock_makedirs, \
             patch(f"{_MOD}.shutil.move") as mock_move, \
             patch(_PATCH_RMTREE):
            from cqc_lem.app.run_scheduler import organize_videos_by_name_and_timestamp
            result = organize_videos_by_name_and_timestamp()

        assert result == 1
        mock_makedirs.assert_called_once()
        mock_move.assert_called_once()


@pytest.mark.unit
class TestAutoInviteToCompanyPages:
    """auto_invite_to_company_pages must only invite users who have a company page set."""

    def test_only_users_with_company_page_are_invited(self):
        with patch(f"{_MOD}.get_active_user_ids", return_value=[1, 2, 3]), \
             patch(f"{_MOD}.get_company_linked_in_url_for_user",
                   side_effect=lambda uid: {1: "https://www.linkedin.com/company/a/",
                                            2: None,
                                            3: "https://www.linkedin.com/company/c/"}.get(uid)), \
             patch(f"{_MOD}.automate_invites_to_company_page_for_user") as mock_task:
            from cqc_lem.app.run_scheduler import auto_invite_to_company_pages
            result = auto_invite_to_company_pages()

        # Users 1 and 3 have pages; user 2 is skipped.
        assert mock_task.apply_async.call_count == 2
        invited = {c.kwargs["kwargs"]["user_id"] for c in mock_task.apply_async.call_args_list}
        assert invited == {1, 3}
        assert "2 user(s)" in result

    def test_no_company_pages_returns_early(self):
        with patch(f"{_MOD}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_MOD}.get_company_linked_in_url_for_user", return_value=None), \
             patch(f"{_MOD}.automate_invites_to_company_page_for_user") as mock_task:
            from cqc_lem.app.run_scheduler import auto_invite_to_company_pages
            result = auto_invite_to_company_pages()
        mock_task.apply_async.assert_not_called()
        assert "No active users with a company page" in result


class TestAutoRefreshProfileSyntheses:
    def test_synthesizes_only_stale_active_users(self):
        # user 3 is stale but NOT active -> must be skipped; user 1 stale+active -> processed.
        with patch(f"{_MOD}.get_active_user_ids", return_value=[1, 2]), \
             patch("cqc_lem.utilities.db.get_user_ids_needing_profile_synthesis", return_value=[1, 3]), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=MagicMock()), \
             patch("cqc_lem.utilities.ai.ai_helper.synthesize_profile", return_value="brief") as syn, \
             patch("cqc_lem.utilities.db.set_profile_synthesis", return_value=True) as setter:
            from cqc_lem.app.run_scheduler import auto_refresh_profile_syntheses
            result = auto_refresh_profile_syntheses()
        syn.assert_called_once()
        setter.assert_called_once_with(1, "brief")
        assert "1/1" in result

    def test_skips_users_without_cached_profile(self):
        with patch(f"{_MOD}.get_active_user_ids", return_value=[1]), \
             patch("cqc_lem.utilities.db.get_user_ids_needing_profile_synthesis", return_value=[1]), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
             patch("cqc_lem.utilities.ai.ai_helper.synthesize_profile") as syn, \
             patch("cqc_lem.utilities.db.set_profile_synthesis") as setter:
            from cqc_lem.app.run_scheduler import auto_refresh_profile_syntheses
            result = auto_refresh_profile_syntheses()
        syn.assert_not_called()
        setter.assert_not_called()
        assert "0/1" in result


class TestAutomationPauseGating:
    _M = "cqc_lem.app.run_scheduler"

    def test_daily_engagement_skips_when_paused(self):
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        with patch(f"{self._M}.is_automation_paused", return_value=True), \
             patch(f"{self._M}.automation_pause_remaining", return_value=999), \
             patch(f"{self._M}.get_active_user_ids") as users:
            result = auto_daily_engagement()
        assert "throttled" in result.lower()  # generic — the skip may be pause OR the 429 breaker
        users.assert_not_called()  # short-circuits before fanning out any Selenium work

    def test_appreciate_dms_skips_when_paused(self):
        from cqc_lem.app.run_scheduler import auto_appreciate_dms
        with patch(f"{self._M}.is_automation_paused", return_value=True), \
             patch(f"{self._M}.automation_pause_remaining", return_value=1), \
             patch(f"{self._M}.get_active_user_ids") as users:
            result = auto_appreciate_dms()
        assert "throttled" in result.lower()  # generic — the skip may be pause OR the 429 breaker
        users.assert_not_called()

    def test_runs_normally_when_not_paused(self):
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        with patch(f"{self._M}.is_automation_paused", return_value=False), \
             patch(f"{self._M}.get_active_user_ids", return_value=[]):
            result = auto_daily_engagement()
        assert "paused" not in result.lower()


class TestDispatchScheduledReplySweeps:
    _M = "cqc_lem.app.run_scheduler"

    def test_dispatches_when_due_and_has_session(self):
        from cqc_lem.app.run_scheduler import dispatch_scheduled_reply_sweeps
        redis = MagicMock(); redis.set.return_value = True  # nx=True → key absent → due
        with patch(f"{self._M}.get_users_with_reply_mode", return_value=[1, 2]), \
             patch(f"{self._M}.has_linkedin_session", return_value=True), \
             patch(f"{self._M}.get_engagement_preferences", return_value={"reply_sweeps_per_day": 4}), \
             patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=redis), \
             patch(f"{self._M}.sweep_reply_comments") as sweep:
            result = dispatch_scheduled_reply_sweeps()
        assert sweep.apply_async.call_count == 2
        assert "2/2" in result
        # interval TTL for 4/day = 6h
        assert redis.set.call_args.kwargs["ex"] == 6 * 60 * 60

    def test_skips_when_interval_not_elapsed(self):
        from cqc_lem.app.run_scheduler import dispatch_scheduled_reply_sweeps
        redis = MagicMock(); redis.set.return_value = False  # key present → too soon
        with patch(f"{self._M}.get_users_with_reply_mode", return_value=[1]), \
             patch(f"{self._M}.has_linkedin_session", return_value=True), \
             patch(f"{self._M}.get_engagement_preferences", return_value={"reply_sweeps_per_day": 2}), \
             patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=redis), \
             patch(f"{self._M}.sweep_reply_comments") as sweep:
            dispatch_scheduled_reply_sweeps()
        sweep.apply_async.assert_not_called()

    def test_skips_users_without_session(self):
        from cqc_lem.app.run_scheduler import dispatch_scheduled_reply_sweeps
        with patch(f"{self._M}.get_users_with_reply_mode", return_value=[1]), \
             patch(f"{self._M}.has_linkedin_session", return_value=False), \
             patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=None), \
             patch(f"{self._M}.sweep_reply_comments") as sweep:
            dispatch_scheduled_reply_sweeps()
        sweep.apply_async.assert_not_called()

    def test_no_scheduled_users(self):
        from cqc_lem.app.run_scheduler import dispatch_scheduled_reply_sweeps
        with patch(f"{self._M}.get_users_with_reply_mode", return_value=[]):
            assert "No scheduled-mode users" in dispatch_scheduled_reply_sweeps()

    def test_caps_interval_floor_at_2h(self):
        from cqc_lem.app.run_scheduler import dispatch_scheduled_reply_sweeps
        redis = MagicMock(); redis.set.return_value = True
        with patch(f"{self._M}.get_users_with_reply_mode", return_value=[1]), \
             patch(f"{self._M}.has_linkedin_session", return_value=True), \
             patch(f"{self._M}.get_engagement_preferences", return_value={"reply_sweeps_per_day": 12}), \
             patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=redis), \
             patch(f"{self._M}.sweep_reply_comments"):
            dispatch_scheduled_reply_sweeps()
        assert redis.set.call_args.kwargs["ex"] == 2 * 60 * 60  # 12/day = 2h, the floor


# ---------------------------------------------------------------------------
# _skip_if_throttled + dispatcher gating on the 429 breaker
# ---------------------------------------------------------------------------

class TestSkipIfThrottled:
    def test_not_throttled_returns_false(self):
        from cqc_lem.app.run_scheduler import _skip_if_throttled
        with patch(f"{_MOD}.is_automation_paused", return_value=False), \
             patch(f"{_MOD}.rate_limit_cooldown_remaining", return_value=0):
            assert _skip_if_throttled("x") is False

    def test_manual_pause_returns_true(self):
        from cqc_lem.app.run_scheduler import _skip_if_throttled
        with patch(f"{_MOD}.is_automation_paused", return_value=True), \
             patch(f"{_MOD}.automation_pause_remaining", return_value=123), \
             patch(f"{_MOD}.rate_limit_cooldown_remaining", return_value=0):
            assert _skip_if_throttled("x") is True

    def test_open_429_breaker_returns_true(self):
        # The core fix: dispatchers must short-circuit on the 429 cooldown, not only the manual pause.
        from cqc_lem.app.run_scheduler import _skip_if_throttled
        with patch(f"{_MOD}.is_automation_paused", return_value=False), \
             patch(f"{_MOD}.rate_limit_cooldown_remaining", return_value=900):
            assert _skip_if_throttled("x") is True


class TestNewsletterPublishGatedOnBreaker:
    def test_skips_dispatch_when_breaker_open(self):
        from cqc_lem.app.run_scheduler import auto_publish_scheduled_editions
        with patch(f"{_MOD}.rate_limit_cooldown_remaining", return_value=900), \
             patch(f"{_MOD}.is_automation_paused", return_value=False), \
             patch("cqc_lem.app.run_automation.auto_publish_edition") as pub, \
             patch("cqc_lem.utilities.db.get_editions_due_to_publish") as due:
            result = auto_publish_scheduled_editions()
        pub.apply_async.assert_not_called()
        due.assert_not_called()
        assert "throttled" in result.lower()

    def test_dispatches_when_not_throttled(self):
        from cqc_lem.app.run_scheduler import auto_publish_scheduled_editions
        with patch("cqc_lem.app.run_automation.auto_publish_edition") as pub, \
             patch("cqc_lem.utilities.db.get_editions_due_to_publish",
                   return_value=[{"id": 7, "user_id": 1}]), \
             patch("cqc_lem.utilities.db.get_pending_newsletter_editions",
                   return_value=[{"id": 7, "scheduled_for": datetime(2026, 1, 1)}]):
            result = auto_publish_scheduled_editions()
        pub.apply_async.assert_called_once()
        assert "Dispatched 1" in result


class TestReplySweepDispatchGatedOnBreaker:
    def test_skips_dispatch_when_breaker_open(self):
        from cqc_lem.app.run_scheduler import dispatch_scheduled_reply_sweeps
        with patch(f"{_MOD}.rate_limit_cooldown_remaining", return_value=900), \
             patch(f"{_MOD}.is_automation_paused", return_value=False), \
             patch(f"{_MOD}.get_users_with_reply_mode") as get_users, \
             patch(f"{_MOD}.sweep_reply_comments") as sweep:
            result = dispatch_scheduled_reply_sweeps()
        get_users.assert_not_called()
        sweep.apply_async.assert_not_called()
        assert "throttled" in result.lower()


class TestPrePostSeleniumGatedOnBreaker:
    def test_pre_post_selenium_skipped_when_breaker_open_but_post_still_dispatched(self):
        from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
        sched = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        with patch(f"{_MOD}.rate_limit_cooldown_remaining", return_value=900), \
             patch(f"{_MOD}.is_automation_paused", return_value=False), \
             patch(_PATCH_GET_POSTS, return_value=[(101, sched, 1)]), \
             patch(_PATCH_GET_ACTIVE, return_value=[1]), \
             patch(_PATCH_UPDATE_POST_STATUS), \
             patch(_PATCH_GET_ORPHANED, return_value=[]), \
             patch(_PATCH_POST_TO_LINKEDIN) as post, \
             patch(_PATCH_AUTOMATE_COMMENTING) as commenting, \
             patch(_PATCH_AUTOMATE_PROFILE_VIEWER) as viewer:
            auto_check_scheduled_posts()
        post.apply_async.assert_called_once()      # API post still goes out
        commenting.apply_async.assert_not_called()  # Selenium pre-post gated off
        viewer.apply_async.assert_not_called()
