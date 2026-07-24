"""Unit tests for the inbound buying-intent classifier — issue #483. Covers the heuristic families,
the cost gate (only ambiguous text reaches the LLM), the LLM tier's fail-closed behaviour, and the
env-tunable threshold."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_LI = "cqc_lem.utilities.ai.lead_intent"
_AI = "cqc_lem.utilities.ai.ai_helper"


def _detect(*args, **kwargs):
    from cqc_lem.utilities.ai.lead_intent import detect_lead_signals
    return detect_lead_signals(*args, **kwargs)


class TestStrongSignals:
    @pytest.mark.parametrize("text,family", [
        ("How much do you charge for this?", "pricing"),
        ("What would this cost for a team of 20?", "pricing"),
        ("Do you offer this as a done-for-you service?", "engage_us"),
        ("Can you help with our LinkedIn strategy?", "engage_us"),
        ("Just sent you a note — DM me when you have a minute", "contact_me"),
        ("Let's talk, I'd like to hear more about how you work", "contact_me"),
        ("We need help with our content pipeline", "stated_need"),
        ("I'm looking for someone who does exactly this", "stated_need"),
        ("Who do you recommend for this kind of work?", "referral"),
        ("How do I get started with this?", "demo_trial"),
    ])
    def test_flags_as_lead(self, text, family):
        result = _detect(text, use_llm=False)
        assert result["is_lead"] is True
        assert family in result["matched"]
        assert result["method"] == "heuristic"

    def test_score_accumulates_but_is_capped(self):
        text = ("How much does it cost? Do you offer this? DM me — we need help with this, "
                "and who do you recommend? I'm curious about your team.")
        assert _detect(text, use_llm=False)["score"] == 100


class TestNonLeads:
    @pytest.mark.parametrize("text", [
        "Great post, thanks for sharing!",
        "Congrats on the launch — this is amazing work.",
        "Totally agree. We saw the same thing last quarter.",
        "",
        None,
        "nice",          # below the minimum length
        "🔥🔥🔥",
    ])
    def test_not_a_lead(self, text):
        result = _detect(text, use_llm=False)
        assert result["is_lead"] is False
        assert result["score"] == 0

    def test_short_text_never_reaches_the_matcher(self):
        assert _detect("hi", use_llm=False)["method"] == "none"


class TestAmbiguousBand:
    _AMBIGUOUS = "Interested in how your team approaches this."

    def test_weak_only_is_below_threshold_without_llm(self):
        result = _detect(self._AMBIGUOUS, use_llm=False)
        assert result["is_lead"] is False
        assert 0 < result["score"] < 40

    def test_llm_promotes_an_ambiguous_hit(self):
        with patch(f"{_LI}._llm_says_lead", return_value=True) as llm:
            result = _detect(self._AMBIGUOUS)
        llm.assert_called_once()
        assert result["is_lead"] is True
        assert result["method"] == "llm"
        assert result["score"] >= 40

    def test_llm_no_keeps_it_out_of_the_inbox(self):
        with patch(f"{_LI}._llm_says_lead", return_value=False):
            result = _detect(self._AMBIGUOUS)
        assert result["is_lead"] is False

    def test_no_signal_at_all_never_pays_for_an_llm_call(self):
        with patch(f"{_LI}._llm_says_lead") as llm:
            _detect("Great post, thanks for sharing!")
        llm.assert_not_called()

    def test_strong_signal_never_pays_for_an_llm_call(self):
        with patch(f"{_LI}._llm_says_lead") as llm:
            assert _detect("How much do you charge?")["is_lead"] is True
        llm.assert_not_called()

    def test_llm_tier_can_be_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("LEAD_INTENT_LLM_ENABLED", "false")
        with patch(f"{_LI}._llm_says_lead") as llm:
            assert _detect(self._AMBIGUOUS)["is_lead"] is False
        llm.assert_not_called()


class TestLlmTier:
    def _response(self, content):
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content=content))]
        return resp

    def test_yes_is_a_lead(self):
        from cqc_lem.utilities.ai.lead_intent import _llm_says_lead, _LLM_MAX_TOKENS
        with patch(f"{_AI}._call_llm", return_value=self._response("Yes")) as call:
            assert _llm_says_lead("do you do this?") is True
        # _call_llm is what emits the PostHog llm_call event, so routing through it keeps this
        # high-volume classifier in the usage metrics — and the answer is one word.
        assert call.call_args.kwargs["model"] == "lem-simple"
        assert call.call_args.kwargs["max_tokens"] == _LLM_MAX_TOKENS

    def test_no_is_not_a_lead(self):
        from cqc_lem.utilities.ai.lead_intent import _llm_says_lead
        with patch(f"{_AI}._call_llm", return_value=self._response("no")):
            assert _llm_says_lead("nice work") is False

    def test_failure_fails_closed(self):
        from cqc_lem.utilities.ai.lead_intent import _llm_says_lead
        with patch(f"{_AI}._call_llm", side_effect=RuntimeError("proxy down")), \
                patch(f"{_LI}.log_warning") as warn:
            assert _llm_says_lead("maybe?") is False
        warn.assert_called_once()

    def test_empty_content_is_not_a_lead(self):
        from cqc_lem.utilities.ai.lead_intent import _llm_says_lead
        with patch(f"{_AI}._call_llm", return_value=self._response(None)):
            assert _llm_says_lead("hmm") is False


class TestThresholdEnv:
    def test_threshold_can_be_raised(self, monkeypatch):
        monkeypatch.setenv("LEAD_INTENT_MIN_SCORE", "90")
        assert _detect("How much do you charge?", use_llm=False)["is_lead"] is False

    def test_garbage_threshold_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("LEAD_INTENT_MIN_SCORE", "not-a-number")
        assert _detect("How much do you charge?", use_llm=False)["is_lead"] is True

    def test_long_text_is_truncated_before_matching(self):
        from cqc_lem.utilities.ai.lead_intent import _MAX_TEXT_CHARS
        text = ("x" * (_MAX_TEXT_CHARS + 50)) + " how much do you charge?"
        assert _detect(text, use_llm=False)["is_lead"] is False
