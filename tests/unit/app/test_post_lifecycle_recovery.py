"""The post lifecycle survives a lost worker, and the warm-up keeps its lane (issue #2032).

Two holes on either side of `post_to_linkedin`'s success branch.

The seed comment, the golden-hour reply sweeps and the second wave are ALL dispatched from inside
that branch. A worker that dies between the status write and the dispatch block leaves a live post
with none of them, and nothing re-adds them — the orphan re-queue re-runs `post_to_linkedin`, which
returns early on the POSTED guard. The post is published, visible, and permanently without a first
comment.

And the pre-post warm-up is dispatched with `queue=se_prepost` explicitly, because on `se_engage` an
eta-bound run waits behind whatever 15-minute golden-hour loop is already going and starts after its
own window has closed (#553). Its self-requeue passed no queue, so every pass after the first went
straight back to the lane the dispatch had paid to escape.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_SCHED = "cqc_lem.app.run_scheduler"
_FEED = "cqc_lem.app.engagement.feed"


class TestAPublishedPostKeepsItsGoldenHour:
    def _run(self, missing):
        with patch(f"{_SCHED}.get_ready_to_post_posts", return_value=[]), \
             patch(f"{_SCHED}.get_orphaned_scheduled_posts", return_value=[]), \
             patch(f"{_SCHED}.get_ready_occasion_posts", return_value=[]), \
             patch(f"{_SCHED}.get_posts_missing_their_seed_comment", return_value=missing), \
             patch(f"{_SCHED}.auto_seed_comment_on_post") as seed, \
             patch(f"{_SCHED}.log_warning") as warn:
            from cqc_lem.app.run_scheduler import auto_check_scheduled_posts
            auto_check_scheduled_posts.run()
        return seed, warn

    def test_a_post_with_no_first_comment_is_re_armed(self):
        seed, warn = self._run([(9, 1)])

        seed.apply_async.assert_called_once_with(kwargs={"user_id": 1, "post_id": 9})
        warn.assert_called_once()

    def test_a_healthy_run_re_arms_nothing(self):
        seed, warn = self._run([])

        seed.apply_async.assert_not_called()
        warn.assert_not_called()

    def test_it_re_arms_every_affected_post(self):
        seed, _warn = self._run([(9, 1), (10, 1), (11, 2)])

        assert seed.apply_async.call_count == 3

    def test_the_publish_guard_is_untouched(self):
        """The publish guard is untouched.

        A SEPARATE reconciler, not a loosened POSTED guard: that guard is what stops a double
        publish, and the recovery must not be able to weaken it. Re-arming is safe on its own terms
        because the seed task is idempotent.
        """
        import inspect

        from cqc_lem.app.engagement.posting import post_to_linkedin

        body = inspect.getsource(post_to_linkedin)
        assert "PostStatus.POSTED" in body


class TestThePrePostWarmUpKeepsItsLane:
    def _requeue(self, post_id):
        """Drive one warm-up pass to the end of its window and capture the re-dispatch."""
        from cqc_lem.app.engagement import feed as mod

        driver, wait = MagicMock(), MagicMock()
        with patch(f"{_FEED}.is_commenting_held", return_value=False), \
             patch(f"{_FEED}.acquire_run_lock", return_value="tok"), \
             patch(f"{_FEED}.release_run_lock"), \
             patch(f"{_FEED}.get_current_profile", return_value=(driver, wait, "e", MagicMock())), \
             patch(f"{_FEED}.navigate_to_feed"), \
             patch(f"{_FEED}.comment_on_feed_inline", return_value=0), \
             patch(f"{_FEED}.record_pre_post_run"), \
             patch(f"{_FEED}.quit_gracefully"), \
             patch.object(mod.automate_commenting, "apply_async") as requeue:
            mod.automate_commenting.run(user_id=1, loop_for_duration=900, future_forward=60,
                                        post_id=post_id)
        return requeue

    def test_a_window_pass_re_queues_onto_se_prepost(self):
        from cqc_lem.app.celeryconfig import SE_PREPOST_QUEUE

        requeue = self._requeue(post_id=42)

        requeue.assert_called_once()
        assert requeue.call_args.kwargs["queue"] == SE_PREPOST_QUEUE

    def test_an_ordinary_run_keeps_the_default_lane(self):
        """An ordinary run keeps the default lane.

        Only a window run is lane-pinned: the golden-hour fan-out of the SAME task belongs on
        `se_engage`, and pinning it here would move it.
        """
        requeue = self._requeue(post_id=None)

        requeue.assert_called_once()
        assert "queue" not in requeue.call_args.kwargs

    def test_the_window_still_shortens_each_pass(self):
        """The lane is the only thing that changed — the loop still ends when its window does."""
        requeue = self._requeue(post_id=42)

        assert requeue.call_args.kwargs["kwargs"]["loop_for_duration"] < 900
