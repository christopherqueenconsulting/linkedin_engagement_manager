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
