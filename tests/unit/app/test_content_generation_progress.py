"""Progress + completion-notification wiring for auto_create_weekly_content (issue #545)."""

from datetime import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"


def _planned(user_id: int, post_id: int, post_type: str = "text"):
    return {"user_id": user_id, "id": post_id, "post_type": post_type, "buyer_stage": "awareness"}


def _generation_patches(planned, create_content_result=("Post text", None), **overrides):
    """The DB/AI seams the buffer top-up touches, all mocked to a happy path."""
    ctx = {
        "acquire_run_lock": {"return_value": "tok"},
        "release_run_lock": {},
        "count_ready_posts_within_buffer": {"return_value": 0},
        "get_planned_posts_within_buffer": {"return_value": planned},
        "get_next_planned_posts_after_buffer": {"return_value": []},
        "get_next_planned_post_date": {"return_value": None},
        "create_content": {"return_value": create_content_result},
        "update_db_post_content": {},
        "update_db_post_status": {},
        "get_user_preferences": {"return_value": {"auto_schedule_posts": True}},
        "_post_missing_required_asset": {"return_value": False},
        "get_post_authenticity_score": {"return_value": None},
        "_score_and_persist_dwell": {},
    }
    ctx.update(overrides)
    return {name: patch(f"{_RCP}.{name}", **kwargs) for name, kwargs in ctx.items()}


class _Patched:
    """Start/stop a dict of patches and expose them by name."""

    def __init__(self, patches: dict):
        self._patches = patches
        self.mocks = {}

    def __enter__(self):
        for name, p in self._patches.items():
            self.mocks[name] = p.start()
        return self.mocks

    def __exit__(self, *exc):
        for p in self._patches.values():
            p.stop()
        return False


@pytest.fixture
def progress():
    """Patch the progress store + notifier where run_content_plan imported them."""
    with patch(f"{_RCP}.mark_in_progress") as in_progress, \
         patch(f"{_RCP}.mark_finished") as finished, \
         patch(f"{_RCP}.mark_empty") as empty, \
         patch(f"{_RCP}.record_post_generated") as generated, \
         patch(f"{_RCP}.record_post_failed") as failed, \
         patch(f"{_RCP}.notify_content_generation_ready") as notify:
        yield {"in_progress": in_progress, "finished": finished, "empty": empty,
               "generated": generated, "failed": failed, "notify": notify}


class TestProgressTracking:
    def test_run_publishes_progress_and_notifies(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        with _Patched(_generation_patches([_planned(1, 11), _planned(1, 12)])):
            auto_create_weekly_content(user_id=1)

        progress["in_progress"].assert_called_once_with(1, [11, 12])
        assert [c.args for c in progress["generated"].call_args_list] == [(1, 11), (1, 12)]
        progress["failed"].assert_not_called()
        progress["finished"].assert_called_once_with(1)
        progress["notify"].assert_called_once_with(1, 2, 0)

    def test_generation_exception_counts_as_failed(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches(
            [_planned(1, 11)], **{"create_content": {"side_effect": RuntimeError("AI down")}})
        with _Patched(patches):
            auto_create_weekly_content(user_id=1)

        progress["failed"].assert_called_once_with(1, 11)
        progress["generated"].assert_not_called()
        progress["finished"].assert_called_once_with(1)
        progress["notify"].assert_not_called()  # nothing ready to review

    def test_empty_content_counts_as_failed(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches(
            [_planned(1, 11), _planned(1, 12)],
            **{"create_content": {"side_effect": [(None, None), ("Post text", None)]}})
        with _Patched(patches):
            auto_create_weekly_content(user_id=1)

        progress["failed"].assert_called_once_with(1, 11)
        progress["generated"].assert_called_once_with(1, 12)
        progress["notify"].assert_called_once_with(1, 1, 1)  # 1 ready, 1 failed

    def test_progress_is_per_user_on_the_all_users_run(self, progress):
        """The daily beat run passes user_id=None — each owner gets their own progress + email."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        by_user = {1: [_planned(1, 11)], 2: [_planned(2, 21), _planned(2, 22)]}
        patches = _generation_patches(
            None,
            **{"get_planned_posts_within_buffer": {"side_effect": lambda uid, *a, **kw: by_user[uid]}})
        with _Patched(patches), \
                patch(f"{_RCP}.get_user_ids_with_planned_posts_within_buffer", return_value=[1, 2]):
            auto_create_weekly_content()

        assert [c.args for c in progress["in_progress"].call_args_list] == [(1, [11]), (2, [21, 22])]
        assert [c.args for c in progress["finished"].call_args_list] == [(1,), (2,)]
        assert [c.args for c in progress["notify"].call_args_list] == [(1, 1, 0), (2, 2, 0)]

    def test_no_planned_posts_closes_out_the_queued_run(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        with _Patched(_generation_patches([])):
            auto_create_weekly_content(user_id=1)

        progress["empty"].assert_called_once()
        progress["in_progress"].assert_not_called()
        progress["notify"].assert_not_called()

    def test_full_buffer_closes_out_the_queued_run(self, progress):
        """Nothing to generate is still an ANSWER — the SPA must stop showing 'queued'."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches([_planned(1, 11)],
                                      **{"count_ready_posts_within_buffer": {"return_value": 99}})
        with _Patched(patches):
            auto_create_weekly_content(user_id=1)

        progress["empty"].assert_called_once()
        progress["in_progress"].assert_not_called()
        progress["generated"].assert_not_called()

    def test_lock_loser_closes_out_the_queued_run(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches([_planned(1, 11)],
                                      **{"acquire_run_lock": {"return_value": None}})
        with _Patched(patches):
            auto_create_weekly_content(user_id=1)

        progress["empty"].assert_called_once()
        progress["in_progress"].assert_not_called()
        progress["generated"].assert_not_called()

    def test_persist_failure_counts_as_failed_and_run_continues(self, progress):
        """A raise AFTER generation (video download, DB write) must not abort the whole run."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches(
            [_planned(1, 11), _planned(1, 12)],
            **{"update_db_post_content": {"side_effect": [RuntimeError("db gone"), None]}})
        with _Patched(patches):
            auto_create_weekly_content(user_id=1)

        progress["failed"].assert_called_once_with(1, 11)
        progress["generated"].assert_called_once_with(1, 12)
        progress["finished"].assert_called_once_with(1)
        progress["notify"].assert_called_once_with(1, 1, 1)

    def test_unexpected_raise_still_finishes_the_run(self, progress):
        """Belt-and-braces: even if the per-post loop blows up, the record can't stay in_progress."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches([_planned(1, 11)])
        with _Patched(patches), \
                patch(f"{_RCP}._create_content_for_planned_post",
                      side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                auto_create_weekly_content(user_id=1)

        progress["finished"].assert_called_once_with(1)
        progress["notify"].assert_not_called()

    def test_no_planned_posts_and_no_user_tracks_nothing(self, progress):
        """The beat run never published a 'queued' record, so there is nothing to close out."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        with _Patched(_generation_patches([])), \
                patch(f"{_RCP}.get_user_ids_with_planned_posts_within_buffer", return_value=[1]):
            auto_create_weekly_content()

        progress["in_progress"].assert_not_called()
        progress["finished"].assert_not_called()
        progress["empty"].assert_not_called()


class TestEmptyRunReason:
    """'0 posts are ready to review' with no reason reads as a broken button (issue #719)."""

    def _reason(self, progress):
        call = progress["empty"].call_args
        return call.args[1], call.kwargs

    def test_lock_loser_reports_already_running(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        from cqc_lem.utilities.content_generation_status import ContentGenerationEmptyReason
        patches = _generation_patches([_planned(1, 11)],
                                      **{"acquire_run_lock": {"return_value": None}})
        with _Patched(patches):
            auto_create_weekly_content(user_id=1)

        reason, _ = self._reason(progress)
        assert reason == ContentGenerationEmptyReason.ALREADY_RUNNING

    def test_full_buffer_reports_the_counts(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        from cqc_lem.utilities.content_generation_status import ContentGenerationEmptyReason
        patches = _generation_patches(
            [_planned(1, 11)],
            **{"count_ready_posts_within_buffer": {"return_value": 5},
               "get_user_preferences": {"return_value": {"content_buffer_days": 5,
                                                         "content_buffer_max_posts": 5}}})
        with _Patched(patches):
            auto_create_weekly_content(user_id=1)

        reason, kwargs = self._reason(progress)
        assert reason == ContentGenerationEmptyReason.BUFFER_FULL
        assert kwargs["ready_count"] == 5 and kwargs["buffer_max"] == 5
        assert kwargs["buffer_days"] == 5

    def test_nothing_planned_reports_the_next_slot_date(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        from cqc_lem.utilities.content_generation_status import ContentGenerationEmptyReason
        next_at = datetime(2026, 8, 3, 13, 30)
        with _Patched(_generation_patches(
                [], **{"get_next_planned_post_date": {"return_value": next_at}})):
            auto_create_weekly_content(user_id=1)

        reason, kwargs = self._reason(progress)
        assert reason == ContentGenerationEmptyReason.NO_PLANNED_SLOTS
        assert kwargs["next_planned_at"] == next_at

    def test_unreadable_next_slot_date_still_closes_the_run(self, progress):
        """The explanation is best-effort — a DB hiccup must not strand the SPA on 'queued'."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        with _Patched(_generation_patches(
                [], **{"get_next_planned_post_date": {"side_effect": RuntimeError("db gone")}})):
            auto_create_weekly_content(user_id=1)

        _, kwargs = self._reason(progress)
        assert kwargs["next_planned_at"] is None


class TestPullForward:
    """An explicit click on an empty window pulls the next planned posts forward (issue #719)."""

    def test_requested_run_generates_the_next_planned_posts(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches(
            [], **{"get_next_planned_posts_after_buffer": {"return_value": [_planned(1, 31)]}})
        with _Patched(patches) as mocks:
            auto_create_weekly_content(user_id=1)

        progress["in_progress"].assert_called_once_with(1, [31])
        progress["generated"].assert_called_once_with(1, 31)
        progress["empty"].assert_not_called()
        # Bounded by the same ceiling, counted across the whole planning horizon.
        assert mocks["get_next_planned_posts_after_buffer"].call_args.args[2] == 5

    def test_beat_run_never_pulls_forward(self, progress):
        """Generating a week early on autopilot is spend nobody asked for."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches(
            [], **{"get_next_planned_posts_after_buffer": {"return_value": [_planned(1, 31)]}})
        with _Patched(patches) as mocks, \
                patch(f"{_RCP}.get_user_ids_with_planned_posts_within_buffer", return_value=[1]):
            auto_create_weekly_content()

        mocks["get_next_planned_posts_after_buffer"].assert_not_called()
        progress["generated"].assert_not_called()

    def test_repeated_clicks_cannot_walk_the_cap_down_the_calendar(self, progress):
        """Everything ahead is already generated — report it instead of generating the month."""
        from cqc_lem.app.run_content_plan import (auto_create_weekly_content,
                                                  _PULL_FORWARD_READY_HORIZON_DAYS)
        from cqc_lem.utilities.content_generation_status import ContentGenerationEmptyReason
        # 0 ready inside the 5-day window, 5 ready across everything generated ahead.
        ready_by_days = {5: 0, _PULL_FORWARD_READY_HORIZON_DAYS: 5}
        patches = _generation_patches(
            [],
            **{"count_ready_posts_within_buffer": {
                "side_effect": lambda uid, days: ready_by_days[days]},
               "get_next_planned_posts_after_buffer": {"return_value": [_planned(1, 31)]}})
        with _Patched(patches) as mocks:
            auto_create_weekly_content(user_id=1)

        mocks["get_next_planned_posts_after_buffer"].assert_not_called()
        progress["generated"].assert_not_called()
        assert progress["empty"].call_args.args[1] == ContentGenerationEmptyReason.BUFFER_FULL
        assert progress["empty"].call_args.kwargs["ready_count"] == 5

    def test_cap_counts_posts_generated_past_the_buffer_ceiling(self, progress):
        """A pulled-forward post can land past MAX_CONTENT_BUFFER_DAYS — the plan appends a month
        after the LAST planning row, so a laid-out plan reaches ~60 days out. Counting the cap over
        the 30-day ceiling would not see those, and the next click would re-earn the whole cap."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        from cqc_lem.utilities.content_generation_status import ContentGenerationEmptyReason
        from cqc_lem.utilities.db import MAX_CONTENT_BUFFER_DAYS
        # Nothing ready within 30 days; the 5 already generated all sit BEYOND day 30.
        ready_by_days = {5: 0, MAX_CONTENT_BUFFER_DAYS: 0}
        patches = _generation_patches(
            [],
            **{"count_ready_posts_within_buffer": {
                "side_effect": lambda uid, days: ready_by_days.get(days, 5)},
               "get_next_planned_posts_after_buffer": {"return_value": [_planned(1, 31)]}})
        with _Patched(patches) as mocks:
            auto_create_weekly_content(user_id=1)

        mocks["get_next_planned_posts_after_buffer"].assert_not_called()
        progress["generated"].assert_not_called()
        assert progress["empty"].call_args.args[1] == ContentGenerationEmptyReason.BUFFER_FULL
