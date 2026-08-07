"""Unit tests for per-user geo/timezone/locale spoofing in get_docker_driver."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.selenium_util"


def _run(user_id=None, geo=None, headless=False, proxy=None):
    fake_driver = MagicMock()
    cdp_calls = {}

    def _cdp(cmd, params):
        cdp_calls[cmd] = params

    fake_driver.execute_cdp_cmd.side_effect = _cdp
    with patch(f"{_MOD}._wait_for_selenium_ready"), \
         patch(f"{_MOD}.DEVICE_FARM_PROJECT_ARN", None), \
         patch(f"{_MOD}.TEST_GRID_PROJECT_ARN", None), \
         patch(f"{_MOD}.webdriver.Remote", return_value=fake_driver), \
         patch("cqc_lem.utilities.db.get_user_geo", return_value=geo), \
         patch("cqc_lem.utilities.db.get_user_proxy", return_value=proxy):
        from cqc_lem.utilities.selenium_util import get_docker_driver
        get_docker_driver(headless=headless, user_id=user_id)
    return cdp_calls


class TestPerUserGeo:
    def test_applies_user_geo_timezone_locale(self):
        geo = {"latitude": 51.5, "longitude": -0.12, "timezone": "Europe/London",
               "locale": "en-GB", "city": "London", "country": "GB"}
        calls = _run(user_id=5, geo=geo)
        assert calls["Emulation.setGeolocationOverride"]["latitude"] == pytest.approx(51.5)
        assert calls["Emulation.setGeolocationOverride"]["longitude"] == pytest.approx(-0.12)
        assert calls["Emulation.setTimezoneOverride"]["timezoneId"] == "Europe/London"
        assert calls["Emulation.setLocaleOverride"]["locale"] == "en-GB"

    def test_falls_back_to_default_when_no_user(self):
        calls = _run(user_id=None, geo=None)
        # Default Jacksonville coordinates when no per-user geo is available
        assert calls["Emulation.setGeolocationOverride"]["latitude"] == pytest.approx(30.3321)
        assert calls["Emulation.setLocaleOverride"]["locale"] == "en-US"

    def test_user_without_stored_geo_uses_defaults(self):
        calls = _run(user_id=7, geo=None)
        assert calls["Emulation.setGeolocationOverride"]["latitude"] == pytest.approx(30.3321)
