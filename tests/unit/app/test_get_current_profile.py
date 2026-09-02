"""Unit tests for graceful profile fallback in `linkedin.session.get_current_profile`.

The function moved down out of `run_automation` in #1154 and took its imports with it, so this
is the module whose bindings it reads. #1206 then deleted `run_automation` outright, so the stale
patch target that used to rebind names nothing looks at now raises instead.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import WebDriverException

pytestmark = pytest.mark.unit

_SESSION = "cqc_lem.utilities.linkedin.session"


def _patches():
    return {
        "get_user_password_pair_by_id": patch(f"{_SESSION}.get_user_password_pair_by_id",
                                              return_value=("e@x.com", "pw")),
        "get_driver_wait_pair": patch(f"{_SESSION}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())),
        "login_to_linkedin": patch(f"{_SESSION}.login_to_linkedin"),
        "quit_gracefully": patch(f"{_SESSION}.quit_gracefully"),
    }


class TestGetCurrentProfile:
    def test_falls_back_to_cached_profile_when_live_scrape_fails(self):
        cached = MagicMock(name="CachedProfile")
        p = _patches()
        with p["get_user_password_pair_by_id"], p["get_driver_wait_pair"], \
             p["login_to_linkedin"], p["quit_gracefully"], \
             patch(f"{_SESSION}.get_my_profile", side_effect=RuntimeError("auth-wall")), \
             patch(f"{_SESSION}.load_profile_for_user", return_value=cached) as mock_cache:
            from cqc_lem.utilities.linkedin.session import get_current_profile
            driver, wait, email, profile = get_current_profile(user_id=1)
        mock_cache.assert_called_once_with(1)
        assert profile is cached

    def test_raises_when_login_fails(self):
        # Login failure (e.g. 429) is fatal — must propagate so the caller backs off.
        p = _patches()
        with p["get_user_password_pair_by_id"], p["get_driver_wait_pair"], \
             p["quit_gracefully"], \
             patch(f"{_SESSION}.login_to_linkedin", side_effect=RuntimeError("HTTP 429 rate-limited")):
            from cqc_lem.utilities.linkedin.session import get_current_profile
            with pytest.raises(RuntimeError, match="429"):
                get_current_profile(user_id=1)

    def test_raises_when_no_profile_anywhere(self):
        p = _patches()
        with p["get_user_password_pair_by_id"], p["get_driver_wait_pair"], \
             p["login_to_linkedin"], p["quit_gracefully"], \
             patch(f"{_SESSION}.get_my_profile", side_effect=RuntimeError("scrape failed")), \
             patch(f"{_SESSION}.load_profile_for_user", return_value=None):
            from cqc_lem.utilities.linkedin.session import get_current_profile
            with pytest.raises(RuntimeError, match="Profile unavailable"):
                get_current_profile(user_id=1)

    def test_tab_crashed_on_login_logs_warning_not_error(self):
        # Issue #1749: a WebDriverException "tab crashed" during the very first login navigation
        # is a dead browser tab (usually a Grid slot reused from a previous OOM-killed session),
        # never a login/rate-limit fault — it must still propagate (the run is over either way) but
        # must not file a grouped PostHog defect for a known-transient Selenium fault.
        p = _patches()
        crash = WebDriverException("Message: tab crashed\n  (Session info: chrome=151.0.7922.108)")
        with p["get_user_password_pair_by_id"], p["get_driver_wait_pair"], p["quit_gracefully"], \
             patch(f"{_SESSION}.login_to_linkedin", side_effect=crash), \
             patch(f"{_SESSION}.log_warning") as warn, \
             patch(f"{_SESSION}.log_error") as err:
            from cqc_lem.utilities.linkedin.session import get_current_profile
            with pytest.raises(WebDriverException):
                get_current_profile(user_id=1)
        warn.assert_called_once()
        assert warn.call_args.kwargs.get("exc") is crash
        err.assert_not_called()

    def test_a_rate_limited_login_logs_debug_not_error(self):
        # A LinkedInRateLimited abort (breaker open, pause, real 429, unsolvable checkpoint) is a
        # deliberate back-off already logged where it was detected, so an ERROR here would only fork
        # a second grouped $exception for the same event. It still propagates so the caller defers.
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        p = _patches()
        with p["get_user_password_pair_by_id"], p["get_driver_wait_pair"], p["quit_gracefully"], \
             patch(f"{_SESSION}.login_to_linkedin", side_effect=LinkedInRateLimited("breaker open")), \
             patch(f"{_SESSION}.log_debug") as dbg, \
             patch(f"{_SESSION}.log_error") as err:
            from cqc_lem.utilities.linkedin.session import get_current_profile
            with pytest.raises(LinkedInRateLimited):
                get_current_profile(user_id=1)
        dbg.assert_called_once()
        err.assert_not_called()

    def test_other_login_failures_still_log_error(self):
        p = _patches()
        with p["get_user_password_pair_by_id"], p["get_driver_wait_pair"], p["quit_gracefully"], \
             patch(f"{_SESSION}.login_to_linkedin", side_effect=RuntimeError("HTTP 429 rate-limited")), \
             patch(f"{_SESSION}.log_warning") as warn, \
             patch(f"{_SESSION}.log_error") as err:
            from cqc_lem.utilities.linkedin.session import get_current_profile
            with pytest.raises(RuntimeError):
                get_current_profile(user_id=1)
        err.assert_called_once()
        warn.assert_not_called()

    def test_returns_live_profile_on_success(self):
        live = MagicMock(name="LiveProfile")
        p = _patches()
        with p["get_user_password_pair_by_id"], p["get_driver_wait_pair"], \
             p["login_to_linkedin"], p["quit_gracefully"], \
             patch(f"{_SESSION}.get_my_profile", return_value=live), \
             patch(f"{_SESSION}.load_profile_for_user") as mock_cache:
            from cqc_lem.utilities.linkedin.session import get_current_profile
            _, _, _, profile = get_current_profile(user_id=1)
        assert profile is live
        mock_cache.assert_not_called()
