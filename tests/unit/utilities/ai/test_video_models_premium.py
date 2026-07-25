"""Unit tests for premium tiers + text->video + audio in video_models."""

import pytest

pytestmark = pytest.mark.unit


class TestTiers:
    def test_credits_and_premium(self):
        from cqc_lem.utilities.ai.video_models import model_credits, is_premium
        assert model_credits("gen4_turbo") == 0 and is_premium("gen4_turbo") is False
        assert model_credits("veo3.1_fast") == 1 and is_premium("veo3.1_fast") is True
        assert model_credits("veo3.1") == 3 and is_premium("veo3.1") is True

    def test_resolve_duration(self):
        from cqc_lem.utilities.ai.video_models import resolve_duration
        assert resolve_duration("gen4_turbo", 5) == 5
        assert resolve_duration("veo3.1_fast", 5) == 6   # 5 invalid for veo -> default 6
        assert resolve_duration("veo3.1_fast", 8) == 8   # 8 is valid

    def test_estimate_cost_veo(self):
        from cqc_lem.utilities.ai.video_models import estimate_video_cost
        assert estimate_video_cost("veo3.1", 6) == 2.4
        assert estimate_video_cost("gen4_turbo", 5) == 0.25


# Every audio-capable prompt now carries ai_helper._audio_direction()'s clause (issue #548);
# create_runway_video refuses to enable audio without it.
_WITH_AUDIO_DIRECTION = ("a scene with motion. Audio: natural ambient sound only, at low volume. "
                         "No spoken dialogue, no voiceover, no narration.")


class TestEndpointSelection:
    def test_text_to_video_when_no_image(self, mock_runwayml):
        from cqc_lem.utilities.ai.video_models import create_runway_video
        url = create_runway_video(None, _WITH_AUDIO_DIRECTION, model="veo3.1_fast", audio=True)
        assert url == "https://runway.example/video.mp4"
        # used the text_to_video endpoint, not image_to_video
        assert mock_runwayml["client"].text_to_video.create.called
        assert not mock_runwayml["client"].image_to_video.create.called
        kw = mock_runwayml["client"].text_to_video.create.call_args[1]
        assert kw["model"] == "veo3.1_fast" and kw["audio"] is True
        assert kw["duration"] == 6 and "prompt_image" not in kw

    def test_image_to_video_when_image_given(self, mock_runwayml):
        from cqc_lem.utilities.ai.video_models import create_runway_video
        create_runway_video("https://img/x.png", _WITH_AUDIO_DIRECTION, model="veo3.1", audio=True)
        assert mock_runwayml["client"].image_to_video.create.called
        kw = mock_runwayml["client"].image_to_video.create.call_args[1]
        assert kw["prompt_image"] == "https://img/x.png" and kw["audio"] is True

    def test_audio_omitted_for_non_audio_model(self, mock_runwayml):
        from cqc_lem.utilities.ai.video_models import create_runway_video
        create_runway_video("https://img/x.png", "move", model="gen4_turbo", audio=True)
        kw = mock_runwayml["client"].image_to_video.create.call_args[1]
        assert "audio" not in kw  # gen4_turbo doesn't support audio


class TestAudioDirectionGate:
    """Issue #548: rendering silent beats rendering a hallucinated foreign-language voiceover."""

    def test_audio_dropped_when_prompt_has_no_audio_direction(self, mock_runwayml):
        from cqc_lem.utilities.ai.video_models import create_runway_video
        create_runway_video("https://img/x.png", "slow push-in", model="veo3.1_fast", audio=True)
        kw = mock_runwayml["client"].image_to_video.create.call_args[1]
        assert "audio" not in kw

    def test_cost_meta_records_the_downgrade(self, mock_runwayml):
        from unittest.mock import patch
        from cqc_lem.utilities.ai.video_models import create_runway_video
        with patch("cqc_lem.utilities.ai.video_models.track_media_cost") as track:
            create_runway_video("https://img/x.png", "slow push-in", model="veo3.1_fast", audio=True)
        assert track.call_args[1]["meta"]["audio"] is False
