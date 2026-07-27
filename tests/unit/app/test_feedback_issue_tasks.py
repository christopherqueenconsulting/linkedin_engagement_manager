"""Unit tests for the feedback auto-filing beat tasks and their schedule entries — issue #498."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_SVC = "cqc_lem.utilities.feedback.issue_service"


class TestAutoFileFeedbackIssues:
    def _repair(self, scanned=0, repaired=0, failed=0):
        return patch(f"{_SVC}.repair_auto_filed_issues",
                     return_value={"scanned": scanned, "repaired": repaired, "failed": failed})

    def test_delegates_to_the_service_and_summarizes_the_pass(self):
        counts = {"processed": 3, "counts": {"filed": 1, "deduped": 2}}
        with patch(f"{_SVC}.process_new_feedback", return_value=counts) as service, self._repair():
            from cqc_lem.app.run_scheduler import auto_file_feedback_issues
            result = auto_file_feedback_issues(limit=7)
        service.assert_called_once_with(limit=7)
        assert "3 row(s)" in result
        assert "'filed': 1" in result

    def test_empty_pass_says_nothing_to_do(self):
        with patch(f"{_SVC}.process_new_feedback", return_value={"processed": 0, "counts": {}}), \
                self._repair():
            from cqc_lem.app.run_scheduler import auto_file_feedback_issues
            assert "nothing to do" in auto_file_feedback_issues()

    def test_every_pass_also_repairs_already_filed_issues(self):
        # Issue #718: filing an issue whose labels never landed is not the job being done — the
        # pipeline selects on labels, so a label-less issue is invisible until this repairs it.
        with patch(f"{_SVC}.process_new_feedback", return_value={"processed": 0, "counts": {}}), \
                self._repair(scanned=4, repaired=2, failed=1) as repair:
            from cqc_lem.app.run_scheduler import auto_file_feedback_issues
            result = auto_file_feedback_issues()
        repair.assert_called_once_with()
        assert "4 checked" in result
        assert "2 repaired" in result
        assert "1 still broken" in result


class TestAutoReclusterFeedback:
    def test_delegates_to_the_service_and_summarizes_the_pass(self):
        with patch(f"{_SVC}.recluster_feedback",
                   return_value={"scanned": 9, "attached": 2, "embedded": 4,
                                 "clusters": 5}) as service:
            from cqc_lem.app.run_scheduler import auto_recluster_feedback
            result = auto_recluster_feedback(limit=50)
        service.assert_called_once_with(limit=50)
        assert "9 scanned" in result
        assert "2 attached" in result
        assert "5 open cluster(s)" in result
        assert "4 embedding(s) backfilled" in result


class TestBeatSchedule:
    def _schedule(self):
        from cqc_lem.app.my_celery import app
        return app.conf.beat_schedule

    def test_both_feedback_entries_point_at_real_tasks(self):
        import cqc_lem.app.run_scheduler as scheduler
        schedule = self._schedule()
        for name, attr in (('file-feedback-issues', 'auto_file_feedback_issues'),
                           ('recluster-feedback', 'auto_recluster_feedback')):
            assert schedule[name]['task'] == f"cqc_lem.app.run_scheduler.{attr}"
            assert hasattr(scheduler, attr)

    def test_filing_runs_every_thirty_minutes(self):
        entry = self._schedule()['file-feedback-issues']['schedule']
        assert entry.minute == {0, 30}

    def test_reclustering_runs_nightly_off_peak(self):
        entry = self._schedule()['recluster-feedback']['schedule']
        assert entry.hour == {2}
        assert entry.minute == {45}
