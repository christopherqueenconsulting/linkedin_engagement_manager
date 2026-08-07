"""Integration tests for the newsletter cover-image endpoints (issue #893).

These drive the FULL lifecycle against a stateful stand-in for the editions table and a real
temporary assets dir, so the thing under test is the contract the SPA actually depends on: what an
upload leaves behind, that a generated cover cannot publish until it is approved, and that removing
one takes the file with it.
"""

import io
import os
from unittest.mock import patch

import pytest

_SESSION = "tok"
_USER = 5
_OTHER_USER = 999
_EDITION = 4


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


def _png(width: int = 1280, height: int = 720) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 40, 90)).save(buf, format="PNG")
    return buf.getvalue()


class _EditionStore:
    """A stateful stand-in for the newsletter_editions row, scoped by owner like the real SQL."""

    def __init__(self, user_id: int = _USER):
        self.row = {"id": _EDITION, "user_id": user_id, "title": "T", "subtitle": "S",
                    "body": "B", "status": "draft", "scheduled_for": None,
                    "cover_image_path": None, "cover_image_source": None,
                    "cover_image_status": None}

    def get(self, edition_id):
        return dict(self.row) if edition_id == self.row["id"] else None

    def set_cover(self, edition_id, user_id, path, source, status):
        if edition_id != self.row["id"] or user_id != self.row["user_id"]:
            return True  # matches the real UPDATE: no row touched, no error
        self.row.update(cover_image_path=path, cover_image_source=source,
                        cover_image_status=status)
        return True

    def set_status(self, edition_id, user_id, status):
        if edition_id != self.row["id"] or user_id != self.row["user_id"]:
            return True
        if self.row["cover_image_path"]:
            self.row["cover_image_status"] = status
        return True

    def clear_cover(self, edition_id, user_id):
        if edition_id != self.row["id"] or user_id != self.row["user_id"]:
            return True
        self.row.update(cover_image_path=None, cover_image_source=None, cover_image_status=None)
        return True

    def pending(self, user_id):
        return [dict(self.row)] if user_id == self.row["user_id"] else []


@pytest.fixture
def store(tmp_path):
    s = _EditionStore()
    settings = {"publish_day": 1, "publish_hour": 9, "cadence": "weekly",
                "last_published_at": None, "max_queued_drafts": 1, "generate_lead_days": 3}
    with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
         patch("cqc_lem.api.main.get_newsletter_edition", side_effect=s.get), \
         patch("cqc_lem.api.main.get_pending_newsletter_editions", side_effect=s.pending), \
         patch("cqc_lem.api.main.get_latest_edition_scheduled_for", return_value=None), \
         patch("cqc_lem.api.main.get_newsletter_settings", return_value=settings), \
         patch("cqc_lem.api.main.get_user_timezone", return_value="UTC"), \
         patch("cqc_lem.utilities.db.set_edition_cover_image", side_effect=s.set_cover), \
         patch("cqc_lem.utilities.db.set_edition_cover_status", side_effect=s.set_status), \
         patch("cqc_lem.utilities.db.clear_edition_cover_image", side_effect=s.clear_cover), \
         patch("cqc_lem.utilities.newsletter_cover.assets_dir", str(tmp_path)):
        s.assets_dir = str(tmp_path)
        yield s


def _upload(client, data=None):
    return client.post("/api/user/newsletter-draft/cover",
                       data={"session_token": _SESSION, "edition_id": _EDITION},
                       files={"file": ("cover.png", data or _png(), "image/png")})


def _queue(client):
    return client.get(f"/api/user/newsletter-draft?session_token={_SESSION}").json()["detail"]


@pytest.mark.integration
class TestUploadLifecycle:
    def test_upload_lands_approved_and_shows_up_on_the_queue(self, client, store):
        assert _upload(client).status_code == 200
        edition = _queue(client)["editions"][0]
        assert edition["cover_image_source"] == "upload"
        # The author's own artwork needs no review — it publishes with the edition.
        assert edition["cover_image_status"] == "approved"
        assert edition["cover_image_url"].startswith("http")
        assert "cover_image_path" not in edition

    def test_the_uploaded_file_is_on_disk_under_the_users_own_folder(self, client, store):
        assert _upload(client).status_code == 200
        relative = store.row["cover_image_path"]
        assert relative.startswith(f"images/newsletter_covers/{_USER}/")
        assert os.path.isfile(os.path.join(store.assets_dir, relative))

    def test_re_uploading_replaces_the_file_and_deletes_the_old_one(self, client, store):
        assert _upload(client).status_code == 200
        first = os.path.join(store.assets_dir, store.row["cover_image_path"])
        assert _upload(client, _png(1600, 900)).status_code == 200
        second = os.path.join(store.assets_dir, store.row["cover_image_path"])
        assert first != second
        assert not os.path.exists(first)
        assert os.path.isfile(second)

    def test_a_portrait_image_is_rejected_and_the_edition_keeps_no_cover(self, client, store):
        resp = _upload(client, _png(700, 1400))
        assert resp.status_code == 400
        assert store.row["cover_image_path"] is None
        assert _queue(client)["editions"][0]["cover_image_url"] is None

    def test_remove_takes_the_row_and_the_file(self, client, store):
        assert _upload(client).status_code == 200
        path = os.path.join(store.assets_dir, store.row["cover_image_path"])
        resp = client.post("/api/user/newsletter-draft/cover/decision", json={
            "session_token": _SESSION, "edition_id": _EDITION, "action": "remove"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["cover_image_url"] is None
        assert store.row["cover_image_status"] is None
        assert not os.path.exists(path)


@pytest.mark.integration
class TestGeneratedCoverGate:
    def test_a_generated_cover_is_pending_until_the_author_approves_it(self, client, store):
        # The task is what writes the row; the endpoint only queues it.
        with patch("cqc_lem.app.run_scheduler.generate_newsletter_cover") as task:
            resp = client.post("/api/user/newsletter-draft/cover/generate", json={
                "session_token": _SESSION, "edition_id": _EDITION})
        assert resp.status_code == 200
        task.apply_async.assert_called_once_with(kwargs={"edition_id": _EDITION,
                                                         "use_avatar": None})

        store.set_cover(_EDITION, _USER, f"images/newsletter_covers/{_USER}/gen.png",
                        "ai", "pending_review")
        edition = _queue(client)["editions"][0]
        assert edition["cover_image_status"] == "pending_review"

        # Nothing may publish it while it is pending.
        from cqc_lem.app.run_automation import _approved_cover_path
        assert _approved_cover_path(store.row) is None

        resp = client.post("/api/user/newsletter-draft/cover/decision", json={
            "session_token": _SESSION, "edition_id": _EDITION, "action": "approve"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["cover_image_status"] == "approved"
        assert store.row["cover_image_status"] == "approved"

    def test_approving_an_edition_with_no_cover_is_a_404(self, client, store):
        resp = client.post("/api/user/newsletter-draft/cover/decision", json={
            "session_token": _SESSION, "edition_id": _EDITION, "action": "approve"})
        assert resp.status_code == 404


@pytest.mark.integration
class TestCoverOwnership:
    def test_another_users_edition_is_a_404_on_every_cover_route(self, client, tmp_path):
        foreign = _EditionStore(user_id=_OTHER_USER)
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_edition", side_effect=foreign.get), \
             patch("cqc_lem.utilities.newsletter_cover.assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.db.set_edition_cover_image") as store_cover, \
             patch("cqc_lem.app.run_scheduler.generate_newsletter_cover") as task:
            assert _upload(client).status_code == 404
            assert client.post("/api/user/newsletter-draft/cover/generate", json={
                "session_token": _SESSION, "edition_id": _EDITION}).status_code == 404
            assert client.post("/api/user/newsletter-draft/cover/decision", json={
                "session_token": _SESSION, "edition_id": _EDITION,
                "action": "remove"}).status_code == 404
        store_cover.assert_not_called()
        task.apply_async.assert_not_called()
        assert not os.path.exists(os.path.join(str(tmp_path), "images"))
