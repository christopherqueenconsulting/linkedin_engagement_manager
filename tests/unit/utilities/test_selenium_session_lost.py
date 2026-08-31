"""Unit tests for the lost-browser-session predicate (issue #988), the crashed-tab one (#1746),
and the Grid-relay-error one (#1784).
"""

import pytest
from selenium.common import InvalidSessionIdException, NoSuchElementException, TimeoutException, WebDriverException

pytestmark = pytest.mark.unit

from cqc_lem.utilities.selenium_util import is_grid_relay_error, is_session_lost, is_tab_crashed

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


class TestIsTabCrashed:
    def test_a_crashed_tab_message_matches(self):
        exc = WebDriverException("Message: tab crashed\n  (Session info: chrome=151.0.7922.108)")
        assert is_tab_crashed(exc) is True

    def test_matches_case_insensitively(self):
        assert is_tab_crashed(WebDriverException("TAB CRASHED")) is True

    def test_a_lost_session_is_not_a_crashed_tab(self):
        """The two predicates cover distinct faults — a caller that needs both checks both."""
        assert is_tab_crashed(WebDriverException("Unable to find session with ID: abc")) is False

    @pytest.mark.parametrize("exc", [NoSuchElementException("no such element"),
                                     TimeoutException("timed out"),
                                     ValueError("tab crashed"),
                                     RuntimeError("boom")])
    def test_other_failures_are_not_crashed_tabs(self, exc):
        assert is_tab_crashed(exc) is False


class TestIsGridRelayError:
    def test_a_dropped_relay_request_matches(self):
        exc = WebDriverException(
            "Message: Failed to execute request (POST http://localhost:2867/session/abc/"
            "execute/sync)\nStacktrace:\njava.io.UncheckedIOException: Failed to execute request "
            "(POST http://localhost:2867/session/abc/execute/sync)"
        )
        assert is_grid_relay_error(exc) is True

    def test_matches_case_insensitively(self):
        assert is_grid_relay_error(WebDriverException("FAILED TO EXECUTE REQUEST")) is True

    def test_a_lost_session_is_not_a_grid_relay_error(self):
        """The three predicates cover distinct faults — a caller that needs all three checks all three."""
        assert is_grid_relay_error(WebDriverException("Unable to find session with ID: abc")) is False

    def test_a_crashed_tab_is_not_a_grid_relay_error(self):
        assert is_grid_relay_error(WebDriverException("tab crashed")) is False

    @pytest.mark.parametrize("exc", [NoSuchElementException("no such element"),
                                     TimeoutException("timed out"),
                                     ValueError("failed to execute request"),
                                     RuntimeError("boom")])
    def test_other_failures_are_not_grid_relay_errors(self, exc):
        assert is_grid_relay_error(exc) is False
