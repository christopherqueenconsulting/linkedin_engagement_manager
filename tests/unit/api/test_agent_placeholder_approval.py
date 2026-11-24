"""An MCP agent's post that is still a placeholder can never be approved (W2b, issue #2689).

`lem_generate_post` creates a PENDING row whose body is `AGENT_POST_PLACEHOLDER` until LEM's writer
replaces it. Every API path that can put a post on the road to LinkedIn refuses that body:

- `/update_post/` writes the body it is SENT, so a publishing status with the placeholder body is a
  409 `agent_draft_not_written` with no read at all; a non-publishing save of the placeholder over a
  draft LEM has since written is a 409 `agent_placeholder_stale` (and the UPDATE repeats that check
  in its WHERE clause);
- `/posts/bulk_update/` approves the rest of a mixed batch and names the placeholders as `skipped`,
  is a 409 with their ids when every id is one, and excludes them in the UPDATE itself;
- `/user/post/rescore` (a clean re-score promotes PENDING -> APPROVED) is a 409.

Every read these depend on fails CLOSED (503). The posting lane's guards are in
`tests/unit/app/test_mcp_spend_tasks.py`.
"""

from typing import Any
from unittest.mock import patch

import pytest

from cqc_lem.utilities.db import AGENT_POST_PLACEHOLDER, OwnershipUnprovable
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
    def test_a_publishing_status_with_the_placeholder_body_is_409_without_a_read(
            self, api_client: Any, signed_in: int, status: str) -> None:
        with patch(f"{_MAIN}.get_post_content") as read, \
                patch(f"{_MAIN}.update_db_post") as write:
            resp = api_client.post("/api/update_post/?post_id=33", json={
                **_BODY, "content": AGENT_POST_PLACEHOLDER, "status": status})
        assert resp.status_code == 409
        assert _code(resp) == "agent_draft_not_written"
        read.assert_not_called()  # the body sent IS the body written: nothing to look up
        write.assert_not_called()

    def test_approving_with_real_text_over_the_placeholder_is_allowed(
            self, api_client: Any, signed_in: int) -> None:
        with patch(f"{_MAIN}.get_post_content") as read, \
                patch(f"{_MAIN}.update_db_post", return_value=True) as write:
            resp = api_client.post("/api/update_post/?post_id=33",
                                   json={**_BODY, "status": "approved"})
        assert resp.status_code == 200
        # `update_post` reads the stored body once for the voice-note edit capture; the placeholder
        # guard adds no read of its own, so that single read is the whole count.
        assert read.call_count == 1
        write.assert_called_once()

    def test_a_stale_save_cannot_overwrite_a_written_draft(self, api_client: Any,
                                                          signed_in: int) -> None:
        """A tab opened while the post was a placeholder, saved after LEM wrote the draft."""
        with patch(f"{_MAIN}.get_post_content", return_value="The draft LEM wrote."), \
                patch(f"{_MAIN}.update_db_post") as write:
            resp = api_client.post("/api/update_post/?post_id=33", json={
                **_BODY, "content": AGENT_POST_PLACEHOLDER, "status": "pending"})
        assert resp.status_code == 409
        assert _code(resp) == "agent_placeholder_stale"
        write.assert_not_called()

    def test_re_saving_a_placeholder_that_is_still_a_placeholder_is_allowed(
            self, api_client: Any, signed_in: int) -> None:
        with patch(f"{_MAIN}.get_post_content", return_value=AGENT_POST_PLACEHOLDER), \
                patch(f"{_MAIN}.update_db_post", return_value=True) as write:
            resp = api_client.post("/api/update_post/?post_id=33", json={
                **_BODY, "content": AGENT_POST_PLACEHOLDER, "status": "pending"})
        assert resp.status_code == 200
        write.assert_called_once()

    def test_an_unreadable_stored_body_fails_closed(self, api_client: Any,
                                                    signed_in: int) -> None:
        with patch(f"{_MAIN}.get_post_content", return_value=None), \
                patch(f"{_MAIN}.update_db_post") as write:
            resp = api_client.post("/api/update_post/?post_id=33", json={
                **_BODY, "content": AGENT_POST_PLACEHOLDER, "status": "pending"})
        assert resp.status_code == 503
        write.assert_not_called()


class TestBulkUpdate:
    def test_a_mixed_batch_approves_the_human_drafts_and_skips_the_placeholder(
            self, api_client: Any, signed_in: int) -> None:
        with patch(f"{_MAIN}.get_placeholder_post_ids", return_value=[34]) as held, \
                patch(f"{_MAIN}.bulk_update_posts", return_value=True) as write:
            resp = api_client.post("/api/posts/bulk_update/", json={
                "session_token": SESSION_TOKEN, "post_ids": [33, 34, 35], "status": "approved"})
        assert resp.status_code == 200
        assert resp.json()["skipped"] == [{"post_id": 34, "code": "agent_draft_not_written"}]
        held.assert_called_once_with(signed_in, [33, 34, 35])
        assert write.call_args.args[0] == [33, 35]
        assert write.call_args.kwargs["exclude_content"] == AGENT_POST_PLACEHOLDER

    def test_an_all_placeholder_batch_is_409_with_the_ids_and_writes_nothing(
            self, api_client: Any, signed_in: int) -> None:
        with patch(f"{_MAIN}.get_placeholder_post_ids", return_value=[33, 34]), \
                patch(f"{_MAIN}.bulk_update_posts") as write:
            resp = api_client.post("/api/posts/bulk_update/", json={
                "session_token": SESSION_TOKEN, "post_ids": [34, 33], "status": "scheduled"})
        assert resp.status_code == 409
        assert _code(resp) == "agent_draft_not_written"
        assert resp.json()["detail"]["post_ids"] == [33, 34]
        write.assert_not_called()

    def test_an_unreadable_batch_fails_closed(self, api_client: Any, signed_in: int) -> None:
        with patch(f"{_MAIN}.get_placeholder_post_ids", side_effect=OwnershipUnprovable("db")), \
                patch(f"{_MAIN}.bulk_update_posts") as write:
            resp = api_client.post("/api/posts/bulk_update/", json={
                "session_token": SESSION_TOKEN, "post_ids": [33], "status": "approved"})
        assert resp.status_code == 503
        write.assert_not_called()

    def test_a_non_publishing_status_neither_reads_nor_excludes(self, api_client: Any,
                                                                signed_in: int) -> None:
        with patch(f"{_MAIN}.get_placeholder_post_ids") as held, \
                patch(f"{_MAIN}.bulk_update_posts", return_value=True) as write:
            resp = api_client.post("/api/posts/bulk_update/", json={
                "session_token": SESSION_TOKEN, "post_ids": [33], "status": "rejected"})
        assert resp.status_code == 200
        assert resp.json()["skipped"] == []
        held.assert_not_called()
        assert write.call_args.kwargs["exclude_content"] is None


class TestRescore:
    def _rescore(self, api_client: Any, signed_in: int, content: Any) -> tuple[Any, Any]:
        with patch(f"{_USER}.get_post_user_id", return_value=signed_in), \
                patch(f"{_USER}.get_post_status", return_value="pending"), \
                patch(f"{_USER}.get_post_content", return_value=content), \
                patch("cqc_lem.app.run_content_plan.rescore_post") as rescore:
            resp = api_client.post("/api/user/post/rescore",
                                   json={"session_token": SESSION_TOKEN, "post_id": 33})
        return resp, rescore

    def test_rescoring_a_placeholder_is_409_and_never_runs_the_judge(
            self, api_client: Any, signed_in: int) -> None:
        resp, rescore = self._rescore(api_client, signed_in, AGENT_POST_PLACEHOLDER)
        assert resp.status_code == 409
        assert _code(resp) == "agent_draft_not_written"
        rescore.assert_not_called()

    def test_an_unreadable_body_fails_closed(self, api_client: Any, signed_in: int) -> None:
        resp, rescore = self._rescore(api_client, signed_in, None)
        assert resp.status_code == 503
        rescore.assert_not_called()


def test_both_routes_declare_their_409_and_503() -> None:
    from cqc_lem.api.main import app

    paths = app.openapi()["paths"]
    for path in ("/api/update_post/", "/api/posts/bulk_update/"):
        responses = paths[path]["post"]["responses"]
        assert {"409", "503"} <= set(responses), path


def test_the_refusal_logs_at_debug_never_warning() -> None:
    """A click on a draft still being written is the designed answer, not a degraded path."""
    from fastapi import HTTPException

    from cqc_lem.api import main

    with patch(f"{_MAIN}.log_debug") as debug, patch(f"{_MAIN}.log_warning") as warn:
        with pytest.raises(HTTPException):
            main._refuse_placeholder_approval("approved", [AGENT_POST_PLACEHOLDER], user_id=7)
    debug.assert_called_once()
    warn.assert_not_called()
