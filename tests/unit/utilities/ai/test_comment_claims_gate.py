"""The comment claims gate (#2665): first-person events and numbers must be backed by the story bank.

Covers the deterministic claim parser, the `COMMENT_CLAIMS_GATE` modes, and the gate inside
`_gated_comment`. The LLM entailment call is mocked; the parser and the modes are not.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import story_bank as sb

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_POST = ("Our support queue got worse after we added a second triage tier — median ticket age went "
         "from 3 days to 12. Nobody warns you that more routing means more waiting.")
_OPEN = ("The second tier being the thing that slowed it down is the part most queue posts miss. "
         "Routing adds a handoff at every hop, and each handoff waits in line. ")
_CLAIM = "We unwound ours for the same reason and the queue moved again. "
_ASK = "What finally made you unwind it?"
_WITH_CLAIM = _OPEN + _CLAIM + _ASK
_COUNTERPOINT = _OPEN + "Capacity, not the number of tiers, is the variable that sets the wait. " + _ASK
_BANK_FACT = "We unwound our second triage tier in 2024 and the queue moved again."


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _verdicts(*entailed):
    return _resp(json.dumps({"verdicts": [{"n": i, "entailed": e}
                                          for i, e in enumerate(entailed, 1)]}))


def _gate(draft_text, monkeypatch, mode, llm, bank=(_BANK_FACT,), attempts="1"):
    from cqc_lem.utilities.ai import ai_helper
    monkeypatch.setenv("COMMENT_CLAIMS_GATE", mode)
    monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", attempts)
    with patch(f"{_AI}._call_llm", **llm) as call, \
         patch(f"{_AI}._story_bank_sources", return_value=list(bank)), \
         patch(f"{_AI}.track_comment_gate"):
        out = ai_helper._gated_comment(lambda _fix: draft_text, _POST, recent_comments=[],
                                       user_id=7, ground_specifics=True)
    return out, call


class TestClaimParser:
    def test_an_invented_event_without_a_number_is_a_claim(self):
        assert sb.first_person_claim_sentences("In a recent rollout I led, the queue moved again.")

    @pytest.mark.parametrize("text", [
        "We cut it by roughly thirty percent.", "We saved a third of the time.",
        "Half of our tickets vanished."])
    def test_spelled_quantities_in_a_first_person_sentence_are_claims(self, text):
        assert sb.first_person_claim_sentences(text) == [text]

    def test_a_counterpoint_with_no_claim_is_none(self):
        assert sb.first_person_claim_sentences(_COUNTERPOINT) == []

    def test_a_number_the_post_itself_states_is_not_a_claim(self):
        assert sb.first_person_claim_sentences("I read 3 days to 12 as the real story.", _POST) == []

    def test_dropping_a_sentence_keeps_the_rest(self):
        out = sb.drop_sentences(_WITH_CLAIM, [_CLAIM.strip()])
        assert _CLAIM.strip() not in out and out.endswith(_ASK) and out.startswith(_OPEN.strip())


class TestMode:
    def test_default_is_shadow_and_junk_falls_back_to_it(self, monkeypatch):
        monkeypatch.delenv("COMMENT_CLAIMS_GATE", raising=False)
        assert sb.claims_gate_mode() == "shadow"
        monkeypatch.setenv("COMMENT_CLAIMS_GATE", "junk")
        assert sb.claims_gate_mode() == "shadow"
        monkeypatch.setenv("COMMENT_CLAIMS_GATE", "ENFORCE")
        assert sb.claims_gate_mode() == "enforce"


class TestGate:
    def test_a_bank_backed_claim_passes_untouched(self, monkeypatch):
        out, call = _gate(_WITH_CLAIM, monkeypatch, "enforce", {"return_value": _verdicts(True)})
        assert out == _WITH_CLAIM and call.call_count == 1
        assert call.call_args.kwargs["model"] == "lem-simple"

    def test_an_invented_event_is_stripped(self, monkeypatch):
        out, _ = _gate(_WITH_CLAIM, monkeypatch, "enforce", {"return_value": _verdicts(False)})
        assert out is not None and "unwound" not in out and out.endswith(_ASK)

    def test_a_counterpoint_makes_no_llm_call(self, monkeypatch):
        out, call = _gate(_COUNTERPOINT, monkeypatch, "enforce", {"side_effect": AssertionError})
        assert out == _COUNTERPOINT and call.call_count == 0

    def test_an_empty_bank_strips_without_a_call(self, monkeypatch):
        out, call = _gate(_WITH_CLAIM, monkeypatch, "enforce", {"side_effect": AssertionError},
                          bank=())
        assert out is not None and "unwound" not in out and call.call_count == 0

    def test_a_strip_that_leaves_too_few_sentences_skips_the_draft(self, monkeypatch):
        out, _ = _gate(_CLAIM + _ASK, monkeypatch, "enforce", {"return_value": _verdicts(False)})
        assert out is None

    def test_enforce_fails_closed_when_the_verdict_is_unreadable(self, monkeypatch):
        out, _ = _gate(_WITH_CLAIM, monkeypatch, "enforce", {"return_value": _resp("not json")})
        assert out is None

    def test_enforce_fails_closed_when_the_call_raises(self, monkeypatch):
        out, _ = _gate(_WITH_CLAIM, monkeypatch, "enforce", {"side_effect": RuntimeError("down")})
        assert out is None

    def test_a_partial_verdict_fails_closed(self, monkeypatch):
        draft = _OPEN + _CLAIM + "We also rebuilt the rota last year. " + _ASK
        out, _ = _gate(draft, monkeypatch, "enforce", {"return_value": _verdicts(True)})
        assert out is None

    def test_shadow_changes_no_output_and_logs_would_strip(self, monkeypatch):
        with patch(f"{_AI}.log_info") as info:
            out, call = _gate(_WITH_CLAIM, monkeypatch, "shadow", {"return_value": _verdicts(False)})
        assert out == _WITH_CLAIM and call.call_count == 1
        assert any("would_strip=1 of 1" in c.args[0] for c in info.call_args_list)

    def test_shadow_survives_a_failed_judge(self, monkeypatch):
        out, _ = _gate(_WITH_CLAIM, monkeypatch, "shadow", {"side_effect": RuntimeError("down")})
        assert out == _WITH_CLAIM

    def test_off_restores_todays_gates(self, monkeypatch):
        out, call = _gate(_WITH_CLAIM, monkeypatch, "off", {"side_effect": AssertionError})
        assert out == _WITH_CLAIM and call.call_count == 0

    def test_a_surface_other_than_the_feed_comment_is_out_of_scope(self, monkeypatch):
        from cqc_lem.utilities.ai import ai_helper
        monkeypatch.setenv("COMMENT_CLAIMS_GATE", "enforce")
        with patch(f"{_AI}._call_llm", side_effect=AssertionError) as call, \
             patch(f"{_AI}.track_comment_gate"):
            out = ai_helper._gated_comment(lambda _f: _WITH_CLAIM, _POST, recent_comments=[],
                                           user_id=7, ground_specifics=False,
                                           surface="own_post_comment")
        assert call.call_count == 0 and out is not None
