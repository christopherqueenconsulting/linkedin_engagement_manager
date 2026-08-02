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


def _brief(prompt="a prompt", focal="a focal concept"):
    from cqc_lem.utilities.ai.image_brief import ImageBrief
    return ImageBrief(prompt=prompt, ratio=nc.COVER_IMAGE_RATIO, surface="newsletter",
                      style_preset="newsletter", focal_concept=focal)


class TestBuildCoverPrompt:
    def test_frames_the_edition_and_the_cover_ratio(self):
        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as writer:
            assert nc.build_cover_prompt("Title", "Subtitle", "Body text") == "a prompt"
        content, kwargs = writer.call_args[0][0], writer.call_args[1]
        assert "Title" in content and "Subtitle" in content and "Body text" in content
        assert kwargs["ratio"] == nc.COVER_IMAGE_RATIO
        assert kwargs["surface"] == "newsletter"

    def test_truncates_a_long_body(self):
        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as writer:
            nc.build_cover_prompt("T", None, "x" * 5000)
        assert len(writer.call_args[0][0]) < 2000


class TestAvatarRelevanceClassifier:
    def _classify(self, payload):
        from types import SimpleNamespace
        resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.return_value = resp
            return nc.classify_avatar_relevance("T", "S", "B")

    def test_relevant_edition_says_yes(self):
        assert self._classify('{"avatar_relevant": true}') is True

    def test_irrelevant_edition_says_no(self):
        assert self._classify('{"avatar_relevant": false}') is False

    def test_unparseable_answer_fails_closed(self):
        assert self._classify('maybe?') is False

    def test_llm_outage_fails_closed(self):
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.side_effect = RuntimeError("down")
            assert nc.classify_avatar_relevance("T", "S", "B") is False

    def test_empty_edition_never_calls_the_llm(self):
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            assert nc.classify_avatar_relevance(None, None, None) is False
        mock_client.chat.completions.create.assert_not_called()


_USABLE_AVATAR = {"status": "succeeded", "model_ref": "owner/lora:v1", "trigger_word": "TOK",
                  "approval_status": "approved", "gender_presentation": "man", "age_band": "40s"}


class TestResolveCoverAvatar:
    def test_explicit_without_never_renders_the_avatar(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for") as resolve:
            assert nc._resolve_cover_avatar(3, False, "T", "S", "B") is None
        resolve.assert_not_called()

    def test_auto_needs_guardrails_AND_classifier(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=_USABLE_AVATAR) as resolve, \
             patch.object(nc, "classify_avatar_relevance", return_value=True):
            assert nc._resolve_cover_avatar(3, None, "T", "S", "B") == _USABLE_AVATAR
        assert resolve.call_args[1]["surface"] == "newsletter"

    def test_auto_with_irrelevant_edition_declines(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=_USABLE_AVATAR), \
             patch.object(nc, "classify_avatar_relevance", return_value=False):
            assert nc._resolve_cover_avatar(3, None, "T", "S", "B") is None

    def test_auto_with_guardrail_decline_never_asks_the_classifier(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch.object(nc, "classify_avatar_relevance") as classify:
            assert nc._resolve_cover_avatar(3, None, "T", "S", "B") is None
        classify.assert_not_called()

    def test_explicit_with_skips_opt_in_but_not_disabled(self):
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_disabled": False}), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=_USABLE_AVATAR):
            assert nc._resolve_cover_avatar(3, True, "T", "S", "B") == _USABLE_AVATAR
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_disabled": True}), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=_USABLE_AVATAR):
            assert nc._resolve_cover_avatar(3, True, "T", "S", "B") is None

    def test_explicit_with_still_requires_an_approved_avatar(self):
        unapproved = dict(_USABLE_AVATAR, approval_status="pending")
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_disabled": False}), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=unapproved):
            assert nc._resolve_cover_avatar(3, True, "T", "S", "B") is None


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
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   return_value=generated) as gen:
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert reason is None
        assert rel.startswith("images/newsletter_covers/3/ed9_")
        with patch.object(nc, "assets_dir", str(assets)):
            assert nc.cover_abs_path(rel) is not None
        # No avatar resolved -> the vision-gated base renderer, at the cover ratio, on the
        # newsletter surface (so the gate is enforced, not advisory).
        assert gen.call_args[1]["ratio"] == nc.COVER_IMAGE_RATIO
        assert gen.call_args[1]["surface"] == "newsletter"
        assert gen.call_args[1]["focal_concept"] == "a focal concept"
        assert os.path.isfile(generated), "the source render must not be moved out from under callers"

    def test_prompt_failure_returns_a_reason_not_an_exception(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   side_effect=RuntimeError("llm down")):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert rel is None and "prompt" in reason

    def test_generation_failure_returns_a_reason(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   side_effect=RuntimeError("replicate down")):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert rel is None and reason == "Image generation failed"

    def test_empty_generation_result_returns_a_reason(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=None):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert rel is None and "nothing" in reason

    def test_a_generation_that_fails_the_gate_is_never_stored(self, tmp_path):
        # A truncated/undersized render must not reach an edition just because generation "worked".
        generated = self._generated(tmp_path, data=_image_bytes(200, 120))
        assets = tmp_path / "assets"
        assets.mkdir()
        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=generated):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert rel is None and "too small" in reason
        assert not (assets / "images").exists()

    def test_avatar_cover_renders_through_the_lora_with_provenance(self, tmp_path):
        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()
        from cqc_lem.utilities.ai.image_gen import QualityVerdict
        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=_USABLE_AVATAR), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as brief, \
             patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=(generated, True)) as lora, \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media") as record, \
             patch("cqc_lem.utilities.ai.image_gen.inspect_render_quality",
                   return_value=QualityVerdict(acceptable=True)):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert reason is None and rel is not None
        # Avatar resolved BEFORE the brief so the subject clause leads the prompt (#744)...
        assert brief.call_args[1]["avatar"] == _USABLE_AVATAR
        # ...the LoRA prompt carries the trigger word + declared clause, at the cover ratio...
        assert lora.call_args[0][0].startswith("TOK, a man in his 40s")
        assert lora.call_args[1]["ratio"] == nc.COVER_IMAGE_RATIO
        # ...and a rendered likeness is C2PA-signed.
        record.assert_called_once_with(generated, None, 3)

    def test_avatar_gate_rejection_re_renders_once(self, tmp_path):
        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()
        from cqc_lem.utilities.ai.image_gen import QualityVerdict
        verdicts = [QualityVerdict(acceptable=False, issues=["distorted face"]),
                    QualityVerdict(acceptable=True)]
        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=_USABLE_AVATAR), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()), \
             patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=(generated, True)) as lora, \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch("cqc_lem.utilities.ai.image_gen.inspect_render_quality",
                   side_effect=verdicts):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert reason is None and rel is not None
        assert lora.call_count == 2
        assert "distorted face" in lora.call_args[0][0]
