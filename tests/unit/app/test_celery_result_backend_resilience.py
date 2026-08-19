"""A momentarily unreachable Redis result backend must not cancel a dispatch (issue #1674).

`send_task` subscribes the caller's result pubsub BEFORE it publishes the message, so the
`RuntimeError` celery raises when that subscribe cannot reconnect used to take the whole dispatch
with it — the task was never queued and the caller got a 500. Three halves are asserted here: the
URL wiring that puts `ResilientRedisBackend` in front of a plain Redis URL, the `backend_cls`
assignment that is the ONLY way that wiring survives `CELERY_RESULT_BACKEND` being set in the
environment, and the class itself degrading to a no-op on exactly the unreachable-backend shapes.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery
from celery.app.backends import by_url
from celery.backends.redis import RedisBackend

from cqc_lem.app.celeryconfig import RESILIENT_REDIS_BACKEND, resilient_result_backend
from cqc_lem.app.result_backend import ResilientRedisBackend

pytestmark = pytest.mark.unit


class TestResilientResultBackendUrl:
    @pytest.mark.parametrize("url", [
        "redis://redis:6379/1",
        "redis://127.0.0.1:6379/0",
        "rediss://cache.example:6380/1?ssl_cert_reqs=required",
        "REDIS://redis:6379/1",
    ])
    def test_plain_redis_urls_are_wrapped(self, url):
        assert resilient_result_backend(url) == f"{RESILIENT_REDIS_BACKEND}+{url}"

    @pytest.mark.parametrize("url", [
        # A different backend class entirely — wrapping would swap the operator's choice out.
        "sentinel://sentinel1:26379/1",
        "redis+socket:///var/run/redis.sock",
        f"{RESILIENT_REDIS_BACKEND}+redis://redis:6379/1",
        "some.other:Backend+redis://redis:6379/1",
        "cache+memcached://localhost:11211/",
        "sqs://",
        # Not a URL at all: a bare alias, or nothing configured.
        "disabled",
        "",
    ])
    def test_everything_else_passes_through(self, url):
        assert resilient_result_backend(url) == url

    def test_configured_result_backend_uses_the_class(self):
        from cqc_lem.app import celeryconfig
        assert celeryconfig.result_backend.startswith(f"{RESILIENT_REDIS_BACKEND}+")

    def test_celery_resolves_the_class_and_keeps_the_url(self):
        """`by_url` must hand `RedisBackend.__init__` the operator's URL byte-for-byte."""
        backend_cls, url = by_url(resilient_result_backend("redis://redis:6379/1"))
        assert backend_cls is ResilientRedisBackend
        assert url == "redis://redis:6379/1"


class TestBackendClassSurvivesTheEnvironment:
    """`CELERY_RESULT_BACKEND` beats `config_from_object`, so the wiring cannot live in the config.

    `Settings.result_backend` returns `os.environ['CELERY_RESULT_BACKEND']` before it looks at
    anything `config_from_object` set (celery/app/utils.py), and the compose stack sets that
    variable — which is exactly how this fix would ship inert.
    """

    def test_env_var_still_beats_the_configured_value(self):
        """The precedence this whole seam exists to answer.

        If celery ever changes it, say so here rather than by silently loading the stock backend
        in production.
        """
        app = Celery("test-1674-precedence", broker="memory://",
                     backend="cqc_lem.app.result_backend:ResilientRedisBackend+redis://x:6379/1")
        assert app.conf.result_backend == os.environ["CELERY_RESULT_BACKEND"]

    def test_backend_cls_wins_and_builds_the_resilient_backend(self):
        app = Celery("test-1674-backend-cls", broker="memory://")
        app.backend_cls = resilient_result_backend("redis://localhost:6379/1")
        assert isinstance(app.backend, ResilientRedisBackend)
        assert app.backend.url == "redis://localhost:6379/1"

    def test_the_app_pins_the_configured_backend_on_backend_cls(self):
        from cqc_lem.app import celeryconfig
        from cqc_lem.app.my_celery import app
        assert app.backend_cls == celeryconfig.result_backend


def _backend() -> ResilientRedisBackend:
    """A real backend instance — constructing one opens no connection."""
    app = Celery("test-1674-instance", broker="memory://")
    app.backend_cls = resilient_result_backend("redis://localhost:6379/1")
    return app.backend


class TestOnTaskCall:
    def test_subscribes_when_the_backend_is_reachable(self):
        backend = _backend()
        with patch.object(RedisBackend, "on_task_call") as parent:
            backend.on_task_call(MagicMock(), "task-1")
        parent.assert_called_once()

    def test_retry_limit_exceeded_is_a_no_op(self):
        """The exact shape from #1674: celery gave up reconnecting and raised RuntimeError."""
        backend = _backend()
        boom = RuntimeError("Retry limit exceeded while trying to reconnect to the Celery "
                            "result store backend.")
        with patch.object(RedisBackend, "on_task_call", side_effect=boom):
            with patch("cqc_lem.app.result_backend.log_debug") as debug:
                assert backend.on_task_call(MagicMock(), "task-2") is None
        debug.assert_called_once()
        assert debug.call_args.kwargs["task_id"] == "task-2"
        assert debug.call_args.kwargs["error_type"] == "RuntimeError"

    def test_connection_error_is_a_no_op(self):
        backend = _backend()
        assert backend.connection_errors, "redis error classes must be available"
        boom = backend.connection_errors[0](
            "Error 111 connecting to redis:6379. Connection refused.")
        with patch.object(RedisBackend, "on_task_call", side_effect=boom):
            with patch("cqc_lem.app.result_backend.log_debug") as debug:
                assert backend.on_task_call(MagicMock(), "task-3") is None
        debug.assert_called_once()

    def test_an_unrelated_error_still_raises(self):
        """Only an unreachable backend is ridden out — a bug in the subscribe path is not."""
        backend = _backend()
        with patch.object(RedisBackend, "on_task_call",
                          side_effect=ValueError("bad task id")):
            with pytest.raises(ValueError):
                backend.on_task_call(MagicMock(), "task-4")
