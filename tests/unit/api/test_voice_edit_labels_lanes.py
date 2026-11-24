"""Edited-draft voice labels for connection requests, lead signals, funnel targets (#2765).

Each lane calls `note_draft_edit` before an edit lands and `record_approved_edit` on approval, in
the single-item PUT and (where the lane has one) the bulk review path.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_R = "cqc_lem.api.routers.outreach"
_VE = "cqc_lem.utilities.ai.voice_exemplars"
_RA = "cqc_lem.utilities.review_actions."
_U, _S = 7, "tok"


def test_connection_request_edit_then_approve_notes_and_records(api_client):
    row = {"id": 3, "user_id": _U, "status": "pending", "message": "drafted"}
    with patch(f"{_M}.get_session_user_id", return_value=_U), \
         patch(f"{_M}.get_connection_request_user_id", return_value=_U), \
         patch(f"{_M}.get_connection_request", return_value=row), \
         patch(f"{_M}.update_connection_request", return_value=True), \
         patch(f"{_VE}.note_draft_edit") as note, \
         patch(f"{_VE}.record_approved_edit") as rec:
        resp = api_client.put("/api/connection_request", json={
            "session_token": _S, "request_id": 3, "message": "my words", "action": "approve"})
    assert resp.status_code == 200
    note.assert_called_once_with(_U, "connection_request", 3, "drafted", "my words")
    rec.assert_called_once_with(_U, "connection_request", 3, "my words")


def test_connection_request_plain_save_records_nothing(api_client):
    row = {"id": 3, "user_id": _U, "status": "pending", "message": "drafted"}
    with patch(f"{_M}.get_session_user_id", return_value=_U), \
         patch(f"{_M}.get_connection_request_user_id", return_value=_U), \
         patch(f"{_M}.get_connection_request", return_value=row), \
         patch(f"{_M}.update_connection_request", return_value=True), \
         patch(f"{_VE}.note_draft_edit") as note, \
         patch(f"{_VE}.record_approved_edit") as rec:
        api_client.put("/api/connection_request", json={
            "session_token": _S, "request_id": 3, "message": "my words"})
    note.assert_called_once()
    rec.assert_not_called()


def test_connection_request_untouched_approve_passes_stored_text(api_client):
    row = {"id": 3, "user_id": _U, "status": "pending", "message": "drafted"}
    with patch(f"{_M}.get_session_user_id", return_value=_U), \
         patch(f"{_M}.get_connection_request_user_id", return_value=_U), \
         patch(f"{_M}.get_connection_request", return_value=row), \
         patch(f"{_M}.update_connection_request", return_value=True), \
         patch(f"{_VE}.note_draft_edit") as note, \
         patch(f"{_VE}.record_approved_edit") as rec:
        api_client.put("/api/connection_request", json={
            "session_token": _S, "request_id": 3, "action": "approve"})
    note.assert_called_once_with(_U, "connection_request", 3, "drafted", None)
    rec.assert_called_once_with(_U, "connection_request", 3, "drafted")


def test_lead_signal_edit_then_approve(api_client):
    sig = {"id": 3, "user_id": _U, "draft_response": "drafted"}
    with patch(f"{_M}.get_session_user_id", return_value=_U), \
         patch(f"{_M}.get_lead_signal", return_value=sig), \
         patch(f"{_M}.update_lead_signal", return_value=True), \
         patch(f"{_M}.send_lead_response.apply_async"), \
         patch(f"{_VE}.note_draft_edit") as note, \
         patch(f"{_VE}.record_approved_edit") as rec:
        resp = api_client.put("/api/lead_signal", json={
            "session_token": _S, "signal_id": 3, "draft_response": "mine", "action": "approve"})
    assert resp.status_code == 200
    note.assert_called_once_with(_U, "lead_signal", 3, "drafted", "mine")
    rec.assert_called_once_with(_U, "lead_signal", 3, "mine")


def test_lead_signal_dismiss_records_nothing(api_client):
    sig = {"id": 3, "user_id": _U, "draft_response": "drafted"}
    with patch(f"{_M}.get_session_user_id", return_value=_U), \
         patch(f"{_M}.get_lead_signal", return_value=sig), \
         patch(f"{_M}.update_lead_signal", return_value=True), \
         patch(f"{_VE}.note_draft_edit"), \
         patch(f"{_VE}.record_approved_edit") as rec:
        api_client.put("/api/lead_signal", json={
            "session_token": _S, "signal_id": 3, "action": "dismiss"})
    rec.assert_not_called()


def test_outreach_target_edit_then_approve(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=_U), \
         patch(f"{_R}.get_outreach_target_user_id", return_value=_U), \
         patch(f"{_R}.get_outreach_target", return_value={"draft_text": "drafted"}), \
         patch(f"{_R}.update_outreach_target", return_value=True), \
         patch(f"{_VE}.note_draft_edit") as note, \
         patch(f"{_VE}.record_approved_edit") as rec:
        resp = api_client.put("/api/outreach/target", json={
            "session_token": _S, "target_id": 3, "draft_text": "mine", "action": "approve"})
    assert resp.status_code == 200
    note.assert_called_once_with(_U, "outreach_target", 3, "drafted", "mine")
    rec.assert_called_once_with(_U, "outreach_target", 3, "mine")


def test_outreach_target_cancel_does_not_read_the_draft(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=_U), \
         patch(f"{_R}.get_outreach_target_user_id", return_value=_U), \
         patch(f"{_R}.get_outreach_target") as get, \
         patch(f"{_R}.update_outreach_target", return_value=True), \
         patch(f"{_VE}.record_approved_edit") as rec:
        api_client.put("/api/outreach/target", json={
            "session_token": _S, "target_id": 3, "action": "cancel"})
    get.assert_not_called()
    rec.assert_not_called()


def test_bulk_approve_labels_connection_request_and_funnel_target():
    from cqc_lem.utilities import review_actions as ra
    with patch(_RA + "get_connection_request_user_id", return_value=_U), \
         patch(_RA + "get_connection_request", return_value={"message": "final cr"}), \
         patch(_RA + "get_outreach_target_user_id", return_value=_U), \
         patch(_RA + "get_outreach_target", return_value={"draft_text": "final ot"}), \
         patch(_RA + "update_connection_request", return_value=True), \
         patch(_RA + "update_outreach_target", return_value=True), \
         patch(f"{_VE}.record_approved_edit") as rec:
        assert ra.approve_connection_request(_U, 2) is None
        assert ra.approve_funnel_target(_U, 3) is None
    assert [c.args for c in rec.call_args_list] == [
        (_U, "connection_request", 2, "final cr"), (_U, "outreach_target", 3, "final ot")]


def test_bulk_failed_write_records_no_label():
    from cqc_lem.utilities import review_actions as ra
    with patch(_RA + "get_connection_request_user_id", return_value=_U), \
         patch(_RA + "get_connection_request", return_value={"message": "x"}), \
         patch(_RA + "update_connection_request", return_value=False), \
         patch(f"{_VE}.record_approved_edit") as rec:
        assert ra.approve_connection_request(_U, 2) == ra.WRITE_FAILED
    rec.assert_not_called()
