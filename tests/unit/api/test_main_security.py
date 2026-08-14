"""Unit tests for security-sensitive endpoints in cqc_lem.api.main."""

import io
import zipfile
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# GET /api/assets — path traversal protection
# ---------------------------------------------------------------------------

class TestAssetsPathTraversal:
    BASE = "/api/assets"

    @pytest.mark.parametrize("bad_name", [
        "../../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "../../src/cqc_lem/utilities/env_constants.py",
        "/etc/passwd",
        "subfolder/../../secret.txt",
    ])
    def test_path_traversal_rejected(self, api_client, bad_name):
        resp = api_client.get(self.BASE, params={"file_name": bad_name})
        assert resp.status_code in (400, 404), (
            f"Expected 400 or 404 for traversal attempt {bad_name!r}, got {resp.status_code}"
        )

    def test_empty_file_name_returns_400(self, api_client):
        resp = api_client.get(self.BASE, params={"file_name": ""})
        assert resp.status_code == 400

    def test_valid_file_name_does_not_get_400_from_traversal_check(self, api_client):
        # A valid filename that would simply 404 because the file doesn't exist
        # — it must NOT hit the 400 "Invalid file name" guard.
        resp = api_client.get(self.BASE, params={"file_name": "some_image.png"})
        assert resp.status_code != 400 or "Invalid file name" not in resp.text


# ---------------------------------------------------------------------------
# POST /api/avatar/training — ZIP upload validation
# ---------------------------------------------------------------------------

def _make_zip(*, file_count: int = 3, filename: str = "photo.jpg", content: bytes = b"fake") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(file_count):
            zf.writestr(f"{i}_{filename}", content)
    return buf.getvalue()


class TestAvatarTrainingUpload:
    BASE = "/api/avatar/training"

    def test_not_a_zip_returns_400(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.avatar.get_avatar_credit_balance", return_value=5):
            resp = api_client.post(
                self.BASE,
                data={"session_token": "tok", "trigger_word": "myface"},
                files={"photos": ("photos.zip", b"not a zip file", "application/zip")},
            )
        assert resp.status_code == 400
        assert "valid ZIP" in resp.json().get("detail", "")

    def test_zip_too_large_returns_413(self, api_client):
        big = b"x" * (51 * 1024 * 1024)  # 51 MB > 50 MB limit
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.avatar.get_avatar_credit_balance", return_value=5):
            resp = api_client.post(
                self.BASE,
                data={"session_token": "tok", "trigger_word": "myface"},
                files={"photos": ("photos.zip", big, "application/zip")},
            )
        assert resp.status_code == 413

    def test_valid_zip_reaches_training_logic(self, api_client):
        zip_bytes = _make_zip()
        # start_avatar_training is imported inside the endpoint function body,
        # so patch it at the source module rather than cqc_lem.api.main.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.avatar.get_avatar_credit_balance", return_value=5), \
             patch("cqc_lem.utilities.avatar.replicate_avatar.start_avatar_training", return_value="train_xyz"), \
             patch("cqc_lem.api.routers.avatar.deduct_avatar_credit"), \
             patch("cqc_lem.api.routers.avatar.insert_avatar_training", return_value=99):
            resp = api_client.post(
                self.BASE,
                data={"session_token": "tok", "trigger_word": "myface"},
                files={"photos": ("photos.zip", zip_bytes, "application/zip")},
            )
        # Either succeeded (200) or hit a downstream mock issue — but must NOT be 400/413
        assert resp.status_code not in (400, 413)

    def test_no_credits_returns_402(self, api_client):
        zip_bytes = _make_zip()
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.avatar.get_avatar_credit_balance", return_value=0):
            resp = api_client.post(
                self.BASE,
                data={"session_token": "tok", "trigger_word": "myface"},
                files={"photos": ("photos.zip", zip_bytes, "application/zip")},
            )
        assert resp.status_code == 402

    def test_unauthenticated_returns_401(self, api_client):
        zip_bytes = _make_zip()
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.post(
                self.BASE,
                data={"session_token": "bad_tok", "trigger_word": "myface"},
                files={"photos": ("photos.zip", zip_bytes, "application/zip")},
            )
        assert resp.status_code == 401
