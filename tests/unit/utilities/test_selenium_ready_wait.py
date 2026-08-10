"""Unit tests for `_wait_for_selenium_ready`, the Grid readiness poll in front of every driver.

Every other unit test patches this function out — `tests/unit/conftest.py::_no_real_selenium` does
it lane-wide, because the real loop sleeps against a 60s deadline and nothing in the unit lane has
a Grid to answer it. That makes this file the only thing holding the loop's contract.

Two details make that work. The function is bound at import time, which happens during collection,
before any fixture has replaced the module attribute — so `_real_wait` is the genuine function and
not the guard's mock. And `time` is swapped for a fake clock rather than having `time.sleep`
no-op'd: with a real clock a no-op sleep turns the deadline branch into a multi-second spin, and
patching the global `time.time` would follow the success path into the logger.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.selenium_util import _wait_for_selenium_ready as _real_wait

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.selenium_util"


class _Clock:
    """A `time` stand-in whose only way to advance is a sleep the code under test asks for."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _response(status_code=200, ready=True):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"value": {"ready": ready}}
    return resp


class TestReadyGridReturns:
    def test_a_ready_grid_returns_without_sleeping(self):
        clock = _Clock()
        with patch(f"{_MOD}.requests.get", return_value=_response()) as get, \
             patch(f"{_MOD}.time", SimpleNamespace(time=clock.time, sleep=clock.sleep)):
            _real_wait("selenium-chrome", "4444", timeout=60)
        assert get.call_args.args[0] == "http://selenium-chrome:4444/wd/hub/status"
        assert clock.slept == []

    def test_a_200_that_is_not_ready_yet_keeps_polling(self):
        # The status endpoint answers well before the node can take a session, so 200 alone is
        # never enough — `value.ready` is the signal.
        clock = _Clock()
        with patch(f"{_MOD}.requests.get",
                   side_effect=[_response(ready=False), _response(ready=True)]), \
             patch(f"{_MOD}.time", SimpleNamespace(time=clock.time, sleep=clock.sleep)):
            _real_wait("h", "1", timeout=60)
        assert clock.slept == [2]


class TestUnreadyGridRaises:
    def test_it_raises_TimeoutError_once_the_deadline_passes(self):
        clock = _Clock()
        with patch(f"{_MOD}.requests.get", side_effect=OSError("no route to host")), \
             patch(f"{_MOD}.time", SimpleNamespace(time=clock.time, sleep=clock.sleep)), \
             pytest.raises(TimeoutError, match="Selenium not ready"):
            _real_wait("selenium-chrome", "4444", timeout=6)
        # Polls until the deadline and no further: 6s of budget at 2s a sleep.
        assert clock.slept == [2, 2, 2]

    def test_a_refused_connection_is_retried_rather_than_propagated(self):
        # A Grid that is still booting refuses the connection. That must not escape as OSError —
        # every caller of this function is written against TimeoutError.
        clock = _Clock()
        with patch(f"{_MOD}.requests.get",
                   side_effect=[OSError("refused"), OSError("refused"), _response()]) as get, \
             patch(f"{_MOD}.time", SimpleNamespace(time=clock.time, sleep=clock.sleep)):
            _real_wait("h", "1", timeout=60)
        assert get.call_count == 3

    def test_the_message_names_the_endpoint_that_never_answered(self):
        clock = _Clock()
        with patch(f"{_MOD}.requests.get", side_effect=OSError("refused")), \
             patch(f"{_MOD}.time", SimpleNamespace(time=clock.time, sleep=clock.sleep)), \
             pytest.raises(TimeoutError) as exc:
            _real_wait("selenium-chrome", "4444", timeout=6)
        assert "http://selenium-chrome:4444/wd/hub/status" in str(exc.value)
