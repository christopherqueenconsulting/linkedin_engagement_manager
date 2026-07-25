"""Progress + completion-notification wiring for auto_create_weekly_content (issue #545)."""

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
         patch(f"{_RCP}.record_post_generated") as generated, \
         patch(f"{_RCP}.record_post_failed") as failed, \
         patch(f"{_RCP}.notify_content_generation_ready") as notify:
        yield {"in_progress": in_progress, "finished": finished, "generated": generated,
               "failed": failed, "notify": notify}


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

        progress["in_progress"].assert_called_once_with(1, [])
        progress["finished"].assert_called_once_with(1)
        progress["notify"].assert_not_called()

    def test_full_buffer_closes_out_the_queued_run(self, progress):
        """Nothing to generate is still an ANSWER — the SPA must stop showing 'queued'."""
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches([_planned(1, 11)],
                                      **{"count_ready_posts_within_buffer": {"return_value": 99}})
        with _Patched(patches):
            auto_create_weekly_content(user_id=1)

        progress["in_progress"].assert_called_once_with(1, [])
        progress["finished"].assert_called_once_with(1)
        progress["generated"].assert_not_called()

    def test_lock_loser_closes_out_the_queued_run(self, progress):
        from cqc_lem.app.run_content_plan import auto_create_weekly_content
        patches = _generation_patches([_planned(1, 11)],
                                      **{"acquire_run_lock": {"return_value": None}})
        with _Patched(patches):
            auto_create_weekly_content(user_id=1)

        progress["in_progress"].assert_called_once_with(1, [])
        progress["finished"].assert_called_once_with(1)
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
