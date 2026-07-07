"""Coverage tests for LinkedIn poster media upload and share routing."""

import json
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_P = "cqc_lem.utilities.linkedin.poster"


def _register_upload_response(asset="urn:li:digitalmediaAsset:abc"):
    resp = MagicMock()
    resp.json.return_value = {
        "value": {
            "uploadMechanism": {
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {
                    "uploadUrl": "https://upload.linkedin.com/x",
                    "headers": {},
                }
            },
            "asset": asset,
            "mediaArtifact": "urn:li:digitalmediaMediaArtifact:xyz",
        }
    }
    return resp


class TestUploadMedia:
    def test_uploads_local_file_and_returns_asset(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import upload_media
        media = tmp_path / "img.png"
        media.write_bytes(b"png-bytes")
        put_resp = MagicMock(status_code=201)
        with patch(f"{_P}.requests.post",
                   return_value=_register_upload_response()) as post, \
             patch(f"{_P}.requests.put", return_value=put_resp) as put:
            asset = upload_media("tok", "SUB1", str(media), "image")
        assert asset == "urn:li:digitalmediaAsset:abc"
        register_body = json.loads(post.call_args[1]["data"])
        assert register_body["registerUploadRequest"]["owner"] == "urn:li:person:SUB1"
        assert "feedshare-image" in register_body["registerUploadRequest"]["recipes"][0]
        assert put.call_args[0][0] == "https://upload.linkedin.com/x"
        assert put.call_args[1]["data"] == b"png-bytes"

    def test_downloads_url_media_and_cleans_temp(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import upload_media
        tmp_file = tmp_path / "dl.png"
        tmp_file.write_bytes(b"dl")
        put_resp = MagicMock(status_code=201)
        with patch(f"{_P}.download_media", return_value=str(tmp_file)) as dl, \
             patch(f"{_P}.requests.post", return_value=_register_upload_response()), \
             patch(f"{_P}.requests.put", return_value=put_resp):
            asset = upload_media("tok", "SUB1", "https://cdn/img.png", "image")
        dl.assert_called_once_with("https://cdn/img.png")
        assert asset == "urn:li:digitalmediaAsset:abc"
        assert not tmp_file.exists()  # temp download removed

    def test_raises_when_upload_not_201(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import upload_media
        media = tmp_path / "img.png"
        media.write_bytes(b"x")
        put_resp = MagicMock(status_code=500)
        with patch(f"{_P}.requests.post", return_value=_register_upload_response()), \
             patch(f"{_P}.requests.put", return_value=put_resp):
            with pytest.raises(Exception, match="Media upload failed"):
                upload_media("tok", "SUB1", str(media), "image")


class TestDetermineMediaType:
    def test_image(self):
        from cqc_lem.utilities.linkedin.poster import determine_media_type
        assert determine_media_type("/a/pic.png") == "IMAGE"

    def test_video(self):
        from cqc_lem.utilities.linkedin.poster import determine_media_type
        assert determine_media_type("/a/clip.mp4") == "VIDEO"

    def test_unsupported_raises(self):
        from cqc_lem.utilities.linkedin.poster import determine_media_type
        with pytest.raises(ValueError, match="Unsupported media type"):
            determine_media_type("/a/file.txt")


class TestShareOnLinkedIn:
    def _restli(self, urn="urn:li:share:1"):
        client = MagicMock()
        client.create.return_value = MagicMock(entity_id=urn)
        return client

    def test_no_credentials_returns_none(self):
        from cqc_lem.utilities.linkedin.poster import share_on_linkedin
        with patch(f"{_P}.RestliClient", return_value=self._restli()), \
             patch(f"{_P}.get_user_linked_sub_id", return_value=None), \
             patch(f"{_P}.get_user_access_token", return_value=None):
            assert share_on_linkedin(1, "hello") is None

    def test_video_media_sets_video_category(self):
        from cqc_lem.utilities.linkedin.poster import share_on_linkedin
        restli = self._restli()
        with patch(f"{_P}.RestliClient", return_value=restli), \
             patch(f"{_P}.get_user_linked_sub_id", return_value="SUB1"), \
             patch(f"{_P}.get_user_access_token", return_value="tok"), \
             patch(f"{_P}.upload_media", return_value="urn:li:asset:v1") as up:
            urn = share_on_linkedin(1, "caption", media_path="/a/clip.mp4")
        assert urn == "urn:li:share:1"
        up.assert_called_once_with("tok", "SUB1", "/a/clip.mp4", "VIDEO")
        entity = restli.create.call_args[1]["entity"]
        share = entity["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == "VIDEO"
        assert share["media"][0]["media"] == "urn:li:asset:v1"
        assert entity["author"] == "urn:li:person:SUB1"

    def test_image_media_sets_image_category(self):
        from cqc_lem.utilities.linkedin.poster import share_on_linkedin
        restli = self._restli()
        with patch(f"{_P}.RestliClient", return_value=restli), \
             patch(f"{_P}.get_user_linked_sub_id", return_value="SUB1"), \
             patch(f"{_P}.get_user_access_token", return_value="tok"), \
             patch(f"{_P}.upload_media", return_value="urn:li:asset:i1"):
            share_on_linkedin(1, "caption", media_path="/a/pic.png")
        share = restli.create.call_args[1]["entity"][
            "specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == "IMAGE"

    def test_article_url_sets_article_category(self):
        from cqc_lem.utilities.linkedin.poster import share_on_linkedin
        restli = self._restli()
        with patch(f"{_P}.RestliClient", return_value=restli), \
             patch(f"{_P}.get_user_linked_sub_id", return_value="SUB1"), \
             patch(f"{_P}.get_user_access_token", return_value="tok"):
            share_on_linkedin(1, "caption", article_url="https://blog.example.com/p")
        share = restli.create.call_args[1]["entity"][
            "specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == "ARTICLE"
        assert share["media"][0]["originalUrl"] == "https://blog.example.com/p"

    def test_text_only_share_has_no_media(self):
        from cqc_lem.utilities.linkedin.poster import share_on_linkedin
        restli = self._restli()
        with patch(f"{_P}.RestliClient", return_value=restli), \
             patch(f"{_P}.get_user_linked_sub_id", return_value="SUB1"), \
             patch(f"{_P}.get_user_access_token", return_value="tok"):
            share_on_linkedin(1, "just text")
        share = restli.create.call_args[1]["entity"][
            "specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == "NONE"
        assert share["media"] == []


class TestDownloadMedia:
    def test_saves_content_with_extension(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import download_media
        resp = MagicMock(content=b"media-bytes")
        with patch(f"{_P}.requests.get", return_value=resp), \
             patch(f"{_P}.uuid.uuid4", return_value="fixed-uuid"), \
             patch("builtins.open", create=True) as mock_open:
            path = download_media("https://cdn/x/video.mp4")
        assert path == "/tmp/fixed-uuid.mp4"
        resp.raise_for_status.assert_called_once()
        mock_open.assert_called_once_with("/tmp/fixed-uuid.mp4", "wb")
