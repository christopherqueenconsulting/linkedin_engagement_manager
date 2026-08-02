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


@pytest.fixture(autouse=True)
def _no_maintenance():
    """Default every test to "not in a maintenance window" — and keep the suite hermetic: the real
    `is_maintenance_mode()` reaches Redis, and a unit test must not depend on a broker being
    absent (or, worse, present) in the environment it runs in."""
    with patch("cqc_lem.utilities.maintenance.is_maintenance_mode", return_value=False):
        yield


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

    def test_registered_but_consuming_nothing_is_degraded(self):
        """The gap this closes, observed live during the v0.120.0 deploy: `maint begin` cancels
        every queue consumer, so the whole tier stays registered and answering while consuming
        NOTHING — and the endpoint called that `healthy`.

        Registration was never the question a monitor is asking. No consumer means no task will
        run, which is the same outage as no worker at all."""
        insp = MagicMock()
        insp.active_queues.return_value = {
            "celery@worker": [],
            "celery@selenium": [],
        }
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            out = _call()
        assert out["status"] == "degraded"
        assert out["workers"] == 2       # they ARE there...
        assert out["consuming"] == 0     # ...and doing nothing

    def test_one_live_consumer_is_enough_to_be_healthy(self):
        """A single idle lane must not fail the whole check — a deploy recreates lanes one at a
        time, and flapping the monitor through every rollout is how an alert gets muted."""
        insp = MagicMock()
        insp.active_queues.return_value = {
            "celery@worker": [{"name": "celery"}],
            "celery@selenium": [],
        }
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            out = _call()
        assert out["status"] == "healthy"
        assert out["consuming"] == 1

    def test_declared_maintenance_window_is_not_an_outage(self):
        """`maint begin` cancels EVERY lane's consumer at once, and deploy.sh runs it on all four
        release windows a day. Reporting that as `degraded` would fire the documented monitor on
        every successful deploy — and an alert that cries wolf on every deploy gets muted.

        The state is still fully visible in the body (`consuming: 0`, `maintenance: true`); only
        the one field a monitor asserts on is held steady."""
        insp = MagicMock()
        insp.active_queues.return_value = {"celery@worker": [], "celery@selenium": []}
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp), \
             patch("cqc_lem.utilities.maintenance.is_maintenance_mode", return_value=True):
            out = _call()
        assert out["status"] == "healthy"
        assert out["maintenance"] is True
        assert out["consuming"] == 0     # the reader still sees exactly what is going on

    def test_maintenance_that_never_lifts_still_degrades(self):
        """The suppression above is bounded by the flag's OWN TTL — deploy.sh sets 1800s and
        `maint end` deletes it. A deploy that dies between begin and end leaves the consumers
        cancelled while the flag expires, and THAT is the state worth waking someone for."""
        insp = MagicMock()
        insp.active_queues.return_value = {"celery@worker": [], "celery@selenium": []}
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp), \
             patch("cqc_lem.utilities.maintenance.is_maintenance_mode", return_value=False):
            out = _call()
        assert out["status"] == "degraded"
        assert out["workers"] == 2
        assert out["consuming"] == 0

    def test_maintenance_never_upgrades_an_unknown_reading(self):
        """Suppression covers `degraded` only. An unreachable control channel means we did not
        measure anything, and unmeasured is never `healthy` — maintenance flag or not."""
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=RuntimeError("down")), \
             patch("cqc_lem.utilities.maintenance.is_maintenance_mode", return_value=True), \
             patch(f"{_MAIN}.log_warning"):
            out = _call()
        assert out["status"] == "unknown"
        assert out["maintenance"] is True

    def test_unreadable_maintenance_flag_never_suppresses_a_degraded_reading(self):
        """`None` means "could not tell whether a window was declared", and a window we cannot
        confirm must not silence a real zero-consumer outage."""
        insp = MagicMock()
        insp.active_queues.return_value = {"celery@worker": []}
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp), \
             patch("cqc_lem.utilities.maintenance.is_maintenance_mode",
                   side_effect=RuntimeError("redis down")):
            out = _call()
        assert out["status"] == "degraded"
        assert out["maintenance"] is None

    def test_unreadable_maintenance_flag_never_downgrades_a_good_answer(self):
        """Redis being unreadable must not turn a trusted control-channel reading into a worse
        one. None means 'could not tell', never False."""
        insp = MagicMock()
        insp.active_queues.return_value = {"celery@worker": [{"name": "celery"}]}
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp), \
             patch("cqc_lem.utilities.maintenance.is_maintenance_mode",
                   side_effect=RuntimeError("redis down")):
            out = _call()
        assert out["status"] == "healthy"
        assert out["maintenance"] is None

    def test_healthy_keyword_is_stable_for_body_assertions(self):
        """The UptimeRobot monitor keys on the literal `"status":"healthy"`. Any change that
        renames the field or the value silently disarms it — a monitor that can no longer match
        looks exactly like a monitor that is passing."""
        import json
        insp = MagicMock()
        insp.active_queues.return_value = {"celery@worker": [{"name": "celery"}]}
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            body = json.dumps(_call(), separators=(",", ":"))
        assert '"status":"healthy"' in body

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
