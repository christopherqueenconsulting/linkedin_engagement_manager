"""Grading `probe_avatar_likeness` against human-labelled frames (issue #1430)."""
import json
import os

import pytest

from cqc_lem.utilities.avatar.likeness_eval import (
    MIN_GRADED,
    MIN_PER_CLASS,
    RECORD_FIELDS,
    frame_id,
    grade,
    leaks_a_frame_path,
    normalize_label,
    run_eval,
    verdict_record,
)

pytestmark = pytest.mark.unit

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "fixtures", "avatar_likeness_verdicts.json")

_MAN_40S = {"gender_presentation": "man", "age_band": "40s"}


def _record(label, checked=True, present=None, clause="a man in his 40s", used_avatar="true"):
    return {"frame_id": "f" * 16, "label": label, "subject_clause": clause,
            "checked": checked, "present": present, "used_avatar": used_avatar}


class TestFrameId:
    def test_digests_content_not_the_filename(self, tmp_path):
        one = tmp_path / "lora-01.png"
        two = tmp_path / "renamed.png"
        one.write_bytes(b"same pixels")
        two.write_bytes(b"same pixels")
        assert frame_id(str(one)) == frame_id(str(two))

    def test_different_content_gets_a_different_id(self, tmp_path):
        one = tmp_path / "a.png"
        two = tmp_path / "b.png"
        one.write_bytes(b"frame a")
        two.write_bytes(b"frame b")
        assert frame_id(str(one)) != frame_id(str(two))
        assert len(frame_id(str(one))) == 16


class TestNormalizeLabel:
    @pytest.mark.parametrize("raw,expected", [
        ("present", "present"), (" ABSENT ", "absent"), ("Present", "present"),
        ("maybe", None), ("", None), (None, None),
    ])
    def test_only_the_two_ground_truth_labels_survive(self, raw, expected):
        assert normalize_label(raw) == expected


class TestVerdictRecord:
    def test_never_carries_the_frame_path(self):
        entry = {"frame": "/home/owner/private/lora-01.png", "label": "present", **_MAN_40S}
        record = verdict_record(entry, {"checked": True, "present": True, "reason": "ok"},
                                fid="abc123")
        assert "frame" not in record
        assert set(record) <= set(RECORD_FIELDS)
        assert record["subject_clause"] == "a man in his 40s"

    def test_reason_is_opt_in(self):
        entry = {"label": "present", **_MAN_40S}
        verdict = {"checked": True, "present": True, "reason": "a man in his 40s is central"}
        assert "reason" not in verdict_record(entry, verdict, fid="a")
        assert verdict_record(entry, verdict, fid="a", include_reason=True)["reason"]

    def test_used_avatar_defaults_to_a_real_lora_render(self):
        record = verdict_record({"label": "absent"}, {"checked": True, "present": False}, fid="a")
        assert record["used_avatar"] == "true"

    @pytest.mark.parametrize("raw,expected", [
        (True, "true"), (False, "false"), ("TRUE", "true"), (" false ", "false"),
        ("maybe", "unknown"), (None, "unknown"),
    ])
    def test_used_avatar_is_canonical_so_the_split_cannot_fork(self, raw, expected):
        """A manifest written with the JSON boolean must not bucket beside the string form."""
        record = verdict_record({"label": "present", "used_avatar": raw},
                                {"checked": True, "present": True}, fid="a")
        assert record["used_avatar"] == expected


class TestRunEval:
    def test_probes_every_frame_in_order(self, tmp_path):
        paths = []
        for i in range(3):
            p = tmp_path / f"frame{i}.png"
            p.write_bytes(f"pixels {i}".encode())
            paths.append(str(p))
        entries = [{"frame": p, "label": "present", **_MAN_40S} for p in paths]
        seen = []

        def probe(path, avatar):
            seen.append(path)
            return {"checked": True, "present": True, "reason": "ok"}

        records = run_eval(entries, probe)
        assert seen == paths
        assert [r["checked"] for r in records] == [True, True, True]

    def test_unreadable_frame_is_unchecked_not_a_failure(self, tmp_path):
        entries = [{"frame": str(tmp_path / "missing.png"), "label": "present", **_MAN_40S}]
        records = run_eval(entries, lambda p, a: pytest.fail("probe must not be called"))
        assert records[0]["checked"] is False
        assert records[0]["present"] is None
        assert "frame" not in records[0]

    def test_reasons_thread_through(self, tmp_path):
        frame = tmp_path / "f.png"
        frame.write_bytes(b"pixels")
        entries = [{"frame": str(frame), "label": "absent", **_MAN_40S}]
        records = run_eval(entries, lambda p, a: {"checked": True, "present": False,
                                                  "reason": "no person"}, include_reasons=True)
        assert records[0]["reason"] == "no person"


class TestGradePolarity:
    """The direction of the two rates is the thing that gets misread — pin it."""

    def test_probe_declining_a_good_frame_is_a_false_negative(self):
        scores = grade([_record("present", present=False)])
        assert scores["overall"]["false_negative"] == 1
        assert scores["overall"]["false_positive"] == 0
        assert scores["overall"]["false_negative_rate"] == 1.0

    def test_probe_passing_a_bad_frame_is_a_false_positive(self):
        scores = grade([_record("absent", present=True)])
        assert scores["overall"]["false_positive"] == 1
        assert scores["overall"]["false_negative"] == 0
        assert scores["overall"]["false_positive_rate"] == 1.0

    def test_agreement_scores_no_errors(self):
        scores = grade([_record("present", present=True), _record("absent", present=False)])
        assert scores["overall"]["false_negative_rate"] == 0.0
        assert scores["overall"]["false_positive_rate"] == 0.0


class TestGradeCounting:
    def test_unchecked_is_never_an_error(self):
        scores = grade([_record("present", checked=False, present=None)])["overall"]
        assert scores["graded"] == 0
        assert scores["false_negative"] == 0
        assert scores["unchecked"] == 1
        assert scores["unchecked_rate"] == 1.0

    def test_an_empty_class_reports_none_not_zero(self):
        """An unmeasured class is not a perfect one."""
        scores = grade([_record("present", present=True)])["overall"]
        assert scores["false_negative_rate"] == 0.0
        assert scores["false_positive_rate"] is None

    def test_unlabelled_rows_are_counted_apart(self):
        scores = grade([{"label": "unsure", "checked": True, "present": True}])["overall"]
        assert scores["unlabelled"] == 1
        assert scores["graded"] == 0

    def test_empty_set_grades_to_nothing(self):
        scores = grade([])["overall"]
        assert scores["total"] == 0
        assert scores["false_negative_rate"] is None
        assert scores["unchecked_rate"] is None
        assert scores["sufficient"] is False


class TestSufficiency:
    def test_a_thin_set_is_not_enough_to_decide_the_hold(self):
        records = [_record("present", present=True)] * 3 + [_record("absent", present=False)] * 3
        assert grade(records)["overall"]["sufficient"] is False

    def test_enough_frames_in_both_classes_is_sufficient(self):
        records = ([_record("present", present=True)] * MIN_GRADED
                   + [_record("absent", present=False)] * MIN_PER_CLASS)
        assert grade(records)["overall"]["sufficient"] is True

    def test_one_class_alone_is_never_sufficient(self):
        records = [_record("present", present=True)] * (MIN_GRADED * 2)
        assert grade(records)["overall"]["sufficient"] is False


class TestSplits:
    def test_fallback_frames_never_land_in_the_lora_error_rate(self):
        """The #1431 review's caveat: a base-Flux fallback frame carries no likeness by design."""
        records = [
            _record("present", present=False, used_avatar="true"),
            _record("absent", present=False, used_avatar="false"),
            _record("absent", present=False, used_avatar="false"),
        ]
        by_avatar = grade(records)["by_used_avatar"]
        assert by_avatar["true"]["false_negative"] == 1
        assert by_avatar["false"]["false_negative"] == 0
        assert by_avatar["false"]["true_negative"] == 2

    def test_rates_break_down_per_declared_subject_clause(self):
        records = [
            _record("present", present=False, clause="a man in his 40s"),
            _record("present", present=True, clause="a woman in her 30s"),
        ]
        by_clause = grade(records)["by_subject_clause"]
        assert by_clause["a man in his 40s"]["false_negative_rate"] == 1.0
        assert by_clause["a woman in her 30s"]["false_negative_rate"] == 0.0

    def test_an_undeclared_clause_gets_its_own_bucket(self):
        scores = grade([_record("present", checked=False, clause="")])
        assert "" in scores["by_subject_clause"]


class TestPublishGuard:
    def test_a_frame_path_is_a_leak(self):
        assert leaks_a_frame_path([{"frame_id": "a", "frame": "/home/owner/lora-01.png"}]) is True

    def test_grader_own_fields_are_not_a_leak(self):
        assert leaks_a_frame_path([_record("present", present=True)]) is False


class TestCommittedFixture:
    """The committed verdict file is what a public repo may carry instead of the frames."""

    @staticmethod
    def _payload():
        with open(FIXTURE, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_carries_no_frame_paths(self):
        assert leaks_a_frame_path(self._payload()["records"]) is False

    def test_recorded_scores_match_a_regrade(self):
        payload = self._payload()
        assert grade(payload["records"]) == payload["scores"]

    def test_declares_where_its_verdicts_came_from(self):
        """A synthetic schema example must never be mistaken for a measurement."""
        payload = self._payload()
        assert payload["source"] in ("synthetic-schema-example", "measured")
        if payload["source"] == "synthetic-schema-example":
            assert payload["scores"]["overall"]["sufficient"] is False
