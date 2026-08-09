"""Unit tests for the catch-up touch API endpoints and preferences (issue #482)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"


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


class TestListTouches:
    def test_lists_with_filters(self, client):
        payload = {"touches": [], "total": 0, "page": 1, "page_size": 25}
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touches", return_value=payload) as lister:
            resp = client.get("/api/catchup/touches",
                              params={"session_token": _S, "status_filter": "pending",
                                      "event_type_filter": "job_change"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == payload
        assert lister.call_args.kwargs["status_filter"] == "pending"
        assert lister.call_args.kwargs["event_type_filter"] == "job_change"

    def test_requires_a_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.get("/api/catchup/touches", params={"session_token": _S})
        assert resp.status_code == 401


def _touch(**kw):
    base = {"id": 3, "user_id": _U, "message": "Congrats Jane!", "status": "pending"}
    base.update(kw)
    return base


class TestUpdateTouch:
    def test_approve_maps_to_approved_status(self, client):
        from cqc_lem.utilities.db import CatchupTouchStatus
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch()), \
             patch(f"{_M}.update_catchup_touch", return_value=True) as upd:
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "action": "approve"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] == CatchupTouchStatus.APPROVED

    def test_edit_message_without_action(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch()), \
             patch(f"{_M}.update_catchup_touch", return_value=True) as upd:
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "message": "Congrats!"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["message"] == "Congrats!"
        assert upd.call_args.kwargs["status"] is None

    def test_approve_saves_the_edited_message_in_the_same_call(self, client):
        from cqc_lem.utilities.db import CatchupTouchStatus
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch()), \
             patch(f"{_M}.update_catchup_touch", return_value=True) as upd:
            resp = client.put("/api/catchup/touch", json={
                "session_token": _S, "touch_id": 3, "action": "approve", "message": "Edited congrats!"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["message"] == "Edited congrats!"
        assert upd.call_args.kwargs["status"] == CatchupTouchStatus.APPROVED

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_blank_message_is_rejected(self, client, blank):
        """A blank message would become a permanent SKIPPED at send time — reject it at the boundary."""
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch()), \
             patch(f"{_M}.update_catchup_touch", return_value=True) as upd:
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "message": blank})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_approving_a_touch_with_no_stored_message_is_rejected(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch(message=None)), \
             patch(f"{_M}.update_catchup_touch", return_value=True) as upd:
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "action": "approve"})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_canceling_a_touch_with_no_message_is_still_allowed(self, client):
        from cqc_lem.utilities.db import CatchupTouchStatus
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch(message=None)), \
             patch(f"{_M}.update_catchup_touch", return_value=True) as upd:
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "action": "cancel"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] == CatchupTouchStatus.CANCELED

    def test_unknown_action_is_rejected(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch()):
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "action": "send_now"})
        assert resp.status_code == 422

    def test_empty_update_is_rejected(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch()):
            resp = client.put("/api/catchup/touch", json={"session_token": _S, "touch_id": 3})
        assert resp.status_code == 422

    def test_another_users_touch_is_not_found(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch(user_id=99)):
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "action": "approve"})
        assert resp.status_code == 404

    def test_missing_touch_is_not_found(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=None):
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "action": "approve"})
        assert resp.status_code == 404

    def test_requires_a_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "action": "approve"})
        assert resp.status_code == 401

    def test_db_failure_is_a_500(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch", return_value=_touch()), \
             patch(f"{_M}.update_catchup_touch", return_value=False):
            resp = client.put("/api/catchup/touch",
                              json={"session_token": _S, "touch_id": 3, "action": "approve"})
        assert resp.status_code == 500


class TestDeleteTouch:
    def test_cancels(self, client):
        from cqc_lem.utilities.db import CatchupTouchStatus
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch_user_id", return_value=_U), \
             patch(f"{_M}.update_catchup_touch_status", return_value=True) as upd:
            resp = client.request("DELETE", "/api/catchup/touch",
                                  json={"session_token": _S, "touch_id": 3})
        assert resp.status_code == 200
        upd.assert_called_once_with(3, CatchupTouchStatus.CANCELED)

    def test_another_users_touch_is_not_found(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch_user_id", return_value=99):
            resp = client.request("DELETE", "/api/catchup/touch",
                                  json={"session_token": _S, "touch_id": 3})
        assert resp.status_code == 404

    def test_requires_a_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.request("DELETE", "/api/catchup/touch",
                                  json={"session_token": _S, "touch_id": 3})
        assert resp.status_code == 401

    def test_db_failure_is_a_500(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_catchup_touch_user_id", return_value=_U), \
             patch(f"{_M}.update_catchup_touch_status", return_value=False):
            resp = client.request("DELETE", "/api/catchup/touch",
                                  json={"session_token": _S, "touch_id": 3})
        assert resp.status_code == 500


class TestCatchupPreferenceValidation:
    def test_defaults_are_the_bd_subset_and_pre_review(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_USER}.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences", json={"session_token": _S})
        assert resp.status_code == 200
        prefs = upd.call_args[0][1]
        assert prefs["catchup_event_types"] == ["job_change", "promotion"]
        assert prefs["catchup_touch_mode"] == "pre_review"
        assert prefs["max_catchup_touches_per_day"] == 5
        assert prefs["catchup_message_source"] == "linkedin"

    def test_unknown_event_types_are_dropped(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_USER}.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences", json={
                "session_token": _S, "catchup_event_types": ["promotion", "wedding"]})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["catchup_event_types"] == ["promotion"]

    @pytest.mark.parametrize("given,expected", [(999, 10), (-1, 0), (3, 3)])
    def test_cap_is_clamped_to_the_absolute_ceiling(self, client, given, expected):
        """The boundary caps at the premium ceiling; the per-plan allowance is applied in db.py."""
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_USER}.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences", json={
                "session_token": _S, "max_catchup_touches_per_day": given})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["max_catchup_touches_per_day"] == expected

    @pytest.mark.parametrize("given,expected", [("ai", "ai"), ("gpt", "linkedin")])
    def test_message_source_is_validated(self, client, given, expected):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_USER}.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences", json={
                "session_token": _S, "catchup_message_source": given})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["catchup_message_source"] == expected

    @pytest.mark.parametrize("allowance", [5, 10])
    def test_get_exposes_the_plan_allowance(self, client, allowance):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_USER}.get_engagement_preferences", return_value={"tone": None}), \
             patch(f"{_USER}.get_or_create_reply_inbound_token", return_value=None), \
             patch(f"{_M}.get_gmail_forward_confirmation", return_value=None), \
             patch(f"{_USER}.max_catchup_touches_allowed", return_value=allowance):
            resp = client.get("/api/user/engagement-preferences", params={"session_token": _S})
        assert resp.status_code == 200
        assert resp.json()["detail"]["max_catchup_touches_allowed"] == allowance

    def test_bad_mode_falls_back_to_pre_review(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_USER}.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences", json={
                "session_token": _S, "catchup_touch_mode": "yolo"})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["catchup_touch_mode"] == "pre_review"

    def test_auto_approve_is_accepted(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_USER}.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences", json={
                "session_token": _S, "catchup_touch_mode": "auto_approve"})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["catchup_touch_mode"] == "auto_approve"
