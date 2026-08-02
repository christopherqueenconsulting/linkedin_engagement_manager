"""Unit tests for tier selection + credit lifecycle in the video pipeline."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


class TestPremiumTier:
    def test_mapping(self):
        from cqc_lem.app.run_content_plan import _premium_tier_for_quality
        assert _premium_tier_for_quality("standard") is None
        assert _premium_tier_for_quality("unknown") is None
        m, c, a = _premium_tier_for_quality("premium")
        assert c == 1 and a is True
        m, c, a = _premium_tier_for_quality("premium_top")
        assert c == 3 and a is True


class TestGenerateVideoSrc:
    def test_premium_no_credits_falls_back_to_standard(self):
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="premium"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=0), \
             patch("cqc_lem.utilities.db.deduct_video_credits") as ded, \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt", return_value="/tmp/i.png"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(1, "text", None, post_id=9)
        ded.assert_not_called()
        assert src == "https://x.mp4"
        assert crv.call_args[1]["model"] == "gen4_turbo"  # standard fallback

    def test_premium_success_deducts_not_refunds(self):
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="premium"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=5), \
             patch("cqc_lem.utilities.db.deduct_video_credits", return_value=True) as ded, \
             patch("cqc_lem.utilities.db.refund_video_credits") as ref, \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(1, "text", None, post_id=9)
        assert src == "https://x.mp4"
        ded.assert_called_once()
        ref.assert_not_called()
        # premium + no avatar -> text->video (first positional image arg is None) with audio
        assert crv.call_args[0][0] is None
        assert crv.call_args[1]["model"] == "veo3.1_fast" and crv.call_args[1]["audio"] is True

    def test_failure_refunds_and_pexels_fallback(self):
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="premium"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=5), \
             patch("cqc_lem.utilities.db.deduct_video_credits", return_value=True), \
             patch("cqc_lem.utilities.db.refund_video_credits") as ref, \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", side_effect=RuntimeError("boom")), \
             patch("cqc_lem.app.run_content_plan.create_folder_if_not_exists"), \
             patch("cqc_lem.utilities.pexels_helper.download_pexels_video", return_value="/tmp/p.mp4", create=True):
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(1, "text", None, post_id=9)
        ref.assert_called_once()
        assert src == "/tmp/p.mp4"

    def test_standard_quality_no_credit_calls(self):
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance") as bal, \
             patch("cqc_lem.utilities.db.deduct_video_credits") as ded, \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt", return_value="/tmp/i.png"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            _generate_video_src(1, "text", None, post_id=9)
        bal.assert_not_called()
        ded.assert_not_called()
        assert crv.call_args[1]["model"] == "gen4_turbo"


_ACTIVE_AVATAR = {"status": "succeeded", "model_ref": "owner/lora:v1", "trigger_word": "TOK",
                  "approval_status": "approved"}


class TestAvatarOnStandardTier:
    def test_standard_uses_avatar_frame_when_present(self):
        """Avatar appears on the standard (free) tier too — frame goes through generate_post_image."""
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=_ACTIVE_AVATAR), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image", return_value="/tmp/avatar.png") as gpi, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt") as flux, \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(7, "text", None, post_id=9)
        assert src == "https://x.mp4"
        gpi.assert_called_once()
        assert gpi.call_args[0][1] == 7  # user_id passed to generate_post_image
        flux.assert_not_called()
        # standard model + avatar frame as the first positional image arg
        assert crv.call_args[1]["model"] == "gen4_turbo"
        assert crv.call_args[0][0] == "/tmp/avatar.png"

    def test_standard_no_avatar_falls_back_to_flux(self):
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image") as gpi, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt", return_value="/tmp/i.png") as render, \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(7, "text", None, post_id=9)
        assert src == "https://x.mp4"
        gpi.assert_not_called()
        render.assert_called_once()
        assert crv.call_args[0][0] == "/tmp/i.png"


    def test_premium_with_avatar_uses_avatar_image_to_video(self):
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="premium"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=5), \
             patch("cqc_lem.utilities.db.deduct_video_credits", return_value=True), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=_ACTIVE_AVATAR), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image", return_value="/tmp/avatar.png") as gpi, \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(7, "text", None, post_id=9)
        assert src == "https://x.mp4"
        gpi.assert_called_once()
        # premium + avatar -> Veo image->video on the avatar frame with audio
        assert crv.call_args[0][0] == "/tmp/avatar.png"
        assert crv.call_args[1]["model"] == "veo3.1_fast" and crv.call_args[1]["audio"] is True


class TestDefaultVideoQualityPreference:
    def test_default_premium_upgrades_standard_post_when_credits(self):
        """post video_quality='standard' but the user's default is premium + has credits -> premium."""
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="premium"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=5), \
             patch("cqc_lem.utilities.db.deduct_video_credits", return_value=True) as ded, \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(1, "text", None, post_id=9)
        assert src == "https://x.mp4"
        ded.assert_called_once()
        assert crv.call_args[1]["model"] == "veo3.1_fast" and crv.call_args[1]["audio"] is True

    def test_default_premium_degrades_to_standard_on_zero_credits(self):
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="premium"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=0), \
             patch("cqc_lem.utilities.db.deduct_video_credits") as ded, \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt", return_value="/tmp/i.png"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            _generate_video_src(1, "text", None, post_id=9)
        ded.assert_not_called()
        assert crv.call_args[1]["model"] == "gen4_turbo"


class TestContentLanguageThreading:
    """Issue #548: the user's language must reach the motion prompt of audio-capable models —
    Veo has no language parameter, so a prompt that omits it gets a voiceover of Veo's choosing."""

    def test_premium_render_passes_the_users_language(self):
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="premium"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=5), \
             patch("cqc_lem.utilities.db.deduct_video_credits", return_value=True), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.utilities.db.get_user_content_language", return_value="es-ES") as lang, \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai",
                   return_value="motion") as motion, \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4"):
            from cqc_lem.app.run_content_plan import _generate_video_src
            _generate_video_src(7, "text", None, post_id=9)
        lang.assert_called_once_with(7)
        assert motion.call_args[1]["language"] == "es-ES"

    def test_standard_render_skips_the_lookup(self):
        """gen4_turbo has no native audio, so there's nothing to steer — don't pay for the query."""
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.utilities.db.get_user_content_language") as lang, \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt", return_value="/tmp/i.png"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4"):
            from cqc_lem.app.run_content_plan import _generate_video_src
            _generate_video_src(7, "text", None, post_id=9)
        lang.assert_not_called()

    def test_text_to_video_prompt_keeps_the_motion_half_intact(self):
        """The audio direction rides on the motion prompt — truncating the combined prompt must
        never eat it (that is exactly how posts #34/#36 lost their audio direction)."""
        long_scene = "s" * 900
        motion_text = "Slow push-in. " + "m" * 400 + " Audio: ambient only."
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="premium"), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=5), \
             patch("cqc_lem.utilities.db.deduct_video_credits", return_value=True), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.utilities.db.get_user_content_language", return_value="en-US"), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value=long_scene), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai",
                   return_value=motion_text), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            _generate_video_src(7, "text", None, post_id=9)
        combined = crv.call_args[0][1]
        assert combined.endswith(motion_text[:512])
        assert "Audio:" in combined
        assert len(combined) <= 980
