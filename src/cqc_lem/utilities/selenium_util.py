import base64
import io
import json
import time
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

import requests
import selenium
from selenium import webdriver
from selenium.common import ElementNotInteractableException, StaleElementReferenceException, \
    TimeoutException, WebDriverException, NoSuchElementException, SessionNotCreatedException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from cqc_lem.utilities.env_constants import *
from cqc_lem.utilities.logger import myprint, log_warning
from cqc_lem.utilities.utils import get_aws_device_farm_url


def quit_gracefully(driver: WebDriver):
    try:
        driver.quit()
        myprint(f"Driver session closed.")
    except Exception as e:
        myprint(f"Error while quitting driver: {e}")
        pass


def get_available_session_driver_id(wait_for_available=True, wait_time=60, retry=3):
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


def _wait_for_selenium_ready(host: str, port: str, timeout: int = 60) -> None:
    """Poll standalone-chrome readiness endpoint until ready or timeout."""
    status_url = f"http://{host}:{port}/wd/hub/status"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(status_url, timeout=3)
            if resp.status_code == 200 and resp.json().get("value", {}).get("ready"):
                myprint(f"Selenium ready at {status_url}")
                myprint(f"VNC viewer available at http://{host}:7900")
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"Selenium not ready at {status_url} after {timeout}s")


def get_docker_driver(headless: bool = True, session_name: str = "ChromeTests", coordinates: dict = None,
                      user_id: int = None, lat: float = None, lng: float = None) -> webdriver.Remote:
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
    if user_id is not None:
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

    driver = webdriver.Remote(command_executor=remote_url, options=options)
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
        myprint(f"Could not apply stealth init script | Error: {e}")

    if coordinates is None:
        coordinates = {
            "latitude": lat if lat is not None else 30.3321,
            "longitude": lng if lng is not None else -81.6556,
            "accuracy": 100,
        }
    # Apply geo, timezone and locale overrides at the CDP layer so JS fingerprint
    # signals (geolocation API, Intl/Date timezone, navigator.language) are consistent.
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", coordinates)
    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": user_timezone})
        driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": user_locale})
    except Exception as e:
        myprint(f"Could not apply timezone/locale override | Error: {e}")

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
            myprint(f"Invalid proxy_url (no host) — ignoring: {proxy_url}")
            return
        hostport = f"{host}:{parsed.port}" if parsed.port else host
        options.add_argument(f"--proxy-server={scheme}://{hostport}")
        myprint(f"Routing browser egress via proxy {scheme}://{hostport}")
        if parsed.username and parsed.password:
            options.add_encoded_extension(
                _build_proxy_auth_extension_b64(parsed.username, parsed.password))
            myprint("Loaded proxy auth extension for credentialed proxy")
        elif parsed.username:
            myprint("Proxy URL has a username but no password; sending host:port only. "
                    "Use user:pass or an IP-allowlisted proxy (see docs/PER_USER_PROXY.md).")
    except Exception as e:
        myprint(f"Could not apply proxy '{proxy_url}': {e}")


def add_headless_options(options: Options) -> Options:
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
    options.add_argument(
        # "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.169 Safari/537.36"
        #"user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.91 Safari/537.36"
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.91 Safari/537.36"
    )

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
            myprint(wait_text + " | Not Interactable | .....retrying")
            time.sleep(5)  # wait 5 seconds
            driver.implicitly_wait(5)  # wait on driver 5 seconds
            element = click_element_wait_retry(driver, wait, find_by_value, wait_text, find_by, max_retry - 1,
                                               parent_element)
        else:

            if element_always_expected:
                # raise TimeoutException("Timeout while " + wait_text)
                myprint(f"Failed to find or interact with element: {wait_text} | Error: {se}")
                # return None
                raise se
            else:
                myprint(f"Failed to find or interact with element: {wait_text}")
                element = None

    except (StaleElementReferenceException, TimeoutException) as st:
        myprint(wait_text + " | Stale or Timed out | ")
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
            myprint(wait_text + " | Stale | .....retrying")
            time.sleep(5)  # wait 5 seconds
            driver.implicitly_wait(5)  # wait on driver 5 seconds
            element = get_element_wait_retry(driver, wait, find_by_value, wait_text, find_by, max_try - 1,
                                             parent_element, element_always_expected)
        else:
            # raise TimeoutException("Timeout while " + wait_text)
            if element_always_expected:
                raise se
            else:
                myprint(f"Failed to find element: {wait_text}")
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
            myprint(wait_text + " | Not visible | .....retrying")
            time.sleep(5)
            return get_visible_element_wait_retry(driver, wait, locators, wait_text,
                                                  max_try - 1, element_always_expected)
        if element_always_expected:
            raise se
        myprint(f"Failed to find visible element: {wait_text}")
        return None


def find_first(driver: WebDriver, wait: WebDriverWait, locators: list[tuple[str, str]], label: str,
               *, required: bool = True, parent_element: WebElement = None,
               max_try: int = MAX_WAIT_RETRY, visible_only: bool = False,
               user_id: int = None, post_id: int = None) -> WebElement | None:
    """Return the first element matching any locator in `locators` (ordered, most-stable first).

    LinkedIn ships hashed CSS classes that churn, so callers pass an ordered fallback chain
    (aria-label/role → data-* → visible text → semantic → legacy class). On a total miss:
    if `required`, raise (like the other helpers); else emit a STRUCTURED warning naming the
    tried selectors + current URL and return None — turning today's silent skips into greppable
    signal. `visible_only` restricts to displayed elements (duplicate hidden+visible copies).
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
            myprint(label + " | not found | .....retrying")
            time.sleep(5)
            return find_first(driver, wait, locators, label, required=required,
                              parent_element=parent_element, max_try=max_try - 1,
                              visible_only=visible_only, user_id=user_id, post_id=post_id)
        if required:
            raise se
        try:
            current_url = driver.current_url
        except Exception:
            current_url = "?"
        log_warning(f"Selector miss: {label}", action_type="scrape", user_id=user_id, post_id=post_id,
                    selectors=[f"{by}={val}" for by, val in locators], url=current_url)
        return None


def click_first(driver: WebDriver, wait: WebDriverWait, locators: list[tuple[str, str]], label: str,
                *, required: bool = True, parent_element: WebElement = None,
                use_action_chain: bool = False, max_try: int = MAX_WAIT_RETRY,
                user_id: int = None, post_id: int = None) -> WebElement | None:
    """Find (via `find_first`) then click the first matching element, with the same resilient
    fallback + structured-miss-logging behavior. Returns the clicked element or None."""
    element = find_first(driver, wait, locators, label, required=required,
                         parent_element=parent_element, max_try=max_try, visible_only=True,
                         user_id=user_id, post_id=post_id)
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
        log_warning(f"Click miss: {label}", action_type="scrape", user_id=user_id, post_id=post_id)
        return None
    return element


def find_all_first(driver: WebDriver, locators: list[tuple[str, str]]) -> list[WebElement]:
    """Return the element list from the FIRST locator that yields any matches (ordered fallback
    for collections, e.g. a comment list). Empty list if none match — caller logs if needed."""
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
    elements = []

    try:
        elements = wait.until(lambda d: d.find_elements(find_by, find_by_value), wait_text)
        # elements_list = list(map(lambda x: getText(x), elements))
    except (StaleElementReferenceException, TimeoutException) as se:
        myprint(wait_text + " | Stale | .....retrying")
        time.sleep(5)  # wait 5 seconds
        if max_retry > 1:
            elements = get_elements_as_list_wait_stale(wait, find_by_value, wait_text, find_by, max_retry - 1)
        else:
            # raise NoSuchElementException("Could not find element by %s with value: %s" % (find_by, find_by_value))
            raise se

    return elements


def wait_for_ajax(driver):
    wait = get_driver_wait(driver)
    try:
        wait.until(lambda d: d.execute_script('return jQuery.active') == 0)
        wait.until(lambda d: d.execute_script('return document.readyState') == 'complete')
    except Exception as e:
        pass


def getText(curElement: WebElement):
    """
    Get Selenium element text

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
    # Force bottom page scroll by scroll_times
    for _ in range(scroll_times):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        if wait_on_ajax:
            wait_for_ajax(driver)
            # time.sleep(2)


def close_tab(driver: WebDriver, handles: list[str] = None, max_retry=3):
    if handles is None:
        handles = driver.window_handles

    wait = get_driver_wait(driver)

    try:
        driver.close()
    except WebDriverException as e:
        myprint("Failed to close browser/tab. Retrying.....")
        try:
            # Wait to close the new window or tab
            wait.until(EC.number_of_windows_to_be(len(handles) - 1), "Waiting for browser/tab to close.")
            pass
        except TimeoutException as te:
            myprint(te)
            if (max_retry > 0):
                close_tab(driver, handles, max_retry - 1)
                pass


def get_driver_wait(driver, wait_time: int = None):
    if wait_time is None:
        wait_time = WAIT_DEFAULT_TIMEOUT

    return WebDriverWait(driver, wait_time,
                         # poll_frequency=3,
                         ignored_exceptions=[
                             NoSuchElementException,  # This is handled individually
                             StaleElementReferenceException  # This is handled by our click_element_wait_retry method
                         ])


def get_driver_wait_pair(headless=False, session_name: str = "ChromeTests", max_retry=3, coordinates: dict = None,
                         user_id: int = None):
    # Create the driver. Passing user_id applies that user's geo/timezone/locale spoofing.
    for attempt in range(max_retry):
        try:
            driver = get_docker_driver(headless=headless, session_name=session_name, coordinates=coordinates,
                                       user_id=user_id)
            break  # Exit the loop if successful
        except SessionNotCreatedException as e:
            if attempt == max_retry - 1:
                raise e  # Raise the exception if max retries reached
            wait_time = 30 * (2 ** attempt)  # Exponential backoff starting at 30 seconds
            time.sleep(wait_time)  # Wait before retrying

    wait = get_driver_wait(driver)

    myprint("Driver and Wait created. Waiting for one window handle")

    # Wait until at least one window handle is available (indicating that the browser has started)
    wait.until(lambda driver: len(driver.window_handles) > 0)

    myprint("Window handle found. Returning driver and wait pair.")

    # Give some time for multiple calls
    # time.sleep(2)

    return driver, wait


def clear_sessions():
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
            myprint(f"Error loading cookie: {cookie}")
            myprint(f"Exception: {e}")
            pass


class RetryableWebDriver:
    def __init__(self, original_driver, max_retries=3, base_delay=1):
        self.driver = original_driver
        self.max_retries = max_retries
        self.base_delay = base_delay

    def __getattr__(self, name):
        # Get the original attribute from the wrapped driver
        attr = getattr(self.driver, name)

        # If it's a method, wrap it with retry logic
        if callable(attr):
            return self._wrap_with_retry(attr)
        return attr

    def _wrap_with_retry(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(self.max_retries):
                try:
                    return func(*args, **kwargs)
                except (WebDriverException, StaleElementReferenceException) as e:
                    last_exception = e
                    if attempt < self.max_retries - 1:
                        delay = self.base_delay * (2 ** attempt)  # exponential backoff
                        time.sleep(delay)
                    continue
            raise last_exception

        return wrapper

    # Explicitly handle quit to ensure proper cleanup
    def quit(self):
        try:
            self.driver.quit()
        except Exception as e:
            print(f"Error during driver quit: {e}")


# Example usage with your existing code structure:
def create_retryable_driver(original_driver, max_retries=3):
    return RetryableWebDriver(original_driver, max_retries)


# Usage example 1 - Basic setup
def setup_driver():
    driver = webdriver.Chrome()  # Your original driver setup
    return create_retryable_driver(driver)


# Usage example 2 - With context manager
class WebDriverManager:
    def __init__(self, max_retries=3):
        self.max_retries = max_retries
        self.driver = None

    def __enter__(self):
        original_driver = webdriver.Chrome()  # Or your preferred driver
        self.driver = create_retryable_driver(original_driver, self.max_retries)
        return self.driver

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.driver:
            self.driver.quit()


# Example of how to use it in your existing code:
def example_automation():
    # Option 1: Direct usage
    driver = setup_driver()
    try:
        # Your existing code remains largely unchanged
        driver.get("https://example.com")
        element = driver.find_element(By.ID, "some-id")
        element.click()
    finally:
        driver.quit()

    # Option 2: Using context manager
    with WebDriverManager() as driver:
        # Your existing code remains unchanged
        driver.get("https://example.com")
        element = driver.find_element(By.ID, "some-id")
        element.click()


# Example with more complex scenarios
def complex_automation_example():
    with WebDriverManager(max_retries=5) as driver:
        try:
            # All these operations will automatically retry on failure
            driver.get("https://example.com")

            # Find and interact with elements
            login_button = driver.find_element(By.ID, "login")
            login_button.click()

            # Handle dynamic elements
            username_field = driver.find_element(By.NAME, "username")
            username_field.send_keys("user@example.com")

            # Even chained operations are handled
            driver.find_element(By.CLASS_NAME, "submit-btn").click()

        except Exception as e:
            print(f"Automation failed after retries: {e}")
            raise


# Example with custom retry logic for specific operations
def custom_retry_example():
    with WebDriverManager() as driver:
        def custom_action():
            elements = driver.find_elements(By.CLASS_NAME, "dynamic-content")
            for element in elements:
                # Each interaction will have retry capability
                element.click()
                element.send_keys("test")

        try:
            driver.get("https://example.com")
            custom_action()
        except Exception as e:
            print(f"Failed to complete custom action: {e}")
