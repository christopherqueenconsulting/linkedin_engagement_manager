"""Integration tests for the /api/user/story-bank CRUD (issue #620).

The story bank is the only sanctioned source of personal specifics in generated posts, so the SPA
has to be able to read it, seed it, edit it and retire entries — and none of that may leak across
users.
"""

import pytest
from unittest.mock import patch

_SESSION = "tok"
_USER = 5


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
    ]
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient
        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


@pytest.mark.integration
class TestGetStoryBank:
    def test_returns_entries_kinds_and_the_seeding_target(self, client):
        entry = {"id": 1, "kind": "client_win", "title": "Onboarding",
                 "body": "We cut it from 12 days to 3.", "happened_at": None,
                 "used_count": 0, "last_used_at": None, "active": True}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_story_bank_entries", return_value=[entry]) as get:
            resp = client.get(f"/api/user/story-bank?session_token={_SESSION}")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["entries"][0]["kind"] == "client_win"
        assert "anecdote" in detail["kinds"]
        assert detail["target_entries"] >= 5
        get.assert_called_once_with(_USER)

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/story-bank?session_token=bad")
        assert resp.status_code == 401


@pytest.mark.integration
class TestUpdateStoryBank:
    def test_seeds_new_entries(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_story_bank_entries", return_value=True) as upd:
            resp = client.put("/api/user/story-bank", json={
                "session_token": _SESSION,
                "entries": [{"kind": "mistake", "body": "I shipped a migration on a Friday.",
                             "happened_at": "2026-03-04"}]})
        assert resp.status_code == 200
        saved = upd.call_args[0][1][0]
        assert saved["kind"] == "mistake"
        assert saved["happened_at"] == "2026-03-04"
        assert upd.call_args[0][0] == _USER

    def test_unknown_kind_falls_back_to_anecdote(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_story_bank_entries", return_value=True) as upd:
            resp = client.put("/api/user/story-bank", json={
                "session_token": _SESSION,
                "entries": [{"kind": "rumour", "body": "Something that happened."}]})
        assert resp.status_code == 200
        assert upd.call_args[0][1][0]["kind"] == "anecdote"

    def test_editing_an_existing_entry_keeps_its_id(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_story_bank_entries", return_value=True) as upd:
            client.put("/api/user/story-bank", json={
                "session_token": _SESSION,
                "entries": [{"id": 9, "body": "edited", "active": False}]})
        saved = upd.call_args[0][1][0]
        assert saved["id"] == 9 and saved["active"] is False

    def test_missing_body_is_422(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER):
            resp = client.put("/api/user/story-bank", json={
                "session_token": _SESSION, "entries": [{"title": "no body"}]})
        assert resp.status_code == 422

    def test_over_long_body_is_422(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER):
            resp = client.put("/api/user/story-bank", json={
                "session_token": _SESSION, "entries": [{"body": "x" * 5001}]})
        assert resp.status_code == 422

    def test_500_on_failure(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_story_bank_entries", return_value=False):
            resp = client.put("/api/user/story-bank",
                              json={"session_token": _SESSION, "entries": []})
        assert resp.status_code == 500

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.put("/api/user/story-bank",
                              json={"session_token": "bad", "entries": []})
        assert resp.status_code == 401


@pytest.mark.integration
class TestDeleteStoryBankEntry:
    def test_deletes_scoped_to_the_session_user(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.delete_story_bank_entry", return_value=True) as dele:
            resp = client.request("DELETE", "/api/user/story-bank",
                                  json={"session_token": _SESSION, "entry_id": 9})
        assert resp.status_code == 200
        dele.assert_called_once_with(_USER, 9)

    def test_500_on_failure(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.delete_story_bank_entry", return_value=False):
            resp = client.request("DELETE", "/api/user/story-bank",
                                  json={"session_token": _SESSION, "entry_id": 9})
        assert resp.status_code == 500

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.request("DELETE", "/api/user/story-bank",
                                  json={"session_token": "bad", "entry_id": 9})
        assert resp.status_code == 401
