"""Unit tests for the article-editor selector ladder (issues #747, #771).

The ladder is exercised against a stub DOM so no live LinkedIn session is needed. Each test checks
that a step's fallback routes are tried in order, that disabled buttons are skipped, and that the
map reports a precise `failed_step`.
"""

from unittest.mock import MagicMock

import pytest
from selenium.common import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By

from cqc_lem.utilities.linkedin import article_editor as ae

pytestmark = pytest.mark.unit


class FakeElement:
    """A minimal WebElement stand-in with configurable attributes and display state."""

    def __init__(self, tag: str = "button", attrs: dict = None, displayed: bool = True,
                 interact_ok: bool = True, *, tag_name_exc=None, get_attr_exc: dict = None,
                 is_displayed_exc=None, clear_exc=None, click_exc=None, send_keys_exc=None):
        self.tag = tag
        self.attrs = attrs or {}
        self._displayed = displayed
        self._interact_ok = interact_ok
        self.tag_name_exc = tag_name_exc
        self.get_attr_exc = get_attr_exc or {}
        self.is_displayed_exc = is_displayed_exc
        self.clear_exc = clear_exc
        self.click_exc = click_exc
        self.send_keys_exc = send_keys_exc
        self.clicked = 0
        self.sent = []
        self.cleared = 0

    def is_displayed(self):
        if self.is_displayed_exc:
            raise self.is_displayed_exc
        return self._displayed

    def get_attribute(self, name):
        if name in self.get_attr_exc:
            raise self.get_attr_exc[name]
        return self.attrs.get(name)

    @property
    def tag_name(self):
        if self.tag_name_exc:
            raise self.tag_name_exc
        return self.tag

    def click(self):
        if self.click_exc:
            raise self.click_exc
        if not self._interact_ok:
            raise RuntimeError("not interactable")
        self.clicked += 1

    def clear(self):
        if self.clear_exc:
            raise self.clear_exc
        self.cleared += 1

    def send_keys(self, *keys):
        if self.send_keys_exc:
            raise self.send_keys_exc
        self.sent.extend(keys)


class FakeDriver:
    """Driver whose DOM is a dict of (By, value) -> list[FakeElement]."""

    def __init__(self, dom: dict = None, *, execute_script_exc=None):
        self.dom = dom or {}
        self.scripts = []
        self.url = None
        self.current_url = "https://www.linkedin.com/article/new/"
        self.execute_script_exc = execute_script_exc

    def get(self, url):
        self.url = url

    def find_elements(self, by, value):
        return self.dom.get((by, value), [])

    def execute_script(self, script, *args):
        if self.execute_script_exc:
            raise self.execute_script_exc
        self.scripts.append((script, args))
        if "arguments[0].click()" in script:
            args[0].click()
        return None


class TestResolveArticleEditorStep:
    def test_title_placeholder_route_wins_first(self, monkeypatch):
        title_field = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        aria_field = FakeElement(tag="textarea", attrs={"aria-label": "Title"})
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title_field],
            (By.CSS_SELECTOR, "textarea[aria-label*='Title']"): [aria_field],
        })
        wait = MagicMock()
        monkeypatch.setattr(ae, "find_first",
                            lambda *a, **k: driver.find_elements(a[2][0][0], a[2][0][1])[0]
                            if driver.find_elements(a[2][0][0], a[2][0][1]) else None)
        result = ae.resolve_article_editor_step(driver, wait, ae.STEP_TITLE, ae._TITLE_LOCATORS)
        assert result.ok
        assert result.route == ae.ROUTE_TITLE_PLACEHOLDER
        assert result.element is title_field

    def test_title_falls_back_to_aria_when_placeholder_missing(self, monkeypatch):
        aria_field = FakeElement(tag="textarea", attrs={"aria-label": "Title"})
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [],
            (By.CSS_SELECTOR, "input[placeholder='Title']"): [],
            (By.CSS_SELECTOR, "textarea[aria-label*='Title']"): [aria_field],
        })
        wait = MagicMock()

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        result = ae.resolve_article_editor_step(driver, wait, ae.STEP_TITLE, ae._TITLE_LOCATORS)
        assert result.ok
        assert result.route == ae.ROUTE_TITLE_ARIA

    def test_disabled_button_is_skipped_to_enabled_route(self, monkeypatch):
        disabled_next = FakeElement(tag="button", attrs={"disabled": "true"})
        enabled_next = FakeElement(tag="button", attrs={"aria-label": "Next step"})
        driver = FakeDriver({
            (By.XPATH, "//button[normalize-space()='Next']"): [disabled_next],
            (By.CSS_SELECTOR, "button.artdeco-button--primary"): [],
            (By.CSS_SELECTOR, "button[aria-label*='Next']"): [enabled_next],
        })
        wait = MagicMock()

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        result = ae.resolve_article_editor_step(driver, wait, ae.STEP_NEXT, ae._NEXT_LOCATORS)
        assert result.ok
        assert result.route == ae.ROUTE_NEXT_ARIA
        assert result.element is enabled_next

    def test_all_missing_returns_missing_with_tried_routes(self, monkeypatch):
        driver = FakeDriver()
        wait = MagicMock()
        monkeypatch.setattr(ae, "find_first", lambda *a, **k: None)
        result = ae.resolve_article_editor_step(driver, wait, ae.STEP_PUBLISH, ae._PUBLISH_LOCATORS)
        assert not result.ok
        assert result.route is None
        assert result.element is None
        assert set(result.tried) == {ae.ROUTE_PUBLISH_TEXT, ae.ROUTE_PUBLISH_ARIA}

    def test_not_displayed_element_is_skipped(self, monkeypatch):
        hidden = FakeElement(tag="textarea", attrs={"placeholder": "Title"}, displayed=False)
        driver = FakeDriver({(By.CSS_SELECTOR, "textarea[placeholder='Title']"): [hidden]})
        wait = MagicMock()
        monkeypatch.setattr(
            ae, "find_first",
            lambda *a, **k: driver.find_elements(a[2][0][0], a[2][0][1])[0]
            if driver.find_elements(a[2][0][0], a[2][0][1]) else None)
        result = ae.resolve_article_editor_step(driver, wait, ae.STEP_TITLE, ae._TITLE_LOCATORS)
        assert not result.ok
        assert ae.ROUTE_TITLE_PLACEHOLDER in result.tried
        assert result.element is None


class TestElementHelpers:
    def test_is_enabled_true_when_tag_name_raises(self):
        el = FakeElement(tag_name_exc=WebDriverException("boom"))
        assert ae._is_enabled(el) is True

    def test_is_enabled_true_when_disabled_attr_raises(self):
        el = FakeElement(tag="button", get_attr_exc={"disabled": WebDriverException("boom")})
        assert ae._is_enabled(el) is True

    def test_is_enabled_false_when_disabled_attr_set(self):
        el = FakeElement(tag="button", attrs={"disabled": "true"})
        assert ae._is_enabled(el) is False

    def test_is_enabled_false_when_aria_disabled_true(self):
        el = FakeElement(tag="button", attrs={"aria-disabled": "true"})
        assert ae._is_enabled(el) is False

    def test_is_enabled_true_when_aria_disabled_attr_raises(self):
        el = FakeElement(tag="button", attrs={"disabled": "false"},
                         get_attr_exc={"aria-disabled": WebDriverException("boom")})
        assert ae._is_enabled(el) is True

    def test_is_displayed_safe_false_on_exception(self):
        el = FakeElement(is_displayed_exc=WebDriverException("boom"))
        assert ae._is_displayed_safe(el) is False

    def test_click_safely_falls_back_to_js_click(self):
        el = FakeElement(click_exc=WebDriverException("boom"))
        driver = FakeDriver()
        # Bypass the driver-side script-to-click helper so the JS click path is exercised cleanly.
        def js_click_only(script, *args):
            driver.scripts.append((script, args))
            if "arguments[0].click()" in script:
                args[0].clicked += 1
            return None
        driver.execute_script = js_click_only
        assert ae._click_safely(driver, el) is True
        assert el.clicked == 1
        assert any("arguments[0].click()" in s for s, _ in driver.scripts)

    def test_click_safely_false_when_js_click_also_fails(self):
        el = FakeElement(click_exc=WebDriverException("boom"))
        driver = FakeDriver(execute_script_exc=WebDriverException("boom"))
        assert ae._click_safely(driver, el) is False

    def test_type_safely_swallows_clear_exception(self, monkeypatch):
        el = FakeElement(tag="textarea", clear_exc=WebDriverException("boom"))
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)
        ae._type_safely(el, "text")
        assert el.sent == ["text"]
        assert el.cleared == 0

    def test_type_safely_logs_and_reraises_field_exception(self, monkeypatch):
        el = FakeElement(tag="textarea", click_exc=WebDriverException("boom"))
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)
        with pytest.raises(WebDriverException):
            ae._type_safely(el, "text")

    def test_type_safely_handles_stale_element_on_clear(self, monkeypatch):
        el = FakeElement(tag="textarea", clear_exc=StaleElementReferenceException("stale"))
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)
        ae._type_safely(el, "text")
        assert el.sent == ["text"]


class TestFindArticleEditorElements:
    def test_map_reports_first_missing_step(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox", "aria-label": "Article editor"})
        nxt = FakeElement(tag="button", attrs={"aria-label": "Next"})
        publish = FakeElement(tag="button", attrs={"aria-label": "Publish"})
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox'][aria-label*='Article editor']"): [body],
            (By.CSS_SELECTOR, "button[aria-label*='Next']"): [nxt],
            (By.CSS_SELECTOR, "button[aria-label*='Publish']"): [publish],
        })
        wait = MagicMock()

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        editor_map = ae.find_article_editor_elements(driver, wait)
        assert editor_map.title.ok
        assert editor_map.body.ok
        assert editor_map.next_button.ok
        assert editor_map.publish_button.ok
        assert editor_map.first_missing() is None

    def test_missing_body_reports_article_body(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        nxt = FakeElement(tag="button", attrs={"aria-label": "Next"})
        publish = FakeElement(tag="button", attrs={"aria-label": "Publish"})
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox'][aria-label*='Article editor']"): [],
            (By.CSS_SELECTOR, "div[role='textbox']"): [],
            (By.CSS_SELECTOR, "div[contenteditable='true']"): [],
            (By.CSS_SELECTOR, "button[aria-label*='Next']"): [nxt],
            (By.CSS_SELECTOR, "button[aria-label*='Publish']"): [publish],
        })
        wait = MagicMock()

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        editor_map = ae.find_article_editor_elements(driver, wait)
        assert editor_map.first_missing() == ae.STEP_BODY


class TestArticleEditorVerdict:
    def test_all_ok_verdict(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button", attrs={"aria-label": "Next"})
        publish = FakeElement(tag="button", attrs={"aria-label": "Publish"})
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.CSS_SELECTOR, "button[aria-label*='Next']"): [nxt],
            (By.CSS_SELECTOR, "button[aria-label*='Publish']"): [publish],
        })
        wait = MagicMock()

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        verdict = ae.article_editor_verdict(ae.find_article_editor_elements(driver, wait))
        assert verdict["all_ok"] is True
        assert verdict["first_missing"] is None
        for step_dict in (verdict["title"], verdict["body"], verdict["next"], verdict["publish"]):
            assert step_dict["verdict"] == ae.StepVerdict.OK

    def test_missing_publish_has_correct_verdict(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button", attrs={"aria-label": "Next"})
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.CSS_SELECTOR, "button[aria-label*='Next']"): [nxt],
            (By.XPATH, "//button[normalize-space()='Publish']"): [],
            (By.CSS_SELECTOR, "button[aria-label*='Publish']"): [],
        })
        wait = MagicMock()

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        verdict = ae.article_editor_verdict(ae.find_article_editor_elements(driver, wait))
        assert verdict["all_ok"] is False
        assert verdict["first_missing"] == ae.STEP_PUBLISH
        assert verdict["publish"]["verdict"] == ae.StepVerdict.MISSING
        assert verdict["publish"]["tried"] == [ae.ROUTE_PUBLISH_TEXT, ae.ROUTE_PUBLISH_ARIA]


class TestFillArticleEditor:
    def test_returns_failed_step_when_title_missing(self, monkeypatch):
        monkeypatch.setattr(ae, "find_first", lambda *a, **k: None)
        driver = FakeDriver()
        wait = MagicMock()
        url, failed = ae.fill_article_editor(driver, wait, "title", "body")
        assert url is None
        assert failed == ae.STEP_TITLE

    def test_returns_url_after_clicking_next_and_publish(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button")
        publish = FakeElement(tag="button")
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
            (By.XPATH, "//button[normalize-space()='Publish']"): [publish],
        })
        wait = MagicMock()
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        url, failed = ae.fill_article_editor(driver, wait, "My title", "My body", user_id=1)
        assert url == driver.current_url
        assert failed is None
        assert title.sent == ["My title"]
        assert body.sent == ["My body"]
        assert nxt.clicked == 1
        assert publish.clicked == 1

    def test_returns_publish_step_when_publish_button_disabled(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button")
        publish = FakeElement(tag="button", attrs={"disabled": "true"})
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
            (By.XPATH, "//button[normalize-space()='Publish']"): [publish],
            (By.CSS_SELECTOR, "button[aria-label*='Publish']"): [],
        })
        wait = MagicMock()
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        url, failed = ae.fill_article_editor(driver, wait, "title", "body")
        assert url is None
        assert failed == ae.STEP_PUBLISH

    def test_returns_step_title_when_typing_fails_and_all_still_present(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"},
                           send_keys_exc=WebDriverException("boom"))
        nxt = FakeElement(tag="button")
        publish = FakeElement(tag="button")
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
            (By.XPATH, "//button[normalize-space()='Publish']"): [publish],
        })
        wait = MagicMock()
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        url, failed = ae.fill_article_editor(driver, wait, "title", "body")
        assert url is None
        assert failed == ae.STEP_TITLE

    def test_returns_next_step_when_next_click_fails(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button", click_exc=WebDriverException("boom"))
        publish = FakeElement(tag="button")
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
            (By.XPATH, "//button[normalize-space()='Publish']"): [publish],
        }, execute_script_exc=WebDriverException("boom"))
        wait = MagicMock()
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        url, failed = ae.fill_article_editor(driver, wait, "title", "body")
        assert url is None
        assert failed == ae.STEP_NEXT

    def test_continues_when_description_fn_raises(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button")
        publish = FakeElement(tag="button")
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
            (By.XPATH, "//button[normalize-space()='Publish']"): [publish],
        })
        wait = MagicMock()
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)

        def bad_fill(d, w, subtitle):
            raise ValueError("description fill failed")

        url, failed = ae.fill_article_editor(driver, wait, "title", "body",
                                              subtitle="sub", fill_description_fn=bad_fill)
        assert url == driver.current_url
        assert failed is None

    def test_returns_publish_when_publish_button_missing_after_next(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button")
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
            (By.XPATH, "//button[normalize-space()='Publish']"): [],
            (By.CSS_SELECTOR, "button[aria-label*='Publish']"): [],
        })
        wait = MagicMock()
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        url, failed = ae.fill_article_editor(driver, wait, "title", "body")
        assert url is None
        assert failed == ae.STEP_PUBLISH

    def test_returns_publish_when_publish_click_fails(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button")
        publish = FakeElement(tag="button", click_exc=WebDriverException("boom"))
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
            (By.XPATH, "//button[normalize-space()='Publish']"): [publish],
        }, execute_script_exc=WebDriverException("boom"))
        wait = MagicMock()
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        url, failed = ae.fill_article_editor(driver, wait, "title", "body")
        assert url is None
        assert failed == ae.STEP_PUBLISH

    def test_returns_publish_when_reresolved_publish_missing(self, monkeypatch):
        """Cover the return on line 360 when the publish button is missing after Next."""
        calls = []
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button")
        publish = FakeElement(tag="button")
        publish_missing = ae.ResolvedElement(step=ae.STEP_PUBLISH)

        def patched_resolve(driver, wait, step, locators, *, user_id=None, visible_only=True):
            calls.append(step)
            if step == ae.STEP_PUBLISH and calls.count(ae.STEP_PUBLISH) >= 2:
                return publish_missing
            if step == ae.STEP_TITLE:
                return ae.ResolvedElement(step=ae.STEP_TITLE, element=title,
                                          route=ae.ROUTE_TITLE_PLACEHOLDER, enabled=True)
            if step == ae.STEP_BODY:
                return ae.ResolvedElement(step=ae.STEP_BODY, element=body,
                                          route=ae.ROUTE_BODY_ROLE, enabled=True)
            if step == ae.STEP_NEXT:
                return ae.ResolvedElement(step=ae.STEP_NEXT, element=nxt,
                                          route=ae.ROUTE_NEXT_TEXT, enabled=True)
            return ae.ResolvedElement(step=ae.STEP_PUBLISH, element=publish,
                                      route=ae.ROUTE_PUBLISH_TEXT, enabled=True)

        monkeypatch.setattr(ae, "resolve_article_editor_step", patched_resolve)
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)
        driver = FakeDriver()
        wait = MagicMock()
        url, failed = ae.fill_article_editor(driver, wait, "title", "body")
        assert url is None
        assert failed == ae.STEP_PUBLISH


class TestLiveValidationProbe:
    def test_probe_article_editor_reports_editor_verdict(self, monkeypatch):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button", attrs={"aria-label": "Next"})
        publish = FakeElement(tag="button", attrs={"aria-label": "Publish"})
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.CSS_SELECTOR, "button[aria-label*='Next']"): [nxt],
            (By.CSS_SELECTOR, "button[aria-label*='Publish']"): [publish],
        })

        import importlib.util
        from pathlib import Path
        script_path = Path(__file__).resolve().parents[4] / "scripts" / "linkedin_live_validation.py"
        spec = importlib.util.spec_from_file_location("linkedin_live_validation", script_path)
        llv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(llv)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        report = llv.probe_article_editor(driver, "https://www.linkedin.com/article/new/",
                                          sleep=lambda s: None)
        assert report["all_ok"] is True
        assert report["first_missing"] is None
