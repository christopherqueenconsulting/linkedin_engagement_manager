import os
import random
import time
from typing import Optional

from cqc_lem.utilities.ai.ai_helper import get_industries_of_profile_from_ai
from cqc_lem.utilities.db import get_cookies, store_cookies, get_linked_in_profile_by_email, add_linkedin_profile, \
    get_linked_in_profile_by_url, get_linked_in_profile_by_user_id
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited, clear_rate_limit, \
    mark_rate_limited, rate_limit_cooldown_remaining, is_automation_paused, automation_pause_remaining
from cqc_lem.utilities.linkedin.scrapper import returnProfileInfo
from cqc_lem.utilities.logger import myprint, log_warning, log_error
from cqc_lem.utilities.selenium_util import load_cookies, get_element_wait_retry, \
    get_visible_element_wait_retry, getText
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait


def _human_pause(min_seconds: float, max_seconds: float) -> None:
    """Sleep a random human-like interval to space out automated navigations. Set
    LINKEDIN_HUMANIZE_DELAYS=false (e.g. in tests) to make this a no-op."""
    if os.getenv("LINKEDIN_HUMANIZE_DELAYS", "true").lower() == "false":
        return
    time.sleep(random.uniform(min_seconds, max_seconds))


def _text_is_transport_error(body: str) -> bool:
    """Chrome network/proxy error interstitial ("This site can't be reached", ERR_* codes) — a
    transport failure (flaky residential proxy, DNS, timeout), NOT a LinkedIn throttle. Kept separate
    so a proxy blip doesn't trip the shared 429 breaker and pause all automation."""
    low = (body or "").lower()
    return ("this site can’t be reached" in low or "this site can't be reached" in low
            or "err_" in low)


def _text_is_rate_limited(body: str) -> bool:
    """True when the rendered page text is a LinkedIn rate-limit response (HTTP 429, or LinkedIn's
    custom 999 anti-bot status). Deliberately does NOT fire on the bare "This page isn't working"
    shell alone — Chrome renders that identical shell for 500/502/503 and for proxy/network errors,
    so keying off it indiscriminately trips the breaker on transient failures."""
    if _text_is_transport_error(body):
        return False
    low = (body or "").lower()
    if "too many requests" in low or "http error 429" in low or "http error 999" in low:
        return True
    # Tiny HTTP-error shell: only a rate-limit status code (429 / LinkedIn's 999) counts.
    if len(body or "") < 200 and ("this page isn’t working" in low or "this page isn't working" in low):
        return "429" in low or "999" in low
    return False


def solve_arkose_challenge(driver: WebDriver, wait: WebDriverWait) -> bool:
    """Attempt to solve an Arkose Labs (FunCaptcha) challenge on the current page.

    Returns True if the challenge was solved and the browser moved past it.
    Returns False for unsolvable challenge types (email/phone verification) or
    when CAPSOLVER_API_KEY is not configured — caller should abort gracefully.
    """
    api_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
    if not api_key:
        log_warning(
            "CAPSOLVER_API_KEY not set — cannot auto-solve LinkedIn challenge",
            action_type="login",
        )
        return False

    # Detect Arkose Labs FunCaptcha iframe on the page.
    # Parse the hostname rather than substring-match to avoid matching attacker-controlled
    # URLs that embed "arkoselabs.com" in a path or query string.
    def _is_arkose_src(src: str) -> bool:
        try:
            from urllib.parse import urlparse as _urlparse
            host = _urlparse(src).netloc
            return host == "arkoselabs.com" or host.endswith(".arkoselabs.com")
        except Exception:
            return False

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    arkose_frame = next(
        (f for f in iframes if _is_arkose_src(f.get_attribute("src") or "")),
        None,
    )
    if arkose_frame is None:
        log_warning(
            "LinkedIn challenge is not an Arkose Labs FunCaptcha — cannot auto-solve",
            action_type="login",
            error_message=f"Challenge URL: {driver.current_url}",
        )
        return False

    try:
        import capsolver  # type: ignore[import-untyped]

        capsolver.api_key = api_key

        # Extract publicKey and surl from the iframe src
        src = arkose_frame.get_attribute("src") or ""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(src)
        qs = parse_qs(parsed.query)
        public_key = qs.get("pk", qs.get("public_key", [""]))[0]
        surl = f"{parsed.scheme}://{parsed.netloc}/"

        if not public_key:
            # Try extracting from page source as fallback
            page = driver.page_source
            import re
            m = re.search(r'"public_key"\s*:\s*"([^"]+)"', page)
            public_key = m.group(1) if m else ""

        if not public_key:
            log_warning(
                "Could not extract Arkose publicKey from page — skipping CAPTCHA solve",
                action_type="login",
            )
            return False

        solution = capsolver.solve({
            "type": "FunCaptchaTask",
            "websiteURL": driver.current_url,
            "websitePublicKey": public_key,
            "funcaptchaApiJSSubdomain": surl,
        })

        token = solution.get("token", "")
        if not token:
            log_warning("CapSolver returned empty token for FunCaptcha", action_type="login")
            return False

        # Inject the token and submit
        driver.execute_script(
            "document.getElementById('FunCaptcha-Token') && "
            "(document.getElementById('FunCaptcha-Token').value = arguments[0]);",
            token,
        )
        driver.execute_script(
            "var el = document.querySelector('[name=\"fc-token\"]');"
            "if (el) el.value = arguments[0];",
            token,
        )
        # Trigger form submission if a submit button is present
        try:
            submit = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
            submit.click()
            time.sleep(2)
        except Exception:
            pass  # No submit button on this challenge page — token injection alone is sufficient

        myprint("Arkose FunCaptcha solved via CapSolver")
        return True

    except Exception as e:
        log_warning(
            "CapSolver CAPTCHA solve failed",
            exc=e,
            action_type="login",
        )
        return False


def _click_by_text(driver, substrings) -> bool:
    substrings = [s.lower() for s in substrings]
    for el in driver.find_elements(By.XPATH, "//button|//a|//*[@role='button']"):
        try:
            text = (el.text or "").strip().lower()
        except WebDriverException:
            continue
        if text and any(s in text for s in substrings):
            try:
                el.click()
            except WebDriverException:
                try:
                    driver.execute_script("arguments[0].click();", el)
                except WebDriverException:
                    continue
            return True
    return False


def _find_visible_otp_input(driver):
    for sel in ("input[name='pin']", "input[autocomplete='one-time-code']",
                "#input__email_verification_pin", "input[inputmode='numeric']",
                "input[type='tel']", "input[maxlength='6']"):
        vis = [e for e in driver.find_elements(By.CSS_SELECTOR, sel) if e.is_displayed()]
        if vis:
            return vis[0]
    texts = [e for e in driver.find_elements(By.CSS_SELECTOR, "input[type='text']") if e.is_displayed()]
    return texts[0] if len(texts) == 1 else None


def drive_email_pin_challenge(driver, user_email: str, is_logged_in) -> bool:
    """Clear a LinkedIn login challenge via the email verification-code path.

    LinkedIn's mobile-app "tap Yes" approval is unreliable, so this drives the more
    dependable email-code flow: navigate to the code-entry screen (which makes LinkedIn
    email a 6-digit code), email the user asking them to REPLY with it, then poll Redis
    (populated by the inbound-parse webhook) for the code and submit it. Returns True if
    login completes. Best-effort: any failure returns False so the caller can fall back.
    """
    from cqc_lem.utilities.db import get_user_id
    from cqc_lem.utilities.linkedin.verification_pin import (
        clear_pin, create_pin_request, get_pin, pin_reply_address)

    otp = None
    for _ in range(5):
        try:
            body = driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            body = ""
        otp = _find_visible_otp_input(driver)
        if otp is not None and any(k in body for k in ("code", "verif", "sent")):
            break
        moved = (_click_by_text(driver, ["access to this device"])
                 or _click_by_text(driver, ["verify using email", "use email"])
                 or _click_by_text(driver, ["email"])
                 or _click_by_text(driver, ["send code", "continue", "next", "verify"]))
        time.sleep(3)
        if not moved:
            break
    otp = _find_visible_otp_input(driver)
    if otp is None:
        return False

    user_id = get_user_id(user_email)
    if not user_id:
        return False
    clear_pin(user_id)
    token = create_pin_request(user_id)
    try:
        from cqc_lem.utilities.email import send_login_pin_request_email
        send_login_pin_request_email(user_email, pin_reply_address(token))
    except Exception as e:
        log_warning("Failed to send verification-PIN request email", exc=e, action_type="login")

    try:
        wait_secs = int(os.getenv("LINKEDIN_PIN_WAIT_SECONDS", "300"))
    except ValueError:
        wait_secs = 300
    log_warning("LinkedIn email verification required — emailed the user to REPLY with the "
                f"6-digit code; waiting up to {wait_secs}s.", action_type="login")
    pin = None
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        time.sleep(5)
        pin = get_pin(user_id)
        if pin:
            break
    if not pin:
        return False

    # Remember this device so future logins from the same (sticky) IP skip the challenge.
    for cb in driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
        try:
            if cb.is_displayed() and not cb.is_selected():
                cb.click()
        except WebDriverException:
            pass
    otp = _find_visible_otp_input(driver) or otp
    try:
        otp.clear()
        otp.send_keys(pin)
    except WebDriverException:
        return False
    clear_pin(user_id)
    _click_by_text(driver, ["submit", "verify", "next", "done", "confirm", "agree"])
    time.sleep(5)
    if is_logged_in(driver.current_url):
        return True
    time.sleep(3)
    return is_logged_in(driver.current_url)


def login_to_linkedin(driver: WebDriver, wait: WebDriverWait, user_email: str, user_password: str):
    linked_url = "https://www.linkedin.com"
    feed_url = "https://www.linkedin.com/feed/"
    login_url = "https://www.linkedin.com/login"

    _CHALLENGE_PATHS = (
        '/checkpoint/', '/authwall', '/uas/login', '/challenge/',
        '/captcha/', '/security-verification', '/error',
    )
    # LinkedIn can land on /home, /feed, /mynetwork, etc. when logged in
    _LOGGED_IN_PATHS = ('/feed', '/home', '/mynetwork', '/jobs', '/messaging',
                        '/notifications', '/in/', '/search')

    def _is_challenge_url(url: str) -> bool:
        return any(p in url for p in _CHALLENGE_PATHS)

    def _is_logged_in(url: str) -> bool:
        return any(p in url for p in _LOGGED_IN_PATHS)

    def _page_body(drv) -> str:
        try:
            return drv.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return ""

    def _page_is_transport_error(drv) -> bool:
        return _text_is_transport_error(_page_body(drv))

    def _page_is_rate_limited(drv) -> bool:
        return _text_is_rate_limited(_page_body(drv))

    def _page_is_redirect_loop(drv) -> bool:
        """Stored cookies minted from a different egress IP (e.g. after switching to a
        residential proxy) can make LinkedIn loop on re-auth, serving a browser error
        page ("redirected you too many times / ERR_TOO_MANY_REDIRECTS"). The URL still
        looks logged-in (/feed), so detect it from the body to avoid mistaking it for a
        live session — the fix is to drop the cookies and do a fresh credential login."""
        try:
            body = drv.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return False
        low = body.lower()
        return "err_too_many_redirects" in low or "redirected you too many times" in low

    def _wait_for_manual_approval() -> bool:
        """Poll for a device-approval / 2FA checkpoint to clear.

        LinkedIn's "Check your LinkedIn app — tap Yes" challenge cannot be solved
        programmatically; it needs a one-time tap in the user's mobile app (the
        "Recognize this device" box is pre-checked, so cookies persist afterward).
        When someone is watching the session over VNC they can approve in real time —
        give them a configurable window before giving up.
        """
        timeout = int(os.getenv("LINKEDIN_APPROVAL_WAIT_SECONDS", "120"))
        log_warning(
            "LinkedIn device-approval required — open your LinkedIn mobile app and tap "
            "'Yes' to confirm this sign-in (watch via VNC). Waiting up to "
            f"{timeout}s for approval.",
            action_type="login",
        )
        # Notify the user immediately (high priority) so a human can approve instead of
        # the run silently stalling. Best-effort — never let email problems break login.
        if os.getenv("LINKEDIN_APPROVAL_EMAIL_ENABLED", "true").lower() != "false":
            try:
                from cqc_lem.utilities.email import send_login_approval_email
                vnc_url = os.getenv(
                    "LEMVNC_URL",
                    "https://lemvnc.christopherqueenconsulting.com/?autoconnect=1&password=secret",
                )
                send_login_approval_email(user_email, vnc_url=vnc_url)
            except Exception as e:
                log_warning("Failed to send login-approval email", exc=e, action_type="login")
        if timeout <= 0:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            current = driver.current_url
            if _is_logged_in(current):
                myprint("Device-approval confirmed — login proceeding")
                return True
            if not _is_challenge_url(current):
                # Left the checkpoint; give the post-approval redirect a moment to settle
                time.sleep(3)
                if _is_logged_in(driver.current_url):
                    myprint("Device-approval confirmed — login proceeding")
                    return True
        return False

    def _handle_challenge(label: str) -> None:
        """Clear a login challenge or raise if it can't be cleared.

        Order: try the automated Arkose/FunCaptcha solver, then fall back to
        waiting for a manual mobile-app device approval.
        """
        if solve_arkose_challenge(driver, wait):
            myprint(f"CAPTCHA solved at {label} — continuing login")
            time.sleep(2)
            return
        # Prefer the email verification-code path — the mobile-app "tap Yes" approval is
        # unreliable (often never prompts). Fall back to waiting for a manual approval.
        if os.getenv("LINKEDIN_EMAIL_PIN_ENABLED", "true").lower() != "false":
            if drive_email_pin_challenge(driver, user_email, _is_logged_in):
                myprint(f"Email verification-code cleared challenge at {label}")
                return
        if _wait_for_manual_approval():
            return
        raise RuntimeError(f"Unsolvable LinkedIn challenge at {label}: {driver.current_url}")

    # Manual global pause: a kill-switch to let a rate-limited account recover. Central gate —
    # every Selenium task logs in through here, so a pause halts all of them without navigating.
    if is_automation_paused():
        raise LinkedInRateLimited(
            f"Automation is paused for ~{automation_pause_remaining()}s — skipping login.")

    # Shared 429 circuit breaker: if a recent task already hit LinkedIn's rate limit,
    # don't navigate at all — re-hitting the feed while throttled prolongs the block.
    cooldown = rate_limit_cooldown_remaining()
    if cooldown > 0:
        raise LinkedInRateLimited(
            f"LinkedIn 429 circuit breaker open — skipping login for ~{cooldown}s. "
            "Reduce automation frequency and retry later.")

    # Load base domain first so cookies can be set against the right origin
    driver.get(linked_url)

    cookies = get_cookies(linked_url, user_email)

    if cookies:
        myprint("Found previous cookies. Loading them now!")
        load_cookies(driver, cookies)
        # Human-like jitter before hitting the feed. Rapid, perfectly-timed base→feed navigations
        # from a headless browser are an easy automation tell; a short randomized settle lets the
        # base page finish and spaces requests out. Tunable/zeroable via env for tests.
        _human_pause(1.5, 4.0)
        # Navigate directly to the feed — if cookies are valid LinkedIn serves it;
        # if invalid/expired it redirects to a login or challenge page
        driver.get(feed_url)
    else:
        myprint("No previous cookies found.")

    # Wait for the redirect / page load to settle
    time.sleep(2)

    if _is_challenge_url(driver.current_url):
        _handle_challenge("post-cookie-load")

    # Stored cookies minted from a different egress IP (e.g. after switching to a
    # residential proxy) can make LinkedIn loop on re-auth, landing on a redirect-error
    # page that still sits on /feed. Handle this BEFORE the 429 check — the redirect
    # page also says "this page isn't working", which the rate-limit heuristic would
    # otherwise misclassify as a 429. Drop the cookies and do a fresh credential login.
    if cookies and _is_logged_in(driver.current_url) and _page_is_redirect_loop(driver):
        myprint("Stored session unusable (redirect loop) — clearing cookies and re-authenticating")
        driver.delete_all_cookies()
        cookies = None
        driver.get(login_url)
        time.sleep(1)

    # LinkedIn serves a "HTTP ERROR 429 / This page isn't working" body at the SAME
    # /feed/ URL when the account/IP is rate-limited. A naive URL check would treat
    # that as "logged in" and downstream profile scraping would crash. Detect it and
    # raise a transient error so callers back off instead of hammering.
    if _is_logged_in(driver.current_url) and _page_is_rate_limited(driver):
        mark_rate_limited("429 at feed after cookie load")
        raise LinkedInRateLimited(
            "LinkedIn is rate-limiting this session (HTTP 429). Backing off — "
            "reduce automation frequency and retry later.")

    # A Chrome transport error (proxy blip / DNS / timeout) also sits on the /feed URL, so it would
    # otherwise be mistaken for a live session below — clearing the breaker and storing junk cookies.
    # Raise a transient error WITHOUT tripping the 429 breaker so the task simply retries later.
    if _is_logged_in(driver.current_url) and _page_is_transport_error(driver):
        raise LinkedInRateLimited(
            "Feed did not load (proxy/network error) — transient, retrying later without "
            "tripping the rate-limit breaker.")

    if _is_logged_in(driver.current_url):
        myprint(f"Already logged in! (current URL: {driver.current_url})")
        clear_rate_limit()
        store_cookies(user_email, driver.get_cookies())
        myprint("Cookies stored to DB!")
        return

    # We had a stored session cookie but it didn't authenticate — it's stale/expired.
    # Auto-detect this and email the user to reconnect (preferred over password login).
    if cookies:
        try:
            from cqc_lem.utilities.db import get_user_id
            from cqc_lem.utilities.notifications import notify_linkedin_session
            uid = get_user_id(user_email)
            if uid:
                notify_linkedin_session(uid, revalidation=True)
        except Exception as e:
            log_warning("Failed to send session re-validation email", exc=e, action_type="login")

    # Cookies missing or expired — do a full credential login
    myprint(f"Logging in to LinkedIn as: {user_email}")

    # Go directly to the login page instead of clicking a "Sign in" link
    driver.get(login_url)
    time.sleep(1)

    if _is_challenge_url(driver.current_url):
        _handle_challenge("login-page")

    # LinkedIn's redesigned login page no longer uses stable id="username"/id="password"
    # or a [type=submit] button — fields now carry ephemeral React ids and the sign-in
    # control is a <button type="button"><span>Sign in</span></button>. Match on stable
    # type/autocomplete attributes (and visible text for the button), keeping the legacy
    # selectors as fallbacks for any A/B variant still serving the old form. The page also
    # renders duplicate hidden+visible copies, so we must select the displayed one.
    username_field = get_visible_element_wait_retry(
        driver, wait,
        [(By.CSS_SELECTOR, "input[autocomplete~='username']"),
         (By.CSS_SELECTOR, "input[name='session_key']"),
         (By.ID, "username"),
         (By.CSS_SELECTOR, "input[type='email']")],
        "Finding Username Field")
    password_field = get_visible_element_wait_retry(
        driver, wait,
        [(By.CSS_SELECTOR, "input[autocomplete~='current-password']"),
         (By.CSS_SELECTOR, "input[name='session_password']"),
         (By.ID, "password"),
         (By.CSS_SELECTOR, "input[type='password']")],
        "Finding Password Field")

    username_field.clear()
    username_field.send_keys(user_email)
    password_field.clear()
    password_field.send_keys(user_password)

    sign_in_button = get_visible_element_wait_retry(
        driver, wait,
        [(By.XPATH, "//button[normalize-space()='Sign in']"),
         (By.CSS_SELECTOR, "button[type='submit']"),
         (By.CSS_SELECTOR, "button[aria-label='Sign in']"),
         (By.XPATH, "//*[@type='submit']")],
        "Finding Sign in Button")
    wait.until(EC.element_to_be_clickable(sign_in_button))
    try:
        sign_in_button.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", sign_in_button)

    # Allow the post-submit redirect to settle, then check for security challenges
    # before waiting for the feed (avoids TimeoutException on 2FA/CAPTCHA pages)
    time.sleep(2)
    if _is_challenge_url(driver.current_url):
        _handle_challenge("post-submit")

    wait.until(EC.title_contains("Feed"), "Waiting for Feed to load after login")

    if _is_logged_in(driver.current_url):
        myprint("Login successful!")
        clear_rate_limit()
        store_cookies(user_email, driver.get_cookies())
        myprint("Cookies stored to DB!")
    else:
        myprint("Login failed. Check your credentials.")


def get_my_profile(driver, wait, user_email: str, user_password: str, user_id: Optional[int] = None) -> LinkedInProfile:
    profile = None

    # Prefer user_id-based cache key; fall back to email for backward compat.
    if user_id is not None:
        profile_json = get_linked_in_profile_by_user_id(user_id)
    else:
        profile_json = get_linked_in_profile_by_email(user_email)

    if profile_json is None:
        myprint(f"Previous Profile not found (or stale) in DB: {user_email}")
        login_to_linkedin(driver, wait, user_email, user_password)

        profile_url = "https://www.linkedin.com/in/"
        driver.get(profile_url)  # Need the page to redirect
        time.sleep(2)
        profile_url = driver.current_url  # Get the updated url

        profile_data = get_linkedin_profile_from_url(driver, wait, profile_url, True)

        if profile_data:

            profile = LinkedInProfile(**profile_data)
            # Add email and password to profile for later use
            profile.email = user_email
            profile.password = user_password

            # Add profile to DB for faster future retrieval
            if add_linkedin_profile(profile, user_id=user_id):
                myprint(f"Profile saved to DB: {profile.full_name}")
            else:
                myprint(f"Failed to save profile to DB: {profile.full_name}")
        else:
            myprint("Failed to get my profile data")
    else:
        # Ensure profile_json is a string
        profile_json_str = profile_json[0] if isinstance(profile_json, tuple) else profile_json
        # Create a LinkedInProfile object from json string data
        profile = LinkedInProfile.model_validate_json(profile_json_str)
        myprint(f"Profile Restored from DB: {profile.full_name}")

    return profile


def load_profile_for_user(user_id: int) -> "LinkedInProfile | None":
    """Best-effort load of a cached LinkedInProfile for a user.

    Returns None when there's no cached profile or the stored JSON can't be parsed —
    callers use it only to enrich prompts, so missing data must never raise.
    """
    try:
        raw = get_linked_in_profile_by_user_id(user_id)
    except Exception as e:
        myprint(f"load_profile_for_user: lookup failed for user_id={user_id}: {e}")
        return None
    if not raw:
        return None
    profile_json_str = raw[0] if isinstance(raw, tuple) else raw
    try:
        return LinkedInProfile.model_validate_json(profile_json_str)
    except Exception as e:
        myprint(f"load_profile_for_user: could not parse profile for user_id={user_id}: {e}")
        return None


def get_linkedin_profile_from_url(driver, wait, profile_url, is_main_user=False, force_save=False):
    # Get the profile from the DB if it exists
    profile_json = get_linked_in_profile_by_url(profile_url)

    if profile_json is None:

        # Set to empty dictionary
        profile_data = {}

        if profile_url != driver.current_url:
            # Open the profile URL
            driver.get(profile_url)
            time.sleep(2)

            # Check if current url changes (redirects)
            if profile_url != driver.current_url:
                # Use the current url as the profile url
                profile_url = driver.current_url

                # Get the profile using the new url
                return get_linkedin_profile_from_url(driver, wait, profile_url, is_main_user)

        # Get the company name
        company_element = get_element_wait_retry(driver, wait, '//button[contains(@aria-label,"Current company")]',
                                                 "Finding Company Name", element_always_expected=False)

        company_name = None
        if company_element:
            company_name = getText(company_element)

        profile_data = returnProfileInfo(driver, profile_url, company_name, is_main_user)

        if profile_data:
            profile = LinkedInProfile(**profile_data)
            # Use AI to determine industry of the profile
            industry = get_industries_of_profile_from_ai(profile, 1)
            profile.industry = industry

            # Save the profile to the DB
            if add_linkedin_profile(profile):
                myprint(f"Profile saved to DB: {profile.full_name}")

        # Use json to output to string
        # myprint(json.dumps(profile_data, indent=4))

    else:
        # Ensure profile_json is a string
        profile_json_str = profile_json[0] if isinstance(profile_json, tuple) else profile_json
        # Create a LinkedInProfile object from json string data
        profile = LinkedInProfile.model_validate_json(profile_json_str)
        profile_data = profile.model_dump()
        myprint(f"Profile Restored from DB: {profile.full_name}")

    return profile_data
