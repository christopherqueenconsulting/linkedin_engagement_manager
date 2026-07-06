"""Unit tests for the anti-self-promo guardrail and focus/goal steering that keep generated
comments and posts aligned to the target post + the user's declared goals (never LEM-drift)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = '{"full_name": "Jane", "job_title": "COO"}'
    return p


def _messages(mock_call):
    """Pull the messages list passed to the mocked _call_llm."""
    kwargs = mock_call.call_args.kwargs
    return kwargs["messages"]


class TestFocusDirective:
    def test_includes_topics_and_goals(self):
        from cqc_lem.utilities.ai.ai_helper import _focus_directive
        d = _focus_directive({"focus_topics": ["AI", "B2B sales"],
                              "business_goals": "book calls", "personal_goals": "grow authority"})
        assert "AI" in d and "B2B sales" in d
        assert "book calls" in d and "grow authority" in d
        assert "never let it change the subject" in d

    def test_empty_without_focus(self):
        from cqc_lem.utilities.ai.ai_helper import _focus_directive
        assert _focus_directive(None) == ""
        assert _focus_directive({}) == ""
        assert _focus_directive({"focus_topics": [], "business_goals": None}) == ""


class TestIntentionDirective:
    def test_baseline_applies_when_empty(self):
        from cqc_lem.utilities.ai.ai_helper import _intention_directive
        d = _intention_directive(None)
        # Baseline relationship-building intention is present with everything left blank.
        assert "Build a genuine relationship" in d
        assert "POSTER" in d and "POTENTIAL FOLLOWERS" in d
        # No user steering was layered in.
        assert "Layer the user's declared focus" not in d

    def test_layers_user_focus_on_top_of_baseline(self):
        from cqc_lem.utilities.ai.ai_helper import _intention_directive
        d = _intention_directive({"focus_topics": ["fintech"], "business_goals": "book calls"})
        assert "Build a genuine relationship" in d          # baseline still present
        assert "Layer the user's declared focus" in d       # layered, not replaced
        assert "fintech" in d and "book calls" in d


class TestAlignmentDirective:
    def test_always_contains_guardrail(self):
        from cqc_lem.utilities.ai.ai_helper import _alignment_directive, _NO_SELF_PROMO_GUARDRAIL
        assert _NO_SELF_PROMO_GUARDRAIL in _alignment_directive(None)
        assert "LEM" in _alignment_directive(None)

    def test_appends_focus_when_present(self):
        from cqc_lem.utilities.ai.ai_helper import _alignment_directive
        d = _alignment_directive({"focus_topics": ["supply chain"]})
        assert "supply chain" in d


class TestGenerateAiResponseGrounding:
    def test_grounds_in_target_post_and_blocks_self_promo(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Nice — what changed your mind?")) as m:
            out = ai_helper.generate_ai_response(
                "POST ABOUT WAREHOUSE ROBOTICS", _profile(),
                prefs={"focus_topics": ["logistics"], "business_goals": "book calls"})
        assert out == "Nice — what changed your mind?"
        msgs = _messages(m)
        system = msgs[0]["content"]
        user_text = msgs[1]["content"][0]["text"]
        # Target post is the subject, profile is voice-only, guardrail present, focus steering present.
        assert "POST ABOUT WAREHOUSE ROBOTICS" in user_text
        assert "SUBJECT of your comment" in user_text
        assert "TONE and CREDIBILITY ONLY" in user_text
        assert "self-promotion" in system.lower()
        assert "never a pivot to the user's own projects" in system
        assert "logistics" in user_text and "book calls" in user_text
        # Baseline relationship-building intention is layered in alongside the user's focus.
        assert "Build a genuine relationship" in user_text

    def test_baseline_intention_when_no_focus_configured(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("What led you there?")) as m:
            ai_helper.generate_ai_response("A POST", _profile(), prefs=None)
        user_text = _messages(m)[1]["content"][0]["text"]
        # Even with nothing configured, the relationship-building default steers the comment.
        assert "Build a genuine relationship" in user_text
        assert "POTENTIAL FOLLOWERS" in user_text


class TestSeedAndThreadGuardrail:
    def test_seed_comment_has_guardrail(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Here's the backstory — thoughts?")) as m:
            ai_helper.generate_seed_comment("my post", _profile(), {"focus_topics": ["AI"]})
        system = _messages(m)[0]["content"]
        user_text = _messages(m)[1]["content"]
        assert "self-promotion" in system.lower()
        assert "AI" in user_text

    def test_thread_reply_has_guardrail(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Good point — how so?")) as m:
            ai_helper.generate_thread_reply("my post", "their comment", _profile(),
                                            {"business_goals": "grow pipeline"})
        system = _messages(m)[0]["content"]
        user_text = _messages(m)[1]["content"]
        assert "self-promotion" in system.lower()
        assert "grow pipeline" in user_text


class TestPostGeneratorAlignment:
    def test_thought_leadership_appends_alignment(self):
        from cqc_lem.utilities.ai import ai_helper
        trends = {"industry": "Logistics", "analysis": "robots everywhere"}
        with patch(f"{_AI}.get_industry_trend_analysis_based_on_user_profile", return_value=trends), \
             patch(f"{_AI}._call_llm", return_value=_resp("A viral post")) as m:
            out = ai_helper.get_thought_leadership_post_from_ai(
                _profile(), "awareness", prefs={"focus_topics": ["3PL efficiency"]})
        assert out == "A viral post"
        user_text = _messages(m)[1]["content"][0]["text"]
        assert "Content alignment rules" in user_text
        assert "3PL efficiency" in user_text
        assert "self-promotion" in user_text.lower()
