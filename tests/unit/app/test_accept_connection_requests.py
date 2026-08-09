"""Unit tests for accept_connection_request (issue #570).

An empty invitation manager is the normal steady state and must NOT page the error cron, and
accepting must pair each click with its own card so we only DM people we actually connected with.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"


def _card(href="https://www.linkedin.com/in/jane?trk=x", link_text="Jane Doe", accept=True):
    """A mock invitation card: find_elements answers the profile-link chain first, then the
    accept-button chain (the locator chains are disjoint, so dispatch on the selector value).
    """
    link = MagicMock()
    link.get_attribute.return_value = href
    link.text = link_text
    button = MagicMock()

    card = MagicMock()

    def _find(_by, value):
        if "Accept" in value or "accept" in value:
            return [button] if accept else []
        return [link] if href else []

    card.find_elements.side_effect = _find
    card.accept_button = button
    return card


@pytest.fixture(autouse=True)
def _no_sleeps():
    with patch("time.sleep"):
        yield


@pytest.fixture
def _session():
    """Patch out login/driver/teardown so only the invitation logic is exercised."""
    driver = MagicMock()
    with patch(f"{_OUT}.get_user_password_pair_by_id", return_value=("u@e.com", "pw")), \
         patch(f"{_OUT}.get_driver_wait_pair", return_value=(driver, MagicMock())), \
         patch(f"{_OUT}.login_to_linkedin"), \
         patch(f"{_OUT}.wait_for_ajax"), \
         patch(f"{_OUT}.quit_gracefully") as quit_mock:
        yield driver, quit_mock


class TestAcceptConnectionRequest:
    def test_no_pending_invitations_is_not_an_error(self, _session):
        """The common case: zero invitations returns {} and never calls log_error (#570)."""
        from cqc_lem.app.engagement.outreach import accept_connection_request
        driver, quit_mock = _session
        with patch(f"{_OUT}.find_all_first", return_value=[]), \
             patch(f"{_OUT}.log_error") as err, \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.log_info") as info:
            assert accept_connection_request(1) == {}
        err.assert_not_called()
        warn.assert_not_called()
        info.assert_called_once()
        quit_mock.assert_called_once_with(driver)

    def test_accepts_each_card_and_returns_profile_url_to_name(self, _session):
        from cqc_lem.app.engagement.outreach import accept_connection_request
        jane = _card(href="https://www.linkedin.com/in/jane/?trk=x", link_text="Jane Doe")
        john = _card(href="https://www.linkedin.com/in/john", link_text="John Smith")
        with patch(f"{_OUT}.find_all_first", return_value=[jane, john]):
            result = accept_connection_request(1)
        assert result == {"https://www.linkedin.com/in/jane": "Jane Doe",
                          "https://www.linkedin.com/in/john": "John Smith"}
        jane.accept_button.click.assert_called_once()
        john.accept_button.click.assert_called_once()

    def test_does_not_reaccept_a_card_linkedin_left_in_place(self, _session):
        """LinkedIn sometimes keeps the accepted card rendered — the URL ledger stops a double click."""
        from cqc_lem.app.engagement.outreach import accept_connection_request
        jane = _card(href="https://www.linkedin.com/in/jane", link_text="Jane Doe")
        with patch(f"{_OUT}.find_all_first", return_value=[jane, jane]):
            result = accept_connection_request(1)
        assert result == {"https://www.linkedin.com/in/jane": "Jane Doe"}
        jane.accept_button.click.assert_called_once()

    def test_a_failed_click_is_warned_not_errored_and_is_not_dmed(self, _session):
        """A stale/blocked accept must not enter the return dict — we only DM people we connected with."""
        from selenium.common import ElementNotInteractableException

        from cqc_lem.app.engagement.outreach import accept_connection_request
        jane = _card(href="https://www.linkedin.com/in/jane", link_text="Jane Doe")
        jane.accept_button.click.side_effect = ElementNotInteractableException("covered")
        with patch(f"{_OUT}.find_all_first", return_value=[jane]), \
             patch(f"{_OUT}.log_error") as err, \
             patch(f"{_OUT}.log_warning") as warn:
            assert accept_connection_request(1) == {}
        err.assert_not_called()
        warn.assert_called_once()

    def test_cards_without_an_accept_button_are_skipped(self, _session):
        from cqc_lem.app.engagement.outreach import accept_connection_request
        pending = _card(href="https://www.linkedin.com/in/jane", link_text="Jane Doe")
        already = _card(href="https://www.linkedin.com/in/john", link_text="John Smith", accept=False)
        with patch(f"{_OUT}.find_all_first", return_value=[already, pending]):
            result = accept_connection_request(1)
        assert result == {"https://www.linkedin.com/in/jane": "Jane Doe"}

    def test_unexpected_failure_warns_instead_of_paging_the_error_cron(self, _session):
        from cqc_lem.app.engagement.outreach import accept_connection_request
        driver, quit_mock = _session
        driver.get.side_effect = RuntimeError("browser died")
        with patch(f"{_OUT}.log_error") as err, patch(f"{_OUT}.log_warning") as warn:
            assert accept_connection_request(1) == {}
        err.assert_not_called()
        warn.assert_called_once()
        quit_mock.assert_called_once_with(driver)


class TestNextPendingInvitation:
    def test_returns_none_when_no_card_has_an_accept_button(self):
        from cqc_lem.app.engagement.outreach import _next_pending_invitation
        card = _card(href="https://www.linkedin.com/in/jane", accept=False)
        with patch(f"{_OUT}.find_all_first", return_value=[card]):
            assert _next_pending_invitation(MagicMock(), set()) is None

    def test_accepts_a_card_with_no_readable_profile_link(self):
        """A missing name/url must not block the accept — it just yields no DM target."""
        from cqc_lem.app.engagement.outreach import _next_pending_invitation
        card = _card(href="", link_text="")
        with patch(f"{_OUT}.find_all_first", return_value=[card]):
            profile_url, name, button = _next_pending_invitation(MagicMock(), set())
        assert profile_url == ""
        assert name == ""
        assert button is card.accept_button

    def test_skips_already_accepted_urls(self):
        from cqc_lem.app.engagement.outreach import _next_pending_invitation
        card = _card(href="https://www.linkedin.com/in/jane")
        with patch(f"{_OUT}.find_all_first", return_value=[card]):
            assert _next_pending_invitation(MagicMock(), {"https://www.linkedin.com/in/jane"}) is None
