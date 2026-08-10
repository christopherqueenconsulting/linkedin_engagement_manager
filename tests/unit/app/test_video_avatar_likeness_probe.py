"""Avatar-likeness probe wiring in the video pipeline (issue #1279)."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_LIKENESS = "cqc_lem.utilities.avatar.likeness_probe"
_OBS = "cqc_lem.utilities.observability"

_AVATAR = {"id": 3, "trigger_word": "LEMAVTR1", "model_ref": "owner/lora:v1",
           "status": "succeeded", "approval_status": "approved",
           "gender_presentation": "man", "age_band": "40s"}


def _generate_video_src(post_id: int = 7):
    from cqc_lem.app.run_content_plan import _generate_video_src
    with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
         patch("cqc_lem.utilities.db.get_video_credit_balance", return_value=0), \
         patch("cqc_lem.utilities.db.deduct_video_credits", return_value=False):
        return _generate_video_src(
            user_id=1,
            text_content="caption body",
            profile=None,
            post_id=post_id,
        )


class TestAvatarLikenessProbeInVideoGeneration:
    def test_probe_runs_on_avatar_source_frame(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=_AVATAR), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch(f"{_RCP}.get_flux_image_prompt_from_ai", return_value="image prompt"), \
             patch(f"{_RCP}.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value="/tmp/avatar_frame.webp") as gen, \
             patch(f"{_RCP}.create_runway_video", return_value="https://runway.video/abc.mp4"), \
             patch(f"{_LIKENESS}.probe_avatar_likeness",
                   return_value={"present": True, "checked": True, "reason": "ok"}) as probe, \
             patch(f"{_OBS}.track_avatar_likeness_probe") as track:
            result = _generate_video_src(post_id=7)
        assert result == "https://runway.video/abc.mp4"
        gen.assert_called_once()
        probe.assert_called_once_with("/tmp/avatar_frame.webp", _AVATAR, user_id=1, post_id=7)
        track.assert_called_once_with(1, 7, {"present": True, "checked": True, "reason": "ok"})

    def test_no_probe_when_no_avatar(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch(f"{_RCP}.get_flux_image_prompt_from_ai", return_value="image prompt"), \
             patch(f"{_RCP}.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt",
                   return_value="/tmp/base_frame.webp"), \
             patch(f"{_RCP}.create_runway_video", return_value="https://runway.video/base.mp4"), \
             patch(f"{_LIKENESS}.probe_avatar_likeness") as probe, \
             patch(f"{_OBS}.track_avatar_likeness_probe") as track:
            result = _generate_video_src(post_id=8)
        assert result == "https://runway.video/base.mp4"
        probe.assert_not_called()
        track.assert_not_called()

    def test_failed_probe_does_not_block_by_default(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=_AVATAR), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch(f"{_RCP}.get_flux_image_prompt_from_ai", return_value="image prompt"), \
             patch(f"{_RCP}.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value="/tmp/avatar_frame.webp"), \
             patch(f"{_RCP}.create_runway_video", return_value="https://runway.video/abc.mp4"), \
             patch(f"{_LIKENESS}.probe_avatar_likeness",
                   return_value={"present": False, "checked": True, "reason": "wrong face"}), \
             patch(f"{_OBS}.track_avatar_likeness_probe"):
            result = _generate_video_src(post_id=9)
        assert result == "https://runway.video/abc.mp4"

    def test_hold_flag_drops_to_fallback_on_failed_probe(self):
        with patch(f"{_RCP}.AVATAR_LIKENESS_VIDEO_HOLD_ENABLED", True), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=_AVATAR), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch(f"{_RCP}.get_flux_image_prompt_from_ai", return_value="image prompt"), \
             patch(f"{_RCP}.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_post_image",
                   return_value="/tmp/avatar_frame.webp"), \
             patch(f"{_RCP}.create_runway_video") as video, \
             patch(f"{_LIKENESS}.probe_avatar_likeness",
                   return_value={"present": False, "checked": True, "reason": "wrong face"}), \
             patch(f"{_OBS}.track_avatar_likeness_probe"):
            result = _generate_video_src(post_id=10)
        assert result is None
        video.assert_not_called()
