"""Integration tests for the group-post preview/edit API (issue #932).

The weekly group post used to be written and published inside one Selenium run, so a user only ever
saw the per-group toggle — never the text. These endpoints are the preview: the draft is readable
before it ships, editable in place, and skippable. Everything is scoped to the caller's OWN open
draft — the request never names a draft id, so one session can't reach another user's post.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_API = "cqc_lem.api.main"

_DRAFT = {"id": 11, "user_id": 1, "group_id": "g1", "group_name": "AI Leaders",
          "content": "A useful insight.", "status": "ready", "created_at": "2026-08-02T15:00:00",
          "updated_at": "2026-08-02T15:00:00", "published_at": None}


def _client():
    from fastapi.testclient import TestClient

    from cqc_lem.api.main import app
    return TestClient(app)


class TestGetGroupPostDraft:
    def test_returns_the_queued_post_text(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.get_open_group_post_draft", return_value=dict(_DRAFT)):
            r = _client().get("/api/user/group-post-draft?session_token=tok")
        assert r.status_code == 200
        assert r.json()["detail"]["content"] == "A useful insight."
        assert r.json()["detail"]["group_name"] == "AI Leaders"

    def test_nothing_queued_is_null_not_an_error(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.get_open_group_post_draft", return_value=None):
            r = _client().get("/api/user/group-post-draft?session_token=tok")
        assert r.status_code == 200
        assert r.json()["detail"] is None

    def test_401_without_a_session(self):
        with patch(f"{_API}.get_session_user_id", return_value=None), \
             patch(f"{_API}.get_open_group_post_draft") as read:
            r = _client().get("/api/user/group-post-draft?session_token=bad")
        assert r.status_code == 401
        read.assert_not_called()


class TestPutGroupPostDraft:
    def test_saves_the_users_rewrite(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.get_open_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_API}.update_group_post_draft", return_value=True) as saved:
            r = _client().put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "My own words."})
        assert r.status_code == 200
        saved.assert_called_once_with(11, content="My own words.", status=None)

    def test_skipping_cancels_this_weeks_post(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.get_open_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_API}.update_group_post_draft", return_value=True) as saved:
            r = _client().put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "skipped"})
        assert r.status_code == 200
        assert str(saved.call_args.kwargs["status"]) == "skipped"

    def test_the_draft_is_resolved_from_the_session_not_the_request(self):
        """A caller-supplied id would let one session edit another user's post — it is ignored."""
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.get_open_group_post_draft", return_value=dict(_DRAFT)) as read, \
             patch(f"{_API}.update_group_post_draft", return_value=True) as saved:
            r = _client().put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "Mine.", "id": 99})
        assert r.status_code == 200
        read.assert_called_once_with(1)
        assert saved.call_args[0][0] == 11

    @pytest.mark.parametrize("body", [
        {"content": "   "},                    # emptying it is not how you cancel
        {"content": "x" * 3001},               # past LinkedIn's own post cap
        {"status": "published"},               # only the publish run may claim a ship
        {},                                    # nothing asked for
    ])
    def test_rejects_a_write_that_would_corrupt_the_queued_post(self, body):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.get_open_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_API}.update_group_post_draft") as saved:
            r = _client().put("/api/user/group-post-draft", json={"session_token": "tok", **body})
        assert r.status_code == 422
        saved.assert_not_called()

    def test_404_when_nothing_is_queued(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.get_open_group_post_draft", return_value=None), \
             patch(f"{_API}.update_group_post_draft") as saved:
            r = _client().put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "Mine."})
        assert r.status_code == 404
        saved.assert_not_called()

    def test_a_failed_write_is_a_500(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.get_open_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_API}.update_group_post_draft", return_value=False):
            r = _client().put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "Mine."})
        assert r.status_code == 500

    def test_401_without_a_session(self):
        with patch(f"{_API}.get_session_user_id", return_value=None), \
             patch(f"{_API}.get_open_group_post_draft") as read, \
             patch(f"{_API}.update_group_post_draft") as saved:
            r = _client().put("/api/user/group-post-draft",
                              json={"session_token": "bad", "content": "Mine."})
        assert r.status_code == 401
        read.assert_not_called()
        saved.assert_not_called()
