"""Unit tests for the article-editor selector ladder (issues #747, #771).

The ladder is exercised against a stub DOM so no live LinkedIn session is needed. Each test checks
that a step's fallback routes are tried in order, that disabled buttons are skipped, and that the
map reports a precise `failed_step`.
"""

import os
from unittest.mock import MagicMock

import pytest
from selenium.common import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By

from cqc_lem.utilities.linkedin import article_editor as ae

pytestmark = pytest.mark.unit


def _load_probe_module():
    """Load `scripts/linkedin_live_validation.py` by path — `scripts/` is not an importable package
    (and is not baked into the runtime image), so the probe is exercised the same way it ships.
    """
    import importlib.util
    from pathlib import Path
    script_path = Path(__file__).resolve().parents[4] / "scripts" / "linkedin_live_validation.py"
    spec = importlib.util.spec_from_file_location("linkedin_live_validation", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeElement:
    """A minimal WebElement stand-in with configurable attributes and display state."""

    def __init__(self, tag: str = "button", attrs: dict = None, displayed: bool = True,
                 interact_ok: bool = True, *, text: str = "", tag_name_exc=None,
                 get_attr_exc: dict = None,
                 is_displayed_exc=None, clear_exc=None, click_exc=None, send_keys_exc=None):
        self.tag = tag
        self.text = text
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

    def test_visible_only_false_keeps_a_hidden_element(self, monkeypatch):
        # The cover's <input type=file> is hidden BY DESIGN — a visibility filter here would
        # reject the only element Selenium can drive and every cover attach would miss.
        hidden_input = FakeElement(tag="input", attrs={"type": "file", "accept": "image/*"},
                                   displayed=False)
        driver = FakeDriver({
            (By.XPATH, "//input[@type='file' and contains(translate(@accept,"
                       "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'image')]"):
                [hidden_input],
        })

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        result = ae.resolve_article_editor_step(driver, MagicMock(), ae.STEP_COVER,
                                                ae._COVER_INPUT_LOCATORS, visible_only=False)
        assert result.ok
        assert result.element is hidden_input


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

        def patched_resolve(driver, wait, step, locators, *, user_id=None, visible_only=True,
                            warn_on_miss=True):
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

    def test_warn_on_miss_is_false_for_initial_publish_resolution(self, monkeypatch):
        """The first Publish resolve must not warn; the one after Next still must.

        The initial `find_article_editor_elements` resolve of Publish happens on the editor
        screen, before Next is clicked, so a missing Publish is expected and must not warn.
        The re-resolve after Next still warns so a real selector gap is surfaced.
        """
        captured = []

        def patched_resolve(driver, wait, step, locators, *, user_id=None, visible_only=True,
                            warn_on_miss=True):
            captured.append((step, warn_on_miss))
            return ae.ResolvedElement(step=step)

        monkeypatch.setattr(ae, "resolve_article_editor_step", patched_resolve)
        ae.find_article_editor_elements(FakeDriver(), MagicMock(), user_id=1)
        initial_publish = [call for call in captured
                           if call[0] == ae.STEP_PUBLISH]
        assert len(initial_publish) == 1
        assert initial_publish[0][1] is False


class TestTwoScreenGrading:
    """The editor is two screens (#804, live-validated 2026-07-31).

    `/article/new/` renders title + body + Next and NOT Publish, which only exists in the dialog
    Next opens. These tests pin the two consequences: an off-screen step is UNKNOWN rather than
    MISSING, and the pre-flight gate before typing may only require the editor screen's controls.
    """

    @staticmethod
    def _editor_screen_driver(publish_present: bool = False):
        """A DOM shaped like the real editor screen: title, body and Next, no publish control."""
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox", "aria-label": "Article editor content"})
        nxt = FakeElement(tag="button", attrs={"type": "submit"})
        dom = {
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox'][aria-label*='Article editor']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
        }
        if publish_present:
            dom[(By.XPATH, "//button[normalize-space()='Publish']")] = [
                FakeElement(tag="button")]
        return FakeDriver(dom)

    @staticmethod
    def _ordered_find_first(driver):
        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None
        return fake_find_first

    def test_first_missing_ignores_publish_when_scoped_to_editor_screen(self, monkeypatch):
        driver = self._editor_screen_driver()
        monkeypatch.setattr(ae, "find_first", self._ordered_find_first(driver))
        editor_map = ae.find_article_editor_elements(driver, MagicMock())
        assert editor_map.first_missing() == ae.STEP_PUBLISH
        assert editor_map.first_missing(ae.EDITOR_SCREEN_STEPS) is None

    def test_editor_screen_verdict_grades_publish_unknown_not_missing(self, monkeypatch):
        driver = self._editor_screen_driver()
        monkeypatch.setattr(ae, "find_first", self._ordered_find_first(driver))
        verdict = ae.article_editor_verdict(
            ae.find_article_editor_elements(driver, MagicMock()), on_editor_screen=True)
        assert verdict["publish"]["verdict"] == ae.StepVerdict.UNKNOWN
        assert verdict["all_ok"] is True
        assert verdict["editor_ready"] is True
        assert verdict["first_missing"] is None
        assert verdict["graded_steps"] == list(ae.EDITOR_SCREEN_STEPS)

    def test_whole_flow_verdict_still_grades_absent_publish_missing(self, monkeypatch):
        """The default (not on the editor screen) is unchanged: absent publish is a real miss."""
        driver = self._editor_screen_driver()
        monkeypatch.setattr(ae, "find_first", self._ordered_find_first(driver))
        verdict = ae.article_editor_verdict(ae.find_article_editor_elements(driver, MagicMock()))
        assert verdict["publish"]["verdict"] == ae.StepVerdict.MISSING
        assert verdict["all_ok"] is False
        assert verdict["first_missing"] == ae.STEP_PUBLISH
        assert verdict["editor_ready"] is True

    def test_editor_screen_verdict_reports_a_broken_editor_step(self, monkeypatch):
        """A missing BODY is a real selector gap and must survive the screen-aware grading."""
        driver = self._editor_screen_driver()
        driver.dom[(By.CSS_SELECTOR, "div[role='textbox'][aria-label*='Article editor']")] = []
        monkeypatch.setattr(ae, "find_first", self._ordered_find_first(driver))
        verdict = ae.article_editor_verdict(
            ae.find_article_editor_elements(driver, MagicMock()), on_editor_screen=True)
        assert verdict["first_missing"] == ae.STEP_BODY
        assert verdict["all_ok"] is False
        assert verdict["editor_ready"] is False

    def test_publish_present_on_editor_screen_reads_ok(self, monkeypatch):
        """If LinkedIn ever puts Publish on the first screen, the probe says OK, not UNKNOWN."""
        driver = self._editor_screen_driver(publish_present=True)
        monkeypatch.setattr(ae, "find_first", self._ordered_find_first(driver))
        verdict = ae.article_editor_verdict(
            ae.find_article_editor_elements(driver, MagicMock()), on_editor_screen=True)
        assert verdict["publish"]["verdict"] == ae.StepVerdict.OK

    def test_resolved_element_verdict_is_three_valued(self):
        found = ae.ResolvedElement(step=ae.STEP_NEXT, element=FakeElement(), route=ae.ROUTE_NEXT_TEXT)
        assert found.verdict == ae.StepVerdict.OK
        absent = ae.ResolvedElement(step=ae.STEP_PUBLISH)
        assert absent.verdict == ae.StepVerdict.MISSING
        off_screen = ae.ResolvedElement(step=ae.STEP_PUBLISH, expected_on_screen=False)
        assert off_screen.verdict == ae.StepVerdict.UNKNOWN
        assert off_screen.to_dict()["verdict"] == ae.StepVerdict.UNKNOWN

    def test_fill_proceeds_when_publish_only_appears_after_next(self, monkeypatch):
        """The #804 regression: the pre-flight used to demand Publish on the editor screen, so the
        flow returned `article_publish` without typing a character. It must now type, click Next,
        and only then require Publish.
        """
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox", "aria-label": "Article editor content"})
        nxt = FakeElement(tag="button", attrs={"type": "submit"})
        publish = FakeElement(tag="button")
        driver = self._editor_screen_driver()
        driver.dom[(By.CSS_SELECTOR, "textarea[placeholder='Title']")] = [title]
        driver.dom[(By.CSS_SELECTOR, "div[role='textbox'][aria-label*='Article editor']")] = [body]
        driver.dom[(By.XPATH, "//button[normalize-space()='Next']")] = [nxt]
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                # Publish only exists once Next has been clicked — exactly like the live editor.
                if "Publish" in value and not nxt.clicked:
                    continue
                if "Publish" in value:
                    return publish
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        url, failed = ae.fill_article_editor(driver, MagicMock(), "My title", "My body", user_id=1)
        assert failed is None
        assert url == driver.current_url
        assert title.sent == ["My title"]
        assert body.sent == ["My body"]
        assert nxt.clicked == 1
        assert publish.clicked == 1


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

        llv = _load_probe_module()

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

    def test_probe_grades_absent_publish_unknown_and_records_the_screen(self, monkeypatch):
        """The live 2026-07-31 shape: title/body/next resolve, no publish control on the screen.

        The probe never clicks Next, so publish must read UNKNOWN and must not drag `all_ok` down —
        and the buttons it DID see are what make that grading checkable.
        """
        llv = _load_probe_module()
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox",
                                             "aria-label": "Article editor content"})
        nxt = FakeElement(tag="button", attrs={"type": "submit"}, text="Next")
        driver = FakeDriver({
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox'][aria-label*='Article editor']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
            (By.TAG_NAME, "button"): [nxt],
        })

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)
        report = llv.probe_article_editor(driver, "https://www.linkedin.com/article/new/",
                                          sleep=lambda s: None)
        assert report["publish"]["verdict"] == ae.StepVerdict.UNKNOWN
        assert report["all_ok"] is True
        assert report["editor_ready"] is True
        assert report["first_missing"] is None
        assert report["title"]["element"]["placeholder"] == "Title"
        assert report["body"]["element"]["aria_label"] == "Article editor content"
        assert "Next" in report["buttons"]
        assert "publish is UNKNOWN by design" in report["verdict"]

    def test_probe_reports_a_broken_editor_step_as_not_ready(self, monkeypatch):
        llv = _load_probe_module()
        driver = FakeDriver({(By.TAG_NAME, "button"): []})
        monkeypatch.setattr(ae, "find_first", lambda *a, **k: None)
        report = llv.probe_article_editor(driver, "https://www.linkedin.com/article/new/",
                                          sleep=lambda s: None)
        assert report["editor_ready"] is False
        assert report["first_missing"] == ae.STEP_TITLE
        assert "NOT usable" in report["verdict"]

    def test_probe_reading_calls_out_a_publish_control_on_the_editor_screen(self):
        llv = _load_probe_module()
        reading = llv.article_editor_reading(
            {"editor_ready": True, "publish": {"verdict": "OK"}})
        assert "publish control is present" in reading


class TestProbeEvidenceHelpers:
    def test_visible_button_labels_dedupes_and_skips_hidden(self):
        llv = _load_probe_module()
        shown = FakeElement(tag="button", attrs={"aria-label": "Next"})
        dupe = FakeElement(tag="button", attrs={"aria-label": "Next"})
        hidden = FakeElement(tag="button", attrs={"aria-label": "Publish"}, displayed=False)
        unlabelled = FakeElement(tag="button")
        driver = FakeDriver({(By.TAG_NAME, "button"): [shown, dupe, hidden, unlabelled]})
        assert llv.visible_button_labels(driver) == ["Next"]

    def test_visible_button_labels_honours_the_limit(self):
        llv = _load_probe_module()
        buttons = [FakeElement(tag="button", attrs={"aria-label": f"b{i}"}) for i in range(10)]
        driver = FakeDriver({(By.TAG_NAME, "button"): buttons})
        assert len(llv.visible_button_labels(driver, limit=3)) == 3

    def test_visible_button_labels_reports_a_stopped_enumeration(self):
        llv = _load_probe_module()

        class Exploding(FakeDriver):
            def find_elements(self, by, value):
                raise WebDriverException("boom")

        labels = llv.visible_button_labels(Exploding())
        assert labels and labels[0].startswith("<enumeration stopped")

    def test_element_evidence_is_best_effort(self):
        llv = _load_probe_module()
        el = FakeElement(tag="textarea", attrs={"placeholder": "Title"},
                         get_attr_exc={"aria-label": WebDriverException("boom")})
        evidence = llv.element_evidence(el)
        assert evidence["tag"] == "textarea"
        assert evidence["placeholder"] == "Title"
        assert "aria_label" not in evidence

    def test_element_evidence_survives_a_dead_element(self):
        llv = _load_probe_module()
        el = FakeElement(tag_name_exc=StaleElementReferenceException("stale"),
                         get_attr_exc={"aria-label": StaleElementReferenceException("stale")})
        assert llv.element_evidence(el) == {}


class TestAttachArticleCover:
    """The cover attach (issue #893) is best-effort: it must never take a publish down with it."""

    @staticmethod
    def _find_first(driver):
        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None
        return fake_find_first

    def test_writes_the_path_into_the_hidden_file_input(self, monkeypatch, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"png")
        file_input = FakeElement(tag="input", attrs={"type": "file", "accept": "image/*"})
        driver = FakeDriver({
            (By.XPATH, "//input[@type='file' and contains(translate(@accept,"
                       "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'image')]"):
                [file_input],
        })
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)
        monkeypatch.setattr(ae, "find_first", self._find_first(driver))
        assert ae.attach_article_cover(driver, MagicMock(), str(image)) is True
        assert file_input.sent == [os.path.abspath(str(image))]

    def test_clicks_the_trigger_when_no_input_is_rendered_yet(self, monkeypatch, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"png")
        trigger = FakeElement(tag="button", text="Add a cover image")
        file_input = FakeElement(tag="input", attrs={"type": "file"})
        trigger_locator = (By.XPATH, "//button[contains(translate(normalize-space(),"
                                     "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                                     "'cover image')]")
        dom = {trigger_locator: [trigger]}
        driver = FakeDriver(dom)
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        # The input only appears once the trigger has been clicked.
        original_click = trigger.click

        def click_then_render():
            original_click()
            dom[(By.CSS_SELECTOR, "input[type='file']")] = [file_input]

        trigger.click = click_then_render
        monkeypatch.setattr(ae, "find_first", fake_find_first)
        assert ae.attach_article_cover(driver, MagicMock(), str(image)) is True
        assert trigger.clicked == 1
        assert file_input.sent == [os.path.abspath(str(image))]

    def test_attaches_to_an_input_that_is_hidden(self, monkeypatch, tmp_path):
        # LinkedIn styles a button over the file input, so on the real page it is NOT displayed.
        # If the resolver filtered on visibility the cover would never attach in production.
        image = tmp_path / "cover.png"
        image.write_bytes(b"png")
        file_input = FakeElement(tag="input", attrs={"type": "file", "accept": "image/*"},
                                 displayed=False)
        driver = FakeDriver({
            (By.XPATH, "//input[@type='file' and contains(translate(@accept,"
                       "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'image')]"):
                [file_input],
        })
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)
        monkeypatch.setattr(ae, "find_first", self._find_first(driver))
        assert ae.attach_article_cover(driver, MagicMock(), str(image)) is True
        assert file_input.sent == [os.path.abspath(str(image))]

    def test_missing_file_on_disk_is_false_not_an_exception(self, tmp_path):
        assert ae.attach_article_cover(FakeDriver(), MagicMock(),
                                       str(tmp_path / "gone.png")) is False

    def test_no_upload_control_is_false_not_an_exception(self, monkeypatch, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"png")
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)
        monkeypatch.setattr(ae, "find_first", lambda *a, **k: None)
        assert ae.attach_article_cover(FakeDriver(), MagicMock(), str(image)) is False

    def test_a_webdriver_error_is_swallowed(self, monkeypatch, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"png")
        file_input = FakeElement(tag="input", attrs={"type": "file", "accept": "image/*"},
                                 send_keys_exc=WebDriverException("boom"))
        driver = FakeDriver({(By.CSS_SELECTOR, "input[type='file']"): [file_input]})
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)
        monkeypatch.setattr(ae, "find_first", self._find_first(driver))
        assert ae.attach_article_cover(driver, MagicMock(), str(image)) is False

    def test_confirms_a_crop_dialog_when_one_is_up(self, monkeypatch, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"png")
        file_input = FakeElement(tag="input", attrs={"type": "file", "accept": "image/*"})
        apply_btn = FakeElement(tag="button", text="Apply")
        driver = FakeDriver({
            (By.CSS_SELECTOR, "input[type='file']"): [file_input],
            (By.XPATH, "//button[normalize-space()='Apply']"): [apply_btn],
        })
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)
        monkeypatch.setattr(ae, "find_first", self._find_first(driver))
        assert ae.attach_article_cover(driver, MagicMock(), str(image)) is True
        assert apply_btn.clicked == 1


class TestFillArticleEditorCover:
    def _dom(self, extra=None):
        title = FakeElement(tag="textarea", attrs={"placeholder": "Title"})
        body = FakeElement(tag="div", attrs={"role": "textbox"})
        nxt = FakeElement(tag="button")
        publish = FakeElement(tag="button")
        dom = {
            (By.CSS_SELECTOR, "textarea[placeholder='Title']"): [title],
            (By.CSS_SELECTOR, "div[role='textbox']"): [body],
            (By.XPATH, "//button[normalize-space()='Next']"): [nxt],
            (By.XPATH, "//button[normalize-space()='Publish']"): [publish],
        }
        dom.update(extra or {})
        return dom, title, body, nxt, publish

    def _wire(self, monkeypatch, driver):
        monkeypatch.setattr(ae.time, "sleep", lambda s: None)

        def fake_find_first(d, w, locators, *args, **kwargs):
            for by, value in locators:
                matches = driver.find_elements(by, value)
                if matches:
                    return matches[0]
            return None

        monkeypatch.setattr(ae, "find_first", fake_find_first)

    def test_no_cover_path_never_touches_the_upload_control(self, monkeypatch):
        file_input = FakeElement(tag="input", attrs={"type": "file", "accept": "image/*"})
        dom, *_ = self._dom({(By.CSS_SELECTOR, "input[type='file']"): [file_input]})
        driver = FakeDriver(dom)
        self._wire(monkeypatch, driver)
        url, failed = ae.fill_article_editor(driver, MagicMock(), "T", "B")
        assert url == driver.current_url and failed is None
        assert file_input.sent == []

    def test_cover_is_attached_before_next(self, monkeypatch, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"png")
        file_input = FakeElement(tag="input", attrs={"type": "file", "accept": "image/*"})
        dom, _title, _body, nxt, publish = self._dom(
            {(By.CSS_SELECTOR, "input[type='file']"): [file_input]})
        driver = FakeDriver(dom)
        self._wire(monkeypatch, driver)
        url, failed = ae.fill_article_editor(driver, MagicMock(), "T", "B",
                                             cover_image_path=str(image))
        assert url == driver.current_url and failed is None
        assert file_input.sent == [os.path.abspath(str(image))]
        assert nxt.clicked == 1 and publish.clicked == 1

    def test_a_cover_that_cannot_attach_still_publishes(self, monkeypatch, tmp_path):
        # No file input anywhere: the cover is lost, the edition is not.
        image = tmp_path / "cover.png"
        image.write_bytes(b"png")
        dom, _title, _body, nxt, publish = self._dom()
        driver = FakeDriver(dom)
        self._wire(monkeypatch, driver)
        url, failed = ae.fill_article_editor(driver, MagicMock(), "T", "B",
                                             cover_image_path=str(image))
        assert url == driver.current_url
        assert failed is None, "an optional cover must never become a failed_step"
        assert nxt.clicked == 1 and publish.clicked == 1
