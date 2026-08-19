"""The result backend Celery loads: a Redis backend whose pubsub cannot veto a dispatch.

`Celery.send_task` calls `backend.on_task_call()` BEFORE it publishes the message
(`celery/app/base.py`), and the Redis backend's implementation SUBSCRIBES the caller's pubsub to
the result channel so that a later `AsyncResult.get()` is event-driven instead of polled. That
subscribe buys LEM nothing: nothing in the tree waits on a result, and the one reader that exists
(`GET /api/admin/task-status/{task_id}`) reads `AsyncResult.state`, a plain GET on the result key.

It costs something, though. If Redis is unreachable for even one connection attempt at exactly that
moment, the subscribe raises, its retry budget is spent reconnecting, and celery turns that into
`RuntimeError("Retry limit exceeded while trying to reconnect to the Celery result store backend")`
— so `send_task_message` never runs, the task is never queued, and the caller gets a 500 for work
its own database write already committed (issue #1674). The dispatcher is a client here, not a
worker: in a prefork child `task_join_will_block()` makes `on_task_call` a no-op, so this only ever
fires in the API and beat processes — exactly the two that queue work for everything else.

A genuine Redis outage still fails the dispatch, loudly and in the right place: the broker is the
SAME Redis instance, so `send_task_message` immediately after fails on its own and the caller sees
the outage as an outage. All this class removes is the ability of a *decoration* to veto the send.
"""

from typing import Any

from celery.backends.redis import RedisBackend

from cqc_lem.utilities.logger import log_debug


class ResilientRedisBackend(RedisBackend):
    """A `RedisBackend` whose `on_task_call` degrades instead of raising.

    `celeryconfig.resilient_result_backend` prefixes this class onto the configured Redis URL and
    `my_celery` pins the result on `app.backend_cls` — the one input `_get_backend()` reads ahead
    of `app.conf`, which answers `CELERY_RESULT_BACKEND` from the environment before anything
    else. `by_url` reads everything before the first `+` as the backend class, so the operator's
    URL reaches `RedisBackend.__init__` byte-for-byte.
    """

    def on_task_call(self, producer: Any, task_id: str) -> None:
        """Pre-subscribe the caller's result pubsub, treating an unreachable backend as a no-op.

        DEBUG, not WARNING: an unreachable Redis is about to fail the broker publish on the very
        next line of `send_task`, and that is the log line that owns the outage. Warning here as
        well would double-count it into a second grouped `$exception` for the same blip.
        """
        try:
            super().on_task_call(producer, task_id)
        except tuple(self.connection_errors) + (RuntimeError,) as exc:
            log_debug("Celery result pubsub subscribe skipped; dispatch continues",
                      task_id=task_id, error_type=type(exc).__name__)
