"""Text an MCP agent wrote is never stored as an example of the user's own writing (PR #2731).

The edited-draft capture (`_voice_note_edit` / `_voice_record_approval`) runs in every outreach PUT
handler. An MCP or agent seat's edit must note nothing, and an approval, by a human or by the MCP seat,
of text the agent last wrote must reach neither `record_approved_edit` nor `ingest_exemplars`. A plain
human edit followed by an approval still records.
"""

from contextlib import ExitStack
from typing import Any, Optional
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_VE = "cqc_lem.utilities.ai.voice_exemplars"
_U, _S = 7, "tok"

# lane -> (route, id key, text key, fixture row without provenance, patches for the lane's reads/writes)
_LANES: dict[str, dict[str, Any]] = {
    "scheduled_dm": {
        "path": "/api/dm", "id": "dm_id", "text": "message", "mark": "source",
        "row": {"id": 3, "user_id": _U, "status": "pending", "message": "drafted"},
        "patches": {"get_scheduled_dm_user_id": _U, "update_scheduled_dm": True}, "row_fn": "get_scheduled_dm"},
    "connection_request": {
        "path": "/api/connection_request", "id": "request_id", "text": "message", "mark": "source",
        "row": {"id": 3, "user_id": _U, "status": "pending", "message": "drafted"},
        "patches": {"get_connection_request_user_id": _U, "update_connection_request": True},
        "row_fn": "get_connection_request"},
    "catchup_touch": {
        "path": "/api/catchup/touch", "id": "touch_id", "text": "message", "mark": "edited_via",
        "row": {"id": 3, "user_id": _U, "status": "pending", "message": "drafted"},
        "patches": {"update_catchup_touch": True}, "row_fn": "get_catchup_touch"},
    "lead_signal": {
        "path": "/api/lead_signal", "id": "signal_id", "text": "draft_response", "mark": None,
        "row": {"id": 3, "user_id": _U, "draft_response": "drafted"},
        "patches": {"update_lead_signal": True}, "row_fn": "get_lead_signal"},
}
_LANE_IDS = list(_LANES)


def _put(api_client: Any, lane: str, *, row_extra: Optional[dict] = None, machine: bool = False,
         text: Optional[str] = None, action: Optional[str] = None, human_edit_noted: bool = False
         ) -> tuple[Any, dict[str, Any]]:
    """PUT one lane's handler with every I/O patched; return the response and the capture mocks."""
    spec = _LANES[lane]
    row = {**spec["row"], **(row_extra or {})}
    body: dict[str, Any] = {"session_token": _S, spec["id"]: 3}
    if text is not None:
        body[spec["text"]] = text
    if action is not None:
        body["action"] = action
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch(f"{_M}.get_session_user_id", return_value=_U))
        enter(patch(f"{_M}.{spec['row_fn']}", return_value=row))
        for name, value in spec["patches"].items():
            enter(patch(f"{_M}.{name}", return_value=value))
        enter(patch(f"{_M}.send_lead_response.apply_async"))
        note = enter(patch(f"{_VE}.note_draft_edit"))
        record = enter(patch(f"{_VE}.record_approved_edit"))
        ingest = enter(patch(f"{_VE}.ingest_exemplars"))
        discard = enter(patch(f"{_VE}.discard_draft_edit"))
        enter(patch(f"{_VE}.human_edit_noted", return_value=human_edit_noted))
        if machine:
            enter(patch(f"{_M}._mcp_source", return_value="mcp"))
            enter(patch(f"{_M}._agent_scoped", return_value=True))
            enter(patch(f"{_M}._edits_pending_only", return_value=True))
            enter(patch(f"{_M}._refuse_agent_approval"))
            enter(patch(f"{_M}._pending_edit_or_raise"))
            enter(patch(f"{_M}._mcp_approval_granted", return_value=True))
            enter(patch(f"{_M}._mcp_approval_fields", return_value={}))
        resp = api_client.put(spec["path"], json=body)
    return resp, {"note": note, "record": record, "ingest": ingest, "discard": discard}


@pytest.mark.parametrize("lane", _LANE_IDS)
def test_an_mcp_edit_notes_nothing_and_discards_a_noted_original(api_client, lane):
    resp, m = _put(api_client, lane, machine=True, text="agent words")
    assert resp.status_code == 200, resp.text
    m["note"].assert_not_called()
    m["discard"].assert_called_once_with(_U, lane, 3)
    m["record"].assert_not_called()
    m["ingest"].assert_not_called()


@pytest.mark.parametrize("lane", _LANE_IDS)
def test_an_mcp_edit_then_an_mcp_approval_never_records(api_client, lane):
    resp, m = _put(api_client, lane, machine=True, text="agent words", action="approve")
    assert resp.status_code == 200, resp.text
    m["note"].assert_not_called()
    m["record"].assert_not_called()
    m["ingest"].assert_not_called()


@pytest.mark.parametrize("lane", ["scheduled_dm", "connection_request", "catchup_touch"])
def test_an_mcp_approval_with_no_new_text_never_records(api_client, lane):
    resp, m = _put(api_client, lane, machine=True, action="approve")
    assert resp.status_code == 200, resp.text
    m["record"].assert_not_called()
    m["ingest"].assert_not_called()


@pytest.mark.parametrize("lane", ["scheduled_dm", "connection_request", "catchup_touch"])
def test_a_human_approval_of_text_the_agent_last_wrote_never_records(api_client, lane):
    mark = _LANES[lane]["mark"]
    resp, m = _put(api_client, lane, row_extra={mark: "mcp"}, action="approve")
    assert resp.status_code == 200, resp.text
    m["record"].assert_not_called()
    m["ingest"].assert_not_called()


def test_a_human_who_edited_an_agent_dm_after_it_was_written_still_records(api_client):
    """A noted human edit means the final text is the user's, so it is a label again."""
    for lane in ("scheduled_dm", "connection_request"):
        resp, m = _put(api_client, lane, row_extra={"source": "mcp"}, text="my words",
                       action="approve", human_edit_noted=True)
        assert resp.status_code == 200, resp.text
        m["note"].assert_called_once_with(_U, lane, 3, "drafted", "my words")
        m["record"].assert_called_once_with(_U, lane, 3, "my words")


@pytest.mark.parametrize("lane", _LANE_IDS)
def test_a_plain_human_edit_then_approval_still_records(api_client, lane):
    resp, m = _put(api_client, lane, text="my words", action="approve")
    assert resp.status_code == 200, resp.text
    m["note"].assert_called_once_with(_U, lane, 3, "drafted", "my words")
    m["record"].assert_called_once_with(_U, lane, 3, "my words")
    m["discard"].assert_not_called()


def test_the_approval_chain_never_reaches_ingest_for_agent_text():
    """Through the real `record_approved_edit`: no label, so no exemplar ingest."""
    from cqc_lem.api import main

    with patch(f"{_M}._mcp_source", return_value=None), patch(f"{_M}._agent_scoped", return_value=False), \
         patch(f"{_VE}.human_edit_noted", return_value=False), \
         patch(f"{_VE}.voice_exemplars_enabled", return_value=True), \
         patch("cqc_lem.utilities.db.get_draft_original", return_value="drafted"), \
         patch(f"{_VE}.ingest_exemplars") as ingest:
        main._voice_record_approval(_U, "catchup_touch", 3, "agent words", {"edited_via": "mcp"})
        main._voice_record_approval(_U, "scheduled_dm", 3, "agent words", {"source": "mcp"})
    ingest.assert_not_called()


@pytest.mark.parametrize("machine", [False, True], ids=["human", "mcp"])
def test_the_lead_approval_records_before_it_sends_on_both_paths(api_client, machine):
    from unittest.mock import MagicMock

    calls = MagicMock()
    row = {"id": 3, "user_id": _U, "draft_response": "drafted"}
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch(f"{_M}.get_session_user_id", return_value=_U))
        enter(patch(f"{_M}.get_lead_signal", return_value=row))
        enter(patch(f"{_M}.update_lead_signal", return_value=True))
        enter(patch(f"{_M}._voice_record_approval", calls.record))
        enter(patch(f"{_M}.send_lead_response.apply_async", calls.send))
        if machine:
            enter(patch(f"{_M}._agent_scoped", return_value=True))
            enter(patch(f"{_M}._edits_pending_only", return_value=True))
            enter(patch(f"{_M}._refuse_agent_approval"))
            enter(patch(f"{_M}._pending_edit_or_raise"))
            enter(patch(f"{_M}._mcp_approval_granted", return_value=True))
        resp = api_client.put("/api/lead_signal", json={
            "session_token": _S, "signal_id": 3, "action": "approve"})
    assert resp.status_code == 200, resp.text
    assert [c[0] for c in calls.mock_calls] == ["record", "send"]
