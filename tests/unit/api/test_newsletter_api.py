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

    def test_put_clamps_max_invites_high(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True,
                "invite_connections_enabled": True, "max_invites_per_run": 9999})
        assert resp.status_code == 200
        args = upd.call_args[0][1]
        assert args["invite_connections_enabled"] is True and args["max_invites_per_run"] == 500

    def test_put_clamps_max_invites_low(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True, "max_invites_per_run": -10})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["max_invites_per_run"] == 0

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/newsletter-settings?session_token=bad")
        assert resp.status_code == 401


class TestNewsletterSubscribers:
    def test_get_returns_history_and_latest(self, client):
        history = [{"subscriber_count": 130, "invites_sent": 0, "captured_at": "2026-07-20T00:00:00"}]
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_settings",
                   return_value={"enabled": True, "newsletter_url": "https://li/news"}), \
             patch("cqc_lem.utilities.db.count_artifact_cta_deliveries",
                   return_value={"window_days": 90, "lead_magnet_dms": 2, "newsletter_links": 3}), \
             patch("cqc_lem.utilities.db.get_latest_newsletter_subscriber_count", return_value=130), \
             patch("cqc_lem.utilities.db.get_newsletter_subscriber_stats", return_value=history):
            resp = client.get(f"/api/user/newsletter-subscribers?session_token={_SESSION}")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["latest"] == 130 and detail["history"][0]["subscriber_count"] == 130

    def test_get_includes_owned_asset_delivery_attribution(self, client):
        """Issue #624: growth is only readable against the CTAs that actually delivered."""
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_settings",
                   return_value={"enabled": True, "newsletter_url": "https://li/news"}), \
             patch("cqc_lem.utilities.db.count_artifact_cta_deliveries",
                   return_value={"window_days": 90, "lead_magnet_dms": 2,
                                 "newsletter_links": 3}) as counts, \
             patch("cqc_lem.utilities.db.get_latest_newsletter_subscriber_count", return_value=130), \
             patch("cqc_lem.utilities.db.get_newsletter_subscriber_stats", return_value=[]):
            resp = client.get(f"/api/user/newsletter-subscribers?session_token={_SESSION}")
        assert resp.json()["detail"]["attribution"] == {
            "window_days": 90, "lead_magnet_dms": 2, "newsletter_links": 3}
        # The subscribe URL is what makes the link side countable — it must be passed through.
        assert counts.call_args.kwargs["newsletter_url"] == "https://li/news"

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/newsletter-subscribers?session_token=bad")
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
        # Explicit-UTC 'Z' (issue #546) — without it the browser parses the string as LOCAL time
        # and the queue renders every edition off by the viewer's offset.
        assert detail["editions"][0]["scheduled_for"] == "2026-07-07T13:00:00Z"
        assert detail["next_publish"] is not None
        assert detail["next_publish"].endswith("Z")
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

    def test_put_forwards_scheduled_datetime(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": _USER}), \
             patch("cqc_lem.api.main.update_newsletter_edition", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-draft", json={
                "session_token": _SESSION, "edition_id": 4, "action": "approve",
                "scheduled_datetime": "2026-07-10T19:00:00"})
        assert resp.status_code == 200
        assert upd.call_args.kwargs["status"] == "approved"
        assert upd.call_args.kwargs["scheduled_for"] is not None

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


def _png(width: int = 1280, height: int = 720) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 40, 90)).save(buf, format="PNG")
    return buf.getvalue()


_OWNED = {"id": 4, "user_id": _USER}


class TestNewsletterCoverUpload:
    """Issue #893 — the author's own artwork is the half that works standalone."""

    def test_upload_stores_the_cover_as_approved(self, client, tmp_path):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=dict(_OWNED)), \
             patch("cqc_lem.utilities.newsletter_cover.assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.db.set_edition_cover_image", return_value=True) as store:
            resp = client.post("/api/user/newsletter-draft/cover",
                               data={"session_token": _SESSION, "edition_id": 4},
                               files={"file": ("cover.png", _png(), "image/png")})
        assert resp.status_code == 200
        args = store.call_args[0]
        assert args[0] == 4 and args[1] == _USER
        assert args[3] == "upload" and args[4] == "approved"

    def test_a_rejected_image_is_a_400_and_stores_nothing(self, client, tmp_path):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=dict(_OWNED)), \
             patch("cqc_lem.utilities.newsletter_cover.assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.db.set_edition_cover_image") as store:
            resp = client.post("/api/user/newsletter-draft/cover",
                               data={"session_token": _SESSION, "edition_id": 4},
                               files={"file": ("cover.png", _png(200, 100), "image/png")})
        assert resp.status_code == 400
        assert "too small" in resp.json()["detail"]
        store.assert_not_called()

    def test_a_failed_write_leaves_no_orphan_file(self, client, tmp_path):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=dict(_OWNED)), \
             patch("cqc_lem.utilities.newsletter_cover.assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.db.set_edition_cover_image", return_value=False), \
             patch("cqc_lem.utilities.newsletter_cover.remove_cover_file") as rm:
            resp = client.post("/api/user/newsletter-draft/cover",
                               data={"session_token": _SESSION, "edition_id": 4},
                               files={"file": ("cover.png", _png(), "image/png")})
        assert resp.status_code == 500
        rm.assert_called_once()

    def test_replacing_a_cover_deletes_the_previous_file(self, client, tmp_path):
        existing = dict(_OWNED, cover_image_path="images/newsletter_covers/5/old.png")
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=existing), \
             patch("cqc_lem.utilities.newsletter_cover.assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.db.set_edition_cover_image", return_value=True), \
             patch("cqc_lem.utilities.newsletter_cover.remove_cover_file") as rm:
            resp = client.post("/api/user/newsletter-draft/cover",
                               data={"session_token": _SESSION, "edition_id": 4},
                               files={"file": ("cover.png", _png(), "image/png")})
        assert resp.status_code == 200
        assert rm.call_args[0][0] == "images/newsletter_covers/5/old.png"

    def test_404_when_not_owner(self, client, tmp_path):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": 999}), \
             patch("cqc_lem.utilities.db.set_edition_cover_image") as store:
            resp = client.post("/api/user/newsletter-draft/cover",
                               data={"session_token": _SESSION, "edition_id": 4},
                               files={"file": ("cover.png", _png(), "image/png")})
        assert resp.status_code == 404
        store.assert_not_called()

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post("/api/user/newsletter-draft/cover",
                               data={"session_token": "bad", "edition_id": 4},
                               files={"file": ("cover.png", _png(), "image/png")})
        assert resp.status_code == 401


class TestNewsletterCoverGenerate:
    def test_dispatches_the_task(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=dict(_OWNED)), \
             patch("cqc_lem.app.run_scheduler.generate_newsletter_cover") as task:
            resp = client.post("/api/user/newsletter-draft/cover/generate", json={
                "session_token": _SESSION, "edition_id": 4})
        assert resp.status_code == 200
        # No explicit choice sent -> Auto (None) rides through to the task.
        task.apply_async.assert_called_once_with(kwargs={"edition_id": 4, "use_avatar": None})

    def test_per_edition_avatar_choice_rides_through(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=dict(_OWNED)), \
             patch("cqc_lem.app.run_scheduler.generate_newsletter_cover") as task:
            resp = client.post("/api/user/newsletter-draft/cover/generate", json={
                "session_token": _SESSION, "edition_id": 4, "use_avatar": True})
        assert resp.status_code == 200
        task.apply_async.assert_called_once_with(kwargs={"edition_id": 4, "use_avatar": True})

    def test_404_when_not_owner_spends_nothing(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value={"id": 4, "user_id": 999}), \
             patch("cqc_lem.app.run_scheduler.generate_newsletter_cover") as task:
            resp = client.post("/api/user/newsletter-draft/cover/generate", json={
                "session_token": _SESSION, "edition_id": 4})
        assert resp.status_code == 404
        task.apply_async.assert_not_called()

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post("/api/user/newsletter-draft/cover/generate", json={
                "session_token": "bad", "edition_id": 4})
        assert resp.status_code == 401


class TestNewsletterCoverDecision:
    _WITH_COVER = dict(_OWNED, cover_image_path="images/newsletter_covers/5/a.png",
                       cover_image_source="ai", cover_image_status="pending_review")

    def test_approve_clears_the_cover_for_publish(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=dict(self._WITH_COVER)), \
             patch("cqc_lem.utilities.db.set_edition_cover_status", return_value=True) as upd:
            resp = client.post("/api/user/newsletter-draft/cover/decision", json={
                "session_token": _SESSION, "edition_id": 4, "action": "approve"})
        assert resp.status_code == 200
        assert upd.call_args[0] == (4, _USER, "approved")

    def test_remove_drops_the_row_and_the_file(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=dict(self._WITH_COVER)), \
             patch("cqc_lem.utilities.db.clear_edition_cover_image", return_value=True) as clear, \
             patch("cqc_lem.utilities.newsletter_cover.remove_cover_file") as rm:
            resp = client.post("/api/user/newsletter-draft/cover/decision", json={
                "session_token": _SESSION, "edition_id": 4, "action": "remove"})
        assert resp.status_code == 200
        clear.assert_called_once_with(4, _USER)
        assert rm.call_args[0][0] == "images/newsletter_covers/5/a.png"

    def test_404_when_there_is_no_cover(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=dict(_OWNED)), \
             patch("cqc_lem.utilities.db.set_edition_cover_status") as upd:
            resp = client.post("/api/user/newsletter-draft/cover/decision", json={
                "session_token": _SESSION, "edition_id": 4, "action": "approve"})
        assert resp.status_code == 404
        upd.assert_not_called()

    def test_unknown_action_is_a_400(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", return_value=dict(self._WITH_COVER)), \
             patch("cqc_lem.utilities.db.set_edition_cover_status") as upd, \
             patch("cqc_lem.utilities.db.clear_edition_cover_image") as clear:
            resp = client.post("/api/user/newsletter-draft/cover/decision", json={
                "session_token": _SESSION, "edition_id": 4, "action": "publish-now"})
        assert resp.status_code == 400
        upd.assert_not_called()
        clear.assert_not_called()

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post("/api/user/newsletter-draft/cover/decision", json={
                "session_token": "bad", "edition_id": 4, "action": "approve"})
        assert resp.status_code == 401


class TestNewsletterDraftCoverSurface:
    def test_queue_returns_a_url_and_never_the_filesystem_path(self, client):
        editions = [{"id": 4, "title": "T", "subtitle": "S", "body": "B", "status": "draft",
                     "scheduled_for": None,
                     "cover_image_path": "images/newsletter_covers/5/a.png",
                     "cover_image_source": "ai", "cover_image_status": "pending_review"}]
        settings = {"publish_day": 1, "publish_hour": 9, "cadence": "weekly",
                    "last_published_at": None, "max_queued_drafts": 1, "generate_lead_days": 3}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_pending_newsletter_editions", return_value=editions), \
             patch("cqc_lem.api.main.get_latest_edition_scheduled_for", return_value=None), \
             patch("cqc_lem.api.main.get_newsletter_settings", return_value=settings), \
             patch("cqc_lem.api.main.get_user_timezone", return_value="UTC"):
            resp = client.get(f"/api/user/newsletter-draft?session_token={_SESSION}")
        edition = resp.json()["detail"]["editions"][0]
        assert "cover_image_path" not in edition
        assert edition["cover_image_url"].endswith(
            "/api/assets?file_name=images/newsletter_covers/5/a.png")
        assert edition["cover_image_status"] == "pending_review"

    def test_settings_put_forwards_the_cover_opt_in(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True, "cover_image_auto": True})
        assert resp.status_code == 200
        assert upd.call_args[0][1]["cover_image_auto"] is True

    def test_cover_opt_in_defaults_off(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True) as upd:
            client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True})
        assert upd.call_args[0][1]["cover_image_auto"] is False
