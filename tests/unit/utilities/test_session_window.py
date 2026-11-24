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
        d = ew.session_window_decision(1, "Update Stale Profile", _utc(2026, 11, 21, 5, 0),
                                       "America/New_York", user_started=True)
        assert d.allowed and d.reason == ew.SESSION_REASON_EXEMPT

    def test_exemption_is_the_flag_never_the_name(self):
        # The nightly sweep and the on-demand refresh share one session name.
        d = ew.session_window_decision(1, "Profile Refresh", _utc(2026, 11, 21, 5, 0), "America/New_York")
        assert not d.allowed

    def test_window_b_opens_around_a_scheduled_post(self):
        post = _utc(2026, 11, 23, 15, 0)  # 10:00 New York
        for hh, mm, ok in ((14, 44, False), (14, 45, True), (15, 59, True), (16, 0, False)):
            d = ew.session_window_decision(1, "x", _utc(2026, 11, 23, hh, mm), "America/New_York",
                                           post_times=(post,))
            assert d.allowed is ok
            assert (d.window == "B") is ok

    def test_blocks_scale_with_approved_comments(self):
        assert ew.session_blocks(0) == ew.SESSION_WINDOWS
        hundred = ew.session_blocks(100)  # ~147 min of typing: 3 blocks of ~49 -> 49 min
        assert len(hundred) == 3
        assert all(end - start == 49 for start, end in hundred.values())
        huge = ew.session_blocks(10_000)
        assert len(huge) == 4 and all(end - start == 60 for start, end in huge.values())

    def test_scaled_block_opens_a_third_window(self):
        at_four_pm = _utc(2026, 11, 23, 21, 10)  # 16:10 New York
        assert not ew.session_window_decision(1, "x", at_four_pm, "America/New_York").allowed
        assert ew.session_window_decision(1, "x", at_four_pm, "America/New_York",
                                          approved_comments=100).window == "D"

    def test_fourth_scaled_session_allowed_fifth_refused(self):
        now = _utc(2026, 11, 23, 13, 10)
        assert ew.session_window_decision(1, "x", now, "America/New_York", sessions_today=3,
                                          approved_comments=10_000).allowed
        assert not ew.session_window_decision(1, "x", now, "America/New_York", sessions_today=4,
                                              approved_comments=10_000).allowed


class TestNextWindowStart:
    def test_later_block_same_day(self):
        eta = ew.next_window_start(_utc(2026, 11, 23, 14, 0), "America/New_York")  # Mon 09:00
        assert eta == _utc(2026, 11, 23, 17, 30)  # C 12:30 EST

    def test_after_last_block_goes_to_next_weekday_a(self):
        eta = ew.next_window_start(_utc(2026, 11, 23, 22, 0), "America/New_York")
        assert eta == _utc(2026, 11, 24, 13, 0)

    def test_friday_night_skips_the_weekend(self):
        eta = ew.next_window_start(_utc(2026, 11, 28, 3, 0), "America/New_York")  # Fri 22:00
        assert eta == _utc(2026, 11, 30, 13, 0)  # Mon 08:00 EST

    def test_weekend_goes_to_monday(self):
        assert ew.next_window_start(_utc(2026, 11, 22, 15, 0), "America/New_York") == _utc(2026, 11, 23, 13, 0)

    def test_capped_day_skips_today(self):
        eta = ew.next_window_start(_utc(2026, 11, 23, 14, 0), "America/New_York", sessions_today=3)
        assert eta == _utc(2026, 11, 24, 13, 0)

    def test_dst_keeps_local_hour(self):
        eta = ew.next_window_start(_utc(2026, 7, 13, 23, 0), "America/New_York")
        assert eta == _utc(2026, 7, 14, 12, 0)  # 08:00 EDT

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

    def test_modes(self, monkeypatch):
        for raw, mode in (("enforce", "enforce"), (" SHADOW ", "shadow"), ("bogus", "off"), ("", "off")):
            monkeypatch.setenv("SESSION_WINDOW_MODE", raw)
            assert ew.session_window_mode() == mode

    def test_user_started_not_counted(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "shadow")
        with patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
             patch(f"{_MOD}.count_session") as counter:
            ew.observe_session_window(1, "Profile Refresh", now=_utc(2026, 11, 24, 3, 0),
                                      user_started=True)
        counter.assert_not_called()


class TestEnforce:
    def _patches(self, approved=(), posts=(), prior=0):
        return (patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"),
                patch("cqc_lem.utilities.db.get_approved_comment_candidates", return_value=list(approved)),
                patch("cqc_lem.utilities.db.get_scheduled_post_times", return_value=list(posts)),
                patch(f"{_MOD}.peek_session_count", return_value=prior),
                patch(f"{_MOD}.count_session"),
                patch(f"{_MOD}.track_session_window_shadow"))

    def _run(self, now, user_started=False, **kw):
        ps = self._patches(**kw)
        with ps[0], ps[1], ps[2], ps[3], ps[4] as counter, ps[5] as emit:
            try:
                return ew.observe_session_window(1, "Post Comment", now=now, user_started=user_started), \
                    counter, emit
            except ew.SessionDeferred as exc:
                return exc, counter, emit

    def test_night_defers_with_eta_and_does_not_count(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "enforce")
        exc, counter, emit = self._run(_utc(2026, 11, 24, 3, 0))  # Mon 22:00 New York
        assert isinstance(exc, ew.SessionDeferred) and exc.reason == "night"
        assert exc.eta == _utc(2026, 11, 24, 13, 0)
        counter.assert_not_called()
        emit.assert_called_once_with(1, "night", "Post Comment", "deferred")

    def test_in_window_allows_and_counts(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "enforce")
        d, counter, _ = self._run(_utc(2026, 11, 23, 13, 10))
        assert d.allowed
        counter.assert_called_once()

    def test_weekend_defers_to_monday(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "enforce")
        exc, _, _ = self._run(_utc(2026, 11, 21, 15, 0))
        assert exc.reason == "weekend" and exc.eta == _utc(2026, 11, 23, 13, 0)

    def test_over_target_defers(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "enforce")
        exc, _, _ = self._run(_utc(2026, 11, 23, 13, 10), prior=3)
        assert exc.reason == "over_daily_target"

    def test_user_started_is_exempt_at_night_and_uncounted(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "enforce")
        d, counter, _ = self._run(_utc(2026, 11, 24, 3, 0), user_started=True)
        assert d.allowed and d.reason == "user_started"
        counter.assert_not_called()

    def test_post_window_and_volume_blocks_are_read(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "enforce")
        post = _utc(2026, 11, 23, 15, 0)
        d, _, _ = self._run(_utc(2026, 11, 23, 15, 10), posts=[post])
        assert d.window == "B"
        d, _, _ = self._run(_utc(2026, 11, 23, 21, 10), approved=range(100))
        assert d.window == "D"

    def test_state_fault_fails_open(self, monkeypatch):
        monkeypatch.setenv("SESSION_WINDOW_MODE", "enforce")
        with patch("cqc_lem.utilities.db.get_user_timezone", side_effect=RuntimeError("db")):
            assert ew.observe_session_window(1, "x", now=_utc(2026, 11, 24, 3, 0)) is None

    def test_off_and_shadow_never_raise_for_the_same_night_session(self, monkeypatch):
        for mode in ("off", "shadow"):
            monkeypatch.setenv("SESSION_WINDOW_MODE", mode)
            with patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
                 patch(f"{_MOD}.count_session", return_value=1), \
                 patch(f"{_MOD}.track_session_window_shadow"):
                ew.observe_session_window(1, "x", now=_utc(2026, 11, 24, 3, 0))

    def test_deferral_is_not_an_exception_subclass(self):
        assert not issubclass(ew.SessionDeferred, Exception)

    def test_peek_session_count(self):
        client = MagicMock()
        client.get.return_value = b"2"
        with patch(f"{_MOD}.shared_redis_client", return_value=client):
            assert ew.peek_session_count(1, "2026-11-23") == 2
        with patch(f"{_MOD}.shared_redis_client", return_value=None):
            assert ew.peek_session_count(1, "2026-11-23") is None
        client.get.side_effect = RuntimeError("down")
        with patch(f"{_MOD}.shared_redis_client", return_value=client):
            assert ew.peek_session_count(1, "2026-11-23") is None


class TestTaskBaseRequeue:
    def _task(self, body):
        from celery import Celery

        from cqc_lem.app.task_outcome import DailyCapAwareTask

        app = Celery("session-window-test", task_cls=DailyCapAwareTask)
        task = app.task(body, name="session_window_test.t")
        task.apply_async = MagicMock()
        return task

    def test_deferral_requeues_with_eta_and_returns_no_op(self):
        eta = _utc(2026, 11, 24, 13, 0)

        def body(user_id):
            raise ew.SessionDeferred(user_id, "Post Comment", "night", eta)

        task = self._task(body)
        result = task(7)
        task.apply_async.assert_called_once_with(args=(7,), kwargs={}, eta=eta)
        assert result.startswith("Deferred to the next session window")

    def test_failed_requeue_raises_the_deferral(self):
        def body():
            raise ew.SessionDeferred(1, "x", "night", _utc(2026, 11, 24, 13, 0))

        task = self._task(body)
        task.apply_async.side_effect = RuntimeError("broker down")
        with pytest.raises(ew.SessionDeferred):
            task()


class TestContentFallbacks:
    def test_resolve_user_profile_uses_cache_when_deferred(self):
        from cqc_lem.app import run_content_plan as rcp
        deferred = ew.SessionDeferred(1, "Create Text Post", "night", _utc(2026, 11, 24, 13, 0))
        cached = MagicMock()
        with patch.object(rcp, "browser_automation_allowed", return_value=True), \
             patch.object(rcp, "get_user_password_pair_by_id", return_value=("e", "p")), \
             patch.object(rcp, "get_driver_wait_pair", side_effect=deferred), \
             patch.object(rcp, "load_profile_for_user", return_value=cached):
            assert rcp._resolve_user_profile(1) is cached


class TestSeamCoverage:
    def test_get_docker_driver_calls_the_hook(self):
        from cqc_lem.utilities import selenium_util
        assert "observe_session_window(user_id, session_name, user_started=user_started)" in inspect.getsource(
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

    def test_only_the_on_demand_refresh_is_user_started(self):
        from cqc_lem.app.engagement import posting
        from cqc_lem.utilities.linkedin import session
        assert "user_started=force_refresh" in inspect.getsource(posting.update_stale_profile.run)
        assert "user_started=user_started" in inspect.getsource(session.get_current_profile)

    def test_every_se_task_is_routed(self):
        from cqc_lem.app import celeryconfig
        se_tasks = [t for t, r in celeryconfig.task_routes.items() if r["queue"].startswith("se_")]
        assert se_tasks, "no Selenium tasks found to cover"
