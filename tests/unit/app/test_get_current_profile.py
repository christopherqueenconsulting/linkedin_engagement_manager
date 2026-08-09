"""Unit tests for graceful profile fallback in `linkedin.session.get_current_profile`.

The function moved down out of `run_automation` in #1154 and took its imports with it, so this
is the module whose bindings it reads — patching them on `run_automation` would rebind names
nothing looks at.
"""

from unittest.mock import MagicMock, patch

import pytest

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
