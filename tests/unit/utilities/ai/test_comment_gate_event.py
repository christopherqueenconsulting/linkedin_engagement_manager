"""The comment gate reports its verdicts as ONE `comment_gate` event per draft.

The #1833 post-body check and the #1834 grounding check already decide the questions the comment
evals ask, but they recorded the answer only at DEBUG, and production logs at INFO. These tests pin
that every exit from the gate emits exactly one event, that the verdicts on it are the ones the
gate acted on, and that no post or draft text rides along.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.observability import EVENTS

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_DB_BANK = "cqc_lem.utilities.db.get_story_bank_entries"

_POST = ("Our support queue got worse after we added a second triage tier — median ticket age went "
         "from 3 days to 12. Nobody warns you that more routing means more waiting.")
_NO_NUMBERS = (
    "The second tier being the thing that slowed it down is the part most queue posts miss. "
    "We unwound ours for the same reason and the queue moved again. "
    "What finally made you unwind it?")
_INVENTED = (
    "The second tier being the thing that slowed it down is the part most queue posts miss. "
    "We logged 1,200 tickets a week before triage and 300 after, a 75% drop. "
    "What finally made you unwind it?")
_INVENTED_ONLY_CLAIM = (
    "We logged 1,200 tickets a week after the second triage tier and 300 before it, a 75% rise. "
    "What finally made you unwind it?")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("COMMENT_GATE_MAX_ATTEMPTS", "FACT_GROUNDING_SEVERITY",
                 "FACT_GROUNDING_SEVERITY_COMMENT"):
        monkeypatch.delenv(name, raising=False)


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = '{"full_name": "Jane", "job_title": "COO"}'
    return p


def _events(post, drafts):
    """Draft one feed comment against `post` and return (result, the comment_gate verdicts)."""
    from cqc_lem.utilities.ai import ai_helper
    with patch(f"{_AI}._call_llm", side_effect=[_resp(d) for d in drafts]), \
         patch(_DB_BANK, return_value=[]), \
         patch(f"{_AI}.track_comment_gate") as track:
        out = ai_helper.generate_ai_response(post, _profile(), user_id=7)
    assert track.call_count == 1, "every exit from the gate emits exactly one event"
    user_id, verdict = track.call_args.args
    assert user_id == 7
    return out, verdict


class TestEveryExitEmitsOnce:
    def test_a_clean_draft_ships_grounded(self):
        out, verdict = _events(_POST, [_NO_NUMBERS])
        assert out == _NO_NUMBERS
        assert verdict["outcome"] == "shipped"
        assert verdict["post_body"] == "readable"
        assert verdict["grounding"] == "grounded"
        assert verdict["ungrounded_count"] == 0
        assert verdict["attempts"] == 1
        assert verdict["failed_checks"] == []

    def test_an_unreadable_post_body_is_recorded_without_a_draft(self):
        out, verdict = _events("https://lnkd.in/gKabw7UJ", [_NO_NUMBERS])
        assert out is None
        assert verdict["outcome"] == "skipped_no_post_body"
        assert verdict["post_body"] == "unreadable"
        assert verdict["grounding"] == "unchecked"
        assert verdict["attempts"] == 0

    def test_an_invented_claim_that_is_rewritten_out_reads_as_stripped(self):
        out, verdict = _events(_POST, [_INVENTED])
        assert out is not None and "1,200" not in out
        assert verdict["outcome"] == "stripped"
        assert verdict["grounding"] == "stripped"
        assert verdict["ungrounded_attempts"] == 1
        assert "grounding" in verdict["failed_checks"]

    def test_a_draft_that_keeps_inventing_is_skipped_as_ungrounded(self, monkeypatch):
        monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", "2")
        out, verdict = _events(_POST, [_INVENTED_ONLY_CLAIM] * 2)
        assert out is None
        assert verdict["outcome"] == "skipped_gate"
        assert verdict["grounding"] == "ungrounded"
        assert verdict["ungrounded_count"] > 0
        assert verdict["ungrounded_attempts"] == 2
        assert verdict["attempts"] == verdict["max_attempts"] == 2

    def test_a_retry_that_recovers_ships_and_counts_the_bad_attempt(self):
        out, verdict = _events(_POST, [_INVENTED_ONLY_CLAIM, _NO_NUMBERS])
        assert out == _NO_NUMBERS
        assert verdict["outcome"] == "shipped"
        assert verdict["grounding"] == "grounded"
        assert verdict["ungrounded_attempts"] == 1
        assert verdict["attempts"] == 2

    def test_warn_severity_ships_the_claim_and_says_so(self, monkeypatch):
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_COMMENT", "warn")
        out, verdict = _events(_POST, [_INVENTED])
        assert out == _INVENTED
        assert verdict["outcome"] == "shipped"
        assert verdict["grounding"] == "ungrounded"

    def test_off_severity_reads_as_unchecked_never_as_grounded(self, monkeypatch):
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_COMMENT", "off")
        _, verdict = _events(_POST, [_INVENTED])
        assert verdict["grounding"] == "unchecked"
        assert verdict["ungrounded_count"] is None

    def test_an_empty_model_answer_reads_as_no_draft(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(None)), \
             patch(f"{_AI}.track_comment_gate") as track:
            assert ai_helper.generate_ai_response(_POST, _profile(), user_id=7) is None
        assert track.call_args.args[1]["outcome"] == "no_draft"


class TestNoContentLeaves:
    def test_the_event_carries_no_post_or_draft_text(self):
        _, verdict = _events(_POST, [_INVENTED])
        wire = repr(verdict)
        assert "support queue" not in wire and "1,200" not in wire

    def test_the_verdicts_a_dashboard_filters_on_are_labels(self):
        filtered = {f.name for f in EVENTS["comment_gate"].fields if f.filtered}
        assert {"surface", "outcome", "post_body", "grounding"} <= filtered

    def test_the_tracker_emits_the_declared_shape(self):
        from cqc_lem.utilities import observability
        with patch.object(observability.posthog, "capture") as capture, \
             patch.object(observability, "telemetry_muted", return_value=False):
            observability.track_comment_gate(7, {"outcome": "shipped", "grounding": "grounded",
                                                 "ungrounded_count": 0})
        kwargs = capture.call_args.kwargs
        assert kwargs["event"] == "comment_gate" and kwargs["distinct_id"] == "7"
        assert kwargs["properties"]["ungrounded_count"] == 0
        assert kwargs["properties"]["failed_checks"] == []
