"""E2E tests for CAPTCHA-solving integration via CapSolver.

These tests run only when CAPSOLVER_API_KEY is set to a real key.
They are auto-skipped (via pytest_collection_modifyitems in conftest.py) when
the key is absent or a placeholder.

To run locally:
    CAPSOLVER_API_KEY=CAP-xxxx \
        poetry run pytest tests/e2e/test_capsolver_e2e.py -v -m e2e
"""

import os
from unittest.mock import MagicMock, patch

import pytest

_CAPSOLVER_BALANCE_URL = "https://api.capsolver.com/getBalance"


# ---------------------------------------------------------------------------
# Helper — build a minimal mock driver without a real browser
# ---------------------------------------------------------------------------

def _mock_driver_no_iframes() -> MagicMock:
    """Minimal WebDriver mock: no iframes, blank page."""
    driver = MagicMock()
    driver.current_url = "https://www.linkedin.com/checkpoint/challenge"
    driver.find_elements.return_value = []
    driver.page_source = "<html><body></body></html>"
    return driver


def _mock_driver_with_arkose_iframe(pk: str = "TEST-PUBLIC-KEY-ABC") -> MagicMock:
    """Mock driver that contains one Arkose Labs iframe in the DOM."""
    src = f"https://client-api.arkoselabs.com/fc/assets/?pk={pk}&surl=https://client-api.arkoselabs.com/"
    iframe = MagicMock()
    iframe.get_attribute.return_value = src

    driver = MagicMock()
    driver.current_url = "https://www.linkedin.com/checkpoint/challenge"
    driver.find_elements.return_value = [iframe]
    driver.page_source = f'<html><iframe src="{src}"></iframe></html>'
    return driver


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.requires_capsolver
class TestCapSolverApiKey:
    """Validate that the configured CAPSOLVER_API_KEY is real and reachable."""

    def test_api_key_has_expected_format(self):
        """Sanity-check: real CapSolver keys start with 'CAP-'."""
        key = os.environ.get("CAPSOLVER_API_KEY", "")
        assert key.startswith("CAP-"), (
            f"CAPSOLVER_API_KEY '{key}' does not look like a real CapSolver key "
            "(expected 'CAP-...'). Check capsolver.com for your API key."
        )

    def test_balance_endpoint_is_reachable(self):
        """Call the CapSolver /getBalance endpoint and verify account is accessible."""
        import requests

        key = os.environ["CAPSOLVER_API_KEY"]
        resp = requests.post(
            _CAPSOLVER_BALANCE_URL,
            json={"clientKey": key},
            timeout=10,
        )
        assert resp.status_code == 200, (
            f"CapSolver /getBalance returned HTTP {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("errorId") == 0, (
            f"CapSolver returned an error: {body.get('errorDescription', body)}"
        )
        balance = body.get("balance", -1)
        assert isinstance(balance, (int, float)), f"Unexpected balance value: {balance}"
        # We don't assert balance > 0 because a $0 balance still means the key is valid.
        assert balance >= 0, f"Negative balance returned: {balance}"


@pytest.mark.e2e
@pytest.mark.requires_capsolver
class TestSolveArkoseChallengeE2E:
    """Integration-style tests for solve_arkose_challenge() with a real API key."""

    def test_returns_false_gracefully_when_no_arkose_iframe(self):
        """Key is set but page has no Arkose iframe → False without error."""
        driver = _mock_driver_no_iframes()
        wait = MagicMock()

        from cqc_lem.utilities.linkedin.helper import solve_arkose_challenge
        result = solve_arkose_challenge(driver, wait)

        assert result is False, (
            "Expected False when no Arkose iframe is present, got True"
        )

    def test_attempts_capsolver_api_when_arkose_iframe_detected(self):
        """Arkose iframe present → capsolver.solve() is invoked with FunCaptchaTask."""
        driver = _mock_driver_with_arkose_iframe(pk="LI-REAL-PUBLIC-KEY")
        wait = MagicMock()

        mock_capsolver = MagicMock()
        # Simulate a failed solve (empty token) so we don't need a real CAPTCHA page,
        # but we still verify the API call was attempted with the right task type.
        mock_capsolver.solve.return_value = {"token": ""}

        with patch.dict("sys.modules", {"capsolver": mock_capsolver}):
            from importlib import reload

            import cqc_lem.utilities.linkedin.helper as helper_mod
            reload(helper_mod)
            result = helper_mod.solve_arkose_challenge(driver, wait)

        assert result is False  # Empty token → False
        mock_capsolver.solve.assert_called_once()
        call_kwargs = mock_capsolver.solve.call_args[0][0]
        assert call_kwargs["type"] == "FunCaptchaTask"
        assert call_kwargs["websitePublicKey"] == "LI-REAL-PUBLIC-KEY"

    @pytest.mark.requires_selenium
    def test_returns_false_on_real_browser_blank_page(self):
        """Real Selenium driver (Docker standalone-chrome) → False on a blank page."""
        import os

        selenium_host = os.environ.get("SELENIUM_HUB_HOST", "localhost")
        selenium_port = os.environ.get("SELENIUM_HUB_PORT", "4444")

        try:
            from selenium import webdriver
            from selenium.webdriver.support.wait import WebDriverWait

            options = webdriver.ChromeOptions()
            options.add_argument("--headless")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")

            driver = webdriver.Remote(
                command_executor=f"http://{selenium_host}:{selenium_port}/wd/hub",
                options=options,
            )
        except Exception as exc:
            pytest.skip(f"Selenium container not reachable at {selenium_host}:{selenium_port} — {exc}")
            return

        try:
            driver.get("about:blank")
            wait = WebDriverWait(driver, timeout=5)

            from cqc_lem.utilities.linkedin.helper import solve_arkose_challenge
            result = solve_arkose_challenge(driver, wait)

            assert result is False, (
                "Expected False on a blank page (no Arkose iframe), got True"
            )
        finally:
            try:
                driver.quit()
            except Exception:
                pass  # Best-effort cleanup; driver may already be closed
