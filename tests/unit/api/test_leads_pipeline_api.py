"""Unit tests for the lead-pipeline API (issue #484) — the scored board, operator edits (manual
stage, notes, dismiss/restore), ownership checks, and the on-demand re-score.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"


_S = "tok"
_U = 5
_LEAD = {"id": 11, "user_id": _U, "person_key": "in:jane", "score": 82, "stage": "hot",
         "manual_stage": None, "notes": None, "dismissed": False}


class TestListLeads:
    def test_lists_the_board_with_the_hot_count(self, api_client):
        board = {"leads": [_LEAD], "total": 1, "page": 1, "page_size": 100}
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_leads", return_value=dict(board)) as get, \
             patch(f"{_M}.count_hot_leads", return_value=3):
            resp = api_client.get(f"/api/leads?session_token={_S}&stage_filter=hot")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["total"] == 1 and detail["hot_count"] == 3
        assert get.call_args.kwargs["stage_filter"] == "hot"
        assert get.call_args.kwargs["include_dismissed"] is False

    def test_can_include_dismissed_leads(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_leads", return_value={"leads": [], "total": 0}) as get, \
             patch(f"{_M}.count_hot_leads", return_value=0):
            resp = api_client.get(f"/api/leads?session_token={_S}&include_dismissed=true")
        assert resp.status_code == 200
        assert get.call_args.kwargs["include_dismissed"] is True

    def test_rejects_an_unknown_stage(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_leads") as get:
            resp = api_client.get(f"/api/leads?session_token={_S}&stage_filter=molten")
        assert resp.status_code == 422
        get.assert_not_called()

    def test_requires_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.get("/api/leads?session_token=bad")
        assert resp.status_code == 401


class TestUpdateLead:
    def test_saves_an_operator_note(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead", return_value=dict(_LEAD)), \
             patch(f"{_M}.update_lead", return_value=True) as upd:
            resp = api_client.put("/api/lead", json={"session_token": _S, "lead_id": 11,
                                                 "notes": "met at a conference"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["notes"] == "met at a conference"
        assert upd.call_args.kwargs["dismissed"] is None

    def test_moves_a_lead_by_hand(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead", return_value=dict(_LEAD)), \
             patch(f"{_M}.update_lead", return_value=True) as upd:
            resp = api_client.put("/api/lead", json={"session_token": _S, "lead_id": 11,
                                                 "stage": "opportunity"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["manual_stage"] == "opportunity"

    def test_empty_stage_clears_the_override(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead", return_value=dict(_LEAD)), \
             patch(f"{_M}.update_lead", return_value=True) as upd:
            resp = api_client.put("/api/lead", json={"session_token": _S, "lead_id": 11, "stage": ""})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["manual_stage"] == ""

    def test_dismiss_and_restore(self, api_client):
        for action, expected in (("dismiss", True), ("restore", False)):
            with patch(f"{_M}.get_session_user_id", return_value=_U), \
                 patch(f"{_M}.get_lead", return_value=dict(_LEAD)), \
                 patch(f"{_M}.update_lead", return_value=True) as upd:
                resp = api_client.put("/api/lead", json={"session_token": _S, "lead_id": 11,
                                                     "action": action})
            assert resp.status_code == 200
            assert upd.call_args.kwargs["dismissed"] is expected

    def test_rejects_an_unknown_action_or_stage(self, api_client):
        for payload in ({"action": "delete"}, {"stage": "molten"}):
            with patch(f"{_M}.get_session_user_id", return_value=_U), \
                 patch(f"{_M}.get_lead", return_value=dict(_LEAD)), \
                 patch(f"{_M}.update_lead") as upd:
                resp = api_client.put("/api/lead", json={"session_token": _S, "lead_id": 11, **payload})
            assert resp.status_code == 422
            upd.assert_not_called()

    def test_an_empty_request_changes_nothing(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead", return_value=dict(_LEAD)), \
             patch(f"{_M}.update_lead") as upd:
            resp = api_client.put("/api/lead", json={"session_token": _S, "lead_id": 11})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_cannot_touch_someone_elses_lead(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead", return_value={"id": 11, "user_id": 99}), \
             patch(f"{_M}.update_lead") as upd:
            resp = api_client.put("/api/lead", json={"session_token": _S, "lead_id": 11,
                                                 "action": "dismiss"})
        assert resp.status_code == 404
        upd.assert_not_called()

    def test_missing_lead_is_a_404(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead", return_value=None):
            resp = api_client.put("/api/lead", json={"session_token": _S, "lead_id": 11,
                                                 "action": "dismiss"})
        assert resp.status_code == 404

    def test_a_failed_write_is_a_500(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch(f"{_M}.get_lead", return_value=dict(_LEAD)), \
             patch(f"{_M}.update_lead", return_value=False):
            resp = api_client.put("/api/lead", json={"session_token": _S, "lead_id": 11,
                                                 "notes": "x"})
        assert resp.status_code == 500

    def test_requires_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.put("/api/lead", json={"session_token": "bad", "lead_id": 11,
                                                 "action": "dismiss"})
        assert resp.status_code == 401


class TestRefreshLeads:
    def test_dispatches_a_rescore_for_the_caller_only(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_U), \
             patch("cqc_lem.app.run_scheduler.rebuild_leads_for_user.apply_async") as task:
            resp = api_client.post("/api/leads/refresh", json={"session_token": _S})
        assert resp.status_code == 200
        task.assert_called_once_with(kwargs={"user_id": _U})

    def test_requires_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None), \
             patch("cqc_lem.app.run_scheduler.rebuild_leads_for_user.apply_async") as task:
            resp = api_client.post("/api/leads/refresh", json={"session_token": "bad"})
        assert resp.status_code == 401
        task.assert_not_called()
