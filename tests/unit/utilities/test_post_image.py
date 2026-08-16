"""Post images (issue #1030): the deterministic gate, where bytes land, and what a stored URL
is allowed to resolve to.

The gate is the half that runs before anything is written — an image LinkedIn would refuse must be
a 400 at upload time, not a broken share at publish time. The URL helpers are the half that runs
after: `image_url` is a full public URL on a row, so "which file is this" has to be answered
without letting a hand-edited value point at an arbitrary path.
"""

import io
import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_PI = "cqc_lem.utilities.post_image"


def _image_bytes(width: int = 800, height: int = 800, fmt: str = "PNG") -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (20, 60, 120)).save(buf, format=fmt)
    return buf.getvalue()


class TestInspectPostImageBytes:
    def test_accepts_a_normal_png(self):
        from cqc_lem.utilities.post_image import inspect_post_image_bytes
        verdict = inspect_post_image_bytes(_image_bytes())
        assert verdict.ok and verdict.extension == ".png"

    def test_accepts_a_jpeg(self):
        from cqc_lem.utilities.post_image import inspect_post_image_bytes
        verdict = inspect_post_image_bytes(_image_bytes(fmt="JPEG"))
        assert verdict.ok and verdict.extension == ".jpg"

    def test_rejects_empty_input(self):
        from cqc_lem.utilities.post_image import inspect_post_image_bytes
        verdict = inspect_post_image_bytes(b"")
        assert not verdict.ok and verdict.reason

    def test_rejects_something_that_is_not_an_image(self):
        from cqc_lem.utilities.post_image import inspect_post_image_bytes
        verdict = inspect_post_image_bytes(b"not an image at all")
        assert not verdict.ok and "readable image" in verdict.reason

    def test_rejects_a_format_linkedin_will_not_take(self):
        from cqc_lem.utilities.post_image import inspect_post_image_bytes
        verdict = inspect_post_image_bytes(_image_bytes(fmt="GIF"))
        assert not verdict.ok and "PNG or JPG" in verdict.reason

    def test_rejects_a_thumbnail_sized_image(self):
        from cqc_lem.utilities.post_image import inspect_post_image_bytes
        verdict = inspect_post_image_bytes(_image_bytes(120, 120))
        assert not verdict.ok and "too small" in verdict.reason

    def test_rejects_bytes_over_the_cap(self):
        from cqc_lem.utilities.post_image import MAX_POST_IMAGE_BYTES, inspect_post_image_bytes
        verdict = inspect_post_image_bytes(b"x" * (MAX_POST_IMAGE_BYTES + 1))
        assert not verdict.ok and "larger than" in verdict.reason


class TestSaveAndRemove:
    def test_an_attached_upload_lands_in_the_post_dir(self, tmp_path):
        from cqc_lem.utilities.post_image import save_post_image_bytes
        with patch(f"{_PI}.assets_dir", str(tmp_path)):
            url = save_post_image_bytes(7, _image_bytes(), post_id=42)
        assert "file_name=images/posts/42/" in url
        assert os.listdir(tmp_path / "images" / "posts" / "42")

    def test_a_compose_upload_lands_under_the_authors_preview_dir(self, tmp_path):
        from cqc_lem.utilities.post_image import save_post_image_bytes
        with patch(f"{_PI}.assets_dir", str(tmp_path)):
            url = save_post_image_bytes(7, _image_bytes())
        assert "file_name=images/post_previews/7/" in url
        assert os.listdir(tmp_path / "images" / "post_previews" / "7")

    def test_stored_names_are_unpredictable(self, tmp_path):
        """/api/assets is public, so a guessable name would expose an unpublished image."""
        from cqc_lem.utilities.post_image import save_post_image_bytes
        with patch(f"{_PI}.assets_dir", str(tmp_path)):
            first = save_post_image_bytes(7, _image_bytes(), post_id=42)
            second = save_post_image_bytes(7, _image_bytes(), post_id=42)
        assert first != second

    def test_a_rejected_upload_writes_nothing(self, tmp_path):
        from cqc_lem.utilities.post_image import PostImageRejected, save_post_image_bytes
        with patch(f"{_PI}.assets_dir", str(tmp_path)):
            with pytest.raises(PostImageRejected):
                save_post_image_bytes(7, b"nope", post_id=42)
        assert not (tmp_path / "images").exists()

    def test_remove_deletes_the_file_behind_a_stored_url(self, tmp_path):
        from cqc_lem.utilities.post_image import post_image_abs_path, remove_post_image_file, save_post_image_bytes
        with patch(f"{_PI}.assets_dir", str(tmp_path)):
            url = save_post_image_bytes(7, _image_bytes(), post_id=42)
            assert post_image_abs_path(url)
            assert remove_post_image_file(url) is True
            assert post_image_abs_path(url) is None

    def test_remove_is_a_no_op_for_a_url_we_never_issued(self, tmp_path):
        from cqc_lem.utilities.post_image import remove_post_image_file
        with patch(f"{_PI}.assets_dir", str(tmp_path)):
            assert remove_post_image_file("https://example.com/someone-elses.png") is False
            assert remove_post_image_file(None) is False


class TestUrlResolution:
    def test_a_traversal_path_never_resolves_outside_assets(self, tmp_path):
        """The value comes from our DB, but a hand-edited row must not hand a delete — or a
        publish — an arbitrary file.
        """
        from cqc_lem.utilities.post_image import post_image_abs_path
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"png")
        assets = tmp_path / "assets"
        assets.mkdir()
        url = "https://api.test/api/assets?file_name=images/posts/../../secret.png"
        with patch(f"{_PI}.assets_dir", str(assets)):
            assert post_image_abs_path(url) is None

    def test_a_foreign_url_has_no_path_of_ours(self):
        from cqc_lem.utilities.post_image import post_image_relative_path
        assert post_image_relative_path("https://cdn.example.com/x.png") is None
        assert post_image_relative_path(
            "https://api.test/api/assets?file_name=images/newsletter_covers/7/a.png") is None
        assert post_image_relative_path("") is None

    def test_ownership_accepts_only_this_users_preview(self):
        from cqc_lem.utilities.post_image import owns_post_image_url
        mine = "https://api.test/api/assets?file_name=images/post_previews/7/img_a.png"
        theirs = "https://api.test/api/assets?file_name=images/post_previews/8/img_a.png"
        attached = "https://api.test/api/assets?file_name=images/posts/42/img_a.png"
        assert owns_post_image_url(7, mine) is True
        assert owns_post_image_url(7, theirs) is False
        # An already-attached image is not a compose-time preview — it belongs to a row that was
        # authorised elsewhere, so it is never accepted as a schedule-time attachment.
        assert owns_post_image_url(7, attached) is False
        assert owns_post_image_url(7, "https://evil.test/x.png") is False
        assert owns_post_image_url(7, None) is False


class TestGenerateImageForPost:
    def _generate(self, tmp_path, *, avatar=None, rendered="render.png", render_raises=False,
                  brief_raises=False, text="Post text"):
        from cqc_lem.utilities.ai.image_brief import ImageBrief
        from cqc_lem.utilities.post_image import generate_image_for_post

        rendered_path = None
        if rendered:
            rendered_path = str(tmp_path / rendered)
            with open(rendered_path, "wb") as fh:
                fh.write(b"png")
        assets = tmp_path / "assets"
        assets.mkdir(exist_ok=True)
        brief = ImageBrief(prompt="a prompt", ratio="1:1", surface="post_image",
                           style_preset="post_image", focal_concept="the idea")

        # Both bindings: the store reads this module's, and the brief receipt written beside it
        # resolves the volume through the package (issue #1377).
        with patch(f"{_PI}.assets_dir", str(assets)), \
             patch("cqc_lem.assets_dir", str(assets)), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=avatar), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   side_effect=RuntimeError("brief down") if brief_raises else None,
                   return_value=brief), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   side_effect=RuntimeError("render down") if render_raises else None,
                   return_value=rendered_path) as render, \
             patch("cqc_lem.utilities.ai.image_gen.render_avatar_image_gated",
                   return_value=rendered_path) as lora:
            return generate_image_for_post(9, text, post_id=42), render, lora, assets

    def test_stores_the_render_and_returns_its_public_url(self, tmp_path):
        (url, reason), render, lora, assets = self._generate(tmp_path)
        assert reason is None
        assert "file_name=images/posts/42/" in url
        assert os.listdir(assets / "images" / "posts" / "42")
        assert render.call_args[1]["surface"] == "post_image"
        lora.assert_not_called()

    def test_a_compose_render_lands_in_the_preview_dir(self, tmp_path):
        from cqc_lem.utilities.ai.image_brief import ImageBrief
        from cqc_lem.utilities.post_image import generate_image_for_post
        rendered_path = str(tmp_path / "render.png")
        with open(rendered_path, "wb") as fh:
            fh.write(b"png")
        assets = tmp_path / "assets"
        assets.mkdir()
        brief = ImageBrief(prompt="a prompt", ratio="1:1", surface="post_image",
                           style_preset="post_image", focal_concept="the idea")
        with patch(f"{_PI}.assets_dir", str(assets)), \
             patch("cqc_lem.assets_dir", str(assets)), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=brief), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=rendered_path):
            url, reason = generate_image_for_post(9, "Post text")
        assert reason is None and "file_name=images/post_previews/9/" in url

    def test_an_avatar_goes_through_the_guardrailed_lora_path(self, tmp_path):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        (url, reason), render, lora, _ = self._generate(tmp_path, avatar=avatar)
        assert url and reason is None
        lora.assert_called_once()
        assert lora.call_args[1]["avatar"] == avatar
        render.assert_not_called()

    def test_empty_text_is_refused_before_any_spend(self, tmp_path):
        (url, reason), render, lora, _ = self._generate(tmp_path, text="   ")
        assert url is None and "content first" in reason
        render.assert_not_called()
        lora.assert_not_called()

    def test_a_failed_render_never_raises(self, tmp_path):
        (url, reason), _, _, _ = self._generate(tmp_path, render_raises=True)
        assert url is None and reason == "Image generation failed"

    def test_a_failed_prompt_never_raises(self, tmp_path):
        (url, reason), render, _, _ = self._generate(tmp_path, brief_raises=True)
        assert url is None and reason == "Could not write an image prompt"
        render.assert_not_called()

    def test_a_render_that_produced_nothing_is_reported(self, tmp_path):
        (url, reason), _, _, _ = self._generate(tmp_path, rendered=None)
        assert url is None and reason == "Image generation returned nothing"

    def test_the_brief_is_recoverable_from_the_stored_url(self, tmp_path):
        """Issue #1377 (P4): `focal_concept` is what rubric row R6 grades a render against.

        Before this it existed only inside the call, so every shipped image was permanently
        unauditable — the last audit had to infer intent from the post's topic and say so.
        """
        from cqc_lem.utilities.media_provenance import read_brief_receipt
        (url, _reason), _render, _lora, assets = self._generate(tmp_path)
        with patch("cqc_lem.assets_dir", str(assets)):
            recovered = read_brief_receipt(url)
        assert recovered["focal_concept"] == "the idea"
        assert recovered["surface"] == "post_image" and recovered["post_id"] == 42

    def test_the_gate_verdict_rides_along_with_it(self, tmp_path):
        """The gate verdict is recorded with the brief.

        It exists only inside the render call, so recording it is what lets an audit tell a render
        the gate passed from one it never looked at.
        """
        from cqc_lem.utilities.ai.image_brief import ImageBrief
        from cqc_lem.utilities.media_provenance import read_brief_receipt
        from cqc_lem.utilities.post_image import generate_image_for_post
        rendered_path = str(tmp_path / "render.png")
        with open(rendered_path, "wb") as fh:
            fh.write(b"png")
        assets = tmp_path / "assets"
        assets.mkdir(exist_ok=True)
        brief = ImageBrief(prompt="a prompt", ratio="1:1", surface="post_image",
                           style_preset="post_image", focal_concept="the idea")

        def _render(*_args, render_info=None, **_kwargs):
            render_info["gate_verdict"] = "rejected"
            return rendered_path

        with patch(f"{_PI}.assets_dir", str(assets)), \
             patch("cqc_lem.assets_dir", str(assets)), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=brief), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", side_effect=_render):
            url, _reason = generate_image_for_post(9, "Post text", post_id=42)
            assert read_brief_receipt(url)["gate_verdict"] == "rejected"

    def test_an_uploaded_image_gets_no_receipt(self, tmp_path):
        """A receipt is a RECORD, never a default — there is no brief behind the author's own file."""
        from cqc_lem.utilities.media_provenance import read_brief_receipt
        from cqc_lem.utilities.post_image import save_post_image_bytes
        assets = tmp_path / "assets"
        assets.mkdir(exist_ok=True)
        with patch(f"{_PI}.assets_dir", str(assets)), patch("cqc_lem.assets_dir", str(assets)):
            url = save_post_image_bytes(9, _image_bytes(), post_id=42)
            assert read_brief_receipt(url) is None


class TestManualGenerationCap:
    def _claim(self, counts, limit=3):
        from cqc_lem.utilities.post_image import claim_manual_generation

        class _Redis:
            def __init__(self):
                self.expired = []

            def incr(self, key):
                return counts.pop(0)

            def expire(self, key, ttl):
                self.expired.append(ttl)

        fake = _Redis()
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client", return_value=fake), \
             patch("cqc_lem.utilities.env_constants.POST_IMAGE_GENERATE_MAX_PER_HOUR", limit):
            return [claim_manual_generation(9) for _ in range(len(counts))], fake

    def test_allows_up_to_the_cap_then_refuses(self):
        results, fake = self._claim([1, 2, 3, 4])
        assert results == [True, True, True, False]
        # TTL set only on the first increment, so the window is fixed rather than sliding.
        assert fake.expired == [3600]

    def test_fails_open_when_redis_is_gone(self):
        from cqc_lem.utilities.post_image import claim_manual_generation
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client", return_value=None):
            assert claim_manual_generation(9) is True

    def test_fails_open_when_redis_errors(self):
        from cqc_lem.utilities.post_image import claim_manual_generation

        class _Broken:
            def incr(self, key):
                raise RuntimeError("broker restarting")

        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   return_value=_Broken()):
            assert claim_manual_generation(9) is True
