"""Coverage tests for LinkedIn poster media upload and share routing.

Share routing is one contract with four inputs — what the caller attached decides the
share category and the media block — so it is a parametrized table (issue #1216).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

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
    @pytest.mark.parametrize("path,expected", [
        ("/a/pic.png", "IMAGE"),
        ("/a/clip.mp4", "VIDEO"),
    ], ids=["image", "video"])
    def test_maps_extension_to_type(self, path, expected):
        from cqc_lem.utilities.linkedin.poster import determine_media_type
        assert determine_media_type(path) == expected

    def test_unsupported_raises(self):
        from cqc_lem.utilities.linkedin.poster import determine_media_type
        with pytest.raises(ValueError, match="Unsupported media type"):
            determine_media_type("/a/file.txt")


# (case id, share kwargs, what upload_media returns, expected shareMediaCategory,
#  expected media block, expected upload_media args or None)
_SHARE_ROUTES = [
    ("video", {"media_path": "/a/clip.mp4"}, "urn:li:asset:v1", "VIDEO",
     [{"media": "urn:li:asset:v1"}], ("tok", "SUB1", "/a/clip.mp4", "VIDEO")),
    ("image", {"media_path": "/a/pic.png"}, "urn:li:asset:i1", "IMAGE",
     [{"media": "urn:li:asset:i1"}], ("tok", "SUB1", "/a/pic.png", "IMAGE")),
    ("article", {"article_url": "https://blog.example.com/p"}, None, "ARTICLE",
     [{"originalUrl": "https://blog.example.com/p"}], None),
    ("text_only", {}, None, "NONE", [], None),
]


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

    @pytest.mark.parametrize("case_id,kwargs,upload_return,category,media,upload_args",
                             _SHARE_ROUTES, ids=[c[0] for c in _SHARE_ROUTES])
    def test_attachment_decides_the_share_category(self, case_id, kwargs, upload_return,
                                                   category, media, upload_args):
        from cqc_lem.utilities.linkedin.poster import share_on_linkedin
        restli = self._restli()
        with patch(f"{_P}.RestliClient", return_value=restli), \
             patch(f"{_P}.get_user_linked_sub_id", return_value="SUB1"), \
             patch(f"{_P}.get_user_access_token", return_value="tok"), \
             patch(f"{_P}.upload_media", return_value=upload_return) as up:
            urn = share_on_linkedin(1, "caption", **kwargs)
        assert urn == "urn:li:share:1"
        entity = restli.create.call_args[1]["entity"]
        share = entity["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == category
        for expected_block, actual in zip(media, share["media"]):
            for key, value in expected_block.items():
                assert actual[key] == value
        assert len(share["media"]) == len(media)
        assert entity["author"] == "urn:li:person:SUB1"
        if upload_args:
            up.assert_called_once_with(*upload_args)
        else:
            up.assert_not_called()


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
