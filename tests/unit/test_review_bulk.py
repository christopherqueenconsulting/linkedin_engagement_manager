"""`POST /api/review/bulk` and the per-lane action helpers (#2625 Phase 2)."""

from unittest.mock import patch

import pytest

from cqc_lem.utilities import review_actions as ra
from cqc_lem.utilities.db import (
    CatchupTouchStatus,
    ConnectionRequestStatus,
    OutreachStatus,
    ScheduledDmStatus,
)

pytestmark = pytest.mark.unit
A = "cqc_lem.utilities.review_actions."


def test_stale_dm_approve_moves_slot_to_now():
    with patch(A + "get_scheduled_dm_user_id", return_value=1), \
         patch(A + "get_scheduled_dm", return_value={"scheduled_time": "old"}), \
         patch("cqc_lem.app.engagement.outreach.scheduled_dm_is_stale", return_value=True), \
         patch(A + "update_scheduled_dm", return_value=True) as upd:
        assert ra.approve_scheduled_dm(1, 5) is None
    assert upd.call_args.kwargs["scheduled_time"] is not None
    assert upd.call_args.kwargs["status"] == ScheduledDmStatus.APPROVED


def test_fresh_dm_approve_leaves_slot():
    with patch(A + "get_scheduled_dm_user_id", return_value=1), \
         patch(A + "get_scheduled_dm", return_value={"scheduled_time": "new"}), \
         patch("cqc_lem.app.engagement.outreach.scheduled_dm_is_stale", return_value=False), \
         patch(A + "update_scheduled_dm", return_value=True) as upd:
        ra.approve_scheduled_dm(1, 5)
    assert upd.call_args.kwargs["scheduled_time"] is None


def test_foreign_rows_are_not_found_on_every_lane():
    with patch(A + "get_scheduled_dm_user_id", return_value=2), \
         patch(A + "get_connection_request_user_id", return_value=2), \
         patch(A + "get_outreach_target_user_id", return_value=2), \
         patch(A + "get_catchup_touch", return_value={"user_id": 2, "message": "m"}):
        for lane, fns in ra.ACTION_LANES.items():
            for action, fn in fns.items():
                assert fn(1, 9) == ra.NOT_FOUND, (lane, action)


def test_approvals_stamp_approved_by():
    with patch(A + "get_connection_request_user_id", return_value=1), \
         patch(A + "get_outreach_target_user_id", return_value=1), \
         patch(A + "user_approver", return_value="user:1"), \
         patch(A + "update_connection_request", return_value=True) as cr, \
         patch(A + "update_outreach_target", return_value=True) as ot:
        assert ra.approve_connection_request(1, 2) is None
        assert ra.approve_funnel_target(1, 3) is None
    assert cr.call_args.kwargs == {"status": ConnectionRequestStatus.APPROVED, "approved_by": "user:1"}
    assert ot.call_args.kwargs == {"status": OutreachStatus.APPROVED, "approved_by": "user:1"}


def test_catchup_empty_message_refused_and_cancel_paths():
    with patch(A + "get_catchup_touch", return_value={"user_id": 1, "message": " "}):
        assert ra.approve_catchup_touch(1, 4) == ra.EMPTY_MESSAGE
    with patch(A + "get_catchup_touch", return_value={"user_id": 1, "message": "hi"}), \
         patch(A + "update_catchup_touch", return_value=True), \
         patch(A + "update_catchup_touch_status", return_value=False) as st:
        assert ra.approve_catchup_touch(1, 4) is None
        assert ra.cancel_catchup_touch(1, 4) == ra.WRITE_FAILED
    st.assert_called_with(4, CatchupTouchStatus.CANCELED)


def test_cancel_paths_succeed():
    with patch(A + "get_scheduled_dm_user_id", return_value=1), \
         patch(A + "get_connection_request_user_id", return_value=1), \
         patch(A + "get_outreach_target_user_id", return_value=1), \
         patch(A + "update_scheduled_dm_status", return_value=True), \
         patch(A + "update_connection_request_status", return_value=True), \
         patch(A + "update_outreach_target_status", return_value=True) as ot:
        assert ra.cancel_scheduled_dm(1, 1) is None
        assert ra.cancel_connection_request(1, 1) is None
        assert ra.cancel_funnel_target(1, 1) is None
    ot.assert_called_once_with(1, OutreachStatus.CANCELED)


def test_apply_isolates_faults_and_edit_only_lanes():
    assert ra.apply_review_action(1, "posts", 1, "approve")["error"] == ra.NOT_ACTIONABLE
    with patch(A + "get_scheduled_dm_user_id", side_effect=RuntimeError("x")):
        out = ra.apply_review_action(1, "scheduled_dms", 1, "cancel")
    assert out == {"lane": "scheduled_dms", "id": 1, "ok": False, "error": ra.WRITE_FAILED}


# --- route ------------------------------------------------------------------------------------

def _post(client, items, action="approve", user=7):
    with patch("cqc_lem.api.main.get_session_user_id", return_value=user):
        return client.post("/api/review/bulk", json={"session_token": "t", "items": items,
                                                     "action": action})


def test_bulk_results_are_per_item(api_client):
    def fake(user_id, lane, item_id, action):
        return {"lane": lane, "id": item_id, "ok": item_id == 1, "error": None if item_id == 1 else "x"}
    with patch("cqc_lem.api.routers.review.apply_review_action", side_effect=fake) as ap:
        r = _post(api_client, [{"lane": "scheduled_dms", "id": 1}, {"lane": "catchup_touches", "id": 2}])
    assert r.status_code == 200
    assert [d["ok"] for d in r.json()["detail"]] == [True, False]
    ap.assert_any_call(7, "scheduled_dms", 1, "approve")


def test_bulk_rejects_over_cap_empty_and_bad_action(api_client):
    over = [{"lane": "scheduled_dms", "id": i} for i in range(101)]
    assert _post(api_client, over).status_code == 422
    assert _post(api_client, []).status_code == 422
    assert _post(api_client, [{"lane": "scheduled_dms", "id": 1}], action="nuke").status_code == 422


def test_bulk_accepts_exactly_the_cap(api_client):
    items = [{"lane": "scheduled_dms", "id": i} for i in range(100)]
    with patch("cqc_lem.api.routers.review.apply_review_action",
               return_value={"ok": True}) as ap:
        assert _post(api_client, items).status_code == 200
    assert ap.call_count == 100


def test_bulk_needs_a_session(api_client):
    assert _post(api_client, [{"lane": "scheduled_dms", "id": 1}], user=None).status_code == 401


def test_agent_session_gets_403_and_nothing_runs(api_client):
    with patch("cqc_lem.api.main._agent_scoped", return_value=True), \
         patch("cqc_lem.api.main._operator_may_approve", return_value=False), \
         patch("cqc_lem.api.routers.review.apply_review_action") as ap:
        r = _post(api_client, [{"lane": "scheduled_dms", "id": 1}])
    assert r.status_code == 403 and not ap.called
