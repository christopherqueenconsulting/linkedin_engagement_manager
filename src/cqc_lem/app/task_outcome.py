"""The ONE outcome vocabulary a lane task ends on, so Celery's task state matches what happened (#2097).

A lane that catches its own failure and returns a string ends in Celery SUCCESS. Across 20,841 runs
in the 2026-09 audit window that meant 0 FAILURE and 0 RETRY, while the newsletter, profile-viewer
and DM lanes delivered nothing — so the `celery_task` failure tile had nothing it could ever fire on.

A lane now names its outcome through `lane_result`. `landed` and `no_op` return the result text as
before. `failed` raises `LaneTaskFailed` carrying that text, and a raised exception is the only
thing that makes Celery record FAILURE and emit the `task-failed` event Flower counts.
`update_state(FAILURE)` + `Ignore` writes the backend but emits no such event, so it stays blind.
`on_task_failure` does not file a `LaneTaskFailed` to PostHog: the lane has already logged the
failure at its own level, and that log line owns the `$exception`.

What counts as `failed`: the run was stopped by a FAULT — a session that would not open, a raise, a
crashed tab, a publish flow that did not complete, a send that did not land. A designed stop (the 429
back-off, a dedup skip, a daily cap, a walk's own time budget, a deploy ending the session) is
`no_op` or `landed`, by whether anything landed.
"""

from enum import Enum
from typing import Any, Optional

from celery import Task

from cqc_lem.utilities.ai_spend_cap import DailyAICapReached
from cqc_lem.utilities.engagement_window import SessionDeferred
from cqc_lem.utilities.linkedin.rate_limit import (
    begin_egress_task_scope,
    egress_unreachable_in_task,
    end_egress_task_scope,
)
from cqc_lem.utilities.logger import log_debug


class TaskOutcome(str, Enum):
    """How a lane run ended. Only `FAILED` changes the Celery state."""

    LANDED = "landed"
    NO_OP = "no_op"
    FAILED = "failed"


class LaneTaskFailed(Exception):
    """Raised by `lane_result` to end a lane task in Celery FAILURE; its message is the result text."""


def landed_or_no_op(landed: int) -> TaskOutcome:
    """The outcome of a run that ended without a fault: `LANDED` when it delivered anything at all."""
    return TaskOutcome.LANDED if landed else TaskOutcome.NO_OP


def lane_result(outcome: TaskOutcome, message: str, cause: Optional[BaseException] = None) -> str:
    """Return `message` as the task result, or raise `LaneTaskFailed` when the outcome is `FAILED`.

    Args:
        outcome: How the run ended.
        message: The result text Flower shows, for every outcome.
        cause: The exception behind a failure, chained so the task traceback keeps it.

    Returns:
        `message`, for a `LANDED` or `NO_OP` outcome.

    Raises:
        LaneTaskFailed: When `outcome` is `FAILED`.
    """
    if outcome is TaskOutcome.FAILED:
        if cause is None:
            # Never `from None`: that would suppress the implicit context of a raise inside `except`.
            raise LaneTaskFailed(message)
        raise LaneTaskFailed(message) from cause
    return message


class DailyCapAwareTask(Task):
    """Every LEM task's base: the free-trial daily AI cap ends a run as `no_op`, never FAILURE (#2378).

    The cap is a designed stop, exactly the kind the outcome contract above calls `no_op`. Without
    this, a refusal raised mid-task would end the task in Celery FAILURE and count against the
    `celery_task` failure alert for working behaviour. Converted HERE, at the task boundary, because
    a task cannot know in advance which of its calls the cap will refuse.

    `app.Task` for the Celery app (`task_cls`) and mixed into `queue_once.QueueOnce`, so a
    `QueueOnce`-based task gets it too.
    """

    abstract = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Run the task; a cap refusal returns a `no_op` result, an egress-blocked run raises.

        Raises:
            LaneTaskFailed: The task's last login could not reach linkedin.com through the egress
                route and the lane deferred (see `egress_failed_result`).
        """
        token = begin_egress_task_scope()
        try:
            try:
                result = super().__call__(*args, **kwargs)
            except DailyAICapReached as exc:
                # DEBUG: the gate already logged the user's transition once at INFO.
                log_debug("Task stopped by the free-trial daily AI cap", user_id=exc.user_id,
                          task_name=getattr(self, "name", None))
                return lane_result(TaskOutcome.NO_OP,
                                   f"Paused by the free-trial daily AI cap until {exc.resets_at.isoformat()}")
            except SessionDeferred as exc:
                return self._requeue_deferred(exc, args, kwargs)
            if token is not None and egress_unreachable_in_task():
                return egress_failed_result(result)
            return result
        finally:
            end_egress_task_scope(token)

    def _requeue_deferred(self, exc: SessionDeferred, args: Any, kwargs: Any) -> str:
        """Re-run this task at the next session window, so an out-of-window run is deferred, never dropped (#2628).

        If the re-queue itself fails the original error is raised: a lost deferral must not read as
        a success.
        """
        try:
            self.apply_async(args=args, kwargs=kwargs, eta=exc.eta)
        except Exception as requeue_error:
            raise exc from requeue_error
        log_debug("Task deferred to the next session window", user_id=exc.user_id,
                  task_name=getattr(self, "name", None))
        return lane_result(TaskOutcome.NO_OP, f"Deferred to the next session window: {exc.eta.isoformat()}")


EGRESS_FAILED_PREFIX = "Egress unreachable"


def egress_failed_result(result: Any) -> str:
    """End a run whose browser never reached linkedin.com in Celery FAILURE (#2620).

    The lane already deferred the work its own way (no attempt charged, the 429 breaker untouched)
    and the login gate already counted the failure toward the once-per-streak escalation (#2346).
    This only stops the run READING as a success: before, "Skipped — rate limited" ended in
    SUCCESS, so a 10-hour outage on 2026-11-23 showed nothing but successful tasks. A route that
    does not work is a fault under the outcome contract above, not a designed stop.

    Args:
        result: What the lane returned; kept in the failure text.

    Raises:
        LaneTaskFailed: Always, carrying ``"Egress unreachable — ..."`` and the lane's own text.
    """
    return lane_result(TaskOutcome.FAILED,
                       f"{EGRESS_FAILED_PREFIX} — the browser could not reach linkedin.com through "
                       f"its egress route; the lane deferred: {result}")
