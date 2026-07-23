"""Unit tests for the A1/A3 authenticity rubric (issue #405 spike deliverable).

These tests ARE the calibration guard: they prove the gate flags every generic golden draft and
demotes NONE of the authentic / AI-assisted-personal ones, and they lock the rubric's structural
invariants (weights sum to 1, threshold ordering, golden-set integrity) so a future edit to the
rubric can't silently break the A1/A3 acceptance contract.
"""

import pytest

from cqc_lem.utilities.ai.authenticity_rubric import (
    AI_SLOP_TELLS,
    AUTHENTICITY_CAUTION_CEILING,
    AUTHENTICITY_DIMENSIONS,
    AUTHENTICITY_GATE_THRESHOLD,
    AUTHENTICITY_JUDGE_CRITERIA,
    GOLDEN_LABELS,
    GOLDEN_SET,
    classify_score,
    gate_draft,
    golden_by_label,
    weighted_score,
)


def test_dimension_weights_sum_to_one():
    total = sum(d["weight"] for d in AUTHENTICITY_DIMENSIONS.values())
    assert total == pytest.approx(1.0)


def test_dimensions_have_required_fields():
    for name, spec in AUTHENTICITY_DIMENSIONS.items():
        assert 0.0 < spec["weight"] < 1.0, name
        assert spec["label"]
        assert spec["scores_high_when"]
        assert spec["scores_low_when"]


def test_first_person_proof_and_specificity_are_heaviest():
    # Calibration principle: the two signals AI can't fake for unlived content dominate.
    ranked = sorted(AUTHENTICITY_DIMENSIONS.items(), key=lambda kv: kv[1]["weight"], reverse=True)
    assert {ranked[0][0], ranked[1][0]} == {"first_person_proof", "specificity"}


def test_threshold_ordering():
    assert 0 < AUTHENTICITY_GATE_THRESHOLD < AUTHENTICITY_CAUTION_CEILING < 100


def test_judge_criteria_states_ai_is_neutral():
    text = AUTHENTICITY_JUDGE_CRITERIA.lower()
    assert "neutral" in text
    assert "0-100" in AUTHENTICITY_JUDGE_CRITERIA


def test_ai_slop_tells_present():
    assert len(AI_SLOP_TELLS) >= 5
    assert all(isinstance(t, str) and t for t in AI_SLOP_TELLS)


class TestWeightedScore:
    def test_all_max_is_100(self):
        assert weighted_score({k: 100 for k in AUTHENTICITY_DIMENSIONS}) == pytest.approx(100.0)

    def test_all_zero_is_0(self):
        assert weighted_score({k: 0 for k in AUTHENTICITY_DIMENSIONS}) == 0.0

    def test_empty_is_0(self):
        assert weighted_score({}) == 0.0

    def test_missing_dimension_gets_no_credit(self):
        full = weighted_score({k: 80 for k in AUTHENTICITY_DIMENSIONS})
        one_missing = weighted_score(
            {k: 80 for k in AUTHENTICITY_DIMENSIONS if k != "first_person_proof"}
        )
        assert one_missing < full

    def test_unknown_keys_ignored(self):
        base = weighted_score({k: 50 for k in AUTHENTICITY_DIMENSIONS})
        with_junk = weighted_score({**{k: 50 for k in AUTHENTICITY_DIMENSIONS}, "reason": "n/a"})
        assert base == with_junk

    def test_out_of_range_is_clamped(self):
        assert weighted_score({k: 999 for k in AUTHENTICITY_DIMENSIONS}) == pytest.approx(100.0)
        assert weighted_score({k: -50 for k in AUTHENTICITY_DIMENSIONS}) == 0.0

    def test_non_numeric_treated_as_zero(self):
        assert weighted_score({k: "oops" for k in AUTHENTICITY_DIMENSIONS}) == 0.0


class TestClassifyAndGate:
    def test_below_threshold_demotes(self):
        assert classify_score(AUTHENTICITY_GATE_THRESHOLD - 1) == "demote"

    def test_caution_band(self):
        assert classify_score(AUTHENTICITY_GATE_THRESHOLD) == "caution"
        assert classify_score(AUTHENTICITY_CAUTION_CEILING - 1) == "caution"

    def test_pass_band(self):
        assert classify_score(AUTHENTICITY_CAUTION_CEILING) == "pass"
        assert classify_score(100) == "pass"

    def test_gate_draft_matches_threshold(self):
        assert gate_draft({k: 0 for k in AUTHENTICITY_DIMENSIONS}) is True
        assert gate_draft({k: 100 for k in AUTHENTICITY_DIMENSIONS}) is False


class TestGoldenSet:
    def test_ids_unique(self):
        ids = [d["id"] for d in GOLDEN_SET]
        assert len(ids) == len(set(ids))

    def test_labels_valid(self):
        for d in GOLDEN_SET:
            assert d["label"] in GOLDEN_LABELS

    def test_every_label_represented(self):
        present = {d["label"] for d in GOLDEN_SET}
        assert present == GOLDEN_LABELS

    def test_expected_action_matches_label(self):
        for d in GOLDEN_SET:
            expected = "demote" if d["label"] == "generic" else "keep"
            assert d["expected_action"] == expected, d["id"]

    def test_entries_are_well_formed(self):
        for d in GOLDEN_SET:
            assert d["text"].strip()
            assert set(d["expected_dimension_scores"]) == set(AUTHENTICITY_DIMENSIONS)
            assert all(0 <= v <= 100 for v in d["expected_dimension_scores"].values())

    def test_golden_by_label(self):
        generic = golden_by_label("generic")
        assert generic and all(d["label"] == "generic" for d in generic)
        assert golden_by_label("nope") == ()

    # --- The A1 acceptance contract: flag generic, demote nothing personal ---

    @pytest.mark.parametrize("draft", GOLDEN_SET, ids=[d["id"] for d in GOLDEN_SET])
    def test_calibration_gate_action(self, draft):
        demoted = gate_draft(draft["expected_dimension_scores"])
        expected_demoted = draft["expected_action"] == "demote"
        assert demoted is expected_demoted, (
            f"{draft['id']} scored {weighted_score(draft['expected_dimension_scores'])}"
        )

    def test_no_false_demotion_of_personal_content(self):
        for label in ("authentic", "ai_assisted_personal"):
            for d in golden_by_label(label):
                assert not gate_draft(d["expected_dimension_scores"]), d["id"]

    def test_all_generic_demoted(self):
        for d in golden_by_label("generic"):
            assert gate_draft(d["expected_dimension_scores"]), d["id"]
