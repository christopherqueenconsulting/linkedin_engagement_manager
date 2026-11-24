"""Other people's data on the `mcp` scope, the resource budget, and paging that never drops a row.

The P3 review items, end to end through the real gate, SDK, registry and loopback:

- an activity row that is not a post action logs its TARGET (a DM or invite recipient's profile) in
  `post_url`, so `mcp` never sees it;
- a DM task's `title` is the recipient's name or message, withheld from `mcp` everywhere it surfaces;
- a data resource spends the tool-call budget like the tool behind it;
- a page trimmed to fit 64 KB is refused rather than silently losing rows;
- the cursor is a positive page number, and a curated post's credit and URL are fenced.
"""

import json
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api.mcp import gate
from cqc_lem.api.mcp.redact import RESULT_CAP_BYTES, THIRD_PARTY_NOTE

from ._admitted import admitted, call, rpc, structured

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_PROFILE = "https://www.linkedin.com/in/pat-recipient/"
_POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:1/"


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")


@pytest.fixture
def mcp(resolver: MagicMock) -> Iterator[MagicMock]:
    with admitted(resolver, "mcp") as inner:
        yield inner


@pytest.fixture
def outreach(resolver: MagicMock) -> Iterator[MagicMock]:
    with admitted(resolver, "mcp_outreach") as inner:
        yield inner


def _log(action_type: str, url: str) -> dict:
    return {"id": hash(action_type) % 1000, "action_type": action_type, "result": "success",
            "post_id": None, "post_url": url, "message": "m", "created_at": None}


_LOGS = [_log("dm", _PROFILE), _log("invite", _PROFILE), _log("followup", _PROFILE),
         _log("follow", _PROFILE), _log("engaged", _PROFILE), _log("comment", _POST_URL),
         _log("post", _POST_URL), _log("reply", _POST_URL), _log("group_comment", _POST_URL)]


class TestActivityTargets:
    def test_mcp_never_sees_a_recipient_profile(self, api_client: Any, mcp: MagicMock) -> None:
        with patch(f"{_M}.get_recent_logs", return_value=[dict(r) for r in _LOGS]):
            rows = structured(call(api_client, "lem_get_activity", {"limit": 20}))["activity"]
        urls = {r["action_type"]: r["post_url"] for r in rows}
        for person_action in ("dm", "invite", "followup", "follow", "engaged"):
            assert urls[person_action] is None, person_action
        for post_action in ("comment", "post", "reply", "group_comment"):
            assert urls[post_action] == _POST_URL, post_action
        assert "pat-recipient" not in json.dumps(rows)

    def test_mcp_outreach_keeps_the_target(self, api_client: Any, outreach: MagicMock) -> None:
        with patch(f"{_M}.get_recent_logs", return_value=[dict(r) for r in _LOGS]):
            rows = structured(call(api_client, "lem_get_activity"))["activity"]
        assert {r["action_type"]: r["post_url"] for r in rows}["dm"] == _PROFILE


_DM_TASK = {"kind": "DM", "id": 4, "title": "Pat Lee", "status": "approved",
            "scheduled_time": None}
_POST_TASK = {"kind": "Post", "id": 5, "title": "My own post", "status": "approved",
              "scheduled_time": None}


class TestDmTaskTitles:
    def test_the_dashboard_tool(self, api_client: Any, mcp: MagicMock) -> None:
        with patch(f"{_M}.get_planned_tasks", return_value=[dict(_DM_TASK), dict(_POST_TASK)]), \
             patch(f"{_M}.get_dashboard_counts", return_value={"posted": 0}):
            body = structured(call(api_client, "lem_get_dashboard"))
        assert [t["title"] for t in body["upcoming"]] == [None, "My own post"]
        assert "Pat Lee" not in json.dumps(body)

    @pytest.mark.parametrize("uri", ["lem://account/overview", "lem://plan/upcoming"])
    def test_the_resources(self, api_client: Any, mcp: MagicMock, uri: str) -> None:
        with patch(f"{_M}.get_planned_tasks", return_value=[dict(_DM_TASK), dict(_POST_TASK)]), \
             patch(f"{_M}.get_dashboard_counts", return_value={"posted": 0}), \
             patch(f"{_M}.get_posts", return_value=([], 0)):
            read = rpc(api_client, "resources/read", {"uri": uri}).json()
        text = read["result"]["contents"][0]["text"]
        assert "My own post" in text, "the tasks did come back"
        assert "Pat Lee" not in text

    def test_outreach_gets_it_fenced(self, api_client: Any, outreach: MagicMock) -> None:
        with patch(f"{_M}.get_planned_tasks", return_value=[dict(_DM_TASK)]), \
             patch(f"{_M}.get_dashboard_counts", return_value={}):
            task = structured(call(api_client, "lem_get_dashboard"))["upcoming"][0]
        assert task["third_party"] == {"note": THIRD_PARTY_NOTE, "title": "Pat Lee"}
        assert "title" not in task


class TestResourceBudget:
    def test_rpc_summary_counts_data_resources_only(self) -> None:
        def summary(uri: str) -> int:
            body = {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}}
            return gate._rpc_summary(json.dumps(body).encode()).tool_calls

        assert summary("lem://guide/how-lem-works") == 0
        assert summary("lem://policy/write-rules") == 0
        for uri in ("lem://account/overview", "lem://plan/upcoming", "lem://post/5", "lem://x"):
            assert summary(uri) == 1, uri

    def test_a_spent_budget_refuses_data_reads_not_the_static_ones(
            self, api_client: Any, mcp: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_RATE_TOOL_CALLS_PER_MIN", "0")
        static = rpc(api_client, "resources/read", {"uri": "lem://policy/write-rules"})
        assert static.status_code == 200
        for uri in ("lem://account/overview", "lem://plan/upcoming", "lem://post/5"):
            refused = rpc(api_client, "resources/read", {"uri": uri})
            assert refused.status_code == 429, uri
            assert refused.json()["reason"] == "rate_limited"


def _post_row(post_id: int, size: int) -> dict:
    return {"id": post_id, "content": "x" * size, "video_url": None, "image_url": None,
            "scheduled_time": None, "post_type": "text", "status": "pending",
            "carousel_slides": None, "authenticity_score": None, "gate_reason": None,
            "rejection_reason": None, "archetype": None, "manual_publish": 0,
            "curated_source_id": None, "source_treatment": None}


class TestPagingNeverDropsARow:
    def test_an_oversized_page_is_refused_not_trimmed(self, api_client: Any,
                                                      mcp: MagicMock) -> None:
        rows = [_post_row(i, 20_000) for i in range(1, 6)]
        assert 5 * 20_000 > RESULT_CAP_BYTES
        with patch(f"{_M}.get_posts", return_value=(rows, 5)):
            result = call(api_client, "lem_list_posts", {"limit": 5})
        assert result["isError"] is True
        assert structured(result)["error"]["code"] == "result_too_large"
        assert "limit" in structured(result)["error"]["message"]

    def test_a_smaller_page_reaches_every_row(self, api_client: Any, mcp: MagicMock) -> None:
        rows = [_post_row(i, 20_000) for i in range(1, 6)]
        seen: list[int] = []
        cursor = None
        with patch(f"{_M}.get_posts",
                   side_effect=lambda user_id, limit, offset, **_kw: (rows[offset:offset + limit],
                                                                       len(rows))):
            for _ in range(5):
                args = {"limit": 2, **({"cursor": cursor} if cursor else {})}
                body = structured(call(api_client, "lem_list_posts", args))
                assert "truncated" not in body
                seen += [p["post_id"] for p in body["posts"]]
                cursor = body["next_cursor"]
                if cursor is None:
                    break
        assert seen == [1, 2, 3, 4, 5]


class TestCursor:
    @pytest.mark.parametrize("cursor", ["0", "00", "01", "-1", "1.0"])
    def test_only_a_positive_page_number(self, api_client: Any, mcp: MagicMock,
                                         cursor: str) -> None:
        result = call(api_client, "lem_list_posts", {"cursor": cursor})
        assert result["isError"] is True
        assert structured(result)["error"]["code"] == "invalid_arguments"


class TestCuratedSource:
    _SOURCE = {"platform": "web", "url": "https://example.com/their-piece", "link_only": 0,
               "author": "Jordan Author", "publisher": "Example Times"}

    def _row(self) -> dict:
        return {**_post_row(7, 10), "curated_source_id": 4, "source_treatment": "commentary"}

    def test_credit_and_url_are_withheld_by_default(self, api_client: Any, mcp: MagicMock) -> None:
        with patch(f"{_M}.get_posts", return_value=([self._row()], 1)), \
             patch(f"{_M}.get_curated_summaries", return_value={4: dict(self._SOURCE)}):
            post = structured(call(api_client, "lem_list_posts"))["posts"][0]
        assert post["curated"]["source_id"] == 4 and post["curated"]["treatment"] == "commentary"
        assert "url" not in post["curated"] and "credit" not in post["curated"]
        assert "example.com" not in json.dumps(post)

    def test_include_text_fences_them(self, api_client: Any, mcp: MagicMock) -> None:
        with patch(f"{_M}.get_post_for_user", return_value=self._row()), \
             patch(f"{_M}.get_post_url_from_log_for_user", return_value=None), \
             patch(f"{_M}.get_curated_summaries", return_value={4: dict(self._SOURCE)}):
            curated = structured(call(api_client, "lem_get_post",
                                      {"post_id": 7, "include_text": True}))["post"]["curated"]
        assert curated["third_party"]["url"] == self._SOURCE["url"]
        assert curated["third_party"]["note"] == THIRD_PARTY_NOTE
        assert "credit" in curated["third_party"]
