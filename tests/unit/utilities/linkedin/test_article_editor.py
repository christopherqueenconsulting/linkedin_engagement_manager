"""Unit tests for the article-editor selector ladder (issues #747, #771).

The ladder is exercised against a stub DOM so no live LinkedIn session is needed. Each test checks
that a step's fallback routes are tried in order, that disabled buttons are skipped, and that the
map reports a precise `failed_step`.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.webdriver.common.by import By

from cqc_lem.utilities.linkedin import article_editor as ae

pytestmark = pytest.mark.unit


class FakeElement:
    """A minimal WebElement stand-in with configurable attributes and display state."""

    def __init__(self, tag: str = "button", attrs: dict = None, displayed: bool = True,
                 interact_ok: bool = True):
        self.tag = tag
        self.attrs = attrs or {}
        self._displayed = displayed
        self._interact_ok = interact_ok
        self.clicked = 0
        self.sent = []
        self.cleared = 0

    def is_displayed(self):
        return self._displayed

    def get_attribute(self, name):
        return self.attrs.get(name)

    @property
    def tag_name(self):
        return self.tag

    def click(self):
        if not self._interact_ok:
            raise RuntimeError("not interactable")
        self.clicked += 1

    def clear(self):
        self.cleared += 1

    def send_keys(self, *keys):
        self.sent.extend(keys)


class FakeDriver:
    """Driver whose DOM is a dict of (By, value) -> list[FakeElement]."""

    def __init__(self, dom: dict = None):
        self.dom = dom or {}
        self.scripts = []
        self.url = None
        self.current_url = "https://www.linkedin.com/article/new/"

    def get(self, url):
        self.url = url

    def find_elements(self, by, value):
        return self.dom.get((by, value), [])

    def execute_script(self, script, *args):
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
