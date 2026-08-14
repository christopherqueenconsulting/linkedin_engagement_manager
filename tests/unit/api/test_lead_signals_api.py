"""Unit tests for the leads-inbox API endpoints (issue #483) — listing, editing a draft, dismissing,
and the approval gate that is the ONLY thing that dispatches a response.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"


_S = "tok"
_U = 5
_SIGNAL = {"id": 3, "user_id": _U, "status": "new", "channel": "reply",
           "draft_response": "Happy to help — what's your timeline?"}


class TestListLeadSignals:
    def test_lists_with_the_unactioned_count(self, api_client):
        listing = {"signals": [_SIGNAL], "total": 1, "page": 1, "page_size": 25}
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signals", return_value=listing) as get, \
             patch(f"{_M}.count_new_lead_signals", return_value=4):
            resp = api_client.get(f"/api/lead_signals?session_token={_S}&status_filter=new")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["total"] == 1 and detail["new_count"] == 4
        assert get.call_args.kwargs["status_filter"] == "new"

    def test_requires_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.get("/api/lead_signals?session_token=bad")
        assert resp.status_code == 401


class TestUpdateLeadSignal:
    def test_approve_dispatches_the_response(self, api_client):
        from cqc_lem.utilities.db import LeadSignalStatus
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=dict(_SIGNAL)), \
             patch(f"{_M}.update_lead_signal", return_value=True) as upd, \
             patch(f"{_M}.send_lead_response.apply_async") as task:
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "action": "approve"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] == LeadSignalStatus.APPROVED
        task.assert_called_once_with(kwargs={"signal_id": 3})

    def test_approve_sends_the_edited_text(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=dict(_SIGNAL)), \
             patch(f"{_M}.update_lead_signal", return_value=True) as upd, \
             patch(f"{_M}.send_lead_response.apply_async"):
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "action": "approve",
                                                        "draft_response": "My own words."})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["draft_response"] == "My own words."

    def test_saving_a_draft_does_not_send_anything(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=dict(_SIGNAL)), \
             patch(f"{_M}.update_lead_signal", return_value=True) as upd, \
             patch(f"{_M}.send_lead_response.apply_async") as task:
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "draft_response": "later"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] is None
        task.assert_not_called()

    def test_dismiss(self, api_client):
        from cqc_lem.utilities.db import LeadSignalStatus
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=dict(_SIGNAL)), \
             patch(f"{_M}.update_lead_signal", return_value=True) as upd, \
             patch(f"{_M}.send_lead_response.apply_async") as task:
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "action": "dismiss"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] == LeadSignalStatus.DISMISSED
        task.assert_not_called()

    def test_cannot_approve_an_empty_response(self, api_client):
        signal = dict(_SIGNAL, draft_response=None)
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=signal), \
             patch(f"{_M}.update_lead_signal") as upd, \
             patch(f"{_M}.send_lead_response.apply_async") as task:
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "action": "approve"})
        assert resp.status_code == 422
        upd.assert_not_called()
        task.assert_not_called()

    def test_unknown_action_is_rejected(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=dict(_SIGNAL)), \
             patch(f"{_M}.update_lead_signal") as upd:
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "action": "send-now"})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_nothing_to_update_is_rejected(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=dict(_SIGNAL)), \
             patch(f"{_M}.update_lead_signal") as upd:
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_another_users_signal_is_not_found(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=dict(_SIGNAL, user_id=99)), \
             patch(f"{_M}.update_lead_signal") as upd:
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "action": "approve"})
        assert resp.status_code == 404
        upd.assert_not_called()

    def test_missing_signal_is_404(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=None):
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "action": "approve"})
        assert resp.status_code == 404

    def test_requires_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.put("/api/lead_signal", json={"session_token": "bad", "signal_id": 3,
                                                        "action": "approve"})
        assert resp.status_code == 401

    def test_db_failure_is_a_500(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead_signal", return_value=dict(_SIGNAL)), \
             patch(f"{_M}.update_lead_signal", return_value=False), \
             patch(f"{_M}.send_lead_response.apply_async") as task:
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "action": "approve"})
        assert resp.status_code == 500
        task.assert_not_called()

    def test_oversized_draft_is_rejected(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U):
            resp = api_client.put("/api/lead_signal", json={"session_token": _S, "signal_id": 3,
                                                        "draft_response": "x" * 3001})
        assert resp.status_code == 422
