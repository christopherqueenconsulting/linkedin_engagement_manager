"""Unit tests for issue #1452 — a still-over-the-ceiling draft is HELD at generation time.

#1265 gave the post similarity gate its embedding-first measure and its one retry, but the
`similarity` finding that actually demotes a post only ran where a post history was handed to
`evaluate_post_gates` — the edit & re-score endpoint. So a semantic near-duplicate that survived its
retry auto-published.

The verdict the review gate reaches is now RECORDED on `posts.gate_reason` and re-read by the
generation-time gate pass, the same way the video probe records why a file was rejected. That keeps
the hold to the ONE embedding call and the ONE history read #1265 already pays: the gate pass never
measures similarity itself.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

# Both drafts carry a concrete first-person lived detail, so the A2-proof half of the review gate
# never fires and the similarity verdict is the only thing driving a retry.
_DRAFT = "I cut a client's onboarding from 12 days to 3."
_SECOND = "I moved a team off nightly batch jobs in 9 days."

_OVER = {"score": 0.84, "threshold": 0.78, "match": "an earlier post", "measure": "embedding",
         "too_similar": True}
_CLEAR = {"score": 0.31, "threshold": 0.78, "match": "an earlier post", "measure": "embedding",
          "too_similar": False}
_LEXICAL_OVER = {"score": 0.61, "threshold": 0.55, "match": "an earlier post", "measure": "lexical",
                 "too_similar": True}

_OTHER_FINDING = {"gate": "malformed_asset", "label": "Unusable media file", "score": None,
                  "threshold": None, "demoted": True, "explanation": "zero-byte file",
                  "remediation": "re-generate", "details": []}


def _review(verdicts, second=_SECOND, existing=None, write=None):
    """Drive `_review_generated_post` with a scripted list of similarity verdicts.

    `verdicts[0]` grades the first draft; `verdicts[1]` (when the gate regenerates) grades the
    retry. Returns (content, post_similarity_report mock, update_db_post_gate_reason mock).
    """
    from cqc_lem.app import run_content_plan as rcp
    sim = MagicMock(side_effect=list(verdicts))
    upd = write or MagicMock()
    retry = (patch(f"{_RCP}.create_text_post", side_effect=second)
             if isinstance(second, Exception) else
             patch(f"{_RCP}.create_text_post", return_value=second))
    with patch(f"{_RCP}.post_similarity_report", sim), \
         patch(f"{_RCP}.get_post_gate_reason", return_value=list(existing or [])), \
         patch(f"{_RCP}.update_db_post_gate_reason", upd), \
         patch(f"{_RCP}._check_post_alignment", return_value=True), \
         retry:
        out = rcp._review_generated_post(
            1, "awareness", "thought_leadership", MagicMock(), {}, 77, "", _DRAFT, ["an earlier post"],
            prefs={}, profile_synthesis="", story=None, story_directive="STORY DIRECTIVE")
    return out, sim, upd


def _written(upd):
    """The findings list the review gate persisted (post_id, findings)."""
    upd.assert_called_once()
    return upd.call_args.args[1]


def _gate_pass(recorded, post_id=77, gates_raise=False):
    """Drive `_gate_findings_for_post` with `recorded` already on the post's gate reason."""
    from cqc_lem.app import run_content_plan as rcp
    sim = MagicMock(side_effect=AssertionError("the gate pass must not measure similarity"))
    evaluate = (patch(f"{_RCP}.evaluate_post_gates", side_effect=RuntimeError("gates down"))
                if gates_raise else patch(f"{_RCP}._post_missing_required_asset", return_value=False))
    with patch(f"{_RCP}.post_similarity_report", sim), \
         patch(f"{_RCP}.get_post_gate_reason", return_value=list(recorded)), \
         patch(f"{_RCP}.get_post_authenticity_score", return_value=90), \
         patch(f"{_RCP}._engagement_prefs_or_empty", return_value={}), \
         patch(f"{_RCP}.get_lead_magnet_settings", return_value={"enabled": False}), \
         patch("cqc_lem.utilities.db.get_post_archetype", return_value="personal_lesson"), \
         evaluate:
        return rcp._gate_findings_for_post(1, post_id, "A clean enough draft.", "text")


class TestTheReviewGateRecordsItsVerdict:
    def test_a_still_over_draft_is_recorded_as_a_demoting_finding(self):
        out, _, upd = _review([_OVER, _OVER])
        assert out == _SECOND
        findings = _written(upd)
        assert [f["gate"] for f in findings] == ["similarity"]
        assert findings[0]["demoted"] is True

    def test_the_recorded_finding_names_the_measure_that_fired(self):
        _, _, upd = _review([_LEXICAL_OVER, _LEXICAL_OVER])
        explanation = _written(upd)[0]["explanation"]
        # The lexical wording is "% of the wording", the cosine one "semantic match" — a reader
        # cannot compare the two scales without being told which produced the number.
        assert "semantic match" not in explanation
        assert _written(upd)[0]["score"] == 0.61

    def test_a_retry_that_cleared_the_ceiling_records_nothing_to_hold(self):
        out, _, upd = _review([_OVER, _CLEAR])
        assert out == _SECOND
        # Nothing was on the post and the shipped draft is clean — the common path never writes.
        upd.assert_not_called()

    def test_a_clean_first_draft_never_regenerates_or_writes(self):
        out, sim, upd = _review([_CLEAR])
        assert out == _DRAFT
        assert sim.call_count == 1
        upd.assert_not_called()

    def test_a_verdict_left_by_an_earlier_draft_is_cleared(self):
        # regenerate_post reuses the row: a stale hold would survive a rewrite that fixed the
        # duplication and the post could never publish.
        stale = [dict(_OTHER_FINDING), {"gate": "similarity", "demoted": True, "explanation": "old",
                                        "remediation": "", "details": [], "score": 0.9,
                                        "threshold": 0.78, "label": "Near-duplicate"}]
        _, _, upd = _review([_CLEAR], existing=stale)
        assert [f["gate"] for f in _written(upd)] == ["malformed_asset"]

    def test_another_gates_finding_survives_the_write(self):
        _, _, upd = _review([_OVER, _OVER], existing=[dict(_OTHER_FINDING)])
        assert [f["gate"] for f in _written(upd)] == ["malformed_asset", "similarity"]

    def test_a_failed_retry_records_the_first_drafts_verdict(self):
        # The first draft is what ships, so ITS verdict is the one the gate pass must read.
        out, sim, upd = _review([_OVER], second=RuntimeError("llm down"))
        assert out == _DRAFT
        assert sim.call_count == 1
        assert [f["gate"] for f in _written(upd)] == ["similarity"]

    def test_an_unwritable_gate_reason_costs_the_hold_not_the_post(self):
        out, _, upd = _review([_OVER, _OVER], write=MagicMock(side_effect=RuntimeError("no db")))
        assert out == _SECOND
        upd.assert_called_once()

    def test_a_preview_with_no_post_row_records_nothing(self):
        from cqc_lem.app import run_content_plan as rcp
        upd = MagicMock()
        with patch(f"{_RCP}.post_similarity_report", return_value=dict(_OVER)), \
             patch(f"{_RCP}.get_post_gate_reason") as read, \
             patch(f"{_RCP}.update_db_post_gate_reason", upd), \
             patch(f"{_RCP}._check_post_alignment", return_value=True), \
             patch(f"{_RCP}.create_text_post", return_value=_SECOND):
            rcp._review_generated_post(1, "awareness", "thought_leadership", MagicMock(), {}, None,
                                       "", _DRAFT, ["an earlier post"], prefs={},
                                       profile_synthesis="", story=None, story_directive="")
        read.assert_not_called()
        upd.assert_not_called()


class TestTheGenerationGatePassReadsIt:
    _RECORDED = [{"gate": "similarity", "label": "Near-duplicate", "score": 0.84,
                  "threshold": 0.78, "demoted": True, "explanation": "says the same thing",
                  "remediation": "change the angle", "details": []}]

    def test_the_recorded_verdict_holds_the_post(self):
        from cqc_lem.utilities.quality_gates import demoting_findings
        findings = _gate_pass(self._RECORDED)
        assert [f["gate"] for f in findings] == ["similarity"]
        assert demoting_findings(findings)

    def test_no_recorded_verdict_means_no_similarity_finding(self):
        assert _gate_pass([]) == []

    def test_another_gates_note_on_the_post_is_not_mistaken_for_one(self):
        assert _gate_pass([dict(_OTHER_FINDING)]) == []

    def test_the_gate_pass_never_measures_similarity_again(self):
        # The AssertionError side effect in `_gate_pass` is the assertion: a second
        # post_similarity_report call here is a second lem-embedding call for one draft.
        assert [f["gate"] for f in _gate_pass(self._RECORDED)] == ["similarity"]

    def test_a_gate_evaluation_failure_still_yields_the_recorded_hold(self):
        # The near-duplicate verdict is already measured and already persisted — losing it to an
        # unrelated gate fault would auto-publish the duplicate.
        assert [f["gate"] for f in _gate_pass(self._RECORDED, gates_raise=True)] == ["similarity"]

    def test_an_unreadable_gate_reason_costs_the_hold_not_the_post(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_post_gate_reason", side_effect=RuntimeError("no db")):
            assert rcp._recorded_similarity_finding(77) == []


class TestRescoreStillMeasuresLive:
    def test_a_rescore_does_not_replay_the_recorded_verdict(self):
        # The author edited the text — the whole point of a re-score is to grade what they wrote,
        # so a verdict from the draft they replaced must never hold the edit.
        from cqc_lem.app import run_content_plan as rcp
        recorded = [{"gate": "similarity", "label": "Near-duplicate", "score": 0.84,
                     "threshold": 0.78, "demoted": True, "explanation": "old draft",
                     "remediation": "", "details": []}]
        with patch(f"{_RCP}.get_post_content", return_value="An edit with nothing in common."), \
             patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_type", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_video_url", return_value=None), \
             patch("cqc_lem.utilities.db.get_post_status", return_value="pending"), \
             patch("cqc_lem.utilities.db.get_post_archetype", return_value="personal_lesson"), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=recorded), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}._engagement_prefs_or_empty", return_value={}), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}._score_and_persist_authenticity"), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=90), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RCP}._persist_gate_findings"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}.update_db_post_status"):
            result = rcp.rescore_post(7)
        assert [f["gate"] for f in result["findings"]] == []
        assert result["passed"] is True
