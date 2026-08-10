"""Unit tests for avatar LoRA generation and the public generate_post_image helper.

The avatar path is the most sensitive surface in media generation: a synthetic likeness
of a real person is published. Tests here verify that the guardrail (resolve_avatar_for)
is the single decision point and that the rendered prompt carries the trigger word only
when policy allows it.
"""
from unittest.mock import patch

import pytest

from cqc_lem.utilities.avatar import replicate_avatar

pytestmark = pytest.mark.unit

_AVATAR = {
    "id": 1,
    "training_id": "train-1",
    "model_ref": "testuser/model:v1",
    "trigger_word": "LEMAVTR42",
    "status": "succeeded",
    "approval_status": "approved",
}


class TestGenerateImageWithAvatar:
    def test_prompt_includes_trigger_word(self):
        with patch(
            "cqc_lem.utilities.ai.ai_helper.get_flux_image_via_replicate",
            return_value="/tmp/image.webp",
        ) as mock_gen:
            replicate_avatar.generate_image_with_avatar("LEMAVTR42, a portrait photo", "testuser/model:v1")
        called_prompt = mock_gen.call_args[0][0]
        assert "LEMAVTR42" in called_prompt
        assert mock_gen.call_args.kwargs["ref"] == "testuser/model:v1"

    def test_ratio_is_threaded_to_replicate(self):
        with patch(
            "cqc_lem.utilities.ai.ai_helper.get_flux_image_via_replicate",
            return_value="/tmp/image.webp",
        ) as mock_gen:
            replicate_avatar.generate_image_with_avatar("a photo", "ref", ratio="9:16")
        assert mock_gen.call_args.kwargs["aspect_ratio"] == "9:16"

    def test_avatar_failure_falls_back_to_base_flux(self):
        with patch(
            "cqc_lem.utilities.ai.ai_helper.get_flux_image_via_replicate",
            side_effect=RuntimeError("inference down"),
        ), \
             patch(
            "cqc_lem.utilities.ai.ai_helper.generate_flux1_image_from_prompt",
            return_value="/flux/fallback.webp",
        ) as mock_fallback:
            path, used_avatar = replicate_avatar.generate_image_with_avatar(
                "LEMAVTR42, a portrait photo", "ref", fallback_prompt="a portrait photo"
            )
        assert path == "/flux/fallback.webp"
        assert used_avatar is False
        assert "LEMAVTR42" not in mock_fallback.call_args[0][0]


class TestGeneratePostImage:
    def test_uses_avatar_when_active_and_succeeded(self):
        # Patch render_avatar_image_gated at its source module so the lazy import
        # inside generate_post_image picks up the mock.
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=_AVATAR), \
             patch(
                 "cqc_lem.utilities.ai.image_gen.render_avatar_image_gated",
                 return_value="/avatar/image.webp",
             ) as mock_gen:
            from cqc_lem.utilities.ai.ai_helper import generate_post_image

            result = generate_post_image("professional headshot", 42)

            assert result == "/avatar/image.webp"
            assert mock_gen.call_args.kwargs["avatar"] == _AVATAR

    def test_falls_back_when_no_active_avatar(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch(
                 "cqc_lem.utilities.ai.image_gen.render_image_gated",
                 return_value="/flux/image.webp",
             ) as mock_flux:
            from cqc_lem.utilities.ai.ai_helper import generate_post_image

            result = generate_post_image("a business photo", 99)

            mock_flux.assert_called_once()
            assert mock_flux.call_args[0][0] == "a business photo"
            assert result == "/flux/image.webp"

    def test_falls_back_when_avatar_not_succeeded(self):
        active_avatar = {
            "id": 1,
            "training_id": "train-1",
            "model_ref": None,
            "trigger_word": "LEMAVTR42",
            "status": "processing",
        }
        with patch("cqc_lem.utilities.db.get_active_avatar", return_value=active_avatar), \
             patch(
                 "cqc_lem.utilities.ai.image_gen.render_image_gated",
                 return_value="/flux/image.webp",
             ) as mock_flux:
            from cqc_lem.utilities.ai.ai_helper import generate_post_image

            result = generate_post_image("a business photo", 1)

            mock_flux.assert_called_once()
            assert mock_flux.call_args[0][0] == "a business photo"
            assert result == "/flux/image.webp"

    def test_focal_concept_reaches_the_gate(self):
        """Issue #1290: generate_post_image threads focal_concept to the vision gate."""
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch(
                 "cqc_lem.utilities.ai.image_gen.render_image_gated",
                 return_value="/flux/image.webp",
             ) as mock_flux:
            from cqc_lem.utilities.ai.ai_helper import generate_post_image

            generate_post_image("a business photo", 1, focal_concept="a founder at a desk")
        assert mock_flux.call_args.kwargs["focal_concept"] == "a founder at a desk"


class TestAvatarGuardrails:
    """The guardrail must fail closed and never duplicate resolve_avatar_for logic."""

    def test_avatar_disabled_overrides_everything(self):
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_disabled": True}), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=_AVATAR), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   return_value="/flux/image.webp") as mock_flux:
            from cqc_lem.utilities.ai.ai_helper import generate_post_image

            result = generate_post_image("a photo", 1)
            mock_flux.assert_called_once()
            assert result == "/flux/image.webp"

    def test_surface_opt_in_off_declines_avatar(self):
        prefs = {
            "avatar_disabled": False,
            "avatar_use_post_image": False,
        }
        with patch("cqc_lem.utilities.db.get_avatar_preferences", return_value=prefs), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=_AVATAR), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   return_value="/flux/image.webp") as mock_flux:
            from cqc_lem.utilities.ai.ai_helper import generate_post_image

            result = generate_post_image("a photo", 1)
            mock_flux.assert_called_once()
            assert result == "/flux/image.webp"
