"""Unit tests for the /api/user/newsletter-settings endpoints."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
        patch("cqc_lem.app.run_scheduler.generate_newsletter_drafts_for_user"),
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


_SESSION = "tok"
_USER = 5


class TestNewsletterSettings:
    def test_get_returns_settings(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_settings", return_value={"enabled": True, "cadence": "weekly"}):
            resp = client.get(f"/api/user/newsletter-settings?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["cadence"] == "weekly"

    def test_put_updates(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True, "title": "Weekly Wins", "cadence": "weekly"})
        assert resp.status_code == 200
        args = upd.call_args[0][1]
        assert "session_token" not in args and args["title"] == "Weekly Wins"

    def test_put_passes_publish_day_hour(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True, "publish_day": 3, "publish_hour": 14})
        assert resp.status_code == 200
        args = upd.call_args[0][1]
        assert args["publish_day"] == 3 and args["publish_hour"] == 14

    def test_put_clamps_draft_config(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True,
                "max_queued_drafts": 15, "generate_lead_days": 100})
        assert resp.status_code == 200
        args = upd.call_args[0][1]
        assert args["max_queued_drafts"] == 10 and args["generate_lead_days"] == 60

    def test_put_clamps_draft_config_low(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True,
                "max_queued_drafts": 0, "generate_lead_days": -5})
        assert resp.status_code == 200
        args = upd.call_args[0][1]
        assert args["max_queued_drafts"] == 1 and args["generate_lead_days"] == 0

    def test_put_enabled_triggers_queue_topup(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True), \
             patch("cqc_lem.app.run_scheduler.generate_newsletter_drafts_for_user") as task:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True, "max_queued_drafts": 3})
        assert resp.status_code == 200
        task.apply_async.assert_called_once_with(kwargs={"user_id": _USER})

    def test_put_disabled_does_not_trigger_topup(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True), \
             patch("cqc_lem.app.run_scheduler.generate_newsletter_drafts_for_user") as task:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": False, "max_queued_drafts": 3})
        assert resp.status_code == 200
        task.apply_async.assert_not_called()

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/newsletter-settings?session_token=bad")
        assert resp.status_code == 401


class TestNewsletterDraft:
    def test_get_returns_editions_and_next_publish(self, client):
        from datetime import datetime
        editions = [{"id": 4, "title": "T", "subtitle": "S", "body": "B", "status": "draft",
                     "scheduled_for": datetime(2026, 7, 7, 13, 0, 0)}]
        settings = {"publish_day": 1, "publish_hour": 9, "cadence": "weekly", "last_published_at": None,
                    "max_queued_drafts": 3, "generate_lead_days": 14}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_pending_newsletter_editions", return_value=editions), \
             patch("cqc_lem.api.main.get_latest_edition_scheduled_for", return_value=None), \
             patch("cqc_lem.api.main.get_newsletter_settings", return_value=settings), \
             patch("cqc_lem.api.main.get_user_timezone", return_value="UTC"):
            resp = client.get(f"/api/user/newsletter-draft?session_token={_SESSION}")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["editions"][0]["id"] == 4
        assert detail["editions"][0]["scheduled_for"].startswith("2026-07-07")
        assert detail["next_publish"] is not None
        assert detail["max_queued_drafts"] == 3 and detail["generate_lead_days"] == 14

    def test_get_empty_when_no_editions(self, client):
        settings = {"publish_day": 1, "publish_hour": 9, "cadence": "weekly", "last_published_at": None,
                    "max_queued_drafts": 1, "generate_lead_days": 3}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_pending_newsletter_editions", return_value=[]), \
             patch("cqc_lem.api.main.get_latest_edition_scheduled_for", return_value=None), \
             patch("cqc_lem.api.main.get_newsletter_settings", return_value=settings), \
             patch("cqc_lem.api.main.get_user_timezone", return_value="UTC"):
            resp = client.get(f"/api/user/newsletter-draft?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["editions"] == []

    def test_get_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/newsletter-draft?session_token=bad")
        assert resp.status_code == 401

    def test_put_approve(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": _USER}), \
             patch("cqc_lem.api.main.update_newsletter_edition", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-draft", json={
                "session_token": _SESSION, "edition_id": 4, "action": "approve"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] == "approved"

    def test_put_skip(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": _USER}), \
             patch("cqc_lem.api.main.update_newsletter_edition", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-draft", json={
                "session_token": _SESSION, "edition_id": 4, "action": "skip"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] == "skipped"

    def test_put_save_leaves_status_none(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": _USER}), \
             patch("cqc_lem.api.main.update_newsletter_edition", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-draft", json={
                "session_token": _SESSION, "edition_id": 4, "title": "New", "action": "save"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] is None

    def test_put_404_when_not_owner(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": 999}):
            resp = client.put("/api/user/newsletter-draft", json={
                "session_token": _SESSION, "edition_id": 4, "action": "save"})
        assert resp.status_code == 404

    def test_put_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.put("/api/user/newsletter-draft", json={
                "session_token": "bad", "edition_id": 4, "action": "save"})
        assert resp.status_code == 401


class TestNewsletterRegenerate:
    def test_dispatches_task_with_guidance(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": _USER}), \
             patch("cqc_lem.app.run_scheduler.regenerate_newsletter_edition") as task:
            resp = client.post("/api/user/newsletter-draft/regenerate", json={
                "session_token": _SESSION, "edition_id": 4, "guidance": "Make it about pricing"})
        assert resp.status_code == 200
        task.apply_async.assert_called_once_with(
            kwargs={"edition_id": 4, "guidance": "Make it about pricing"})

    def test_blank_guidance_becomes_none(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": _USER}), \
             patch("cqc_lem.app.run_scheduler.regenerate_newsletter_edition") as task:
            resp = client.post("/api/user/newsletter-draft/regenerate", json={
                "session_token": _SESSION, "edition_id": 4, "guidance": "   "})
        assert resp.status_code == 200
        task.apply_async.assert_called_once_with(kwargs={"edition_id": 4, "guidance": None})

    def test_404_when_not_owner(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": 999}), \
             patch("cqc_lem.app.run_scheduler.regenerate_newsletter_edition") as task:
            resp = client.post("/api/user/newsletter-draft/regenerate", json={
                "session_token": _SESSION, "edition_id": 4})
        assert resp.status_code == 404
        task.apply_async.assert_not_called()

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post("/api/user/newsletter-draft/regenerate", json={
                "session_token": "bad", "edition_id": 4})
        assert resp.status_code == 401
