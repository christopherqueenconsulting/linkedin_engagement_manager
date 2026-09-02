"""Unit tests for cqc_lem.utilities.linkedin.helper — login_to_linkedin."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MODULE = "cqc_lem.utilities.linkedin.helper"


@pytest.fixture(autouse=True)
def _no_real_sleep():
    """Login flow uses real time.sleep() between mocked Selenium steps — those
    seconds are dead wait in unit tests (drivers are mocked, not timing-dependent).
    """
    with patch(f"{_MODULE}.time.sleep"):
        yield


@pytest.fixture(autouse=True)
def _no_approval_wait(monkeypatch):
    """Disable the device-approval wait loop by default — it polls real wall-clock
    for minutes. Tests that exercise the approval path opt back in explicitly.
    """
    monkeypatch.setenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "0")


@pytest.fixture(autouse=True)
def _breaker_closed():
    """Keep the 429 circuit breaker closed and hermetic by default so login tests
    never touch Redis. Tests exercising the breaker patch these explicitly.
    """
    with patch(f"{_MODULE}.rate_limit_cooldown_remaining", return_value=0), \
         patch(f"{_MODULE}.mark_rate_limited"), \
         patch(f"{_MODULE}.is_automation_paused", return_value=False), \
         patch(f"{_MODULE}.clear_rate_limit"):
        yield


@pytest.fixture(autouse=True)
def _stub_approval_email():
    """Stub the high-priority approval email (lazily imported inside the login flow)
    so tests never touch a real provider; tests can assert on the returned mock.
    """
    with patch("cqc_lem.utilities.email.send_login_approval_email", return_value=True) as m:
        yield m


def _make_driver(url: str) -> MagicMock:
    driver = MagicMock()
    driver.current_url = url
    driver.get_cookies.return_value = [{"name": "li_at", "value": "tok"}]
    return driver


def _make_wait() -> MagicMock:
    wait = MagicMock()
    wait.until = MagicMock()
    return wait


@pytest.mark.unit
class TestLoginToLinkedinCookiePath:
    """Tests for the cookie-based fast-path (already logged in)."""

    def _run(self, url_after_load: str, cookies=None):
        driver = _make_driver(url_after_load)
        wait = _make_wait()

        with patch(f"{_MODULE}.get_cookies", return_value=cookies or [{"name": "x"}]), \
             patch(f"{_MODULE}.load_cookies") as mock_load, \
             patch(f"{_MODULE}.store_cookies") as mock_store:
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "test@example.com", "password")

        return driver, mock_load, mock_store

    def test_already_logged_in_feed_url(self):
        """Navigating to /feed/ with valid cookies → detected as logged in."""
        driver, _, mock_store = self._run("https://www.linkedin.com/feed/")
        # Cookies are refreshed in the DB
        mock_store.assert_called_once_with("test@example.com", driver.get_cookies())

    def test_already_logged_in_home_url(self):
        """LinkedIn /home redirect (new behaviour) → still detected as logged in."""
        driver, _, mock_store = self._run("https://www.linkedin.com/home")
        mock_store.assert_called_once()

    def test_already_logged_in_mynetwork_url(self):
        """LinkedIn /mynetwork → logged in."""
        driver, _, mock_store = self._run("https://www.linkedin.com/mynetwork/")
        mock_store.assert_called_once()

    def test_already_logged_in_jobs_url(self):
        driver, _, mock_store = self._run("https://www.linkedin.com/jobs/")
        mock_store.assert_called_once()

    def test_navigates_to_feed_after_loading_cookies(self):
        """After loading cookies, must navigate to /feed/ (not bare homepage)."""
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()
        get_calls = []
        driver.get.side_effect = lambda url: get_calls.append(url)

        with patch(f"{_MODULE}.get_cookies", return_value=[{"name": "x"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "user@e.com", "pw")

        assert any("feed" in u for u in get_calls), (
            f"Expected a navigation to /feed/ after loading cookies, got: {get_calls}"
        )

    def test_challenge_url_after_cookies_raises(self):
        """Security challenge after cookie load → RuntimeError (not silent fail)."""
        driver = _make_driver("https://www.linkedin.com/checkpoint/challenge")
        wait = _make_wait()

        with patch(f"{_MODULE}.get_cookies", return_value=[{"name": "x"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies"), \
             pytest.raises(RuntimeError, match="Unsolvable LinkedIn challenge"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "user@e.com", "pw")

    def test_no_cookies_goes_to_login_flow(self):
        """No stored cookies → must do credential login (navigate to /login)."""
        # After /login navigate, URL becomes the login page; after submit, /feed/
        call_count = 0
        get_urls = []

        def fake_get(url):
            get_urls.append(url)

        # Simulate: base page → challenge-free login page → feed after submit
        driver = MagicMock()
        driver.get_cookies.return_value = [{"name": "li_at"}]

        # current_url changes with each driver.get() call
        url_sequence = [
            "https://www.linkedin.com",   # initial get
            "https://www.linkedin.com/login",  # after driver.get(login_url)
            "https://www.linkedin.com/feed/",  # after submit
        ]
        get_index = [0]

        def get_url():
            return url_sequence[min(get_index[0], len(url_sequence) - 1)]

        type(driver).current_url = property(lambda self: get_url())
        driver.get.side_effect = lambda u: get_index.__setitem__(0, get_index[0] + 1)

        wait = _make_wait()
        mock_field = MagicMock()

        with patch(f"{_MODULE}.get_cookies", return_value=None), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies") as mock_store, \
             patch(f"{_MODULE}.get_visible_element_wait_retry", return_value=mock_field):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "user@e.com", "pw")

        # Must have navigated to the login page directly
        calls = [c[0][0] for c in driver.get.call_args_list]
        assert any("login" in u for u in calls), f"Expected /login navigation, got: {calls}"
        # Cookies stored after successful login
        mock_store.assert_called_once()

    def test_stale_cookie_triggers_revalidation_email(self):
        """Cookies present but they don't authenticate → auto-detect stale session and
        email the user to reconnect (revalidation=True).
        """
        state = {"url": "https://www.linkedin.com"}
        driver = MagicMock()
        driver.get_cookies.return_value = [{"name": "li_at"}]
        driver.find_elements.return_value = []
        type(driver).current_url = property(lambda self: state["url"])

        def on_get(u):
            if "feed" in u:
                state["url"] = "https://www.linkedin.com/login"   # stale cookie → bounced
            elif "login" in u:
                state["url"] = "https://www.linkedin.com/feed/"   # credential login succeeds
            else:
                state["url"] = "https://www.linkedin.com"
        driver.get.side_effect = on_get

        wait = _make_wait()
        with patch(f"{_MODULE}.get_cookies", return_value=[{"name": "li_at"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies"), \
             patch(f"{_MODULE}.get_visible_element_wait_retry", return_value=MagicMock()), \
             patch("cqc_lem.utilities.db.get_user_id", return_value=7), \
             patch("cqc_lem.utilities.notifications.notify_linkedin_session") as notify:
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "user@e.com", "pw")

        notify.assert_called_once()
        assert notify.call_args.args[0] == 7
        assert notify.call_args.kwargs.get("revalidation") is True

    def test_login_form_timeout_logs_url_and_reraises(self):
        """Login form never renders (an unrecognized challenge/checkpoint page, #1908).

        The TimeoutException must still propagate (behavior unchanged), but the current URL
        and a page-body snippet are logged first so the next occurrence is diagnosable —
        the original failure left nothing but a bare "Finding Username Field" timeout.
        """
        from selenium.common.exceptions import TimeoutException

        driver = _make_driver("https://www.linkedin.com/login")
        driver.find_element.return_value.text = "Let's do a quick security check"
        wait = _make_wait()

        with patch(f"{_MODULE}.get_cookies", return_value=None), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies"), \
             patch(f"{_MODULE}.get_visible_element_wait_retry",
                   side_effect=TimeoutException("Finding Username Field")), \
             patch(f"{_MODULE}.log_warning") as mock_warn, \
             pytest.raises(TimeoutException):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "user@e.com", "pw")

        mock_warn.assert_called_once()
        args, kwargs = mock_warn.call_args
        assert "unrecognized challenge" in args[0].lower()
        assert kwargs.get("url") == "https://www.linkedin.com/login"
        assert "security check" in kwargs.get("page_snippet", "")


@pytest.mark.unit
class TestSolveArkoseChallenge:
    """Unit tests for solve_arkose_challenge() in linkedin/helper.py."""

    def _make_driver(self, iframes=None):
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/checkpoint/challenge"
        driver.find_elements.return_value = iframes or []
        driver.page_source = "<html></html>"
        return driver

    def _make_arkose_iframe(self, src="https://client-api.arkoselabs.com/fc/assets/"):
        frame = MagicMock()
        frame.get_attribute.return_value = src
        return frame

    def test_returns_false_when_api_key_not_set(self, monkeypatch):
        """No CAPSOLVER_API_KEY → False immediately (no API call attempted)."""
        monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
        driver = self._make_driver()
        wait = _make_wait()

        from cqc_lem.utilities.linkedin.helper import solve_arkose_challenge
        result = solve_arkose_challenge(driver, wait)

        assert result is False
        driver.find_elements.assert_not_called()

    def test_returns_false_when_api_key_is_empty(self, monkeypatch):
        monkeypatch.setenv("CAPSOLVER_API_KEY", "")
        driver = self._make_driver()
        wait = _make_wait()

        from cqc_lem.utilities.linkedin.helper import solve_arkose_challenge
        assert solve_arkose_challenge(driver, wait) is False

    def test_returns_false_when_no_arkose_iframe(self, monkeypatch):
        """Page has no Arkose Labs iframe → False, no capsolver call."""
        monkeypatch.setenv("CAPSOLVER_API_KEY", "CAP-test-key-abc123")
        driver = self._make_driver(iframes=[])
        wait = _make_wait()

        with patch(f"{_MODULE}.capsolver", create=True):
            from cqc_lem.utilities.linkedin.helper import solve_arkose_challenge
            result = solve_arkose_challenge(driver, wait)

        assert result is False

    def test_returns_false_when_iframe_has_no_arkoselabs_src(self, monkeypatch):
        """Iframe present but not from arkoselabs.com → not an Arkose challenge."""
        monkeypatch.setenv("CAPSOLVER_API_KEY", "CAP-test-key-abc123")
        non_arkose = MagicMock()
        non_arkose.get_attribute.return_value = "https://other.com/captcha"
        driver = self._make_driver(iframes=[non_arkose])
        wait = _make_wait()

        from cqc_lem.utilities.linkedin.helper import solve_arkose_challenge
        assert solve_arkose_challenge(driver, wait) is False

    def test_returns_false_when_capsolver_raises(self, monkeypatch):
        """capsolver.solve() raises an exception → False (logged warning, no crash)."""
        monkeypatch.setenv("CAPSOLVER_API_KEY", "CAP-test-key-abc123")
        arkose = self._make_arkose_iframe(
            "https://client-api.arkoselabs.com/fc/?pk=ABCD-1234&surl=https://client-api.arkoselabs.com/"
        )
        driver = self._make_driver(iframes=[arkose])
        wait = _make_wait()

        mock_capsolver = MagicMock()
        mock_capsolver.solve.side_effect = RuntimeError("API unreachable")

        with patch.dict("sys.modules", {"capsolver": mock_capsolver}):
            from importlib import reload

            import cqc_lem.utilities.linkedin.helper as helper_mod
            reload(helper_mod)
            result = helper_mod.solve_arkose_challenge(driver, wait)

        assert result is False

    def test_returns_false_when_capsolver_returns_empty_token(self, monkeypatch):
        """capsolver.solve() returns a dict with no token → False."""
        monkeypatch.setenv("CAPSOLVER_API_KEY", "CAP-test-key-abc123")
        arkose = self._make_arkose_iframe(
            "https://client-api.arkoselabs.com/fc/?pk=ABCD-1234&surl=https://client-api.arkoselabs.com/"
        )
        driver = self._make_driver(iframes=[arkose])
        wait = _make_wait()

        mock_capsolver = MagicMock()
        mock_capsolver.solve.return_value = {"token": ""}

        with patch.dict("sys.modules", {"capsolver": mock_capsolver}):
            from importlib import reload

            import cqc_lem.utilities.linkedin.helper as helper_mod
            reload(helper_mod)
            result = helper_mod.solve_arkose_challenge(driver, wait)

        assert result is False

    def test_returns_true_and_injects_token_when_solved(self, monkeypatch):
        """Valid Arkose iframe + capsolver returns token → True, token injected via JS."""
        monkeypatch.setenv("CAPSOLVER_API_KEY", "CAP-test-key-abc123")
        arkose = self._make_arkose_iframe(
            "https://client-api.arkoselabs.com/fc/?pk=LI-TEST-KEY&surl=https://client-api.arkoselabs.com/"
        )
        driver = self._make_driver(iframes=[arkose])
        driver.find_elements.return_value = [arkose]
        wait = _make_wait()

        token = "2.abc123.challenge_token_value"
        mock_capsolver = MagicMock()
        mock_capsolver.solve.return_value = {"token": token}

        with patch.dict("sys.modules", {"capsolver": mock_capsolver}):
            from importlib import reload

            import cqc_lem.utilities.linkedin.helper as helper_mod
            reload(helper_mod)
            result = helper_mod.solve_arkose_challenge(driver, wait)

        assert result is True
        # Token must be injected via JS
        js_calls = [str(c) for c in driver.execute_script.call_args_list]
        assert any(token in call for call in js_calls), (
            f"Expected token '{token}' injected via execute_script, got: {js_calls}"
        )


@pytest.mark.unit
class TestLoginToLinkedinPostSubmitChallenge:
    """Post-submit challenge detection (new behaviour after login button click)."""

    def test_post_submit_challenge_calls_solver(self):
        """After submit, if URL is a challenge page, solve_arkose_challenge is invoked."""
        url_seq = [
            "https://www.linkedin.com",        # initial load
            "https://www.linkedin.com/login",  # after driver.get(login_url)
            "https://www.linkedin.com/checkpoint/challenge",  # after submit
        ]
        idx = [0]

        driver = MagicMock()
        driver.get_cookies.return_value = [{"name": "li_at"}]
        driver.find_elements.return_value = []  # no Arkose iframe

        def advance_url(url=None):
            idx[0] = min(idx[0] + 1, len(url_seq) - 1)

        type(driver).current_url = property(lambda self: url_seq[idx[0]])
        driver.get.side_effect = advance_url
        driver.find_element.return_value = MagicMock()

        wait = _make_wait()
        mock_field = MagicMock()

        with patch(f"{_MODULE}.get_cookies", return_value=None), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies"), \
             patch(f"{_MODULE}.get_visible_element_wait_retry", return_value=mock_field), \
             patch(f"{_MODULE}.solve_arkose_challenge", return_value=False) as mock_solve:
            with pytest.raises(RuntimeError, match="Unsolvable LinkedIn challenge"):
                from cqc_lem.utilities.linkedin.helper import login_to_linkedin
                login_to_linkedin(driver, wait, "user@e.com", "pw")

        mock_solve.assert_called_once()

    def test_device_approval_emails_user_high_priority(self, _stub_approval_email):
        """When the post-submit device-approval challenge is hit, the user is emailed."""
        url_seq = [
            "https://www.linkedin.com",
            "https://www.linkedin.com/login",
            "https://www.linkedin.com/checkpoint/challenge",
        ]
        idx = [0]
        driver = MagicMock()
        driver.get_cookies.return_value = [{"name": "li_at"}]
        driver.find_elements.return_value = []

        def advance_url(url=None):
            idx[0] = min(idx[0] + 1, len(url_seq) - 1)

        type(driver).current_url = property(lambda self: url_seq[idx[0]])
        driver.get.side_effect = advance_url

        wait = _make_wait()
        with patch(f"{_MODULE}.get_cookies", return_value=None), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies"), \
             patch(f"{_MODULE}.get_visible_element_wait_retry", return_value=MagicMock()), \
             patch(f"{_MODULE}.solve_arkose_challenge", return_value=False), \
             pytest.raises(RuntimeError):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "approver@e.com", "pw")

        _stub_approval_email.assert_called_once()
        assert _stub_approval_email.call_args[0][0] == "approver@e.com"

    def test_manual_approval_clears_checkpoint_and_logs_in(self, monkeypatch):
        """Device-approval checkpoint that clears mid-wait (user taps 'Yes' in the
        mobile app) → login completes and cookies are persisted.
        """
        monkeypatch.setenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "30")

        # url is driven by side effects: gets land on /login, the Sign-in click moves
        # to the checkpoint, and the first sleep *inside* challenge handling flips to /feed.
        state = {"url": "https://www.linkedin.com", "handling": False}
        driver = MagicMock()
        driver.get_cookies.return_value = [{"name": "li_at"}]
        driver.find_elements.return_value = []
        type(driver).current_url = property(lambda self: state["url"])
        driver.get.side_effect = lambda u: state.__setitem__("url", "https://www.linkedin.com/login")

        mock_field = MagicMock()
        mock_field.click.side_effect = lambda: state.__setitem__(
            "url", "https://www.linkedin.com/checkpoint/challenge")

        def fake_solve(*_a, **_k):
            state["handling"] = True  # entered challenge handling
            return False

        def sleep_flip(*_a, **_k):
            if state["handling"]:  # first poll inside the approval wait → user approved
                state["url"] = "https://www.linkedin.com/feed/"

        wait = _make_wait()
        with patch(f"{_MODULE}.get_cookies", return_value=None), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies") as mock_store, \
             patch(f"{_MODULE}.get_visible_element_wait_retry", return_value=mock_field), \
             patch(f"{_MODULE}.solve_arkose_challenge", side_effect=fake_solve) as mock_solve, \
             patch(f"{_MODULE}.time.sleep", side_effect=sleep_flip):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "user@e.com", "pw")

        mock_solve.assert_called_once()
        mock_store.assert_called_once()

    def test_new_challenge_paths_detected(self):
        """Newly added challenge paths (/captcha/, /security-verification) are caught."""
        for challenge_path in ["/captcha/", "/security-verification", "/error"]:
            url = f"https://www.linkedin.com{challenge_path}"
            driver = _make_driver(url)
            wait = _make_wait()

            with patch(f"{_MODULE}.get_cookies", return_value=[{"name": "x"}]), \
                 patch(f"{_MODULE}.load_cookies"), \
                 patch(f"{_MODULE}.store_cookies"), \
                 pytest.raises(RuntimeError):
                from cqc_lem.utilities.linkedin.helper import login_to_linkedin
                login_to_linkedin(driver, wait, "u@e.com", "pw")


@pytest.mark.unit
class TestLoginToLinkedinChallengePaths:
    """Challenge URL detection must cover all known challenge path prefixes."""

    @pytest.mark.parametrize("challenge_url", [
        "https://www.linkedin.com/checkpoint/challenge/abc",
        "https://www.linkedin.com/authwall?trk=x",
        "https://www.linkedin.com/uas/login",
        "https://www.linkedin.com/challenge/",
    ])
    def test_challenge_url_after_cookie_load_raises(self, challenge_url):
        driver = _make_driver(challenge_url)
        wait = _make_wait()

        with patch(f"{_MODULE}.get_cookies", return_value=[{"name": "x"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies"), \
             pytest.raises(RuntimeError):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "u@e.com", "pw")


@pytest.mark.unit
class TestRateLimitCircuitBreaker:
    """The shared 429 breaker must short-circuit login and record/clear state."""

    def test_open_breaker_skips_navigation(self):
        """When the breaker is open, login must raise before touching LinkedIn."""
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited

        with patch(f"{_MODULE}.rate_limit_cooldown_remaining", return_value=300), \
             patch(f"{_MODULE}.mark_rate_limited"), patch(f"{_MODULE}.clear_rate_limit"), \
             patch(f"{_MODULE}.get_cookies") as mock_cookies, \
             pytest.raises(LinkedInRateLimited, match="circuit breaker open"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "u@e.com", "pw")

        driver.get.assert_not_called()
        mock_cookies.assert_not_called()

    def test_paused_automation_skips_navigation(self):
        """A manual automation pause must halt login before touching LinkedIn."""
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited

        with patch(f"{_MODULE}.is_automation_paused", return_value=True), \
             patch(f"{_MODULE}.automation_pause_remaining", return_value=3600), \
             patch(f"{_MODULE}.get_cookies") as mock_cookies, \
             pytest.raises(LinkedInRateLimited, match="paused"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "u@e.com", "pw")

        driver.get.assert_not_called()
        mock_cookies.assert_not_called()

    def test_measurement_session_survives_the_suppression_pause(self):
        """Read-only stat capture keeps logging in under the suppression tripwire's OWN pause — it
        produces the readings the tripwire re-evaluates, so freezing it would make recovery
        permanently undetectable (issue #629). Every other pause still stops it.
        """
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()
        from cqc_lem.utilities.linkedin.helper import login_to_linkedin
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited

        with patch(f"{_MODULE}.is_measurement_paused", return_value=False), \
             patch(f"{_MODULE}.is_automation_paused", return_value=True), \
             patch(f"{_MODULE}.get_cookies", return_value=None), \
             patch(f"{_MODULE}.store_cookies"), \
             patch(f"{_MODULE}.load_cookies"):
            login_to_linkedin(driver, wait, "u@e.com", "pw", measurement_only=True)
        assert driver.get.called

        driver = _make_driver("https://www.linkedin.com/feed/")
        with patch(f"{_MODULE}.is_measurement_paused", return_value=True), \
             patch(f"{_MODULE}.automation_pause_remaining", return_value=3600), \
             patch(f"{_MODULE}.get_cookies") as mock_cookies, \
             pytest.raises(LinkedInRateLimited, match="paused"):
            login_to_linkedin(driver, wait, "u@e.com", "pw", measurement_only=True)
        driver.get.assert_not_called()
        mock_cookies.assert_not_called()

    def test_rate_limited_feed_no_cookies_opens_breaker(self):
        """A 429 body at /feed/ with NO stored cookies (nothing to drop) opens the breaker."""
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()
        body_el = MagicMock()
        body_el.text = "HTTP ERROR 429 Too Many Requests"
        driver.find_element.return_value = body_el
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited

        with patch(f"{_MODULE}.rate_limit_cooldown_remaining", return_value=0), \
             patch(f"{_MODULE}.mark_rate_limited") as mock_mark, \
             patch(f"{_MODULE}.clear_rate_limit") as mock_clear, \
             patch(f"{_MODULE}.get_cookies", return_value=None), \
             patch(f"{_MODULE}.load_cookies"), patch(f"{_MODULE}.store_cookies"), \
             pytest.raises(LinkedInRateLimited, match="rate-limiting"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "u@e.com", "pw")

        mock_mark.assert_called_once()
        mock_clear.assert_not_called()

    def test_rate_limited_with_cookies_self_heals_via_fresh_login(self):
        """A 429 at /feed/ WITH stored cookies is a stale-cookie/egress-IP mismatch: drop the
        cookies and do a fresh credential login instead of opening the breaker.
        """
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()
        # stage drives both the page body and the "logged-in" URL:
        #   feed_429 (cookie'd feed 429) -> login (after cookie drop) -> feed_ok (after re-login)
        state = {"stage": "feed_429"}
        bodies = {"feed_429": "HTTP ERROR 429 Too Many Requests",
                  "login": "Sign in to LinkedIn", "feed_ok": "Welcome to your feed"}

        def _body(*_a, **_k):
            el = MagicMock(); el.text = bodies[state["stage"]]; return el
        driver.find_element.side_effect = _body

        def _get(url):
            if "login" in url:
                state["stage"] = "login"
                driver.current_url = "https://www.linkedin.com/login"
            else:
                driver.current_url = "https://www.linkedin.com/feed/"
        driver.get.side_effect = _get

        field = MagicMock()

        def _click():  # submitting the fresh login lands back on a clean feed
            state["stage"] = "feed_ok"
            driver.current_url = "https://www.linkedin.com/feed/"
        field.click.side_effect = _click

        with patch(f"{_MODULE}.rate_limit_cooldown_remaining", return_value=0), \
             patch(f"{_MODULE}.mark_rate_limited") as mock_mark, \
             patch(f"{_MODULE}.clear_rate_limit") as mock_clear, \
             patch(f"{_MODULE}.get_cookies", return_value=[{"name": "li_at"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies") as mock_store, \
             patch(f"{_MODULE}.get_visible_element_wait_retry", return_value=field):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "u@e.com", "pw")

        # Profile-wide, NOT driver.delete_all_cookies — the browser is parked on a Chrome error
        # page at this point, where the scoped delete clears nothing at all.
        assert any(c.args and c.args[0] == "Network.clearBrowserCookies"
                   for c in driver.execute_cdp_cmd.call_args_list), \
            "stale cookies must be dropped profile-wide"
        mock_mark.assert_not_called()                     # self-healed — breaker NOT opened
        mock_clear.assert_called()                        # fresh login cleared stale breaker state
        mock_store.assert_called()                        # fresh proxy-native cookies stored

    def test_successful_login_clears_breaker(self):
        """A clean cookie login must clear any stale breaker state."""
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()

        with patch(f"{_MODULE}.rate_limit_cooldown_remaining", return_value=0), \
             patch(f"{_MODULE}.mark_rate_limited"), \
             patch(f"{_MODULE}.clear_rate_limit") as mock_clear, \
             patch(f"{_MODULE}.get_cookies", return_value=[{"name": "x"}]), \
             patch(f"{_MODULE}.load_cookies"), patch(f"{_MODULE}.store_cookies"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "u@e.com", "pw")

        mock_clear.assert_called_once()


class TestChallengeCooldownBreaker:
    """Issue #1920: back off after an unsolvable login challenge.

    An account whose login challenge was unsolvable by every automated path (Arkose, email PIN,
    manual approval) must back off instead of repeating the identical failed dance on the very
    next scheduled run.
    """

    def test_active_cooldown_skips_navigation(self):
        """When this account's cooldown is active, login must raise before touching LinkedIn."""
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited

        with patch(f"{_MODULE}._user_id_for_email", return_value=7), \
             patch(f"{_MODULE}.challenge_cooldown_remaining", return_value=300), \
             patch(f"{_MODULE}.get_cookies") as mock_cookies, \
             pytest.raises(LinkedInRateLimited, match="cooling down"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "u@e.com", "pw")

        driver.get.assert_not_called()
        mock_cookies.assert_not_called()

    def test_no_cooldown_proceeds_normally(self):
        """No recorded cooldown must not change ordinary cookie-login behaviour."""
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()

        with patch(f"{_MODULE}._user_id_for_email", return_value=7), \
             patch(f"{_MODULE}.challenge_cooldown_remaining", return_value=0), \
             patch(f"{_MODULE}.get_cookies", return_value=[{"name": "li_at"}]), \
             patch(f"{_MODULE}.load_cookies"), patch(f"{_MODULE}.store_cookies"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "u@e.com", "pw")

        assert driver.get.called

    def test_unresolvable_user_id_never_blocks_login(self):
        """A failed uid lookup must fail OPEN.

        Same as the shared 429 breaker — never blocking a login that would otherwise work.
        """
        driver = _make_driver("https://www.linkedin.com/feed/")
        wait = _make_wait()

        with patch(f"{_MODULE}._user_id_for_email", return_value=None), \
             patch(f"{_MODULE}.challenge_cooldown_remaining") as mock_cooldown, \
             patch(f"{_MODULE}.get_cookies", return_value=[{"name": "li_at"}]), \
             patch(f"{_MODULE}.load_cookies"), patch(f"{_MODULE}.store_cookies"):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "u@e.com", "pw")

        mock_cooldown.assert_not_called()
        assert driver.get.called

    def test_unsolvable_challenge_records_the_cooldown_for_the_next_attempt(self):
        """The final unsolvable-challenge branch must mark this account for the next attempt.

        A call that follows within the cooldown window takes the fast, no-navigation path above
        instead of repeating the whole failed login (issue #1920, 19 occurrences in under 5h for
        one account).
        """
        url_seq = [
            "https://www.linkedin.com",
            "https://www.linkedin.com/login",
            "https://www.linkedin.com/checkpoint/challenge",
        ]
        idx = [0]

        driver = MagicMock()
        driver.get_cookies.return_value = [{"name": "li_at"}]
        driver.find_elements.return_value = []  # no Arkose iframe

        def advance_url(url=None):
            idx[0] = min(idx[0] + 1, len(url_seq) - 1)

        type(driver).current_url = property(lambda self: url_seq[idx[0]])
        driver.get.side_effect = advance_url
        driver.find_element.return_value = MagicMock()

        wait = _make_wait()

        with patch(f"{_MODULE}._user_id_for_email", return_value=7), \
             patch(f"{_MODULE}.challenge_cooldown_remaining", return_value=0), \
             patch(f"{_MODULE}.mark_challenge_unsolvable") as mock_mark, \
             patch(f"{_MODULE}.get_cookies", return_value=None), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies"), \
             patch(f"{_MODULE}.get_visible_element_wait_retry", return_value=MagicMock()), \
             patch(f"{_MODULE}.solve_arkose_challenge", return_value=False):
            with pytest.raises(RuntimeError, match="Unsolvable LinkedIn challenge"):
                from cqc_lem.utilities.linkedin.helper import login_to_linkedin
                login_to_linkedin(driver, wait, "u@e.com", "pw")

        mock_mark.assert_called_once_with(7)


@pytest.mark.unit
class TestDriveEmailPinChallenge:
    """The email verification-code challenge path (drive_email_pin_challenge)."""

    def _driver(self, otp=None, buttons=None,
                body="Enter the verification code we sent to your email"):
        from selenium.webdriver.common.by import By as _By
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/checkpoint/challenge/x"
        body_el = MagicMock(); body_el.text = body
        driver.find_element.return_value = body_el

        def find_elements(by, sel):
            if by == _By.XPATH:
                return buttons or []
            if by == _By.CSS_SELECTOR and sel == "input[name='pin']":
                return [otp] if otp is not None else []
            return []
        driver.find_elements.side_effect = find_elements
        return driver

    def test_no_otp_field_returns_false_without_emailing(self):
        driver = self._driver(otp=None, buttons=[])  # no code field, nothing to click
        with patch("cqc_lem.utilities.email.send_login_pin_request_email") as mail, \
             patch("cqc_lem.utilities.db.get_user_id", return_value=1):
            from cqc_lem.utilities.linkedin.helper import drive_email_pin_challenge
            assert drive_email_pin_challenge(driver, "u@e.com", lambda url: True) is False
        mail.assert_not_called()

    def test_success_emails_polls_and_submits(self):
        otp = MagicMock(); otp.is_displayed.return_value = True
        submit_btn = MagicMock(); submit_btn.text = "Submit"
        driver = self._driver(otp=otp, buttons=[submit_btn])
        with patch("cqc_lem.utilities.db.get_user_id", return_value=7), \
             patch("cqc_lem.utilities.linkedin.verification_pin.create_pin_request", return_value="tok123"), \
             patch("cqc_lem.utilities.linkedin.verification_pin.clear_pin"), \
             patch("cqc_lem.utilities.linkedin.verification_pin.pin_reply_address", return_value="pin+tok123@parse.x"), \
             patch("cqc_lem.utilities.linkedin.verification_pin.get_pin", return_value="483920"), \
             patch("cqc_lem.utilities.email.send_login_pin_request_email", return_value=True) as mail:
            from cqc_lem.utilities.linkedin.helper import drive_email_pin_challenge
            ok = drive_email_pin_challenge(driver, "u@e.com", lambda url: True)
        assert ok is True
        mail.assert_called_once_with("u@e.com", "pin+tok123@parse.x")
        otp.send_keys.assert_called_once_with("483920")

    def test_pin_never_arrives_returns_false(self):
        otp = MagicMock(); otp.is_displayed.return_value = True
        driver = self._driver(otp=otp, buttons=[])
        with patch("cqc_lem.utilities.db.get_user_id", return_value=7), \
             patch("cqc_lem.utilities.linkedin.verification_pin.create_pin_request", return_value="tok"), \
             patch("cqc_lem.utilities.linkedin.verification_pin.clear_pin"), \
             patch("cqc_lem.utilities.linkedin.verification_pin.pin_reply_address", return_value="a@b"), \
             patch("cqc_lem.utilities.linkedin.verification_pin.get_pin", return_value=None), \
             patch("cqc_lem.utilities.email.send_login_pin_request_email", return_value=True), \
             patch(f"{_MODULE}.time.time", side_effect=[0, 0, 10_000]):  # trip the poll deadline
            from cqc_lem.utilities.linkedin.helper import drive_email_pin_challenge
            assert drive_email_pin_challenge(driver, "u@e.com", lambda url: True) is False


@pytest.mark.unit
class TestRedirectLoopRecovery:
    """Stale cookies from a different egress IP produce a redirect-loop page at /feed/;
    login must recover via a fresh credential login instead of mistaking it for a 429
    or for a live session.
    """

    def _run(self):
        state = {"url": "https://www.linkedin.com"}
        body = {"text": ""}

        def on_get(u):
            if "feed" in u:
                state["url"] = "https://www.linkedin.com/feed/"
                body["text"] = ("This page isn't working www.linkedin.com redirected "
                                "you too many times ERR_TOO_MANY_REDIRECTS")
            elif "login" in u:
                state["url"] = "https://www.linkedin.com/login"
                body["text"] = ""
            else:
                state["url"] = "https://www.linkedin.com"
                body["text"] = ""

        driver = MagicMock()
        driver.get.side_effect = on_get
        driver.get_cookies.return_value = [{"name": "li_at", "value": "fresh"}]
        type(driver).current_url = property(lambda self: state["url"])
        body_el = MagicMock()
        type(body_el).text = property(lambda self: body["text"])
        driver.find_element.return_value = body_el

        signin = MagicMock()
        def do_click():
            state["url"] = "https://www.linkedin.com/feed/"
            body["text"] = ""   # feed loads cleanly after credential login
        signin.click.side_effect = do_click
        fields = [MagicMock(), MagicMock(), signin]  # username, password, sign-in

        wait = _make_wait()
        with patch(f"{_MODULE}.rate_limit_cooldown_remaining", return_value=0), \
             patch(f"{_MODULE}.mark_rate_limited") as mock_mark, \
             patch(f"{_MODULE}.clear_rate_limit"), \
             patch(f"{_MODULE}.get_cookies", return_value=[{"name": "li_at", "value": "stale"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies") as mock_store, \
             patch(f"{_MODULE}.get_visible_element_wait_retry", side_effect=fields):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            login_to_linkedin(driver, wait, "user@e.com", "pw")
        return driver, mock_mark, mock_store

    def test_redirect_loop_clears_cookies_and_reauths(self):
        driver, mock_mark, mock_store = self._run()
        # Stale browser cookies dropped, then a fresh credential login navigated to /login.
        # The clear must be profile-wide: the loop page has no linkedin.com origin, so the
        # scoped driver.delete_all_cookies() silently clears nothing.
        assert any(c.args and c.args[0] == "Network.clearBrowserCookies"
                   for c in driver.execute_cdp_cmd.call_args_list), \
            "expected a profile-wide cookie clear, not the origin-scoped one"
        assert any("login" in c.args[0] for c in driver.get.call_args_list), \
            "expected a fresh credential login navigation to /login"

    def test_redirect_loop_not_treated_as_rate_limit(self):
        # The redirect page also says "this page isn't working"; it must NOT trip the 429
        # breaker (checked before the rate-limit heuristic).
        _, mock_mark, _ = self._run()
        mock_mark.assert_not_called()

    def test_redirect_loop_recovers_to_successful_login(self):
        _, _, mock_store = self._run()
        mock_store.assert_called_once()  # fresh cookies persisted after re-auth


@pytest.mark.unit
class TestHardClearCookies:
    """The cookie clear must be profile-wide, not origin-scoped.

    `delete_all_cookies` is scoped to the current document's origin, and every caller of the
    cookie-clearing recovery paths is parked on a Chrome error page — which has no origin. The
    clear has to be profile-wide or it silently does nothing.
    """

    def test_prefers_the_profile_wide_cdp_clear(self):
        from cqc_lem.utilities.linkedin.helper import hard_clear_cookies
        driver = MagicMock()
        assert hard_clear_cookies(driver) is True
        driver.execute_cdp_cmd.assert_called_once_with("Network.clearBrowserCookies", {})
        driver.delete_all_cookies.assert_not_called()

    def test_falls_back_when_cdp_is_unavailable(self):
        from cqc_lem.utilities.linkedin.helper import hard_clear_cookies
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = Exception("no CDP on this driver")
        assert hard_clear_cookies(driver) is False
        driver.delete_all_cookies.assert_called_once()

    def test_never_raises_into_the_login_path(self):
        """Both clears failing must not abort a login — it is a best-effort recovery step."""
        from cqc_lem.utilities.linkedin.helper import hard_clear_cookies
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = Exception("no CDP")
        driver.delete_all_cookies.side_effect = Exception("no origin either")
        assert hard_clear_cookies(driver) is False


@pytest.mark.unit
class TestLoopSurvivesTheCookieClear:
    """A throttle that survives the cookie clear must back off, not retry forever.

    LinkedIn throttles the ACCOUNT, so the same error page comes back after the cookies are
    dropped. The old code fell through to the username-field wait, timed out, and the task
    retried a minute later — forever, never opening the breaker and discarding a perfectly
    good `li_at` on every pass.
    """

    def _run(self, login_body: str):
        state = {"url": "https://www.linkedin.com"}
        body = {"text": ""}
        def on_get(u):
            if "feed" in u:
                state["url"] = "https://www.linkedin.com/feed/"
                body["text"] = "HTTP ERROR 429 Too Many Requests"
            elif "login" in u:
                state["url"] = "https://www.linkedin.com/login"
                body["text"] = login_body
            else:
                state["url"] = "https://www.linkedin.com"
                body["text"] = ""

        driver = MagicMock()
        driver.get.side_effect = on_get
        driver.get_cookies.return_value = [{"name": "li_at", "value": "fresh"}]
        type(driver).current_url = property(lambda self: state["url"])
        body_el = MagicMock()
        type(body_el).text = property(lambda self: body["text"])
        driver.find_element.return_value = body_el

        with patch(f"{_MODULE}.rate_limit_cooldown_remaining", return_value=0), \
             patch(f"{_MODULE}.mark_rate_limited") as mock_mark, \
             patch(f"{_MODULE}.clear_rate_limit"), \
             patch(f"{_MODULE}.get_cookies", return_value=[{"name": "li_at"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies"), \
             patch(f"{_MODULE}.get_visible_element_wait_retry") as mock_find:
            from cqc_lem.utilities.linkedin.helper import LinkedInRateLimited, login_to_linkedin
            with pytest.raises(LinkedInRateLimited) as exc:
                login_to_linkedin(driver, wait := _make_wait(), "u@e.com", "pw")
                assert wait
        return exc.value, mock_mark, mock_find

    def test_still_looping_after_the_clear_opens_the_breaker(self):
        err, mock_mark, mock_find = self._run(
            "This page isn't working www.linkedin.com redirected you too many times "
            "ERR_TOO_MANY_REDIRECTS")
        assert "ERR_TOO_MANY_REDIRECTS" in str(err)
        # The breaker is the ONLY thing that lets an account-level throttle decay.
        mock_mark.assert_called_once()
        # And we must never fall through to hunting for a form on an error page.
        mock_find.assert_not_called()

    def test_login_page_429_after_the_clear_opens_the_breaker(self):
        _, mock_mark, mock_find = self._run("HTTP ERROR 429 Too Many Requests")
        mock_mark.assert_called_once()
        mock_find.assert_not_called()

    def test_proxy_blip_on_the_login_page_does_not_open_the_breaker(self):
        """A proxy blip must stay transient.

        The task retries later, and tripping the shared breaker on a blip would pause every
        lane for nothing.
        """
        err, mock_mark, _ = self._run("This site can't be reached ERR_CONNECTION_RESET")
        assert "proxy/network error" in str(err)
        mock_mark.assert_not_called()

    def test_a_usable_login_page_still_proceeds_to_credential_login(self):
        """Recovery backs off only when the page is genuinely broken.

        A real login form after the clear is the self-heal path and has to keep working.
        """
        state = {"url": "https://www.linkedin.com"}
        body = {"text": ""}

        def on_get(u):
            if "feed" in u:
                state["url"] = "https://www.linkedin.com/feed/"
                body["text"] = "HTTP ERROR 429 Too Many Requests"
            elif "login" in u:
                state["url"] = "https://www.linkedin.com/login"
                body["text"] = "Sign in Email or phone Password"
            else:
                state["url"] = "https://www.linkedin.com"
                body["text"] = ""

        driver = MagicMock()
        driver.get.side_effect = on_get
        driver.get_cookies.return_value = [{"name": "li_at", "value": "new"}]
        type(driver).current_url = property(lambda self: state["url"])
        body_el = MagicMock()
        type(body_el).text = property(lambda self: body["text"])
        driver.find_element.return_value = body_el

        signin = MagicMock()

        def do_click():
            state["url"] = "https://www.linkedin.com/feed/"
            body["text"] = "Welcome to your feed"
        signin.click.side_effect = do_click

        with patch(f"{_MODULE}.rate_limit_cooldown_remaining", return_value=0), \
             patch(f"{_MODULE}.mark_rate_limited") as mock_mark, \
             patch(f"{_MODULE}.clear_rate_limit"), \
             patch(f"{_MODULE}.get_cookies", return_value=[{"name": "li_at"}]), \
             patch(f"{_MODULE}.load_cookies"), \
             patch(f"{_MODULE}.store_cookies") as mock_store, \
             patch(f"{_MODULE}.get_visible_element_wait_retry",
                   side_effect=[MagicMock(), MagicMock(), signin]):
            from cqc_lem.utilities.linkedin.helper import login_to_linkedin
            assert login_to_linkedin(driver, _make_wait(), "u@e.com", "pw") is True

        mock_mark.assert_not_called()
        mock_store.assert_called()


# ---------------------------------------------------------------------------
# 429 / transport-error page classification (hardened detector)
# ---------------------------------------------------------------------------

class TestPageClassification:
    def test_explicit_429_body_is_rate_limited(self):
        from cqc_lem.utilities.linkedin.helper import _text_is_rate_limited
        assert _text_is_rate_limited("HTTP ERROR 429") is True

    def test_too_many_requests_is_rate_limited(self):
        from cqc_lem.utilities.linkedin.helper import _text_is_rate_limited
        assert _text_is_rate_limited("Too Many Requests") is True

    def test_linkedin_999_is_rate_limited(self):
        from cqc_lem.utilities.linkedin.helper import _text_is_rate_limited
        assert _text_is_rate_limited("HTTP ERROR 999") is True

    def test_tiny_shell_with_429_code_is_rate_limited(self):
        from cqc_lem.utilities.linkedin.helper import _text_is_rate_limited
        assert _text_is_rate_limited("This page isn't working. HTTP ERROR 429") is True

    def test_tiny_shell_without_code_is_not_rate_limited(self):
        # Bare "This page isn't working" (no 429/999) must NOT trip the breaker — it's the generic
        # Chrome HTTP-error shell also used for 500/502/503.
        from cqc_lem.utilities.linkedin.helper import _text_is_rate_limited
        assert _text_is_rate_limited("This page isn't working right now.") is False

    def test_server_error_500_is_not_rate_limited(self):
        from cqc_lem.utilities.linkedin.helper import _text_is_rate_limited
        assert _text_is_rate_limited("This page isn't working. HTTP ERROR 500") is False

    def test_proxy_error_is_not_rate_limited(self):
        # A flaky residential proxy must not be mistaken for a LinkedIn throttle.
        from cqc_lem.utilities.linkedin.helper import _text_is_rate_limited
        assert _text_is_rate_limited("This site can't be reached. ERR_PROXY_CONNECTION_FAILED") is False

    def test_proxy_error_is_transport_error(self):
        from cqc_lem.utilities.linkedin.helper import _text_is_transport_error
        assert _text_is_transport_error("This site can't be reached\nERR_TIMED_OUT") is True

    def test_normal_feed_is_neither(self):
        from cqc_lem.utilities.linkedin.helper import _text_is_rate_limited, _text_is_transport_error
        body = "Home  My Network  Jobs  Messaging  Notifications  Start a post"
        assert _text_is_rate_limited(body) is False
        assert _text_is_transport_error(body) is False

    def test_empty_body_is_neither(self):
        from cqc_lem.utilities.linkedin.helper import _text_is_rate_limited, _text_is_transport_error
        assert _text_is_rate_limited("") is False
        assert _text_is_transport_error("") is False


class TestHumanPause:
    def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_HUMANIZE_DELAYS", "false")
        import cqc_lem.utilities.linkedin.helper as h
        with patch.object(h.time, "sleep") as slept:
            h._human_pause(1.0, 2.0)
        slept.assert_not_called()

    def test_enabled_sleeps_within_range(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_HUMANIZE_DELAYS", raising=False)
        import cqc_lem.utilities.linkedin.helper as h
        with patch.object(h.time, "sleep") as slept, \
             patch.object(h.random, "uniform", return_value=1.5) as uni:
            h._human_pause(1.0, 2.0)
        uni.assert_called_once_with(1.0, 2.0)
        slept.assert_called_once_with(1.5)


class TestPersistSessionCookies:
    """store_cookies' bool is load-bearing (issue #745) — the login path must not claim a
    session was saved when the write was refused or swallowed a driver error. A lost write is
    invisible until the NEXT run falls back to a password login and trips the device challenge.
    """

    def _run(self, stored):
        from cqc_lem.utilities.linkedin.helper import _persist_session_cookies
        driver = MagicMock()
        driver.get_cookies.return_value = [{"name": "li_at"}]
        with patch(f"{_MODULE}.store_cookies", return_value=stored), \
             patch(f"{_MODULE}.log_error") as logged:
            return _persist_session_cookies(driver, "a@b.com"), logged

    def test_successful_write_is_reported_as_stored(self):
        ok, logged = self._run(True)
        assert ok is True
        logged.assert_not_called()

    def test_failed_write_is_surfaced_not_announced_as_success(self):
        ok, logged = self._run(False)
        assert ok is False
        logged.assert_called_once()
