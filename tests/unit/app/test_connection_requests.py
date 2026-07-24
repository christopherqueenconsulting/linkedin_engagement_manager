"""Unit tests for the proactive connection-request task + scanner (issue #398)."""

import pytest
from unittest.mock import patch

from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"


class TestSendConnectionRequest:
    def _req(self, status="approved"):
        return {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/jane",
                "message": "hi jane", "status": status}

    def test_sends_and_marks_sent(self):
        from cqc_lem.app import run_automation as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_RA}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_RA}.invite_to_connect_now", return_value=True) as send, \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd:
            out = ra.send_connection_request(3)
        send.assert_called_once_with(1, "https://x/in/jane", "hi jane")
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.SENT)
        assert "sent" in out

    def test_failed_send_marks_failed(self):
        from cqc_lem.app import run_automation as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_RA}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_RA}.invite_to_connect_now", return_value=False), \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd:
            ra.send_connection_request(3)
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.FAILED)

    def test_defers_when_cap_reached(self):
        from cqc_lem.app import run_automation as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=10), \
             patch(f"{_RA}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_RA}.invite_to_connect_now") as send, \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd:
            out = ra.send_connection_request(3)
        send.assert_not_called()  # over cap → never sends
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.APPROVED)  # deferred for next scan
        assert "cap" in out.lower()

    def test_defers_when_throttled(self):
        # Kill-switch / 429 breaker open mid-send → invite_to_connect_now raises → defer, not fail.
        from cqc_lem.app import run_automation as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_RA}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_RA}.invite_to_connect_now", side_effect=LinkedInRateLimited("throttled")), \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd:
            out = ra.send_connection_request(3)
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.APPROVED)  # deferred, not failed
        assert "throttled" in out.lower()

    def test_skips_non_sendable_status(self):
        from cqc_lem.app import run_automation as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req(status="sent")), \
             patch(f"{_RA}.invite_to_connect_now") as send:
            out = ra.send_connection_request(3)
        send.assert_not_called()
        assert "not sendable" in out


class TestInviteToConnectWrapper:
    def test_deferred_on_throttle(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.invite_to_connect_now", side_effect=LinkedInRateLimited("throttled")):
            out = ra.invite_to_connect(1, "https://x/in/jane", "hi")
        assert "deferred" in out.lower()

    def test_returns_success_string(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.invite_to_connect_now", return_value=True):
            out = ra.invite_to_connect(1, "https://x/in/jane")
        assert out == "Connection Request Sent Successfully"


class TestAutoCheckConnectionRequests:
    def test_dispatches_within_budget(self):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_connection_requests", return_value=[(3, 1)]), \
             patch(f"{_RS}.get_orphaned_connection_requests", return_value=[]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_RS}.count_invites_sent_today", return_value=0), \
             patch(f"{_RS}.update_connection_request_status") as upd, \
             patch(f"{_RS}.send_connection_request") as task:
            out = rs.auto_check_connection_requests()
        task.apply_async.assert_called_once()
        assert task.apply_async.call_args.kwargs["kwargs"] == {"request_id": 3}
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.SENDING)
        assert "Dispatched 1" in out

    def test_respects_daily_cap_budget(self):
        # Two approved for the same user but the daily cap leaves only 1 send left today.
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_connection_requests", return_value=[(3, 1), (4, 1)]), \
             patch(f"{_RS}.get_orphaned_connection_requests", return_value=[]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.get_engagement_preferences", return_value={"max_invites_per_day": 5}), \
             patch(f"{_RS}.count_invites_sent_today", return_value=4), \
             patch(f"{_RS}.update_connection_request_status"), \
             patch(f"{_RS}.send_connection_request") as task:
            rs.auto_check_connection_requests()
        assert task.apply_async.call_count == 1  # only 1 of the budget of 1 dispatched

    def test_skips_when_throttled(self):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_RS}.get_approved_connection_requests") as approved, \
             patch(f"{_RS}.send_connection_request") as task:
            out = rs.auto_check_connection_requests()
        approved.assert_not_called()
        task.apply_async.assert_not_called()
        assert "throttled" in out.lower()

    def test_skips_inactive_user(self):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_connection_requests", return_value=[(3, 99)]), \
             patch(f"{_RS}.get_orphaned_connection_requests", return_value=[]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.update_connection_request_status") as upd, \
             patch(f"{_RS}.send_connection_request") as task:
            out = rs.auto_check_connection_requests()
        task.apply_async.assert_not_called()
        upd.assert_not_called()
        assert "No Connection Requests" in out

    def test_requeues_orphaned(self):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_connection_requests", return_value=[]), \
             patch(f"{_RS}.get_orphaned_connection_requests", return_value=[(7, 1)]) as orph, \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.update_connection_request_status") as upd, \
             patch(f"{_RS}.send_connection_request") as task:
            out = rs.auto_check_connection_requests()
        orph.assert_called_once_with(lookback_hours=2)
        task.apply_async.assert_called_once_with(kwargs={"request_id": 7})
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(7, ConnectionRequestStatus.SENDING)
        assert "re-queued 1 orphaned" in out
