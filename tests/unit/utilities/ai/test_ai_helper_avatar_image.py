"""Unit tests for avatar-conditioned image generation (issue #744).

Covers the three defects §2.2 of docs/AVATAR_FIDELITY_AND_VIDEO_LANGUAGE.md traced: the invented
gender, the silently dropped aspect ratio, and the trigger word on person-less scenes.
"""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_AH = "cqc_lem.utilities.ai.ai_helper"
_AVATAR = {"id": 3, "trigger_word": "LEMAVTR1", "model_ref": "owner/lora:v1",
           "status": "succeeded", "approval_status": "approved",
           "gender_presentation": "man", "age_band": "40s"}


class TestProfileVisualContext:
    def test_subject_directive_leads_the_context(self):
        from cqc_lem.utilities.ai.ai_helper import _profile_visual_context
        from cqc_lem.utilities.avatar.attributes import subject_directive

        class _P:
            job_title, industry, company_name = "CTO", "SaaS", "Acme"

        ctx = _profile_visual_context(_P(), subject_directive(_AVATAR))
        assert ctx.startswith("When a person appears")
        assert "a man in his 40s" in ctx
        assert "The author is a CTO" in ctx

    def test_directive_survives_a_profile_with_no_usable_fields(self):
        """The likeness matters more than the brand line — it must not be dropped with it."""
        from cqc_lem.utilities.ai.ai_helper import _profile_visual_context
        assert "a man in his 40s" in _profile_visual_context(None, "a man in his 40s. ")

    def test_no_profile_and_no_directive_is_empty(self):
        from cqc_lem.utilities.ai.ai_helper import _profile_visual_context
        assert _profile_visual_context(None) == ""


class TestGetFluxImagePromptFromAi:
    def _prompt_text(self, avatar):
        from cqc_lem.utilities.ai.ai_helper import get_flux_image_prompt_from_ai
        with patch(f"{_AH}._call_llm") as llm:
            llm.return_value.choices[0].message.content = "a scene"
            get_flux_image_prompt_from_ai("post body", avatar=avatar)
        return llm.call_args.kwargs["messages"][1]["content"]

    def test_declared_subject_reaches_the_prompt_author(self):
        assert "a man in his 40s" in self._prompt_text(_AVATAR)

    def test_no_avatar_adds_nothing(self):
        assert "IS the author" not in self._prompt_text(None)


class TestGeneratePostImage:
    def _generate(self, **kwargs):
        from cqc_lem.utilities.ai.ai_helper import generate_post_image
        return generate_post_image("a whiteboard session", 7, **kwargs)

    def test_ratio_is_threaded_to_the_avatar_render(self):
        """The 9:16 source frame for premium avatar video used to be rendered square."""
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=_AVATAR), \
             patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/a.webp", True)) as gen, \
             patch(f"{_AH}._record_avatar_media"):
            assert self._generate(ratio="9:16") == "/tmp/a.webp"
        assert gen.call_args.kwargs["ratio"] == "9:16"
        assert gen.call_args.kwargs["fallback_prompt"] == "a whiteboard session"

    def test_prompt_carries_trigger_word_and_declared_clause(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=_AVATAR), \
             patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/a.webp", True)) as gen, \
             patch(f"{_AH}._record_avatar_media"):
            self._generate()
        assert gen.call_args[0][0] == "LEMAVTR1, a man in his 40s, a whiteboard session"

    def test_object_scenes_never_reach_the_lora(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for") as resolve, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt",
                   return_value="/tmp/f.webp") as render:
            assert self._generate(depicts_person=False) == "/tmp/f.webp"
        resolve.assert_not_called()
        render.assert_called_once()

    def test_guardrail_decline_uses_the_base_renderer(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt",
                   return_value="/tmp/f.webp") as render:
            assert self._generate(ratio="4:5") == "/tmp/f.webp"
        assert render.call_args.kwargs["ratio"] == "4:5"

    def test_pinned_replicate_model_stays_a_flux_render(self):
        """The admin variant tool names a specific FLUX model — that must not silently
        become a gpt-image render.
        """
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch(f"{_AH}.generate_flux1_image_from_prompt", return_value="/tmp/f.webp") as flux:
            assert self._generate(image_model="black-forest-labs/flux-1.1-pro") == "/tmp/f.webp"
        assert flux.call_args.kwargs["image_model"] == "black-forest-labs/flux-1.1-pro"

    def test_provenance_recorded_only_when_the_avatar_actually_rendered(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=_AVATAR), \
             patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/a.webp", False)), \
             patch(f"{_AH}._record_avatar_media") as record:
            self._generate(post_id=9)
        record.assert_not_called()

    def test_provenance_recorded_on_a_real_avatar_render(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=_AVATAR), \
             patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/a.webp", True)), \
             patch(f"{_AH}._record_avatar_media") as record:
            self._generate(post_id=9)
        record.assert_called_once_with("/tmp/a.webp", 9, 7)


class TestRecordAvatarMedia:
    def test_signs_the_file_and_flags_the_post(self):
        from cqc_lem.utilities.ai.ai_helper import _record_avatar_media
        with patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials") as sign, \
             patch("cqc_lem.utilities.db.mark_post_avatar_media") as mark:
            _record_avatar_media("/tmp/a.webp", 9, 7)
        sign.assert_called_once_with("/tmp/a.webp")
        mark.assert_called_once_with(9)

    def test_no_post_id_still_signs(self):
        from cqc_lem.utilities.ai.ai_helper import _record_avatar_media
        with patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials") as sign, \
             patch("cqc_lem.utilities.db.mark_post_avatar_media") as mark:
            _record_avatar_media("/tmp/a.webp", None, 7)
        sign.assert_called_once()
        mark.assert_not_called()

    @pytest.mark.parametrize("failing", ["sign", "mark"])
    def test_provenance_failures_never_break_generation(self, failing):
        from cqc_lem.utilities.ai.ai_helper import _record_avatar_media
        boom = RuntimeError("nope")
        with patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials",
                   side_effect=boom if failing == "sign" else None), \
             patch("cqc_lem.utilities.db.mark_post_avatar_media",
                   side_effect=boom if failing == "mark" else None):
            _record_avatar_media("/tmp/a.webp", 9, 7)  # must not raise
