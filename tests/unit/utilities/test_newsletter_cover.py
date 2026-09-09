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


def _brief(prompt="a prompt", focal="a focal concept", fallback=False):
    from cqc_lem.utilities.ai.image_brief import ImageBrief
    return ImageBrief(prompt=prompt, ratio=nc.COVER_IMAGE_RATIO, surface="newsletter",
                      style_preset="newsletter", focal_concept=focal, fallback=fallback)


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


def _concept_resp(payload):
    import json as _json
    from types import SimpleNamespace
    content = payload if isinstance(payload, str) else _json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class TestExtractCoverConcept:
    """Issue #1992: the concept-extraction call reads the FULL body, never the hook excerpt.

    `_edition_text` sends the brief author the hook; this degrades to `{}` on anything but a
    usable reply.
    """

    def test_a_usable_response_is_parsed(self):
        payload = {"core_mechanism": "a switch routes cheap traffic down the cheap path",
                  "tangible_metaphor_candidates": ["a rotary switch", "a rail track fork"],
                  "avoid": ["laptop", "screen"]}
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.return_value = _concept_resp(payload)
            concept = nc._extract_cover_concept("T", "S", "B")
        assert concept["core_mechanism"] == payload["core_mechanism"]
        assert concept["tangible_metaphor_candidates"] == payload["tangible_metaphor_candidates"]
        assert concept["avoid"] == ["laptop", "screen"]

    def test_the_token_budget_leaves_room_for_reasoning_tokens(self):
        """`lem-simple` is a reasoning model, so the budget covers thinking tokens too.

        At 300 the whole budget went to reasoning and every one of the five live editions came
        back EMPTY with finish_reason='length'.
        """
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.return_value = _concept_resp(
                {"core_mechanism": "m", "tangible_metaphor_candidates": ["o"], "avoid": []})
            nc._extract_cover_concept("T", "S", "B")
        kwargs = mock_client.chat.completions.create.call_args[1]
        assert kwargs["max_tokens"] >= 1200, (
            "a real extraction costs 540-1020 completion tokens including reasoning")

    def test_the_full_body_reaches_the_prompt_not_a_1500_char_excerpt(self):
        long_body = "x" * 5000
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.return_value = _concept_resp(
                {"core_mechanism": "m", "tangible_metaphor_candidates": [], "avoid": []})
            nc._extract_cover_concept("T", "S", long_body)
        sent = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"]
        assert "x" * 4000 in sent, "the concept extractor must see well past the 1500-char hook"

    def test_empty_edition_never_calls_the_llm(self):
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            assert nc._extract_cover_concept(None, None, None) == {}
        mock_client.chat.completions.create.assert_not_called()

    def test_llm_outage_degrades_to_empty(self):
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.side_effect = RuntimeError("down")
            assert nc._extract_cover_concept("T", "S", "B") == {}

    def test_unparseable_response_degrades_to_empty(self):
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.return_value = _concept_resp("not json")
            assert nc._extract_cover_concept("T", "S", "B") == {}

    def test_a_response_with_neither_mechanism_nor_candidates_is_empty(self):
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.return_value = _concept_resp(
                {"core_mechanism": "", "tangible_metaphor_candidates": [], "avoid": ["laptop"]})
            assert nc._extract_cover_concept("T", "S", "B") == {}


class TestCoverConceptText:
    def test_uses_the_extracted_concept_when_available(self):
        with patch.object(nc, "_extract_cover_concept", return_value={
                "core_mechanism": "a leak drips from a pipe",
                "tangible_metaphor_candidates": ["a copper pipe joint"], "avoid": []}):
            content, avoid = nc._cover_concept_text("T", "S", "B")
        assert avoid == []
        assert "Core mechanism to depict: a leak drips from a pipe" in content
        assert "Candidate physical metaphors: a copper pipe joint" in content
        assert "B" not in content, "the raw hook excerpt is dropped once a concept exists"

    def test_falls_back_to_the_excerpt_when_extraction_is_empty(self):
        with patch.object(nc, "_extract_cover_concept", return_value={}):
            content, _avoid = nc._cover_concept_text("T", "S", "the raw body text")
        assert "the raw body text" in content

    def test_variety_avoid_comes_back_separately_never_in_the_content(self):
        """The content reaches a RENDERER on the fallback path, so an avoid noun in it is drawn."""
        with patch.object(nc, "_extract_cover_concept",
                          return_value={"core_mechanism": "m", "avoid": ["laptop"]}):
            content, avoid = nc._cover_concept_text("T", "S", "B",
                                                    variety_avoid=["prior concept one"])
        assert avoid == ["laptop", "prior concept one"]
        assert "prior concept one" not in content
        assert "laptop" not in content

    def test_no_avoid_at_all_returns_an_empty_list(self):
        with patch.object(nc, "_extract_cover_concept", return_value={}):
            _content, avoid = nc._cover_concept_text("T", "S", "B")
        assert avoid == []


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


class TestFocalObjects:
    """Issue #2000: a focal concept is a SENTENCE, and a sentence never matches the next one.

    Four consecutive live covers all chose a valve while every one of those sentences sat in the
    "already used" context doing nothing.
    """

    def test_the_preset_vocabulary_wins_over_the_generic_pass(self):
        assert nc._focal_objects(
            "Budget leak depicted as a dripping valve spilling money") == ["leak", "valve"]

    def test_abstract_wrapping_is_stripped(self):
        assert nc._focal_objects(
            "A valve symbolizing efficiency in AI auditing for e-commerce") == ["valve"]

    def test_the_first_named_object_leads(self):
        """dict.fromkeys, not set(): the object named FIRST is the one the cover was built on."""
        assert nc._focal_objects("A cracked gear beside a small valve")[0] == "gear"

    def test_a_concept_naming_no_known_object_falls_back_to_tokens(self):
        assert nc._focal_objects("A weathered anvil in an empty forge") == ["anvil", "forge"]

    def test_empty_or_unusable_input_steers_nothing(self):
        assert nc._focal_objects("") == []
        assert nc._focal_objects(None) == []
        assert nc._focal_objects("the a of in") == []

    def test_never_more_than_the_cap_from_one_concept(self):
        objects = nc._focal_objects("a valve a gear a scale a domino a fuse")
        assert len(objects) <= nc._MAX_OBJECTS_PER_CONCEPT


class TestRecentFocalObjects:
    @staticmethod
    def _receipt(directory, name, concept, mtime, fallback=False):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"focal_concept": concept, "fallback": fallback}, fh)
        os.utime(path, (mtime, mtime))

    def test_objects_come_back_deduped_most_recent_first(self, tmp_path):
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)):
            directory = nc._cover_dir(7)
            self._receipt(directory, "a.brief.json", "A cracked gear in a still machine", 1000)
            self._receipt(directory, "b.brief.json", "A dripping valve over a puddle", 2000)
            self._receipt(directory, "c.brief.json", "A leaking valve on a bench", 3000)
            objects = nc._recent_focal_objects(7)
        assert objects[0] == "valve", "most recent first"
        assert objects.count("valve") == 1, "deduped"
        assert "gear" in objects

    def test_a_fallback_receipt_contributes_nothing(self, tmp_path):
        """Its focal concept is the edition's own title text, not an object."""
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)):
            directory = nc._cover_dir(7)
            self._receipt(directory, "a.brief.json",
                          "The Overlooked Costs of Your AI LinkedIn Strategy", 1000,
                          fallback=True)
            objects = nc._recent_focal_objects(7)
        assert objects == []

    def test_the_variety_avoid_list_carries_objects_not_sentences(self, tmp_path):
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)):
            directory = nc._cover_dir(3)
            self._receipt(directory, "a.brief.json",
                          "Budget leak depicted as a dripping valve spilling money", 1000)
            with patch.object(nc, "_resolve_cover_avatar", return_value=None), \
                 patch.object(nc, "_extract_cover_concept", return_value={}), \
                 patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                       return_value=_brief()) as brief, \
                 patch("cqc_lem.utilities.ai.image_gen.render_image_gated", return_value=None):
                nc.generate_cover_for_edition(3, 9, "T", "S", "B")
        avoid = brief.call_args[1]["avoid_terms"]
        # The PRIMARY object of that concept, never the whole sentence (#2000), and one per
        # cover so the topic's own noun is not banned alongside it (#2005).
        assert avoid == ["leak"]
        assert not any(" " in term for term in avoid), "sentences steer nothing"


class TestTopicWordsAreNeverBanned:
    """Issue #2005: an edition about a leak cannot be steered away from a leak."""

    def test_a_term_the_title_uses_is_dropped(self):
        assert nc._drop_topic_words(["valve", "leak"],
                                    "Spot the Leak in Your LinkedIn AI Budget", None) == ["valve"]

    def test_a_term_the_subtitle_uses_is_dropped(self):
        assert nc._drop_topic_words(["gear"], "T", "A cracked gear in the chain") == []

    def test_unrelated_terms_survive(self):
        assert nc._drop_topic_words(["valve", "gear"], "Audit your AI spend", "") == \
            ["valve", "gear"]

    def test_no_title_or_subtitle_bans_nothing(self):
        assert nc._drop_topic_words(["valve"], None, None) == ["valve"]


class TestOnlyThePrimaryObjectSteers:
    def test_the_second_object_in_a_concept_is_the_topics_own_noun(self, tmp_path):
        assets = tmp_path / "assets"
        with patch.object(nc, "assets_dir", str(assets)):
            directory = nc._cover_dir(5)
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "a.brief.json"), "w", encoding="utf-8") as fh:
                json.dump({"focal_concept": "Budget leak depicted as a dripping valve",
                           "fallback": False}, fh)
            objects = nc._recent_focal_objects(5)
        assert objects == ["leak"], "one object per prior cover, the primary one"
