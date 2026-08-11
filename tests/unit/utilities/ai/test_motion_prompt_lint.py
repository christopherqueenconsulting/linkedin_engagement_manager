"""Deterministic motion-prompt linter (issue #1277) — the CHECKING half of the #1140 contract.

These tests pin three things the feature is worthless without: every banned family actually fires,
the default is WARN-ONLY (no render is held and no extra LLM call is spent until the
`video-motion-lint-hold` flag is flipped), and one telemetry row is emitted per graded prompt.
"""

import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# A prompt that satisfies the contract: one continuous camera move, a subject beat in the opening
# sentence, no mood words, nothing about audio.
CLEAN = ("Slow push-in toward the founder at her standing desk. She looks up from the laptop and "
         "holds eye contact. Papers shift gently in the background.")


@pytest.fixture(autouse=True)
def _lint_defaults(monkeypatch):
    """Grade with the shipped defaults — no severity overrides leaking in from the environment."""
    for key in list(os.environ):
        if key.startswith("SLOP_LINT_SEVERITY_MOTION") or key.startswith("MOTION_PROMPT_LINT"):
            monkeypatch.delenv(key, raising=False)


class TestBannedPatterns:
    """Each banned family fires, and a contract-clean prompt fires nothing."""

    def test_clean_prompt_passes_with_no_violations(self):
        from cqc_lem.utilities.ai.slop_lint import motion_prompt_report
        report = motion_prompt_report(CLEAN, model="gen4_turbo")
        assert report["checked"] is True
        assert report["passes"] is True
        assert report["violations"] == []

    @pytest.mark.parametrize("phrase", ["cut to", "cuts to", "then transition to", "b-roll",
                                        "montage", "jump cut", "split screen", "fade out",
                                        "fade to black"])
    def test_montage_language_fires(self, phrase):
        from cqc_lem.utilities.ai.slop_lint import MOTION_CHECK_MONTAGE, motion_prompt_report
        report = motion_prompt_report(f"{CLEAN} Then {phrase} the product on the desk.")
        assert MOTION_CHECK_MONTAGE in report["checks"]
        assert report["passes"] is False

    def test_a_renderable_fade_is_not_an_editorial_one(self):
        """"the background fades softly" is a motion; only "fade out"/"fade to black" are edits."""
        from cqc_lem.utilities.ai.slop_lint import MOTION_CHECK_MONTAGE, motion_prompt_report
        report = motion_prompt_report(f"{CLEAN} The background light fades softly.")
        assert MOTION_CHECK_MONTAGE not in report["checks"]

    @pytest.mark.parametrize("phrase", ["cinematic", "dynamic energy", "epic", "film grain",
                                        "35mm", "breathtaking", "4k"])
    def test_mood_and_film_stock_words_fire(self, phrase):
        from cqc_lem.utilities.ai.slop_lint import MOTION_CHECK_MOOD, motion_prompt_report
        report = motion_prompt_report(f"{CLEAN} The look is {phrase}.")
        assert MOTION_CHECK_MOOD in report["checks"]
        assert report["passes"] is False

    @pytest.mark.parametrize("phrase", ["voiceover", "narration", "dialogue", "soundtrack",
                                        "music", "lyrics"])
    def test_audio_language_fires(self, phrase):
        from cqc_lem.utilities.ai.slop_lint import MOTION_CHECK_AUDIO, motion_prompt_report
        report = motion_prompt_report(f"{CLEAN} A soft {phrase} plays over the shot.")
        assert MOTION_CHECK_AUDIO in report["checks"]

    def test_missing_opening_window_signal_fires_as_a_warning(self):
        from cqc_lem.utilities.ai.slop_lint import (
            MOTION_CHECK_OPENING,
            SEVERITY_WARN,
            motion_prompt_report,
        )
        report = motion_prompt_report("The office is bright and quiet. She smiles.")
        assert MOTION_CHECK_OPENING in report["checks"]
        # The opening check is an allow-list heuristic, so it must never hold a render on its own.
        assert report["passes"] is True
        assert report["violations"][0]["severity"] == SEVERITY_WARN

    def test_short_banned_token_needs_a_word_boundary(self):
        """"4k" must not fire inside a longer token — the check greps words, not substrings."""
        from cqc_lem.utilities.ai.slop_lint import MOTION_CHECK_MOOD, motion_prompt_report
        report = motion_prompt_report(f"{CLEAN} The camera passes a 4kg dumbbell.")
        assert MOTION_CHECK_MOOD not in report["checks"]

    def test_appended_audio_direction_is_never_graded(self):
        """The appended clause is excluded from grading.

        The deterministic #548 clause legitimately says "no voiceover", so grading it would make the
        fix for one defect fire the audio check on every audio-capable render.
        """
        from cqc_lem.utilities.ai.ai_helper import _audio_direction
        from cqc_lem.utilities.ai.slop_lint import motion_prompt_report
        prompt = f"{CLEAN} {_audio_direction('veo3.1')}"
        assert motion_prompt_report(prompt, model="veo3.1")["violations"] == []

    def test_writer_authored_audio_still_fires_under_the_same_marker(self):
        """A writer that ignores the contract answers with "Audio:" too — and must still be caught.

        Only LEM's own clause (marker + `AUDIO_DIRECTION_LEAD`) is excluded from grading; keying
        that exclusion on the marker alone hid the exact violation this check exists for.
        """
        from cqc_lem.utilities.ai.ai_helper import _audio_direction
        from cqc_lem.utilities.ai.slop_lint import MOTION_CHECK_AUDIO, motion_prompt_report
        from cqc_lem.utilities.ai.video_models import AUDIO_DIRECTION_MARKER
        written = f"{CLEAN} {AUDIO_DIRECTION_MARKER} a warm voiceover narrates over soft music."
        assert MOTION_CHECK_AUDIO in motion_prompt_report(written, model="veo3.1")["checks"]
        # ...and it is still caught once the deterministic clause is appended behind it.
        both = f"{written} {_audio_direction('veo3.1')}"
        assert MOTION_CHECK_AUDIO in motion_prompt_report(both, model="veo3.1")["checks"]

    def test_empty_and_disabled_fail_open(self, monkeypatch):
        from cqc_lem.utilities.ai.slop_lint import motion_prompt_report
        assert motion_prompt_report("")["checked"] is False
        assert motion_prompt_report("")["passes"] is True
        monkeypatch.setenv("MOTION_PROMPT_LINT_ENABLED", "false")
        off = motion_prompt_report("A cinematic montage, then cuts to the product.")
        assert off["checked"] is False
        assert off["passes"] is True

    def test_severity_is_promotable_per_check_without_a_deploy(self, monkeypatch):
        from cqc_lem.utilities.ai.slop_lint import SEVERITY_HARD, motion_prompt_report
        monkeypatch.setenv("SLOP_LINT_SEVERITY_MOTION_OPENING", "hard")
        report = motion_prompt_report("The office is bright and quiet. She smiles.")
        assert report["violations"][0]["severity"] == SEVERITY_HARD
        assert report["passes"] is False


class TestSharedContract:
    """The writer side and the checking side read ONE list — POST_BANNED_SCAFFOLDS' invariant."""

    def test_directive_names_every_banned_phrase_the_checker_greps(self):
        from cqc_lem.utilities.ai.content_framework import (
            MOTION_BANNED_AUDIO,
            MOTION_BANNED_MONTAGE,
            MOTION_BANNED_MOOD,
            motion_prompt_directive,
        )
        directive = motion_prompt_directive().lower()
        for phrase in MOTION_BANNED_MONTAGE + MOTION_BANNED_MOOD + MOTION_BANNED_AUDIO:
            assert phrase in directive, f"writer contract never names: {phrase}"


class TestVerdict:
    """hold vs regenerate vs warn — and the fact that only the flag can reach the first two."""

    def _dirty(self):
        from cqc_lem.utilities.ai.slop_lint import motion_prompt_report
        return motion_prompt_report("A cinematic scene that cuts to the product.")

    def test_warn_only_when_enforcement_is_off(self):
        from cqc_lem.utilities.ai.slop_lint import MOTION_VERDICT_WARN, motion_prompt_verdict
        report = self._dirty()
        assert report["hard"], "fixture must carry a HARD violation"
        assert motion_prompt_verdict(report, enforced=False, attempt=1) == MOTION_VERDICT_WARN

    def test_regenerate_then_hold_when_enforced(self):
        from cqc_lem.utilities.ai.slop_lint import (
            MOTION_VERDICT_HOLD,
            MOTION_VERDICT_REGENERATE,
            motion_prompt_verdict,
        )
        report = self._dirty()
        assert motion_prompt_verdict(report, enforced=True, attempt=1,
                                     max_attempts=2) == MOTION_VERDICT_REGENERATE
        assert motion_prompt_verdict(report, enforced=True, attempt=2,
                                     max_attempts=2) == MOTION_VERDICT_HOLD

    def test_warn_severity_alone_never_regenerates_even_when_enforced(self):
        from cqc_lem.utilities.ai.slop_lint import (
            MOTION_VERDICT_WARN,
            motion_prompt_report,
            motion_prompt_verdict,
        )
        report = motion_prompt_report("The office is bright and quiet. She smiles.")
        assert motion_prompt_verdict(report, enforced=True, attempt=1) == MOTION_VERDICT_WARN

    def test_pass_and_unchecked(self):
        from cqc_lem.utilities.ai.slop_lint import (
            MOTION_VERDICT_PASS,
            MOTION_VERDICT_UNCHECKED,
            motion_prompt_report,
            motion_prompt_verdict,
        )
        assert motion_prompt_verdict(motion_prompt_report(CLEAN),
                                     enforced=True) == MOTION_VERDICT_PASS
        assert motion_prompt_verdict(motion_prompt_report("")) == MOTION_VERDICT_UNCHECKED

    def test_retry_directive_names_each_pattern_that_fired(self):
        from cqc_lem.utilities.ai.slop_lint import motion_retry_directive
        steer = motion_retry_directive(self._dirty()["hard"])
        assert "cinematic" in steer and "cuts to" in steer
        assert motion_retry_directive([]) == ""


class TestGeneratorWiring:
    """`get_runway_ml_video_prompt_from_ai` grades what it returns, and only the flag changes it."""

    def _mock_client(self, mock_openai_client, text):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = text
        return mock_openai_client

    def test_warn_only_default_returns_the_prompt_and_spends_one_call(self, mock_openai_client):
        dirty = "A cinematic scene that cuts to the product."
        client = self._mock_client(mock_openai_client, dirty)
        with patch("cqc_lem.utilities.ai.ai_helper.client", client), \
             patch("cqc_lem.utilities.ai.ai_helper.flag_enabled", return_value=False), \
             patch("cqc_lem.utilities.ai.ai_helper.track_motion_prompt_check") as track:
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            out = get_runway_ml_video_prompt_from_ai("post", "an office", model="gen4_turbo")
        assert out == dirty
        assert client.chat.completions.create.call_count == 1
        assert track.call_count == 1
        assert track.call_args[1]["verdict"] == "warn"
        assert track.call_args[1]["enforced"] is False

    def test_enforced_regenerates_once_then_holds(self, mock_openai_client):
        client = self._mock_client(mock_openai_client, "A cinematic scene that cuts to the product.")
        with patch("cqc_lem.utilities.ai.ai_helper.client", client), \
             patch("cqc_lem.utilities.ai.ai_helper.flag_enabled", return_value=True), \
             patch("cqc_lem.utilities.ai.ai_helper.track_motion_prompt_check"):
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            from cqc_lem.utilities.ai.slop_lint import MotionPromptHeld
            with pytest.raises(MotionPromptHeld) as held:
                get_runway_ml_video_prompt_from_ai("post", "an office", model="gen4_turbo")
        assert client.chat.completions.create.call_count == 2
        assert "motion_mood" in held.value.report["checks"]

    def test_enforced_retry_carries_the_steer_and_a_clean_rewrite_ships(self, mock_openai_client):
        drafts = ["A cinematic scene that cuts to the product.", CLEAN]
        client = mock_openai_client

        def _next(*args, **kwargs):
            response = client.chat.completions.create.return_value
            response.choices[0].message.content = drafts.pop(0)
            return response

        client.chat.completions.create.side_effect = _next
        with patch("cqc_lem.utilities.ai.ai_helper.client", client), \
             patch("cqc_lem.utilities.ai.ai_helper.flag_enabled", return_value=True), \
             patch("cqc_lem.utilities.ai.ai_helper.track_motion_prompt_check") as track:
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            out = get_runway_ml_video_prompt_from_ai("post", "an office", model="gen4_turbo")
        assert out == CLEAN
        assert client.chat.completions.create.call_count == 2
        retry_system = client.chat.completions.create.call_args[1]["messages"][0]["content"]
        assert "YOUR PREVIOUS MOTION PROMPT VIOLATED THE CONTRACT" in retry_system
        assert [c[1]["verdict"] for c in track.call_args_list] == ["regenerate", "pass"]

    def test_disabled_lint_grades_nothing_and_emits_nothing(self, mock_openai_client, monkeypatch):
        """The kill switch is the kill switch: no grading, no event, prompt unchanged."""
        dirty = "A cinematic scene that cuts to the product."
        client = self._mock_client(mock_openai_client, dirty)
        monkeypatch.setenv("MOTION_PROMPT_LINT_ENABLED", "false")
        with patch("cqc_lem.utilities.ai.ai_helper.client", client), \
             patch("cqc_lem.utilities.ai.ai_helper.flag_enabled", return_value=True), \
             patch("cqc_lem.utilities.ai.ai_helper.track_motion_prompt_check") as track:
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            out = get_runway_ml_video_prompt_from_ai("post", "an office", model="gen4_turbo")
        assert out == dirty
        assert client.chat.completions.create.call_count == 1
        assert track.call_count == 0

    def test_audio_clause_is_appended_after_grading(self, mock_openai_client):
        client = self._mock_client(mock_openai_client, CLEAN)
        with patch("cqc_lem.utilities.ai.ai_helper.client", client), \
             patch("cqc_lem.utilities.ai.ai_helper.flag_enabled", return_value=True), \
             patch("cqc_lem.utilities.ai.ai_helper.track_motion_prompt_check") as track:
            from cqc_lem.utilities.ai.ai_helper import get_runway_ml_video_prompt_from_ai
            out = get_runway_ml_video_prompt_from_ai("post", "an office", model="veo3.1")
        from cqc_lem.utilities.ai.video_models import AUDIO_DIRECTION_MARKER
        assert AUDIO_DIRECTION_MARKER in out
        # The clause rides on the RETURNED prompt but was never part of what was graded.
        assert track.call_args[1]["verdict"] == "pass"


class TestTelemetry:
    """One `motion_prompt_check` row per graded prompt, carrying no prompt body."""

    def test_event_shape(self):
        from cqc_lem.utilities.ai.slop_lint import motion_prompt_report, motion_prompt_verdict
        from cqc_lem.utilities.observability import track_motion_prompt_check
        report = motion_prompt_report("A cinematic scene that cuts to the product.",
                                      model="gen4_turbo")
        verdict = motion_prompt_verdict(report, enforced=False, attempt=1)
        with patch("cqc_lem.utilities.observability.posthog.capture") as capture:
            track_motion_prompt_check(report, verdict=verdict, model="gen4_turbo", attempt=1,
                                      enforced=False, user_id=7, post_id=42)
        props = capture.call_args[1]["properties"]
        assert capture.call_args[1]["event"] == "motion_prompt_check"
        assert capture.call_args[1]["distinct_id"] == "7"
        assert props["verdict"] == "warn" and props["enforced"] is False
        assert props["model"] == "gen4_turbo" and props["post_id"] == 42
        assert props["hard_count"] == 2 and props["checks"]
        assert "cinematic" in props["evidence"]
        assert "product" not in str(props), "the prompt body must never be sent"

    def test_unchecked_report_still_reports_honestly(self):
        from cqc_lem.utilities.observability import track_motion_prompt_check
        with patch("cqc_lem.utilities.observability.posthog.capture") as capture:
            track_motion_prompt_check({}, verdict="unchecked")
        props = capture.call_args[1]["properties"]
        assert props["checked"] is None and props["verdict"] == "unchecked"
        assert props["checks"] == [] and props["hard_count"] == 0


class TestFlagRegistration:
    """The enforcement toggle is a REGISTERED flag with an env fallback, OFF by default."""

    def test_registered_and_off_by_default(self, monkeypatch):
        from cqc_lem.utilities import flags
        monkeypatch.delenv("VIDEO_MOTION_LINT_HOLD_ENABLED", raising=False)
        spec = flags.FLAGS[flags.VIDEO_MOTION_LINT_HOLD]
        assert spec.env_var == "VIDEO_MOTION_LINT_HOLD_ENABLED"
        assert spec.default is False
        assert flags.env_default(flags.VIDEO_MOTION_LINT_HOLD) is False
        monkeypatch.setenv("VIDEO_MOTION_LINT_HOLD_ENABLED", "true")
        assert flags.env_default(flags.VIDEO_MOTION_LINT_HOLD) is True
