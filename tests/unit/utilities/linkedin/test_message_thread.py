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
# What a real profile page carries: the VIEWER's own URN (the Me menu) lands in the document before
# the person being viewed, so "first URN in the page" is routinely somebody else.
VIEWER_URN = "urn:li:fsd_profile:ACoAAAVIEWER"
PAGE_MODEL = ('{"me":{"entityUrn":"' + VIEWER_URN + '"},'
              '"included":[{"publicIdentifier":"jane-doe-8a4b21","entityUrn":"' + URN + '"}]}')


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
        # What the composer's recipient container renders; None means there is no container at all.
        self.recipient = None

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
        if script is mt._RECIPIENT_PILL_JS:
            return self.recipient
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


class TestWaitThreadOpen:
    """A bare composer must not end the wait: LinkedIn paints the compose form before the message
    list, and a thread reported with zero events is UNKNOWN — which parks that person's follow-up.
    """

    def test_events_that_arrive_after_the_composer_are_still_read(self, monkeypatch):
        readings = [{"events": 0, "composer": True, "surface": "page"},
                    {"events": 0, "composer": True, "surface": "page"},
                    {"events": 9, "composer": True, "surface": "page"}]
        monkeypatch.setattr(mt, "thread_reading", lambda _d: readings.pop(0))
        assert mt._wait_thread_open(MagicMock(), timeout=5)["events"] == 9

    def test_a_composer_only_thread_is_still_returned_once_the_budget_is_spent(self, monkeypatch):
        monkeypatch.setattr(mt, "thread_reading",
                            lambda _d: {"events": 0, "composer": True, "surface": "page"})
        reading = mt._wait_thread_open(MagicMock(), timeout=0)
        assert reading["composer"] is True and reading["events"] == 0

    def test_nothing_rendered_is_nothing_rendered(self, monkeypatch):
        monkeypatch.setattr(mt, "thread_reading",
                            lambda _d: {"events": 0, "composer": False, "surface": None})
        assert mt._wait_thread_open(MagicMock(), timeout=0) == {"events": 0, "composer": False,
                                                                "surface": None}


class TestNameMatches:
    """Whole-word, both directions of harm: a loose match opens a stranger's thread, and a loose
    SELF match reads their reply as our own message and sends the follow-up anyway.
    """

    def test_a_full_name_matches_inside_a_label(self):
        assert mt.name_matches("Jane Doe", "Jane Doe\nthanks!")

    def test_case_and_whitespace_are_ignored(self):
        assert mt.name_matches("  jane   doe ", "JANE\nDOE")

    def test_a_prefix_of_a_longer_name_does_not_match(self):
        assert not mt.name_matches("Jane", "Janet Smithers")
        assert not mt.name_matches("Chris", "Christine Baker")

    def test_a_trailing_suffix_still_matches(self):
        assert mt.name_matches("Christopher Queen", "Christopher Queen, MBA")

    def test_empty_either_side_is_never_a_match(self):
        assert not mt.name_matches("", "Jane Doe")
        assert not mt.name_matches("Jane Doe", "")
        assert not mt.name_matches(None, None)


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
        d = FakeDriver(page_source=PAGE_MODEL)

        def _get(url):
            d.urls.append(url)
            if "compose" in url:
                d.thread = {"events": 2, "composer": True, "overlay": False}

        d.get = _get
        result = self._ladder(d)
        assert result.route == mt.ROUTE_DIRECT_URL
        # `recipient` is what ADDS the person; profileUrn alone opens an unaddressed composer.
        assert d.urls[-1] == (f"{mt.COMPOSE_URL}?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAABCDEF"
                              f"&recipient=ACoAAABCDEF"
                              f"&screenContext={mt._COMPOSE_SCREEN_CONTEXT}")

    def test_the_urn_is_captured_before_any_route_navigates_away(self):
        # An earlier route can leave us on a page that no longer carries the person's URN — the
        # direct-URL fallback would then have nothing to build from.
        d = FakeDriver(page_source=PAGE_MODEL)
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
        assert "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAABCDEF" in d.urls[-1]

    def test_direct_url_route_rejects_a_zero_event_composer(self):
        # Issue #1851: this URL is a COMPOSE surface, not a thread view. With no prior history it
        # renders a blank composer addressed to nobody-yet — indistinguishable from a genuinely
        # empty real thread — so it must not count as opened here.
        d = FakeDriver(page_source=PAGE_MODEL)
        d.thread = {"events": 0, "composer": True, "overlay": False}
        assert mt._try_direct_url(d, 0, None, PROFILE) is None

    def test_direct_url_route_accepts_a_reading_with_events(self):
        d = FakeDriver(page_source=PAGE_MODEL)
        d.thread = {"events": 1, "composer": True, "overlay": False}
        reading = mt._try_direct_url(d, 0, None, PROFILE)
        assert reading is not None and reading["events"] == 1

    def test_a_zero_event_direct_url_composer_falls_through_to_messaging_search(self):
        # The whole point of the fix: route five's false "opened" used to stop the ladder before
        # route six — the one most likely to find real history — ever ran.
        d = FakeDriver(page_source=PAGE_MODEL)
        convo = FakeElement(text="Jane Doe\nthanks!", on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "li.msg-conversation-listitem")] = [convo]

        def _get(url):
            d.urls.append(url)
            if "compose" in url:
                d.thread = {"events": 0, "composer": True, "overlay": False}

        d.get = _get
        with patch.object(mt, "find_first", return_value=FakeElement()):
            result = self._ladder(d, person_name="Jane Doe")
        assert result.route == mt.ROUTE_MESSAGING_SEARCH
        assert result.tried == [mt.ROUTE_ANCHOR, mt.ROUTE_BUTTON, mt.ROUTE_TEXT_NODE,
                                mt.ROUTE_OVERFLOW, mt.ROUTE_DIRECT_URL, mt.ROUTE_MESSAGING_SEARCH]

    def test_direct_url_route_prefers_the_compose_anchors_own_urn(self):
        d = FakeDriver(page_source="<code>urn:li:fsd_profile:WRONGONE</code>")
        d.dom[(By.CSS_SELECTOR, "a[href*='profileUrn=']")] = [
            FakeElement({"href": "https://x/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3ARIGHT"})]
        assert mt.profile_urn_from_page(d, PROFILE) == "urn:li:fsd_profile:RIGHT"

    def test_the_page_model_urn_is_the_one_beside_this_persons_slug(self):
        # The viewer's own URN (Me menu) comes FIRST in the document. Taking it would compose to
        # ourselves and then judge this person's follow-up from our own thread.
        assert mt.profile_urn_from_page(FakeDriver(page_source=PAGE_MODEL), PROFILE) == URN

    def test_a_page_with_no_urn_for_this_person_yields_none_rather_than_a_stranger(self):
        d = FakeDriver(page_source='{"me":{"entityUrn":"' + VIEWER_URN + '"}}')
        assert mt.profile_urn_from_page(d, PROFILE) is None
        # …and with nothing to build from, the direct-URL route never navigates.
        assert mt._try_direct_url(d, 0, None, PROFILE) is None
        assert d.urls == []

    def test_a_urn_cannot_be_resolved_without_a_slug_to_scope_it(self):
        d = FakeDriver(page_source=PAGE_MODEL)
        assert mt.profile_urn_from_page(d, "https://x/feed/") is None

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

    def test_messaging_search_does_not_match_a_name_that_merely_starts_the_same(self):
        # 'Jane' is a substring of 'Janet' — opening her thread would judge Jane's follow-up from a
        # stranger's conversation, which is the spam this whole issue is about.
        d = FakeDriver()
        janet = FakeElement(text="Janet Smithers\nsure thing", on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "li.msg-conversation-listitem")] = [janet]
        with patch.object(mt, "find_first", return_value=FakeElement()):
            result = self._ladder(d, person_name="Jane")
        assert not result.opened and janet.clicked == 0

    def test_messaging_search_rejects_a_row_that_links_to_a_different_profile(self):
        # The label may read right and the link still name somebody else — the link wins.
        d = FakeDriver()
        link = FakeElement({"href": "https://www.linkedin.com/in/jane-doe-other-99/"})
        convo = FakeElement(text="Jane Doe", children={(By.TAG_NAME, "a"): [link]},
                            on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "li.msg-conversation-listitem")] = [convo]
        with patch.object(mt, "find_first", return_value=FakeElement()):
            result = self._ladder(d, person_name="Jane Doe")
        assert not result.opened and convo.clicked == 0

    def test_messaging_search_matches_on_the_profile_slug_when_the_name_does_not(self):
        d = FakeDriver()
        link = FakeElement({"href": "https://www.linkedin.com/in/JANE-DOE-8a4b21/"})
        convo = FakeElement(text="J. Doe", children={(By.TAG_NAME, "a"): [link]},
                            on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "li.msg-conversation-listitem")] = [convo]
        with patch.object(mt, "find_first", return_value=FakeElement()):
            result = self._ladder(d, person_name="Jane Doe")
        assert result.route == mt.ROUTE_MESSAGING_SEARCH

    def test_missing_search_box_is_not_a_warning(self):
        """Messaging search is the LAST route in the ladder — reached only once every earlier route
        has already failed. A missing search box there is the expected shape of "this account
        cannot message this person at all" (or the messaging SPA didn't boot, issue #1774), not
        selector rot, so it must not WARN — a WARNING here recurred into
        `RecurringWarning: Selector miss: Messaging search box` for working refusal-to-follow-up-blind
        behavior (issue #1783), the same reasoning `open_message_thread` already applies to the
        ladder as a whole (issue #1752).
        """
        d = FakeDriver()
        with patch.object(mt, "find_first", return_value=None) as find_first:
            result = mt._try_messaging_search(d, MagicMock(), "Jane Doe", PROFILE, timeout=0)
        assert result is None
        find_first.assert_called_once()
        assert find_first.call_args.kwargs.get("warn_on_miss") is False


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

    def test_no_route_opening_logs_debug_not_warning(self):
        # issue #1752: exhausting every route is the expected outcome for anyone this account
        # cannot message this way (not connected, InMail-only, messaging restricted) — not selector
        # rot. `check_dm_replied` already turns this into ThreadState.UNKNOWN and skips the
        # follow-up, so a WARNING here recurred and filed a RecurringWarning for working
        # skip-rather-than-guess behavior. This must stay DEBUG and never reach `log_escalation`.
        d = FakeDriver()
        dud = FakeElement({"href": "/messaging/compose/?x"})
        d.dom[(By.CSS_SELECTOR, "main a[href*='/messaging/compose/']")] = [dud]
        with patch.object(mt, "log_warning") as warn, patch.object(mt, "log_debug") as debug:
            result = mt.open_message_thread(d, MagicMock(), PROFILE, timeout=0)
        assert not result.opened
        warn.assert_not_called()
        assert any("No route opened a message thread" in call.args[0]
                   for call in debug.call_args_list)

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


class TestEmptyComposePageIsNotAThread:
    """The #1851 verdict belongs to the READING, not to one route id.

    #1853 fixed `_try_direct_url`, the route production caught. But the profile's own Message
    control is an ``<a href='/messaging/compose/…'>`` (`_ANCHOR_LOCATORS[0]`), so route ONE lands on
    the identical compose screen and could claim the ladder the same way, four routes earlier. The
    rule is therefore applied wherever a reading is judged.
    """

    def _ladder(self, driver, person_name=None):
        return mt.open_message_thread(driver, MagicMock(), PROFILE, person_name=person_name,
                                      timeout=0)

    def test_a_compose_page_with_no_messages_is_not_an_open_thread(self):
        assert not mt.is_open_thread({"events": 0, "composer": True, "surface": mt.SURFACE_PAGE})

    def test_message_events_prove_a_thread_on_either_surface(self):
        assert mt.is_open_thread({"events": 1, "composer": True, "surface": mt.SURFACE_PAGE})
        assert mt.is_open_thread({"events": 1, "composer": False, "surface": mt.SURFACE_OVERLAY})

    def test_an_empty_overlay_bubble_still_counts(self):
        # The bubble is anchored to the person whose control we clicked, so an empty one is a real
        # thread with no history yet. The full-page compose screen affords no such guarantee, which
        # is the whole reason the two surfaces are judged differently.
        assert mt.is_open_thread({"events": 0, "composer": True, "surface": mt.SURFACE_OVERLAY})

    def test_nothing_rendered_is_never_a_thread(self):
        assert not mt.is_open_thread({"events": 0, "composer": False, "surface": None})
        assert not mt.is_open_thread(None)

    def test_an_anchor_that_lands_on_an_empty_compose_page_keeps_walking(self):
        # Route ONE reproducing the #1851 shape: the anchor navigates to /messaging/compose/, which
        # renders a composer and no events. It must not claim the ladder either.
        d = FakeDriver()
        anchor = FakeElement({"href": "/messaging/compose/?profileUrn=x"},
                             on_click=_opens(d, events=0))
        d.dom[(By.CSS_SELECTOR, "main a[href*='/messaging/compose/']")] = [anchor]
        result = mt.open_message_thread(d, MagicMock(), PROFILE, timeout=0)
        assert anchor.clicked == 1  # it WAS clicked — it just never produced a thread
        assert result.route != mt.ROUTE_ANCHOR
        assert mt.ROUTE_MESSAGING_SEARCH in result.tried

    def test_an_empty_overlay_route_is_unchanged(self):
        # NO-REGRESSION: the overlay half must keep counting, or a real thread we have not spoken
        # in yet would stop being reachable at all.
        d = FakeDriver()
        el = FakeElement(text="Message", on_click=_opens(d, events=0, overlay=True))
        d.dom[(By.XPATH, mt._TEXT_NODE_LOCATORS[0][1])] = [el]
        result = mt.open_message_thread(d, MagicMock(), PROFILE, timeout=0)
        assert result.opened and result.route == mt.ROUTE_TEXT_NODE
        assert result.surface == mt.SURFACE_OVERLAY and result.events == 0

    def test_a_route_with_message_events_is_unchanged(self):
        # NO-REGRESSION: events > 0 still wins on the first route that has them.
        d = FakeDriver()
        anchor = FakeElement({"href": "/messaging/compose/?profileUrn=x"}, on_click=_opens(d))
        d.dom[(By.CSS_SELECTOR, "main a[href*='/messaging/compose/']")] = [anchor]
        result = self._ladder(d)
        assert result.route == mt.ROUTE_ANCHOR and result.events == 4
        assert result.tried == [mt.ROUTE_ANCHOR]

    def test_walking_past_a_compose_page_is_debug_not_a_warning(self):
        # A compose screen instead of a thread is an ordinary ladder step, not selector rot — a
        # warning here would recur into a RecurringWarning for working behaviour (#1752).
        d = FakeDriver()
        anchor = FakeElement({"href": "/messaging/compose/?profileUrn=x"},
                             on_click=_opens(d, events=0))
        d.dom[(By.CSS_SELECTOR, "main a[href*='/messaging/compose/']")] = [anchor]
        with patch.object(mt, "find_first", return_value=None), \
                patch.object(mt, "log_warning") as warn, patch.object(mt, "log_debug") as debug:
            mt.open_message_thread(d, MagicMock(), PROFILE, person_name="Jane Doe", timeout=0)
        warn.assert_not_called()
        assert any("not an open thread" in call.args[0] for call in debug.call_args_list)

    def test_the_send_path_still_accepts_the_full_page_composer(self):
        # NO-REGRESSION: `open_addressed_composer` WANTS the compose page the read ladder rejects;
        # its proof is the recipient pill, not message history (issue #1030).
        d = FakeDriver(page_source=PAGE_MODEL)
        d.thread = {"events": 0, "composer": True, "overlay": False}
        d.recipient = "Jane Doe"
        result = mt.open_addressed_composer(d, MagicMock(), PROFILE, timeout=0)
        assert result.addressed and result.recipient == "Jane Doe"


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

    def test_sender_retries_through_a_transient_empty_read(self):
        # issue #1864: the name attach lands async, so the first read(s) after the thread opens
        # can be empty even though the thread is genuinely readable — a bare retry recovers it
        # without ever reaching the "no sender could be read" warning.
        d = MagicMock()
        d.execute_script.side_effect = ["", "", "Jane Doe"]
        assert mt.read_last_sender(d) == "Jane Doe"
        assert d.execute_script.call_count == 3

    def test_sender_still_empty_after_every_retry_is_unreadable(self):
        # A genuinely rotated selector (or a sender that never arrives) must still end up '' once
        # the retry budget is spent — the caller's warning is the correct outcome here.
        d = MagicMock()
        d.execute_script.return_value = ""
        assert mt.read_last_sender(d) == ""
        assert d.execute_script.call_count == mt._SENDER_READ_RETRIES


class TestResolveSelfName:
    """The name reply detection compares the last sender against (issue #731). The SAVED settings
    value is the user's own declaration of what LinkedIn renders; the scraped profile is only the
    fallback, and '' means UNKNOWN — never 'they replied'.
    """

    def _saved(self, value):
        return patch("cqc_lem.utilities.db.get_user_linkedin_display_name", return_value=value)

    def test_saved_name_wins_over_the_scraped_profile(self):
        profile = MagicMock(full_name="C. Queen (Consultant)")
        with self._saved("Christopher Queen"):
            assert mt.resolve_self_name(1, profile) == "Christopher Queen"

    def test_falls_back_to_the_scraped_profile(self):
        profile = MagicMock(full_name="  Jordan Alvarez ")
        with self._saved(None):
            assert mt.resolve_self_name(1, profile) == "Jordan Alvarez"

    def test_blank_saved_name_falls_back_too(self):
        profile = MagicMock(full_name="Jordan Alvarez")
        with self._saved("   "):
            assert mt.resolve_self_name(1, profile) == "Jordan Alvarez"

    def test_nothing_anywhere_is_empty_not_a_guess(self):
        with self._saved(None):
            assert mt.resolve_self_name(1, None) == ""

    def test_a_db_failure_still_falls_back_to_the_profile(self):
        profile = MagicMock(full_name="Jordan Alvarez")
        with patch("cqc_lem.utilities.db.get_user_linkedin_display_name",
                   side_effect=RuntimeError("db down")):
            assert mt.resolve_self_name(1, profile) == "Jordan Alvarez"

    def test_no_user_id_never_queries_the_db(self):
        profile = MagicMock(full_name="Jordan Alvarez")
        with patch("cqc_lem.utilities.db.get_user_linkedin_display_name") as lookup:
            assert mt.resolve_self_name(None, profile) == "Jordan Alvarez"
        lookup.assert_not_called()


class TestComposeUrl:
    """LinkedIn's own top-card link carries profileUrn AND recipient — the 2026-08-04 grounding run
    found that dropping the second one opens a composer addressed to nobody.
    """

    def test_the_url_addresses_the_person_not_just_the_thread(self):
        url = mt.compose_url_for(URN)
        assert "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAABCDEF" in url
        assert "recipient=ACoAAABCDEF" in url

    def test_the_recipient_is_the_urns_trailing_id(self):
        assert "recipient=ACoAAABCDEF" in mt.compose_url_for(URN)


class TestComposerRecipient:
    def test_the_pill_name_is_the_recipient(self):
        d = FakeDriver()
        d.recipient = "Jay Bailey\nShow suggested recipients for your message"
        assert mt.composer_recipient(d) == "Jay Bailey"

    def test_the_empty_state_placeholder_is_not_a_recipient(self):
        # This is the whole hazard: 'Enter message recipients' is long enough to read as a name.
        d = FakeDriver()
        d.recipient = "Enter message recipients"
        assert mt.composer_recipient(d) == ""

    def test_no_recipient_container_at_all_is_empty(self):
        assert mt.composer_recipient(FakeDriver()) == ""

    def test_a_js_failure_reads_as_unaddressed_rather_than_raising(self):
        d = FakeDriver()
        d.execute_script = MagicMock(side_effect=WebDriverException("boom"))
        assert mt.composer_recipient(d) == ""


class TestOpenAddressedComposer:
    """The send path's contract: open is not enough, it has to be addressed to the right person."""

    @staticmethod
    def _driver(recipient="Jane Doe", thread=None):
        d = FakeDriver(page_source=PAGE_MODEL)
        d.recipient = recipient

        def _get(url):
            d.urls.append(url)
            if "compose" in url:
                d.thread = thread or {"events": 0, "composer": True, "overlay": False}

        d.get = _get
        return d

    def test_an_addressed_composer_is_the_success_case(self):
        d = self._driver()
        result = mt.open_addressed_composer(d, MagicMock(), PROFILE, user_id=1, timeout=0)
        assert result.addressed and bool(result) is True
        assert result.recipient == "Jane Doe"
        assert result.urn == URN
        assert "recipient=ACoAAABCDEF" in d.urls[-1]

    def test_a_composer_naming_nobody_is_refused(self):
        d = self._driver(recipient="Enter message recipients")
        result = mt.open_addressed_composer(d, MagicMock(), PROFILE, user_id=1, timeout=0)
        assert not result.addressed
        assert result.reason == "unaddressed"

    def test_no_urn_never_guesses_a_recipient(self):
        d = self._driver()
        d.page_source = ""  # nothing on the page identifies this person
        result = mt.open_addressed_composer(d, MagicMock(), PROFILE, user_id=1, timeout=0)
        assert not result.addressed
        assert result.reason == "no_urn"
        assert not any("compose" in u for u in d.urls)  # and never opened a composer to find out

    def test_a_composer_that_never_renders_is_not_addressed(self):
        d = self._driver()
        d.get = lambda url: d.urls.append(url)  # nothing ever opens
        result = mt.open_addressed_composer(d, MagicMock(), PROFILE, user_id=1, timeout=0)
        assert not result.addressed
        assert result.reason == "composer_missing"

    def test_a_composer_that_never_renders_logs_debug_not_warning(self):
        # issue #1710: a composer that never renders is the expected outcome for anyone we can't
        # message this way (not connected, InMail-only) — not selector rot. A WARNING here recurred
        # 3x/24h and filed a code defect (RecurringWarning) for working refusal-to-send behavior, so
        # this must stay DEBUG and never reach `log_escalation`.
        d = self._driver()
        d.get = lambda url: d.urls.append(url)  # nothing ever opens
        with patch.object(mt, "log_warning") as warn, patch.object(mt, "log_debug") as debug:
            result = mt.open_addressed_composer(d, MagicMock(), PROFILE, user_id=1, timeout=0)
        assert result.reason == "composer_missing"
        warn.assert_not_called()
        assert any("never rendered" in call.args[0] for call in debug.call_args_list)

    def test_an_unreachable_profile_stops_before_composing(self):
        d = self._driver()
        d.get = MagicMock(side_effect=WebDriverException("no route"))
        result = mt.open_addressed_composer(d, MagicMock(), PROFILE, user_id=1, timeout=0)
        assert not result.addressed
        assert result.reason == "profile_unreachable"

    def test_it_never_clicks_a_message_control(self):
        # Clicking whichever control the DOM offers first is how a send reaches a stranger; this
        # path navigates to the person's OWN compose URL instead.
        d = self._driver()
        stranger = FakeElement({"href": "/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3ASTRANGER"})
        d.dom[(By.CSS_SELECTOR, "main a[href*='/messaging/compose/']")] = [stranger]
        result = mt.open_addressed_composer(d, MagicMock(), PROFILE, user_id=1, timeout=0)
        assert result.addressed
        assert stranger.clicked == 0
