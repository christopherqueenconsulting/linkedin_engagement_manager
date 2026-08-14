"""Unit tests for tier selection + credit lifecycle in the video pipeline."""

from unittest.mock import patch

import pytest

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


class TestSourceFrameRatio:
    """The brief and the render must agree on the aspect ratio.

    Issue #1141: the brief composes framing for the ratio it is HANDED, so briefing 1:1 and
    rendering the premium source frame at 9:16 asked for a square composition and cropped it.
    """

    def _run(self, quality, model_name):
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value=quality), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value=quality), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=5), \
             patch("cqc_lem.utilities.db.deduct_video_credits", return_value=True), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=_ACTIVE_AVATAR), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai",
                   return_value="scene") as brief, \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value="/tmp/a.png") as gpi, \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai",
                   return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video",
                   return_value="https://x.mp4") as crv:
            from cqc_lem.app.run_content_plan import _generate_video_src
            _generate_video_src(1, "text", None, post_id=9)
        assert crv.call_args[1]["model"] == model_name
        return brief.call_args[1]["ratio"], gpi.call_args[1]["ratio"]

    def test_premium_briefs_and_renders_the_same_vertical_frame(self):
        briefed, rendered = self._run("premium", "veo3.1_fast")
        assert briefed == rendered == "9:16"

    def test_standard_briefs_and_renders_the_same_default_frame(self):
        from cqc_lem.utilities.env_constants import DEFAULT_IMAGE_RATIO
        briefed, rendered = self._run("standard", "gen4_turbo")
        assert briefed == rendered == DEFAULT_IMAGE_RATIO


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
        """Post video_quality='standard' but the user's default is premium + has credits -> premium."""
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


class TestPersistedRenderModel:
    """Issue #1410: `posts.video_model` records the model that ACTUALLY rendered the asset.

    `posts.video_quality` records what was requested — it survives the no-credits degrade and the
    Pexels fallback unchanged — so it cannot answer which model ran, and the stored URL is written
    under `videos/runwayml/` whatever produced it.
    """

    def _render(self, quality="premium", balance=5, render="https://x.mp4", stock="/tmp/p.mp4",
                post_id=9):
        from unittest.mock import MagicMock
        writer = MagicMock(return_value=True)
        crv = (patch("cqc_lem.app.run_content_plan.create_runway_video", side_effect=render)
               if isinstance(render, Exception)
               else patch("cqc_lem.app.run_content_plan.create_runway_video", return_value=render))
        pexels = (patch("cqc_lem.utilities.pexels_helper.download_pexels_video",
                        side_effect=stock, create=True)
                  if isinstance(stock, Exception)
                  else patch("cqc_lem.utilities.pexels_helper.download_pexels_video",
                             return_value=stock, create=True))
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value=quality), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value=quality), \
             patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=balance), \
             patch("cqc_lem.utilities.db.deduct_video_credits", return_value=True), \
             patch("cqc_lem.utilities.db.refund_video_credits"), \
             patch("cqc_lem.utilities.db.update_db_post_video_model", writer), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt", return_value="/tmp/i.png"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai",
                   return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_folder_if_not_exists"), \
             crv, pexels:
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(1, "text", None, post_id=post_id)
        return src, writer

    def test_premium_render_records_the_veo_key(self):
        src, writer = self._render(quality="premium", balance=5)
        assert src == "https://x.mp4"
        writer.assert_called_once_with(9, "veo3.1_fast")

    def test_a_degraded_premium_records_the_model_that_actually_ran(self):
        """No credits -> the render is standard, so the recorded model must be too."""
        _src, writer = self._render(quality="premium", balance=0)
        writer.assert_called_once_with(9, "gen4_turbo")

    def test_the_stock_fallback_records_pexels(self):
        from cqc_lem.utilities.content_quality import VIDEO_MODEL_PEXELS
        src, writer = self._render(render=RuntimeError("boom"))
        assert src == "/tmp/p.mp4"
        writer.assert_called_once_with(9, VIDEO_MODEL_PEXELS)

    def test_a_render_that_produced_nothing_clears_the_column(self):
        """Leaving a previous render's key behind would name the model of a video it never made."""
        src, writer = self._render(render=RuntimeError("boom"), stock=None)
        assert src is None
        writer.assert_called_once_with(9, None)

    def test_a_failed_stock_fallback_clears_the_column_too(self):
        src, writer = self._render(render=RuntimeError("boom"), stock=RuntimeError("pexels down"))
        assert src is None
        writer.assert_called_once_with(9, None)

    def test_no_post_id_writes_nothing(self):
        """`create_video_content` can run without a post row (preview paths) — nothing to write to."""
        src, writer = self._render(post_id=None)
        assert src == "https://x.mp4"
        writer.assert_not_called()

    def test_a_write_that_raises_never_costs_the_video(self):
        from unittest.mock import MagicMock
        writer = MagicMock(side_effect=RuntimeError("db down"))
        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.update_db_post_video_model", writer), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch("cqc_lem.app.run_content_plan.get_flux_image_prompt_from_ai", return_value="scene"), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt", return_value="/tmp/i.png"), \
             patch("cqc_lem.app.run_content_plan.get_runway_ml_video_prompt_from_ai",
                   return_value="motion"), \
             patch("cqc_lem.app.run_content_plan.create_runway_video", return_value="https://x.mp4"):
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(1, "text", None, post_id=9)
        # The telemetry write is best-effort: the rendered video is returned either way.
        assert src == "https://x.mp4"

    def test_a_rejected_probe_clears_the_recorded_model(self):
        """The render happened, but its file never became the post's media.

        On the regenerate path the row still carries the PREVIOUS video's URL, so leaving the key
        would name the rejected render as the model of the video that actually shipped.
        """
        from unittest.mock import MagicMock
        writer = MagicMock(return_value=True)
        with patch("cqc_lem.app.run_content_plan._probe_video_file",
                   return_value=(False, "empty file")), \
             patch("cqc_lem.app.run_content_plan.track_video_asset_probe"), \
             patch("cqc_lem.app.run_content_plan.VIDEO_PROBE_ENABLED", False), \
             patch("cqc_lem.utilities.db.update_db_post_video_model", writer):
            from cqc_lem.app.run_content_plan import _accept_probed_video
            accepted = _accept_probed_video(9, "/tmp/v.mp4", "https://x.mp4")
        assert accepted is False
        writer.assert_called_once_with(9, None)

    def test_a_passing_probe_leaves_the_recorded_model_alone(self):
        from unittest.mock import MagicMock
        writer = MagicMock(return_value=True)
        with patch("cqc_lem.app.run_content_plan._probe_video_file", return_value=(True, "")), \
             patch("cqc_lem.app.run_content_plan.track_video_asset_probe"), \
             patch("cqc_lem.utilities.db.update_db_post_video_model", writer):
            from cqc_lem.app.run_content_plan import _accept_probed_video
            accepted = _accept_probed_video(9, "/tmp/v.mp4", "https://x.mp4")
        assert accepted is True
        writer.assert_not_called()

    def test_a_hard_probe_failure_clears_it_before_raising(self):
        """`VIDEO_PROBE_ENABLED` raises, so the clear has to happen first or it never happens."""
        from unittest.mock import MagicMock
        writer = MagicMock(return_value=True)
        with patch("cqc_lem.app.run_content_plan._probe_video_file",
                   return_value=(False, "empty file")), \
             patch("cqc_lem.app.run_content_plan.track_video_asset_probe"), \
             patch("cqc_lem.app.run_content_plan.VIDEO_PROBE_ENABLED", True), \
             patch("cqc_lem.utilities.db.update_db_post_video_model", writer):
            from cqc_lem.app.run_content_plan import _accept_probed_video
            with pytest.raises(RuntimeError):
                _accept_probed_video(9, "/tmp/v.mp4", "https://x.mp4")
        writer.assert_called_once_with(9, None)

    def test_a_download_that_raises_clears_it_on_the_store_path(self):
        """The probe never runs when the download raises, so the clear has to happen there too.

        `save_video_url_to_dir` raises on a non-2xx; the regenerate path then keeps the PREVIOUS
        video's URL, so a left-behind key would name a render that produced nothing for this post.
        """
        from unittest.mock import MagicMock
        writer = MagicMock(return_value=True)
        with patch("cqc_lem.app.run_content_plan.create_folder_if_not_exists"), \
             patch("cqc_lem.app.run_content_plan.save_video_url_to_dir",
                   side_effect=RuntimeError("404")), \
             patch("cqc_lem.app.run_content_plan._accept_probed_video") as accept, \
             patch("cqc_lem.utilities.db.update_db_post_video_model", writer):
            from cqc_lem.app.run_content_plan import _store_video_asset
            with pytest.raises(RuntimeError):
                _store_video_asset(9, "https://x.mp4")
        writer.assert_called_once_with(9, None)
        accept.assert_not_called()

    def test_a_stored_video_keeps_its_key_when_a_later_step_fails(self):
        """Only the download is wrapped: a key describing an asset that IS stored stays put."""
        from unittest.mock import MagicMock
        writer = MagicMock(return_value=True)
        with patch("cqc_lem.app.run_content_plan.create_folder_if_not_exists"), \
             patch("cqc_lem.app.run_content_plan.save_video_url_to_dir", return_value="/tmp/v.mp4"), \
             patch("cqc_lem.app.run_content_plan._accept_probed_video", return_value=True), \
             patch("cqc_lem.app.run_content_plan._caption_video_asset"), \
             patch("cqc_lem.app.run_content_plan.update_db_post_video_url", return_value=True), \
             patch("cqc_lem.utilities.db.update_db_post_video_model", writer):
            from cqc_lem.app.run_content_plan import _store_video_asset
            assert _store_video_asset(9, "/tmp/pexels_1.mp4") is not None
        writer.assert_not_called()


class TestContentLanguageThreading:
    """Issue #548: the user's language must reach the motion prompt of audio-capable models —
    Veo has no language parameter, so a prompt that omits it gets a voiceover of Veo's choosing.
    """

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
        never eat it (that is exactly how posts #34/#36 lost their audio direction).
        """
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
