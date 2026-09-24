"""The DM content gate (issue #2099): meeting ask, fact grounding and slop lint on every DM."""

import pytest

from cqc_lem.utilities.ai.content_alignment import meeting_ask_excerpts, without_meeting_asks
from cqc_lem.utilities.ai.dm_nurture import dm_gate_directive, dm_gate_reasons

pytestmark = pytest.mark.unit

# DM 37 as it was SENT on 2026-09-16 (audit B-2/B-4).
_DM_37 = ("Your cybernetic AI work brings to mind a DoD LLM rollout that cut false positives 30%. "
          "Do you've 15 minutes for a quick call to swap ideas?")


class TestDm37:
    def test_blocked_for_the_meeting_ask_and_the_invented_claim(self):
        reasons = dm_gate_reasons(_DM_37)
        assert any(r.startswith("meeting_ask:") for r in reasons)
        assert "unsourced_claim: 30%" in reasons

    def test_still_blocked_for_the_claim_in_a_replied_thread(self):
        assert dm_gate_reasons(_DM_37, thread_replied=True) == ["unsourced_claim: 30%"]

    def test_dm_33_invented_figure_is_blocked(self):
        assert dm_gate_reasons("It helped us cut iteration time about 30%.") == [
            "unsourced_claim: 30%"]


class TestMeetingAsk:
    @pytest.mark.parametrize("text", [
        "Do you have 15 minutes for a quick call?",
        "Got 20 min to chat next week?",
        "Happy to jump on a brief call.",
        "Could you spare fifteen minutes for a chat?",
    ])
    def test_a_cold_thread_blocks_the_ask(self, text):
        assert meeting_ask_excerpts(text)
        assert any(r.startswith("meeting_ask:") for r in dm_gate_reasons(text))

    @pytest.mark.parametrize("text", [
        "The call ran 15 minutes for a change.",
        "I spent 15 minutes on a call with the vendor.",
        "We have 10 minutes for questions at the end.",
    ])
    def test_narrative_is_not_an_ask(self, text):
        assert meeting_ask_excerpts(text) == []

    @pytest.mark.parametrize("text", [
        "Would a short call be useful, or shall I send the one thing that helps most?",
        "Open to a quick chat Thursday?",
        "Can we find 15 minutes this week?",
        "Would a 20-minute call next week work?",
        "Worth a quick call?",
    ])
    def test_a_cold_thread_blocks_the_dm_question_forms(self, text):
        assert any(r.startswith("meeting_ask:") for r in dm_gate_reasons(text))
        # The same ask is allowed once they replied — and its "15"/"20" is not a claim.
        assert dm_gate_reasons(text, thread_replied=True) == []

    @pytest.mark.parametrize("text", [
        "I was open to a call but they went quiet.",
        "Would a team of 3 catch that? Probably.",
        "Thanks for connecting, what are you working on?",
    ])
    def test_a_statement_or_unrelated_question_is_not_an_ask(self, text):
        assert not any(r.startswith("meeting_ask:") for r in dm_gate_reasons(text, anchors=[text]))

    def test_the_ask_length_is_not_graded_as_a_claim(self):
        assert "15" not in without_meeting_asks("Do you have 15 minutes for a quick call?")
        assert dm_gate_reasons("Do you have 15 minutes for a quick call?", thread_replied=True) == []


class TestFactGate:
    def test_an_anchored_number_passes(self):
        assert dm_gate_reasons("We cut false positives 30%.",
                               anchors=["false positives down 30% at a client"]) == []

    def test_ops_can_turn_the_dm_fact_gate_down(self, monkeypatch):
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_DM", "warn")
        assert dm_gate_reasons("We cut false positives 30%.") == []


class TestSlop:
    def test_a_hard_slop_check_blocks(self):
        reasons = dm_gate_reasons("It's not just a tool, it's a mindset. Thoughts?")
        assert reasons and reasons[0].startswith("slop:")

    def test_a_clean_dm_passes(self):
        assert dm_gate_reasons("Thanks Jane, good to connect.") == []


def test_the_directive_names_every_reason():
    directive = dm_gate_directive(["meeting_ask: 15 minutes for a call", "unsourced_claim: 30%"])
    assert "15 minutes for a call" in directive and "30%" in directive
