"""Unit tests for the newsletter cover module (issue #893).

The gate is what stops an unusable image reaching a published article, so it is tested from both
sides: what it accepts, and every reason it rejects.
"""

import io
import json
import os
from unittest.mock import patch

import pytest

from cqc_lem.utilities import newsletter_cover as nc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_stage_one_or_brand_reads():
    """Stub Stage 1 (an LLM call) and the brand kit (a DB read) to their neutral answers.

    Both are wired into every image surface since issue #2241; these tests pin other behaviour.
    Tests that care patch them again on top.
    """
    with patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image", return_value=None), \
         patch("cqc_lem.utilities.brand_kit.brand_clause_for_user", return_value=""):
        yield


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

    def test_remove_also_deletes_the_brief_receipt(self, tmp_path):
        """Issue #2010: a regenerated cover must not orphan its `.brief.json` forever."""
        with patch.object(nc, "assets_dir", str(tmp_path)):
            rel = nc.save_cover_bytes(7, 42, _image_bytes())
            abs_path = nc.cover_abs_path(rel)
            receipt_path = os.path.splitext(abs_path)[0] + ".brief.json"
            with open(receipt_path, "w", encoding="utf-8") as fh:
                json.dump({"focal_concept": "a valve"}, fh)

            assert nc.remove_cover_file(rel) is True

            assert not os.path.isfile(abs_path)
            assert not os.path.isfile(receipt_path)

    def test_remove_without_a_receipt_still_deletes_the_cover(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)):
            rel = nc.save_cover_bytes(7, 42, _image_bytes())
            assert nc.remove_cover_file(rel) is True
            assert nc.cover_abs_path(rel) is None

    def test_public_url_carries_the_relative_path(self):
        url = nc.cover_public_url("images/newsletter_covers/7/ed42_ab.png")
        assert url.endswith("/api/assets?file_name=images/newsletter_covers/7/ed42_ab.png")

    def test_public_url_of_none_is_none(self):
        assert nc.cover_public_url(None) is None


def _brief(prompt="a prompt", focal="a focal concept", fallback=False):
    from cqc_lem.utilities.ai.image_brief import ImageBrief
    return ImageBrief(prompt=prompt, ratio=nc.COVER_IMAGE_RATIO, surface="newsletter",
                      style_preset="newsletter", focal_concept=focal, fallback=fallback)


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

    def test_guidance_rides_through_as_extra_direction(self, tmp_path):
        # Issue #1890: the cover's own guidance is threaded into the existing extra_direction
        # param on the shared image-brief builder — never a per-surface prompt helper.
        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()
        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as brief, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=generated):
            nc.generate_cover_for_edition(3, 9, "T", "S", "B", guidance="brighter colors, no text")
        assert brief.call_args[1]["extra_direction"] == "brighter colors, no text"

    def test_no_guidance_passes_none_through(self, tmp_path):
        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()
        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as brief, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=generated):
            nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert brief.call_args[1]["extra_direction"] is None

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
        # The re-render says what the cover must SHOW, not what was wrong with the last one:
        # this path is FLUX, which renders whatever a prompt names (issue #1141).
        retry = lora.call_args[0][0]
        assert "naturally proportioned subject" in retry
        assert "distorted" not in retry


class TestGenerateCoverForEditionReceipt:
    """Issue #1992: every stored cover gets a `.brief.json` receipt beside it.

    Real brief AND fallback alike — the only way to tell these apart after the fact (Box 5).
    """

    def _generated(self, tmp_path, name="gen.png", data=None):
        path = tmp_path / name
        path.write_bytes(data if data is not None else _image_bytes())
        return str(path)

    def test_writes_a_brief_receipt_beside_the_stored_cover(self, tmp_path):
        from cqc_lem.utilities.media_provenance import read_brief_receipt

        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()
        with patch.object(nc, "assets_dir", str(assets)), \
             patch("cqc_lem.assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief(focal="a switch")), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   return_value=generated):
            rel, reason = nc.generate_cover_for_edition(
                3, 9, "T", "S", "B", edition_format="case_study", hook_style="personal_story")
            assert reason is None
            receipt = read_brief_receipt(nc.cover_public_url(rel))
        assert receipt is not None
        assert receipt["focal_concept"] == "a switch"
        assert receipt["fallback"] is False
        assert receipt["edition_format"] == "case_study"
        assert receipt["hook_style"] == "personal_story"
        assert receipt["edition_id"] == 9

    def test_fallback_brief_still_gets_a_receipt_and_says_so(self, tmp_path):
        from cqc_lem.utilities.media_provenance import read_brief_receipt

        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()
        with patch.object(nc, "assets_dir", str(assets)), \
             patch("cqc_lem.assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief(focal="deterministic fallback subject", fallback=True)), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   return_value=generated):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
            assert reason is None
            receipt = read_brief_receipt(nc.cover_public_url(rel))
        assert receipt["fallback"] is True

    def test_no_receipt_for_a_render_that_never_gets_stored(self, tmp_path):
        assets = tmp_path / "assets"
        assets.mkdir()
        with patch.object(nc, "assets_dir", str(assets)), \
             patch("cqc_lem.assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=None):
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert rel is None
        assert not any(assets.rglob("*.brief.json"))


class TestCoverVariety:
    """Issue #1992 Box 2: a new cover's brief is steered away from the last few covers.

    Read off the last `_VARIETY_WINDOW` covers' `.brief.json` receipts — no new DB column.
    """

    def _write_receipt(self, directory, name, focal_concept, mtime):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"focal_concept": focal_concept}, fh)
        os.utime(path, (mtime, mtime))

    def test_recent_focal_concepts_reads_the_last_five_most_recent_first(self, tmp_path):
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)):
            directory = nc._cover_dir(3)
            for i in range(7):
                self._write_receipt(directory, f"ed{i}_x.brief.json", f"concept-{i}", 1000 + i)
            recent = nc._recent_focal_concepts(3)
        assert recent == ["concept-6", "concept-5", "concept-4", "concept-3", "concept-2"]

    def test_no_receipts_yet_is_an_empty_list(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path / "assets")):
            assert nc._recent_focal_concepts(3) == []

    def test_a_broken_receipt_is_skipped_not_fatal(self, tmp_path):
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)):
            directory = nc._cover_dir(3)
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "bad.brief.json"), "w") as fh:
                fh.write("not json")
            self._write_receipt(directory, "good.brief.json", "concept-good", 1000)
            assert nc._recent_focal_concepts(3) == ["concept-good"]

    def test_a_sixth_brief_is_steered_away_from_the_prior_five_concepts(self, tmp_path):
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)):
            directory = nc._cover_dir(3)
            for i in range(5):
                self._write_receipt(directory, f"ed{i}_x.brief.json", f"prior-concept-{i}",
                                    1000 + i)

            with patch.object(nc, "_resolve_cover_avatar", return_value=None), \
                 patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                       return_value=_brief(focal="a brand new concept")) as brief, \
                 patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=None):
                nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        avoid = brief.call_args[1]["avoid_terms"]
        for i in range(5):
            assert f"prior-concept-{i}" in avoid

    def test_no_prior_covers_adds_no_avoid_line(self, tmp_path):
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as brief, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=None):
            nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert brief.call_args[1]["avoid_terms"] == []

    def test_regenerating_leaves_exactly_one_receipt_per_stored_cover(self, tmp_path):
        """Issue #2010: the real regeneration flow — save, receipt, then replace.

        Must not leave the superseded cover's `.brief.json` behind with nothing left to
        reference it.
        """
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)):
            first_rel = nc.save_cover_bytes(3, 9, _image_bytes())
            first_abs = nc.cover_abs_path(first_rel)
            self._write_receipt(os.path.dirname(first_abs), os.path.basename(
                os.path.splitext(first_abs)[0] + ".brief.json"), "concept-one", 1000)

            # Regeneration: the new cover is saved, then the previous one is torn down — the
            # order every real caller (`api/routers/user.py`, `run_scheduler.py`) uses.
            second_rel = nc.save_cover_bytes(3, 9, _image_bytes())
            second_abs = nc.cover_abs_path(second_rel)
            self._write_receipt(os.path.dirname(second_abs), os.path.basename(
                os.path.splitext(second_abs)[0] + ".brief.json"), "concept-two", 1001)
            assert nc.remove_cover_file(first_rel) is True

            directory = nc._cover_dir(3)
            receipts = [n for n in os.listdir(directory) if n.endswith(".brief.json")]
            assert len(receipts) == 1
            assert nc._recent_focal_concepts(3) == ["concept-two"]


class TestContentShape:
    def test_edition_format_and_hook_style_reach_the_brief(self, tmp_path):
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as brief, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=None):
            nc.generate_cover_for_edition(3, 9, "T", "S", "B", edition_format="case_study",
                                          hook_style="personal_story")
        assert brief.call_args[1]["content_shape"] == \
            "format=case_study, hook_style=personal_story"

    def test_neither_format_nor_hook_style_passes_none(self, tmp_path):
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as brief, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=None):
            nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert brief.call_args[1]["content_shape"] is None


def _concept(**overrides):
    from cqc_lem.utilities.ai.image_concept import ImageConcept
    fields = {"thesis": "Late invoices quietly starve a small agency's payroll",
              "audience": "agency owners", "specific_entities": ("unpaid invoices", "payroll run",
                                                                 "agency owner"),
              "emotional_beat": "dread", "hook_phrase": "", "treatment": "concrete_scene",
              "treatment_rationale": "a tangible situation"}
    fields.update(overrides)
    return ImageConcept(**fields)


class TestStagedCoverEngine:
    """Issue #2241: covers are built from the WHOLE edition, judged blind, and recorded."""

    def _generated(self, tmp_path):
        path = tmp_path / "gen.png"
        path.write_bytes(_image_bytes())
        return str(path)

    def test_stage_one_reads_the_full_body_with_the_title(self, tmp_path):
        body = "intro " * 50 + "THE PAYOFF SITS HERE " + "tail " * 2000
        with patch.object(nc, "assets_dir", str(tmp_path / "assets")), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=_concept()) as stage1, \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as writer, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=None):
            nc.generate_cover_for_edition(3, 9, "The Title", "The Sub", body)
        text, kwargs = stage1.call_args[0][0], stage1.call_args[1]
        assert "THE PAYOFF SITS HERE" in text, "the payoff sits past the old 1500-char excerpt"
        assert text.startswith("The Sub") and text.rstrip().endswith("tail")
        assert kwargs["title"] == "The Title" and kwargs["surface"] == "newsletter"
        assert kwargs["user_id"] == 3
        assert writer.call_args[1]["concept"] == _concept()

    def test_concept_and_hook_reach_the_base_render_gate(self, tmp_path):
        from cqc_lem.utilities.ai.image_brief import ImageBrief
        concept = _concept(treatment="editorial_graphic", hook_phrase="Payroll eats first")
        brief = ImageBrief(prompt="p", ratio=nc.COVER_IMAGE_RATIO, surface="newsletter",
                           style_preset="newsletter", focal_concept="f", concept=concept,
                           treatment="editorial_graphic", hook_text="Payroll eats first")
        with patch.object(nc, "assets_dir", str(tmp_path / "assets")), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=concept), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=brief), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   return_value=None) as gen:
            nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert gen.call_args[1]["concept"] is concept
        assert gen.call_args[1]["hook_text"] == "Payroll eats first"

    def test_the_avatar_path_gets_the_staged_judge_too(self, tmp_path):
        """It used to skip every newsletter gate (`newsletter_gate = ... and not avatar`)."""
        from cqc_lem.utilities.ai.image_brief import ImageBrief
        avatar = {"status": "succeeded", "model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        concept = _concept(treatment="people_scene")
        brief = ImageBrief(prompt="p", ratio=nc.COVER_IMAGE_RATIO, surface="newsletter",
                           style_preset="newsletter", focal_concept="f", concept=concept,
                           treatment="people_scene")
        with patch.object(nc, "assets_dir", str(tmp_path / "assets")), \
             patch.object(nc, "_resolve_cover_avatar", return_value=avatar), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=concept), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=brief) as writer, \
             patch("cqc_lem.utilities.ai.image_gen.render_avatar_image_gated",
                   return_value=None) as gen:
            nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert writer.call_args[1]["avatar"] is avatar
        assert gen.call_args[1]["concept"] is concept
        assert gen.call_args[1]["hook_text"] is None

    def test_a_rejected_render_records_the_rubric_on_the_receipt(self, tmp_path):
        from cqc_lem.utilities.media_provenance import read_brief_receipt

        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()
        rubric = {"specificity": 2, "no_cliche": 1, "craft": 5}

        def fake_gate(*_args, **kwargs):
            kwargs["render_info"].update({
                "gate_verdict": "rejected", "gate_rubric": rubric,
                "gate_failing": ["specificity", "no_cliche"],
                "gate_issues": ["no_cliche 1/5", "a brass valve"],
                "gate_blind_description": "A brass valve on a workbench."})
            return generated

        with patch.object(nc, "assets_dir", str(assets)), \
             patch("cqc_lem.assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", side_effect=fake_gate), \
             patch.object(nc, "log_info") as info:
            rel, reason = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
            receipt = read_brief_receipt(nc.cover_public_url(rel))
        assert reason is None, "a rejected cover still lands for the author's review"
        assert receipt["gate_verdict"] == "rejected"
        assert receipt["gate_rubric"] == rubric
        assert receipt["gate_failing"] == ["specificity", "no_cliche"]
        assert receipt["gate_issues"] == ["no_cliche 1/5", "a brass valve"]
        assert receipt["gate_blind_description"] == "A brass valve on a workbench."
        assert any("failed the image judge" in c[0][0] for c in info.call_args_list)

    def test_an_accepted_render_logs_no_rejection(self, tmp_path):
        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()

        def fake_gate(*_args, **kwargs):
            kwargs["render_info"]["gate_verdict"] = "accepted"
            return generated

        with patch.object(nc, "assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=None), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", side_effect=fake_gate), \
             patch.object(nc, "log_info") as info:
            nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert not any("failed the image judge" in c[0][0] for c in info.call_args_list)

    def test_the_receipt_records_the_concept_and_treatment(self, tmp_path):
        from cqc_lem.utilities.ai.image_brief import ImageBrief
        from cqc_lem.utilities.media_provenance import read_brief_receipt

        generated = self._generated(tmp_path)
        assets = tmp_path / "assets"
        assets.mkdir()
        concept = _concept()
        brief = ImageBrief(prompt="p", ratio=nc.COVER_IMAGE_RATIO, surface="newsletter",
                           style_preset="newsletter", focal_concept="unpaid invoices",
                           concept=concept, treatment="concrete_scene",
                           required_entities=("unpaid invoices", "payroll run"),
                           prompt_check="judge passed")
        with patch.object(nc, "assets_dir", str(assets)), \
             patch("cqc_lem.assets_dir", str(assets)), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=concept), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=brief), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=generated):
            rel, _ = nc.generate_cover_for_edition(3, 9, "T", "S", "B")
            receipt = read_brief_receipt(nc.cover_public_url(rel))
        assert receipt["treatment"] == "concrete_scene"
        assert receipt["concept"]["thesis"] == concept.thesis
        assert receipt["concept"]["specific_entities"] == list(concept.specific_entities)
        assert receipt["required_entities"] == ["unpaid invoices", "payroll run"]
        assert receipt["prompt_check"] == "judge passed"

    def test_the_dead_metaphor_helpers_are_gone(self):
        for name in ("build_cover_prompt", "_extract_cover_concept", "_cover_concept_text",
                     "_focal_objects", "_recent_focal_objects", "_drop_topic_words"):
            assert not hasattr(nc, name), f"{name} should have been removed with the extractor"


class TestRecentCoverSignals:
    """The variety steer is soft: treatment + focal concept, real briefs only (issue #2241)."""

    def _write(self, directory, name, payload, mtime):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.utime(path, (mtime, mtime))

    def test_signals_name_treatment_and_focal_most_recent_first(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path / "assets")):
            directory = nc._cover_dir(3)
            self._write(directory, "a.brief.json",
                        {"focal_concept": "an agency owner at payroll",
                         "treatment": "people_scene"}, 1000)
            self._write(directory, "b.brief.json", {"focal_concept": "a stack of invoices"}, 1001)
            self._write(directory, "c.brief.json",
                        {"focal_concept": "template text", "fallback": True}, 1002)
            signals = nc._recent_cover_signals(3)
        assert signals == ["a stack of invoices", "people_scene: an agency owner at payroll"]

    def test_a_non_dict_receipt_is_skipped(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path / "assets")):
            directory = nc._cover_dir(3)
            self._write(directory, "a.brief.json", ["not", "a", "dict"], 1000)
            assert nc._recent_cover_signals(3) == []


class TestRecentCoverTreatments:
    """Gauntlet round 1 of #2241: every edition chose a graphic — Stage 1 now sees the last 3."""

    def _write(self, directory, name, payload, mtime):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.utime(path, (mtime, mtime))

    def test_the_last_three_treatments_fallbacks_included_most_recent_first(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path / "assets")):
            directory = nc._cover_dir(3)
            for i, (treatment, fallback) in enumerate([("people_scene", False),
                                                       ("concrete_scene", False),
                                                       ("editorial_graphic", True),
                                                       ("people_scene", False)]):
                self._write(directory, f"e{i}.brief.json",
                            {"focal_concept": f"c{i}", "treatment": treatment,
                             "fallback": fallback}, 1000 + i)
            self._write(directory, "old.brief.json", {"focal_concept": "legacy"}, 900)
            assert nc._recent_cover_treatments(3) == ["people_scene", "editorial_graphic",
                                                      "concrete_scene"]

    def test_stage_one_receives_them(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path / "assets")), \
             patch.object(nc, "_resolve_cover_avatar", return_value=None), \
             patch.object(nc, "_recent_cover_treatments",
                          return_value=["editorial_graphic"]) as recent, \
             patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                   return_value=None) as stage1, \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=None):
            nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        assert recent.call_args[0] == (3, 3)
        assert stage1.call_args[1]["recent_treatments"] == ["editorial_graphic"]
