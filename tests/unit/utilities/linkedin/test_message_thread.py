"""Unit tests for the message-thread resolution ladder (issue #731).

Every route gets a stubbed driver: the point of the ladder is that a route which finds nothing (or
raises) hands off to the next one, and that NO route counts until the thread is provably open.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common import WebDriverException
from selenium.webdriver.common.by import By

from cqc_lem.utilities.linkedin import message_thread as mt

pytestmark = pytest.mark.unit

PROFILE = "https://www.linkedin.com/in/jane-doe-8a4b21/"
URN = "urn:li:fsd_profile:ACoAAABCDEF"


class FakeElement:
    def __init__(self, attrs: dict = None, text: str = "", displayed: bool = True,
                 children: dict = None, on_click=None):
        self._attrs = attrs or {}
        self.text = text
        self._displayed = displayed
        self._children = children or {}
        self._on_click = on_click
        self.clicked = 0

    def is_displayed(self):
        return self._displayed

    def get_attribute(self, name):
        return self._attrs.get(name)

    def find_elements(self, by, value):
        return self._children.get((by, value), [])

    def click(self):
        self.clicked += 1
        if self._on_click:
            self._on_click()

    def clear(self):
        self.text = ""

    def send_keys(self, *_keys):
        return None


class FakeDriver:
    """A driver whose DOM is a dict of locator -> elements, mutated by whatever a click does."""

    def __init__(self, dom: dict = None, thread=None, page_source: str = ""):
        self.dom = dom or {}
        # `thread` is the reading _THREAD_STATE_JS should return; None means "nothing open yet".
        self.thread = thread
        self.page_source = page_source
        self.urls = []
        self.sender = None
        self.body = None

    def get(self, url):
        self.urls.append(url)

    def find_elements(self, by, value):
        return self.dom.get((by, value), [])

    def execute_script(self, script, *args):
        if script is mt._THREAD_STATE_JS:
            return self.thread or {"events": 0, "composer": False, "overlay": False}
        if script is mt._LAST_SENDER_JS:
            return self.sender
        if script is mt._LAST_MESSAGE_JS:
            return self.body
        if "arguments[0].click()" in script:
            args[0].click()
            return None
        return None


def _opens(driver, events=4, overlay=False):
    """A click handler that makes the thread render."""
    def _handler():
        driver.thread = {"events": events, "composer": True, "overlay": overlay}
    return _handler


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(mt.time, "sleep", lambda *_a, **_k: None)


class TestSlugAndName:
    def test_slug_is_lowercased_and_query_stripped(self):
        assert mt.profile_slug("https://x/in/Jane-Doe-8a4b21/?x=1") == "jane-doe-8a4b21"

    def test_no_slug_is_empty_not_none(self):
        assert mt.profile_slug("https://x/company/acme") == ""
        assert mt.profile_slug(None) == ""

    def test_name_drops_the_trailing_id_segment(self):
        assert mt.name_from_profile_url(PROFILE) == "jane doe"

    def test_name_is_empty_when_there_is_no_slug(self):
        assert mt.name_from_profile_url("https://x/feed/") == ""


class TestThreadReading:
    def test_overlay_and_page_are_both_recognized(self):
        d = FakeDriver(thread={"events": 7, "composer": True, "overlay": True})
        assert mt.thread_reading(d) == {"events": 7, "composer": True, "surface": "overlay"}
        d.thread = {"events": 7, "composer": True, "overlay": False}
        assert mt.thread_reading(d)["surface"] == "page"

    def test_nothing_open_has_no_surface(self):
        assert mt.thread_reading(FakeDriver())["surface"] is None

    def test_a_js_failure_reads_as_closed_rather_than_raising(self):
        d = MagicMock()
        d.execute_script.side_effect = WebDriverException("boom")
        assert mt.thread_reading(d) == {"events": 0, "composer": False, "surface": None}

    def test_a_non_numeric_event_count_is_zero(self):
        d = FakeDriver(thread={"events": "lots", "composer": False, "overlay": False})
        assert mt.thread_reading(d)["events"] == 0


class TestRoutes:
    def _ladder(self, driver, person_name=None):
        return mt.open_message_thread(driver, MagicMock(), PROFILE, person_name=person_name,
                                      timeout=0)

    def test_anchor_route_wins_on_todays_dom(self):
        d = FakeDriver()
        anchor = FakeElement({"href": "/messaging/compose/?profileUrn=x"}, on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "main a[href*='/messaging/compose/']")] = [anchor]
        result = self._ladder(d)
        assert result.opened and result.route == mt.ROUTE_ANCHOR
        assert result.events == 4 and result.surface == "page"
        assert result.tried == [mt.ROUTE_ANCHOR]

    def test_button_route_still_works_where_linkedin_renders_one(self):
        d = FakeDriver()
        btn = FakeElement({"aria-label": "Message Jane"}, on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "button[aria-label^='Message']")] = [btn]
        result = self._ladder(d)
        assert result.route == mt.ROUTE_BUTTON
        assert result.tried == [mt.ROUTE_ANCHOR, mt.ROUTE_BUTTON]

    def test_text_node_route_catches_a_tag_agnostic_control(self):
        d = FakeDriver()
        el = FakeElement(text="Message", on_click=_opens(d, overlay=True))
        d.dom[(By.XPATH, mt._TEXT_NODE_LOCATORS[0][1])] = [el]
        result = self._ladder(d)
        assert result.route == mt.ROUTE_TEXT_NODE
        assert result.surface == "overlay"

    def test_overflow_route_opens_the_more_menu_then_the_control_inside_it(self):
        d = FakeDriver()
        item = FakeElement(text="Message", on_click=_opens(d))

        def _reveal():
            d.dom[(By.XPATH, mt._TEXT_NODE_LOCATORS[0][1])] = [item]

        more = FakeElement({"aria-label": "More actions"}, on_click=_reveal)
        d.dom[(By.CSS_SELECTOR, "main button[aria-label^='More actions']")] = [more]
        result = self._ladder(d)
        assert result.route == mt.ROUTE_OVERFLOW
        assert more.clicked == 1

    def test_direct_url_route_builds_the_compose_url_from_the_profile_urn(self):
        d = FakeDriver(page_source=f'<code>{URN}</code>')

        def _get(url):
            d.urls.append(url)
            if "compose" in url:
                d.thread = {"events": 2, "composer": True, "overlay": False}

        d.get = _get
        result = self._ladder(d)
        assert result.route == mt.ROUTE_DIRECT_URL
        assert d.urls[-1] == f"{mt.COMPOSE_URL}?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAABCDEF"

    def test_the_urn_is_captured_before_any_route_navigates_away(self):
        # An earlier route can leave us on a page that no longer carries the person's URN — the
        # direct-URL fallback would then have nothing to build from.
        d = FakeDriver(page_source=f"<code>{URN}</code>")
        dud = FakeElement({"href": "/messaging/compose/?x"})

        def _get(url):
            d.urls.append(url)
            if url != PROFILE:
                d.page_source = ""  # the profile page is gone once a route navigates
            if "compose" in url:
                d.thread = {"events": 1, "composer": True, "overlay": False}

        d.dom[(By.CSS_SELECTOR, "main a[href*='/messaging/compose/']")] = [dud]
        d.get = _get
        result = self._ladder(d)
        assert result.route == mt.ROUTE_DIRECT_URL
        assert d.urls[-1].endswith("ACoAAABCDEF")

    def test_direct_url_route_prefers_the_compose_anchors_own_urn(self):
        d = FakeDriver(page_source="<code>urn:li:fsd_profile:WRONGONE</code>")
        d.dom[(By.CSS_SELECTOR, "a[href*='profileUrn=']")] = [
            FakeElement({"href": "https://x/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3ARIGHT"})]
        assert mt.profile_urn_from_page(d) == "urn:li:fsd_profile:RIGHT"

    def test_messaging_search_route_opens_the_matching_conversation(self):
        d = FakeDriver()
        convo = FakeElement(text="Jane Doe\nthanks!", on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "li.msg-conversation-listitem")] = [convo]
        box = FakeElement()
        with patch.object(mt, "find_first", return_value=box):
            result = self._ladder(d, person_name="Jane Doe")
        assert result.route == mt.ROUTE_MESSAGING_SEARCH
        assert mt.MESSAGING_URL in d.urls

    def test_messaging_search_skips_a_conversation_with_somebody_else(self):
        d = FakeDriver()
        other = FakeElement(text="Bob Smith\nhello", on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "li.msg-conversation-listitem")] = [other]
        with patch.object(mt, "find_first", return_value=FakeElement()):
            result = self._ladder(d, person_name="Jane Doe")
        assert not result.opened
        assert other.clicked == 0

    def test_messaging_search_matches_on_the_profile_slug_when_the_name_does_not(self):
        d = FakeDriver()
        link = FakeElement({"href": "https://www.linkedin.com/in/JANE-DOE-8a4b21/"})
        convo = FakeElement(text="J. Doe", children={(By.TAG_NAME, "a"): [link]},
                            on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "li.msg-conversation-listitem")] = [convo]
        with patch.object(mt, "find_first", return_value=FakeElement()):
            result = self._ladder(d, person_name="Jane Doe")
        assert result.route == mt.ROUTE_MESSAGING_SEARCH


class TestLadderContract:
    def test_a_control_that_clicks_but_opens_nothing_is_not_a_success(self):
        # The whole failure mode of the old code: the click "worked" and nothing was verified.
        d = FakeDriver()
        dud = FakeElement({"href": "/messaging/compose/?x"})
        d.dom[(By.CSS_SELECTOR, "main a[href*='/messaging/compose/']")] = [dud]
        result = mt.open_message_thread(d, MagicMock(), PROFILE, timeout=0)
        assert dud.clicked == 1  # it WAS clicked — it just never produced a readable thread
        assert not result.opened and result.route is None
        assert result.tried == list(mt.ROUTES)

    def test_a_raising_route_does_not_end_the_ladder(self):
        d = FakeDriver()
        btn = FakeElement({"aria-label": "Message"}, on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "button[aria-label^='Message']")] = [btn]
        with patch.object(mt, "_try_control",
                          side_effect=[RuntimeError("route 1 exploded"),
                                       {"events": 3, "composer": True, "surface": "page"}]):
            result = mt.open_message_thread(d, MagicMock(), PROFILE, timeout=0)
        assert result.route == mt.ROUTE_BUTTON

    def test_a_hidden_control_is_never_clicked(self):
        d = FakeDriver()
        hidden = FakeElement({"href": "/messaging/compose/?x"}, displayed=False, on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "main a[href*='/messaging/compose/']")] = [hidden]
        result = mt.open_message_thread(d, MagicMock(), PROFILE, timeout=0)
        assert not result.opened

    def test_a_navigation_failure_returns_closed_rather_than_raising(self):
        d = FakeDriver()
        d.get = MagicMock(side_effect=WebDriverException("no session"))
        result = mt.open_message_thread(d, MagicMock(), PROFILE, timeout=0)
        assert not result.opened and result.tried == []

    def test_thread_open_is_falsy_when_nothing_opened(self):
        assert not mt.ThreadOpen()
        assert mt.ThreadOpen(opened=True, route=mt.ROUTE_ANCHOR)


class TestReaders:
    def test_last_sender_and_body_are_trimmed(self):
        d = FakeDriver()
        d.sender = "  Jane Doe \n"
        d.body = " how much? "
        assert mt.read_last_sender(d) == "Jane Doe"
        assert mt.read_last_message(d) == "how much?"

    def test_unreadable_dom_is_empty_string_not_an_exception(self):
        d = MagicMock()
        d.execute_script.side_effect = WebDriverException("gone")
        assert mt.read_last_sender(d) == ""
        assert mt.read_last_message(d) == ""
