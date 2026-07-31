"""LinkedIn article editor selector resolution map — issues #406, #747, #771, #804.

LinkedIn's article editor is a multi-step SDUI flow that rotates the DOM anchors for its
title field, content body, Next button, and Publish button. This module is the selector ladder
for that editor: four resolution steps, each with two or more independent routes, ordered by
the TEXT / aria-label / role / placeholder attributes LinkedIn is least likely to churn.

A step only succeeds when the element is found and is actionable (visible for fields,
enabled for buttons). The ladder returns the element that won and the route id, so callers
log a precise `failed_step` and so live validation can report which route resolved today.

**The editor is TWO screens, and that is the whole reason this module grades per screen.**
`/article/new/` renders the title, the body and Next — and NOT Publish, which only exists in
the "Ready to publish?" dialog that Next opens. The 2026-07-31 live validation (#804) confirmed
it: title/body/next resolved, publish resolved by no route at all. So a step that simply is not
on the current screen is `UNKNOWN`, never `MISSING` — and the pre-flight gate before typing may
only require `EDITOR_SCREEN_STEPS`. Requiring all four up front made every publish abort before
it typed a character, reporting `failed_step=article_publish` for a button that was never
supposed to be there yet.

No class names are keyed on. Use `resolve_article_editor_step(...)` directly, or
`find_article_editor_elements(driver, wait)` to resolve all four fields/buttons at once.
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from selenium.common import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.wait import WebDriverWait

from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.selenium_util import find_first

STEP_TITLE = "article_title"
STEP_BODY = "article_body"
STEP_NEXT = "article_next"
STEP_PUBLISH = "article_publish"

# Which steps each screen of the flow is allowed to require. Publish is deliberately NOT in
# EDITOR_SCREEN_STEPS: it renders in the dialog Next opens, so demanding it on the editor screen
# grades a correct page as broken (and, before #804, aborted the publish flow outright).
EDITOR_SCREEN_STEPS = (STEP_TITLE, STEP_BODY, STEP_NEXT)
PUBLISH_SCREEN_STEPS = (STEP_PUBLISH,)
ALL_STEPS = EDITOR_SCREEN_STEPS + PUBLISH_SCREEN_STEPS

# Stable route ids used in telemetry. "primary" / "secondary" refer to LinkedIn's two action
# styles for the Next button; "text" means the literal button text.
ROUTE_TITLE_PLACEHOLDER = "title_placeholder"
ROUTE_TITLE_ARIA = "title_aria"
ROUTE_TITLE_LABEL = "title_label"
ROUTE_BODY_ARIA = "body_aria"
ROUTE_BODY_ROLE = "body_role"
ROUTE_BODY_LABEL = "body_label"
ROUTE_NEXT_PRIMARY = "next_primary"
ROUTE_NEXT_TEXT = "next_text"
ROUTE_NEXT_ARIA = "next_aria"
ROUTE_PUBLISH_TEXT = "publish_text"
ROUTE_PUBLISH_ARIA = "publish_aria"

# Ordered fallback chains. Every locator avoids hashed class names and keys on visible text,
# aria-label, role, or placeholder. XPath uses normalize-space() so whitespace won't break.
_TITLE_LOCATORS: list[tuple[str, list[tuple[str, str]], str]] = [
    (ROUTE_TITLE_PLACEHOLDER, [
        (By.CSS_SELECTOR, "textarea[placeholder='Title']"),
        (By.CSS_SELECTOR, "input[placeholder='Title']"),
        (By.XPATH, "//textarea[contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                   "'abcdefghijklmnopqrstuvwxyz'),'title')]"),
        (By.XPATH, "//input[contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                  "'abcdefghijklmnopqrstuvwxyz'),'title')]"),
    ]),
    (ROUTE_TITLE_ARIA, [
        (By.CSS_SELECTOR, "textarea[aria-label*='Title']"),
        (By.CSS_SELECTOR, "input[aria-label*='Title']"),
        (By.XPATH, "//textarea[contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                   "'abcdefghijklmnopqrstuvwxyz'),'title')]"),
        (By.XPATH, "//input[contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                  "'abcdefghijklmnopqrstuvwxyz'),'title')]"),
    ]),
    (ROUTE_TITLE_LABEL, [
        (By.XPATH, "//*[normalize-space(text())='Title']"
                   "/ancestor-or-self::*[self::textarea or self::input][1]"),
        (By.XPATH, "//label[normalize-space()='Title']/textarea"),
        (By.XPATH, "//label[normalize-space()='Title']/input"),
    ]),
]

_BODY_LOCATORS: list[tuple[str, list[tuple[str, str]], str]] = [
    (ROUTE_BODY_ARIA, [
        (By.CSS_SELECTOR, "div[role='textbox'][aria-label*='Article editor']"),
        (By.CSS_SELECTOR, "div[role='textbox'][aria-label*='article']"),
        (By.XPATH, "//div[@role='textbox' and contains(translate(@aria-label,"
                   "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'article')]"),
    ]),
    (ROUTE_BODY_ROLE, [
        (By.CSS_SELECTOR, "div[role='textbox']"),
        (By.CSS_SELECTOR, "div[contenteditable='true']"),
        (By.XPATH, "//div[@contenteditable='true']"),
    ]),
    (ROUTE_BODY_LABEL, [
        (By.XPATH, "//*[normalize-space(text())='Article body']"
                   "/ancestor-or-self::div[@role='textbox' or @contenteditable='true'][1]"),
        (By.XPATH, "//label[normalize-space()='Body']/div[@role='textbox' or @contenteditable='true']"),
    ]),
]

_NEXT_LOCATORS: list[tuple[str, list[tuple[str, str]], str]] = [
    (ROUTE_NEXT_TEXT, [
        (By.XPATH, "//button[normalize-space()='Next']"),
        (By.XPATH, "//button[.//span[normalize-space()='Next']]"),
    ]),
    (ROUTE_NEXT_PRIMARY, [
        (By.CSS_SELECTOR, "button.artdeco-button--primary"),
        (By.XPATH, "//button[@type='button' and not(@disabled)][last()]"),
    ]),
    (ROUTE_NEXT_ARIA, [
        (By.CSS_SELECTOR, "button[aria-label*='Next']"),
        (By.XPATH, "//button[contains(translate(@aria-label,"
                   "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'next')]"),
    ]),
]

_PUBLISH_LOCATORS: list[tuple[str, list[tuple[str, str]], str]] = [
    (ROUTE_PUBLISH_TEXT, [
        (By.XPATH, "//button[normalize-space()='Publish']"),
        (By.XPATH, "//button[.//span[normalize-space()='Publish']]"),
    ]),
    (ROUTE_PUBLISH_ARIA, [
        (By.CSS_SELECTOR, "button[aria-label*='Publish']"),
        (By.XPATH, "//button[contains(translate(@aria-label,"
                   "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'publish')]"),
    ]),
]


class StepVerdict(str, Enum):
    OK = "OK"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


@dataclass
class ResolvedElement:
    """The outcome of one selector-ladder step."""
    step: str
    element: Optional[WebElement] = None
    route: Optional[str] = None
    enabled: bool = True
    tried: list[str] = None
    # False when this step's control does not belong on the screen that was just read. An absent
    # control is then UNKNOWN ("nothing to say yet"), not MISSING ("every route failed") — the
    # difference between a selector that needs fixing and a page that is behaving correctly.
    expected_on_screen: bool = True

    def __post_init__(self):
        if self.tried is None:
            self.tried = []

    @property
    def ok(self) -> bool:
        return self.element is not None and self.enabled

    @property
    def verdict(self) -> StepVerdict:
        if self.ok:
            return StepVerdict.OK
        return StepVerdict.MISSING if self.expected_on_screen else StepVerdict.UNKNOWN

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "verdict": self.verdict,
            "route": self.route,
            "enabled": self.enabled,
            "tried": list(self.tried),
        }


def _is_enabled(element: WebElement) -> bool:
    """Best-effort enabled check for buttons; fields are always considered enabled."""
    try:
        tag = (element.tag_name or "").lower()
    except (WebDriverException, StaleElementReferenceException):
        return True
    if tag != "button":
        return True
    try:
        disabled_attr = element.get_attribute("disabled")
    except (WebDriverException, StaleElementReferenceException):
        return True
    if disabled_attr is not None and str(disabled_attr).lower() in {"true", "disabled", "1"}:
        return False
    try:
        aria_disabled = element.get_attribute("aria-disabled")
    except (WebDriverException, StaleElementReferenceException):
        aria_disabled = None
    if aria_disabled is not None and str(aria_disabled).lower() == "true":
        return False
    return True


def _is_displayed_safe(element: WebElement) -> bool:
    try:
        return element.is_displayed()
    except (WebDriverException, StaleElementReferenceException):
        return False


def resolve_article_editor_step(
    driver: WebDriver,
    wait: WebDriverWait,
    step: str,
    locators: list[tuple[str, list[tuple[str, str]], str]],
    *,
    user_id: Optional[int] = None,
    visible_only: bool = True,
) -> ResolvedElement:
    """Walk one step's locator ladder and return the first actionable element + route.

    Each step contributes at least two independent routes; a route only counts when its
    element is found and, for buttons, enabled. `failed_step` callers log the route id
    so telemetry shows which entry points LinkedIn is rotating.
    """
    result = ResolvedElement(step=step)
    for route_id, route_locators in locators:
        result.tried.append(route_id)
        element = find_first(
            driver, wait, route_locators,
            label=f"Article editor {step}",
            required=False,
            max_try=1,
            visible_only=visible_only,
            user_id=user_id,
        )
        if element is not None:
            if not _is_displayed_safe(element):
                log_debug(f"Article editor route found but not visible",
                          step=step, route=route_id, user_id=user_id, action_type="article_editor")
                continue
            enabled = _is_enabled(element)
            if enabled:
                result.element = element
                result.route = route_id
                result.enabled = True
                log_debug(f"Article editor step resolved",
                          step=step, route=route_id, user_id=user_id, action_type="article_editor")
                return result
            result.enabled = False
            # Keep walking: a disabled primary route is not a win, but a later route may be enabled.
    return result


@dataclass
class ArticleEditorMap:
    title: ResolvedElement
    body: ResolvedElement
    next_button: ResolvedElement
    publish_button: ResolvedElement

    def steps(self) -> "tuple[ResolvedElement, ...]":
        return (self.title, self.body, self.next_button, self.publish_button)

    def first_missing(self, steps: "tuple[str, ...] | None" = None) -> Optional[str]:
        """The first unresolved step, restricted to `steps` when given.

        Callers that are on the editor screen pass `EDITOR_SCREEN_STEPS`; passing nothing keeps
        the whole-flow view used for reporting.
        """
        wanted = steps if steps is not None else ALL_STEPS
        for resolved in self.steps():
            if resolved.step in wanted and not resolved.ok:
                return resolved.step
        return None

    def to_dict(self) -> dict:
        return {
            "title": self.title.to_dict(),
            "body": self.body.to_dict(),
            "next": self.next_button.to_dict(),
            "publish": self.publish_button.to_dict(),
        }


def find_article_editor_elements(
    driver: WebDriver,
    wait: WebDriverWait,
    *,
    user_id: Optional[int] = None,
) -> ArticleEditorMap:
    """Resolve all four article-editor steps at once."""
    return ArticleEditorMap(
        title=resolve_article_editor_step(driver, wait, STEP_TITLE, _TITLE_LOCATORS,
                                           user_id=user_id),
        body=resolve_article_editor_step(driver, wait, STEP_BODY, _BODY_LOCATORS,
                                         user_id=user_id),
        next_button=resolve_article_editor_step(driver, wait, STEP_NEXT, _NEXT_LOCATORS,
                                                 user_id=user_id),
        publish_button=resolve_article_editor_step(driver, wait, STEP_PUBLISH, _PUBLISH_LOCATORS,
                                                    user_id=user_id),
    )


def article_editor_verdict(editor_map: ArticleEditorMap, *,
                           on_editor_screen: bool = False) -> dict:
    """Report the reply-state-style verdict used by live validation: OK / MISSING / UNKNOWN.

    `on_editor_screen=True` is what a READ-ONLY probe passes: it has opened `/article/new/` and
    will not click Next, so the publish dialog was never rendered. Publish is then graded UNKNOWN
    and excluded from `all_ok`, because "we did not look" is not evidence of a broken selector.
    The default grades all four, which is the whole-flow view.
    """
    if on_editor_screen:
        editor_map.publish_button.expected_on_screen = False
    graded = EDITOR_SCREEN_STEPS if on_editor_screen else ALL_STEPS
    first_missing = editor_map.first_missing(graded)
    out = editor_map.to_dict()
    out["first_missing"] = first_missing
    out["all_ok"] = first_missing is None
    out["graded_steps"] = list(graded)
    out["editor_ready"] = editor_map.first_missing(EDITOR_SCREEN_STEPS) is None
    return out


def _type_safely(element: WebElement, text: str) -> None:
    """Click, clear, and send keys to a field, swallowing stale/interact exceptions."""
    try:
        element.click()
        time.sleep(0.3)
        # Best-effort clear: contenteditable bodies don't always have .clear(), so send Ctrl+a
        # then Backspace; then fall back to the WebElement clear API.
        try:
            element.clear()
        except (WebDriverException, StaleElementReferenceException):
            pass  # contenteditable / custom fields may not implement clear(); send_keys overwrites below.
        element.send_keys(text)
    except (WebDriverException, StaleElementReferenceException) as e:
        log_warning("Article editor field interaction failed", exc=e, action_type="article_editor")
        raise


def _click_safely(driver: WebDriver, element: WebElement) -> bool:
    """Click an element, falling back to a JS click when native click is intercepted."""
    try:
        element.click()
        return True
    except (WebDriverException, StaleElementReferenceException):
        try:
            driver.execute_script("arguments[0].click();", element)
            return True
        except (WebDriverException, StaleElementReferenceException):
            return False


def fill_article_editor(
    driver: WebDriver,
    wait: WebDriverWait,
    title: str,
    body: str,
    *,
    user_id: Optional[int] = None,
    subtitle: Optional[str] = None,
    fill_description_fn=None,
) -> "tuple[str | None, str | None]":
    """Use the selector ladder to fill LinkedIn's article editor and publish.

    Returns ``(published_url, None)`` on success or ``(None, failed_step)`` on failure.
    ``failed_step`` is one of the STEP_* constants so callers can emit an actionable
    log_error with `failed_step=...`.
    """
    editor = find_article_editor_elements(driver, wait, user_id=user_id)
    # Only the EDITOR screen's controls gate the start of the flow. Publish is re-resolved after
    # Next below, where it actually exists — gating on it here aborted every publish before a
    # character was typed and blamed `article_publish` for it (#804).
    failed = editor.first_missing(EDITOR_SCREEN_STEPS)
    if failed:
        log_warning("Article editor step missing", step=failed, user_id=user_id,
                    action_type="article_editor", routes=editor.to_dict())
        return None, failed

    try:
        _type_safely(editor.title.element, title)
        time.sleep(1)
        _type_safely(editor.body.element, body)
        time.sleep(2)
    except (WebDriverException, StaleElementReferenceException) as e:
        log_warning("Article editor typing failed", exc=e, user_id=user_id,
                    action_type="article_editor")
        # Distinguish which field likely failed by re-checking after exception. Still on the editor
        # screen, so Publish is out of scope here too — reporting it would blame the dialog for a
        # typing failure that happened two steps earlier.
        editor = find_article_editor_elements(driver, wait, user_id=user_id)
        return None, editor.first_missing(EDITOR_SCREEN_STEPS) or STEP_TITLE

    if not _click_safely(driver, editor.next_button.element):
        return None, STEP_NEXT
    time.sleep(3)

    if fill_description_fn is not None:
        try:
            fill_description_fn(driver, wait, subtitle)
        except Exception as e:
            log_warning("Edition description fill failed, continuing to publish",
                        exc=e, user_id=user_id, action_type="article_editor")

    # Re-resolve the publish button because LinkedIn may repaint after Next.
    publish_step = resolve_article_editor_step(driver, wait, STEP_PUBLISH, _PUBLISH_LOCATORS,
                                             user_id=user_id)
    if not publish_step.ok:
        return None, STEP_PUBLISH

    if not _click_safely(driver, publish_step.element):
        # The element was found and enabled, but the click itself failed (intercepted/stale).
        return None, STEP_PUBLISH
    time.sleep(5)
    return driver.current_url, None
