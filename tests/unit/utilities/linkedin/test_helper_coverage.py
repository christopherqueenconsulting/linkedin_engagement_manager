"""Coverage tests for linkedin/helper.py: Arkose solver, text clicking, profile fetch/caching.

The two refusal contracts — every way the Arkose solve gives up, every way a text click
fails to land — are parametrized tables (issue #1216). The login and profile-cache paths
stay plain: each pins a different sequence of collaborator calls.
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import WebDriverException

pytestmark = pytest.mark.unit

_H = "cqc_lem.utilities.linkedin.helper"


def _profile_json():
    return ('{"full_name": "Jane Doe", "job_title": "CTO", "company_name": "Acme"}',)


# Every way the solve gives up answers the same way — False, and nothing typed into the
# challenge. (case id, API key, iframe src, page source, what capsolver.solve does,
# whether solve should have been called at all)
_ARKOSE_REFUSALS = [
    ("no_api_key", None, None, None, None, False),
    # The host must BE arkoselabs, not merely mention it in a query string.
    ("iframe_is_not_arkose", "key", "https://evil.com/?arkoselabs.com", None, None, False),
    ("empty_token", "key", "https://client-api.arkoselabs.com/fc/api?pk=PUBKEY123", None,
     {"token": ""}, True),
    ("no_public_key_anywhere", "key", "https://client-api.arkoselabs.com/fc/api",
     "<html>nothing here</html>", {"token": "tok"}, False),
    ("solver_exception", "key", "https://client-api.arkoselabs.com/fc/api?pk=PUBKEY123", None,
     RuntimeError("api down"), True),
]


class TestSolveArkoseChallenge:
    @pytest.mark.parametrize("case_id,api_key,frame_src,page_source,solve,solve_called",
                             _ARKOSE_REFUSALS, ids=[c[0] for c in _ARKOSE_REFUSALS])
    def test_refusals_return_false(self, monkeypatch, case_id, api_key, frame_src, page_source,
                                   solve, solve_called):
        from cqc_lem.utilities.linkedin.helper import solve_arkose_challenge
        if api_key:
            monkeypatch.setenv("CAPSOLVER_API_KEY", api_key)
        else:
            monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
        driver = MagicMock()
        if frame_src:
            frame = MagicMock()
            frame.get_attribute.return_value = frame_src
            driver.find_elements.return_value = [frame]
            driver.current_url = "https://www.linkedin.com/checkpoint/challenge"
        if page_source is not None:
            driver.page_source = page_source
        fake_capsolver = types.ModuleType("capsolver")
        fake_capsolver.solve = MagicMock(
            side_effect=solve if isinstance(solve, Exception) else None,
            return_value=solve if not isinstance(solve, Exception) else None)
        with patch.dict(sys.modules, {"capsolver": fake_capsolver}), \
             patch(f"{_H}.time.sleep"):
            assert solve_arkose_challenge(driver, MagicMock()) is False
        if solve_called:
            # These two cases exist to pin what the SOLVER answered, so they are only worth
            # anything while the solver is still reached — a pk-parsing regression would
            # otherwise turn them into duplicates of the refuse-before-solving cases.
            fake_capsolver.solve.assert_called_once()
        else:
            fake_capsolver.solve.assert_not_called()

    def _driver_with_arkose(self):
        driver = MagicMock()
        frame = MagicMock()
        frame.get_attribute.return_value = (
            "https://client-api.arkoselabs.com/fc/api?pk=PUBKEY123")
        driver.find_elements.return_value = [frame]
        driver.current_url = "https://www.linkedin.com/checkpoint/challenge"
        return driver

    def test_solves_funcaptcha_with_capsolver(self, monkeypatch):
        from cqc_lem.utilities.linkedin.helper import solve_arkose_challenge
        monkeypatch.setenv("CAPSOLVER_API_KEY", "key")
        fake_capsolver = types.ModuleType("capsolver")
        fake_capsolver.solve = MagicMock(return_value={"token": "tok123"})
        driver = self._driver_with_arkose()
        submit = MagicMock()
        driver.find_element.return_value = submit
        with patch.dict(sys.modules, {"capsolver": fake_capsolver}), \
             patch(f"{_H}.time.sleep"):
            assert solve_arkose_challenge(driver, MagicMock()) is True
        task = fake_capsolver.solve.call_args[0][0]
        assert task["type"] == "FunCaptchaTask"
        assert task["websitePublicKey"] == "PUBKEY123"
        submit.click.assert_called_once()

    def test_missing_public_key_falls_back_to_page_source(self, monkeypatch):
        from cqc_lem.utilities.linkedin.helper import solve_arkose_challenge
        monkeypatch.setenv("CAPSOLVER_API_KEY", "key")
        fake_capsolver = types.ModuleType("capsolver")
        fake_capsolver.solve = MagicMock(return_value={"token": "tok"})
        driver = MagicMock()
        frame = MagicMock()
        frame.get_attribute.return_value = "https://client-api.arkoselabs.com/fc/api"
        driver.find_elements.return_value = [frame]
        driver.page_source = '{"public_key": "FROMPAGE"}'
        driver.current_url = "https://www.linkedin.com/checkpoint"
        with patch.dict(sys.modules, {"capsolver": fake_capsolver}), \
             patch(f"{_H}.time.sleep"):
            solve_arkose_challenge(driver, MagicMock())
        assert fake_capsolver.solve.call_args[0][0]["websitePublicKey"] == "FROMPAGE"


def _element(text="", click_exc=None):
    el = MagicMock()
    if isinstance(text, Exception):
        type(el).text = property(lambda self: (_ for _ in ()).throw(text))
    else:
        el.text = text
    if click_exc:
        el.click.side_effect = click_exc
    return el


class TestClickByText:
    def test_clicks_matching_element(self):
        from cqc_lem.utilities.linkedin.helper import _click_by_text
        driver = MagicMock()
        el = _element("Send Code")
        driver.find_elements.return_value = [_element("Other"), el]
        assert _click_by_text(driver, ["send code"]) is True
        el.click.assert_called_once()

    def test_falls_back_to_js_click(self):
        from cqc_lem.utilities.linkedin.helper import _click_by_text
        driver = MagicMock()
        el = _element("Verify", click_exc=WebDriverException("obscured"))
        driver.find_elements.return_value = [el]
        assert _click_by_text(driver, ["verify"]) is True
        driver.execute_script.assert_called_once()

    # Success is the click LANDING, so every way it doesn't reads False rather than
    # "we tried". (case id, the element on the page, keywords, whether the JS fallback fails)
    @pytest.mark.parametrize("case_id,text,click_exc,keywords,js_fails", [
        ("stale_element", WebDriverException("stale"), None, ["verify"], False),
        ("js_click_also_fails", "Verify", WebDriverException("obscured"), ["verify"], True),
        ("no_matching_text", "Cancel", None, ["submit"], False),
    ], ids=["stale_element", "js_click_also_fails", "no_matching_text"])
    def test_unlanded_clicks_return_false(self, case_id, text, click_exc, keywords, js_fails):
        from cqc_lem.utilities.linkedin.helper import _click_by_text
        driver = MagicMock()
        driver.find_elements.return_value = [_element(text, click_exc=click_exc)]
        if js_fails:
            driver.execute_script.side_effect = WebDriverException("js fail")
        assert _click_by_text(driver, keywords) is False


class TestGetMyProfile:
    def test_restores_cached_profile_by_user_id(self):
        from cqc_lem.utilities.linkedin.helper import get_my_profile
        with patch(f"{_H}.get_linked_in_profile_by_user_id",
                   return_value=_profile_json()) as by_uid:
            profile = get_my_profile(MagicMock(), MagicMock(), "a@x.com", "pw", user_id=3)
        by_uid.assert_called_once_with(3)
        assert profile.full_name == "Jane Doe"

    def test_falls_back_to_email_cache_without_user_id(self):
        from cqc_lem.utilities.linkedin.helper import get_my_profile
        with patch(f"{_H}.get_linked_in_profile_by_email",
                   return_value=_profile_json()) as by_email:
            profile = get_my_profile(MagicMock(), MagicMock(), "a@x.com", "pw")
        by_email.assert_called_once_with("a@x.com")
        assert profile.full_name == "Jane Doe"

    def test_scrapes_and_saves_when_cache_miss(self):
        from cqc_lem.utilities.linkedin.helper import get_my_profile
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/in/jane/"
        scraped = {"full_name": "Jane Doe", "job_title": "CTO"}
        with patch(f"{_H}.get_linked_in_profile_by_user_id", return_value=None), \
             patch(f"{_H}.login_to_linkedin") as login, \
             patch(f"{_H}.get_linkedin_profile_from_url", return_value=scraped), \
             patch(f"{_H}.add_linkedin_profile", return_value=True) as add, \
             patch(f"{_H}.time.sleep"):
            profile = get_my_profile(driver, MagicMock(), "a@x.com", "pw", user_id=3)
        login.assert_called_once()
        assert profile.full_name == "Jane Doe"
        assert profile.email == "a@x.com" and profile.password == "pw"
        assert add.call_args[1]["user_id"] == 3

    def test_returns_none_when_scrape_fails(self):
        from cqc_lem.utilities.linkedin.helper import get_my_profile
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/in/jane/"
        with patch(f"{_H}.get_linked_in_profile_by_user_id", return_value=None), \
             patch(f"{_H}.login_to_linkedin"), \
             patch(f"{_H}.get_linkedin_profile_from_url", return_value=None), \
             patch(f"{_H}.time.sleep"):
            assert get_my_profile(driver, MagicMock(), "a@x.com", "pw", user_id=3) is None

    def test_force_refresh_scrapes_past_a_warm_cache(self):
        """The freshness window is exactly wrong for a profile edited minutes ago (issue #1076).

        The row is fresh by AGE and stale by CONTENT, so the on-demand refresh skips the read.
        """
        from cqc_lem.utilities.linkedin.helper import get_my_profile
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/in/jane/"
        scraped = {"full_name": "Jane Doe", "job_title": "CTO"}
        with patch(f"{_H}.get_linked_in_profile_by_user_id",
                   return_value=_profile_json()) as by_uid, \
             patch(f"{_H}.get_linked_in_profile_by_email",
                   return_value=_profile_json()) as by_email, \
             patch(f"{_H}.login_to_linkedin") as login, \
             patch(f"{_H}.get_linkedin_profile_from_url", return_value=scraped) as from_url, \
             patch(f"{_H}.add_linkedin_profile", return_value=True), \
             patch(f"{_H}.time.sleep"):
            profile = get_my_profile(driver, MagicMock(), "a@x.com", "pw", user_id=3,
                                     force_refresh=True)
        by_uid.assert_not_called()
        by_email.assert_not_called()
        login.assert_called_once()
        assert profile.full_name == "Jane Doe"
        # The by-URL cache is the second layer, and skipping only the first would hand back the
        # very row the caller asked to ignore.
        assert from_url.call_args.kwargs["force_refresh"] is True


class TestGetLinkedInProfileFromUrl:
    def test_cached_profile_returned_as_dict(self):
        from cqc_lem.utilities.linkedin.helper import get_linkedin_profile_from_url
        with patch(f"{_H}.get_linked_in_profile_by_url", return_value=_profile_json()):
            data = get_linkedin_profile_from_url(MagicMock(), MagicMock(),
                                                 "https://www.linkedin.com/in/jane/")
        assert data["full_name"] == "Jane Doe"

    def test_scrapes_enriches_industry_and_saves(self):
        from cqc_lem.utilities.linkedin.helper import get_linkedin_profile_from_url
        url = "https://www.linkedin.com/in/jane/"
        driver = MagicMock()
        driver.current_url = url  # no navigation/redirect needed
        company_el = MagicMock()
        scraped = {"full_name": "Jane Doe", "company_name": "Acme"}
        with patch(f"{_H}.get_linked_in_profile_by_url", return_value=None), \
             patch(f"{_H}.get_element_wait_retry", return_value=company_el), \
             patch(f"{_H}.getText", return_value="Acme"), \
             patch(f"{_H}.returnProfileInfo", return_value=scraped) as rpi, \
             patch(f"{_H}.get_industries_of_profile_from_ai", return_value="Tech"), \
             patch(f"{_H}.add_linkedin_profile", return_value=True) as add:
            data = get_linkedin_profile_from_url(driver, MagicMock(), url)
        # Fresh scrape now returns the enriched LinkedInProfile dict, not the raw scraped dict,
        # so `industry` is present on the first call (issue #1101).
        assert data["full_name"] == "Jane Doe"
        assert data["industry"] == "Tech"
        rpi.assert_called_once_with(driver, url, "Acme", False)
        assert add.call_args[0][0].industry == "Tech"

    def test_redirect_recurses_with_new_url(self):
        from cqc_lem.utilities.linkedin.helper import get_linkedin_profile_from_url
        driver = MagicMock()
        # First check: current_url differs -> driver.get; still differs -> recurse
        driver.current_url = "https://www.linkedin.com/in/jane-redirected/"
        with patch(f"{_H}.get_linked_in_profile_by_url",
                   side_effect=[None, _profile_json()]), \
             patch(f"{_H}.time.sleep"):
            data = get_linkedin_profile_from_url(driver, MagicMock(),
                                                 "https://www.linkedin.com/in/old/")
        driver.get.assert_called_once_with("https://www.linkedin.com/in/old/")
        assert data["full_name"] == "Jane Doe"

    def test_force_refresh_never_reads_the_by_url_cache(self):
        from cqc_lem.utilities.linkedin.helper import get_linkedin_profile_from_url
        url = "https://www.linkedin.com/in/jane/"
        driver = MagicMock()
        driver.current_url = url
        scraped = {"full_name": "Jane Doe", "company_name": "Acme"}
        with patch(f"{_H}.get_linked_in_profile_by_url",
                   return_value=_profile_json()) as by_url, \
             patch(f"{_H}.get_element_wait_retry", return_value=None), \
             patch(f"{_H}.returnProfileInfo", return_value=scraped), \
             patch(f"{_H}.get_industries_of_profile_from_ai", return_value="Tech"), \
             patch(f"{_H}.add_linkedin_profile", return_value=True):
            data = get_linkedin_profile_from_url(driver, MagicMock(), url, force_refresh=True)
        by_url.assert_not_called()
        assert data["full_name"] == "Jane Doe"
        assert data["company_name"] == "Acme"
        assert data["industry"] == "Tech"

    def test_force_refresh_survives_the_redirect_recursion(self):
        """LinkedIn rewrites vanity URLs, so the walk re-enters itself on the resolved URL.

        If the flag did not ride along, the second hop would serve the cached row after all.
        """
        from cqc_lem.utilities.linkedin.helper import get_linkedin_profile_from_url
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/in/jane-redirected/"
        scraped = {"full_name": "Jane Doe"}
        with patch(f"{_H}.get_linked_in_profile_by_url",
                   return_value=_profile_json()) as by_url, \
             patch(f"{_H}.get_element_wait_retry", return_value=None), \
             patch(f"{_H}.returnProfileInfo", return_value=scraped), \
             patch(f"{_H}.get_industries_of_profile_from_ai", return_value="Tech"), \
             patch(f"{_H}.add_linkedin_profile", return_value=True), \
             patch(f"{_H}.time.sleep"):
            data = get_linkedin_profile_from_url(driver, MagicMock(),
                                                 "https://www.linkedin.com/in/old/",
                                                 force_refresh=True)
        by_url.assert_not_called()
        assert data["full_name"] == "Jane Doe"
        assert data["industry"] == "Tech"


class TestLoginToLinkedIn:
    def _driver_logged_in(self):
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/"
        driver.find_element.return_value = MagicMock(text="")
        driver.title = "Feed | LinkedIn"
        return driver

    def test_cookie_login_success_returns_true(self):
        from cqc_lem.utilities.linkedin.helper import login_to_linkedin
        driver = self._driver_logged_in()
        with patch(f"{_H}.get_cookies", return_value=[{"name": "li_at", "value": "x"}]), \
             patch(f"{_H}.load_cookies"), \
             patch(f"{_H}._persist_session_cookies", return_value=True) as persist, \
             patch(f"{_H}.clear_rate_limit"), \
             patch(f"{_H}._human_pause"), \
             patch(f"{_H}.time.sleep"):
            result = login_to_linkedin(driver, MagicMock(), "a@x.com", "pw")
        assert result is True
        persist.assert_called_once_with(driver, "a@x.com")

    def test_credential_login_success_returns_true(self):
        """The credential path must be the one that runs — the URL only becomes /feed on click.

        Setting `current_url` to the feed BEFORE the call short-circuits at the "Already logged in"
        branch, so the test would pass without ever reaching the credential return.
        """
        from cqc_lem.utilities.linkedin.helper import login_to_linkedin
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/login"
        driver.title = "Feed | LinkedIn"
        driver.find_element.return_value = MagicMock(text="")

        field = MagicMock()

        def _sign_in():
            driver.current_url = "https://www.linkedin.com/feed/"

        field.click.side_effect = _sign_in
        with patch(f"{_H}.get_cookies", return_value=None), \
             patch(f"{_H}.load_cookies"), \
             patch(f"{_H}._persist_session_cookies", return_value=True) as persist, \
             patch(f"{_H}.clear_rate_limit"), \
             patch(f"{_H}.get_visible_element_wait_retry", return_value=field) as fields, \
             patch(f"{_H}.EC.element_to_be_clickable"), \
             patch(f"{_H}._human_pause"), \
             patch(f"{_H}.time.sleep"):
            result = login_to_linkedin(driver, MagicMock(), "a@x.com", "pw")
        assert result is True
        # username field, password field, sign-in button — proof the credential flow ran
        assert fields.call_count == 3
        field.send_keys.assert_any_call("a@x.com")
        persist.assert_called_once_with(driver, "a@x.com")

    def test_failed_login_returns_false(self):
        from cqc_lem.utilities.linkedin.helper import login_to_linkedin
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/login"
        driver.title = "Feed | LinkedIn"
        driver.find_element.return_value = MagicMock(text="")
        with patch(f"{_H}.get_cookies", return_value=None), \
             patch(f"{_H}.load_cookies"), \
             patch(f"{_H}._persist_session_cookies") as persist, \
             patch(f"{_H}.clear_rate_limit"), \
             patch(f"{_H}.get_visible_element_wait_retry", return_value=MagicMock()), \
             patch(f"{_H}.EC.element_to_be_clickable"), \
             patch(f"{_H}._human_pause"), \
             patch(f"{_H}.time.sleep"), \
             patch(f"{_H}.log_error") as log_error:
            # Title wait succeeds but final URL is still the login page
            driver.current_url = "https://www.linkedin.com/login"
            result = login_to_linkedin(driver, MagicMock(), "a@x.com", "pw")
        assert result is False
        persist.assert_not_called()
        log_error.assert_called_once()

    def test_automation_pause_raises(self):
        from cqc_lem.utilities.linkedin.helper import LinkedInRateLimited, login_to_linkedin
        with patch(f"{_H}.is_automation_paused", return_value=True), \
             patch(f"{_H}.automation_pause_remaining", return_value=300):
            with pytest.raises(LinkedInRateLimited):
                login_to_linkedin(MagicMock(), MagicMock(), "a@x.com", "pw")


class TestDriveEmailPinChallenge:
    def test_returns_false_when_no_otp_input(self):
        from cqc_lem.utilities.linkedin.helper import drive_email_pin_challenge
        driver = MagicMock()
        driver.find_element.return_value = MagicMock(text="verification code sent")
        with patch(f"{_H}._find_visible_otp_input", return_value=None), \
             patch(f"{_H}._click_by_text", return_value=False), \
             patch(f"{_H}.time.sleep"):
            assert drive_email_pin_challenge(driver, "a@x.com", lambda u: False) is False

    def test_returns_false_when_user_unknown(self):
        from cqc_lem.utilities.linkedin.helper import drive_email_pin_challenge
        driver = MagicMock()
        driver.find_element.return_value = MagicMock(text="enter the code we sent")
        otp = MagicMock()
        with patch(f"{_H}._find_visible_otp_input", return_value=otp), \
             patch(f"{_H}.time.sleep"), \
             patch("cqc_lem.utilities.db.get_user_id", return_value=None):
            assert drive_email_pin_challenge(driver, "a@x.com", lambda u: False) is False

    def test_full_flow_submits_polled_pin(self, monkeypatch):
        from cqc_lem.utilities.linkedin.helper import drive_email_pin_challenge
        monkeypatch.setenv("LINKEDIN_PIN_WAIT_SECONDS", "10")
        driver = MagicMock()
        driver.find_element.return_value = MagicMock(text="enter the code we sent")
        checkbox = MagicMock()
        checkbox.is_displayed.return_value = True
        checkbox.is_selected.return_value = False
        driver.find_elements.return_value = [checkbox]
        otp = MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/"
        with patch(f"{_H}._find_visible_otp_input", return_value=otp), \
             patch(f"{_H}._click_by_text", return_value=True), \
             patch(f"{_H}.time.sleep"), \
             patch("cqc_lem.utilities.db.get_user_id", return_value=3), \
             patch("cqc_lem.utilities.linkedin.verification_pin.clear_pin") as clear, \
             patch("cqc_lem.utilities.linkedin.verification_pin.create_pin_request",
                   return_value="token123"), \
             patch("cqc_lem.utilities.linkedin.verification_pin.pin_reply_address",
                   return_value="pin+token123@x.com"), \
             patch("cqc_lem.utilities.linkedin.verification_pin.get_pin",
                   return_value="654321"), \
             patch("cqc_lem.utilities.email.send_login_pin_request_email"):
            result = drive_email_pin_challenge(driver, "a@x.com",
                                               lambda u: "/feed" in u)
        assert result is True
        otp.send_keys.assert_called_once_with("654321")
        checkbox.click.assert_called_once()  # "remember device" ticked
        assert clear.call_count == 2  # cleared before request and after use
