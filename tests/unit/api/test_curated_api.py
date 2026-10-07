"""`/api/curated/*` and the curated block on `GET /api/posts/` (docs/curated-sources.md).

Acceptance (#2260): an agent-scoped session may list and queue but is REFUSED `approve`; an owner
approval records `user:<id>` (the only approval the curated publisher accepts); a read failure is a
503, never an empty list; the Content Studio sees source, treatment and the exact credit line.
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from tests.unit.api.conftest import SESSION_TOKEN, SESSION_USER_ID

pytestmark = pytest.mark.unit

_R = "cqc_lem.api.routers.curated"
_MAIN = "cqc_lem.api.main"

SOURCE = {"id": 5, "platform": "linkedin", "url": "https://www.linkedin.com/feed/update/urn:li:share:9/",
          "canonical_id": "urn:li:share:9", "author": "Jane Doe", "publisher": "LinkedIn",
          "title": None, "licence": "linkedin_native", "status": "drafted", "block_reason": None,
          "post_id": 10, "link_only": False}
CONTEXT = {"post_id": 10, "post_user_id": SESSION_USER_ID, "post_status": "pending",
           "approved_by": None, "source_treatment": "reshare", "curated_source_id": 5,
           "source": SOURCE}


@pytest.fixture
def main_mod():
    from cqc_lem.api import main
    return main


@pytest.fixture
def as_agent(main_mod):
    token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_AGENT)
    yield
    main_mod._request_session_scope.reset(token)


class TestAgentScope:
    @pytest.mark.parametrize("path", ["/curated/sources", "/curated/source"])
    def test_the_queueing_paths_are_on_the_agent_surface(self, main_mod, path):
        key = main_mod._scope_path("/api" + path)
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT, key) is True

    def test_an_agent_may_never_approve_a_curated_draft(self, main_mod, as_agent):
        from cqc_lem.api.routers.curated import CuratedSourceActionRequest, act_on_curated_source
        with patch(f"{_R}.update_db_post_status") as approve, \
             patch(f"{_R}.get_curated_source") as read, \
             pytest.raises(HTTPException) as exc:
            act_on_curated_source(CuratedSourceActionRequest(session_token="t", source_id=5,
                                                             action="approve"))
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "agent_may_not_approve"
        approve.assert_not_called()
        read.assert_not_called()

    def test_an_agent_may_still_reject_and_queue(self, main_mod, as_agent):
        from cqc_lem.api.routers.curated import (
            CuratedSourceActionRequest,
            CuratedSourceRequest,
            act_on_curated_source,
            create_curated_source,
        )
        with patch(f"{_MAIN}.get_session_user_id", return_value=SESSION_USER_ID), \
             patch(f"{_R}.get_curated_source", return_value=SOURCE), \
             patch(f"{_R}.get_post_curated_context", return_value=CONTEXT), \
             patch(f"{_R}.bulk_update_posts") as reject_post, \
             patch(f"{_R}.update_curated_source_status", return_value=True):
            out = act_on_curated_source(CuratedSourceActionRequest(session_token="t",
                                                                   source_id=5, action="reject"))
        assert out.detail["status"] == "rejected"
        reject_post.assert_called_once()
        with patch(f"{_MAIN}.get_session_user_id", return_value=SESSION_USER_ID), \
             patch(f"{_R}.record_candidate", return_value=(8, "new")), \
             patch(f"{_R}.get_curated_source", return_value={"block_reason": None}):
            out = create_curated_source(CuratedSourceRequest(
                session_token="t", url="https://x.com/a/status/1", author="A", excerpt="Text"))
        assert out.detail == {"source_id": 8, "status": "new", "block_reason": None}


class TestRoutes:
    def test_list(self, api_client, signed_in):
        with patch(f"{_R}.get_curated_sources", return_value=[SOURCE]) as read:
            resp = api_client.get("/api/curated/sources",
                                  params={"session_token": SESSION_TOKEN, "status_filter": "new"})
        assert resp.status_code == 200
        row = resp.json()["detail"]["sources"][0]
        assert row["reshareable"] is True
        assert row["credit"] == "Source: Jane Doe, LinkedIn."
        assert read.call_args.args == (SESSION_USER_ID,)

    def test_list_read_failure_is_503_and_bad_status_422(self, api_client, signed_in):
        with patch(f"{_R}.get_curated_sources", return_value=None):
            resp = api_client.get("/api/curated/sources", params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 503
        resp = api_client.get("/api/curated/sources",
                              params={"session_token": SESSION_TOKEN, "status_filter": "nope"})
        assert resp.status_code == 422

    def test_no_session_is_401(self, api_client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=None):
            resp = api_client.get("/api/curated/sources", params={"session_token": "x"})
        assert resp.status_code == 401

    def test_paste_a_political_url_is_recorded_blocked(self, api_client, signed_in):
        with patch("cqc_lem.utilities.curated_collectors.insert_curated_source",
                   return_value=9) as insert, \
             patch(f"{_R}.get_curated_source",
                   return_value={"block_reason": "political:senator"}):
            resp = api_client.post("/api/curated/source", json={
                "session_token": SESSION_TOKEN, "url": "https://x.com/a/status/1",
                "author": "Senator Smith", "excerpt": "My bill."})
        assert resp.status_code == 200
        assert resp.json()["detail"]["status"] == "blocked"
        assert insert.call_args.kwargs["status"] == "blocked"

    def test_paste_validation(self, api_client, signed_in):
        resp = api_client.post("/api/curated/source", json={"session_token": SESSION_TOKEN,
                                                            "url": "javascript:alert(1)"})
        assert resp.status_code == 422
        with patch(f"{_R}.record_candidate", return_value=(None, "new")):
            resp = api_client.post("/api/curated/source", json={"session_token": SESSION_TOKEN,
                                                                "url": "https://a.b/c"})
        assert resp.status_code == 409

    def test_owner_approval_records_the_human_approver(self, api_client, signed_in):
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_R}.get_curated_source", return_value=SOURCE), \
             patch(f"{_R}.get_post_curated_context", return_value=CONTEXT), \
             patch(f"{_R}.update_db_post_status", return_value=True) as approve, \
             patch(f"{_R}.update_curated_source_status") as status:
            resp = api_client.put("/api/curated/source", json={
                "session_token": SESSION_TOKEN, "source_id": 5, "action": "approve"})
        assert resp.status_code == 200
        approve.assert_called_once_with(10, PostStatus.APPROVED,
                                        approved_by=f"user:{SESSION_USER_ID}")
        assert status.call_args.args == (5, "approved")

    @pytest.mark.parametrize("source,context,code", [
        (None, CONTEXT, 404),
        ({**SOURCE, "status": "new"}, CONTEXT, 409),
        (SOURCE, {**CONTEXT, "post_user_id": 999}, 409),
        (SOURCE, {**CONTEXT, "post_status": "approved"}, 409),
        (SOURCE, {"unreadable": True}, 503),
    ])
    def test_approval_refusals(self, api_client, signed_in, source, context, code):
        with patch(f"{_R}.get_curated_source", return_value=source), \
             patch(f"{_R}.get_post_curated_context", return_value=context), \
             patch(f"{_R}.update_db_post_status") as approve:
            resp = api_client.put("/api/curated/source", json={
                "session_token": SESSION_TOKEN, "source_id": 5, "action": "approve"})
        assert resp.status_code == code
        approve.assert_not_called()

    def test_failed_approve_write_and_unknown_action(self, api_client, signed_in):
        with patch(f"{_R}.get_curated_source", return_value=SOURCE), \
             patch(f"{_R}.get_post_curated_context", return_value=CONTEXT), \
             patch(f"{_R}.update_db_post_status", return_value=False):
            resp = api_client.put("/api/curated/source", json={
                "session_token": SESSION_TOKEN, "source_id": 5, "action": "approve"})
        assert resp.status_code == 500
        resp = api_client.put("/api/curated/source", json={
            "session_token": SESSION_TOKEN, "source_id": 5, "action": "publish"})
        assert resp.status_code == 422

    def test_reject_write_failure(self, api_client, signed_in):
        with patch(f"{_R}.get_curated_source", return_value={**SOURCE, "post_id": None}), \
             patch(f"{_R}.update_curated_source_status", return_value=False):
            resp = api_client.put("/api/curated/source", json={
                "session_token": SESSION_TOKEN, "source_id": 5, "action": "reject"})
        assert resp.status_code == 500


class TestPostsListCarriesTheSource:
    def test_a_curated_post_shows_source_treatment_and_credit(self, api_client, signed_in):
        posts = [{"id": 10, "content": "Take", "video_url": None, "scheduled_time": None,
                  "post_type": "text", "status": "pending", "carousel_slides": None,
                  "curated_source_id": 5, "source_treatment": "reshare"},
                 {"id": 11, "content": "Own", "video_url": None, "scheduled_time": None,
                  "post_type": "text", "status": "pending", "carousel_slides": None}]
        with patch(f"{_MAIN}.get_posts", return_value=(posts, 2)), \
             patch(f"{_MAIN}.get_curated_summaries", return_value={5: SOURCE}) as read:
            resp = api_client.get("/api/posts/", params={"session_token": SESSION_TOKEN})
        rows = resp.json()["detail"]["posts"]
        assert rows[0]["curated"] == {"source_id": 5, "treatment": "reshare",
                                      "platform": "linkedin", "url": SOURCE["url"],
                                      "credit": "Source: Jane Doe, LinkedIn.", "link_only": False}
        assert rows[1]["curated"] is None
        read.assert_called_once_with(SESSION_USER_ID, [5])

    def test_a_missing_source_still_flags_the_post(self):
        from cqc_lem.api.main import _curated_summary
        out = _curated_summary({"curated_source_id": 5, "source_treatment": "link"}, {})
        assert out["credit"] == "Source unavailable"
