"""Integration tests for `POST /user/post/video` (issue #1443).

The endpoint the Content Studio's "Upload video" button calls before it attaches a video to the
weekly group post. What is under test is the contract that button depends on: a file that fails the
video contract is a 400 with a reason and NOTHING on disk, a file that passes lands under the
caller's own preview dir, and the URL it hands back is one `PUT /user/group-post-draft` will accept
from that caller and no other.
"""

import os
from unittest.mock import patch

import pytest

_SESSION = "tok"
_USER = 5

_PV = "cqc_lem.utilities.post_video"


def _mp4(brand: bytes = b"isom", size_bytes: int = 200 * 1024) -> bytes:
    """Bytes that open with a valid ISO `ftyp` box — the deterministic half of the gate."""
    box = (24).to_bytes(4, "big") + b"ftyp" + brand + b"\x00" * 12
    return box + b"\x00" * max(0, size_bytes - len(box))


@pytest.fixture
def assets(tmp_path):
    """A real temporary assets dir, with ffprobe out of the picture.

    The measured half is covered in `tests/unit/utilities/test_post_video.py`; here the file is a
    valid container with no real stream in it, which is exactly the fail-open case an API container
    without ffmpeg installed takes.
    """
    with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
         patch(f"{_PV}.assets_dir", str(tmp_path)), \
         patch(f"{_PV}.shutil.which", return_value=None):
        yield tmp_path


def _upload(api_client, data=None, name="clip.mp4"):
    return api_client.post("/api/user/post/video", data={"session_token": _SESSION},
                           files={"file": (name, data if data is not None else _mp4(),
                                           "video/mp4")})


@pytest.mark.integration
class TestUploadPostVideo:
    def test_a_valid_upload_lands_under_the_callers_preview_dir(self, api_client, assets):
        resp = _upload(api_client)
        assert resp.status_code == 200
        url = resp.json()["detail"]["video_url"]
        assert f"file_name=videos/post_previews/{_USER}/" in url
        assert os.listdir(assets / "videos" / "post_previews" / str(_USER))

    def test_the_url_it_hands_back_is_one_the_group_draft_will_accept(self, api_client, assets):
        from cqc_lem.utilities.post_video import owns_post_media_url
        url = _upload(api_client).json()["detail"]["video_url"]
        assert owns_post_media_url(_USER, url) is True
        # …and only from the account it was issued to.
        assert owns_post_media_url(_USER + 1, url) is False

    def test_a_file_that_is_not_a_video_is_a_400_and_stores_nothing(self, api_client, assets):
        resp = _upload(api_client, b"PK\x03\x04" + b"\x00" * (200 * 1024), name="clip.mp4")
        assert resp.status_code == 400
        assert "MP4 or MOV" in resp.json()["detail"]
        assert not (assets / "videos").exists()

    def test_a_truncated_upload_is_a_400(self, api_client, assets):
        resp = _upload(api_client, _mp4(size_bytes=2048))
        assert resp.status_code == 400
        assert "too small" in resp.json()["detail"]

    def test_an_upload_over_the_cap_is_refused_without_being_read_into_memory(self, api_client,
                                                                             assets):
        """The cap is enforced while the upload streams, not after it.

        A 5 GB POST would otherwise be read into memory to find out it is over the limit; the read
        stops one byte past it instead.
        """
        from cqc_lem.utilities.post_video import MAX_POST_VIDEO_BYTES
        with patch("cqc_lem.api.routers.user.MAX_POST_VIDEO_BYTES", 4096):
            resp = _upload(api_client, _mp4(size_bytes=64 * 1024))
        assert resp.status_code == 400
        assert "larger than" in resp.json()["detail"]
        assert not (assets / "videos").exists()
        assert MAX_POST_VIDEO_BYTES > 4096  # the real cap is not what this test pinned

    def test_a_session_that_is_not_valid_is_refused(self, api_client, tmp_path):
        from fastapi import HTTPException
        with patch("cqc_lem.api.main.require_session_user_id",
                   side_effect=HTTPException(status_code=401, detail="Invalid or expired session")), \
             patch(f"{_PV}.assets_dir", str(tmp_path)):
            resp = _upload(api_client)
        assert resp.status_code == 401
        assert not (tmp_path / "videos").exists()
