"""The Connect-dialog open on the invite send path (issues #571 + the 2026-08-03 rail hazard).

The SDUI profile top card carries no "Invite <name> to connect" button — the only aria-labels
matching the old unscoped locator are the "More profiles for you" rail, so clicking the first
match INVITED A RANDOM SUGGESTED PERSON and then failed on the missing Send dialog. The rebuilt
route only reports success when the dialog's own controls are provably present. A total miss
stays a WARNING (ordinary outcome), never an error.

Re-grounded 2026-08-29 (#1733). Navigating the /preload/custom-invite URL renders a BLANK
document now — it is an in-app route, not a page — so the link is CLICKED where the profile
renders it (top card in one layout, More menu in the other) and the URL navigation is demoted to
a last-resort fallback. The anchor is attributed by the `vanityName` in its own href, which is a
harder #1012 guard than any label: a rail anchor for a stranger carries that stranger's slug.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# The connect rail moved to its own module (#1154); patches for it must bind THERE, because that
# is the module whose globals the invite code reads.
_INV = "cqc_lem.app.engagement.invites"

_PROFILE_URL = "https://www.linkedin.com/in/jane-doe-123/"
_CUSTOM_INVITE_URL = "https://www.linkedin.com/preload/custom-invite/?vanityName=jane-doe-123"

_SEND_BARE_XPATH = '//button[contains(@aria-label,"Send without a note")]'


class _Routes:
    """Configurable stand-ins for find_first / click_first / click_element_wait_retry, recording
    every locator so the rail-hazard regression can assert nothing 'Invite …' is ever clicked.
    """

    def __init__(self, dialog_on_url=False, dialog_after_menu=False,
                 more_menu=False, menu_item=False, send_xpaths=frozenset(),
                 connect_button=False, dialog_after_connect_button=False,
                 anchor_hrefs=(), anchor_hrefs_after_menu=(), dialog_after_anchor=False):
        self.dialog_on_url = dialog_on_url
        self.dialog_after_menu = dialog_after_menu
        self.more_menu = more_menu
        self.menu_item = menu_item
        self.send_xpaths = set(send_xpaths)
        self.connect_button = connect_button
        self.dialog_after_connect_button = dialog_after_connect_button
        # The custom-invite anchors the PAGE renders — before the More menu opens, and after.
        self.anchor_hrefs = list(anchor_hrefs)
        self.anchor_hrefs_after_menu = list(anchor_hrefs_after_menu)
        self.dialog_after_anchor = dialog_after_anchor
        self.more_menu_opened = False
        self.clicked_anchor_hrefs: list[str] = []
        self.anchor_clicked = False
        self.menu_item_clicked = False
        self.connect_button_clicked = False
        self.find_labels: list[str] = []
        self.click_labels: list[str] = []
        self.all_locators: list[str] = []
        self.legacy_click_xpaths: list[str] = []  # click_element_wait_retry (note/send steps)

    def find_first(self, driver, wait, locators, label, **kwargs):
        self.find_labels.append(label)
        self.all_locators += [v for _, v in locators]
        if label == "Connect invite dialog":
            if self.dialog_after_anchor and self.anchor_clicked:
                return MagicMock()
            if self.dialog_on_url and not self.menu_item_clicked \
                    and not self.connect_button_clicked and not self.anchor_clicked:
                return MagicMock()
            if self.dialog_after_menu and self.menu_item_clicked:
                return MagicMock()
            if self.dialog_after_connect_button and self.connect_button_clicked:
                return MagicMock()
        return None

    def click_first(self, driver, wait, locators, label, **kwargs):
        self.click_labels.append(label)
        self.all_locators += [v for _, v in locators]
        if label == "Profile Connect button" and self.connect_button:
            self.connect_button_clicked = True
            return MagicMock()
        if label == "Profile More menu" and self.more_menu:
            self.more_menu_opened = True
            return MagicMock()
        if label == "Connect menu item" and self.menu_item:
            self.menu_item_clicked = True
            return MagicMock()
        return None

    def find_elements(self, by, value):
        """Stand-in for `driver.find_elements`.

        Only the custom-invite anchor lookup returns anything: the miss-evidence dump and the
        restriction read use their own selectors and must see an empty page here, or they would
        read the anchors as page copy.
        """
        from cqc_lem.app.engagement import invites as ra
        if value != ra._CUSTOM_INVITE_ANCHOR_XPATH:
            return []
        hrefs = (self.anchor_hrefs_after_menu if self.more_menu_opened else self.anchor_hrefs)
        return [self._anchor(href) for href in hrefs]

    def _anchor(self, href):
        anchor = MagicMock()
        anchor.get_attribute.side_effect = lambda name: href if name == "href" else None
        anchor.is_displayed.return_value = True
        anchor.click.side_effect = lambda: self._record(href)
        return anchor

    def _record(self, href):
        self.clicked_anchor_hrefs.append(href)
        self.anchor_clicked = True

    def click_element_wait_retry(self, driver, wait, xpath, label, **kwargs):
        self.all_locators.append(xpath)
        self.legacy_click_xpaths.append(xpath)
        if xpath in self.send_xpaths:
            return MagicMock()
        raise Exception(f"no element for {xpath}")


def _chains(_driver):
    """An ActionChains stand-in that CLICKS the element it was handed, so a route that clicks the
    wrong anchor is recorded rather than silently passing.
    """
    chain = MagicMock()
    holder = {}

    def move_to_element(element):
        holder["element"] = element
        return chain

    def click():
        element = holder.get("element")
        if element is not None:
            element.click()
        return chain

    chain.move_to_element.side_effect = move_to_element
    chain.click.side_effect = click
    return chain


def _invite(routes: _Routes, message: str = None):
    from cqc_lem.app.engagement import invites as ra
    driver = MagicMock()
    driver.current_url = "about:blank"
    driver.find_elements.side_effect = routes.find_elements
    with patch(f"{_INV}.ActionChains", _chains), \
         patch(f"{_INV}.record_invite_dialog_miss") as miss, \
         patch(f"{_INV}.hold_invites") as hold, \
         patch(f"{_INV}.clear_invite_dialog_misses"), \
         patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch(f"{_INV}.get_driver_wait_pair", return_value=(driver, MagicMock())), \
         patch(f"{_INV}.login_to_linkedin"), \
         patch(f"{_INV}._profile_is_first_degree", return_value=False), \
         patch(f"{_INV}.find_first", routes.find_first), \
         patch(f"{_INV}.click_first", routes.click_first), \
         patch(f"{_INV}.click_element_wait_retry", routes.click_element_wait_retry), \
         patch(f"{_INV}.time.sleep"), \
         patch(f"{_INV}.log_error") as log_error, \
         patch(f"{_INV}.log_warning") as log_warning, \
         patch(f"{_INV}.insert_new_log") as insert_log, \
         patch(f"{_INV}.record_action"), \
         patch(f"{_INV}.quit_gracefully"):
        sent, reason = ra.invite_to_connect_now(1, _PROFILE_URL, message)
    routes.dialog_miss_recorded = miss.call_count
    routes.holds_set = hold.call_args_list
    return sent, reason, driver, insert_log, log_error, log_warning


class TestOwnCustomInviteAnchorRoute:
    """#1733: the target's own `/preload/custom-invite/?vanityName=<slug>` link, CLICKED.

    Both live layouts render that link — one on the top card as an `<a>`, the other inside the More
    menu — and navigating its URL renders a blank document, so clicking it where the page put it is
    the route that actually opens the dialog.
    """

    _OWN = "https://www.linkedin.com/preload/custom-invite/?vanityName=jane-doe-123"
    _RAIL = "https://www.linkedin.com/preload/custom-invite/?vanityName=bob-smith-9"

    def test_the_top_card_anchor_opens_the_dialog_before_any_menu_is_touched(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        routes = _Routes(anchor_hrefs=[self._OWN], dialog_after_anchor=True,
                         send_xpaths={_SEND_BARE_XPATH})
        sent, reason, driver, _log, log_error, log_warning = _invite(routes)

        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert routes.clicked_anchor_hrefs == [self._OWN]
        assert routes.click_labels == []  # no Connect button, no More menu, no menu item
        # The dead URL route is never reached once a real route worked.
        assert not any(_CUSTOM_INVITE_URL == c.args[0] for c in driver.get.call_args_list)
        log_error.assert_not_called()
        log_warning.assert_not_called()

    def test_the_menu_anchor_opens_the_dialog_when_the_top_card_has_none(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        routes = _Routes(anchor_hrefs=[], anchor_hrefs_after_menu=[self._OWN],
                         more_menu=True, dialog_after_anchor=True,
                         send_xpaths={_SEND_BARE_XPATH})
        sent, reason, _driver, _log, log_error, log_warning = _invite(routes)

        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert routes.clicked_anchor_hrefs == [self._OWN]
        assert "Profile More menu" in routes.click_labels
        # The menu's own anchor answered, so the label-matched menu-item locator never runs.
        assert "Connect menu item" not in routes.click_labels
        log_error.assert_not_called()
        log_warning.assert_not_called()

    def test_an_anchor_naming_someone_else_is_never_clicked(self):
        # The #1012 hazard in its current shape: the suggestion rail renders one custom-invite
        # anchor per suggested person, and they are on the page beside the target's own.
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        routes = _Routes(anchor_hrefs=[self._RAIL], dialog_after_anchor=True)
        sent, reason, _driver, _log, log_error, log_warning = _invite(routes)

        assert routes.clicked_anchor_hrefs == []
        assert sent is False and reason == NO_CONNECT_BUTTON_MESSAGE
        log_error.assert_not_called()
        log_warning.assert_called_once()

    def test_the_targets_own_anchor_is_picked_out_from_among_the_rails(self):
        routes = _Routes(anchor_hrefs=[self._RAIL, self._OWN, self._RAIL],
                         dialog_after_anchor=True, send_xpaths={_SEND_BARE_XPATH})
        sent, _reason, _driver, _log, _err, _warn = _invite(routes)

        assert sent is True
        assert routes.clicked_anchor_hrefs == [self._OWN]

    def test_a_slug_that_merely_starts_with_the_targets_is_not_the_target(self):
        # Exact equality, never a prefix: `jane-doe-123` must not match `jane-doe-1234`.
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        routes = _Routes(
            anchor_hrefs=["https://www.linkedin.com/preload/custom-invite/?vanityName=jane-doe-1234"],
            dialog_after_anchor=True)
        sent, reason, _driver, _log, _err, log_warning = _invite(routes)

        assert routes.clicked_anchor_hrefs == []
        assert sent is False and reason == NO_CONNECT_BUTTON_MESSAGE
        log_warning.assert_called_once()

    def test_an_anchor_whose_href_cannot_be_read_is_refused(self):
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        routes = _Routes(anchor_hrefs=[None], dialog_after_anchor=True)
        sent, reason, _driver, _log, _err, log_warning = _invite(routes)

        assert routes.clicked_anchor_hrefs == []
        assert sent is False and reason == NO_CONNECT_BUTTON_MESSAGE
        log_warning.assert_called_once()

    def test_a_clicked_anchor_that_opens_no_dialog_is_still_a_miss(self):
        # Success is the DIALOG, never the click — the same gate every other route answers to.
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        routes = _Routes(anchor_hrefs=[self._OWN], dialog_after_anchor=False)
        sent, reason, _driver, _log, log_error, log_warning = _invite(routes)

        assert routes.clicked_anchor_hrefs == [self._OWN]
        assert sent is False and reason == NO_CONNECT_BUTTON_MESSAGE
        log_error.assert_not_called()
        log_warning.assert_called_once()


class TestCustomInviteUrlRoute:
    def test_the_url_route_is_the_last_resort_after_every_click_route_missed(self):
        # It still works where it works, but it is no longer tried first: navigating that URL
        # renders a blank document on the current SDUI, which is what cost 20 invites (#1733).
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        routes = _Routes(dialog_on_url=True, send_xpaths={_SEND_BARE_XPATH})
        sent, reason, driver, _log, log_error, log_warning = _invite(routes)

        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert any(_CUSTOM_INVITE_URL == c.args[0] for c in driver.get.call_args_list)
        # The profile is loaded FIRST and every click route is tried before the navigation.
        profile_index = next(i for i, c in enumerate(driver.get.call_args_list)
                             if c.args[0] == _PROFILE_URL)
        url_index = next(i for i, c in enumerate(driver.get.call_args_list)
                         if c.args[0] == _CUSTOM_INVITE_URL)
        assert profile_index < url_index
        assert routes.click_labels == ["Profile Connect button", "Profile More menu"]
        log_error.assert_not_called()
        log_warning.assert_not_called()

    def test_no_locator_on_the_open_path_can_hit_the_suggestion_rail(self):
        # The regression that sent invites to random rail people: an unscoped
        # //main//button[contains(@aria-label,"Invite ")] click. No locator on the
        # dialog-open path may carry that shape again.
        routes = _Routes(dialog_on_url=True, send_xpaths={_SEND_BARE_XPATH})
        _invite(routes)
        assert not any('"Invite ' in loc for loc in routes.all_locators)


class TestDirectConnectButtonRoute:
    """Issue #1734: the direct top-card Connect button route.

    Some profiles render a bare "Connect" button directly on the top card instead of burying it
    behind the More menu — a route the URL/More-menu chain alone was missing.
    """

    def test_direct_button_opens_the_dialog_when_the_url_route_renders_none(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        routes = _Routes(dialog_after_connect_button=True, connect_button=True,
                         send_xpaths={_SEND_BARE_XPATH})
        sent, reason, driver, _log, log_error, log_warning = _invite(routes)

        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert routes.click_labels == ["Profile Connect button"]
        assert "Profile More menu" not in routes.click_labels
        assert any(_PROFILE_URL == c.args[0] for c in driver.get.call_args_list)
        log_error.assert_not_called()
        log_warning.assert_not_called()

    def test_falls_through_to_the_more_menu_when_the_direct_button_click_lands_no_dialog(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        routes = _Routes(connect_button=True,  # clicks, but no dialog behind it
                         dialog_after_menu=True, more_menu=True, menu_item=True,
                         send_xpaths={_SEND_BARE_XPATH})
        sent, reason, _driver, _log, log_error, log_warning = _invite(routes)

        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert routes.click_labels == ["Profile Connect button", "Profile More menu",
                                       "Connect menu item"]
        log_error.assert_not_called()
        log_warning.assert_not_called()

    def test_no_locator_on_the_direct_button_route_can_hit_the_suggestion_rail(self):
        # Same #1012 hazard guard as the URL route: nothing here may click a control naming
        # someone other than the target.
        routes = _Routes(dialog_after_connect_button=True, connect_button=True,
                         send_xpaths={_SEND_BARE_XPATH})
        _invite(routes)
        assert not any('"Invite ' in loc for loc in routes.all_locators)


class TestMoreMenuFallback:
    def test_menu_route_opens_the_dialog_when_the_url_route_renders_none(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        routes = _Routes(dialog_after_menu=True, more_menu=True, menu_item=True,
                         send_xpaths={_SEND_BARE_XPATH})
        sent, reason, driver, _log, log_error, log_warning = _invite(routes)

        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert routes.click_labels == ["Profile Connect button", "Profile More menu",
                                       "Connect menu item"]
        assert any(_PROFILE_URL == c.args[0] for c in driver.get.call_args_list)
        log_error.assert_not_called()
        log_warning.assert_not_called()

    def test_a_clicked_menu_item_without_a_dialog_is_still_a_miss(self):
        # Success is the DIALOG being present, not a click having landed.
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        routes = _Routes(more_menu=True, menu_item=True)  # dialog never renders
        sent, reason, _driver, _log, log_error, log_warning = _invite(routes)

        assert sent is False and reason == NO_CONNECT_BUTTON_MESSAGE
        log_error.assert_not_called()
        log_warning.assert_called_once()


class TestNoRouteOpensTheDialog:
    def test_stops_with_a_named_reason_and_no_note_or_send_attempt(self):
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        routes = _Routes()
        sent, reason, _driver, insert_log, log_error, log_warning = _invite(
            routes, message="hi jane")

        assert sent is False and reason == NO_CONNECT_BUTTON_MESSAGE
        insert_log.assert_called_once()
        assert insert_log.call_args.kwargs["message"] == NO_CONNECT_BUTTON_MESSAGE
        # With no dialog open, the note/send steps never run (no click attempts at all).
        assert routes.legacy_click_xpaths == []
        log_error.assert_not_called()
        log_warning.assert_called_once()
        assert log_warning.call_args.kwargs["user_id"] == 1


class TestSluglessProfileUrl:
    def test_a_url_without_a_slug_skips_straight_to_the_profile_route(self):
        from cqc_lem.app.engagement import invites as ra
        routes = _Routes()
        driver = MagicMock()
        driver.current_url = "about:blank"
        driver.find_elements.side_effect = routes.find_elements
        with patch(f"{_INV}.find_first", routes.find_first), \
             patch(f"{_INV}.click_first", routes.click_first), \
             patch(f"{_INV}.log_warning") as log_warning, \
             patch(f"{_INV}.log_debug"):
            opened, restriction = ra._open_connect_invite_dialog(
                driver, MagicMock(), 1, "https://www.linkedin.com/company/acme/")

        assert opened is False and restriction is None
        # No custom-invite navigation happened — there is no /in/ slug to build it from.
        assert not any("custom-invite" in str(c.args[0]) for c in driver.get.call_args_list)
        assert "Profile More menu" in routes.click_labels
        log_warning.assert_called_once()


class TestDirectConnectButtonLocatorAgainstRealMarkup:
    """`_PROFILE_CONNECT_BUTTON_LOCATORS` evaluated against actual HTML, not the `_Routes` stub.

    The stub only asserts locator STRINGS never contain '"Invite ' literally, which would not
    catch a locator that matches a rail button by visible text alone. The 2026-08-03 grounding
    only proved the rail's ARIA-LABEL names the suggested person; it never grounded that the
    rail's visible text does too, so a bare `normalize-space()="Connect"` match is not provably
    safe against a button like `<button aria-label="Invite Jane Doe to connect">Connect</button>`
    — a short visible label with a longer accessible name is a common pattern. This test proves
    the shipped locator excludes that shape regardless of visible text.
    """

    _TOP_CARD_BUTTON = '<button aria-label="Connect">Connect</button>'
    _TOP_CARD_BUTTON_NO_ARIA = '<button>Connect</button>'
    _RAIL_BUTTON_NAMED_ARIA = '<button aria-label="Invite Jane Doe to connect">Connect</button>'

    def _matches(self, button_html: str) -> bool:
        import lxml.html

        from cqc_lem.app.engagement import invites as ra
        tree = lxml.html.fromstring(f"<html><body><main>{button_html}</main></body></html>")
        xpath = ra._PROFILE_CONNECT_BUTTON_LOCATORS[0][1]
        return len(tree.xpath(xpath)) > 0

    def test_matches_the_targets_own_bare_connect_button(self):
        assert self._matches(self._TOP_CARD_BUTTON) is True

    def test_matches_even_when_the_targets_button_carries_no_aria_label(self):
        assert self._matches(self._TOP_CARD_BUTTON_NO_ARIA) is True

    def test_never_matches_a_button_whose_aria_label_names_someone_else(self):
        # Same visible text ("Connect") as the target's own button — only the accessible name
        # differs, which is exactly the rail-button shape the 2026-08-03 grounding never ruled out.
        assert self._matches(self._RAIL_BUTTON_NAMED_ARIA) is False


class TestTheDialogIsFoundAcrossAShadowBoundary:
    """#1733: the Connect dialog moved into an open shadow root, and that is why every route missed.

    Neither XPath nor `driver.find_elements` crosses a shadow boundary, so an OPEN dialog read
    EXACTLY like one that never opened — the same rotation #1621 found under the share-box composer.
    The click behind each route was working the whole time; only the reading was blind.
    """

    def _control(self, label):
        control = MagicMock()
        control.get_attribute.side_effect = lambda name: label if name == "aria-label" else None
        control.text = ""
        return control

    def test_a_shadow_mounted_dialog_counts_as_present(self):
        from cqc_lem.app.engagement import invites as ra
        deep = [self._control("Dismiss"), self._control("Send without a note")]
        with patch(f"{_INV}.find_first", return_value=None), \
             patch(f"{_INV}.find_deep_elements", return_value=deep):
            assert ra._connect_dialog_present(MagicMock(), MagicMock(), 1) is True

    def test_an_empty_shadow_read_is_still_a_miss(self):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.find_first", return_value=None), \
             patch(f"{_INV}.find_deep_elements", return_value=[]):
            assert ra._connect_dialog_present(MagicMock(), MagicMock(), 1) is False

    def test_the_light_dom_lookup_still_answers_first(self):
        # An account not yet moved to the shadow-mounted overlay must not pay for a JS query.
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.find_first", return_value=MagicMock()), \
             patch(f"{_INV}.find_deep_elements") as deep:
            assert ra._connect_dialog_present(MagicMock(), MagicMock(), 1) is True
        deep.assert_not_called()

    def test_the_send_click_reaches_a_shadow_mounted_button(self):
        from cqc_lem.app.engagement import invites as ra
        send = self._control("Send without a note")
        with patch(f"{_INV}.click_element_wait_retry", side_effect=Exception("not in light DOM")), \
             patch(f"{_INV}.find_deep_elements", return_value=[send]), \
             patch(f"{_INV}.log_error") as log_error:
            assert ra._submit_connect_invite(MagicMock(), MagicMock(), 1, with_note=False) is True
        send.click.assert_called_once()
        log_error.assert_not_called()

    def test_a_control_naming_a_different_action_is_never_pressed(self):
        # Label matching is a prefix match on the dialog's OWN vocabulary; "Dismiss" is not a Send.
        from cqc_lem.app.engagement import invites as ra
        dismiss = self._control("Dismiss")
        with patch(f"{_INV}.click_element_wait_retry", side_effect=Exception("not in light DOM")), \
             patch(f"{_INV}.find_deep_elements", return_value=[dismiss]), \
             patch(f"{_INV}.log_error") as log_error:
            assert ra._submit_connect_invite(MagicMock(), MagicMock(), 1, with_note=False) is False
        dismiss.click.assert_not_called()
        log_error.assert_called_once()

    def test_the_preferred_send_label_follows_whether_a_note_was_attached(self):
        from cqc_lem.app.engagement import invites as ra
        bare, noted = self._control("Send without a note"), self._control("Send invitation")
        for with_note, expected in ((True, noted), (False, bare)):
            for control in (bare, noted):
                control.click.reset_mock()
            with patch(f"{_INV}.click_element_wait_retry", side_effect=Exception("light DOM miss")), \
                 patch(f"{_INV}.find_deep_elements", return_value=[bare, noted]):
                assert ra._submit_connect_invite(MagicMock(), MagicMock(), 1,
                                                 with_note=with_note) is True
            expected.click.assert_called_once()
