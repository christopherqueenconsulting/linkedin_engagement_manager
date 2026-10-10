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
        """Run the task; a cap refusal returns a `no_op` result instead of raising."""
        try:
            return super().__call__(*args, **kwargs)
        except DailyAICapReached as exc:
            # DEBUG: the gate already logged the user's transition once at INFO.
            log_debug("Task stopped by the free-trial daily AI cap", user_id=exc.user_id,
                      task_name=getattr(self, "name", None))
            return lane_result(TaskOutcome.NO_OP,
                               f"Paused by the free-trial daily AI cap until {exc.resets_at.isoformat()}")
