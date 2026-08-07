"""Unit tests for DM reply-intent classification (issue #485) — the branch that decides whether a
lead's reply becomes a call proposal, an answered objection, a light touch, or a hard stop.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.ai.dm_nurture"


def _resp(text):
    r = MagicMock(); r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _classify(text, **kw):
    from cqc_lem.utilities.ai.dm_nurture import classify_reply_intent
    return classify_reply_intent(text, **kw)


class TestHeuristicIntents:
    @pytest.mark.parametrize("text", [
        "Sounds good — let's talk next week",
        "I'm interested, tell me more about how this works",
        "How much would something like this cost?",
        "Yes please, send me the details",
        "When are you free for a quick call?",
    ])
    def test_interested(self, text):
        got = _classify(text, use_llm=False)
        assert got["intent"] == "interested"
        assert got["method"] == "heuristic"

    @pytest.mark.parametrize("text", [
        "Honestly that's too expensive for us right now in terms of budget",
        "We already work with an agency on this",
        "How is this different from what everyone else offers?",
        "Not sure this would work for a team our size",
    ])
    def test_objection(self, text):
        assert _classify(text, use_llm=False)["intent"] == "objection"

    @pytest.mark.parametrize("text", [
        "Not right now, but keep me posted",
        "Let's circle back next quarter",
        "We're too busy with the launch at the moment",
    ])
    def test_not_now(self, text):
        assert _classify(text, use_llm=False)["intent"] == "not_now"

    @pytest.mark.parametrize("text", [
        "Not interested, thanks",
        "Please stop messaging me",
        "Remove me from your list",
        "This isn't a good fit for us",
    ])
    def test_disinterest(self, text):
        from cqc_lem.utilities.ai.dm_nurture import is_stop_intent
        got = _classify(text, use_llm=False)
        assert got["intent"] == "disinterest"
        assert is_stop_intent(got["intent"]) is True

    def test_disinterest_wins_over_not_now(self):
        # "not interested right now" matches BOTH families; the only safe reading is stop.
        assert _classify("Not interested right now", use_llm=False)["intent"] == "disinterest"

    def test_matched_evidence_is_the_phrase_that_fired(self):
        got = _classify("Please stop messaging me about this", use_llm=False)
        assert got["matched"] == ["stop messaging"]


class TestNeutralAndShortText:
    def test_too_short_is_neutral_without_any_llm_call(self):
        with patch(f"{_MOD}._llm_intent") as llm:
            got = _classify("ok")
        assert got == {"intent": "neutral", "matched": [], "method": "none"}
        llm.assert_not_called()

    def test_empty_is_neutral(self):
        assert _classify(None)["intent"] == "neutral"

    def test_llm_disabled_falls_back_to_neutral(self):
        got = _classify("Appreciate you reaching out about the report", use_llm=False)
        assert got == {"intent": "neutral", "matched": [], "method": "heuristic"}


class TestLlmTier:
    def test_llm_classifies_ambiguous_text(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp("Objection")):
            got = _classify("We looked at something similar last year and it went sideways")
        assert got["intent"] == "objection"
        assert got["method"] == "llm"

    def test_llm_answer_normalizes_hyphens_and_punctuation(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(" not-now. ")):
            assert _classify("Interesting timing on this one honestly")["intent"] == "not_now"

    def test_unrecognized_llm_answer_is_neutral(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp("maybe?")):
            assert _classify("Interesting timing on this one honestly")["intent"] == "neutral"

    def test_llm_failure_never_invents_disinterest(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("llm down")):
            got = _classify("Interesting timing on this one honestly")
        assert got["intent"] == "neutral"

    def test_env_flag_disables_the_llm_tier(self, monkeypatch):
        monkeypatch.setenv("DM_NURTURE_INTENT_LLM_ENABLED", "false")
        with patch(f"{_MOD}._llm_intent") as llm:
            assert _classify("Interesting timing on this one honestly")["intent"] == "neutral"
        llm.assert_not_called()

    def test_blank_env_keeps_the_default_on(self, monkeypatch):
        monkeypatch.setenv("DM_NURTURE_INTENT_LLM_ENABLED", "  ")
        with patch(f"{_MOD}._llm_intent", return_value=None) as llm:
            _classify("Interesting timing on this one honestly")
        llm.assert_called_once()


class TestBranchPolicy:
    def test_guidance_differs_per_intent(self):
        from cqc_lem.utilities.ai.dm_nurture import nurture_guidance
        interested = nurture_guidance("interested")
        not_now = nurture_guidance("not_now")
        assert "call" in interested.lower()
        assert interested != not_now
        assert "do not" in nurture_guidance("objection").lower() or "no " in nurture_guidance("objection").lower()

    def test_unknown_intent_falls_back_to_the_neutral_brief_not_a_pitch(self):
        from cqc_lem.utilities.ai.dm_nurture import nurture_guidance
        assert nurture_guidance("who_knows") == nurture_guidance("neutral")

    def test_not_now_waits_far_longer_than_interest(self):
        from cqc_lem.utilities.ai.dm_nurture import nurture_delay_hours
        assert nurture_delay_hours("interested") < nurture_delay_hours("objection") \
               < nurture_delay_hours("neutral") < nurture_delay_hours("not_now")

    def test_unknown_intent_delay_falls_back_to_neutral(self):
        from cqc_lem.utilities.ai.dm_nurture import nurture_delay_hours
        assert nurture_delay_hours("who_knows") == nurture_delay_hours("neutral")

    def test_only_disinterest_stops(self):
        from cqc_lem.utilities.ai.dm_nurture import is_stop_intent
        assert is_stop_intent("disinterest") is True
        assert not any(is_stop_intent(i) for i in ("interested", "objection", "not_now", "neutral"))
