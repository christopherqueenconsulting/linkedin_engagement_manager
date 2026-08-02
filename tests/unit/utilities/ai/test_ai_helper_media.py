"""Unit tests for the rewritten image/video prompt + Replicate helpers."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


def _system_text(create_mock):
    msgs = create_mock.call_args[1]["messages"]
    return msgs[0]["content"]


def _user_text(create_mock):
    msgs = create_mock.call_args[1]["messages"]
    content = msgs[1]["content"]
    # Brief-engine calls send a plain string; legacy multimodal calls send a content list.
    return content if isinstance(content, str) else content[0]["text"]


class TestProfileVisualContext:
    def test_builds_line_from_profile(self, sample_linkedin_profile):
        from cqc_lem.utilities.ai.ai_helper import _profile_visual_context
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        ctx = _profile_visual_context(LinkedInProfile(**sample_linkedin_profile))
        assert "Software Engineer" in ctx and "Technology" in ctx

    def test_none_profile_returns_empty(self):
        from cqc_lem.utilities.ai.ai_helper import _profile_visual_context
        assert _profile_visual_context(None) == ""


class TestFluxImagePrompt:
    def test_injects_profile_and_no_text_constraint(self, mock_openai_client, sample_linkedin_profile):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_flux_image_prompt_from_ai
            from cqc_lem.utilities.linkedin.profile import LinkedInProfile
            profile = LinkedInProfile(**sample_linkedin_profile)
            out = get_flux_image_prompt_from_ai("My post", profile=profile, ratio="1:1")
            assert isinstance(out, str)
            create = mock_openai_client.chat.completions.create
            assert create.call_args[1]["model"] == "lem-medium"
            assert "Software Engineer" in _user_text(create)
            sys = _system_text(create)
            assert "NO text" in sys  # the no-garbled-text constraint

    def test_works_without_profile(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_flux_image_prompt_from_ai
            out = get_flux_image_prompt_from_ai("My post")
            assert isinstance(out, str)


class TestRunwayMotionPrompt:
    def test_motion_first_system_prompt(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            get_runway_ml_video_prompt_from_ai("post", "an office scene", model="gen4_turbo")
            sys = _system_text(mock_openai_client.chat.completions.create)
            assert "motion" in sys.lower()
            assert "audio" not in sys.lower()  # not veo3.1

    def test_veo_adds_audio_cue(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            get_runway_ml_video_prompt_from_ai("post", "scene", model="veo3.1")
            sys = _system_text(mock_openai_client.chat.completions.create)
            assert "audio" in sys.lower()


class TestAudioDirection:
    """Issue #548: Veo has no language parameter, so an audio-capable render whose prompt carries
    no audio direction invents a voiceover in a language of its own choosing (posts #34/#36)."""

    def test_only_audio_capable_models_get_a_direction(self):
        from cqc_lem.utilities.ai.ai_helper import _audio_direction
        from cqc_lem.utilities.ai.video_models import VIDEO_MODELS
        for name, spec in VIDEO_MODELS.items():
            direction = _audio_direction(name, "en-US")
            assert bool(direction) is spec.supports_audio, name

    def test_unknown_model_gets_no_direction(self):
        from cqc_lem.utilities.ai.ai_helper import _audio_direction
        assert _audio_direction("not-a-model") == ""

    def test_bans_speech_and_names_the_language(self):
        from cqc_lem.utilities.ai.ai_helper import _audio_direction
        from cqc_lem.utilities.ai.video_models import AUDIO_DIRECTION_MARKER
        direction = _audio_direction("veo3.1_fast", "es-ES")
        assert direction.startswith(AUDIO_DIRECTION_MARKER)
        assert "Spanish (es-ES)" in direction
        for banned in ("no spoken dialogue", "no voiceover", "no narration"):
            assert banned in direction.lower()

    def test_defaults_to_english_when_language_unset(self):
        from cqc_lem.utilities.ai.ai_helper import _audio_direction
        assert "English" in _audio_direction("veo3.1_fast")

    def test_appended_to_the_returned_motion_prompt(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import (get_runway_ml_video_prompt_from_ai,
                                                        _audio_direction)
            out = get_runway_ml_video_prompt_from_ai("post", "scene", model="veo3.1_fast",
                                                     language="fr-FR")
            assert out.startswith("Mock AI response")
            assert out.endswith(_audio_direction("veo3.1_fast", "fr-FR"))

    def test_not_appended_for_silent_models(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            out = get_runway_ml_video_prompt_from_ai("post", "scene", model="gen4_turbo")
            assert out == "Mock AI response"

    def test_long_motion_is_trimmed_but_the_direction_survives(self, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = "x" * 900
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import (get_runway_ml_video_prompt_from_ai,
                                                        _audio_direction, MAX_MOTION_PROMPT_CHARS)
            out = get_runway_ml_video_prompt_from_ai("post", "scene", model="veo3.1")
            # Callers cap the prompt at MAX_MOTION_PROMPT_CHARS — the direction must survive that.
            assert len(out) <= MAX_MOTION_PROMPT_CHARS
            assert out[:MAX_MOTION_PROMPT_CHARS].endswith(_audio_direction("veo3.1"))

    def test_system_prompt_keeps_the_llm_off_audio(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            get_runway_ml_video_prompt_from_ai("post", "scene", model="veo3.1_fast")
            sys = _system_text(mock_openai_client.chat.completions.create)
            assert "Say NOTHING about audio" in sys


class TestFluxViaReplicate:
    def test_flux_dev_schema_and_aspect_ratio(self):
        with patch("cqc_lem.utilities.ai.ai_helper.replicate.run", return_value=["http://x/folder/img.webp"]) as run, \
             patch("cqc_lem.utilities.ai.ai_helper.save_video_url_to_dir", return_value="/tmp/img.webp"), \
             patch("cqc_lem.utilities.ai.ai_helper.create_folder_if_not_exists"):
            from cqc_lem.utilities.ai.ai_helper import get_flux_image_via_replicate
            path = get_flux_image_via_replicate("a prompt", ref="black-forest-labs/flux-dev",
                                                aspect_ratio="1:1")
            assert path == "/tmp/img.webp"
            inp = run.call_args[1]["input"]
            assert inp["aspect_ratio"] == "1:1"
            assert "num_inference_steps" in inp  # flux-dev schema

    def test_flux_pro_schema_and_single_output(self):
        with patch("cqc_lem.utilities.ai.ai_helper.replicate.run", return_value="http://x/folder/img.png") as run, \
             patch("cqc_lem.utilities.ai.ai_helper.save_video_url_to_dir", return_value="/tmp/img.png"), \
             patch("cqc_lem.utilities.ai.ai_helper.create_folder_if_not_exists"):
            from cqc_lem.utilities.ai.ai_helper import get_flux_image_via_replicate
            path = get_flux_image_via_replicate("a prompt", ref="black-forest-labs/flux-1.1-pro",
                                                aspect_ratio="4:5")
            assert path == "/tmp/img.png"
            inp = run.call_args[1]["input"]
            assert inp["aspect_ratio"] == "4:5"
            assert "prompt_upsampling" in inp  # flux-1.1-pro schema
            assert "num_inference_steps" not in inp
