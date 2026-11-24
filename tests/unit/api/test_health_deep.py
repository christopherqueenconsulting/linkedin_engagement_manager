"""`/health/deep` — the readiness probe an external monitor can actually use.

`/health` returned 200 while the entire Celery tier sat in `Created` for four hours (v0.118.0).
The API was genuinely fine; nothing reachable from outside knew automation was dead. These tests
pin the three answers that matter and, in particular, that an unreadable control channel is
reported as `unknown` rather than `healthy`.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MAIN = "cqc_lem.api.main"

# Captured at import, before the autouse fixture below patches it, for the one test that drives
# the real reading end to end.
from cqc_lem.utilities.egress_probe import egress_health as _REAL_EGRESS_HEALTH  # noqa: E402
from cqc_lem.utilities.linkedin.session_health import (  # noqa: E402
    session_health as _REAL_SESSION_HEALTH,
)


def _call():
    from cqc_lem.api.main import health_check_deep
    return health_check_deep()


@pytest.fixture(autouse=True)
def _no_maintenance():
    """Default every test to "not in a maintenance window" — and keep the suite hermetic: the real
    `is_maintenance_mode()` reaches Redis, and a unit test must not depend on a broker being
    absent (or, worse, present) in the environment it runs in.
    """
    with patch("cqc_lem.utilities.maintenance.is_maintenance_mode", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _egress_direct():
    """Default every test to "nothing proxied" so the egress probe never opens a socket.

    Tests of the egress reading itself patch `egress_health` / `should_degrade` explicitly.
    """
    with patch("cqc_lem.utilities.egress_probe.egress_health",
               return_value={"egress": "direct", "egress_checked": 0, "egress_failing": 0}), \
         patch("cqc_lem.utilities.egress_probe.should_degrade", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _session_unknown():
    """Default every test to an unmeasured LinkedIn session so nothing reaches Redis or MySQL.

    Tests of the session reading itself patch `session_health` explicitly.
    """
    with patch("cqc_lem.utilities.linkedin.session_health.session_health",
               return_value={"linkedin_session": "unknown", "session_checked": 0,
                             "session_failing": 0}):
        yield


@pytest.fixture(autouse=True)
def _no_egress_streak():
    """Default every test to "Redis unreadable" for the streak so nothing reaches Redis (#2620)."""
    with patch("cqc_lem.utilities.linkedin.rate_limit.egress_streak_summary", return_value=None):
        yield


class TestEgressStreakFields:
    """#2620: the escalated egress streak is visible to an external monitor — words and counts."""

    def _deep(self):
        insp = MagicMock()
        insp.active_queues.return_value = {"celery@w": [{"name": "default"}]}
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            return _call()

    def test_redis_down_reads_null(self):
        out = self._deep()
        assert out["egress_down_since"] is None
        assert out["egress_failures"] is None
        assert out["egress_failure_kind"] is None

    def test_escalated_streak_is_reported(self):
        summary = {"down_since": 1000, "failures": 12, "kind": "base_page_not_origin"}
        with patch("cqc_lem.utilities.linkedin.rate_limit.egress_streak_summary",
                   return_value=summary):
            out = self._deep()
        assert out["egress_down_since"] == 1000
        assert out["egress_failures"] == 12
        assert out["egress_failure_kind"] == "base_page_not_origin"
        assert out["status"] == "healthy"  # report-only: never changes `status`

    def test_no_outage_reads_zero_failures(self):
        with patch("cqc_lem.utilities.linkedin.rate_limit.egress_streak_summary",
                   return_value={"down_since": None, "failures": 0, "kind": None}):
            out = self._deep()
        assert out["egress_failures"] == 0 and out["egress_down_since"] is None

    def test_a_raising_reader_never_breaks_the_endpoint(self):
        with patch("cqc_lem.utilities.linkedin.rate_limit.egress_streak_summary",
                   side_effect=RuntimeError("boom")):
            out = self._deep()
        assert out["egress_failures"] is None


class TestHealthDeep:
    def test_counts_workers_and_consumers_without_naming_them(self):
        """The counts are derived from the per-worker lane map, but only the COUNTS ship.

        This endpoint is unauthenticated by design — an external dead-man's switch cannot carry a
        credential — so the body is public. `lanes` named container IDs and the internal queue
        topology (issue #1020); the counts it feeds do not.
        """
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
        assert out["consuming"] == 2

    def test_body_leaks_no_worker_or_queue_topology(self):
        """The whole point of #1020: a name that identifies a container or a queue must not appear
        in a body anyone on the internet can GET. Asserted on the SHAPE (an exact key set), not on
        the absence of the string "lanes" — a future field re-adding the same disclosure under a
        different name has to fail this too.
        """
        import json
        insp = MagicMock()
        insp.active_queues.return_value = {
            "selenium-se_engage-worker@3571c22235c8": [{"name": "se_engage"}],
        }
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            out = _call()
        assert set(out) == {"status", "workers", "consuming", "maintenance", "egress",
                            "egress_checked", "egress_failing", "linkedin_session",
                            "session_checked", "session_failing", "egress_down_since",
                            "egress_failures", "egress_failure_kind"}
        assert "3571c22235c8" not in json.dumps(out)
        assert "se_engage" not in json.dumps(out)

    def test_field_order_keeps_the_status_literal_first(self):
        """`docs/stack-watchdog.md` pins the literal `"status":"healthy"` as a monitor contract.
        FastAPI preserves dict insertion order, so `status` staying FIRST is what makes dropping a
        later key byte-identical to a monitor asserting on that substring.
        """
        import json
        insp = MagicMock()
        insp.active_queues.return_value = {"celery@worker": [{"name": "default"}]}
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            out = _call()
        assert list(out) == ["status", "workers", "consuming", "maintenance", "egress",
                             "egress_checked", "egress_failing", "linkedin_session",
                             "session_checked", "session_failing", "egress_down_since",
                             "egress_failures", "egress_failure_kind"]
        assert json.dumps(out).startswith('{"status": "healthy"')

    def test_registered_but_consuming_nothing_is_degraded(self):
        """The gap this closes, observed live during the v0.120.0 deploy: `maint begin` cancels
        every queue consumer, so the whole tier stays registered and answering while consuming
        NOTHING — and the endpoint called that `healthy`.

        Registration was never the question a monitor is asking. No consumer means no task will
        run, which is the same outage as no worker at all.
        """
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
        time, and flapping the monitor through every rollout is how an alert gets muted.
        """
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
        the one field a monitor asserts on is held steady.
        """
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
        cancelled while the flag expires, and THAT is the state worth waking someone for.
        """
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
        measure anything, and unmeasured is never `healthy` — maintenance flag or not.
        """
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=RuntimeError("down")), \
             patch("cqc_lem.utilities.maintenance.is_maintenance_mode", return_value=True), \
             patch(f"{_MAIN}.log_warning"):
            out = _call()
        assert out["status"] == "unknown"
        assert out["maintenance"] is True

    def test_unreadable_maintenance_flag_never_suppresses_a_degraded_reading(self):
        """`None` means "could not tell whether a window was declared", and a window we cannot
        confirm must not silence a real zero-consumer outage.
        """
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
        one. None means 'could not tell', never False.
        """
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
        looks exactly like a monitor that is passing.
        """
        import json
        insp = MagicMock()
        insp.active_queues.return_value = {"celery@worker": [{"name": "celery"}]}
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp):
            body = json.dumps(_call(), separators=(",", ":"))
        assert '"status":"healthy"' in body

    def test_no_consumers_is_degraded_not_healthy(self):
        """The exact v0.118.0 shape: broker up, every worker container never started. An empty
        reply must not read as healthy — that is the silence the outage hid behind.
        """
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
        a deep check there would fail deploys whenever the broker hiccuped.
        """
        import inspect

        from cqc_lem.api.main import health_check
        body = inspect.getsource(health_check)
        for forbidden in ("_inspect", "redis", "mysql", "get_db"):
            assert forbidden not in body


def _healthy_inspect():
    insp = MagicMock()
    insp.active_queues.return_value = {"celery@worker": [{"name": "default"}]}
    return patch("cqc_lem.utilities.maintenance._inspect", return_value=insp)


class TestHealthDeepEgress:
    """#2346: the proxy the browser egresses through died for nine hours under a `healthy` reading."""

    def _with(self, reading, degrade):
        return (patch("cqc_lem.utilities.egress_probe.egress_health", return_value=reading),
                patch("cqc_lem.utilities.egress_probe.should_degrade", return_value=degrade))

    def test_a_persistently_failing_egress_degrades_a_healthy_stack(self):
        reading = {"egress": "unreachable", "egress_checked": 1, "egress_failing": 1}
        h, d = self._with(reading, True)
        with _healthy_inspect(), h, d:
            out = _call()
        assert out["status"] == "degraded"
        assert out["egress"] == "unreachable" and out["egress_failing"] == 1
        assert out["consuming"] == 1  # the reason is legible: Celery is fine, egress is not

    def test_a_single_failed_probe_reports_but_does_not_degrade(self):
        reading = {"egress": "auth_failed", "egress_checked": 1, "egress_failing": 1}
        h, d = self._with(reading, False)
        with _healthy_inspect(), h, d:
            out = _call()
        assert out["status"] == "healthy"
        assert out["egress"] == "auth_failed"

    @pytest.mark.parametrize("state", ["ok", "direct", "unknown"])
    def test_non_failing_states_never_degrade(self, state):
        reading = {"egress": state, "egress_checked": 0, "egress_failing": 0}
        h, d = self._with(reading, True)
        with _healthy_inspect(), h, d:
            assert _call()["status"] == "healthy"

    def test_egress_never_upgrades_an_unknown_reading(self):
        reading = {"egress": "ok", "egress_checked": 1, "egress_failing": 0}
        h, d = self._with(reading, False)
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=RuntimeError("down")), \
             patch(f"{_MAIN}.log_warning"), h, d:
            assert _call()["status"] == "unknown"

    def test_a_crashing_probe_is_unknown_and_never_raises(self):
        with _healthy_inspect(), \
             patch("cqc_lem.utilities.egress_probe.egress_health", side_effect=RuntimeError("x")), \
             patch(f"{_MAIN}.log_warning") as warn:
            out = _call()
        assert out["status"] == "healthy"
        assert out["egress"] == "unknown"
        warn.assert_called_once()

    def test_body_names_no_proxy(self):
        """The reading is counts and a state word; a configured proxy's host never appears.

        `user:secret@proxy.example` is a fake placeholder (an RFC 2606 domain), not a credential.
        """
        import json
        with _healthy_inspect(), \
             patch("cqc_lem.utilities.egress_probe.configured_egress_proxies",
                   return_value=["http://user:secret@proxy.example:3128"]), \
             patch("cqc_lem.utilities.egress_probe.probe_proxy", return_value="ok"), \
             patch("cqc_lem.utilities.egress_probe._redis", return_value=None):
            from cqc_lem.utilities.egress_probe import reset_cache
            reset_cache()
            with patch("cqc_lem.utilities.egress_probe.egress_health", new=_REAL_EGRESS_HEALTH):
                body = json.dumps(_call())
            reset_cache()
        assert "proxy.example" not in body and "secret" not in body and "3128" not in body
        assert '"egress": "ok"' in body


class TestHealthDeepLinkedInSession:
    """#2356: a healthy egress still leaves every lane dead without a usable signed-in session."""

    _STALE = {"linkedin_session": "stale", "session_checked": 2, "session_failing": 1}

    def _with(self, reading):
        return patch("cqc_lem.utilities.linkedin.session_health.session_health",
                     return_value=reading)

    def test_flag_off_reports_a_stale_session_but_stays_healthy(self, monkeypatch):
        monkeypatch.delenv("HEALTH_DEEP_SESSION_DEGRADES", raising=False)
        with _healthy_inspect(), self._with(self._STALE):
            out = _call()
        assert out["status"] == "healthy"
        assert out["linkedin_session"] == "stale" and out["session_failing"] == 1

    def test_flag_on_degrades_a_stale_session(self, monkeypatch):
        monkeypatch.setenv("HEALTH_DEEP_SESSION_DEGRADES", "true")
        with _healthy_inspect(), self._with(self._STALE):
            out = _call()
        assert out["status"] == "degraded"
        assert out["consuming"] == 1  # legible: Celery is fine, the session is not

    @pytest.mark.parametrize("state", ["ok", "backing_off", "unknown"])
    def test_flag_on_never_degrades_a_non_failing_state(self, monkeypatch, state):
        monkeypatch.setenv("HEALTH_DEEP_SESSION_DEGRADES", "true")
        reading = {"linkedin_session": state, "session_checked": 1, "session_failing": 0}
        with _healthy_inspect(), self._with(reading):
            assert _call()["status"] == "healthy"

    def test_session_never_upgrades_an_unknown_reading(self, monkeypatch):
        monkeypatch.setenv("HEALTH_DEEP_SESSION_DEGRADES", "true")
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=RuntimeError("down")), \
             patch(f"{_MAIN}.log_warning"), self._with(self._STALE):
            assert _call()["status"] == "unknown"

    def test_a_crashing_reading_is_unknown_and_never_raises(self):
        with _healthy_inspect(), \
             patch("cqc_lem.utilities.linkedin.session_health.session_health",
                   side_effect=RuntimeError("x")), \
             patch(f"{_MAIN}.log_warning") as warn:
            out = _call()
        assert out["status"] == "healthy"
        assert out["linkedin_session"] == "unknown"
        warn.assert_called_once()

    def test_body_names_no_user_email_or_timestamp(self):
        """End to end through the real reader: only the three keys reach the public body."""
        import json

        from cqc_lem.utilities.linkedin import session_health as sh
        record = {"state": "signed_in", "signed_in_at": "2026-10-09T07:00:00+00:00",
                  "email": "owner@example.com"}
        with _healthy_inspect(), \
             patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   return_value=MagicMock()), \
             patch("cqc_lem.utilities.db.get_active_user_ids", return_value=[4242]), \
             patch("cqc_lem.utilities.linkedin.rate_limit.rate_limit_cooldown_remaining",
                   return_value=0), \
             patch("cqc_lem.utilities.linkedin.rate_limit.is_automation_paused",
                   return_value=False), \
             patch("cqc_lem.utilities.linkedin.login_status.get_login_status",
                   return_value=record), \
             patch("cqc_lem.utilities.linkedin.login_status.challenge_cooldown_remaining",
                   return_value=0), \
             patch("cqc_lem.utilities.linkedin.session_health.session_health",
                   new=_REAL_SESSION_HEALTH):
            sh.reset_cache()
            out = _call()
            sh.reset_cache()
        session_keys = {k for k in out if k.startswith("session_") or k == "linkedin_session"}
        assert session_keys == {"linkedin_session", "session_checked", "session_failing"}
        body = json.dumps(out)
        assert "4242" not in body and "example.com" not in body and "2026-10-09" not in body
        assert out["session_checked"] == 1


class TestCeleryReadIsShortAndReused:
    """2026-10-20: once the edge stopped caching it, every request waited the full 10 s inspect."""

    def _insp(self):
        insp = MagicMock()
        insp.active_queues.return_value = {"w1": [{"name": "celery"}]}
        return insp

    def test_the_deep_check_uses_its_own_short_inspect_timeout(self):
        import cqc_lem.api.main as m
        insp = self._insp()
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp) as inspect:
            assert m._health_active_queues() == {"w1": [{"name": "celery"}]}
        inspect.assert_called_once_with(timeout=m._HEALTH_INSPECT_TIMEOUT_SECONDS)
        assert m._HEALTH_INSPECT_TIMEOUT_SECONDS < 10

    def test_a_successful_read_is_reused_within_the_window(self):
        import cqc_lem.api.main as m
        insp = self._insp()
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp) as inspect:
            m._health_active_queues()
            m._health_active_queues()
        assert inspect.call_count == 1

    def test_the_read_is_repeated_after_the_window(self, monkeypatch):
        import cqc_lem.api.main as m
        insp = self._insp()
        later = 100.0 + m._HEALTH_QUEUES_CACHE_SECONDS + 1
        clock = iter([100.0, 100.0, later, later])  # each miss reads the clock twice
        monkeypatch.setattr(m.time, "monotonic", lambda: next(clock))
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp) as inspect:
            m._health_active_queues()
            m._health_active_queues()
        assert inspect.call_count == 2

    def test_a_failed_read_is_cached_for_the_window(self):
        """A broker fault costs one attempt per window; every caller in it still reads `unknown`."""
        import cqc_lem.api.main as m
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=RuntimeError("down")) as inspect:
            for _ in range(3):
                with pytest.raises(RuntimeError):
                    m._health_active_queues()
        assert inspect.call_count == 1

    def test_a_failure_does_not_outlive_its_window(self, monkeypatch):
        import cqc_lem.api.main as m
        later = 100.0 + m._HEALTH_QUEUES_CACHE_SECONDS + 1
        clock = iter([100.0, 100.0, later, later])
        monkeypatch.setattr(m.time, "monotonic", lambda: next(clock))
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=[RuntimeError("down"), self._insp()]):
            with pytest.raises(RuntimeError):
                m._health_active_queues()
            assert m._health_active_queues() == {"w1": [{"name": "celery"}]}

    def test_the_endpoint_reads_unknown_while_a_failure_is_cached(self):
        import cqc_lem.api.main as m
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=RuntimeError("down")):
            assert m.health_check_deep()["status"] == "unknown"
            assert m.health_check_deep()["status"] == "unknown"

    def test_concurrent_failures_share_one_attempt(self):
        import threading
        import time as _time

        import cqc_lem.api.main as m
        insp = self._insp()

        def failing():
            _time.sleep(0.2)
            raise RuntimeError("broker down")

        insp.active_queues.side_effect = failing
        errors = []

        def call():
            try:
                m._health_active_queues()
            except RuntimeError as e:
                errors.append(e)

        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp) as inspect:
            threads = [threading.Thread(target=call) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert inspect.call_count == 1 and len(errors) == 6

    def test_with_the_cache_off_reads_run_in_parallel(self, monkeypatch):
        """The rollback (cache 0) restores the old behaviour: no lock, one broadcast per request."""
        import threading
        import time as _time

        import cqc_lem.api.main as m
        monkeypatch.setattr(m, "_HEALTH_QUEUES_CACHE_SECONDS", 0.0)
        insp = self._insp()

        def slow():
            _time.sleep(0.2)
            return {"w1": [{"name": "celery"}]}

        insp.active_queues.side_effect = slow
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp) as inspect:
            threads = [threading.Thread(target=m._health_active_queues) for _ in range(3)]
            started = _time.monotonic()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = _time.monotonic() - started
        assert inspect.call_count == 3 and elapsed < 0.5

    def test_concurrent_misses_share_one_broadcast(self):
        """A burst against an expired cache waits on one lock and sends one broadcast."""
        import threading
        import time as _time

        import cqc_lem.api.main as m
        insp = self._insp()

        def slow_queues():
            _time.sleep(0.2)
            return {"w1": [{"name": "celery"}]}

        insp.active_queues.side_effect = slow_queues
        with patch("cqc_lem.utilities.maintenance._inspect", return_value=insp) as inspect:
            threads = [threading.Thread(target=m._health_active_queues) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert inspect.call_count == 1

    @pytest.mark.parametrize("raw,expected", [("abc", 2.0), ("2s", 2.0), ("nan", 2.0), ("inf", 2.0),
                                              ("0", 0.5), ("-3", 0.5), ("99", 10.0), ("1.5", 1.5)])
    def test_a_bad_timeout_value_falls_back_or_clamps(self, monkeypatch, raw, expected):
        import cqc_lem.api.main as m
        monkeypatch.setenv("HEALTH_DEEP_INSPECT_TIMEOUT", raw)
        assert m._health_env_seconds("HEALTH_DEEP_INSPECT_TIMEOUT", 2.0, 0.5, 10.0) == expected

    def test_a_rejected_value_warns_once_with_the_name_only(self, monkeypatch):
        import cqc_lem.api.main as m
        monkeypatch.setenv("HEALTH_DEEP_INSPECT_TIMEOUT", "abc")
        with patch.object(m, "log_warning") as warn:
            assert m._health_env_seconds("HEALTH_DEEP_INSPECT_TIMEOUT", 2.0, 0.5, 10.0) == 2.0
        warn.assert_called_once()
        assert "HEALTH_DEEP_INSPECT_TIMEOUT" in warn.call_args.args[0] and "abc" not in warn.call_args.args[0]

    def test_a_cached_failure_is_a_fresh_exception_each_time(self):
        """Re-raising one stored instance would grow its traceback on every request."""
        import traceback

        import cqc_lem.api.main as m
        caught = []
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=RuntimeError("down")):
            for _ in range(3):
                try:
                    m._health_active_queues()
                except RuntimeError as e:
                    caught.append(e)
        assert len(caught) == 3
        assert caught[1] is not caught[2] and caught[0] is not caught[1]
        assert len(traceback.extract_tb(caught[1].__traceback__)) == len(
            traceback.extract_tb(caught[2].__traceback__))

    def test_one_warning_per_failed_window(self):
        """Every request in the window reads `unknown`; only the one that broadcast warns."""
        import cqc_lem.api.main as m
        with patch("cqc_lem.utilities.maintenance._inspect", side_effect=RuntimeError("down")), \
             patch.object(m, "log_warning") as warn:
            statuses = [m.health_check_deep()["status"] for _ in range(5)]
        assert statuses == ["unknown"] * 5
        celery_warnings = [c for c in warn.call_args_list
                           if "Celery control channel" in c.args[0]]
        assert len(celery_warnings) == 1
