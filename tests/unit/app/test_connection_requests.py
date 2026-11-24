"""Unit tests for the proactive connection-request task + scanner (issue #398)."""

from unittest.mock import patch

import pytest

from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE

pytestmark = pytest.mark.unit

# The connect rail moved to its own module (#1154); patches for it must bind THERE, because that
# is the module whose globals the invite code reads.
_INV = "cqc_lem.app.engagement.invites"
_RS = "cqc_lem.app.run_scheduler"


class TestSendConnectionRequest:
    def _req(self, status="approved", recipient_email=None):
        return {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/jane",
                "message": "hi jane", "status": status, "recipient_email": recipient_email}

    def test_sends_and_marks_sent(self):
        from cqc_lem.app.engagement import invites as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now", return_value=(True, "Connection Request Sent Successfully")) as send, \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
             patch("cqc_lem.utilities.db.record_connection_request_attempt") as rec:
            out = ra.send_connection_request(3)
        send.assert_called_once_with(1, "https://x/in/jane", "hi jane", recipient_email=None)
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.SENT, count_attempt=True)
        rec.assert_not_called()  # a sent invite is terminal already, no attempt bookkeeping needed
        assert "sent" in out

    def test_passes_the_known_recipient_email_through(self):
        # issue #1836 — the one place a stored recipient_email reaches the send rail.
        from cqc_lem.app.engagement import invites as ra
        with patch("cqc_lem.utilities.db.get_connection_request",
                   return_value=self._req(recipient_email="jane@example.com")), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now",
                   return_value=(True, "Connection Request Sent Successfully")) as send, \
             patch("cqc_lem.utilities.db.update_connection_request_status"), \
             patch("cqc_lem.utilities.db.record_connection_request_attempt"):
            ra.send_connection_request(3)
        send.assert_called_once_with(1, "https://x/in/jane", "hi jane",
                                     recipient_email="jane@example.com")

    def test_email_verification_required_goes_terminal_without_burning_attempts(self):
        # Class C (#1836) — a target fact, not selector drift: terminal on the FIRST occurrence,
        # never through record_connection_request_attempt's ceiling.
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.db import EMAIL_VERIFICATION_REQUIRED_MESSAGE
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now",
                   return_value=(False, EMAIL_VERIFICATION_REQUIRED_MESSAGE)), \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
             patch("cqc_lem.utilities.db.record_connection_request_attempt") as rec:
            out = ra.send_connection_request(3)
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.FAILED,
                                    failure_reason=EMAIL_VERIFICATION_REQUIRED_MESSAGE)
        rec.assert_not_called()  # not "burn one of three attempts" — terminal immediately
        assert "failed" in out.lower()

    def test_failed_send_records_attempt_and_defers_below_ceiling(self):
        # A real attempt reached LinkedIn and did not send — counts toward the ceiling, but stays
        # 'approved' for another try while attempts remain.
        from cqc_lem.app.engagement import invites as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now", return_value=(False, NO_CONNECT_BUTTON_MESSAGE)), \
             patch("cqc_lem.utilities.db.record_connection_request_attempt", return_value=(False, 1)) as rec:
            out = ra.send_connection_request(3)
        rec.assert_called_once_with(3, NO_CONNECT_BUTTON_MESSAGE, terminal=False)
        assert "deferred" in out.lower()

    def test_failed_send_goes_terminal_at_ceiling(self):
        # The Nth real attempt still fails — the row must stop being dispatched.
        from cqc_lem.app.engagement import invites as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now", return_value=(False, NO_CONNECT_BUTTON_MESSAGE)), \
             patch("cqc_lem.utilities.db.record_connection_request_attempt", return_value=(True, 3)) as rec:
            out = ra.send_connection_request(3)
        rec.assert_called_once_with(3, NO_CONNECT_BUTTON_MESSAGE, terminal=False)
        assert "giving up" in out.lower()

    def test_defers_when_cap_reached(self):
        from cqc_lem.app.engagement import invites as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=10), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now") as send, \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
             patch("cqc_lem.utilities.db.record_connection_request_attempt") as rec:
            out = ra.send_connection_request(3)
        send.assert_not_called()  # over cap → never sends
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.APPROVED)  # deferred for next scan
        rec.assert_not_called()  # nothing was attempted — attempts must not move
        assert "cap" in out.lower()

    def test_defers_when_throttled(self):
        # Kill-switch / 429 breaker open mid-send → invite_to_connect_now raises → defer, not fail.
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now", side_effect=LinkedInRateLimited("throttled")), \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
             patch("cqc_lem.utilities.db.record_connection_request_attempt") as rec:
            out = ra.send_connection_request(3)
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.APPROVED)  # deferred, not failed
        rec.assert_not_called()  # throttled — nothing reached LinkedIn, attempts must not move
        assert "throttled" in out.lower()

    def test_defers_when_held_without_touching_attempts(self):
        # The account-level invite hold (#1000) defers exactly like the cap and the breaker do —
        # nothing was attempted, so attempts must not move.
        from cqc_lem.app.engagement import invites as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req()), \
             patch(f"{_INV}.is_invites_held", return_value=True), \
             patch(f"{_INV}.invite_hold_reason", return_value="LinkedIn walled the account"), \
             patch(f"{_INV}.invite_to_connect_now") as send, \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
             patch("cqc_lem.utilities.db.record_connection_request_attempt") as rec:
            out = ra.send_connection_request(3)
        send.assert_not_called()
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.APPROVED,
                                   failure_reason="LinkedIn walled the account")
        rec.assert_not_called()
        assert "deferred" in out.lower()

    def test_skips_non_sendable_status(self):
        from cqc_lem.app.engagement import invites as ra
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=self._req(status="sent")), \
             patch(f"{_INV}.invite_to_connect_now") as send:
            out = ra.send_connection_request(3)
        send.assert_not_called()
        assert "not sendable" in out


class TestBrandAcquisitionHold:
    """Owner ruling 2026-10-10 (#2376, option A): brand acquisition requests are never auto-sent.

    Every other request — the owner's own manual ones, roster and profile-viewer rows, and every
    other user's — sends as before.
    """

    def _send(self, user_id, source, brand_user_id="", approved_by=None):
        from cqc_lem.app.engagement import invites as ra
        req = {"id": 3, "user_id": user_id, "recipient_profile_url": "https://x/in/jane",
               "message": "hi jane", "status": "approved", "recipient_email": None,
               "source": source, "approved_by": approved_by}
        with patch("cqc_lem.utilities.brand_account.BRAND_USER_ID", brand_user_id), \
             patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now",
                   return_value=(True, "Connection Request Sent Successfully")) as send, \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
             patch("cqc_lem.utilities.db.record_connection_request_attempt"):
            out = ra.send_connection_request(3)
        return out, send, upd

    @pytest.mark.parametrize("source", ["own_post", "adjacent_post", "outreach_funnel"])
    def test_a_brand_acquisition_request_is_held_and_returned_to_pending(self, source):
        from cqc_lem.utilities.brand_account import ACQUISITION_HOLD_REASON
        from cqc_lem.utilities.db import ConnectionRequestStatus
        out, send, upd = self._send(1, source)
        send.assert_not_called()
        upd.assert_called_once_with(3, ConnectionRequestStatus.PENDING,
                                    failure_reason=ACQUISITION_HOLD_REASON)
        assert "held" in out

    @pytest.mark.parametrize("source", ["own_post", "adjacent_post", "outreach_funnel"])
    def test_a_human_approval_releases_the_hold(self, source):
        """#2623: the owner's explicit Approve is the send decision."""
        out, send, _upd = self._send(1, source, approved_by="user:1")
        send.assert_called_once()
        assert "sent" in out

    @pytest.mark.parametrize("approved_by", [None, "system:auto_queue"])
    def test_a_non_human_approval_stays_held(self, approved_by):
        _out, send, _upd = self._send(1, "adjacent_post", approved_by=approved_by)
        send.assert_not_called()

    def test_a_human_approval_still_honours_the_daily_cap(self):
        from cqc_lem.app.engagement import invites as ra
        req = {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/jane", "message": "m",
               "status": "approved", "recipient_email": None, "source": "adjacent_post",
               "approved_by": "user:1"}
        with patch("cqc_lem.utilities.brand_account.BRAND_USER_ID", ""), \
             patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=10), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.invite_to_connect_now") as send, \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd:
            out = ra.send_connection_request(3)
        send.assert_not_called()
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.APPROVED)
        assert "deferred" in out

    @pytest.mark.parametrize("source", [None, "", "roster", "profile_viewer"])
    def test_the_brand_accounts_ordinary_requests_still_send(self, source):
        out, send, _upd = self._send(1, source)
        send.assert_called_once()
        assert "sent" in out

    @pytest.mark.parametrize("source", ["own_post", "adjacent_post", "outreach_funnel"])
    def test_other_users_acquisition_requests_still_send(self, source):
        out, send, _upd = self._send(2, source)
        send.assert_called_once()
        assert "sent" in out

    def test_the_hold_follows_an_overridden_brand_user(self):
        _out, send, _upd = self._send(7, "own_post", brand_user_id="7")
        send.assert_not_called()
        _out, send, _upd = self._send(1, "own_post", brand_user_id="7")
        send.assert_called_once()


class TestInviteToConnectWrapper:
    def test_deferred_on_throttle(self):
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_INV}.invite_to_connect_now", side_effect=LinkedInRateLimited("throttled")):
            out = ra.invite_to_connect(1, "https://x/in/jane", "hi")
        assert "deferred" in out.lower()

    def test_returns_success_string(self):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.invite_to_connect_now", return_value=(True, "ok")):
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
