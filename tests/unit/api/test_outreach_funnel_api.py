"""Unit tests for the outreach-funnel API endpoints (issue #399)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"


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


class TestCreateTarget:
    def test_creates_pending(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_by_url", return_value=None), \
             patch(f"{_M}.insert_outreach_target", return_value=11) as ins:
            resp = client.post("/api/outreach/target", json={
                "session_token": _S, "target_profile_url": "https://x/in/jane",
                "target_name": "Jane", "context_url": "https://x/post/1", "draft_text": "nice"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["target_id"] == 11
        from cqc_lem.utilities.db import OutreachStatus
        assert ins.call_args.kwargs["status"] == OutreachStatus.PENDING

    def test_approved_status_maps(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_by_url", return_value=None), \
             patch(f"{_M}.insert_outreach_target", return_value=12) as ins:
            resp = client.post("/api/outreach/target", json={
                "session_token": _S, "target_profile_url": "https://x/in/jane", "status": "approved"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import OutreachStatus
        assert ins.call_args.kwargs["status"] == OutreachStatus.APPROVED

    def test_strips_whitespace_before_dedup_and_insert(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_by_url", return_value=None) as dup, \
             patch(f"{_M}.insert_outreach_target", return_value=13) as ins:
            resp = client.post("/api/outreach/target", json={
                "session_token": _S, "target_profile_url": "  https://x/in/jane  ",
                "target_name": "  Jane  ", "context_url": "  https://x/post/1  "})
        assert resp.status_code == 200
        assert dup.call_args.args[1] == "https://x/in/jane"
        assert ins.call_args.args[1] == "https://x/in/jane"
        assert ins.call_args.kwargs["target_name"] == "Jane"
        assert ins.call_args.kwargs["context_url"] == "https://x/post/1"

    def test_blank_url_422(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.insert_outreach_target") as ins:
            resp = client.post("/api/outreach/target", json={
                "session_token": _S, "target_profile_url": "   "})
        assert resp.status_code == 422
        ins.assert_not_called()

    def test_duplicate_409(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_by_url", return_value={"id": 1}), \
             patch(f"{_M}.insert_outreach_target") as ins:
            resp = client.post("/api/outreach/target", json={
                "session_token": _S, "target_profile_url": "https://x/in/jane"})
        assert resp.status_code == 409
        ins.assert_not_called()

    def test_401(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.post("/api/outreach/target", json={
                "session_token": "bad", "target_profile_url": "u"})
        assert resp.status_code == 401

    def test_insert_failure_500(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_by_url", return_value=None), \
             patch(f"{_M}.insert_outreach_target", return_value=None):
            resp = client.post("/api/outreach/target", json={
                "session_token": _S, "target_profile_url": "https://x/in/jane"})
        assert resp.status_code == 500


class TestListTargets:
    def test_lists(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_targets",
                   return_value={"targets": [], "total": 0, "page": 1, "page_size": 25}) as lst:
            resp = client.get(f"/api/outreach/targets?session_token={_S}&status_filter=pending&stage_filter=comment")
        assert resp.status_code == 200
        assert lst.call_args.kwargs["status_filter"] == "pending"
        assert lst.call_args.kwargs["stage_filter"] == "comment"

    def test_401(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.get("/api/outreach/targets?session_token=bad")
        assert resp.status_code == 401


class TestUpdateAndCancel:
    def test_approve(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_user_id", return_value=_U), \
             patch(f"{_M}.update_outreach_target", return_value=True) as upd:
            resp = client.put("/api/outreach/target", json={"session_token": _S, "target_id": 3, "action": "approve"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import OutreachStatus
        assert upd.call_args.kwargs["status"] == OutreachStatus.APPROVED

    def test_save_draft_only(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_user_id", return_value=_U), \
             patch(f"{_M}.update_outreach_target", return_value=True) as upd:
            resp = client.put("/api/outreach/target", json={"session_token": _S, "target_id": 3, "draft_text": "hi"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["draft_text"] == "hi"
        assert upd.call_args.kwargs["status"] is None

    def test_update_404_when_not_owner(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_user_id", return_value=999):
            resp = client.put("/api/outreach/target", json={"session_token": _S, "target_id": 3, "draft_text": "x"})
        assert resp.status_code == 404

    def test_unknown_action_422(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_user_id", return_value=_U), \
             patch(f"{_M}.update_outreach_target") as upd:
            resp = client.put("/api/outreach/target", json={"session_token": _S, "target_id": 3, "action": "nope"})
        assert resp.status_code == 422
        assert "Unknown action" in resp.json()["detail"]
        upd.assert_not_called()

    def test_no_fields_and_no_action_422(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_user_id", return_value=_U), \
             patch(f"{_M}.update_outreach_target") as upd:
            resp = client.put("/api/outreach/target", json={"session_token": _S, "target_id": 3})
        assert resp.status_code == 422
        assert "Nothing to update" in resp.json()["detail"]
        upd.assert_not_called()

    def test_cancel_sets_canceled(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_user_id", return_value=_U), \
             patch(f"{_M}.update_outreach_target_status", return_value=True) as upd:
            resp = client.request("DELETE", "/api/outreach/target", json={"session_token": _S, "target_id": 3})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import OutreachStatus
        upd.assert_called_once_with(3, OutreachStatus.CANCELED)

    def test_delete_404_when_not_owner(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_outreach_target_user_id", return_value=999):
            resp = client.request("DELETE", "/api/outreach/target", json={"session_token": _S, "target_id": 3})
        assert resp.status_code == 404
