"""The animated-loop publish path (docs/animated-posts.md).

A GIF goes through the versioned Images API; every failure before a post exists falls back to the
still, and the one ambiguous failure (a /rest/posts read timeout) never retries, so a fallback can
never publish the post twice.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

pytestmark = pytest.mark.unit

_P = "cqc_lem.utilities.linkedin.poster"
_STILL = "https://api.example.com/api/assets?file_name=images/posts/10/a.png"


def _gif(tmp_path, frames=5):
    from PIL import Image
    path = tmp_path / "loop.gif"
    images = [Image.new("RGB", (8, 8), (i % 256, (i // 256) * 50, 0)) for i in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:], loop=0)
    return str(path)


def _init_response():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"value": {"uploadUrl": "https://up.example/img",
                                            "image": "urn:li:image:G1"}}
    return response


@pytest.fixture
def creds():
    with patch(f"{_P}.get_user_linked_sub_id", return_value="sub-1"), \
         patch(f"{_P}.get_user_access_token", return_value="tok"):
        yield


class TestUploadImageVersioned:
    def test_initializes_on_rest_images_and_puts_the_bytes(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import upload_image_versioned
        gif = _gif(tmp_path)
        with patch("requests.post", return_value=_init_response()) as post, \
             patch("requests.put", return_value=MagicMock(status_code=201)) as put:
            assert upload_image_versioned("tok", "sub-1", gif) == "urn:li:image:G1"
        assert post.call_args.args[0].startswith("https://api.linkedin.com/rest/images")
        assert post.call_args.kwargs["json"] == {
            "initializeUploadRequest": {"owner": "urn:li:person:sub-1"}}
        assert put.call_args.kwargs["data"].startswith(b"GIF8")

    def test_a_failed_put_raises(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import upload_image_versioned
        with patch("requests.post", return_value=_init_response()), \
             patch("requests.put", return_value=MagicMock(status_code=500)):
            with pytest.raises(RuntimeError):
                upload_image_versioned("tok", "sub-1", _gif(tmp_path))


class TestShareAnimated:
    def test_publishes_the_gif_through_rest_posts(self, tmp_path, creds):
        from cqc_lem.utilities.linkedin.poster import share_animated_image_on_linkedin
        posted = MagicMock(headers={"x-restli-id": "urn:li:share:9"})
        with patch(f"{_P}.upload_image_versioned", return_value="urn:li:image:G1"), \
             patch("requests.post", return_value=posted) as post, \
             patch(f"{_P}.share_on_linkedin") as still:
            assert share_animated_image_on_linkedin(1, "body", _gif(tmp_path), _STILL) == \
                "urn:li:share:9"
        still.assert_not_called()
        body = post.call_args.kwargs["json"]
        assert body["content"] == {"media": {"id": "urn:li:image:G1"}}
        assert body["commentary"] == "body"

    def test_commentary_is_little_text_escaped(self, tmp_path, creds):
        from cqc_lem.utilities.linkedin.poster import share_animated_image_on_linkedin
        posted = MagicMock(headers={"x-restli-id": "urn:li:share:9"})
        with patch(f"{_P}.upload_image_versioned", return_value="urn:li:image:G1"), \
             patch("requests.post", return_value=posted) as post, \
             patch(f"{_P}.share_on_linkedin"):
            share_animated_image_on_linkedin(1, "Loop [v2] <3 ~ #GIF", _gif(tmp_path), _STILL)
        assert post.call_args.kwargs["json"]["commentary"] == \
            r"Loop \[v2\] \<3 \~ {hashtag|\#|GIF}"

    def test_over_250_frames_never_uploads_and_ships_the_still(self, tmp_path, creds):
        from cqc_lem.utilities.linkedin.poster import share_animated_image_on_linkedin
        with patch(f"{_P}.upload_image_versioned") as upload, \
             patch(f"{_P}.share_on_linkedin", return_value="urn:li:ugcPost:1") as still:
            urn = share_animated_image_on_linkedin(1, "body", _gif(tmp_path, frames=251), _STILL)
        assert urn == "urn:li:ugcPost:1"
        upload.assert_not_called()
        still.assert_called_once_with(1, "body", _STILL)

    def test_upload_failure_ships_the_still(self, tmp_path, creds):
        from cqc_lem.utilities.linkedin.poster import share_animated_image_on_linkedin
        with patch(f"{_P}.upload_image_versioned", side_effect=RuntimeError("426")), \
             patch(f"{_P}.share_on_linkedin", return_value="urn:li:ugcPost:1") as still:
            assert share_animated_image_on_linkedin(1, "body", _gif(tmp_path), _STILL) == \
                "urn:li:ugcPost:1"
        still.assert_called_once_with(1, "body", _STILL)

    def test_an_answered_post_error_ships_the_still(self, tmp_path, creds):
        from cqc_lem.utilities.linkedin.poster import share_animated_image_on_linkedin
        rejected = MagicMock()
        rejected.raise_for_status.side_effect = requests.HTTPError("400")
        with patch(f"{_P}.upload_image_versioned", return_value="urn:li:image:G1"), \
             patch("requests.post", return_value=rejected), \
             patch(f"{_P}.share_on_linkedin", return_value="urn:li:ugcPost:1") as still:
            assert share_animated_image_on_linkedin(1, "body", _gif(tmp_path), _STILL) == \
                "urn:li:ugcPost:1"
        still.assert_called_once()

    def test_a_read_timeout_never_double_posts(self, tmp_path, creds):
        from cqc_lem.utilities.linkedin.poster import share_animated_image_on_linkedin
        with patch(f"{_P}.upload_image_versioned", return_value="urn:li:image:G1"), \
             patch("requests.post", side_effect=requests.exceptions.ReadTimeout("slow")), \
             patch(f"{_P}.share_on_linkedin") as still:
            assert share_animated_image_on_linkedin(1, "body", _gif(tmp_path), _STILL) is None
        still.assert_not_called()

    def test_a_2xx_without_an_id_never_double_posts(self, tmp_path, creds):
        from cqc_lem.utilities.linkedin.poster import share_animated_image_on_linkedin
        posted = MagicMock(headers={})
        posted.json.return_value = {}
        with patch(f"{_P}.upload_image_versioned", return_value="urn:li:image:G1"), \
             patch("requests.post", return_value=posted), \
             patch(f"{_P}.share_on_linkedin") as still:
            assert share_animated_image_on_linkedin(1, "body", _gif(tmp_path), _STILL) is None
        still.assert_not_called()

    def test_no_credentials_defers_to_the_still_path(self, tmp_path):
        from cqc_lem.utilities.linkedin.poster import share_animated_image_on_linkedin
        with patch(f"{_P}.get_user_linked_sub_id", return_value=None), \
             patch(f"{_P}.get_user_access_token", return_value=None), \
             patch(f"{_P}.share_on_linkedin", return_value=None) as still:
            assert share_animated_image_on_linkedin(1, "body", _gif(tmp_path), None) is None
        still.assert_called_once_with(1, "body")
