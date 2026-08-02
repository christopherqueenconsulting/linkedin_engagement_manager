"""Unit tests for the newsletter cover module (issue #893).

The gate is what stops an unusable image reaching a published article, so it is tested from both
sides: what it accepts, and every reason it rejects.
"""

import io
import os
from unittest.mock import patch

import pytest

from cqc_lem.utilities import newsletter_cover as nc

pytestmark = pytest.mark.unit


def _image_bytes(width: int = 1280, height: int = 720, fmt: str = "PNG") -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 40, 90)).save(buf, format=fmt)
    return buf.getvalue()


class TestInspectCoverBytes:
    def test_accepts_a_landscape_png(self):
        verdict = nc.inspect_cover_bytes(_image_bytes())
        assert verdict.ok and verdict.reason is None
        assert (verdict.width, verdict.height) == (1280, 720)
        assert verdict.extension == ".png"

    def test_accepts_jpeg_and_webp(self):
        for fmt, ext in (("JPEG", ".jpg"), ("WEBP", ".webp")):
            verdict = nc.inspect_cover_bytes(_image_bytes(fmt=fmt))
            assert verdict.ok, fmt
            assert verdict.extension == ext

    def test_rejects_empty_payload(self):
        verdict = nc.inspect_cover_bytes(b"")
        assert not verdict.ok and "No image data" in verdict.reason

    def test_rejects_oversized_payload_without_decoding(self):
        verdict = nc.inspect_cover_bytes(b"x" * (nc.MAX_COVER_BYTES + 1))
        assert not verdict.ok and "larger than" in verdict.reason

    def test_rejects_non_image_bytes(self):
        verdict = nc.inspect_cover_bytes(b"this is not an image at all")
        assert not verdict.ok and "not a readable image" in verdict.reason

    def test_rejects_too_small(self):
        verdict = nc.inspect_cover_bytes(_image_bytes(320, 180))
        assert not verdict.ok and "too small" in verdict.reason

    def test_rejects_portrait(self):
        verdict = nc.inspect_cover_bytes(_image_bytes(700, 1400))
        assert not verdict.ok and "landscape" in verdict.reason

    def test_rejects_extreme_panorama(self):
        verdict = nc.inspect_cover_bytes(_image_bytes(4000, 400))
        assert not verdict.ok and "landscape" in verdict.reason

    def test_rejects_disallowed_format(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (1280, 720)).save(buf, format="BMP")
        verdict = nc.inspect_cover_bytes(buf.getvalue())
        assert not verdict.ok and "PNG, JPG, or WEBP" in verdict.reason


class TestInspectCoverFile:
    def test_reads_a_file_from_disk(self, tmp_path):
        path = tmp_path / "cover.png"
        path.write_bytes(_image_bytes())
        assert nc.inspect_cover_file(str(path)).ok

    def test_missing_file_is_a_verdict_not_an_exception(self, tmp_path):
        verdict = nc.inspect_cover_file(str(tmp_path / "nope.png"))
        assert not verdict.ok and "could not be read" in verdict.reason


class TestSaveAndResolve:
    def test_save_then_resolve_round_trip(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)):
            rel = nc.save_cover_bytes(7, 42, _image_bytes())
            assert rel.startswith("images/newsletter_covers/7/ed42_")
            abs_path = nc.cover_abs_path(rel)
            assert abs_path and os.path.isfile(abs_path)

    def test_save_rejects_a_bad_image(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)):
            with pytest.raises(nc.CoverRejected):
                nc.save_cover_bytes(7, 42, b"nope")

    def test_two_saves_never_collide(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)):
            first = nc.save_cover_bytes(7, 42, _image_bytes())
            second = nc.save_cover_bytes(7, 42, _image_bytes())
        assert first != second

    def test_abs_path_is_none_for_missing_file(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)):
            assert nc.cover_abs_path("images/newsletter_covers/7/gone.png") is None

    def test_abs_path_refuses_to_escape_assets_dir(self, tmp_path):
        outside = tmp_path / "secret.png"
        outside.write_bytes(_image_bytes())
        root = tmp_path / "assets"
        root.mkdir()
        with patch.object(nc, "assets_dir", str(root)):
            assert nc.cover_abs_path("../secret.png") is None

    def test_abs_path_of_none_is_none(self):
        assert nc.cover_abs_path(None) is None

    def test_remove_deletes_the_file(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)):
            rel = nc.save_cover_bytes(7, 42, _image_bytes())
            assert nc.remove_cover_file(rel) is True
            assert nc.cover_abs_path(rel) is None

    def test_remove_of_missing_file_is_false_not_an_error(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)):
            assert nc.remove_cover_file("images/newsletter_covers/7/gone.png") is False

    def test_public_url_carries_the_relative_path(self):
        url = nc.cover_public_url("images/newsletter_covers/7/ed42_ab.png")
        assert url.endswith("/api/assets?file_name=images/newsletter_covers/7/ed42_ab.png")

    def test_public_url_of_none_is_none(self):
        assert nc.cover_public_url(None) is None


class TestBuildCoverPrompt:
    def test_frames_the_edition_and_the_cover_ratio(self):
        with patch("cqc_lem.utilities.ai.ai_helper.get_flux_image_prompt_from_ai",
                   return_value="a prompt") as writer:
            assert nc.build_cover_prompt("Title", "Subtitle", "Body text") == "a prompt"
        content, kwargs = writer.call_args[0][0], writer.call_args[1]
        assert "Title" in content and "Subtitle" in content and "Body text" in content
        assert kwargs["ratio"] == nc.COVER_IMAGE_RATIO

    def test_truncates_a_long_body(self):
        with patch("cqc_lem.utilities.ai.ai_helper.get_flux_image_prompt_from_ai",
                   return_value="p") as writer:
            nc.build_cover_prompt("T", None, "x" * 5000)
        assert len(writer.call_args[0][0]) < 2000


class TestGenerateCoverForEdition:
    def _generated(self, tmp_path, name="gen.png", data=None):
        path = tmp_path / name
        path.write_bytes(data if data is not None else _image_bytes())
        return str(path)

    def test_happy_path_copies_into_the_users_cover_dir(self, tmp_path):
        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()
        with patch.object(nc, "assets_dir", str(assets)), \
             patch("cqc_lem.utilities.ai.ai_helper.get_flux_image_prompt_from_ai", return_value="p"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value=generated) as gen:
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert reason is None
        assert rel.startswith("images/newsletter_covers/3/ed9_")
        with patch.object(nc, "assets_dir", str(assets)):
            assert nc.cover_abs_path(rel) is not None
        # The cover is a brand asset, not a scene the author is in — the avatar path stays out.
        assert gen.call_args[1]["depicts_person"] is False
        assert gen.call_args[1]["ratio"] == nc.COVER_IMAGE_RATIO
        assert os.path.isfile(generated), "the source render must not be moved out from under callers"

    def test_prompt_failure_returns_a_reason_not_an_exception(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.ai.ai_helper.get_flux_image_prompt_from_ai",
                   side_effect=RuntimeError("llm down")):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert rel is None and "prompt" in reason

    def test_generation_failure_returns_a_reason(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.ai.ai_helper.get_flux_image_prompt_from_ai", return_value="p"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   side_effect=RuntimeError("replicate down")):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert rel is None and reason == "Image generation failed"

    def test_empty_generation_result_returns_a_reason(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.ai.ai_helper.get_flux_image_prompt_from_ai", return_value="p"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image", return_value=None):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert rel is None and "nothing" in reason

    def test_a_generation_that_fails_the_gate_is_never_stored(self, tmp_path):
        # A truncated/undersized render must not reach an edition just because generation "worked".
        generated = self._generated(tmp_path, data=_image_bytes(200, 120))
        assets = tmp_path / "assets"
        assets.mkdir()
        with patch.object(nc, "assets_dir", str(assets)), \
             patch("cqc_lem.utilities.ai.ai_helper.get_flux_image_prompt_from_ai", return_value="p"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image", return_value=generated):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert rel is None and "too small" in reason
        assert not (assets / "images").exists()
