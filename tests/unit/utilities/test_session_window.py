"""Session window gate, Phase 1 (issue #2628): pure decision, shadow hook, seam coverage."""

import inspect
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import engagement_window as ew

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.engagement_window"


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class TestDecision:
    # 2026-11-23 is a Monday. New York is UTC-5 then.
    def test_window_a_allowed(self):
        d = ew.session_window_decision(1, "Post Comment", _utc(2026, 11, 23, 13, 10), "America/New_York")
        assert d.allowed and d.window == "A"

    def test_window_c_allowed(self):
        d = ew.session_window_decision(1, "Post Comment", _utc(2026, 11, 23, 17, 40), "America/New_York")
        assert d.allowed and d.window == "C"

    def test_night_refused(self):
        d = ew.session_window_decision(1, "Post Comment", _utc(2026, 11, 24, 3, 0), "America/New_York")
        assert not d.allowed and d.reason == ew.SESSION_REASON_NIGHT

    def test_ban_edges(self):
        zone = "America/New_York"
        assert ew.session_window_decision(1, "x", _utc(2026, 11, 24, 0, 0), zone).reason == "night"  # 19:00
        assert ew.session_window_decision(1, "x", _utc(2026, 11, 23, 11, 59), zone).reason == "night"  # 06:59

    def test_weekend_refused(self):
        d = ew.session_window_decision(1, "x", _utc(2026, 11, 21, 13, 10), "America/New_York")  # Sat 08:10
        assert not d.allowed and d.reason == ew.SESSION_REASON_WEEKEND

    def test_midday_outside_window(self):
        d = ew.session_window_decision(1, "x", _utc(2026, 11, 23, 16, 0), "America/New_York")
        assert not d.allowed and d.reason == ew.SESSION_REASON_OUTSIDE_WINDOW

    def test_fourth_session_refused(self):
        now = _utc(2026, 11, 23, 13, 10)
        assert ew.session_window_decision(1, "x", now, "America/New_York", sessions_today=2).allowed
        d = ew.session_window_decision(1, "x", now, "America/New_York", sessions_today=3)
        assert not d.allowed and d.reason == ew.SESSION_REASON_OVER_TARGET

    def test_user_started_exempt_even_at_night(self):
        d = ew.session_window_decision(1, "Profile Refresh", _utc(2026, 11, 21, 5, 0), "America/New_York")
        assert d.allowed and d.reason == ew.SESSION_REASON_EXEMPT

    @pytest.mark.parametrize("tz,hour_utc,allowed", [
        ("America/Los_Angeles", 16, True),   # 08:00 PST
        ("Europe/London", 8, True),          # 08:00 GMT
        ("Asia/Tokyo", 23, True),            # 08:00 JST (Mon 23 Nov 23:00Z is Tue 08:00)
        ("America/Los_Angeles", 13, False),  # 05:00 PST
    ])
    def test_timezones(self, tz, hour_utc, allowed):
        assert ew.session_window_decision(1, "x", _utc(2026, 11, 23, hour_utc, 5), tz).allowed is allowed

    def test_dst_holds_local_hour(self):
        # 08:10 New York is 12:10Z in summer (EDT) and 13:10Z in winter (EST).
        assert ew.session_window_decision(1, "x", _utc(2026, 7, 13, 12, 10), "America/New_York").window == "A"
        assert ew.session_window_decision(1, "x", _utc(2026, 7, 13, 13, 10), "America/New_York").window is None
        assert ew.session_window_decision(1, "x", _utc(2026, 12, 14, 13, 10), "America/New_York").window == "A"

    @pytest.mark.parametrize("bad", [None, "", "Not/AZone"])
    def test_bad_timezone_falls_back_to_new_york_not_utc(self, bad):
        # 13:10Z is 08:10 in New York (window A) but 13:10 in UTC (outside every window).
        d = ew.session_window_decision(1, "x", _utc(2026, 11, 23, 13, 10), bad)
        assert d.allowed and d.window == "A"

    def test_bad_timezone_warns_once(self):
        ew._TZ_WARNED.clear()
        with patch(f"{_MOD}.log_warning") as warn:
            ew.resolve_session_zone("Bad/Zone")
            ew.resolve_session_zone("Bad/Zone")
        assert warn.call_count == 1


class TestCounter:
    def test_no_redis_fails_open(self):
        with patch(f"{_MOD}.shared_redis_client", return_value=None):
            assert ew.count_session(1, "2026-11-23") is None

    def test_redis_error_fails_open(self):
        client = MagicMock()
        client.incr.side_effect = RuntimeError("down")
        with patch(f"{_MOD}.shared_redis_client", return_value=client):
            assert ew.count_session(1, "2026-11-23") is None

    def test_first_count_sets_ttl(self):
        client = MagicMock()
        client.incr.return_value = 1
        with patch(f"{_MOD}.shared_redis_client", return_value=client):
            assert ew.count_session(1, "2026-11-23") == 1
        client.expire.assert_called_once()


class TestShadowHook:
    def test_off_touches_nothing(self, monkeypatch):
        monkeypatch.delenv("SESSION_WINDOW_MODE", raising=False)
        with patch(f"{_MOD}.count_session") as counter:
            assert ew.observe_session_window(1, "Post Comment") is None
        counter.assert_not_called()

    def test_shadow_would_defer_logs_and_emits_without_refusing(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "shadow")
        with patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
             patch(f"{_MOD}.count_session", return_value=1), \
             patch(f"{_MOD}.track_session_window_shadow") as emit, \
             patch(f"{_MOD}.log_info") as info, patch(f"{_MOD}.log_warning") as warn:
            d = ew.observe_session_window(1, "Post Comment", now=_utc(2026, 11, 24, 3, 0))
        assert d is not None and not d.allowed
        emit.assert_called_once_with(1, "night", "Post Comment", "none")
        info.assert_called_once()
        warn.assert_not_called()

    def test_shadow_in_window_emits_nothing(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "shadow")
        with patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
             patch(f"{_MOD}.count_session", return_value=1), \
             patch(f"{_MOD}.track_session_window_shadow") as emit:
            d = ew.observe_session_window(1, "Post Comment", now=_utc(2026, 11, 23, 13, 10))
        assert d.allowed
        emit.assert_not_called()

    def test_hook_error_never_raises(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "shadow")
        with patch("cqc_lem.utilities.db.get_user_timezone", side_effect=RuntimeError("db")):
            assert ew.observe_session_window(1, "x") is None

    def test_enforce_is_shadow_until_built(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "enforce")
        assert ew.session_window_mode() == "shadow"

    def test_user_started_not_counted(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "shadow")
        with patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
             patch(f"{_MOD}.count_session") as counter:
            ew.observe_session_window(1, "Profile Refresh", now=_utc(2026, 11, 24, 3, 0))
        counter.assert_not_called()


class TestSeamCoverage:
    def test_get_docker_driver_calls_the_hook(self):
        from cqc_lem.utilities import selenium_util
        assert "observe_session_window(user_id, session_name)" in inspect.getsource(
            selenium_util.get_docker_driver)

    def test_every_lane_reaches_the_seam(self):
        """Every lane opens its driver through the seam; no task module builds Chrome itself."""
        import pathlib

        from cqc_lem.utilities import selenium_util
        assert "get_docker_driver(" in inspect.getsource(selenium_util.get_driver_wait_pair)
        root = pathlib.Path(selenium_util.__file__).parents[1]
        offenders = [str(p.relative_to(root)) for p in (root / "app").rglob("*.py")
                     if "webdriver.Chrome(" in p.read_text() or "webdriver.Remote(" in p.read_text()]
        assert offenders == []

    def test_every_se_task_is_routed(self):
        from cqc_lem.app import celeryconfig
        se_tasks = [t for t, r in celeryconfig.task_routes.items() if r["queue"].startswith("se_")]
        assert se_tasks, "no Selenium tasks found to cover"
