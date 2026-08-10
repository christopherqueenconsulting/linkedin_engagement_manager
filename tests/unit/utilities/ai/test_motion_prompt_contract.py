"""Regression guard for the video motion-prompt contract (issue #1140).

The writer-side contract lives in `content_framework.motion_prompt_directive()` and is appended
to the system prompt in `ai_helper.get_runway_ml_video_prompt_from_ai()`. These tests fail the build
if either of those two facts drifts, or if any of the six contract rules is removed from the prompt
the model actually sees.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestMotionPromptDirective:
    """`motion_prompt_directive()` is a named, falsifiable contract."""

    def test_all_six_rules_present(self):
        from cqc_lem.utilities.ai.content_framework import motion_prompt_directive
        directive = motion_prompt_directive()
        for rule in (
            "OPEN IN THE FIRST 1–2 SECONDS",
            "ONE CONTINUOUS MOTION ONLY",
            "CONCRETE PHYSICAL TERMS ONLY",
            "MATCH THE TIER",
            "NEVER DESCRIBE AUDIO",
            "END ON A RESOLVED VISUAL BEAT",
        ):
            assert rule in directive, f"missing rule: {rule}"

    def test_bans_montage_language(self):
        from cqc_lem.utilities.ai.content_framework import motion_prompt_directive
        directive = motion_prompt_directive().lower()
        for banned in ("no cuts", "no montage", "no 'then transition to'", "no 'b-roll'"):
            assert banned in directive, f"missing ban: {banned}"

    def test_bans_mood_and_film_stock_words(self):
        from cqc_lem.utilities.ai.content_framework import motion_prompt_directive
        directive = motion_prompt_directive().lower()
        for banned in ("no mood words", "no 'cinematic'", "no 'dynamic energy'",
                       "no film-stock"):
            assert banned in directive, f"missing ban: {banned}"

    def test_forbids_audio_handling(self):
        from cqc_lem.utilities.ai.content_framework import motion_prompt_directive
        assert "NEVER DESCRIBE AUDIO" in motion_prompt_directive()


class TestMotionPromptSystemPrompt:
    """The contract reaches the actual LLM system prompt."""

    def _system_text(self, create_mock):
        msgs = create_mock.call_args[1]["messages"]
        return msgs[0]["content"]

    def test_gen4_system_prompt_includes_contract(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            get_runway_ml_video_prompt_from_ai("post", "an office scene", model="gen4_turbo")
        sys = self._system_text(mock_openai_client.chat.completions.create)
        assert "MOTION-PROMPT CONTRACT" in sys
        assert "OPEN IN THE FIRST 1–2 SECONDS" in sys
        assert "ONE CONTINUOUS MOTION ONLY" in sys
        assert "NEVER DESCRIBE AUDIO" in sys

    def test_veo_system_prompt_includes_contract(self, mock_openai_client):
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            get_runway_ml_video_prompt_from_ai("post", "an office scene", model="veo3.1_fast")
        sys = self._system_text(mock_openai_client.chat.completions.create)
        assert "MOTION-PROMPT CONTRACT" in sys
        assert "MATCH THE TIER" in sys

    def test_contract_comes_after_existing_rules(self, mock_openai_client):
        """The directive extends the existing system prompt, not replacing it."""
        with patch("cqc_lem.utilities.ai.ai_helper.client", mock_openai_client):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            get_runway_ml_video_prompt_from_ai("post", "scene", model="gen4_turbo")
        sys = self._system_text(mock_openai_client.chat.completions.create)
        assert "Output only the motion prompt" in sys
        assert "MOTION-PROMPT CONTRACT" in sys
