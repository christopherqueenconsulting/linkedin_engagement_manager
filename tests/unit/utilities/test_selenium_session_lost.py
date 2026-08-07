"""Unit tests for the lost-browser-session predicate (issue #988)."""

import pytest
from selenium.common import InvalidSessionIdException, NoSuchElementException, TimeoutException, WebDriverException

pytestmark = pytest.mark.unit

from cqc_lem.utilities.selenium_util import is_session_lost

_GRID_MESSAGE = (
    "Message: Unable to find session with ID: e5d140e95d14b7cef20ad67aec43a5d1. Session was "
    "removed at 2026-08-02T17:28:27Z (2 seconds ago), reason: session closed normally (QUIT "
    "command), node: http://selenium-node-debug:5555"
)


class TestIsSessionLost:
    def test_invalid_session_id_exception_is_a_lost_session(self):
        assert is_session_lost(InvalidSessionIdException(_GRID_MESSAGE)) is True

    def test_grid_message_on_a_bare_webdriver_exception_is_a_lost_session(self):
        """The same fault reaches us as a plain WebDriverException from a wrapped/older driver."""
        assert is_session_lost(WebDriverException(_GRID_MESSAGE)) is True

    @pytest.mark.parametrize("message", ["invalid session id", "No such session",
                                         "session deleted because of page crash"])
    def test_every_session_gone_marker_matches_case_insensitively(self, message):
        assert is_session_lost(WebDriverException(message)) is True

    def test_a_grid_we_cannot_reach_is_not_a_lost_session(self):
        """A hub that refuses connections is a different fault and must stay loud."""
        exc = WebDriverException("Failed to establish a new connection: Connection refused")
        assert is_session_lost(exc) is False

    @pytest.mark.parametrize("exc", [NoSuchElementException("no such element"),
                                     TimeoutException("timed out"),
                                     ValueError("unable to find session"),
                                     RuntimeError("boom")])
    def test_other_failures_are_not_lost_sessions(self, exc):
        assert is_session_lost(exc) is False
