"""A run whose browser never reached linkedin.com ends in Celery FAILURE, not SUCCESS (#2620).

On 2026-11-23 every browser lane deferred on `LinkedInEgressUnreachable` for about 10 hours and
Celery recorded each run as succeeded ("Skipped — rate limited"). These tests drive real tasks
through `Task.apply()`, so what is asserted is the STATE Celery records, and they pin what must NOT
change: the lane's own deferral text and the once-per-streak escalation.
"""

from functools import lru_cache
from unittest.mock import patch

import pytest

from cqc_lem.app.task_outcome import EGRESS_FAILED_PREFIX, LaneTaskFailed
from cqc_lem.utilities.linkedin import rate_limit
from cqc_lem.utilities.linkedin.rate_limit import (
    LinkedInEgressUnreachable,
    begin_egress_task_scope,
    egress_unreachable_in_task,
    end_egress_task_scope,
    mark_egress_unreachable_in_task,
)

pytestmark = pytest.mark.unit


def _apply(task, **kwargs):
    from cqc_lem.app import my_celery
    with patch.object(my_celery, "track_task") as tracked, \
            patch.object(my_celery, "capture_exception") as captured:
        result = task.apply(kwargs=kwargs)
    return result, tracked, captured


@lru_cache(maxsize=None)
def _lane_that_defers_on_egress():
    """A lane shaped like the real ones: the login gate marks the failure, the lane returns a skip."""
    from cqc_lem.app import my_celery

    @my_celery.app.task(name="tests.egress.deferring_lane")
    def deferring_lane(user_id: int):
        try:
            # What helper._note_egress_failure does right before raising.
            mark_egress_unreachable_in_task()
            raise LinkedInEgressUnreachable("Base page did not load on LinkedIn's origin")
        except rate_limit.LinkedInRateLimited as e:
            return f"Skipped — rate limited: {e}"

    return deferring_lane


class TestTaskBoundary:
    def test_an_egress_deferral_ends_in_failure_with_the_lane_text(self):
        result, tracked, captured = _apply(_lane_that_defers_on_egress(), user_id=1)
        assert result.state == "FAILURE"
        assert isinstance(result.result, LaneTaskFailed)
        text = str(result.result)
        assert text.startswith(EGRESS_FAILED_PREFIX)
        assert "Skipped — rate limited" in text
        assert tracked.call_args.kwargs["state"] == "FAILURE"
        # The lane already logged at its own level; a LaneTaskFailed is never filed to PostHog.
        captured.assert_not_called()

    def test_a_plain_rate_limit_skip_still_ends_in_success(self):
        from cqc_lem.app import my_celery

        @my_celery.app.task(name="tests.egress.throttled_lane")
        def throttled_lane():
            return "Skipped — rate limited: LinkedIn 429 circuit breaker open"

        result, _, _ = _apply(throttled_lane)
        assert result.state == "SUCCESS"

    def test_a_login_that_works_later_in_the_task_clears_the_mark(self):
        from cqc_lem.app import my_celery

        @my_celery.app.task(name="tests.egress.recovered_lane")
        def recovered_lane():
            mark_egress_unreachable_in_task()
            mark_egress_unreachable_in_task(False)  # what _clear_egress_streak does on success
            return "landed 1"

        result, _, _ = _apply(recovered_lane)
        assert result.state == "SUCCESS" and result.result == "landed 1"

    def test_the_mark_does_not_leak_into_the_next_task(self):
        _apply(_lane_that_defers_on_egress(), user_id=1)
        from cqc_lem.app import my_celery

        @my_celery.app.task(name="tests.egress.next_task")
        def next_task():
            return "ok"

        result, _, _ = _apply(next_task)
        assert result.state == "SUCCESS"
        assert egress_unreachable_in_task() is False

    def test_a_nested_task_call_reports_once_at_the_outermost_task(self):
        from cqc_lem.app import my_celery
        inner = _lane_that_defers_on_egress()

        @my_celery.app.task(name="tests.egress.outer_task")
        def outer_task():
            # Calling a task directly runs its __call__ in this task's scope: it must return the
            # lane's own text (no raise inside the outer task), and the OUTER task reports.
            assert inner(user_id=1).startswith("Skipped")
            return "outer done"

        result, _, _ = _apply(outer_task)
        assert result.state == "FAILURE"
        assert "outer done" in str(result.result)


class TestScope:
    def test_marking_outside_a_task_is_a_no_op(self):
        mark_egress_unreachable_in_task()
        assert egress_unreachable_in_task() is False

    def test_a_nested_scope_shares_the_outer_one(self):
        outer = begin_egress_task_scope()
        try:
            inner = begin_egress_task_scope()
            assert inner is None
            mark_egress_unreachable_in_task()
            end_egress_task_scope(inner)
            assert egress_unreachable_in_task() is True
        finally:
            end_egress_task_scope(outer)
        assert egress_unreachable_in_task() is False


class TestLoginGateMarksTheTask:
    def test_note_egress_failure_marks_even_without_a_user(self):
        from cqc_lem.utilities.linkedin import helper
        token = begin_egress_task_scope()
        try:
            helper._note_egress_failure(None)
            assert egress_unreachable_in_task() is True
        finally:
            end_egress_task_scope(token)

    def test_note_egress_failure_keeps_the_streak_escalation(self):
        from cqc_lem.utilities.linkedin import helper
        token = begin_egress_task_scope()
        try:
            with patch.object(helper, "record_egress_failure",
                              return_value={"escalate_now": False}) as recorded:
                helper._note_egress_failure(5)
            recorded.assert_called_once_with(5, kind="unknown", host_class="unknown")
            assert egress_unreachable_in_task() is True
        finally:
            end_egress_task_scope(token)

    def test_a_successful_login_clears_the_mark(self):
        from cqc_lem.utilities.linkedin import helper
        token = begin_egress_task_scope()
        try:
            mark_egress_unreachable_in_task()
            with patch.object(helper, "clear_egress_failures", return_value=None):
                helper._clear_egress_streak(5)
            assert egress_unreachable_in_task() is False
        finally:
            end_egress_task_scope(token)
