"""A cadence change takes effect on the next plan run, not in seven weeks (issue #2021).

`auto_create_content_plan` starts the day after the last `planning` row and bails when that is more
than 30 days out. Production on 2026-09-10: the tail ran to 2026-10-30, so every run since Sept 3
logged `Content Plan | Start Date: 2026-10-31 … | >30 days out | Skipped`. The user changed
`posts_per_week` to 3 that day; the 34 rows already laid at Mon-Fri 5/week would have kept
publishing until the end of October, because nothing reconciled them.

A setting that takes seven weeks to do anything reads to the user as a setting that does nothing.

The decision is pure and lives here; the delete re-asserts `status='planning'` and `scheduled_time >
NOW()` in its own WHERE clause, so even a stale id list cannot remove an approved or published post.
"""

from datetime import datetime

import pytest

from cqc_lem.app.run_content_plan import planned_slots_to_drop

pytestmark = pytest.mark.unit

MON, TUE, WED, THU, FRI, SAT = 0, 1, 2, 3, 4, 5


def _row(post_id: int, iso: str):
    return (post_id, datetime.fromisoformat(iso))


class TestTheDayAllowList:
    """`posting_days` (#581) is the HARDER bound: cadence says how many, this says which days."""

    def test_a_slot_on_a_switched_off_day_is_dropped(self):
        rows = [_row(1, "2026-09-14T09:00"),   # Monday
                _row(2, "2026-09-15T09:00")]   # Tuesday, not in the allow-list
        assert planned_slots_to_drop(rows, {MON, WED, FRI}, 3) == [2]

    def test_an_empty_allow_list_does_not_drop_everything(self):
        """'Not configured' must not read as 'no day is eligible' — that would empty the plan."""
        rows = [_row(1, "2026-09-14T09:00"), _row(2, "2026-09-15T09:00")]
        assert planned_slots_to_drop(rows, set(), 7) == []


class TestTheWeeklyCount:
    def test_a_week_is_trimmed_to_the_cadence(self):
        """Mon-Fri laid, 3/week configured: Thursday and Friday go."""
        rows = [_row(1, "2026-09-14T09:00"), _row(2, "2026-09-15T09:00"),
                _row(3, "2026-09-16T09:00"), _row(4, "2026-09-17T09:00"),
                _row(5, "2026-09-18T09:00")]
        assert planned_slots_to_drop(rows, {MON, TUE, WED, THU, FRI}, 3) == [4, 5]

    def test_the_earliest_slots_in_a_week_survive(self):
        """The earliest slots in a week survive.

        Lowering a cadence trims the END of each week rather than reshuffling what is already laid
        — the same "adding days never moves the ones in use" property `weekly_post_slots` has.
        """
        rows = [_row(9, "2026-09-14T09:00"), _row(8, "2026-09-16T09:00"),
                _row(7, "2026-09-18T09:00")]
        assert planned_slots_to_drop(rows, {MON, WED, FRI}, 2) == [7]

    def test_each_week_is_counted_separately(self):
        rows = [_row(1, "2026-09-14T09:00"), _row(2, "2026-09-16T09:00"),
                _row(3, "2026-09-21T09:00"), _row(4, "2026-09-23T09:00")]
        assert planned_slots_to_drop(rows, {MON, WED}, 2) == []

    def test_a_week_that_straddles_the_year_is_still_one_week(self):
        """ISO week keys carry the year, so late December and early January cannot collide."""
        rows = [_row(1, "2026-12-28T09:00"), _row(2, "2027-01-04T09:00")]
        assert planned_slots_to_drop(rows, {MON}, 1) == []


class TestTheProductionShape:
    def test_the_september_tail_is_trimmed_to_three_a_week(self):
        """The real rows: Mon-Fri through two weeks, against `posts_per_week=3`."""
        rows = [_row(103, "2026-09-15T08:17"), _row(104, "2026-09-16T08:18"),
                _row(105, "2026-09-17T12:34"), _row(106, "2026-09-18T12:35"),
                _row(107, "2026-09-21T08:00"), _row(108, "2026-09-22T08:00"),
                _row(109, "2026-09-23T08:23"), _row(110, "2026-09-24T12:38"),
                _row(111, "2026-09-25T12:39")]
        drop = planned_slots_to_drop(rows, {MON, TUE, WED, THU, FRI}, 3)
        # Week 1 already holds four (Tue-Fri) → the last one goes; week 2 holds five → two go.
        assert drop == [106, 110, 111]

    def test_a_saturday_slot_goes_when_saturday_is_switched_off(self):
        """Weekends are opt-in. A 6-7/week cadence used to produce them as a side effect."""
        rows = [_row(1, "2026-09-19T09:00")]  # Saturday
        assert planned_slots_to_drop(rows, {MON, TUE, WED, THU, FRI}, 5) == [1]


class TestItIsConservative:
    def test_a_conforming_tail_is_left_alone(self):
        rows = [_row(1, "2026-09-14T09:00"), _row(2, "2026-09-16T09:00"),
                _row(3, "2026-09-18T09:00")]
        assert planned_slots_to_drop(rows, {MON, WED, FRI}, 3) == []

    def test_no_rows_is_not_an_error(self):
        assert planned_slots_to_drop([], {MON}, 3) == []
        assert planned_slots_to_drop(None, {MON}, 3) == []

    def test_a_row_with_no_date_is_skipped_rather_than_dropped(self):
        """An unreadable slot is not evidence that it violates the cadence."""
        assert planned_slots_to_drop([(1, None)], {MON}, 3) == []


class TestTheDeleteCannotReachACommitment:
    def test_the_repository_reasserts_status_and_the_future(self):
        """The delete re-asserts status and the future in its own WHERE clause.

        Rather than trusting them from the caller: this deletes rows, and the one thing it must
        never do — even given a stale id list — is remove a post somebody approved or that has
        already published.
        """
        import inspect

        from cqc_lem.platform.db.repositories.posts import delete_planned_posts

        sql = inspect.getsource(delete_planned_posts)
        assert "status = 'planning'" in sql
        assert "scheduled_time > NOW()" in sql

    def test_an_empty_id_list_runs_no_query(self):
        from unittest.mock import patch

        from cqc_lem.platform.db.repositories.posts import delete_planned_posts

        with patch("cqc_lem.platform.db.connection.get_db_connection") as conn:
            assert delete_planned_posts(1, []) == 0
        conn.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #2137: the reconcile could only take slots away, so production sat at 2/week
# ---------------------------------------------------------------------------

_RCP = "cqc_lem.app.run_content_plan"
SUN = 6
TUE_THU_SAT = [TUE, THU, SAT]  # production `posting_days`: [1, 3, 5]


class TestCommittedRowsFillTheirWeek:
    def test_a_committed_post_is_counted_but_never_dropped(self):
        """An approved Friday is a commitment: it stays, and it uses one of the week's slots."""
        rows = [(1, datetime.fromisoformat("2026-09-15T13:00"), "planning"),   # Tue
                (2, datetime.fromisoformat("2026-09-17T13:00"), "planning"),   # Thu
                (3, datetime.fromisoformat("2026-09-18T13:00"), "approved"),   # Fri, off-day
                (4, datetime.fromisoformat("2026-09-19T13:00"), "planning")]   # Sat
        assert planned_slots_to_drop(rows, {TUE, THU, SAT}, 3) == [4]

    def test_the_weekday_is_judged_in_the_users_zone(self):
        """Sat 01:00 UTC is Friday evening in New York — Friday is not a posting day there."""
        import pytz

        rows = [(1, datetime.fromisoformat("2026-09-19T01:00"), "planning")]
        assert planned_slots_to_drop(rows, {TUE, THU, SAT}, 3) == []
        assert planned_slots_to_drop(rows, {TUE, THU, SAT}, 3,
                                     pytz.timezone("America/New_York")) == [1]


class TestCadenceHoles:
    def test_the_production_holes_are_the_saturdays(self):
        from datetime import date

        from cqc_lem.app.run_content_plan import cadence_holes

        candidates = [date(2026, 9, 29), date(2026, 10, 1), date(2026, 10, 3)]  # Tue/Thu/Sat
        occupied = [date(2026, 9, 29), date(2026, 10, 1)]
        assert cadence_holes(candidates, occupied, 3) == [date(2026, 10, 3)]

    def test_a_week_already_at_cadence_takes_nothing(self):
        """A committed post on an off day still fills the week — a refill never goes PAST cadence."""
        from datetime import date

        from cqc_lem.app.run_content_plan import cadence_holes

        candidates = [date(2026, 9, 29), date(2026, 10, 1), date(2026, 10, 3)]
        occupied = [date(2026, 9, 29), date(2026, 10, 1), date(2026, 10, 2)]  # + approved Fri
        assert cadence_holes(candidates, occupied, 3) == []

    def test_nothing_occupied_is_every_candidate(self):
        from datetime import date

        from cqc_lem.app.run_content_plan import cadence_holes

        candidates = [date(2026, 9, 29), date(2026, 10, 1), date(2026, 10, 3)]
        assert cadence_holes(candidates, [], 3) == candidates
        assert cadence_holes(None, None, 3) == []


def _run_plan(rows, last_planned, posting_days=TUE_THU_SAT, posts_per_week=3,
              tz="America/New_York", post_hour=12, deleted=None):
    """plan_content_for_user against a stored tail; returns (saved plan, delete mock)."""
    from datetime import time
    from unittest.mock import patch

    from cqc_lem.app.run_content_plan import plan_content_for_user

    saved = {}

    def _capture(_user_id, daily_plan):
        saved["plan"] = daily_plan

    with patch(f"{_RCP}.get_post_type_counts", return_value={"text": 1}), \
         patch(f"{_RCP}.get_future_post_slots", return_value=rows), \
         patch(f"{_RCP}.delete_planned_posts",
               side_effect=deleted or (lambda _u, ids: len(ids))) as delete, \
         patch(f"{_RCP}.get_last_planned_post_date_for_user", return_value=last_planned), \
         patch(f"{_RCP}.get_engagement_preferences",
               return_value={"posts_per_week": posts_per_week, "posting_days": posting_days}), \
         patch(f"{_RCP}.get_user_timezone", return_value=tz), \
         patch(f"{_RCP}.get_post_time", return_value=time(post_hour, 0)), \
         patch(f"{_RCP}.save_content_plan", side_effect=_capture):
        plan_content_for_user.run(user_id=1)
    return saved.get("plan", []), delete


def _tail(start: str, weeks: int, weekdays: list) -> list:
    """Planning rows at 16:00 UTC (12:00 New York) on `weekdays` for `weeks` weeks from Monday."""
    from datetime import timedelta

    monday = datetime.fromisoformat(start)
    rows, post_id = [], 100
    for week in range(weeks):
        for wd in weekdays:
            rows.append((post_id, monday + timedelta(days=7 * week + wd, hours=16), "planning"))
            post_id += 1
    return rows


def _local_dates(instants, tz="America/New_York"):
    import pytz

    zone = pytz.timezone(tz)
    return [pytz.utc.localize(i).astimezone(zone).date() for i in instants]


class TestTheRefill:
    """Acceptance for #2137: after a cadence change the next 30 days carry `posts_per_week`."""

    def test_a_cadence_change_refills_the_next_30_days(self):
        """The production shape: a Mon-Fri tail 6 weeks deep, `posting_days` now Tue/Thu/Sat.

        The reconcile drops Mon/Wed/Fri. Before #2137 that was the whole run — the tail still reached
        past 30 days, so the forward plan was skipped and every week sat at two.
        """
        from datetime import timedelta

        from freezegun import freeze_time

        with freeze_time("2026-09-13 12:00:00"):  # Sunday
            rows = _tail("2026-09-14", 7, [MON, TUE, WED, THU, FRI])
            plan, delete = _run_plan(rows, last_planned=rows[-1][1])

            dropped = set(delete.call_args.args[1])
            kept = [r[1] for r in rows if r[0] not in dropped]
            laid = [p["scheduled_datetime"] for p in plan]
            horizon = datetime(2026, 9, 13).date() + timedelta(days=30)
            per_week: dict = {}
            for day in _local_dates(kept + laid):
                if day <= horizon:
                    per_week.setdefault(day.isocalendar()[:2], []).append(day)

        assert laid, "the refill laid nothing — the plan stays at two a week"
        full_weeks = [w for w in per_week if (2026, 38) <= w <= (2026, 41)]
        assert full_weeks and all(len(per_week[w]) == 3 for w in full_weeks), per_week

    def test_no_slot_is_laid_outside_posting_days(self):
        """Every laid slot sits on a configured day in the USER'S zone — even an evening one."""
        from freezegun import freeze_time

        with freeze_time("2026-09-13 12:00:00"):
            rows = _tail("2026-09-14", 7, [MON, TUE, WED, THU, FRI])
            plan, _delete = _run_plan(rows, last_planned=rows[-1][1], post_hour=21)
        laid = _local_dates([p["scheduled_datetime"] for p in plan])
        assert laid
        assert {d.weekday() for d in laid} <= set(TUE_THU_SAT)

    def test_an_unreadable_tail_refills_nothing(self):
        """A slot the planner cannot see is not a hole — refilling blind would double-book a day."""
        from freezegun import freeze_time

        with freeze_time("2026-09-13 12:00:00"):
            plan, delete = _run_plan(None, last_planned=datetime(2026, 10, 30, 16))
        assert plan == []
        delete.assert_not_called()

    def test_a_partial_delete_refills_nothing(self):
        """If not every drop landed, the survivors are unknown — fail closed."""
        from freezegun import freeze_time

        with freeze_time("2026-09-13 12:00:00"):
            rows = _tail("2026-09-14", 7, [MON, TUE, WED, THU, FRI])
            plan, _delete = _run_plan(rows, last_planned=rows[-1][1], deleted=lambda _u, _ids: 0)
        assert plan == []

    def test_a_conforming_tail_is_left_as_it_is(self):
        from freezegun import freeze_time

        with freeze_time("2026-09-13 12:00:00"):
            rows = _tail("2026-09-14", 7, TUE_THU_SAT)
            plan, delete = _run_plan(rows, last_planned=rows[-1][1])
        assert plan == []
        delete.assert_not_called()


class TestTheTimingBounds:
    def test_a_24h_push_onto_an_off_day_is_not_laid(self):
        """The floor may push a slot past midnight; a slot on a switched-off day is never laid."""
        from datetime import date, time
        from unittest.mock import patch

        import pytz

        from cqc_lem.app.run_content_plan import _time_slots

        stored = [datetime(2026, 9, 18, 23, 59, 50)]  # Friday, last minute
        with patch(f"{_RCP}.get_user_timezone", return_value="UTC"), \
             patch(f"{_RCP}.get_post_time", return_value=time(9, 0)):
            timed = _time_slots([date(2026, 9, 19)], 1, {SAT}, pytz.utc, stored)
        assert timed == []

    def test_a_hole_inside_24h_of_a_later_stored_post_is_not_laid(self):
        from datetime import date, time
        from unittest.mock import patch

        import pytz

        from cqc_lem.app.run_content_plan import _time_slots

        stored = [datetime(2026, 9, 20, 1, 0)]  # Sunday 01:00
        with patch(f"{_RCP}.get_user_timezone", return_value="UTC"), \
             patch(f"{_RCP}.get_post_time", return_value=time(20, 0)):
            assert _time_slots([date(2026, 9, 19)], 1, {SAT, SUN}, pytz.utc, stored) == []
            laid = _time_slots([date(2026, 9, 19)], 1, {SAT, SUN}, pytz.utc, [])
        assert [d for d, _ in laid] == [date(2026, 9, 19)]

    def test_east_of_utc_the_first_forward_slot_never_shares_the_last_posts_day(self):
        """Sat 08:00 Tokyo is Fri 23:00 UTC, so the forward plan starts on (UTC) Saturday.

        Until #2137 the floor was seeded with that last post and pushed the slot off; the floor now
        only sees earlier LOCAL days, so the stored-post check has to look both ways.
        """
        from freezegun import freeze_time

        with freeze_time("2026-09-13 12:00:00"):
            rows = [(1, datetime(2026, 9, 18, 23, 0), "planning")]  # Sat 08:00 Asia/Tokyo
            plan, _delete = _run_plan(rows, last_planned=rows[0][1], posting_days=[SAT, SUN],
                                      posts_per_week=2, tz="Asia/Tokyo", post_hour=20)
        laid = _local_dates([p["scheduled_datetime"] for p in plan], "Asia/Tokyo")
        assert datetime(2026, 9, 19).date() not in laid
        assert laid, "the rest of the forward plan still lays"
