"""The LinkedIn session: getting a Selenium driver signed in, and the cached profile behind it.

`login_to_linkedin` is the ONE door every Selenium task goes through, which is why the safety gates
live at the top of it rather than at each call site: the manual/deploy/suppression pause and the
shared 429 breaker both refuse here, before a single navigation, because probing LinkedIn while
throttled is what prolongs the throttle. Cookies are the preferred credential (`li_at` since #745)
and a password login is the fallback; `_persist_session_cookies` is where both paths meet, so it is
also where a sign-in gets recorded (issue #933).

The rest of the module is the scraped-text hygiene that keeps bad data out of the DB — a body that
says "HTTP ERROR 429" or "redirected you too many times" still sits on a `/feed` URL, so being
logged in is never decided from the URL alone, and `clean_person_name` strips the badge text SDUI
renders inside the same anchor as a person's name (issue #623) before it can reach a connection note.
"""

import os
import random
import re
import time
from typing import Optional

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from cqc_lem.utilities.ai.ai_helper import get_industries_of_profile_from_ai
from cqc_lem.utilities.db import (
    add_linkedin_profile,
    get_cookies,
    get_linked_in_profile_by_email,
    get_linked_in_profile_by_url,
    get_linked_in_profile_by_user_id,
    store_cookies,
)
from cqc_lem.utilities.linkedin.login_status import mark_approval_pending, mark_approval_timed_out, mark_signed_in
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.linkedin.rate_limit import (
    LinkedInRateLimited,
    automation_pause_remaining,
    clear_rate_limit,
    is_automation_paused,
    is_measurement_paused,
    mark_rate_limited,
    rate_limit_cooldown_remaining,
)
from cqc_lem.utilities.linkedin.scrapper import returnProfileInfo
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.profile_skills_window import record_profile_skills_change
from cqc_lem.utilities.selenium_util import (
    get_element_wait_retry,
    get_visible_element_wait_retry,
    getText,
    load_cookies,
)


def _human_pause(min_seconds: float, max_seconds: float) -> None:
    """Sleep a random human-like interval to space out automated navigations. Set
    LINKEDIN_HUMANIZE_DELAYS=false (e.g. in tests) to make this a no-op.
    """
    if os.getenv("LINKEDIN_HUMANIZE_DELAYS", "true").lower() == "false":
        return
    time.sleep(random.uniform(min_seconds, max_seconds))


# --- Scraped display-name hygiene (issue #623) --------------------------------------------------
# LinkedIn's SDUI renders badge and status text INSIDE the same <a> as the person's name, so a naive
# `link.text` read stores things like "Harshal Karanpuriya Verified Profile 1st" as somebody's name.
# That name then goes out in a connection note and poisons every name-keyed dedup we have. Every
# place we scrape a person's name runs it through clean_person_name(); connection_degree() reads the
# badge we just stripped, so callers can skip people we are ALREADY connected to instead of burning
# an invite slot on them.

# aria-labels wrap the name in a sentence ("View Jane Doe's profile") — the name is inside it.
_NAME_FROM_ARIA_RE = re.compile(r"view\s+(.+?)['’]s\s+(?:profile|graphic)", re.IGNORECASE)
_DEGREE_RE = re.compile(r"(?<!\w)(1st|2nd|3rd\+?)(?!\w)", re.IGNORECASE)
# Badge / status / degree text LinkedIn renders alongside the name. Deliberately does NOT include
# button words (Connect / Message / Follow): those live in sibling elements, and stripping them
# would mangle a real name that happens to contain one.
_NAME_NOISE_RE = re.compile(
    r"(?:\bview\s+.+?['’]s\s+(?:profile|graphic)\b"
    r"|\b(?:1st|2nd|3rd)\+?(?:\s*degree)?(?:\s*connection)?\b"
    r"|\bverified\s+profile\b|\bverified\b|\bpremium(?:\s+member)?\b|\binfluencer\b"
    r"|\bopen\s+to\s+work\b|\b(?:is\s+)?hiring\b"
    r"|\bstatus\s+is\s+(?:online|offline|reachable)\b)",
    re.IGNORECASE)
# SDUI stacks name / badge / degree as separate lines or bullet-separated spans.
_NAME_SEGMENT_RE = re.compile(r"[\n\r•·|]+")


def _collapse_repeated_name(name: str) -> str:
    """LinkedIn renders the name twice (visible + screen-reader copy) inside one link, which
    `.text` flattens to "Jane Doe Jane Doe". Fold an exact doubling back to one.
    """
    parts = name.split()
    half = len(parts) // 2
    if half and len(parts) % 2 == 0 and parts[:half] == parts[half:]:
        return " ".join(parts[:half])
    return name


def clean_person_name(raw: str) -> str:
    """A scraped display name with LinkedIn's badge/status/degree text removed. Returns '' when
    nothing name-like survives (a link whose text was only a status badge).
    """
    text = str(raw or "").replace("\u00a0", " ")
    aria = _NAME_FROM_ARIA_RE.search(text)
    if aria:
        text = aria.group(1)
    for segment in _NAME_SEGMENT_RE.split(text):
        candidate = " ".join(_NAME_NOISE_RE.sub(" ", segment).split()).strip(" ,-–—")
        if candidate:
            return _collapse_repeated_name(candidate)[:255]
    return ""


def connection_degree(raw: str) -> Optional[str]:
    """The connection-degree badge ('1st' / '2nd' / '3rd+') carried in scraped name text, or None
    when LinkedIn didn't render one (it omits the badge on your own card and on some SDUI surfaces).
    """
    match = _DEGREE_RE.search(str(raw or ""))
    if not match:
        return None
    degree = match.group(1).lower()
    return "3rd+" if degree.startswith("3rd") else degree


def is_first_degree(raw: str) -> bool:
    """True only when the badge SAYS 1st. A missing badge is unknown, not 'not connected' — callers
    that skip on this must fail open, or a selector drift silently stops all outreach.
    """
    return connection_degree(raw) == "1st"


def can_open_dm_thread(raw: str) -> bool:
    """Can we start a NEW DM thread with the person this badge belongs to?

    Opening one needs a 1st-degree connection — `send_dm_now` addresses a compose URL and LinkedIn
    simply will not accept a message to a 2nd/3rd+ person without InMail, so a draft queued for one
    is un-sendable the moment it is approved (issue #1528). Continuing a thread that is ALREADY open
    is a different question and is not gated here: they replied to us, so the thread exists whatever
    the badge says.

    False only when the badge SAYS a degree we cannot message. A missing badge is UNKNOWN, not "not
    connected" (see `is_first_degree`), so this fails OPEN — a selector drift must never silently
    stop every delivery.
    """
    degree = connection_degree(raw)
    return degree is None or degree == "1st"


def _text_is_transport_error(body: str) -> bool:
    """Chrome network/proxy error interstitial ("This site can't be reached", ERR_* codes) — a
    transport failure (flaky residential proxy, DNS, timeout), NOT a LinkedIn throttle. Kept separate
    so a proxy blip doesn't trip the shared 429 breaker and pause all automation.
    """
    low = (body or "").lower()
    return ("this site can’t be reached" in low or "this site can't be reached" in low
            or "err_" in low)


def _text_is_rate_limited(body: str) -> bool:
    """True when the rendered page text is a LinkedIn rate-limit response (HTTP 429, or LinkedIn's
    custom 999 anti-bot status). Deliberately does NOT fire on the bare "This page isn't working"
    shell alone — Chrome renders that identical shell for 500/502/503 and for proxy/network errors,
    so keying off it indiscriminately trips the breaker on transient failures.
    """
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
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(src)
        qs = parse_qs(parsed.query)
        public_key = qs.get("pk", qs.get("public_key", [""]))[0]
        surl = f"{parsed.scheme}://{parsed.netloc}/"

        if not public_key:
            # Try extracting from page source as fallback
            page = driver.page_source
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

        log_info("Arkose FunCaptcha solved via CapSolver")
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
    from cqc_lem.utilities.linkedin.verification_pin import clear_pin, create_pin_request, get_pin, pin_reply_address

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
        except WebDriverException as e:
            log_debug("Could not click remember-device checkbox", exc=e)
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


def _user_id_for_email(user_email: str) -> Optional[int]:
    """Best-effort id lookup for the status writes below — a failed lookup only costs the SPA a
    status line, so it can never raise into the login path.
    """
    try:
        from cqc_lem.utilities.db import get_user_id
        return get_user_id(user_email)
    except Exception as e:
        log_debug(f"Could not resolve user_id for LinkedIn login status: {e}", action_type="login")
        return None


def _persist_session_cookies(driver: WebDriver, user_email: str) -> bool:
    """Store the freshly authenticated session and report honestly whether it landed.

    `store_cookies` swallows per-row driver errors and refuses a write it cannot bind to a user
    (issue #745), so a lost write is invisible here unless we look. The consequence is delayed and
    silent: this run works, but the next one finds no li_at, falls back to a password login and
    trips LinkedIn's new-device challenge — while the log said the cookies were saved.
    """
    # Both success paths of `login_to_linkedin` meet here, so this is the one place that knows a
    # sign-in actually completed — and therefore that any device approval the user was asked for
    # was received (issue #933). Recorded before the cookie write is judged: the approval landed
    # whether or not the cookies persisted.
    uid = _user_id_for_email(user_email)
    if uid:
        mark_signed_in(uid)
    stored = store_cookies(user_email, driver.get_cookies())
    if not stored:
        log_error("Could not persist LinkedIn session cookies — the next run will have no li_at "
                  "and will fall back to a password login", action_type="login")
        return False
    log_info("Cookies stored to DB!")
    return True


def login_to_linkedin(driver: WebDriver, wait: WebDriverWait, user_email: str, user_password: str,
                      measurement_only: bool = False) -> bool:
    """Sign `driver` in — stored cookies first, a credential login only if they no longer authenticate.

    The central gate for all Selenium work. Both back-off checks run BEFORE any navigation, because
    the whole point of the 429 breaker is not to touch LinkedIn while throttled. `measurement_only`
    marks the read-only lanes (post stats, follower capture): they still stop for every pause EXCEPT
    the suppression tripwire's own, since freezing the very scrape that would show a recovery makes
    the tripwire self-perpetuating (issue #629).

    Being signed in is never decided from the URL alone. A 429 body, a Chrome transport error and a
    cookie/egress-IP redirect loop all sit on `/feed`, and only the first is a real throttle — the
    other two drop the cookies (or raise transiently) rather than tripping the breaker on a network
    blip. A 429 WITH stored cookies is treated as a stale-cookie mismatch and re-authenticated fresh;
    only a 429 with nothing left to drop trips the breaker.

    Returns:
        bool: True when the session is authenticated and cookies were persisted; False only when
            the credential flow completed without reaching a signed-in state. Every other failure
            path raises, so a False return is unambiguous.

    Raises:
        LinkedInRateLimited: automation paused, breaker open, a genuine 429, or a transient
            proxy/network failure that must NOT trip the breaker.
        RuntimeError: a challenge that neither the CAPTCHA solver, the email PIN flow nor a manual
            mobile approval could clear.
    """
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
        live session — the fix is to drop the cookies and do a fresh credential login.
        """
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
        # Publish the ask to the SPA as well as the inbox (issue #933) — the email was the only
        # place this challenge was ever visible, so a user who had already approved had nowhere
        # in the product to confirm it.
        uid = _user_id_for_email(user_email)
        if uid:
            mark_approval_pending(uid)
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

        def _approved() -> bool:
            # Close the pending record HERE, not only at the cookie persist further down: the
            # approval has landed, and a login that dies in between (feed never loads, a 429 on the
            # way in) would otherwise leave the Account page still asking a user who already tapped
            # 'Yes' to go and tap it — the exact blind spot #933 is about.
            if uid:
                mark_signed_in(uid)
            return True

        def _gave_up() -> bool:
            # The pending record self-expires, but only after the SPA has spent minutes telling the
            # user we are still waiting. Close it out explicitly so the Account page says what
            # actually happened: nobody approved in time and the next run will ask again.
            if uid:
                mark_approval_timed_out(uid)
            return False

        if timeout <= 0:
            return _gave_up()
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            current = driver.current_url
            if _is_logged_in(current):
                log_info("Device-approval confirmed — login proceeding")
                return _approved()
            if not _is_challenge_url(current):
                # Left the checkpoint; give the post-approval redirect a moment to settle
                time.sleep(3)
                if _is_logged_in(driver.current_url):
                    log_info("Device-approval confirmed — login proceeding")
                    return _approved()
        return _gave_up()

    def _handle_challenge(label: str) -> None:
        """Clear a login challenge or raise if it can't be cleared.

        Order: try the automated Arkose/FunCaptcha solver, then fall back to
        waiting for a manual mobile-app device approval.
        """
        if solve_arkose_challenge(driver, wait):
            log_info(f"CAPTCHA solved at {label} — continuing login")
            time.sleep(2)
            return
        # Prefer the email verification-code path — the mobile-app "tap Yes" approval is
        # unreliable (often never prompts). Fall back to waiting for a manual approval.
        if os.getenv("LINKEDIN_EMAIL_PIN_ENABLED", "true").lower() != "false":
            if drive_email_pin_challenge(driver, user_email, _is_logged_in):
                log_info(f"Email verification-code cleared challenge at {label}")
                return
        if _wait_for_manual_approval():
            return
        raise RuntimeError(f"Unsolvable LinkedIn challenge at {label}: {driver.current_url}")

    # Manual global pause: a kill-switch to let a rate-limited account recover. Central gate —
    # every Selenium task logs in through here, so a pause halts all of them without navigating.
    # `measurement_only` callers (read-only stat capture) still stop for every pause EXCEPT the
    # suppression tripwire's own — that one has to keep measuring or recovery is never visible.
    if is_measurement_paused() if measurement_only else is_automation_paused():
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
        log_info("Found previous cookies. Loading them now!")
        load_cookies(driver, cookies)
        # Human-like jitter before hitting the feed. Rapid, perfectly-timed base→feed navigations
        # from a headless browser are an easy automation tell; a short randomized settle lets the
        # base page finish and spaces requests out. Tunable/zeroable via env for tests.
        _human_pause(1.5, 4.0)
        # Navigate directly to the feed — if cookies are valid LinkedIn serves it;
        # if invalid/expired it redirects to a login or challenge page
        driver.get(feed_url)
    else:
        log_info("No previous cookies found.")

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
        log_info("Stored session unusable (redirect loop) — clearing cookies and re-authenticating")
        driver.delete_all_cookies()
        cookies = None
        driver.get(login_url)
        time.sleep(1)

    # LinkedIn serves a "HTTP ERROR 429 / This page isn't working" body at the SAME
    # /feed/ URL when the account/IP is rate-limited. A naive URL check would treat
    # that as "logged in" and downstream profile scraping would crash.
    #
    # When we hit this WITH stored cookies, the usual cause is a cookie/egress-IP
    # mismatch — cookies minted from a different IP (e.g. before switching to the
    # residential proxy) replayed through the proxy — not a genuine account throttle.
    # That is the same root cause the redirect-loop path above handles, and a fresh
    # credential login on the current IP clears it (verified live: a stale li_at from
    # the datacenter era 429'd on the proxy, but a cookie-less login succeeded). So drop
    # the cookies and fall through to a fresh login instead of giving up. Only treat it
    # as a real rate limit when we arrived here WITHOUT cookies (nothing left to drop).
    if _is_logged_in(driver.current_url) and _page_is_rate_limited(driver):
        if cookies:
            log_info("429 at feed with stored cookies — likely a stale-cookie/egress-IP "
                    "mismatch; clearing cookies and re-authenticating fresh")
            driver.delete_all_cookies()
            cookies = None
            driver.get(login_url)
            time.sleep(1)
        else:
            mark_rate_limited("429 at feed (no stored cookies to drop)")
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
        log_info(f"Already logged in! (current URL: {driver.current_url})")
        clear_rate_limit()
        _persist_session_cookies(driver, user_email)
        return True

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
    log_info(f"Logging in to LinkedIn as: {user_email}")

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
    try:
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
    except TimeoutException:
        # The login form never rendered — LinkedIn served a page `_is_challenge_url` didn't
        # recognize as a challenge, so this fell straight through to hunting for a field that
        # was never there (issue #1908: a 6h outage where the only signal was a bare
        # "TimeoutException: Finding Username Field", with no way to tell which unmatched
        # challenge/checkpoint page caused it). Log what was actually on screen so the NEXT
        # occurrence is diagnosable instead of another blind guess.
        log_warning(
            "Login form fields never appeared — unrecognized challenge/checkpoint page",
            action_type="login", url=driver.current_url, page_snippet=_page_body(driver)[:300])
        raise

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

    # A fresh credential login can still 429 when the IP/account is genuinely throttled
    # (not just a stale-cookie mismatch). Detect it here so we back off cleanly instead
    # of timing out on the Feed-title wait below.
    if _page_is_rate_limited(driver):
        mark_rate_limited("429 after fresh credential login")
        raise LinkedInRateLimited(
            "LinkedIn is rate-limiting this session (HTTP 429) even after a fresh login. "
            "Backing off — reduce automation frequency and retry later.")

    wait.until(EC.title_contains("Feed"), "Waiting for Feed to load after login")

    if _is_logged_in(driver.current_url):
        log_info("Login successful!")
        clear_rate_limit()
        _persist_session_cookies(driver, user_email)
        return True

    # ERROR, not INFO: this is the fall-through where the whole login flow ran and did not
    # reach a signed-in state. Everything downstream then does nothing, quietly, for as long
    # as it lasts — which is exactly how engagement sat dead for weeks. No exc= : there is no
    # exception here, so this is a loud log line rather than a filed $exception.
    log_error("LinkedIn login did not reach a signed-in state", action_type="login")
    return False


def get_my_profile(driver, wait, user_email: str, user_password: str, user_id: Optional[int] = None,
                   force_refresh: bool = False) -> Optional[LinkedInProfile]:
    """The signed-in user's OWN profile, from the DB cache when it is there and by scraping when it is
    not.

    Only the cache-miss path touches Chrome (it logs in and follows `/in/` to whatever URL LinkedIn
    redirects to), so a warm cache costs no browser time. A row older than the getter's freshness
    window reads as absent, so this re-scrapes roughly daily. `user_id` is the preferred cache key;
    email is the backward-compatible fallback.

    `force_refresh` skips the cache read entirely — the on-demand refresh (issue #1076) exists
    precisely for the case the freshness window gets wrong: the user edited their profile MINUTES
    ago, so a row written this morning is fresh by age and stale by content. It bypasses BOTH cache
    layers, since the by-URL cache inside `get_linkedin_profile_from_url` would otherwise hand back
    the same stale row the by-user read was told to ignore.

    Returns None when the scrape yields nothing; every caller must handle that, because a DOM
    change makes it the normal failure.
    """
    profile = None

    # Prefer user_id-based cache key; fall back to email for backward compat.
    if force_refresh:
        profile_json = None
    elif user_id is not None:
        profile_json = get_linked_in_profile_by_user_id(user_id)
    else:
        profile_json = get_linked_in_profile_by_email(user_email)

    if profile_json is None:
        # DEBUG: the docstring says a row past the freshness window reads as absent, so this is
        # the daily cache miss the function is built around — an expected no-op.
        log_debug(f"Previous Profile not found (or stale) in DB: {user_email}")
        login_to_linkedin(driver, wait, user_email, user_password)

        profile_url = "https://www.linkedin.com/in/"
        driver.get(profile_url)  # Need the page to redirect
        time.sleep(2)
        profile_url = driver.current_url  # Get the updated url

        profile_data = get_linkedin_profile_from_url(driver, wait, profile_url, True,
                                                    force_refresh=force_refresh)

        if profile_data:

            profile = LinkedInProfile(**profile_data)
            # Add email and password to profile for later use
            profile.email = user_email
            profile.password = user_password

            # Add profile to DB for faster future retrieval
            if add_linkedin_profile(profile, user_id=user_id):
                log_info(f"Profile saved to DB: {profile.full_name}")
                # Issue #1075: detect top-5 skill reorder/additions after a fresh scrape and open
                # the re-index window so generated content echoes the new keywords.
                if user_id is not None:
                    try:
                        record_profile_skills_change(user_id, profile)
                    except Exception as e:
                        log_debug("Could not record profile-skills change", exc=e, user_id=user_id)
            else:
                # DEBUG: add_linkedin_profile logs the write failure where it happens, at ERROR.
                # Warn where you detect, not where you notice (issue #1038).
                log_debug(f"Failed to save profile to DB: {profile.full_name}")
        else:
            # WARNING: the docstring names this the NORMAL failure when the DOM changes, and it
            # costs every caller its voice/profile grounding. Once is SDUI noise; repeatedly is
            # drift, and that is the defect worth an issue.
            log_warning("Failed to get my profile data — scrape returned nothing",
                        user_id=user_id, action_type="login")
    else:
        # Ensure profile_json is a string
        profile_json_str = profile_json[0] if isinstance(profile_json, tuple) else profile_json
        # Create a LinkedInProfile object from json string data
        profile = LinkedInProfile.model_validate_json(profile_json_str)
        log_info(f"Profile Restored from DB: {profile.full_name}")

    return profile


def load_profile_for_user(user_id: int) -> "LinkedInProfile | None":
    """Best-effort load of a cached LinkedInProfile for a user.

    Returns None when there's no cached profile or the stored JSON can't be parsed —
    callers use it only to enrich prompts, so missing data must never raise.
    """
    try:
        raw = get_linked_in_profile_by_user_id(user_id)
    except Exception as e:
        # WARNING: the repository swallows its own mysql errors and returns None, so anything
        # arriving here is an unexpected fault, and the caller loses its prompt grounding.
        log_warning("load_profile_for_user: lookup failed", exc=e, user_id=user_id)
        return None
    if not raw:
        return None
    profile_json_str = raw[0] if isinstance(raw, tuple) else raw
    try:
        return LinkedInProfile.model_validate_json(profile_json_str)
    except Exception as e:
        # WARNING: a stored profile that will not parse is corrupt data — it fails identically on
        # every run until the row is rewritten, which is what escalation should surface.
        log_warning("load_profile_for_user: could not parse the stored profile", exc=e,
                    user_id=user_id)
        return None


def get_linkedin_profile_from_url(driver, wait, profile_url, is_main_user=False, force_refresh=False):
    """Anyone's profile as a plain dict — DB cache first, scrape (plus an AI industry guess) on a miss.

    A dict rather than a `LinkedInProfile` because that is what `get_my_profile` and the viewer
    outreach walk feed onward. LinkedIn rewrites vanity URLs, so a navigation that lands somewhere
    else re-enters this function on the URL it actually got — the cache is keyed on the resolved URL,
    not the one asked for, and `force_refresh` rides that recursion so the second hop does not read
    the cache the first hop was told to skip.

    Both cache-hit and fresh-scrape paths now return the same dict shape, including the AI-derived
    `industry` — previously a fresh scrape returned only the raw scraped fields.

    Raises:
        ProfileUnavailableError: from the scraper, and deliberately not caught here — an auth-wall or
            rate-limited page must never be cached or read as a sparse profile.
    """
    # Get the profile from the DB if it exists
    profile_json = None if force_refresh else get_linked_in_profile_by_url(profile_url)

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
                return get_linkedin_profile_from_url(driver, wait, profile_url, is_main_user,
                                                     force_refresh=force_refresh)

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
                log_info(f"Profile saved to DB: {profile.full_name}")

            # Return the same shape the cache-hit path returns, including AI-derived fields.
            profile_data = profile.model_dump()

        # Use json to output to string
        # myprint(json.dumps(profile_data, indent=4))

    else:
        # Ensure profile_json is a string
        profile_json_str = profile_json[0] if isinstance(profile_json, tuple) else profile_json
        # Create a LinkedInProfile object from json string data
        profile = LinkedInProfile.model_validate_json(profile_json_str)
        profile_data = profile.model_dump()
        log_info(f"Profile Restored from DB: {profile.full_name}")

    return profile_data
