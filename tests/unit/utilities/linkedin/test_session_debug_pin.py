"""`get_current_profile` must carry the debug-node pin all the way down (issue #1301).

The pin is only worth anything if the request that actually opens the browser carries it. This is
the seam where it would go missing silently: `get_current_profile` is what every Selenium task —
and the live-validation probe — calls, and a dropped keyword here would put an agent's probe back
on a production Chrome slot with nothing failing.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.linkedin.session"


def _run(**kwargs):
    """Call `get_current_profile` with every I/O boundary mocked; return the driver-pair call."""
    from cqc_lem.utilities.linkedin.session import get_current_profile

    with patch(f"{_MOD}.get_user_password_pair_by_id", return_value=("a@b.c", "pw")), \
         patch(f"{_MOD}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())) as pair, \
         patch(f"{_MOD}.login_to_linkedin"), \
         patch(f"{_MOD}.get_my_profile", return_value=MagicMock()), \
         patch(f"{_MOD}.log_info"):
        get_current_profile(user_id=1, **kwargs)
    return pair.call_args


class TestDebugRequiredReachesTheSessionRequest:
    def test_it_is_forwarded_when_asked_for(self):
        assert _run(debug=True, debug_required=True).kwargs["debug_required"] is True

    def test_it_defaults_to_off_so_ordinary_tasks_keep_the_pool_fallback(self):
        # Every Celery lane calls this. If `required` leaked on by default, one busy debug node
        # would start failing real engagement work instead of just a probe.
        call = _run()
        assert call.kwargs["debug_required"] is False
        assert call.kwargs["debug"] is False


class TestLoginFormTimeoutLogsOnce:
    def test_timeout_is_debug_not_a_second_warning(self):
        # login_to_linkedin already warns with the page snippet (#1908); a repeat here escalated
        # to a grouped $exception for the same event (#2857).
        from selenium.common.exceptions import TimeoutException

        from cqc_lem.utilities.linkedin.session import get_current_profile

        with patch(f"{_MOD}.get_user_password_pair_by_id", return_value=("a@b.c", "pw")), \
             patch(f"{_MOD}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_MOD}.login_to_linkedin",
                   side_effect=TimeoutException("Finding Username Field")), \
             patch(f"{_MOD}.quit_gracefully"), \
             patch(f"{_MOD}.log_debug") as dbg, \
             patch(f"{_MOD}.log_warning") as warn, \
             patch(f"{_MOD}.log_error") as err:
            with pytest.raises(TimeoutException):
                get_current_profile(user_id=1)
        dbg.assert_called_once()
        warn.assert_not_called()
        err.assert_not_called()
