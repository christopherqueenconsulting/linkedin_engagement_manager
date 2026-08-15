"""Slide text gets a text-quality reading, and it is advisory (issue #1512).

Before this, a carousel's SLIDES ran one grader (`deck_reference_report`) plus an advisory
fact-grounding log, while the caption ran the whole gate suite — so on the one format where the
slides ARE the post, a banned scaffold opener or a bait closer on a slide was recorded nowhere.

These tests pin the three things that make the reading trustworthy: it is the EXISTING
`slop_lint_report` (never a carousel-only linter), the result survives to the review queue even
though the slide text is gone by the time the gate pass runs, and it holds nothing.
"""

from unittest.mock import patch

import pytest

import cqc_lem.app.run_content_plan as rcp
from cqc_lem.utilities.quality_gates import (
    GATE_SLIDE_SLOP,
    authenticity_finding,
    demoting_findings,
    slide_slop_finding,
)

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

# A clean deck: plain sentences, real specifics, no scaffold, no manufactured beat.
_CLEAN_DECK = {
    "cover": {"title": "Deploy time", "content": "We cut it from 41 minutes to 9."},
    "insights": [
        {"title": "Build cache", "content": "One shared cache took 14 minutes off the build."},
        {"title": "Parallel jobs", "content": "Two runners, split by test lane."},
    ],
    "call_to_action": {"title": "Save this", "content": "Keep it for your next pipeline review."},
}

# The same deck with a POST_BANNED_SCAFFOLDS opener on a body slide.
_SCAFFOLD_DECK = {
    "cover": {"title": "Deploy time", "content": "We cut it from 41 minutes to 9."},
    "insights": [
        {"title": "Build cache",
         "content": "In my experience as a consultant, one shared cache took 14 minutes off."},
    ],
}


def _lint(deck, post_id=7, keyword=None):
    """Run the reporter with the DB seams mocked; returns the `update_db_post_gate_reason` mock."""
    with patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
         patch(f"{_RCP}.update_db_post_gate_reason") as store, \
         patch(f"{_RCP}._cta_keyword_for", return_value=keyword):
        rcp._report_carousel_slide_slop(1, post_id, deck)
    return store


class TestDeckTextIsWhatGetsGraded:
    def test_every_slide_including_the_cover_and_the_cta_is_in_the_graded_text(self):
        # `deck_slides` marks cover/CTA ungraded for the REFERENCE gate (they cannot carry a
        # reusable artifact) — but a bait closer lives exactly there, so the text check reads them.
        text = rcp._deck_text(_CLEAN_DECK)
        assert "Deploy time" in text and "Keep it for your next pipeline review." in text
        assert "Two runners, split by test lane." in text

    def test_a_deck_with_no_slides_is_empty_text(self):
        assert rcp._deck_text({"headline": "not a slide"}) == ""


class TestSlideTextIsGradedByTheExistingLinter:
    def test_it_calls_slop_lint_report_on_the_post_surface(self):
        with patch(f"{_RCP}.slop_lint_report",
                   return_value={"checked": True, "passes": True, "violations": [], "hard": [],
                                 "warnings": []}) as lint, \
             patch(f"{_RCP}._cta_keyword_for", return_value=None), \
             patch(f"{_RCP}._record_slide_slop_finding"):
            rcp._report_carousel_slide_slop(1, 7, _CLEAN_DECK)
        assert lint.call_args[0][1] == "post"
        assert "One shared cache took 14 minutes off the build." in lint.call_args[0][0]

    def test_the_sanctioned_lead_magnet_keyword_is_exempted_the_way_the_caption_is(self):
        with patch(f"{_RCP}.slop_lint_report",
                   return_value={"checked": True, "passes": True, "violations": [], "hard": [],
                                 "warnings": []}) as lint, \
             patch(f"{_RCP}._cta_keyword_for", return_value="BLUEPRINT"), \
             patch(f"{_RCP}._record_slide_slop_finding"):
            rcp._report_carousel_slide_slop(1, 7, _CLEAN_DECK)
        assert lint.call_args[1]["exempt_keyword"] == "BLUEPRINT"

    def test_a_disabled_linter_records_nothing(self):
        with patch(f"{_RCP}.slop_lint_report",
                   return_value={"checked": False, "passes": True, "violations": [], "hard": [],
                                 "warnings": []}), \
             patch(f"{_RCP}._cta_keyword_for", return_value=None), \
             patch(f"{_RCP}._record_slide_slop_finding") as record:
            rcp._report_carousel_slide_slop(1, 7, _CLEAN_DECK)
        record.assert_not_called()


class TestWhatARealDeckRecords:
    def test_a_clean_deck_records_no_violation(self):
        assert not _lint(_CLEAN_DECK).called

    def test_a_deck_with_a_banned_scaffold_records_one(self):
        findings = _lint(_SCAFFOLD_DECK).call_args[0][1]
        assert [f["gate"] for f in findings] == [GATE_SLIDE_SLOP]
        assert any("scaffold" in d.lower() for d in findings[0]["details"])

    def test_the_advisory_path_changes_no_post_status(self):
        # The finding is recorded, and nothing in it can demote the post: `demoting_findings`
        # is what the content-plan status-setter reads to hold a draft at PENDING.
        findings = _lint(_SCAFFOLD_DECK).call_args[0][1]
        assert findings[0]["demoted"] is False
        assert demoting_findings(findings) == []

    def test_a_deck_without_a_post_row_is_never_recorded(self):
        with patch(f"{_RCP}.update_db_post_gate_reason") as store:
            rcp._report_carousel_slide_slop(1, None, _SCAFFOLD_DECK)
        store.assert_not_called()

    def test_a_lint_failure_never_costs_the_deck(self):
        with patch(f"{_RCP}.slop_lint_report", side_effect=RuntimeError("boom")), \
             patch(f"{_RCP}._cta_keyword_for", return_value=None), \
             patch(f"{_RCP}.log_warning") as warn:
            rcp._report_carousel_slide_slop(1, 7, _SCAFFOLD_DECK)
        warn.assert_called_once()


class TestRecordSlideSlopFinding:
    _REPORT = {"checked": True, "passes": True, "violations": [{"check": "canned_scaffold"}],
               "hard": [], "warnings": [{"check": "canned_scaffold",
                                         "detail": "opens on a canned scaffold"}]}

    def test_other_gates_survive_and_the_note_never_duplicates(self):
        prior = [authenticity_finding(41, 60), slide_slop_finding(["old reason"])]
        with patch(f"{_RCP}.get_post_gate_reason", return_value=prior), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store:
            rcp._record_slide_slop_finding(1, 7, self._REPORT)
        findings = store.call_args[0][1]
        assert [f["gate"] for f in findings] == ["authenticity", GATE_SLIDE_SLOP]
        assert findings[-1]["details"] == ["(advisory) canned_scaffold: opens on a canned scaffold"]

    def test_a_regenerated_clean_deck_clears_a_stale_note(self):
        clean = {"checked": True, "passes": True, "violations": [], "hard": [], "warnings": []}
        with patch(f"{_RCP}.get_post_gate_reason",
                   return_value=[authenticity_finding(41, 60), slide_slop_finding(["old"])]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store:
            rcp._record_slide_slop_finding(1, 7, clean)
        assert [f["gate"] for f in store.call_args[0][1]] == ["authenticity"]

    def test_a_clean_deck_with_nothing_to_clear_never_writes(self):
        clean = {"checked": True, "passes": True, "violations": [], "hard": [], "warnings": []}
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[authenticity_finding(41, 60)]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store:
            rcp._record_slide_slop_finding(1, 7, clean)
        store.assert_not_called()

    def test_an_unwritable_note_only_logs(self):
        with patch(f"{_RCP}.get_post_gate_reason", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store, \
             patch(f"{_RCP}.log_warning") as warn:
            rcp._record_slide_slop_finding(1, 7, self._REPORT)
        store.assert_not_called()
        warn.assert_called_once()


class TestGenerationCallsIt:
    def test_create_carousel_content_reports_the_slide_lint_for_the_generated_deck(self):
        deck = {"cover": {"title": "T", "content": "C"}}
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}._select_story_for_post", return_value=None), \
             patch(f"{_RCP}._select_carousel_blueprint", return_value=None), \
             patch(f"{_RCP}._report_carousel_fact_grounding"), \
             patch(f"{_RCP}._report_carousel_slide_slop") as slide_lint, \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("caption", deck)):
            assert rcp.create_carousel_content(1, "awareness", None) == "caption"
        slide_lint.assert_called_once_with(1, None, deck)


class TestGatePassCarriesTheNote:
    """`evaluate_post_gates` cannot re-derive it — the deck is persisted as rendered images."""

    def _gates(self, post_type, recorded):
        with patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=recorded):
            return rcp.evaluate_post_gates(7, "", post_type)

    def test_a_carousel_carries_the_recorded_slide_note(self):
        findings = self._gates("carousel", [slide_slop_finding([], ["opens on a canned scaffold"])])
        assert [f["gate"] for f in findings] == [GATE_SLIDE_SLOP]
        assert findings[0]["demoted"] is False

    def test_a_document_post_carries_it_too(self):
        assert [f["gate"] for f in self._gates("document", [slide_slop_finding(["x"])])] \
            == [GATE_SLIDE_SLOP]

    def test_a_text_post_never_reads_it(self):
        assert self._gates("text", [slide_slop_finding(["x"])]) == []

    def test_a_deck_that_recorded_nothing_gets_no_note(self):
        assert self._gates("carousel", []) == []

    def test_an_unreadable_note_only_costs_the_record(self):
        with patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_gate_reason", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.log_warning") as warn:
            assert rcp.evaluate_post_gates(7, "", "carousel") == []
        warn.assert_called_once()


class TestAGatePassThatRaisesDoesNotEraseTheNote:
    """The caller PERSISTS whatever comes back, so anything dropped here is dropped for good."""

    def _findings(self, recorded):
        with patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch(f"{_RCP}._post_archetype_or_none", return_value=None), \
             patch(f"{_RCP}._recorded_similarity_finding", return_value=[]), \
             patch(f"{_RCP}._engagement_prefs_or_empty", return_value={}), \
             patch(f"{_RCP}._fact_anchors_for", return_value=[]), \
             patch(f"{_RCP}._cta_keyword_for", return_value=None), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=recorded), \
             patch(f"{_RCP}.evaluate_post_gates", side_effect=RuntimeError("gates down")), \
             patch(f"{_RCP}.log_warning"):
            return rcp._gate_findings_for_post(1, 7, "caption", "carousel")

    def test_the_recorded_slide_note_survives_a_failed_gate_pass(self):
        findings = self._findings([slide_slop_finding([], ["opens on a canned scaffold"])])
        assert [f["gate"] for f in findings] == [GATE_SLIDE_SLOP]

    def test_a_post_that_recorded_nothing_still_returns_nothing(self):
        assert self._findings([]) == []


class TestSlideSlopFindingCopy:
    def test_it_is_advisory_by_default_and_names_the_hard_reasons_first(self):
        finding = slide_slop_finding(["uses a contrastive frame"], ["opens on a canned scaffold"])
        assert finding["demoted"] is False
        assert finding["details"] == ["uses a contrastive frame",
                                      "(advisory) opens on a canned scaffold"]
        assert "not held" in finding["explanation"]
        assert "regenerate" in finding["remediation"].lower()

    def test_the_holding_posture_is_one_argument_away(self):
        finding = slide_slop_finding(["uses a contrastive frame"], demoted=True)
        assert finding["demoted"] is True
        assert "not held" not in finding["explanation"]

    def test_a_warn_only_deck_carries_no_meaningless_score_pair(self):
        # Most checks that fire on concatenated slide text are WARN on the `post` surface, so this
        # is the DOMINANT shape. `score`/`threshold` render in the SPA as "score N · your limit M";
        # a pattern count is not a measurement against a limit, and 0-against-0 beside an
        # explanation saying a pattern matched is the state this asserts can never be built.
        finding = slide_slop_finding([], ["opens on a canned scaffold"])
        assert finding["score"] is None and finding["threshold"] is None
        assert "matches 1 AI-slop pattern(s)" in finding["explanation"]

    def test_a_hard_deck_carries_no_score_pair_either(self):
        finding = slide_slop_finding(["uses a contrastive frame"])
        assert finding["score"] is None and finding["threshold"] is None
