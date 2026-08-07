"""The Connect-dialog open on the invite send path (issues #571 + the 2026-08-03 rail hazard).

The SDUI profile top card carries no "Invite <name> to connect" button — the only aria-labels
matching the old unscoped locator are the "More profiles for you" rail, so clicking the first
match INVITED A RANDOM SUGGESTED PERSON and then failed on the missing Send dialog. The rebuilt
route goes straight to the /preload/custom-invite URL (no wrong-person hazard), falls back to
the top-card More menu's Connect item, and only reports success when the dialog's own controls
are provably present. A total miss stays a WARNING (ordinary outcome), never an error.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"

_PROFILE_URL = "https://www.linkedin.com/in/jane-doe-123/"
_CUSTOM_INVITE_URL = "https://www.linkedin.com/preload/custom-invite/?vanityName=jane-doe-123"

_SEND_BARE_XPATH = '//button[contains(@aria-label,"Send without a note")]'


class _Routes:
    """Configurable stand-ins for find_first / click_first / click_element_wait_retry, recording
    every locator so the rail-hazard regression can assert nothing 'Invite …' is ever clicked.
    """

    def __init__(self, dialog_on_url=False, dialog_after_menu=False,
                 more_menu=False, menu_item=False, send_xpaths=frozenset()):
        self.dialog_on_url = dialog_on_url
        self.dialog_after_menu = dialog_after_menu
        self.more_menu = more_menu
        self.menu_item = menu_item
        self.send_xpaths = set(send_xpaths)
        self.menu_item_clicked = False
        self.find_labels: list[str] = []
        self.click_labels: list[str] = []
        self.all_locators: list[str] = []
        self.legacy_click_xpaths: list[str] = []  # click_element_wait_retry (note/send steps)

    def find_first(self, driver, wait, locators, label, **kwargs):
        self.find_labels.append(label)
        self.all_locators += [v for _, v in locators]
        if label == "Connect invite dialog":
            if self.dialog_on_url and not self.menu_item_clicked:
                return MagicMock()
            if self.dialog_after_menu and self.menu_item_clicked:
                return MagicMock()
        return None

    def click_first(self, driver, wait, locators, label, **kwargs):
        self.click_labels.append(label)
        self.all_locators += [v for _, v in locators]
        if label == "Profile More menu" and self.more_menu:
            return MagicMock()
        if label == "Connect menu item" and self.menu_item:
            self.menu_item_clicked = True
            return MagicMock()
        return None

    def click_element_wait_retry(self, driver, wait, xpath, label, **kwargs):
        self.all_locators.append(xpath)
        self.legacy_click_xpaths.append(xpath)
        if xpath in self.send_xpaths:
            return MagicMock()
        raise Exception(f"no element for {xpath}")


def _invite(routes: _Routes, message: str = None):
    from cqc_lem.app import run_automation as ra
    driver = MagicMock()
    driver.current_url = "about:blank"
    with patch(f"{_RA}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch(f"{_RA}.get_driver_wait_pair", return_value=(driver, MagicMock())), \
         patch(f"{_RA}.login_to_linkedin"), \
         patch(f"{_RA}._profile_is_first_degree", return_value=False), \
         patch(f"{_RA}.find_first", routes.find_first), \
         patch(f"{_RA}.click_first", routes.click_first), \
         patch(f"{_RA}.click_element_wait_retry", routes.click_element_wait_retry), \
         patch(f"{_RA}.time.sleep"), \
         patch(f"{_RA}.log_error") as log_error, \
         patch(f"{_RA}.log_warning") as log_warning, \
         patch(f"{_RA}.insert_new_log") as insert_log, \
         patch(f"{_RA}.record_action"), \
         patch(f"{_RA}.quit_gracefully"):
        sent, reason = ra.invite_to_connect_now(1, _PROFILE_URL, message)
    return sent, reason, driver, insert_log, log_error, log_warning


class TestCustomInviteUrlRoute:
    def test_dialog_via_url_sends_without_touching_the_profile_menus(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        routes = _Routes(dialog_on_url=True, send_xpaths={_SEND_BARE_XPATH})
        sent, reason, driver, _log, log_error, log_warning = _invite(routes)

        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert any(_CUSTOM_INVITE_URL == c.args[0] for c in driver.get.call_args_list)
        assert "Profile More menu" not in routes.click_labels
        log_error.assert_not_called()
        log_warning.assert_not_called()

    def test_no_locator_on_the_open_path_can_hit_the_suggestion_rail(self):
        # The regression that sent invites to random rail people: an unscoped
        # //main//button[contains(@aria-label,"Invite ")] click. No locator on the
        # dialog-open path may carry that shape again.
        routes = _Routes(dialog_on_url=True, send_xpaths={_SEND_BARE_XPATH})
        _invite(routes)
        assert not any('"Invite ' in loc for loc in routes.all_locators)


class TestMoreMenuFallback:
    def test_menu_route_opens_the_dialog_when_the_url_route_renders_none(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        routes = _Routes(dialog_after_menu=True, more_menu=True, menu_item=True,
                         send_xpaths={_SEND_BARE_XPATH})
        sent, reason, driver, _log, log_error, log_warning = _invite(routes)

        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert routes.click_labels == ["Profile More menu", "Connect menu item"]
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
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        routes = _Routes()
        driver = MagicMock()
        driver.current_url = "about:blank"
        with patch(f"{_RA}.find_first", routes.find_first), \
             patch(f"{_RA}.click_first", routes.click_first), \
             patch(f"{_RA}.log_warning") as log_warning, \
             patch(f"{_RA}.log_debug"):
            opened = ra._open_connect_invite_dialog(driver, MagicMock(), 1,
                                                    "https://www.linkedin.com/company/acme/")

        assert opened is False
        # No custom-invite navigation happened — there is no /in/ slug to build it from.
        assert not any("custom-invite" in str(c.args[0]) for c in driver.get.call_args_list)
        assert "Profile More menu" in routes.click_labels
        log_warning.assert_called_once()
