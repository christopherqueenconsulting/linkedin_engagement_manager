"""Unit tests for the connection-request API endpoints (issue #398)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
    ]
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient
        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


_S = "tok"
_U = 5


class TestCreateConnectionRequest:
    def test_creates_pending(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=11) as ins:
            resp = client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "recipient_name": "Jane", "message": "Hi Jane"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["request_id"] == 11
        from cqc_lem.utilities.db import ConnectionRequestStatus
        assert ins.call_args.kwargs["status"] == ConnectionRequestStatus.PENDING

    def test_approved_status_maps(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=12) as ins:
            resp = client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "status": "approved"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ConnectionRequestStatus
        assert ins.call_args.kwargs["status"] == ConnectionRequestStatus.APPROVED

    def test_over_limit_note_rejected_422(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_connection_request", return_value=1) as ins:
            resp = client.post("/api/connection_request", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "message": "x" * 301})
        assert resp.status_code == 422
        ins.assert_not_called()

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post("/api/connection_request", json={
                "session_token": "bad", "recipient_profile_url": "u"})
        assert resp.status_code == 401


class TestListConnectionRequests:
    def test_lists(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_requests",
                   return_value={"requests": [], "total": 0, "page": 1, "page_size": 25}) as lst:
            resp = client.get(f"/api/connection_requests?session_token={_S}&status_filter=pending")
        assert resp.status_code == 200
        assert lst.call_args.kwargs["status_filter"] == "pending"


class TestUpdateAndCancel:
    def test_approve(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request", return_value=True) as upd:
            resp = client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "action": "approve"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ConnectionRequestStatus
        assert upd.call_args.kwargs["status"] == ConnectionRequestStatus.APPROVED

    def test_update_404_when_not_owner(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=999):
            resp = client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "message": "x"})
        assert resp.status_code == 404

    def test_unknown_action_422(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request") as upd:
            resp = client.put("/api/connection_request",
                              json={"session_token": _S, "request_id": 3, "action": "sned"})
        assert resp.status_code == 422
        assert "Unknown action" in resp.json()["detail"]
        upd.assert_not_called()

    def test_no_fields_and_no_action_422(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request") as upd:
            resp = client.put("/api/connection_request", json={"session_token": _S, "request_id": 3})
        assert resp.status_code == 422
        assert "Nothing to update" in resp.json()["detail"]
        upd.assert_not_called()

    def test_cancel_sets_canceled(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_connection_request_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_connection_request_status", return_value=True) as upd:
            resp = client.request("DELETE", "/api/connection_request",
                                  json={"session_token": _S, "request_id": 3})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ConnectionRequestStatus
        upd.assert_called_once_with(3, ConnectionRequestStatus.CANCELED)
