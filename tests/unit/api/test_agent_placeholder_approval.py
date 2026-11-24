"""An MCP agent's post that is still a placeholder can never be approved (W2b, issue #2689).

`lem_generate_post` creates a PENDING row whose body is `AGENT_POST_PLACEHOLDER` until LEM's writer
replaces it. Every API path that can put a post on the road to LinkedIn refuses that row with a 409
`agent_draft_not_written`: `/update_post/` with a publishing status, `/posts/bulk_update/` with one,
and `/user/post/rescore` (a clean re-score promotes PENDING -> APPROVED). The posting lane's own
guards are in `tests/unit/app/test_mcp_spend_tasks.py`.
"""

from typing import Any
from unittest.mock import patch

import pytest

from cqc_lem.utilities.db import AGENT_POST_PLACEHOLDER
from tests.unit.api.conftest import SESSION_TOKEN  # noqa: E402

pytestmark = pytest.mark.unit

_MAIN = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_BODY = {"session_token": SESSION_TOKEN, "content": "A real draft.", "post_type": "text",
         "scheduled_datetime": "2099-01-01T10:00:00+00:00"}


def _code(resp: Any) -> str:
    return resp.json()["detail"]["code"]


class TestUpdatePost:
    @pytest.mark.parametrize("status", ["approved", "scheduled", "posted"])
    def test_a_publishing_status_on_a_stored_placeholder_is_409(
            self, api_client: Any, signed_in: int, status: str) -> None:
        with patch(f"{_MAIN}.get_post_content", return_value=AGENT_POST_PLACEHOLDER), \
                patch(f"{_MAIN}.update_db_post") as write:
            resp = api_client.post("/api/update_post/?post_id=33",
                                   json={**_BODY, "content": "", "status": status})
        assert resp.status_code == 409
        assert _code(resp) == "agent_draft_not_written"
        write.assert_not_called()

    def test_sending_the_placeholder_itself_with_approved_is_409(self, api_client: Any,
                                                                 signed_in: int) -> None:
        with patch(f"{_MAIN}.update_db_post") as write:
            resp = api_client.post("/api/update_post/?post_id=33", json={
                **_BODY, "content": AGENT_POST_PLACEHOLDER, "status": "approved"})
        assert resp.status_code == 409
        write.assert_not_called()

    def test_approving_with_real_text_replacing_the_placeholder_is_allowed(
            self, api_client: Any, signed_in: int) -> None:
        with patch(f"{_MAIN}.get_post_content", return_value=AGENT_POST_PLACEHOLDER), \
                patch(f"{_MAIN}.update_db_post", return_value=True) as write:
            resp = api_client.post("/api/update_post/?post_id=33",
                                   json={**_BODY, "status": "approved"})
        assert resp.status_code == 200
        write.assert_called_once()

    def test_a_non_publishing_edit_of_a_placeholder_is_not_refused(self, api_client: Any,
                                                                   signed_in: int) -> None:
        with patch(f"{_MAIN}.update_db_post", return_value=True):
            resp = api_client.post("/api/update_post/?post_id=33", json={
                **_BODY, "content": AGENT_POST_PLACEHOLDER, "status": "rejected"})
        assert resp.status_code == 200


class TestBulkUpdate:
    def test_approving_a_batch_holding_a_placeholder_is_409_and_writes_nothing(
            self, api_client: Any, signed_in: int) -> None:
        contents = {33: "A real draft.", 34: AGENT_POST_PLACEHOLDER}
        with patch(f"{_MAIN}.get_post_content", side_effect=contents.get), \
                patch(f"{_MAIN}.bulk_update_posts") as write:
            resp = api_client.post("/api/posts/bulk_update/", json={
                "session_token": SESSION_TOKEN, "post_ids": [33, 34], "status": "approved"})
        assert resp.status_code == 409
        assert _code(resp) == "agent_draft_not_written"
        write.assert_not_called()

    def test_rejecting_a_placeholder_batch_is_allowed(self, api_client: Any,
                                                      signed_in: int) -> None:
        with patch(f"{_MAIN}.get_post_content", return_value=AGENT_POST_PLACEHOLDER) as read, \
                patch(f"{_MAIN}.bulk_update_posts", return_value=True):
            resp = api_client.post("/api/posts/bulk_update/", json={
                "session_token": SESSION_TOKEN, "post_ids": [33], "status": "rejected"})
        assert resp.status_code == 200
        read.assert_not_called()  # a non-publishing status never reads the bodies


class TestRescore:
    def test_rescoring_a_placeholder_is_409_and_never_runs_the_judge(
            self, api_client: Any, signed_in: int) -> None:
        with patch(f"{_USER}.get_post_user_id", return_value=signed_in), \
                patch(f"{_USER}.get_post_status", return_value="pending"), \
                patch(f"{_USER}.get_post_content", return_value=AGENT_POST_PLACEHOLDER), \
                patch("cqc_lem.app.run_content_plan.rescore_post") as rescore:
            resp = api_client.post("/api/user/post/rescore",
                                   json={"session_token": SESSION_TOKEN, "post_id": 33})
        assert resp.status_code == 409
        assert _code(resp) == "agent_draft_not_written"
        rescore.assert_not_called()


def test_the_refusal_logs_at_debug_never_warning() -> None:
    """A click on a draft still being written is the designed answer, not a degraded path."""
    from fastapi import HTTPException

    from cqc_lem.api import main

    with patch(f"{_MAIN}.log_debug") as debug, patch(f"{_MAIN}.log_warning") as warn:
        with pytest.raises(HTTPException):
            main._refuse_placeholder_approval("approved", [AGENT_POST_PLACEHOLDER], user_id=7)
    debug.assert_called_once()
    warn.assert_not_called()
