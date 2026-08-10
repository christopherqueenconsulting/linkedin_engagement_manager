"""Browser session creation and the resilient element helpers every Selenium lane shares.

`get_docker_driver` is the ONE way a session is opened. It waits for the standalone-chrome container
to report ready, applies the user's proxy / timezone / locale / geolocation and the stealth init
script, and times the acquisition so a saturated session pool is measured rather than guessed.
Nothing may instantiate `webdriver.Chrome()` directly: a session created outside this module
egresses from the host's datacenter IP with a location that contradicts it, which is the
configuration behind the sustained LinkedIn /feed 429s.

Also owned here: the in-memory MV3 proxy-auth extension (`_build_proxy_auth_extension_b64`), because
Chrome cannot take proxy credentials on the command line and MV2 background pages are gone; and the
ordered-fallback locators (`find_first` / `click_first` / `find_all_first`) that LinkedIn's churning
SDUI markup requires.
"""
import base64
import io
import json
import time
import zipfile
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

import requests
import selenium
from selenium import webdriver
from selenium.common import (
    ElementNotInteractableException,
    InvalidSessionIdException,
    NoSuchElementException,
    SessionNotCreatedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from cqc_lem.utilities.env_constants import *

# Named explicitly on top of the star import above. The star import leaves ruff unable to prove the
# binding exists (F405), and the Docstring & Lint Gate is a ratchet — one new F405 fails the PR. The
# rest of this module's constants predate the baseline; a new one has to come in clean.
from cqc_lem.utilities.env_constants import SELENIUM_READY_TIMEOUT
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.utils import get_aws_device_farm_url

# Last-resort geolocation when a session has no user_id and therefore no stored Login Location.
_DEFAULT_LATITUDE = 30.3321
_DEFAULT_LONGITUDE = -81.6556


def quit_gracefully(driver: WebDriver):
    """Close a session, swallowing whatever the quit itself raises.

    Teardown runs on paths that are already finishing or already failing — most often on a session a
    deploy recreated the container out from under (`is_session_lost`) — so it must never become the
    error the caller reports.
    """
    try:
        driver.quit()
        log_info("Driver session closed.")
    except Exception as e:
        # DEBUG: the docstring is the reason — teardown runs on paths that are already finishing or
        # already failing, so this must never become the error the caller reports.
        log_debug(f"Error while quitting driver: {e}")


# What the Grid says when the session a call names is gone. The exception type covers the normal
# case; the message markers catch the same fault arriving as a bare WebDriverException from an
# older/wrapped driver. Deliberately NOT here: connection failures to the hub itself — a Grid that
# cannot be reached is a different fault and must stay loud.
_SESSION_GONE_MARKERS = ("invalid session id", "unable to find session", "no such session",
                         "session deleted")


def is_session_lost(exc: BaseException) -> bool:
    """True when a WebDriver call failed because the browser SESSION no longer exists.

    A deploy is the ordinary cause (issue #988): maintenance mode drains the workers for
    `maintenance.DEFAULT_DRAIN_TIMEOUT_SECONDS` and then recreates the containers regardless, so a
    long Selenium task still holding a session has it quit out from under it. The run is over
    either way — what this decides is whether the caller ends it on what already shipped or crashes
    and files a defect for a routine release.
    """
    if isinstance(exc, InvalidSessionIdException):
        return True
    if isinstance(exc, WebDriverException):
        message = (getattr(exc, "msg", None) or str(exc) or "").lower()
        return any(marker in message for marker in _SESSION_GONE_MARKERS)
    return False


def get_available_session_driver_id(wait_for_available=True, wait_time=60, retry=3):
    """The id of the Grid's first slot, or of the session already occupying it.

    Both branches of the walk break on the FIRST slot of the FIRST node, so what comes back is "a
    slot the Grid knows about", not necessarily a free one. TimeoutError only when the Grid reports
    no nodes or slots at all — after `retry` waits of `wait_time` seconds if `wait_for_available`.
    No caller in the tree today; sessions are acquired by asking for one via `get_docker_driver`.
    """
    # Query the Selenium Grid for available sessions
    url = f"http://{SELENIUM_HUB_HOST}:{SELENIUM_HUB_PORT}/status"
    response = requests.get(url)
    data = response.json()

    # Find an available session
    session_id = None
    for node in data['value']['nodes']:
        for slot in node['slots']:
            if slot['session'] is None:
                session_id = slot['id']['id']
                break
                # return session_id # No available sessions

            else:
                session = slot['session']
                # myprint(f"Session: {session}")
                session_id = session['sessionId']
                break
        if session_id:
            break

    if not session_id:
        if wait_for_available:
            if retry > 0:
                time.sleep(wait_time)
                return get_available_session_driver_id(wait_for_available, wait_time, retry - 1)
            else:
                raise TimeoutError("Timeout while waiting for available session")
        else:
            raise TimeoutError("No available session")

    return session_id


def _record_session_wait(seconds: float) -> None:
    """Feed one session-acquisition duration to the capacity monitor. Imported lazily and swallowed
    whole: instrumentation must never cost a browser session.
    """
    try:
        from cqc_lem.utilities.capacity_alerts import record_session_wait
        record_session_wait(seconds)
    except Exception:
        # Intentionally silent: a dead Redis or an import error in the monitor must not fail
        # (or slow, via a log round-trip) the caller that just acquired a browser session.
        pass


def _wait_for_selenium_ready(host: str, port: str, timeout: int = None) -> None:
    """Poll standalone-chrome readiness endpoint until ready or timeout.

    `timeout` defaults to `SELENIUM_READY_TIMEOUT` (60s, the value that used to be hard-coded here).
    It is worth tuning down on a box where the Grid restarting is a real event: this is a per-CALL-SITE
    stall, not a per-run one. `get_driver_wait_pair` retries only `SessionNotCreatedException`, so the
    `TimeoutError` raised here propagates on the first attempt — and each of the 25 call sites that
    reach a driver pays the full wait while the Grid is unreachable (issue #1339).

    Resolved at CALL time, not import time, so a test or an operator can change it without
    re-importing the module.
    """
    if timeout is None:
        timeout = SELENIUM_READY_TIMEOUT
    status_url = f"http://{host}:{port}/wd/hub/status"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(status_url, timeout=3)
            if resp.status_code == 200 and resp.json().get("value", {}).get("ready"):
                log_info(f"Selenium ready at {status_url}")
                log_info(f"VNC viewer available at http://{host}:7900")
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"Selenium not ready at {status_url} after {timeout}s")


def get_docker_driver(headless: bool = True, session_name: str = "ChromeTests", coordinates: dict = None,
                      user_id: int = None, lat: float = None, lng: float = None,
                      debug: bool = None, debug_required: bool = False) -> webdriver.Remote:
    """Open a Chrome session on the standalone-chrome container — the ONE way a driver is created.

    Everything that makes a session look like the user rather than a datacenter bot is applied here:
    their resolved proxy, timezone, locale and Login Location coordinates, plus the stealth init
    script. Passing `user_id` is what unlocks all of it — without one the session egresses from the
    host IP with fallback coordinates that may contradict it, so that case warns rather than passing
    silently. Every one of those steps is best-effort: a CDP override that fails is logged, not
    raised, so a session is still returned.

    Blocks here while the fixed session pool is full, and records that wait either way
    (`_record_session_wait`) — a request that gives up waiting is the loudest capacity signal there
    is, so it must not unwind unmeasured.

    `debug` (default: env `SELENIUM_DEBUG_NODE`) asks for the watchable Grid node and falls back to
    the normal pool when it is busy or absent, so debugging never queues behind itself.
    `debug_required=True` turns that fallback into a `DebugNodeUnavailable` — the live-validation
    probe must never quietly spend a slot the engagement lanes are sized for (#1108). Not asking
    for the debug node EXCLUDES it, which is what keeps a Celery lane off the watchable node.
    """
    if debug is None:
        debug = isTrue(os.getenv("SELENIUM_DEBUG_NODE", "False"))
    if debug_required:
        debug = True

    if DEVICE_FARM_PROJECT_ARN and TEST_GRID_PROJECT_ARN:
        remote_url = get_aws_device_farm_url(DEVICE_FARM_PROJECT_ARN, TEST_GRID_PROJECT_ARN)
    else:
        _wait_for_selenium_ready(SELENIUM_HUB_HOST, SELENIUM_HUB_PORT)
        remote_url = f"http://{SELENIUM_HUB_HOST}:{SELENIUM_HUB_PORT}/wd/hub"

    # Per-user geo profile: make the browser's reported location/timezone/locale match
    # where the user normally logs in, to reduce LinkedIn "new location" challenges.
    user_timezone = TZ
    user_locale = "en-US"
    user_proxy = None
    user_country = None
    if user_id is None:
        # Without a user_id there is no proxy, no timezone, no locale and no geo — the session
        # egresses straight from the host (a datacenter IP on the VPS) with a mismatched location.
        # That is the configuration that drove the sustained LinkedIn /feed 429s, and it used to
        # happen silently at nearly every call site. Never let it be silent again.
        log_warning(
            f"Selenium session '{session_name}' created WITHOUT user_id — no proxy, timezone, "
            "locale or geolocation will be applied and egress will be the host IP. Pass user_id "
            "for any session that touches LinkedIn.",
            action_type="login")
    else:
        from cqc_lem.utilities.db import get_user_geo, get_user_proxy
        geo = get_user_geo(user_id)
        if geo:
            if lat is None and geo.get("latitude") is not None:
                lat, lng = geo["latitude"], geo["longitude"]
            if geo.get("timezone"):
                user_timezone = geo["timezone"]
            if geo.get("locale"):
                user_locale = geo["locale"]
            user_country = geo.get("country")
        user_proxy = get_user_proxy(user_id)

    # Resolve egress proxy with zero user setup: explicit override → regional proxy
    # matched to the user's country → global default → none (direct egress).
    from cqc_lem.utilities.proxy import resolve_proxy
    effective_proxy = resolve_proxy(user_proxy, user_country)

    # One line per session answering "was this actually proxied, and as whom?" — previously this
    # could only be determined by shelling into the container. Host:port only; never the
    # credentials embedded in the proxy URL.
    log_info(f"Selenium session '{session_name}' egress={_proxy_label(effective_proxy)} "
             f"tz={user_timezone} locale={user_locale}", user_id=user_id, action_type="login")

    options = getBaseOptions()
    options.add_argument("--ignore-ssl-errors=yes")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument(f"--lang={user_locale}")
    if effective_proxy:
        apply_proxy(options, effective_proxy)
    if headless:
        options = add_headless_options(options)

    options.set_capability("se:timeZone", user_timezone)
    options.set_capability("se:screenResolution", "1920x1080")
    options.set_capability("se:name", f"CQC_LEM ({session_name})")
    # Before the session request, so a refusal costs nothing: `plan_daily_invites`' rule — decide
    # the allowance BEFORE Chrome opens.
    apply_debug_node(options, debug, required=debug_required)

    # A free slot answers in seconds; a full pool blocks HERE until one frees up, which is the only
    # direct measurement of the session cap being the binding constraint (capacity_alerts §552).
    _session_started = time.time()
    try:
        driver = webdriver.Remote(command_executor=remote_url, options=options)
    except Exception:
        # A session request that gave up waiting for a free slot is the LOUDEST capacity signal —
        # record it before the exception unwinds, or the worst case is the one we never measure.
        _record_session_wait(time.time() - _session_started)
        raise
    _record_session_wait(time.time() - _session_started)
    driver.set_window_size(1920, 1080)

    # Stealth: scrub the most-checked automation tells before any page script runs.
    # navigator.webdriver=undefined (the #1 signal), a realistic navigator.languages
    # matching the user's locale, and a present window.chrome. Best-effort.
    primary_lang = user_locale.split("-")[0] if user_locale else "en"
    stealth_js = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        f"Object.defineProperty(navigator,'languages',{{get:()=>['{user_locale}','{primary_lang}']}});"
        "window.chrome=window.chrome||{runtime:{}};"
    )
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": stealth_js})
    except Exception as e:
        # WARNING, like the missing-geolocation fallback below: a session that kept
        # navigator.webdriver is a degraded, more-detectable session, not a neutral one.
        log_warning("Could not apply stealth init script", exc=e, user_id=user_id,
                    action_type="login")

    if coordinates is None:
        # lat/lng come from the user's stored Login Location (get_user_geo above). The constants are
        # a last-resort fallback ONLY — reporting a fixed city that contradicts the egress IP's city
        # is itself a "new location" signal, so a session without a resolved geo is degraded, not
        # neutral. That's part of what the missing-user_id warning below is about.
        coordinates = {
            "latitude": lat if lat is not None else _DEFAULT_LATITUDE,
            "longitude": lng if lng is not None else _DEFAULT_LONGITUDE,
            "accuracy": 100,
        }
        if lat is None:
            log_warning(
                "Selenium session has no resolved geolocation — falling back to default "
                f"coordinates ({_DEFAULT_LATITUDE}, {_DEFAULT_LONGITUDE}), which may contradict "
                "the egress IP's location",
                user_id=user_id, action_type="login")
    # Apply geo, timezone and locale overrides at the CDP layer so JS fingerprint
    # signals (geolocation API, Intl/Date timezone, navigator.language) are consistent.
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", coordinates)
    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": user_timezone})
        driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": user_locale})
    except Exception as e:
        # WARNING for the same reason: a timezone that contradicts the egress IP is itself a signal.
        log_warning("Could not apply timezone/locale override", exc=e, user_id=user_id,
                    action_type="login")

    return driver


def _build_proxy_auth_extension_b64(username: str, password: str) -> str:
    """Build an in-memory MV3 Chrome extension that answers the proxy's auth challenge.

    Chrome's --proxy-server can't carry credentials, so an authenticated proxy (e.g. a
    residential provider whose sticky-session + geo target live in the username, like
    DataImpulse) needs an extension that supplies them via webRequest.onAuthRequired.
    MV2 background pages are disabled in current Chrome (149+), so this uses an MV3
    service worker with the `webRequestAuthProvider` permission (which is what re-enables
    a blocking onAuthRequired listener for a normal extension). Returned base64 zip is
    passed to options.add_encoded_extension so there are no temp files to clean up.
    """
    manifest = {
        "name": "lem-proxy-auth",
        "version": "1.0",
        "manifest_version": 3,
        "permissions": ["proxy", "webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "minimum_chrome_version": "108",
    }
    # json.dumps escapes the ';' and '.' in the sticky-session username safely.
    background = (
        f"const U = {json.dumps(username)};\n"
        f"const P = {json.dumps(password)};\n"
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "  function (details) { return { authCredentials: { username: U, password: P } }; },\n"
        "  { urls: ['<all_urls>'] },\n"
        "  ['blocking']\n"
        ");\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("background.js", background)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _proxy_label(proxy_url: Optional[str]) -> str:
    """host:port of a proxy URL for logging, or 'DIRECT'. Never leaks the credentials."""
    if not proxy_url:
        return "DIRECT"
    try:
        parsed = urlparse(proxy_url)
        if not parsed.hostname:
            return "invalid"
        return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
    except Exception:
        return "invalid"


SELENIUM_DEBUG_CAPABILITY = "lem:debug"


class DebugNodeUnavailable(RuntimeError):
    """The watchable debug node was REQUIRED and could not be had.

    Raised instead of silently falling back to the pool, for the one caller that must never spend
    a lane slot: the read-only live-validation probe an autonomous agent runs (#1108).
    """


def debug_node_status() -> dict:
    """What the Grid currently says about the watchable debug node.

    Three separate answers, because the fallback decision needs all three and they fail
    differently:

    - ``registered``: the node is in ``/status`` at all.
    - ``advertised``: its stereotype actually DECLARES ``lem:debug``. Grid's DefaultSlotMatcher only
      compares an extension capability a stereotype declares, so a node that does not advertise it
      matches a ``lem:debug=true`` request as readily as the debug node does — i.e. an unadvertised
      capability is not a pin, it is decoration. Between #753 and #1108 this was live: the compose
      value carried literal quotes, the node's merge failed, and every `--watch` session went to a
      random pool node.
    - ``free_slot``: it has a slot nothing is using.

    An unreachable hub answers all three ``False`` — the caller decides whether that is a fallback
    or a refusal.
    """
    status = {"registered": False, "advertised": False, "free_slot": False,
              "host": SELENIUM_DEBUG_NODE_HOST}
    if not SELENIUM_DEBUG_NODE_HOST:
        return status
    try:
        url = f"http://{SELENIUM_HUB_HOST}:{SELENIUM_HUB_PORT}/status"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        nodes = (response.json().get("value") or {}).get("nodes") or []
    except Exception:
        return status

    for node in nodes:
        uri = str(node.get("uri") or "")
        if f"//{SELENIUM_DEBUG_NODE_HOST}:" not in uri:
            continue
        status["registered"] = True
        for slot in node.get("slots") or []:
            stereotype = slot.get("stereotype") or {}
            if SELENIUM_DEBUG_CAPABILITY in stereotype:
                status["advertised"] = True
            if not slot.get("session"):
                status["free_slot"] = True
        return status
    return status  # debug node is not registered


def _debug_node_has_free_slot() -> bool:
    """True if the Grid's watchable debug node is usable AND free right now."""
    status = debug_node_status()
    return bool(status["registered"] and status["advertised"] and status["free_slot"])


def apply_debug_node(options: Options, debug: bool = False, required: bool = False) -> bool:
    """Decide which side of the Grid this session may run on. Returns True if it was pinned.

    The capability is set either way, and that is the point (#1108). A production session declares
    ``lem:debug=False``, which the debug node's ``lem:debug=true`` stereotype cannot match — so the
    ninth, watchable node is structurally excluded from the pool the Celery lanes share, rather
    than merely being "a spare node nobody happens to ask for". Pool nodes declare
    ``lem:debug=false``, so the reverse pin is exact too.

    A node that declares nothing is still matched by both requests: Grid ignores an extension
    capability its stereotype does not carry. That is deliberately fail-OPEN on capacity — a
    mis-declared pool node keeps taking production work — and is why ``required`` exists.

    ``required=True`` raises `DebugNodeUnavailable` instead of falling back, for a caller whose
    whole point is not to take a lane slot.
    """
    if not debug:
        # NOT the absence of the capability: an absent capability matches the debug node too.
        options.set_capability(SELENIUM_DEBUG_CAPABILITY, False)
        return False
    if not SELENIUM_DEBUG_NODE_HOST:
        if required:
            raise DebugNodeUnavailable(
                "SELENIUM_DEBUG_NODE_HOST is empty, so there is no watchable node to pin to")
        log_warning("Debug node requested but SELENIUM_DEBUG_NODE_HOST is empty; using pool",
                    action_type="login")
        return False

    status = debug_node_status()
    if status["registered"] and status["advertised"] and status["free_slot"]:
        options.set_capability(SELENIUM_DEBUG_CAPABILITY, True)
        log_info(f"Pinning session to debug node {SELENIUM_DEBUG_NODE_HOST}",
                 action_type="login")
        return True

    if required:
        raise DebugNodeUnavailable(
            f"debug node {SELENIUM_DEBUG_NODE_HOST} unavailable "
            f"(registered={status['registered']}, advertises {SELENIUM_DEBUG_CAPABILITY}="
            f"{status['advertised']}, free_slot={status['free_slot']}) — refusing to take a "
            "production Chrome slot instead")
    log_warning(f"Debug node {SELENIUM_DEBUG_NODE_HOST} busy or absent; using pool",
                action_type="login")
    options.set_capability(SELENIUM_DEBUG_CAPABILITY, False)
    return False


def apply_proxy(options: Options, proxy_url: str) -> None:
    """Route the browser's egress through `proxy_url` (scheme://[user:pass@]host:port).

    Auth-less proxies (IP-allowlisted, Tailscale/cloudflared exit node) work via a bare
    --proxy-server=scheme://host:port. Credentialed proxies additionally get an MV3
    auth-handler extension so Chrome can answer the proxy's 407 challenge (see
    docs/PER_USER_PROXY.md). Best-effort: a malformed URL is logged and ignored rather
    than failing driver creation.
    """
    try:
        parsed = urlparse(proxy_url)
        scheme = parsed.scheme or "http"
        host = parsed.hostname or ""
        if not host:
            # WARNING: a proxy that was configured and then ignored means this session egresses
            # from the datacentre IP, which is what #372 exists to prevent. The URL itself is not
            # interpolated — it carries credentials.
            log_warning("Invalid proxy_url (no host) — session will egress unproxied",
                        action_type="login")
            return
        hostport = f"{host}:{parsed.port}" if parsed.port else host
        options.add_argument(f"--proxy-server={scheme}://{hostport}")
        log_info(f"Routing browser egress via proxy {scheme}://{hostport}")
        if parsed.username and parsed.password:
            options.add_encoded_extension(
                _build_proxy_auth_extension_b64(parsed.username, parsed.password))
            log_info("Loaded proxy auth extension for credentialed proxy")
        elif parsed.username:
            log_info("Proxy URL has a username but no password; sending host:port only. "
                    "Use user:pass or an IP-allowlisted proxy (see docs/PER_USER_PROXY.md).")
    except Exception as e:
        # Same consequence as the no-host branch: the browser starts, unproxied.
        log_warning("Could not apply proxy — session will egress unproxied", exc=e,
                    action_type="login")


def add_headless_options(options: Options) -> Options:
    """Add the headless flags, and the window/stealth arguments that have to go with them.

    Mutates and returns the SAME object it was given, so reassigning the result is a convenience and
    not a copy. `--enable-automation` is deliberately absent: it sets navigator.webdriver, the exact
    tell the rest of this module works to suppress.
    """
    # options.add_argument("--headless=new") # <--- DOES NOT WORK
    # options.add_argument("--headless=chrome")  # <--- WORKING
    options.add_argument("--headless")  # <--- ???

    # Additional options while headless
    options.add_argument('--start-maximized')  # Working
    options.add_argument("--window-size=1920x1080")  # Working
    options.add_argument('--disable-popup-blocking')  # Working
    options.add_argument('--incognito')  # Working
    options.add_argument('--no-sandbox')  # Working
    options.add_argument('--disable-gpu')  # Working
    options.add_argument('--disable-extensions')  # Working
    options.add_argument('--disable-infobars')  # Working
    # NOTE: deliberately NOT adding --enable-automation — it advertises automation
    # (sets navigator.webdriver=true), which is exactly what we want to avoid.
    options.add_argument('--disable-browser-side-navigation')  # Working
    options.add_argument('--disable-dev-shm-usage')  # Working
    options.add_argument('--disable-features=VizDisplayCompositor')  # Working
    options.add_argument('--dns-prefetch-disable')  # Working
    options.add_argument("--force-device-scale-factor=1")  # Working
    options.add_argument("--disable-blink-features=AutomationControlled")  # stealth

    options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

    return options


def getBaseOptions(base_download_directory: str = None):
    """The Chrome options every LEM session starts from: download prefs, container-safe flags, stealth.

    Downloads land in `<base_download_directory>/downloads` (default: the process CWD) and PDFs are
    saved rather than opened in the viewer, which is what makes a downloaded file appear on disk at
    all. No user-agent is pinned — the long comment below records why an invented UA is a liability.
    """
    options = Options()
    # options.add_argument("--incognito") # May cause issues with tabs
    if base_download_directory is None:
        base_download_directory = os.getcwd()
    prefs = {"download.default_directory": base_download_directory + '/downloads',
             "download.prompt_for_download": False,
             "download.directory_upgrade": True,
             "plugins.always_open_pdf_externally": True}
    options.add_experimental_option("prefs", prefs)

    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    # Options to make us undetectable (Review https://amiunique.org/fingerprint from the browser to verify)
    # Stealth flags: stop Chrome advertising automation. --disable-blink-features=
    # AutomationControlled keeps navigator.webdriver from being forced to true, and the
    # excludeSwitches/useAutomationExtension pair removes the "controlled by automated
    # test software" banner and the cdc_ automation extension — both are signals LinkedIn
    # uses to flag a session as a bot. (Note: the dominant "new device/location" signal is
    # still the egress IP; these reduce the automation fingerprint, not the IP geo.)
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("window-size=1920x1080")
    # NO --user-agent override. A pinned UA string is a LIABILITY, not stealth: Chrome derives
    # User-Agent Client Hints (navigator.userAgentData, Sec-CH-UA, Sec-CH-UA-Platform) from the
    # real build, and --user-agent does NOT change them. Any string we invent therefore contradicts
    # the Client Hints the same request sends. This code pinned "Chrome/130 on macOS" while the
    # selenium/standalone-chrome image silently rolled forward to Chrome 150 on Linux — a 20-version
    # AND wrong-OS mismatch that is trivial for LinkedIn to check, and a prime suspect in the
    # sustained /feed 429s. Chrome's own UA is always self-consistent; let it speak for itself.
    # If a specific UA is ever genuinely required, set it via CDP Network.setUserAgentOverride WITH
    # a matching userAgentMetadata payload — that updates the Client Hints too.

    # options.set_capability("browserVersion", "100.0")  # This is needed for this version

    # options.page_load_strategy = 'eager'  # interactive
    # options.page_load_strategy = "normal"  # complete

    return options


def click_element_wait_retry(driver: WebDriver, wait: WebDriverWait, find_by_value: str, wait_text: str,
                             find_by: str = By.XPATH,
                             max_retry: int = MAX_WAIT_RETRY,
                             parent_element: WebElement = None,
                             use_action_chain=False,
                             element_always_expected=True) -> WebElement:
    """Wait for an element, wait for it to be clickable, then click it — riding out DOM timing churn.

    An ElementNotInteractableException is retried up to `max_retry` times, 5s apart; a stale or
    timed-out element is not retried here (`get_element_wait_retry` already did that for the lookup).
    `element_always_expected=False` turns every one of those failures into a None return instead of a
    raise, for a control that legitimately may not be on the page.

    The interactable retry recurses WITHOUT `use_action_chain` or `element_always_expected`, so a
    retried click always goes through a plain `element.click()` and always re-raises on failure.
    """
    # element = False
    try:
        # Wait for element
        element = get_element_wait_retry(driver, wait, find_by_value, wait_text, find_by, max_retry, parent_element,
                                         element_always_expected)

        if element:
            # Wait for element to be clickable
            element = wait.until(EC.element_to_be_clickable(element))
            if use_action_chain:
                ActionChains(driver).move_to_element(element).click().perform()
                wait_for_ajax(driver)
            else:
                element.click()
        else:
            if element_always_expected:
                raise ElementNotInteractableException("Element not found or interactable")

    except ElementNotInteractableException as se:
        if max_retry > 1:
            # DEBUG: this is the retry loop's own bookkeeping, not an outcome. A miss that still
            # has attempts left has not failed yet.
            log_debug(wait_text + " | Not Interactable | .....retrying")
            time.sleep(5)  # wait 5 seconds
            driver.implicitly_wait(5)  # wait on driver 5 seconds
            element = click_element_wait_retry(driver, wait, find_by_value, wait_text, find_by, max_retry - 1,
                                               parent_element)
        else:

            if element_always_expected:
                # raise TimeoutException("Timeout while " + wait_text)
                log_info(f"Failed to find or interact with element: {wait_text} | Error: {se}")
                # return None
                raise se
            else:
                log_info(f"Failed to find or interact with element: {wait_text}")
                element = None

    except (StaleElementReferenceException, TimeoutException) as st:
        # DEBUG: either the caller declared the element optional (a legitimate absence) or this
        # re-raises and the caller reports it. Neither wants a second record here.
        log_debug(wait_text + " | Stale or Timed out | ")
        if element_always_expected:
            raise st
        else:
            element = None

    return element


def get_element_wait_retry(driver: WebDriver, wait: WebDriverWait, find_by_value: str, wait_text: str,
                           find_by: str = By.XPATH,
                           max_try: int = MAX_WAIT_RETRY,
                           parent_element: WebElement = None,
                           element_always_expected=True) -> WebElement:
    """Wait for ONE element by a single locator, retrying stale/timeout `max_try` times, 5s apart.

    Scoped to `parent_element` when one is given, otherwise to the whole document.
    `element_always_expected=False` returns None on the final miss instead of re-raising — the same
    fail-soft switch `click_element_wait_retry` carries, and the reason both return a falsy value a
    caller can branch on.
    """
    # element = False
    try:
        # Wait for element
        if parent_element:
            # Use the parent element to find the child element
            element = wait.until(
                lambda d: parent_element.find_element(find_by, find_by_value),
                wait_text)
        else:
            # Use the driver to find the element
            element = wait.until(
                lambda d: d.find_element(find_by, find_by_value),
                wait_text)

    except (StaleElementReferenceException, TimeoutException) as se:
        if max_try > 1:
            # DEBUG: retry bookkeeping — see click_element_wait_retry.
            log_debug(wait_text + " | Stale | .....retrying")
            time.sleep(5)  # wait 5 seconds
            driver.implicitly_wait(5)  # wait on driver 5 seconds
            element = get_element_wait_retry(driver, wait, find_by_value, wait_text, find_by, max_try - 1,
                                             parent_element, element_always_expected)
        else:
            # raise TimeoutException("Timeout while " + wait_text)
            if element_always_expected:
                raise se
            else:
                log_info(f"Failed to find element: {wait_text}")
                element = None

    return element


def get_visible_element_wait_retry(driver: WebDriver, wait: WebDriverWait,
                                   locators: list[tuple[str, str]], wait_text: str,
                                   max_try: int = MAX_WAIT_RETRY,
                                   element_always_expected: bool = True) -> WebElement | None:
    """Return the first *displayed* element matching any of `locators`.

    `locators` is an ordered list of (By, value) pairs tried in turn. Some pages
    (e.g. LinkedIn's redesigned login) render duplicate hidden+visible copies of
    the same field, so matching on presence alone can return an invisible element —
    we must pick the one the user actually sees.
    """

    def _find_visible(d):
        for find_by, value in locators:
            try:
                for el in d.find_elements(find_by, value):
                    try:
                        if el.is_displayed():
                            return el
                    except StaleElementReferenceException:
                        continue
            except (StaleElementReferenceException, NoSuchElementException):
                continue
        return False

    try:
        return wait.until(_find_visible, wait_text)
    except (StaleElementReferenceException, TimeoutException) as se:
        if max_try > 1:
            # DEBUG: retry bookkeeping — see click_element_wait_retry.
            log_debug(wait_text + " | Not visible | .....retrying")
            time.sleep(5)
            return get_visible_element_wait_retry(driver, wait, locators, wait_text,
                                                  max_try - 1, element_always_expected)
        if element_always_expected:
            raise se
        log_info(f"Failed to find visible element: {wait_text}")
        return None


def find_first(driver: WebDriver, wait: WebDriverWait, locators: list[tuple[str, str]], label: str,
               *, required: bool = True, parent_element: WebElement = None,
               max_try: int = MAX_WAIT_RETRY, visible_only: bool = False,
               warn_on_miss: bool = True,
               user_id: int = None, post_id: int = None) -> WebElement | None:
    """Return the first element matching any locator in `locators` (ordered, most-stable first).

    LinkedIn ships hashed CSS classes that churn, so callers pass an ordered fallback chain
    (aria-label/role → data-* → visible text → semantic → legacy class). On a total miss:
    if `required`, raise (like the other helpers); else emit a STRUCTURED warning naming the
    tried selectors + current URL and return None — turning today's silent skips into greppable
    signal. `visible_only` restricts to displayed elements (duplicate hidden+visible copies).

    `warn_on_miss=False` logs that miss at DEBUG instead: use it where the element is legitimately
    absent on some surfaces (a home-feed-only control looked for on a group page), because a
    repeated WARNING escalates to ERROR and files a defect for working behaviour.
    """
    root = parent_element if parent_element is not None else driver

    def _find(_d):
        for find_by, value in locators:
            try:
                if visible_only:
                    for el in root.find_elements(find_by, value):
                        try:
                            if el.is_displayed():
                                return el
                        except StaleElementReferenceException:
                            continue
                else:
                    els = root.find_elements(find_by, value)
                    if els:
                        return els[0]
            except (StaleElementReferenceException, NoSuchElementException):
                continue
        return False

    try:
        return wait.until(_find, label)
    except (StaleElementReferenceException, TimeoutException) as se:
        if max_try > 1:
            # DEBUG, and this one matters: a caller that passed warn_on_miss=False asked for the
            # FINAL miss to be silent, so the retries in between must not be louder than it.
            log_debug(label + " | not found | .....retrying")
            time.sleep(5)
            return find_first(driver, wait, locators, label, required=required,
                              parent_element=parent_element, max_try=max_try - 1,
                              visible_only=visible_only, warn_on_miss=warn_on_miss,
                              user_id=user_id, post_id=post_id)
        if required:
            raise se
        try:
            current_url = driver.current_url
        except Exception:
            current_url = "?"
        emit = log_warning if warn_on_miss else log_debug
        emit(f"Selector miss: {label}", action_type="scrape", user_id=user_id, post_id=post_id,
             selectors=[f"{by}={val}" for by, val in locators], url=current_url)
        return None


def click_first(driver: WebDriver, wait: WebDriverWait, locators: list[tuple[str, str]], label: str,
                *, required: bool = True, parent_element: WebElement = None,
                use_action_chain: bool = False, max_try: int = MAX_WAIT_RETRY,
                warn_on_miss: bool = True,
                user_id: int = None, post_id: int = None) -> WebElement | None:
    """Find (via `find_first`) then click the first matching element, with the same resilient
    fallback + structured-miss-logging behavior — including `warn_on_miss`, so a control the caller
    already has a working fallback for logs BOTH of its miss paths (not found, and found but not
    clickable) at DEBUG. Returns the clicked element or None.
    """
    element = find_first(driver, wait, locators, label, required=required,
                         parent_element=parent_element, max_try=max_try, visible_only=True,
                         warn_on_miss=warn_on_miss, user_id=user_id, post_id=post_id)
    if element is None:
        return None
    try:
        element = wait.until(EC.element_to_be_clickable(element))
        if use_action_chain:
            ActionChains(driver).move_to_element(element).click().perform()
            wait_for_ajax(driver)
        else:
            element.click()
    except (ElementNotInteractableException, StaleElementReferenceException, TimeoutException) as se:
        if required:
            raise se
        # A hover-revealed control can go un-clickable between the find and the click, which returns
        # None exactly like a selector miss does — so the caller's fallback is the same one. Warning
        # here regardless of warn_on_miss would re-open the escalation the flag was added to close.
        emit = log_warning if warn_on_miss else log_debug
        emit(f"Click miss: {label}", action_type="scrape", user_id=user_id, post_id=post_id)
        return None
    return element


def find_all_first(driver: WebDriver, locators: list[tuple[str, str]]) -> list[WebElement]:
    """Return the element list from the FIRST locator that yields any matches (ordered fallback
    for collections, e.g. a comment list). Empty list if none match — caller logs if needed.
    """
    for find_by, value in locators:
        try:
            els = driver.find_elements(find_by, value)
            if els:
                return els
        except (StaleElementReferenceException, NoSuchElementException):
            continue
    return []


def get_elements_as_list_wait_stale(wait: WebDriverWait, find_by_value: str, wait_text: str,
                                    find_by: str = By.XPATH, max_retry=3) -> list[WebElement]:
    """Wait for ALL elements matching one locator, retrying stale/timeout `max_retry` times.

    `wait.until` treats an empty list as "not ready yet", so ZERO matches is a timeout here, not an
    empty return: after the retries are spent the TimeoutException is re-raised. A caller that wants
    "the page had none" as an answer wants `find_all_first`, which returns `[]`.
    """
    elements = []

    try:
        elements = wait.until(lambda d: d.find_elements(find_by, find_by_value), wait_text)
        # elements_list = list(map(lambda x: getText(x), elements))
    except (StaleElementReferenceException, TimeoutException) as se:
        # DEBUG: retry bookkeeping — the last attempt re-raises, so the caller reports the failure.
        log_debug(wait_text + " | Stale | .....retrying")
        time.sleep(5)  # wait 5 seconds
        if max_retry > 1:
            elements = get_elements_as_list_wait_stale(wait, find_by_value, wait_text, find_by, max_retry - 1)
        else:
            # raise NoSuchElementException("Could not find element by %s with value: %s" % (find_by, find_by_value))
            raise se

    return elements


def wait_for_ajax(driver):
    """Best-effort wait for jQuery to go idle and then for document.readyState to reach 'complete'.

    Everything is swallowed, including the JavascriptException a page WITHOUT jQuery raises on the
    very first check — on such a page this returns immediately and the readyState wait never runs at
    all, so it must never be relied on as a settle barrier.
    """
    wait = get_driver_wait(driver)
    try:
        wait.until(lambda d: d.execute_script('return jQuery.active') == 0)
        wait.until(lambda d: d.execute_script('return document.readyState') == 'complete')
    except Exception:
        # Best-effort settle. A page without jQuery, or one still streaming when the wait expires,
        # is ordinary — the caller's own locator wait is the real gate, so a miss here must not
        # become the error it reports.
        pass


def getText(curElement: WebElement):
    """Get Selenium element text

    Args:
        curElement (WebElement): selenium web element
    Returns:
        str
    Raises:
    """
    # # for debug
    # elementHtml = curElement.get_attribute("innerHTML")
    # print("elementHtml=%s" % elementHtml)

    elementText = curElement.text  # sometimes does not work

    if not elementText:
        elementText = curElement.get_attribute("innerText")

    if not elementText:
        elementText = curElement.get_attribute("textContent")

    # print("elementText=%s" % elementText)
    return elementText


def window_scroll(driver: WebDriver, scroll_times: int = 0, wait_on_ajax=False):
    """Jump to the bottom of the page `scroll_times` times, to pull in lazily-loaded content.

    The default of 0 scrolls NOTHING — a caller that omits the count gets a no-op, not one scroll.
    """
    # Force bottom page scroll by scroll_times
    for _ in range(scroll_times):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        if wait_on_ajax:
            wait_for_ajax(driver)
            # time.sleep(2)


def close_tab(driver: WebDriver, handles: list[str] = None, max_retry=3):
    """Close the current tab, falling back to WAITING for the window count to drop.

    A close that raises is never retried by closing again — it is retried by waiting for one fewer
    handle than `handles`, so a tab the browser has already closed resolves instead of erroring.
    Gives up silently once `max_retry` is spent: the caller is expected to be tearing down anyway.
    """
    if handles is None:
        handles = driver.window_handles

    wait = get_driver_wait(driver)

    try:
        driver.close()
    except WebDriverException:
        # DEBUG: the docstring says this gives up silently — the caller is tearing down anyway.
        log_debug("Failed to close browser/tab. Retrying.....")
        try:
            # Wait to close the new window or tab
            wait.until(EC.number_of_windows_to_be(len(handles) - 1), "Waiting for browser/tab to close.")
        except TimeoutException as te:
            log_debug(f"Timed out waiting for browser/tab to close: {te}")
            if (max_retry > 0):
                close_tab(driver, handles, max_retry - 1)
        

def get_driver_wait(driver, wait_time: int = None):
    """Build the WebDriverWait every helper here shares (default timeout `WAIT_DEFAULT_TIMEOUT`).

    NoSuchElement and StaleElementReference are on its ignore list because the retry helpers in this
    module handle both themselves — a wait that raised them would pre-empt that retry.
    """
    if wait_time is None:
        wait_time = WAIT_DEFAULT_TIMEOUT

    return WebDriverWait(driver, wait_time,
                         # poll_frequency=3,
                         ignored_exceptions=[
                             NoSuchElementException,  # This is handled individually
                             StaleElementReferenceException  # This is handled by our click_element_wait_retry method
                         ])


def get_driver_wait_pair(headless=False, session_name: str = "ChromeTests", max_retry=3, coordinates: dict = None,
                         user_id: int = None, debug: bool = None, debug_required: bool = False):
    """Create a driver and its matching wait, retrying session creation with exponential backoff.

    ONLY SessionNotCreatedException is retried: a full pool clears on its own, while any other
    failure is not a capacity problem and must surface immediately. Backoff is `30 * 2**attempt`
    seconds, but the LAST attempt re-raises before sleeping — so at the default `max_retry=3` the
    waits are 30s and 60s and the run gives up after two, never three. Returns once the browser
    reports at least one window handle, so the caller never gets a half-started session.

    `debug_required` is NOT retried on purpose: `DebugNodeUnavailable` is not a full pool, it is a
    caller that may not use the pool at all (#1108).
    """
    # Create the driver. Passing user_id applies that user's geo/timezone/locale spoofing.
    driver = None
    for attempt in range(max_retry):
        try:
            driver = get_docker_driver(headless=headless, session_name=session_name, coordinates=coordinates,
                                       user_id=user_id, debug=debug, debug_required=debug_required)
            break  # Exit the loop if successful
        except SessionNotCreatedException as e:
            if attempt == max_retry - 1:
                raise e  # Raise the exception if max retries reached
            wait_time = 30 * (2 ** attempt)  # Exponential backoff starting at 30 seconds
            time.sleep(wait_time)  # Wait before retrying

    if driver is None:
        # `range(max_retry)` with max_retry < 1 never enters the loop, so nothing assigns `driver`
        # and nothing raises — the next line used to fail with a bare NameError from inside a
        # Selenium helper, which reads like a driver fault rather than a bad argument.
        raise ValueError(f"get_driver_wait_pair needs max_retry >= 1, got {max_retry}")

    wait = get_driver_wait(driver)

    log_info("Driver and Wait created. Waiting for one window handle")

    # Wait until at least one window handle is available (indicating that the browser has started)
    wait.until(lambda driver: len(driver.window_handles) > 0)

    log_info("Window handle found. Returning driver and wait pair.")

    # Give some time for multiple calls
    # time.sleep(2)

    return driver, wait


def clear_sessions():
    """DELETE every live session on the Grid — LEM's own included.

    A blunt recovery for a wedged pool: it walks the Grid status and deletes each occupied slot, so
    any task holding a browser at that moment loses it mid-run. Nothing in `src/` calls it.
    """
    # base_url = f"http://{SELENIUM_HUB_HOST}:4444"
    base_url = f"http://{SELENIUM_HUB_HOST}:{SELENIUM_HUB_PORT}"
    response = requests.get(f"{base_url}/status")
    data = json.loads(response.text)

    for node in data['value']['nodes']:
        for slot in node['slots']:
            if slot['session']:
                session_id = slot['session']['sessionId']
                delete_url = f"{base_url}/session/{session_id}"
                requests.delete(delete_url)


def load_cookies(driver: WebDriver, cookies: list[dict]):
    """Restore stored cookies into the current session, skipping any that Chrome refuses.

    A missing or unparseable expiry becomes 30 days out, so a restored cookie is persistent rather
    than session-scoped. A rejected cookie is skipped and the rest still load, which means a PARTIAL
    restore is a normal outcome — the caller must verify it is actually signed in, never assume it
    from this returning.
    """
    for cookie in cookies:
        expiry = cookie['expiry']
        if expiry is not None:
            try:
                expiry = int(expiry)
            except ValueError:
                expiry = int((datetime.now() + timedelta(days=30)).timestamp())
        else:
            expiry = int((datetime.now() + timedelta(days=30)).timestamp())
        try:
            driver.add_cookie({
                'name': cookie['name'],
                'value': cookie['value'],
                'domain': cookie['domain'] if cookie['domain'] else None,
                'path': cookie['path'] if cookie['path'] else None,
                'expiry': expiry,
                'secure': bool(cookie['secure']) if cookie['secure'] is not None else False,
                'httpOnly': bool(cookie['http_only']) if cookie['http_only'] is not None else False,
            })
        except selenium.common.exceptions.InvalidArgumentException as e:
            # DEBUG, and ONE line for ONE condition: the docstring calls a partial restore a NORMAL
            # outcome and puts the signed-in verdict on the caller, so a refused cookie is not this
            # function's failure to report. The cookie value is never interpolated — it is a secret.
            log_debug(f"Chrome refused a stored cookie ({cookie.get('name', '?')}): {e}")

