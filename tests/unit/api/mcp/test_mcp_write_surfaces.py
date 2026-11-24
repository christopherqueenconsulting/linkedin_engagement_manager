"""The human-side surfaces W2 adds around agent writes (plan §3.5, §5).

- `GET /user/mcp-token/activity`: the account's `mcp_*` audit rows for the Agents tab.
- The Daily review: an MCP-created DM or connection request carries `agent_created`, and
  `POST /review/bulk` never approves one as part of a multi-item approve.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from cqc_lem.utilities import review_queue
from cqc_lem.utilities.review_actions import APPROVE_ONE_AT_A_TIME

pytestmark = pytest.mark.unit

_T = "cqc_lem.api.routers.mcp_tokens"
_R = "cqc_lem.api.routers.review"
_UID = 41


class TestActivityRoute:
    def _get(self, api_client: Any, rows: Any) -> tuple[Any, Any]:
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_UID), \
                patch(f"{_T}.list_auth_audit_events", return_value=rows) as read:
            resp = api_client.get("/api/user/mcp-token/activity", params={"session_token": "tok"})
        return resp, read

    def test_it_lists_the_mcp_rows_newest_first(self, api_client: Any) -> None:
        rows = [{"event": "mcp_write", "success": 1, "user_agent": "claude-code",
                 "details": '{"tool": "lem_queue_dm", "target_id": 5, "scope": "mcp_outreach"}',
                 "created_at": datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)},
                {"event": "mcp_approval", "success": 1, "user_agent": None,
                 "details": {"via": "action"}, "created_at": None},
                {"event": "mcp_access", "success": 1, "user_agent": None,
                 "details": "not json", "created_at": None}]
        resp, read = self._get(api_client, rows)
        assert resp.status_code == 200, resp.text
        read.assert_called_once_with(_UID, prefix="mcp_", limit=50)
        events = resp.json()["detail"]["events"]
        assert [e["event"] for e in events] == ["mcp_write", "mcp_approval", "mcp_access"]
        assert events[0]["details"]["target_id"] == 5
        assert events[0]["client"] == "claude-code"
        assert events[0]["created_at"].startswith("2026-11-24T03:00:00")
        assert events[1]["details"] == {"via": "action"}
        assert events[2]["details"] is None

    def test_an_unreadable_log_is_503_never_an_empty_list(self, api_client: Any) -> None:
        resp, _ = self._get(api_client, None)
        assert resp.status_code == 503

    def test_no_session_is_401(self, api_client: Any) -> None:
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.get("/api/user/mcp-token/activity", params={"session_token": "x"})
        assert resp.status_code == 401


def _queue_item(lane: str, item_id: int, agent: bool) -> dict:
    return {"lane": lane, "id": item_id, "allowed_actions": ["approve", "cancel"],
            "agent_created": agent}


class TestDailyReviewNeverBulkApprovesAgentRows:
    QUEUE = [_queue_item("scheduled_dms", 1, True), _queue_item("scheduled_dms", 2, False),
             _queue_item("connection_requests", 3, True)]

    def _bulk(self, api_client: Any, items: list, action: str = "approve") -> tuple[Any, Any]:
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_UID), \
                patch(f"{_R}.get_review_queue", return_value=self.QUEUE), \
                patch(f"{_R}.apply_review_action",
                      side_effect=lambda u, lane, i, a: {"lane": lane, "id": i, "ok": True,
                                                         "error": None}) as act:
            resp = api_client.post("/api/review/bulk", json={
                "session_token": "tok", "action": action,
                "items": [{"lane": lane, "id": i} for lane, i in items]})
        return resp, act

    def test_a_multi_item_approve_leaves_agent_rows_waiting(self, api_client: Any) -> None:
        resp, act = self._bulk(api_client, [("scheduled_dms", 1), ("scheduled_dms", 2),
                                            ("connection_requests", 3)])
        assert resp.status_code == 200, resp.text
        results = {(r["lane"], r["id"]): r for r in resp.json()["detail"]}
        assert results[("scheduled_dms", 1)]["error"] == APPROVE_ONE_AT_A_TIME
        assert results[("connection_requests", 3)]["error"] == APPROVE_ONE_AT_A_TIME
        assert results[("scheduled_dms", 2)]["ok"] is True
        assert [c.args[1:3] for c in act.call_args_list] == [("scheduled_dms", 2)]

    def test_an_agent_row_approved_alone_goes_through(self, api_client: Any) -> None:
        resp, act = self._bulk(api_client, [("scheduled_dms", 1)])
        assert resp.json()["detail"][0]["ok"] is True
        act.assert_called_once()

    def test_a_multi_item_cancel_still_covers_agent_rows(self, api_client: Any) -> None:
        resp, act = self._bulk(api_client, [("scheduled_dms", 1), ("connection_requests", 3)],
                               action="cancel")
        assert all(r["ok"] for r in resp.json()["detail"])
        assert act.call_count == 2


class TestTheQueueMarksAgentRows:
    def test_dm_and_connection_rows_carry_agent_created_from_their_source(self) -> None:
        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        dms = {"dms": [{"id": 1, "source": "mcp", "message": "m"},
                       {"id": 2, "source": "nurture", "message": "m"},
                       {"id": 3, "source": None, "message": "m"}]}
        crs = {"requests": [{"id": 4, "source": "mcp"}, {"id": 5, "source": "icp"}]}
        with patch("cqc_lem.utilities.db.get_scheduled_dms", return_value=dms), \
                patch("cqc_lem.utilities.db.get_connection_requests", return_value=crs):
            dm_items = review_queue._read_scheduled_dms(_UID, now)
            cr_items = review_queue._read_connection_requests(_UID, now)
        assert [i["agent_created"] for i in dm_items] == [True, False, False]
        assert [i["agent_created"] for i in cr_items] == [True, False]

    def test_every_other_lane_defaults_to_not_agent_created(self) -> None:
        item = review_queue._item("catchup_touches", 1, None, "s", "d", ("approve",), "/x")
        assert item["agent_created"] is False
