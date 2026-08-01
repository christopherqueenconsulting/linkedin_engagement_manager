"""`/health/deep` — the readiness probe an external monitor can actually use.

`/health` returned 200 while the entire Celery tier sat in `Created` for four hours (v0.118.0).
The API was genuinely fine; nothing reachable from outside knew automation was dead. These tests
pin the three answers that matter and, in particular, that an unreadable control channel is
reported as `unknown` rather than `healthy`."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MAIN = "cqc_lem.api.main"


def _call():
    from cqc_lem.api.main import health_check_deep
    return health_check_deep()


class TestHealthDeep:
    def test_reports_each_worker_and_its_lanes(self):
        replies = {
            "celery@worker": [{"name": "default"}],
            "celery@selenium": [{"name": "se_engage"}],
        }
        insp = MagicMock()
        insp.active_queues.return_value = replies
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            out = _call()
        assert out["status"] == "healthy"
        assert out["workers"] == 2
        assert out["lanes"]["celery@selenium"] == ["se_engage"]

    def test_no_consumers_is_degraded_not_healthy(self):
        """The exact v0.118.0 shape: broker up, every worker container never started. An empty
        reply must not read as healthy — that is the silence the outage hid behind."""
        insp = MagicMock()
        insp.active_queues.return_value = {}
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            out = _call()
        assert out["status"] == "degraded"
        assert out["workers"] == 0

    def test_none_reply_is_degraded(self):
        """`active_queues()` returns None (not {}) when nothing answers before the timeout."""
        insp = MagicMock()
        insp.active_queues.return_value = None
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            out = _call()
        assert out["status"] == "degraded"

    def test_unreachable_broker_is_unknown_never_healthy(self):
        """Unmeasured is never 'healthy' — a monitor that can't tell must say so."""
        with patch("cqc_lem.utilities.maintenance._inspect",
                   side_effect=RuntimeError("redis down")), \
             patch(f"{_MAIN}.log_warning") as warn:
            out = _call()
        assert out["status"] == "unknown"
        assert out["workers"] == 0
        warn.assert_called_once()

    def test_never_raises(self):
        """A monitor scraping this must get a body, not a 500 — the 500 tells it nothing."""
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=Exception("boom")), \
             patch(f"{_MAIN}.log_warning"):
            assert _call()["status"] == "unknown"

    def test_plain_health_stays_trivial(self):
        """`/health` gates the blue/green flip, so it must not gain a Redis/DB/Celery dependency —
        a deep check there would fail deploys whenever the broker hiccuped."""
        import inspect
        from cqc_lem.api.main import health_check
        body = inspect.getsource(health_check)
        for forbidden in ("_inspect", "redis", "mysql", "get_db"):
            assert forbidden not in body
