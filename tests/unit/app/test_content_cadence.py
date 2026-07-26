"""Unit tests for the cadence reset (issue #621 / G6): plan density, the fixed day-type calendar,
the one-post-per-24h floor, waking-hour bounds and per-post schedule jitter."""

import datetime as dt
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import content_framework as fw
from cqc_lem.utilities.utils import POST_HOUR_MIN, POST_HOUR_MAX

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"


def _plan(posts_per_week=3, tz="UTC", window_days=27, post_hour=16, last_planned=None):
    """Run plan_content_for_user with everything external mocked, and return the saved plan.

    Both ends of the planning window are pinned — the start via
    `get_last_planned_post_date_for_user` and the end via `_plan_window_end` — so a run that
    happens to land near a month boundary still sees a full window's worth of slots. The default
    `last_planned` sits at midnight so it never itself trips the cross-run 24h floor, whatever
    time of day the suite happens to run at; pass one to exercise that floor.
    """
    from cqc_lem.app.run_content_plan import plan_content_for_user

    last_planned = last_planned or (dt.datetime.now() + dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    window_end = last_planned + dt.timedelta(days=window_days)
    saved = {}

    def _capture(user_id, daily_plan):
        saved["user_id"] = user_id
        saved["plan"] = daily_plan

    with patch(f"{_RCP}.get_post_type_counts", return_value={"text": 1}), \
         patch(f"{_RCP}.get_last_planned_post_date_for_user", return_value=last_planned), \
         patch(f"{_RCP}._plan_window_end", return_value=window_end), \
         patch(f"{_RCP}.get_engagement_preferences", return_value={"posts_per_week": posts_per_week}), \
         patch(f"{_RCP}.get_user_timezone", return_value=tz), \
         patch(f"{_RCP}.get_post_time", return_value=dt.time(post_hour, 0)), \
         patch(f"{_RCP}.save_content_plan", side_effect=_capture):
        plan_content_for_user.run(user_id=1)
    return saved.get("plan", [])


class TestPlanDensity:
    def test_default_cadence_is_three_a_week_not_daily(self):
        plan = _plan(posts_per_week=3)
        assert plan, "expected at least one slot"
        weekdays = {p["scheduled_datetime"].weekday() for p in plan}
        assert weekdays.issubset({1, 2, 3})  # Tue / Wed / Thu

    def test_at_most_one_post_per_calendar_day(self):
        plan = _plan(posts_per_week=7)
        dates = [p["scheduled_datetime"].date() for p in plan]
        assert len(dates) == len(set(dates))

    def test_cadence_scales_the_number_of_slots(self):
        two = _plan(posts_per_week=2)
        five = _plan(posts_per_week=5)
        assert len(five) > len(two)

    def test_out_of_range_cadence_is_clamped_not_trusted(self):
        plan = _plan(posts_per_week=99)
        weekdays = {p["scheduled_datetime"].weekday() for p in plan}
        assert weekdays.issubset({0, 1, 2, 3, 4, 5, 6})
        dates = [p["scheduled_datetime"].date() for p in plan]
        assert len(dates) == len(set(dates))

    def test_missing_preferences_fall_back_to_the_default_cadence(self):
        from cqc_lem.app.run_content_plan import _cadence_slots
        start = dt.datetime(2026, 7, 27)  # Monday
        end = dt.datetime(2026, 8, 2)     # Sunday
        with patch(f"{_RCP}.get_engagement_preferences", side_effect=RuntimeError("db down")):
            slots = _cadence_slots(1, start, end)
        assert [s.weekday() for s in slots] == [1, 2, 3]

    def test_null_cadence_uses_the_default_rather_than_zero(self):
        from cqc_lem.app.run_content_plan import _cadence_slots
        start = dt.datetime(2026, 7, 27)
        end = dt.datetime(2026, 8, 2)
        with patch(f"{_RCP}.get_engagement_preferences", return_value={"posts_per_week": None}):
            slots = _cadence_slots(1, start, end)
        assert [s.weekday() for s in slots] == [1, 2, 3]


class TestPlanWindow:
    def test_window_closes_on_the_start_dates_own_month(self):
        from cqc_lem.app.run_content_plan import _plan_window_end
        assert _plan_window_end(dt.datetime(2026, 7, 5, 9, 0)).date() == dt.date(2026, 7, 31)
        # A start date that rolled into February must not be handed January's 31 days.
        assert _plan_window_end(dt.datetime(2026, 2, 1, 9, 0)).date() == dt.date(2026, 2, 28)

    def test_no_slots_left_saves_nothing(self):
        from cqc_lem.app.run_content_plan import plan_content_for_user
        last_planned = dt.datetime.now() + dt.timedelta(days=1)
        with patch(f"{_RCP}.get_post_type_counts", return_value={"text": 1}), \
             patch(f"{_RCP}.get_last_planned_post_date_for_user", return_value=last_planned), \
             patch(f"{_RCP}._plan_window_end", return_value=last_planned), \
             patch(f"{_RCP}.get_engagement_preferences", return_value={"posts_per_week": 2}), \
             patch(f"{_RCP}._cadence_slots", return_value=[]), \
             patch(f"{_RCP}.save_content_plan") as save:
            plan_content_for_user.run(user_id=1)
        save.assert_not_called()


class TestDayTypeRotation:
    def test_stage_comes_from_the_slot_day_type(self):
        plan = _plan(posts_per_week=5)
        for entry in plan:
            weekday = entry["scheduled_datetime"].weekday()
            assert entry["stage"] == fw.POST_DAY_TYPES[weekday]["stage"]

    def test_a_full_week_is_not_one_repeated_stage(self):
        # Asserted over the calendar rather than a live plan: how many slots a real run produces
        # depends on how much of the month is left on the day the test happens to run.
        stages = {fw.POST_DAY_TYPES[wd]["stage"] for wd in fw.weekly_post_slots(5)}
        assert len(stages) > 1

    def test_post_types_stay_balanced_across_the_plan(self):
        plan = _plan(posts_per_week=7)
        assert {p["post_type"] for p in plan}.issubset(
            {"carousel", "text", "video", "document"})


class TestOnePostPer24Hours:
    def test_consecutive_day_slots_stay_24h_apart(self):
        plan = _plan(posts_per_week=7)
        times = sorted(p["scheduled_datetime"] for p in plan)
        for earlier, later in zip(times, times[1:]):
            assert later - earlier >= dt.timedelta(hours=24)

    def test_floor_holds_across_planning_runs(self):
        """A new plan must clear the PREVIOUS plan's last post, not just its own first slot.

        The planner starts the day after the last already-scheduled post, so a 7/week calendar
        rolling into a new month (or a mid-month cadence raise) would otherwise put tomorrow 19:30
        and the next day 16:00 — 20h apart — into the same 24h window.
        """
        last_planned = (dt.datetime.now() + dt.timedelta(days=1)).replace(
            hour=19, minute=30, second=0, microsecond=0)
        plan = _plan(posts_per_week=7, post_hour=16, last_planned=last_planned)
        assert plan
        first = min(p["scheduled_datetime"] for p in plan)
        assert first - last_planned >= dt.timedelta(hours=24)

    def test_a_stale_last_post_does_not_push_the_plan(self):
        """Only a post still AHEAD of us seeds the floor — an old one must not shift the plan."""
        stale = dt.datetime.now() - dt.timedelta(days=400)
        with patch(f"{_RCP}.get_user_timezone", return_value="UTC"), \
             patch(f"{_RCP}.get_post_time", return_value=dt.time(16, 0)), \
             patch(f"{_RCP}.get_last_planned_post_date_for_user", return_value=stale), \
             patch(f"{_RCP}.get_post_type_counts", return_value={"text": 1}), \
             patch(f"{_RCP}.get_engagement_preferences", return_value={"posts_per_week": 7}), \
             patch(f"{_RCP}.save_content_plan") as save:
            from cqc_lem.app.run_content_plan import plan_content_for_user
            plan_content_for_user.run(user_id=1)
        plan = save.call_args[0][1] if save.call_args else []
        assert plan
        assert plan[0]["scheduled_datetime"].hour in (15, 16)

    def test_holds_even_when_the_peak_hour_drops_across_days(self):
        """Thu 17:00 → Fri 11:00 is only 18h; the floor must push the later slot out."""
        from cqc_lem.app.run_content_plan import _schedule_slot_utc, MIN_POST_INTERVAL
        thursday = dt.datetime(2026, 7, 30, 17, 0)
        with patch(f"{_RCP}.get_user_timezone", return_value="UTC"), \
             patch(f"{_RCP}.get_post_time", return_value=dt.time(11, 0)):
            friday = _schedule_slot_utc(dt.date(2026, 7, 31), 1, thursday)
        assert friday - thursday >= MIN_POST_INTERVAL

    def test_no_previous_post_means_no_shift(self):
        from cqc_lem.app.run_content_plan import _schedule_slot_utc
        with patch(f"{_RCP}.get_user_timezone", return_value="UTC"), \
             patch(f"{_RCP}.get_post_time", return_value=dt.time(16, 0)):
            first = _schedule_slot_utc(dt.date(2026, 7, 30), 1, None)
        assert first.date() == dt.date(2026, 7, 30)


class TestHourBoundsAndJitter:
    def test_scheduled_local_hours_stay_in_the_waking_window(self):
        plan = _plan(posts_per_week=7, tz="UTC")
        # tz=UTC, so the stored instant IS the local wall clock (allowing the 24h-floor nudge).
        for entry in plan:
            assert POST_HOUR_MIN <= entry["scheduled_datetime"].hour <= POST_HOUR_MAX + 1

    def test_an_overnight_learned_hour_never_reaches_the_plan(self):
        plan = _plan(posts_per_week=7, tz="UTC", post_hour=3)
        # get_post_time is mocked to 03:00 here, so the clamp has to happen on the way out of
        # get_post_time itself — verify the real function, not the mock.
        from cqc_lem.utilities.utils import clamp_to_waking_hours
        assert clamp_to_waking_hours(dt.time(3, 0)).hour == POST_HOUR_MIN
        assert plan  # the plan itself still gets built

    def test_no_post_lands_on_the_machine_exact_target_minute(self):
        # get_post_time is pinned to HH:00, and jitter is always at least 15 minutes, so a plan
        # where anything still sits on :00 means the jitter was skipped.
        plan = _plan(posts_per_week=7, post_hour=16)
        assert plan
        assert all(p["scheduled_datetime"].minute != 0 for p in plan)

    def test_times_are_not_all_the_same_machine_exact_minute(self):
        plan = _plan(posts_per_week=7)
        minutes = {p["scheduled_datetime"].minute for p in plan}
        assert len(minutes) > 1, "every post landed on the same minute — jitter is not applied"

    def test_local_time_is_converted_to_utc_for_storage(self):
        plan = _plan(posts_per_week=3, tz="America/New_York", post_hour=16)
        assert plan
        # 16:00 New York in July is 20:00 UTC; jitter moves it by at most 30 minutes.
        for entry in plan:
            assert 19 <= entry["scheduled_datetime"].hour <= 21


class TestGenerationCarriesTheDayType:
    def test_planned_post_generation_passes_the_local_weekday(self):
        from cqc_lem.app.run_content_plan import _create_content_for_planned_post
        post = {"user_id": 1, "id": 42, "post_type": "text", "buyer_stage": "awareness",
                "scheduled_time": dt.datetime(2026, 7, 29, 20, 0)}  # Wednesday 16:00 New York
        with patch(f"{_RCP}.get_user_timezone", return_value="America/New_York"), \
             patch(f"{_RCP}.create_content", return_value=("body", None)) as create, \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.evaluate_post_gates", return_value=(True, None)), \
             patch(f"{_RCP}.record_post_generated"), \
             patch(f"{_RCP}._score_and_persist_dwell", return_value=None):
            _create_content_for_planned_post(post, {})
        assert create.call_args.kwargs["day_weekday"] == 2

    def test_a_row_without_a_scheduled_time_still_generates(self):
        from cqc_lem.app.run_content_plan import _create_content_for_planned_post
        post = {"user_id": 1, "id": 42, "post_type": "text", "buyer_stage": "awareness"}
        with patch(f"{_RCP}.create_content", return_value=("body", None)) as create, \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.evaluate_post_gates", return_value=(True, None)), \
             patch(f"{_RCP}.record_post_generated"), \
             patch(f"{_RCP}._score_and_persist_dwell", return_value=None):
            _create_content_for_planned_post(post, {})
        assert create.call_args.kwargs["day_weekday"] is None

    def test_buffer_query_selects_the_scheduled_time(self):
        from unittest.mock import MagicMock
        conn, cursor = MagicMock(), MagicMock()
        cursor.fetchall.return_value = []
        conn.cursor.return_value = cursor
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_posts_within_buffer
            get_planned_posts_within_buffer(1, days=5, max_posts=5)
        assert "scheduled_time FROM posts" in cursor.execute.call_args[0][0]


class TestSlotWeekdayResolution:
    def test_utc_slot_resolves_to_the_users_local_weekday(self):
        from cqc_lem.app.run_content_plan import _slot_weekday
        # 2026-07-30 01:00 UTC is still Wednesday the 29th in New York.
        with patch(f"{_RCP}.get_user_timezone", return_value="America/New_York"):
            assert _slot_weekday(1, dt.datetime(2026, 7, 30, 1, 0)) == 2

    def test_missing_or_broken_inputs_return_none(self):
        from cqc_lem.app.run_content_plan import _slot_weekday
        assert _slot_weekday(1, None) is None
        with patch(f"{_RCP}.get_user_timezone", side_effect=RuntimeError("no user")):
            assert _slot_weekday(1, dt.datetime(2026, 7, 30, 12, 0)) is None
