"""Unit tests for the shipped-fix changelog + reporter notification (issue #502)."""

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.feedback.shipped"


def _pr(number=539, title="fix(feed): comments now post (closes #498)", body="Closes #498",
        merged_hours_ago=1):
    merged = (datetime.now(timezone.utc) - timedelta(hours=merged_hours_ago))
    return {"number": number, "title": title, "body": body,
            "merged_at": merged.strftime("%Y-%m-%dT%H:%M:%SZ")}


class TestClosingIssueNumbers:
    def test_matches_every_closing_keyword_across_title_and_body(self):
        from cqc_lem.utilities.feedback.shipped import closing_issue_numbers
        assert closing_issue_numbers("feat(dms): thing (closes #12)",
                                     "Fixes #13\nResolved: #14\nclosed #15") == [12, 13, 14, 15]

    def test_ignores_bare_references_and_dedupes(self):
        from cqc_lem.utilities.feedback.shipped import closing_issue_numbers
        # "related to #99" is a mention, not a fix — it must never claim someone's report shipped.
        assert closing_issue_numbers("Closes #12", "related to #99, closes #12") == [12]

    def test_empty_inputs_are_safe(self):
        from cqc_lem.utilities.feedback.shipped import closing_issue_numbers
        assert closing_issue_numbers(None, "") == []


class TestChangelogLine:
    def test_conventional_commit_becomes_human_copy(self):
        from cqc_lem.utilities.feedback.shipped import build_changelog_line
        line = build_changelog_line("fix(feed): comments now post reliably (closes #498)", 498, 539)
        assert line == "**Fixed:** Comments now post reliably (#498, PR #539)"

    def test_feature_and_unknown_types_get_their_own_label(self):
        from cqc_lem.utilities.feedback.shipped import build_changelog_line, humanize_title
        assert humanize_title("feat(dms): add follow-ups")[0] == "New"
        assert humanize_title("perf(api): speed up stats")[0] == "Faster"
        assert humanize_title("wat(api): something")[0] == "Improved"
        assert build_changelog_line("A plain title", 7).startswith("**Improved:** A plain title")

    def test_falls_back_when_the_title_is_only_a_prefix_and_ref(self):
        from cqc_lem.utilities.feedback.shipped import build_changelog_line
        assert build_changelog_line("fix: closes #3", 3) == "**Fixed:** A fix you reported (#3)"

    def test_line_is_bounded(self):
        from cqc_lem.utilities.feedback.shipped import MAX_CHANGELOG_CHARS, build_changelog_line
        assert len(build_changelog_line("fix(x): " + "y" * 900, 1, 2)) <= MAX_CHANGELOG_CHARS


class TestEnvKnobs:
    def test_lookback_and_delay_defaults_and_clamps(self, monkeypatch):
        from cqc_lem.utilities.feedback.shipped import (FIX_CSAT_DELAY_HOURS_DEFAULT,
                                                        LOOKBACK_HOURS_DEFAULT,
                                                        fix_csat_delay_hours, lookback_hours)
        monkeypatch.delenv("FEEDBACK_SHIPPED_LOOKBACK_HOURS", raising=False)
        monkeypatch.delenv("FEEDBACK_FIX_CSAT_DELAY_HOURS", raising=False)
        assert lookback_hours() == LOOKBACK_HOURS_DEFAULT
        assert fix_csat_delay_hours() == FIX_CSAT_DELAY_HOURS_DEFAULT

        monkeypatch.setenv("FEEDBACK_SHIPPED_LOOKBACK_HOURS", "9999")
        monkeypatch.setenv("FEEDBACK_FIX_CSAT_DELAY_HOURS", "0")
        assert lookback_hours() == 720
        assert fix_csat_delay_hours() == 0

        monkeypatch.setenv("FEEDBACK_SHIPPED_LOOKBACK_HOURS", "junk")
        monkeypatch.setenv("FEEDBACK_FIX_CSAT_DELAY_HOURS", "junk")
        assert lookback_hours() == LOOKBACK_HOURS_DEFAULT
        assert fix_csat_delay_hours() == FIX_CSAT_DELAY_HOURS_DEFAULT


class TestMergedPullRequests:
    def test_no_token_means_no_github_call(self):
        from cqc_lem.utilities.feedback.shipped import merged_pull_requests
        with patch(f"{_MOD}.github_token", return_value=None), \
                patch(f"{_MOD}.github_request") as req:
            assert merged_pull_requests() == []
        req.assert_not_called()

    def test_keeps_only_prs_merged_inside_the_window(self):
        from cqc_lem.utilities.feedback.shipped import merged_pull_requests
        rows = [_pr(number=1, merged_hours_ago=2),
                _pr(number=2, merged_hours_ago=200),
                {"number": 3, "merged_at": None},
                {"number": 4, "merged_at": "not-a-date"},
                "junk"]
        with patch(f"{_MOD}.github_token", return_value="t"), \
                patch(f"{_MOD}.github_request", return_value=rows):
            merged = merged_pull_requests(hours=48)
        assert [pr["number"] for pr in merged] == [1]

    def test_non_list_response_is_not_an_error(self):
        from cqc_lem.utilities.feedback.shipped import merged_pull_requests
        with patch(f"{_MOD}.github_token", return_value="t"), \
                patch(f"{_MOD}.github_request", return_value=None):
            assert merged_pull_requests() == []


class TestNotifyShippedIssue:
    def test_notifies_every_reporter_once_and_resolves_the_cluster(self):
        from cqc_lem.utilities.feedback.shipped import ShipAction, notify_shipped_issue
        notify = MagicMock(return_value=True)
        with patch(f"{_MOD}.get_feedback_reporters_for_issue", return_value=[4, 7]), \
                patch(f"{_MOD}.get_shipped_notice_by_issue", return_value=None), \
                patch(f"{_MOD}.record_shipped_notice", return_value=11) as record, \
                patch(f"{_MOD}.get_shipped_notice_recipient_ids", return_value=[]), \
                patch(f"{_MOD}.record_shipped_notice_recipient", return_value=True) as recip, \
                patch(f"{_MOD}.mark_feedback_resolved_for_issue", return_value=3) as resolve, \
                patch("cqc_lem.utilities.notifications.notify_shipped_fix", notify):
            result = notify_shipped_issue(498, pr_title="fix(feed): comments post", pr_number=539)

        assert result["action"] == ShipAction.NOTIFIED
        assert result["notified"] == 2
        assert result["resolved"] == 3
        assert record.call_args[0][1].startswith("**Fixed:** Comments post")
        assert [c[0][1] for c in recip.call_args_list] == [4, 7]
        resolve.assert_called_once_with(498)

    def test_a_reporter_already_on_the_ledger_is_never_emailed_again(self):
        from cqc_lem.utilities.feedback.shipped import ShipAction, notify_shipped_issue
        notify = MagicMock(return_value=True)
        with patch(f"{_MOD}.get_feedback_reporters_for_issue", return_value=[4, 7]), \
                patch(f"{_MOD}.get_shipped_notice_by_issue",
                      return_value={"id": 11, "changelog_line": "**Fixed:** X (#498)"}), \
                patch(f"{_MOD}.get_shipped_notice_recipient_ids", return_value=[4]), \
                patch(f"{_MOD}.record_shipped_notice_recipient", return_value=True), \
                patch(f"{_MOD}.mark_feedback_resolved_for_issue", return_value=0), \
                patch("cqc_lem.utilities.notifications.notify_shipped_fix", notify):
            result = notify_shipped_issue(498, pr_title="fix: x")
        # User 4 was told on an earlier pass — only the new reporter hears about it.
        assert [c[0][0] for c in notify.call_args_list] == [7]
        assert result["action"] == ShipAction.NOTIFIED
        assert result["notified"] == 1

    def test_no_reporters_means_no_notice_and_no_writes(self):
        from cqc_lem.utilities.feedback.shipped import ShipAction, notify_shipped_issue
        with patch(f"{_MOD}.get_feedback_reporters_for_issue", return_value=[]), \
                patch(f"{_MOD}.record_shipped_notice") as record:
            result = notify_shipped_issue(500, pr_title="fix: x")
        assert result["action"] == ShipAction.NO_REPORTERS
        record.assert_not_called()

    def test_reuses_the_existing_changelog_line_and_reports_already_sent(self):
        from cqc_lem.utilities.feedback.shipped import ShipAction, notify_shipped_issue
        existing = {"id": 11, "changelog_line": "**Fixed:** The original wording (#498)"}
        notify = MagicMock(return_value=True)
        with patch(f"{_MOD}.get_feedback_reporters_for_issue", return_value=[4]), \
                patch(f"{_MOD}.get_shipped_notice_by_issue", return_value=existing), \
                patch(f"{_MOD}.record_shipped_notice") as record, \
                patch(f"{_MOD}.get_shipped_notice_recipient_ids", return_value=[4]), \
                patch(f"{_MOD}.record_shipped_notice_recipient") as recip, \
                patch(f"{_MOD}.mark_feedback_resolved_for_issue", return_value=0), \
                patch("cqc_lem.utilities.notifications.notify_shipped_fix", notify):
            result = notify_shipped_issue(498, pr_title="fix(feed): a NEW wording", pr_number=540)
        assert result["action"] == ShipAction.ALREADY_SENT
        # A second pass re-words nothing and re-sends nothing.
        record.assert_not_called()
        notify.assert_not_called()
        recip.assert_not_called()

    def test_a_failed_email_does_not_mark_the_reporter_as_told(self):
        from cqc_lem.utilities.feedback.shipped import ShipAction, notify_shipped_issue
        with patch(f"{_MOD}.get_feedback_reporters_for_issue", return_value=[4]), \
                patch(f"{_MOD}.get_shipped_notice_by_issue", return_value=None), \
                patch(f"{_MOD}.record_shipped_notice", return_value=11), \
                patch(f"{_MOD}.get_shipped_notice_recipient_ids", return_value=[]), \
                patch(f"{_MOD}.record_shipped_notice_recipient") as recip, \
                patch(f"{_MOD}.mark_feedback_resolved_for_issue", return_value=1), \
                patch("cqc_lem.utilities.notifications.notify_shipped_fix", return_value=False):
            result = notify_shipped_issue(498, pr_title="fix: x")
        recip.assert_not_called()
        # An owed-but-undelivered notice is an error to retry, NOT "already sent".
        assert result["action"] == ShipAction.ERROR

    def test_a_raising_notifier_never_blocks_the_other_reporters(self):
        from cqc_lem.utilities.feedback.shipped import ShipAction, notify_shipped_issue
        with patch(f"{_MOD}.get_feedback_reporters_for_issue", return_value=[4, 7]), \
                patch(f"{_MOD}.get_shipped_notice_by_issue", return_value=None), \
                patch(f"{_MOD}.record_shipped_notice", return_value=11), \
                patch(f"{_MOD}.get_shipped_notice_recipient_ids", return_value=[]), \
                patch(f"{_MOD}.record_shipped_notice_recipient", return_value=True), \
                patch(f"{_MOD}.mark_feedback_resolved_for_issue", return_value=1), \
                patch("cqc_lem.utilities.notifications.notify_shipped_fix",
                      side_effect=[RuntimeError("smtp down"), True]):
            result = notify_shipped_issue(498, pr_title="fix: x")
        assert result["action"] == ShipAction.NOTIFIED
        assert result["notified"] == 1

    def test_unrecordable_changelog_line_is_an_error_not_a_send(self):
        from cqc_lem.utilities.feedback.shipped import ShipAction, notify_shipped_issue
        notify = MagicMock()
        with patch(f"{_MOD}.get_feedback_reporters_for_issue", return_value=[4]), \
                patch(f"{_MOD}.get_shipped_notice_by_issue", return_value=None), \
                patch(f"{_MOD}.record_shipped_notice", return_value=None), \
                patch("cqc_lem.utilities.notifications.notify_shipped_fix", notify):
            result = notify_shipped_issue(498, pr_title="fix: x")
        assert result["action"] == ShipAction.ERROR
        notify.assert_not_called()


class TestProcessShippedFixes:
    def test_walks_every_closing_issue_of_every_merged_pr(self):
        from cqc_lem.utilities.feedback.shipped import ShipAction, process_shipped_fixes
        prs = [_pr(number=539, title="fix(feed): a (closes #498)", body="Also closes #499")]
        results = [{"action": ShipAction.NOTIFIED, "issue_number": 498,
                    "changelog_line": "**Fixed:** A (#498)"},
                   {"action": ShipAction.NO_REPORTERS, "issue_number": 499}]
        with patch(f"{_MOD}.merged_pull_requests", return_value=prs), \
                patch(f"{_MOD}.notify_shipped_issue", side_effect=results) as notify:
            out = process_shipped_fixes()
        assert out["pull_requests"] == 1
        assert out["counts"] == {ShipAction.NOTIFIED: 1, ShipAction.NO_REPORTERS: 1}
        assert out["changelog"] == ["**Fixed:** A (#498)"]
        assert [c[0][0] for c in notify.call_args_list] == [498, 499]

    def test_one_exploding_issue_does_not_abort_the_pass(self):
        from cqc_lem.utilities.feedback.shipped import ShipAction, process_shipped_fixes
        prs = [_pr(number=1, title="fix: a (closes #10)", body=""),
               _pr(number=2, title="fix: b (closes #11)", body="")]
        with patch(f"{_MOD}.merged_pull_requests", return_value=prs), \
                patch(f"{_MOD}.notify_shipped_issue",
                      side_effect=[RuntimeError("boom"),
                                   {"action": ShipAction.NOTIFIED, "issue_number": 11}]):
            out = process_shipped_fixes()
        assert out["counts"] == {ShipAction.ERROR: 1, ShipAction.NOTIFIED: 1}

    def test_a_pr_that_closes_nothing_is_skipped(self):
        from cqc_lem.utilities.feedback.shipped import process_shipped_fixes
        with patch(f"{_MOD}.merged_pull_requests",
                   return_value=[_pr(title="chore: bump deps", body="see #12")]), \
                patch(f"{_MOD}.notify_shipped_issue") as notify:
            out = process_shipped_fixes()
        assert out["counts"] == {}
        notify.assert_not_called()
