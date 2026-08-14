"""Unit tests for the scheduled-DM API endpoints (issue #306)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


_S = "tok"
_U = 5


class TestScheduleDm:
    def test_creates_pending(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_scheduled_dm", return_value=11) as ins:
            resp = api_client.post("/api/schedule_dm", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "recipient_name": "Jane", "message": "Hi Jane",
                "scheduled_datetime": "2026-08-01T09:00:00"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["dm_id"] == 11
        from cqc_lem.utilities.db import ScheduledDmStatus
        assert ins.call_args.kwargs["status"] == ScheduledDmStatus.PENDING

    def test_approved_status_maps(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_scheduled_dm", return_value=12) as ins:
            resp = api_client.post("/api/schedule_dm", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "message": "Hi", "scheduled_datetime": "2026-08-01T09:00:00", "status": "approved"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ScheduledDmStatus
        assert ins.call_args.kwargs["status"] == ScheduledDmStatus.APPROVED

    def test_over_limit_message_rejected_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.insert_scheduled_dm", return_value=1) as ins:
            resp = api_client.post("/api/schedule_dm", json={
                "session_token": _S, "recipient_profile_url": "https://x/in/jane",
                "message": "x" * 2001, "scheduled_datetime": "2026-08-01T09:00:00"})
        assert resp.status_code == 422
        ins.assert_not_called()

    def test_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.post("/api/schedule_dm", json={
                "session_token": "bad", "recipient_profile_url": "u", "message": "m",
                "scheduled_datetime": "2026-08-01T09:00:00"})
        assert resp.status_code == 401


class TestListDms:
    def test_lists(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_scheduled_dms",
                   return_value={"dms": [], "total": 0, "page": 1, "page_size": 25}) as lst:
            resp = api_client.get(f"/api/dms?session_token={_S}&status_filter=pending")
        assert resp.status_code == 200
        assert lst.call_args.kwargs["status_filter"] == "pending"

    def test_datetimes_serialized_as_explicit_utc(self, api_client):
        """Issue #546 — db.get_scheduled_dms isoformats naive UTC with no offset; the endpoint must
        stamp the 'Z' or the SPA parses it as local time and shows the wrong send time.
        """
        from datetime import datetime
        dms = [{"id": 3, "scheduled_time": datetime(2026, 8, 1, 13, 0),
                "created_at": datetime(2026, 7, 30, 8, 0), "updated_at": None}]
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_scheduled_dms",
                   return_value={"dms": dms, "total": 1, "page": 1, "page_size": 25}):
            resp = api_client.get(f"/api/dms?session_token={_S}")
        row = resp.json()["detail"]["dms"][0]
        assert row["scheduled_time"] == "2026-08-01T13:00:00Z"
        assert row["created_at"] == "2026-07-30T08:00:00Z"
        assert row["updated_at"] is None


class TestUpdateAndCancel:
    def test_approve(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_scheduled_dm_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_scheduled_dm", return_value=True) as upd:
            resp = api_client.put("/api/dm", json={"session_token": _S, "dm_id": 3, "action": "approve"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ScheduledDmStatus
        assert upd.call_args.kwargs["status"] == ScheduledDmStatus.APPROVED

    def test_update_404_when_not_owner(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_scheduled_dm_user_id", return_value=999):
            resp = api_client.put("/api/dm", json={"session_token": _S, "dm_id": 3, "message": "x"})
        assert resp.status_code == 404

    def test_unknown_action_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_scheduled_dm_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_scheduled_dm") as upd:
            resp = api_client.put("/api/dm", json={"session_token": _S, "dm_id": 3, "action": "sned"})
        assert resp.status_code == 422
        assert "Unknown action" in resp.json()["detail"]
        upd.assert_not_called()

    def test_no_fields_and_no_action_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_scheduled_dm_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_scheduled_dm") as upd:
            resp = api_client.put("/api/dm", json={"session_token": _S, "dm_id": 3})
        assert resp.status_code == 422
        assert "Nothing to update" in resp.json()["detail"]
        upd.assert_not_called()

    def test_cancel_sets_canceled(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
             patch("cqc_lem.api.main.get_scheduled_dm_user_id", return_value=_U), \
             patch("cqc_lem.api.main.update_scheduled_dm_status", return_value=True) as upd:
            resp = api_client.request("DELETE", "/api/dm", json={"session_token": _S, "dm_id": 3})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import ScheduledDmStatus
        upd.assert_called_once_with(3, ScheduledDmStatus.CANCELED)
