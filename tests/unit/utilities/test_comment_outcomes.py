"""Unit tests for the comment-outcome scoring core — issue #628. Pure arithmetic: reply/like rates,
the three-valued 'Most relevant' demotion signal, and the verdict that gates feed commenting."""

import pytest

from cqc_lem.utilities.comment_outcomes import (VERDICT_HOLD, VERDICT_OK, VERDICT_UNKNOWN,
                                                VERDICT_WATCH, comment_quality_report,
                                                demotion_hold_rate, hold_seconds,
                                                min_visibility_sample, quality_verdict,
                                                summarize_outcomes)

pytestmark = pytest.mark.unit


def _row(**kw):
    base = {"status": "checked", "author_replied": 0, "reply_count": 0, "like_count": 0,
            "visible_most_relevant": 1, "our_reply_sent": 0}
    base.update(kw)
    return base


class TestSummarize:
    def test_empty_rows_have_no_rates(self):
        s = summarize_outcomes([])
        assert s["sample_size"] == 0
        # None, not 0.0 — an empty window has no reply rate and charting 0% reads as a collapse.
        assert s["author_reply_rate"] is None and s["reply_rate"] is None
        assert s["like_rate"] is None and s["demotion_rate"] is None

    def test_none_input_is_safe(self):
        assert summarize_outcomes(None)["sample_size"] == 0

    def test_rates_over_checked_rows(self):
        rows = [_row(author_replied=1, reply_count=2, like_count=3),
                _row(reply_count=1),
                _row(),
                _row(like_count=5)]
        s = summarize_outcomes(rows)
        assert s["checked"] == 4
        assert s["author_replies"] == 1 and s["author_reply_rate"] == 0.25
        assert s["replied_comments"] == 2 and s["reply_rate"] == 0.5
        assert s["liked_comments"] == 2 and s["like_rate"] == 0.5
        assert s["total_replies"] == 3 and s["total_likes"] == 8

    def test_skipped_rows_are_excluded_from_rates_but_counted(self):
        rows = [_row(reply_count=1), {"status": "skipped", "skip_reason": "comment-not-found"}]
        s = summarize_outcomes(rows)
        assert s["sample_size"] == 2 and s["checked"] == 1 and s["skipped"] == 1
        assert s["reply_rate"] == 1.0  # the skipped check never observed anything to average in

    def test_null_visibility_is_not_counted_as_healthy(self):
        rows = [_row(visible_most_relevant=None), _row(visible_most_relevant=None),
                _row(visible_most_relevant=0)]
        s = summarize_outcomes(rows)
        assert s["visibility_sample"] == 1
        assert s["demoted"] == 1
        assert s["demotion_rate"] == 1.0  # 1 of 1 READABLE, not 1 of 3

    def test_visible_rows_are_not_demoted(self):
        s = summarize_outcomes([_row(visible_most_relevant=1), _row(visible_most_relevant=0)])
        assert s["visibility_sample"] == 2 and s["demotion_rate"] == 0.5

    def test_our_reply_sent_is_tallied(self):
        s = summarize_outcomes([_row(our_reply_sent=1), _row()])
        assert s["our_replies_sent"] == 1


class TestVerdict:
    def test_no_readings_is_unknown(self):
        v = quality_verdict(summarize_outcomes([]))
        assert v["status"] == VERDICT_UNKNOWN

    def test_healthy_is_ok(self):
        v = quality_verdict(summarize_outcomes([_row() for _ in range(12)]))
        assert v["status"] == VERDICT_OK and v["demotion_rate"] == 0.0

    def test_high_demotion_on_a_real_sample_holds(self):
        rows = [_row(visible_most_relevant=0) for _ in range(10)]
        v = quality_verdict(summarize_outcomes(rows))
        assert v["status"] == VERDICT_HOLD
        assert "Most relevant" in v["reason"]

    def test_high_demotion_on_a_thin_sample_only_watches(self):
        rows = [_row(visible_most_relevant=0) for _ in range(3)]
        v = quality_verdict(summarize_outcomes(rows))
        assert v["status"] == VERDICT_WATCH  # never pause a user's engagement off three reads

    def test_threshold_and_sample_are_env_tunable(self, monkeypatch):
        monkeypatch.setenv("COMMENT_DEMOTION_HOLD_RATE", "0.2")
        monkeypatch.setenv("COMMENT_QUALITY_MIN_SAMPLE", "2")
        assert demotion_hold_rate() == 0.2 and min_visibility_sample() == 2
        rows = [_row(visible_most_relevant=0), _row(visible_most_relevant=1),
                _row(visible_most_relevant=1)]
        assert quality_verdict(summarize_outcomes(rows))["status"] == VERDICT_HOLD

    def test_garbage_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("COMMENT_DEMOTION_HOLD_RATE", "not-a-number")
        monkeypatch.setenv("COMMENT_QUALITY_MIN_SAMPLE", "")
        monkeypatch.setenv("COMMENT_QUALITY_HOLD_SECONDS", "abc")
        assert demotion_hold_rate() == 0.5
        assert min_visibility_sample() == 10
        assert hold_seconds() == 7 * 24 * 60 * 60

    def test_hold_seconds_is_floored(self, monkeypatch):
        monkeypatch.setenv("COMMENT_QUALITY_HOLD_SECONDS", "1")
        assert hold_seconds() == 60


class TestReport:
    def test_report_carries_window_and_verdict(self):
        report = comment_quality_report([_row(author_replied=1)], days=14)
        assert report["days"] == 14
        assert report["checked"] == 1
        assert report["verdict"]["status"] in (VERDICT_OK, VERDICT_WATCH, VERDICT_UNKNOWN)

    def test_report_of_nothing_is_still_well_shaped(self):
        report = comment_quality_report(None)
        assert report["sample_size"] == 0 and report["verdict"]["status"] == VERDICT_UNKNOWN
