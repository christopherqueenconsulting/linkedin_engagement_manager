"""Curated publishing through /rest/posts: the reshare body shape, article cards, refusals.

Acceptance (#2260): the reshare body carries `reshareContext.parent` as a share/ugcPost URN and an
activity URN is refused before any request. Commentary is little-text escaped. No network.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cqc_lem.utilities.linkedin import reshare as rs

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.linkedin.reshare"
_AUTHOR = "urn:li:person:abc"


def _response(status=201, urn="urn:li:share:999", body=None):
    response = MagicMock(status_code=status, headers={"x-restli-id": urn} if urn else {})
    response.json.return_value = body or {}
    if status >= 400:
        err = requests.exceptions.HTTPError(f"{status}")
        err.response = MagicMock(status_code=status)
        response.raise_for_status.side_effect = err
    else:
        response.raise_for_status.return_value = None
    return response


@pytest.fixture
def creds():
    with patch(f"{_M}.get_user_linked_sub_id", return_value="abc"), \
         patch(f"{_M}.get_user_access_token", return_value="tok"):
        yield


class TestReshareBody:
    @pytest.mark.parametrize("parent", ["urn:li:share:123", "urn:li:ugcPost:456"])
    def test_shape(self, parent):
        body = rs.build_reshare_body(_AUTHOR, "Jane found (this) matters #AI", parent)
        assert body["reshareContext"] == {"parent": parent}
        assert body["author"] == _AUTHOR
        assert body["visibility"] == "PUBLIC"
        assert body["lifecycleState"] == "PUBLISHED"
        assert body["distribution"]["feedDistribution"] == "MAIN_FEED"
        assert body["commentary"] == r"Jane found \(this\) matters {hashtag|\#|AI}"
        assert "content" not in body

    @pytest.mark.parametrize("parent", ["urn:li:activity:123", "", None, "urn:li:share:x"])
    def test_an_activity_urn_is_refused(self, parent):
        with pytest.raises(ValueError):
            rs.build_reshare_body(_AUTHOR, "text", parent)


class TestArticleBody:
    def test_shape_with_thumbnail(self):
        body = rs.build_article_body(_AUTHOR, "Read it [now]", "https://a.b/x", "T" * 300,
                                     "D" * 400, "urn:li:image:1")
        article = body["content"]["article"]
        assert article == {"source": "https://a.b/x", "title": "T" * rs.ARTICLE_TITLE_MAX,
                           "description": "D" * rs.ARTICLE_DESCRIPTION_MAX,
                           "thumbnail": "urn:li:image:1"}
        assert body["commentary"] == r"Read it \[now\]"

    def test_no_thumbnail_no_description(self):
        article = rs.build_article_body(_AUTHOR, "x", "https://a.b", "")["content"]["article"]
        assert article == {"source": "https://a.b", "title": "https://a.b"}


class TestShareReshare:
    def test_posts_the_body_and_returns_the_urn(self, creds):
        with patch(f"{_M}.requests.post", return_value=_response()) as post:
            urn = rs.share_reshare_on_linkedin(1, "My take.", "urn:li:share:5")
        assert urn == "urn:li:share:999"
        assert post.call_args.args[0] == rs.POSTS_URL
        assert post.call_args.kwargs["json"]["reshareContext"]["parent"] == "urn:li:share:5"
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_a_4xx_is_refused_not_retried(self, creds):
        with patch(f"{_M}.requests.post", return_value=_response(403)) as post, \
             pytest.raises(rs.ReshareRefused):
            rs.share_reshare_on_linkedin(1, "x", "urn:li:share:5")
        assert post.call_count == 1

    def test_a_5xx_raises(self, creds):
        with patch(f"{_M}.requests.post", return_value=_response(503)), \
             pytest.raises(requests.exceptions.HTTPError):
            rs.share_reshare_on_linkedin(1, "x", "urn:li:share:5")

    def test_a_read_timeout_returns_none(self, creds):
        with patch(f"{_M}.requests.post", side_effect=requests.exceptions.ReadTimeout()):
            assert rs.share_reshare_on_linkedin(1, "x", "urn:li:share:5") is None

    def test_no_credentials_is_none(self):
        with patch(f"{_M}.get_user_linked_sub_id", return_value=None), \
             patch(f"{_M}.get_user_access_token", return_value=None), \
             patch(f"{_M}.requests.post") as post:
            assert rs.share_reshare_on_linkedin(1, "x", "urn:li:share:5") is None
        post.assert_not_called()

    def test_id_from_the_body_when_no_header(self, creds):
        with patch(f"{_M}.requests.post",
                   return_value=_response(urn=None, body={"id": "urn:li:share:7"})):
            assert rs.share_reshare_on_linkedin(1, "x", "urn:li:share:5") == "urn:li:share:7"


class TestCuratedImage:
    def test_escapes_and_uploads(self, creds):
        with patch(f"{_M}.upload_image_versioned", return_value="urn:li:image:1") as up, \
             patch(f"{_M}._create_image_post_versioned", return_value="urn:li:share:3") as create:
            assert rs.share_curated_image_on_linkedin(1, "a_b", "/tmp/x.png") == "urn:li:share:3"
        up.assert_called_once_with("tok", "abc", "/tmp/x.png")
        # Raw here: the builder is the ONE layer that escapes (#2261).
        assert create.call_args.args[2] == "a_b"

    def test_commentary_is_escaped_exactly_once_on_the_wire(self, creds):
        posted = MagicMock(headers={"x-restli-id": "urn:li:share:4"})
        posted.raise_for_status = MagicMock()
        with patch(f"{_M}.upload_image_versioned", return_value="urn:li:image:1"), \
             patch("requests.post", return_value=posted) as post:
            assert rs.share_curated_image_on_linkedin(1, "a_b (c) #AI", "/tmp/x.png") == \
                "urn:li:share:4"
        assert post.call_args.kwargs["json"]["commentary"] == r"a\_b \(c\) {hashtag|\#|AI}"

    def test_timeout_and_no_creds(self, creds):
        with patch(f"{_M}.upload_image_versioned", return_value="urn:li:image:1"), \
             patch(f"{_M}._create_image_post_versioned",
                   side_effect=requests.exceptions.ReadTimeout()):
            assert rs.share_curated_image_on_linkedin(1, "x", "/tmp/x.png") is None

    def test_no_creds(self):
        with patch(f"{_M}.get_user_linked_sub_id", return_value=None), \
             patch(f"{_M}.get_user_access_token", return_value="t"):
            assert rs.share_curated_image_on_linkedin(1, "x", "/tmp/x.png") is None


class TestThumbnail:
    def _get(self, kind="image/png", chunks=(b"png",)):
        response = MagicMock(headers={"content-type": kind})
        response.raise_for_status.return_value = None
        response.iter_content.return_value = list(chunks)
        return response

    def test_only_https_images_under_the_cap(self):
        assert rs.fetch_thumbnail(None) is None
        assert rs.fetch_thumbnail("http://a.b/x.png") is None
        with patch(f"{_M}.requests.get", return_value=self._get("text/html")):
            assert rs.fetch_thumbnail("https://a.b/x") is None
        big = [b"x" * (rs.THUMBNAIL_MAX_BYTES // 2 + 1)] * 2
        with patch(f"{_M}.requests.get", return_value=self._get(chunks=big)):
            assert rs.fetch_thumbnail("https://a.b/x.png") is None
        with patch(f"{_M}.requests.get", side_effect=requests.ConnectionError("x")):
            assert rs.fetch_thumbnail("https://a.b/x.png") is None

    def test_bytes_are_kept_unmodified(self):
        import os
        with patch(f"{_M}.requests.get", return_value=self._get(chunks=(b"ab", b"cd"))):
            path = rs.fetch_thumbnail("https://a.b/x.png")
        try:
            with open(path, "rb") as fh:
                assert fh.read() == b"abcd"
        finally:
            os.remove(path)


class TestShareArticle:
    def test_card_with_uploaded_og_image(self, creds, tmp_path):
        thumb = tmp_path / "og.png"
        thumb.write_bytes(b"png")
        with patch(f"{_M}.fetch_thumbnail", return_value=str(thumb)), \
             patch(f"{_M}.upload_image_versioned", return_value="urn:li:image:9"), \
             patch(f"{_M}.requests.post", return_value=_response()) as post:
            urn = rs.share_article_on_linkedin(1, "Take", "https://a.b/x", "Title", "Desc",
                                               "https://a.b/og.png")
        assert urn == "urn:li:share:999"
        assert post.call_args.kwargs["json"]["content"]["article"]["thumbnail"] == "urn:li:image:9"
        assert not thumb.exists()

    def test_a_failed_thumbnail_upload_still_posts_the_card(self, creds, tmp_path):
        thumb = tmp_path / "og.png"
        thumb.write_bytes(b"png")
        with patch(f"{_M}.fetch_thumbnail", return_value=str(thumb)), \
             patch(f"{_M}.upload_image_versioned", side_effect=RuntimeError("upload")), \
             patch(f"{_M}.requests.post", return_value=_response()) as post:
            assert rs.share_article_on_linkedin(1, "Take", "https://a.b/x", "T") == \
                "urn:li:share:999"
        assert "thumbnail" not in post.call_args.kwargs["json"]["content"]["article"]

    def test_timeout_and_no_creds(self, creds):
        with patch(f"{_M}.fetch_thumbnail", return_value=None), \
             patch(f"{_M}.requests.post", side_effect=requests.exceptions.ReadTimeout()):
            assert rs.share_article_on_linkedin(1, "Take", "https://a.b/x", "T") is None
        with patch(f"{_M}.get_user_linked_sub_id", return_value=None):
            assert rs.share_article_on_linkedin(1, "Take", "https://a.b/x", "T") is None
