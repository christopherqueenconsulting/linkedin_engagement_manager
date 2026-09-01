"""Unit tests for the connection-request API endpoints (issue #398)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


_S = "tok"
_U = 5


class TestCreateConnectionRequest:
    def test_no_status_auto_approve_mode_queues(self, api_client):
        # Default mode auto_approve → a newly-added target is queued (APPROVED) with no explicit status.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_engagement_preferences",
                   return_value={"connection_request_mode": "auto_approve"}), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=11) as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "recipient_name": "Jane", "message": "Hi Jane"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["request_id"] == 11
        from cqc_lem.utilities.db import ConnectionRequestStatus
        assert ins.call_args.kwargs["status"] == ConnectionRequestStatus.APPROVED

    def test_no_status_pre_review_mode_pending(self, api_client):
        # Pre-review mode → a newly-added target waits as a draft (PENDING) for human approval.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_engagement_preferences",
                   return_value={"connection_request_mode": "pre_review"}), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=11) as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ConnectionRequestStatus
        assert ins.call_args.kwargs["status"] == ConnectionRequestStatus.PENDING

    def test_explicit_approved_status_maps(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=12) as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "status": "approved"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ConnectionRequestStatus
        assert ins.call_args.kwargs["status"] == ConnectionRequestStatus.APPROVED

    def test_invalid_status_rejected_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=1) as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "status": "sent"})
        assert resp.status_code == 422
        assert "Invalid status" in resp.json()["detail"]
        ins.assert_not_called()

    def test_over_limit_note_rejected_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=1) as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "message": "x" * 301})
        assert resp.status_code == 422
        ins.assert_not_called()

    def test_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.post("/api/connection_request", json={
                "session_token": "bad", "recipient_profile_url": "u"})
        assert resp.status_code == 401


class TestRecipientEmail:
    """Issue #1836 — accept a known email and use it to clear LinkedIn's verification challenge."""

    def test_a_known_email_is_persisted(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=11) as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "recipient_email": "jane@example.com"})
        assert resp.status_code == 200
        assert ins.call_args.kwargs["recipient_email"] == "jane@example.com"

    def test_a_malformed_email_is_rejected_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=11) as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "recipient_email": "not-an-email"})
        assert resp.status_code == 422
        ins.assert_not_called()

    def test_an_agent_scoped_session_supplying_an_email_still_lands_pending(self, api_client):
        # Section 3's guarantee: adding a field must not add a fourth road to APPROVED.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main._agent_scoped", return_value=True), \
             patch("cqc_lem.api.main.get_engagement_preferences",
                   return_value={"connection_request_mode": "auto_approve"}), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=11) as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "recipient_email": "jane@example.com"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ConnectionRequestStatus
        assert ins.call_args.kwargs["status"] == ConnectionRequestStatus.PENDING
        assert ins.call_args.kwargs["recipient_email"] == "jane@example.com"

    def test_an_agent_scoped_session_still_cannot_approve_even_with_an_email(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main._agent_scoped", return_value=True), \
             patch("cqc_lem.api.main.insert_connection_request") as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "recipient_email": "jane@example.com", "status": "approved"})
        assert resp.status_code == 403
        ins.assert_not_called()

    def test_update_can_supply_an_email_ahead_of_a_retry(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request", return_value=True) as upd:
            resp = api_client.put("/api/connection_request", json={
                "session_token": _S, "request_id": 3, "recipient_email": "jane@example.com"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["recipient_email"] == "jane@example.com"

    def test_an_empty_email_is_absent_not_malformed(self, api_client):
        # A caller with no address for this row says so with "" and must not be punished with a 422
        # — "" is normalised to None, exactly as if the key had been omitted.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=11) as ins:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "recipient_email": "   "})
        assert resp.status_code == 200
        assert ins.call_args.kwargs["recipient_email"] is None

    def test_an_empty_email_on_a_put_is_not_an_update_at_all(self, api_client):
        # Consequence of the same normalisation: "" reaches the handler as None, so it can never
        # blank an address a human already saved — and alone it is "nothing to update".
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request", return_value=True) as upd:
            resp = api_client.put("/api/connection_request", json={
                "session_token": _S, "request_id": 3, "recipient_email": ""})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_a_field_only_put_carrying_an_email_is_not_an_approval(self, api_client):
        # The owner's decision note on #1836, asserted rather than asserted-in-prose:
        # `_refuse_agent_approval` gates 'approve' and 'retry' only, so an agent-scoped field-only
        # PUT was already reachable before this field existed and stays reachable after.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main._agent_scoped", return_value=True), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request", return_value=True) as upd:
            resp = api_client.put("/api/connection_request", json={
                "session_token": _S, "request_id": 3, "recipient_email": "jane@example.com"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] is None

    def test_an_agent_scoped_email_put_still_cannot_retry(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main._agent_scoped", return_value=True), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request", return_value={"status": "failed"}), \
             patch("cqc_lem.api.main.update_connection_request") as upd:
            resp = api_client.put("/api/connection_request", json={
                "session_token": _S, "request_id": 3, "recipient_email": "jane@example.com",
                "action": "retry"})
        assert resp.status_code == 403
        upd.assert_not_called()


class TestListConnectionRequests:
    def test_lists(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_requests",
                   return_value={"requests": [], "total": 0, "page": 1, "page_size": 25}) as lst:
            resp = api_client.get(f"/api/connection_requests?session_token={_S}&status_filter=pending")
        assert resp.status_code == 200
        assert lst.call_args.kwargs["status_filter"] == "pending"

    def test_recipient_email_is_never_echoed(self, api_client):
        # Issue #1836 — a `has_recipient_email` boolean stands in for the address itself.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_requests",
                   return_value={"requests": [
                       {"id": 1, "has_recipient_email": True},
                       {"id": 2, "has_recipient_email": False}],
                                "total": 2, "page": 1, "page_size": 25}):
            resp = api_client.get(f"/api/connection_requests?session_token={_S}")
        assert resp.status_code == 200
        rows = resp.json()["detail"]["requests"]
        assert all("recipient_email" not in row for row in rows)
        assert [row["has_recipient_email"] for row in rows] == [True, False]


class TestUpdateAndCancel:
    def test_approve(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request", return_value=True) as upd:
            resp = api_client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "action": "approve"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ConnectionRequestStatus
        assert upd.call_args.kwargs["status"] == ConnectionRequestStatus.APPROVED

    def test_update_404_when_not_owner(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=999):
            resp = api_client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "message": "x"})
        assert resp.status_code == 404

    def test_unknown_action_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request") as upd:
            resp = api_client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "action": "sned"})
        assert resp.status_code == 422
        assert "Unknown action" in resp.json()["detail"]
        upd.assert_not_called()

    def test_no_fields_and_no_action_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request") as upd:
            resp = api_client.put("/api/connection_request", json={"session_token": _S, "request_id": 3})
        assert resp.status_code == 422
        assert "Nothing to update" in resp.json()["detail"]
        upd.assert_not_called()

    def test_retry_sets_approved(self, api_client):
        # Issue #1735 — a 'failed' request is only ever re-sent by an explicit user-directed retry.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request",
                   return_value={"id": 3, "user_id": _U, "status": "failed"}), \
             patch("cqc_lem.api.main.update_connection_request", return_value=True) as upd:
            resp = api_client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "action": "retry"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ConnectionRequestStatus
        assert upd.call_args.kwargs["status"] == ConnectionRequestStatus.APPROVED

    def test_retry_rejected_when_not_failed(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request",
                   return_value={"id": 3, "user_id": _U, "status": "pending"}), \
             patch("cqc_lem.api.main.update_connection_request") as upd:
            resp = api_client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "action": "retry"})
        assert resp.status_code == 422
        assert "Only a 'failed'" in resp.json()["detail"]
        upd.assert_not_called()

    def test_retry_unreadable_row_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request", return_value=None), \
             patch("cqc_lem.api.main.update_connection_request") as upd:
            resp = api_client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "action": "retry"})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_retry_refused_for_agent_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request",
                   return_value={"id": 3, "user_id": _U, "status": "failed"}), \
             patch("cqc_lem.api.main._agent_scoped", return_value=True), \
             patch("cqc_lem.api.main.update_connection_request") as upd:
            resp = api_client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "action": "retry"})
        assert resp.status_code == 403
        upd.assert_not_called()

    def test_cancel_sets_canceled(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request_status", return_value=True) as upd:
            resp = api_client.request("DELETE", "/api/connection_request",
                                  json={"session_token": _S, "request_id": 3})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.CANCELED)
