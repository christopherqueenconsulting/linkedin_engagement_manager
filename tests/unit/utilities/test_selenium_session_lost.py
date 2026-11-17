"""Unit tests for the browser-session-fault predicates.

Covers the lost-session predicate (issue #988), the crashed-tab one (#1746), and the
Grid-relay-error one (#1784).
"""

from unittest.mock import patch

import pytest
from selenium.common import InvalidSessionIdException, NoSuchElementException, TimeoutException, WebDriverException

pytestmark = pytest.mark.unit

from cqc_lem.utilities.selenium_util import (
    is_grid_relay_error,
    is_proxy_tunnel_failure,
    is_session_lost,
    is_tab_crashed,
)

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


class TestIsProxyTunnelFailure:
    def test_a_tunnel_failure_matches(self):
        exc = WebDriverException("Message: unknown error: net::ERR_TUNNEL_CONNECTION_FAILED")
        assert is_proxy_tunnel_failure(exc) is True

    @pytest.mark.parametrize("exc", [WebDriverException("tab crashed"),
                                     ValueError("ERR_TUNNEL_CONNECTION_FAILED")])
    def test_other_failures_do_not_match(self, exc):
        assert is_proxy_tunnel_failure(exc) is False


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

    def test_a_relay_client_with_a_shut_down_executor_matches(self):
        """Issue #2478: the same drop, refused by the Grid's already-terminated HTTP client executor."""
        exc = WebDriverException(
            "Message: Task java.util.concurrent.FutureTask@31a5783a[Not completed, task = "
            "java.util.concurrent.Executors$RunnableAdapter@3b5d134a[Wrapped task = "
            "org.openqa.selenium.remote.http.jdk.JdkHttpClient$$Lambda/0x00007601b029ef50@359afa51]] "
            "rejected from java.util.concurrent.ThreadPoolExecutor@6f1d2a3b[Terminated, pool size = 0]"
        )
        assert is_grid_relay_error(exc) is True
        assert is_session_lost(exc) is False

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


class TestNoteTabCrash:
    """The ONE crash decision for a lane's run-ending failure (#2393, #2402, #2403)."""

    _SU = "cqc_lem.utilities.selenium_util"

    def _call(self, exc, **kw):
        from cqc_lem.utilities.selenium_util import note_tab_crash
        with patch(f"{self._SU}.log_debug") as debug, \
             patch(f"{self._SU}.log_warning") as warning:
            crashed = note_tab_crash(exc, task_name="some_lane", user_id=7, **kw)
        return crashed, debug, warning

    def test_a_crashed_tab_is_one_warning_naming_the_lane(self):
        exc = WebDriverException("Message: tab crashed")
        crashed, debug, warning = self._call(exc)
        assert crashed is True
        warning.assert_called_once()
        assert warning.call_args.args[0] == "Browser tab crashed — ending some_lane"
        assert warning.call_args.kwargs == {"exc": exc, "task_name": "some_lane", "user_id": 7}
        debug.assert_not_called()

    def test_a_crash_already_reported_upstream_is_debug(self):
        crashed, debug, warning = self._call(WebDriverException("tab crashed"),
                                             crash_logged_upstream=True)
        assert crashed is True
        debug.assert_called_once()
        warning.assert_not_called()

    @pytest.mark.parametrize("exc", [WebDriverException("Unable to find session with ID: abc"),
                                     RuntimeError("boom")])
    def test_anything_else_is_left_to_the_lane_and_logs_nothing(self, exc):
        crashed, debug, warning = self._call(exc, crash_logged_upstream=True)
        assert crashed is False
        debug.assert_not_called()
        warning.assert_not_called()
