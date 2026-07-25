"""Unit tests for the post-fix micro-CSAT and the shipped-fix notification (issue #502)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_SURVEYS = "cqc_lem.utilities.surveys"
_NOTIFY = "cqc_lem.utilities.notifications"


class TestFixCsatKeys:
    def test_key_is_per_issue_and_fits_the_ledger_column(self):
        from cqc_lem.utilities.surveys import fix_csat_key, is_fix_csat_key
        key = fix_csat_key(502)
        assert key == "fix_csat_502"
        assert len(key) <= 32
        assert is_fix_csat_key(key)

    def test_only_generated_keys_are_recognised(self):
        from cqc_lem.utilities.surveys import is_fix_csat_key
        assert not is_fix_csat_key("fix_csat_")
        assert not is_fix_csat_key("fix_csat_abc")
        assert not is_fix_csat_key("nps_day3")
        assert not is_fix_csat_key(None)

    def test_dismiss_accepts_a_generated_key_but_still_rejects_junk(self):
        from cqc_lem.utilities.surveys import dismiss_survey
        with patch(f"{_SURVEYS}.record_survey_prompt", return_value=True) as record:
            assert dismiss_survey(4, "fix_csat_502") is True
            assert dismiss_survey(4, "made_up_key") is False
        record.assert_called_once_with(4, "fix_csat_502")


class TestRecordFixCsatResponse:
    def test_a_yes_is_stored_resolved_so_it_never_burns_a_classification(self):
        from cqc_lem.utilities.db import FeedbackSource, FeedbackStatus
        from cqc_lem.utilities.surveys import record_fix_csat_response
        with patch(f"{_SURVEYS}.insert_feedback", return_value=77) as insert, \
                patch(f"{_SURVEYS}.update_feedback_triage") as triage, \
                patch(f"{_SURVEYS}.record_survey_prompt") as record, \
                patch(f"{_SURVEYS}._track_response") as track:
            assert record_fix_csat_response(4, 498, True) == 77

        kwargs = insert.call_args.kwargs
        assert kwargs["source"] == FeedbackSource.CSAT
        assert kwargs["type_hint"] == "fix_csat"
        assert kwargs["sentiment"] == "positive"
        assert kwargs["context"]["issue_number"] == 498
        assert kwargs["context"]["resolved"] is True
        assert kwargs["context"]["survey_key"] == "fix_csat_498"
        triage.assert_called_once_with(77, status=FeedbackStatus.RESOLVED)
        record.assert_called_once_with(4, "fix_csat_498")
        assert track.call_args[0][1] == "csat"

    def test_a_no_stays_new_so_it_re_enters_the_auto_work_loop(self):
        from cqc_lem.utilities.surveys import record_fix_csat_response
        with patch(f"{_SURVEYS}.insert_feedback", return_value=78) as insert, \
                patch(f"{_SURVEYS}.update_feedback_triage") as triage, \
                patch(f"{_SURVEYS}.record_survey_prompt"), \
                patch(f"{_SURVEYS}._track_response"):
            assert record_fix_csat_response(4, 498, False, comment="still broken on mobile") == 78

        assert insert.call_args[0][0] == "still broken on mobile"
        assert insert.call_args.kwargs["sentiment"] == "negative"
        triage.assert_not_called()

    def test_body_falls_back_to_a_readable_line_when_no_comment_is_given(self):
        from cqc_lem.utilities.surveys import record_fix_csat_response
        with patch(f"{_SURVEYS}.insert_feedback", return_value=1) as insert, \
                patch(f"{_SURVEYS}.update_feedback_triage"), \
                patch(f"{_SURVEYS}.record_survey_prompt"), \
                patch(f"{_SURVEYS}._track_response"):
            record_fix_csat_response(4, 498, False, comment="   ")
        assert insert.call_args[0][0] == "Still not fixed (issue #498)"

    def test_a_failed_insert_records_no_ask_and_no_event(self):
        from cqc_lem.utilities.surveys import record_fix_csat_response
        with patch(f"{_SURVEYS}.insert_feedback", return_value=None), \
                patch(f"{_SURVEYS}.update_feedback_triage") as triage, \
                patch(f"{_SURVEYS}.record_survey_prompt") as record, \
                patch(f"{_SURVEYS}._track_response") as track:
            assert record_fix_csat_response(4, 498, True) is None
        triage.assert_not_called()
        record.assert_not_called()
        track.assert_not_called()


class TestNotifyShippedFix:
    def test_sends_the_email_and_tracks_it(self):
        from cqc_lem.utilities.notifications import notify_shipped_fix
        with patch(f"{_NOTIFY}.get_user_email", return_value="a@b.co"), \
                patch("cqc_lem.utilities.email.send_shipped_fix_email",
                      return_value=True) as send, \
                patch("cqc_lem.utilities.observability.track_shipped_notice") as track:
            assert notify_shipped_fix(4, "**Fixed:** X (#498)", 498) is True
        assert send.call_args[0] == ("a@b.co", "**Fixed:** X (#498)", 498)
        track.assert_called_once_with(4, 498)

    def test_no_email_address_means_no_send(self):
        from cqc_lem.utilities.notifications import notify_shipped_fix
        with patch(f"{_NOTIFY}.get_user_email", return_value=None), \
                patch("cqc_lem.utilities.email.send_shipped_fix_email") as send:
            assert notify_shipped_fix(4, "line", 498) is False
        send.assert_not_called()

    def test_a_raising_send_is_swallowed(self):
        from cqc_lem.utilities.notifications import notify_shipped_fix
        with patch(f"{_NOTIFY}.get_user_email", return_value="a@b.co"), \
                patch("cqc_lem.utilities.email.send_shipped_fix_email",
                      side_effect=RuntimeError("smtp down")):
            assert notify_shipped_fix(4, "line", 498) is False


class TestShippedFixEmail:
    def test_escapes_the_changelog_line_and_names_the_issue(self):
        from cqc_lem.utilities.email import send_shipped_fix_email
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch:
            assert send_shipped_fix_email("a@b.co", "**Fixed:** <script>x</script> (#498)",
                                          498) is True
        to_email, subject, html = dispatch.call_args[0]
        assert to_email == "a@b.co"
        assert "#498" in subject
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        # The markdown emphasis of the stored line has no meaning in HTML — it is stripped.
        assert "**" not in html
