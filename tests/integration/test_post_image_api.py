"""Integration tests for the post-image endpoints (issue #1030).

These drive the full lifecycle against a stateful stand-in for the posts row and a real temporary
assets dir, so what is under test is the contract the Content Studio depends on: what an upload
leaves on the row AND on disk, that a compose-time image touches no row at all, that only a preview
this account was issued can ride into `/schedule_post/`, and that removing one takes the file.
"""

import io
import os
from unittest.mock import patch

import pytest

_SESSION = "tok"
_USER = 5
_OTHER_USER = 999
_POST = 42


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


def _png(width: int = 800, height: int = 800, fmt: str = "PNG") -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 40, 90)).save(buf, format=fmt)
    return buf.getvalue()


class _PostStore:
    """A stateful stand-in for the posts row, scoped by owner like the real SQL."""

    def __init__(self, user_id: int = _USER, status: str = "pending"):
        self.row = {"id": _POST, "user_id": user_id, "content": "Stored draft text",
                    "status": status, "image_url": None}
        self.inserted: list = []

    def owner(self, post_id):
        return self.row["user_id"] if post_id == self.row["id"] else None

    def status(self, post_id):
        return self.row["status"] if post_id == self.row["id"] else None

    def content(self, post_id):
        return self.row["content"] if post_id == self.row["id"] else None

    def image_url(self, post_id):
        return self.row["image_url"] if post_id == self.row["id"] else None

    def set_image_url(self, post_id, url):
        if post_id != self.row["id"]:
            return True  # matches the real UPDATE: no row touched, no error
        self.row["image_url"] = url
        return True

    def insert(self, *args, **kwargs):
        self.inserted.append(kwargs)
        return True


@pytest.fixture
def store(tmp_path):
    s = _PostStore()
    with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
         patch("cqc_lem.api.main.get_user_email", return_value="user@example.com"), \
         patch("cqc_lem.api.main.record_auth_event", return_value=True), \
         patch("cqc_lem.api.main.get_post_user_id", side_effect=s.owner), \
         patch("cqc_lem.api.main.get_post_status", side_effect=s.status), \
         patch("cqc_lem.api.main.get_post_content", side_effect=s.content), \
         patch("cqc_lem.api.main.get_post_image_url", side_effect=s.image_url), \
         patch("cqc_lem.api.main.update_db_post_image_url", side_effect=s.set_image_url), \
         patch("cqc_lem.api.main.insert_post", side_effect=s.insert), \
         patch("cqc_lem.utilities.post_image.assets_dir", str(tmp_path)):
        s.assets_dir = str(tmp_path)
        yield s


def _upload(client, data=None, post_id=_POST, name="shot.png"):
    payload = {"session_token": _SESSION}
    if post_id is not None:
        payload["post_id"] = post_id
    return client.post("/api/user/post/image", data=payload,
                       files={"file": (name, data or _png(), "image/png")})


def _generate(client, **body):
    return client.post("/api/user/post/image/generate",
                       json={"session_token": _SESSION, **body})


def _abs(store, url):
    from cqc_lem.utilities.post_image import post_image_relative_path
    return os.path.join(store.assets_dir, post_image_relative_path(url))


@pytest.mark.integration
class TestUploadLifecycle:
    def test_upload_attaches_the_image_to_the_row_and_writes_the_file(self, client, store):
        resp = _upload(client)
        assert resp.status_code == 200
        url = resp.json()["detail"]["image_url"]
        assert store.row["image_url"] == url
        assert f"file_name=images/posts/{_POST}/" in url
        assert os.path.isfile(_abs(store, url))

    def test_re_uploading_replaces_the_file_and_deletes_the_old_one(self, client, store):
        first = _upload(client).json()["detail"]["image_url"]
        first_path = _abs(store, first)
        second = _upload(client, _png(1200, 1200)).json()["detail"]["image_url"]
        assert first != second
        assert not os.path.exists(first_path)
        assert os.path.isfile(_abs(store, second))

    def test_a_format_linkedin_will_not_take_is_a_400_and_the_row_keeps_nothing(self, client, store):
        resp = _upload(client, _png(fmt="GIF"), name="shot.gif")
        assert resp.status_code == 400
        assert "PNG or JPG" in resp.json()["detail"]
        assert store.row["image_url"] is None

    def test_a_thumbnail_sized_upload_is_a_400(self, client, store):
        assert _upload(client, _png(100, 100)).status_code == 400
        assert store.row["image_url"] is None

    def test_remove_clears_the_row_and_deletes_the_file(self, client, store):
        url = _upload(client).json()["detail"]["image_url"]
        path = _abs(store, url)
        resp = client.post("/api/user/post/image/remove",
                           json={"session_token": _SESSION, "post_id": _POST})
        assert resp.status_code == 200
        assert resp.json()["detail"]["image_url"] is None
        assert store.row["image_url"] is None
        assert not os.path.exists(path)


@pytest.mark.integration
class TestComposeTimePreview:
    def test_an_upload_with_no_post_id_touches_no_row(self, client, store):
        resp = _upload(client, post_id=None)
        assert resp.status_code == 200
        url = resp.json()["detail"]["image_url"]
        assert f"file_name=images/post_previews/{_USER}/" in url
        assert store.row["image_url"] is None
        assert os.path.isfile(_abs(store, url))

    def test_scheduling_carries_this_accounts_preview_onto_the_new_post(self, client, store):
        url = _upload(client, post_id=None).json()["detail"]["image_url"]
        resp = client.post("/api/schedule_post/", json={
            "session_token": _SESSION, "content": "Hello", "post_type": "text",
            "scheduled_datetime": "2026-08-10T09:00:00", "status": "pending",
            "image_url": url,
        })
        assert resp.status_code == 200
        assert store.inserted[-1]["image_url"] == url

    def test_a_url_we_never_issued_is_dropped_rather_than_stored(self, client, store):
        """`image_url` is caller-supplied on a field the publish step later fetches — anything but
        this account's own preview is refused.
        """
        for hostile in ("https://evil.test/payload.png",
                        f"https://api.test/api/assets?file_name=images/post_previews/{_OTHER_USER}/x.png",
                        "https://api.test/api/assets?file_name=../../etc/passwd"):
            resp = client.post("/api/schedule_post/", json={
                "session_token": _SESSION, "content": "Hello", "post_type": "text",
                "scheduled_datetime": "2026-08-10T09:00:00", "status": "pending",
                "image_url": hostile,
            })
            assert resp.status_code == 200
            assert store.inserted[-1]["image_url"] is None


@pytest.mark.integration
class TestGenerate:
    def test_generation_attaches_the_render_and_prefers_the_text_on_screen(self, client, store):
        with patch("cqc_lem.api.main.generate_image_for_post",
                   return_value=("https://api.test/api/assets?file_name=images/posts/42/g.png",
                                 None)) as gen:
            resp = _generate(client, post_id=_POST, content="Unsaved edit")
        assert resp.status_code == 200
        assert gen.call_args[0][1] == "Unsaved edit"
        assert store.row["image_url"].endswith("g.png")

    def test_generation_falls_back_to_the_stored_draft(self, client, store):
        with patch("cqc_lem.api.main.generate_image_for_post",
                   return_value=("https://api.test/api/assets?file_name=images/posts/42/g.png",
                                 None)) as gen:
            assert _generate(client, post_id=_POST).status_code == 200
        assert gen.call_args[0][1] == "Stored draft text"

    def test_a_compose_time_generation_needs_no_post(self, client, store):
        preview = f"https://api.test/api/assets?file_name=images/post_previews/{_USER}/g.png"
        with patch("cqc_lem.api.main.generate_image_for_post", return_value=(preview, None)):
            resp = _generate(client, content="Fresh copy")
        assert resp.status_code == 200
        assert resp.json()["detail"] == {"post_id": None, "image_url": preview}
        assert store.row["image_url"] is None

    def test_a_draft_longer_than_a_linkedin_post_still_generates(self, client, store):
        """Nothing truncates a generated draft and the Review & Edit textarea has no maxLength, so
        a 3000-char field cap here would answer 422 — whose `detail` is a list the SPA cannot
        render, leaving the author with an unexplainable failure on their own post.
        """
        with patch("cqc_lem.api.main.generate_image_for_post",
                   return_value=("https://api.test/api/assets?file_name=images/posts/42/g.png",
                                 None)) as gen:
            resp = _generate(client, post_id=_POST, content="x" * 4000)
        assert resp.status_code == 200
        assert len(gen.call_args[0][1]) == 4000

    def test_nothing_written_is_a_400_before_any_spend(self, client, store):
        with patch("cqc_lem.api.main.generate_image_for_post") as gen:
            assert _generate(client, content="   ").status_code == 400
        gen.assert_not_called()

    def test_a_failed_render_is_a_502_carrying_the_reason(self, client, store):
        with patch("cqc_lem.api.main.generate_image_for_post",
                   return_value=(None, "Image generation failed")):
            resp = _generate(client, post_id=_POST)
        assert resp.status_code == 502
        assert resp.json()["detail"] == "Image generation failed"
        assert store.row["image_url"] is None

    def test_the_hourly_cap_refuses_before_the_render(self, client, store):
        with patch("cqc_lem.api.main.claim_manual_generation", return_value=False), \
             patch("cqc_lem.api.main.generate_image_for_post") as gen:
            resp = _generate(client, post_id=_POST, content="More please")
        assert resp.status_code == 429
        gen.assert_not_called()


@pytest.mark.integration
class TestOwnershipAndState:
    def test_another_users_post_is_a_404_on_every_image_route(self, client, tmp_path):
        foreign = _PostStore(user_id=_OTHER_USER)
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.record_auth_event", return_value=True), \
             patch("cqc_lem.api.main.get_post_user_id", side_effect=foreign.owner), \
             patch("cqc_lem.api.main.get_post_status", side_effect=foreign.status), \
             patch("cqc_lem.utilities.post_image.assets_dir", str(tmp_path)), \
             patch("cqc_lem.api.main.update_db_post_image_url") as write, \
             patch("cqc_lem.api.main.generate_image_for_post") as gen:
            assert _upload(client).status_code == 404
            assert _generate(client, post_id=_POST, content="x").status_code == 404
            assert client.post("/api/user/post/image/remove", json={
                "session_token": _SESSION, "post_id": _POST}).status_code == 404
        write.assert_not_called()
        gen.assert_not_called()
        assert not os.path.exists(os.path.join(str(tmp_path), "images"))

    def test_a_published_post_is_a_409(self, client, store):
        store.row["status"] = "posted"
        with patch("cqc_lem.api.main.generate_image_for_post") as gen:
            assert _upload(client).status_code == 409
            assert _generate(client, post_id=_POST, content="x").status_code == 409
            assert client.post("/api/user/post/image/remove", json={
                "session_token": _SESSION, "post_id": _POST}).status_code == 409
        gen.assert_not_called()
        assert store.row["image_url"] is None

    def test_no_session_is_a_401(self, client, tmp_path):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None), \
             patch("cqc_lem.api.main.record_auth_event", return_value=True), \
             patch("cqc_lem.utilities.post_image.assets_dir", str(tmp_path)):
            assert _upload(client, post_id=None).status_code == 401
            assert _generate(client, content="x").status_code == 401
        assert not os.path.exists(os.path.join(str(tmp_path), "images"))
