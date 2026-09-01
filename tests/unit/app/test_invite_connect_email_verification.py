"""Class C connection targets (#1836): a variant of the Connect dialog that demands an email.

A subset of profiles render an email-verification variant of the Connect dialog and refuse to
accept the invite without the recipient's email. This detects that variant from the dialog's own
words/controls, threads a known email into it, and — when none is known — records a distinct
terminal reason instead of feeding the dialog-miss streak or the account-level hold that exist to
catch a dead SELECTOR, not a target LinkedIn is deliberately gating.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"

# Captured production, 2026-09-01 (issue #1836) — verbatim, both variants under the same
# `Add a note to your invitation?` heading with the same Add-a-note / Send-without-a-note controls.
_VERIFICATION_TEXT = (
    "Dialog content start. Add a note to your invitation? To verify this member knows you, please "
    "enter their email to connect. You can also include a personal note. Learn why Add a note Send "
    "without a note Dialog content end.")
_ORDINARY_TEXT = (
    "Dialog content start. Add a note to your invitation? Personalize your invitation to Kaitlyn "
    "Albertoli by adding a note. LinkedIn members are more likely to accept invitations that include "
    "a note. Add a note Send without a note Dialog content end.")


class TestConnectDialogWantsEmailFromText:
    """Pure text → three-valued reading.

    No driver to mock, so the exact production strings are asserted against it directly.
    """

    def test_the_verification_variant_wants_email(self):
        from cqc_lem.app.engagement import invites as ra
        assert ra._connect_dialog_wants_email_from_text(_VERIFICATION_TEXT) is True

    def test_the_ordinary_variant_does_not_want_email(self):
        from cqc_lem.app.engagement import invites as ra
        assert ra._connect_dialog_wants_email_from_text(_ORDINARY_TEXT) is False

    def test_unreadable_text_is_unknown_not_wants_email(self):
        # Same posture as ThreadState: unknown is NEVER "wants email" — an undetected verification
        # variant must fall through to the ordinary send/miss path, never a false skip.
        from cqc_lem.app.engagement import invites as ra
        assert ra._connect_dialog_wants_email_from_text("") is None
        assert ra._connect_dialog_wants_email_from_text(None) is None
        assert ra._connect_dialog_wants_email_from_text("   ") is None


class TestConnectDialogWantsEmailFromDriver:
    def test_an_email_input_inside_the_dialog_wins_over_the_prose(self):
        from cqc_lem.app.engagement import invites as ra
        container = MagicMock()
        email_input = MagicMock()

        def deep(driver, css, **kwargs):
            if css == ra._CONNECT_DIALOG_CONTAINER_CSS:
                return [container]
            if css == ra._CONNECT_DIALOG_EMAIL_INPUT_CSS:
                return [email_input]
            return []

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._connect_dialog_wants_email(MagicMock()) is True

    def test_falls_back_to_the_dialogs_own_text(self):
        from cqc_lem.app.engagement import invites as ra
        container = MagicMock()

        def deep(driver, css, **kwargs):
            return [container] if css == ra._CONNECT_DIALOG_CONTAINER_CSS else []

        with patch(f"{_INV}.find_deep_elements", side_effect=deep), \
             patch(f"{_INV}._element_text", return_value=_VERIFICATION_TEXT):
            assert ra._connect_dialog_wants_email(MagicMock()) is True

    def test_no_dialog_container_at_all_is_unknown(self):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.find_deep_elements", return_value=[]):
            assert ra._connect_dialog_wants_email(MagicMock()) is None

    def test_an_email_input_outside_the_dialog_does_not_answer(self):
        from cqc_lem.app.engagement import invites as ra
        container = MagicMock()

        def deep(driver, css, **kwargs):
            if css == ra._CONNECT_DIALOG_CONTAINER_CSS:
                return [container]
            return []

        with patch(f"{_INV}.find_deep_elements", side_effect=deep), \
             patch(f"{_INV}._element_text", return_value=_ORDINARY_TEXT):
            assert ra._connect_dialog_wants_email(MagicMock()) is False


class TestFindConnectDialogEmailInput:
    def test_returns_the_input_found_inside_the_dialog_container(self):
        from cqc_lem.app.engagement import invites as ra
        container = MagicMock()
        email_input = MagicMock()

        def deep(driver, css, **kwargs):
            if css == ra._CONNECT_DIALOG_CONTAINER_CSS:
                return [container]
            if css == ra._CONNECT_DIALOG_EMAIL_INPUT_CSS and kwargs.get("root") is container:
                return [email_input]
            return []

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._find_connect_dialog_email_input(MagicMock()) is email_input

    def test_no_container_at_all_returns_none(self):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.find_deep_elements", return_value=[]):
            assert ra._find_connect_dialog_email_input(MagicMock()) is None

    def test_a_container_with_no_email_input_returns_none(self):
        from cqc_lem.app.engagement import invites as ra
        container = MagicMock()

        def deep(driver, css, **kwargs):
            return [container] if css == ra._CONNECT_DIALOG_CONTAINER_CSS else []

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._find_connect_dialog_email_input(MagicMock()) is None


class TestFillConnectDialogEmail:
    def test_types_the_email_into_the_element_that_answered(self):
        # THE ELEMENT THAT ANSWERED is what gets typed into — a shadow-mounted control cannot be
        # re-found by a fresh query that never saw it (#1733).
        from cqc_lem.app.engagement import invites as ra
        field = MagicMock()
        assert ra._fill_connect_dialog_email(field, "jane@example.com", 1) is True
        field.click.assert_called_once()
        field.clear.assert_called_once()
        field.send_keys.assert_called_once_with("jane@example.com")

    def test_no_input_found_returns_false(self):
        from cqc_lem.app.engagement import invites as ra
        assert ra._fill_connect_dialog_email(None, "jane@example.com", 1) is False

    def test_a_typing_failure_warns_without_ever_naming_the_email(self):
        from cqc_lem.app.engagement import invites as ra
        field = MagicMock()
        field.send_keys.side_effect = Exception("stale")
        with patch(f"{_INV}.log_warning") as warn:
            assert ra._fill_connect_dialog_email(field, "jane@example.com", 1) is False
        warn.assert_called_once()
        assert "jane@example.com" not in warn.call_args.args[0]


class TestOverlayEvidenceRedaction:
    def test_an_email_in_the_dialog_text_is_redacted_before_evidence_is_logged(self):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.find_deep_elements", return_value=[]), \
             patch(f"{_INV}._overlay_notice_text",
                   return_value="Enter jane@example.com to verify this connection."):
            _, text = ra._overlay_evidence(MagicMock())
        assert "jane@example.com" not in text
        assert "[redacted email]" in text


class TestInviteToConnectNowThreadsTheEmail:
    def _invite(self, wants_email, recipient_email=None, fill_result=True, submit_result=True,
                message=None, note_result=False):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin"), \
             patch(f"{_INV}._profile_is_first_degree", return_value=False), \
             patch(f"{_INV}._open_connect_invite_dialog", return_value=(True, None)), \
             patch(f"{_INV}._find_connect_dialog_email_input",
                   return_value=MagicMock() if wants_email else None), \
             patch(f"{_INV}._connect_dialog_wants_email", return_value=wants_email), \
             patch(f"{_INV}._fill_connect_dialog_email", return_value=fill_result) as fill, \
             patch(f"{_INV}._add_connect_note", return_value=note_result) as note, \
             patch(f"{_INV}._submit_connect_invite", return_value=submit_result) as submit, \
             patch(f"{_INV}.record_invite_dialog_miss") as miss, \
             patch(f"{_INV}.hold_invites") as hold, \
             patch(f"{_INV}.insert_new_log") as insert_log, \
             patch(f"{_INV}.record_action"), \
             patch(f"{_INV}.quit_gracefully"):
            sent, reason = ra.invite_to_connect_now(1, "https://x/in/jane", message,
                                                     recipient_email=recipient_email)
        return sent, reason, fill, note, submit, miss, hold, insert_log

    def test_wants_email_but_none_known_does_not_send_and_never_touches_the_miss_streak(self):
        from cqc_lem.utilities.db import EMAIL_VERIFICATION_REQUIRED_MESSAGE
        sent, reason, fill, note, submit, miss, hold, insert_log = self._invite(
            wants_email=True, recipient_email=None)
        assert sent is False and reason == EMAIL_VERIFICATION_REQUIRED_MESSAGE
        fill.assert_not_called()
        note.assert_not_called()
        submit.assert_not_called()   # never reaches the send click at all
        miss.assert_not_called()     # a target fact, not a selector miss
        hold.assert_not_called()     # not an account-level wall
        assert insert_log.call_args.kwargs["message"] == EMAIL_VERIFICATION_REQUIRED_MESSAGE

    def test_wants_email_and_known_fills_it_then_proceeds_to_send(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        sent, reason, fill, note, submit, miss, hold, insert_log = self._invite(
            wants_email=True, recipient_email="jane@example.com")
        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        fill.assert_called_once()
        assert fill.call_args.args[1] == "jane@example.com"
        submit.assert_called_once()
        miss.assert_not_called()
        hold.assert_not_called()

    def test_a_fill_failure_stops_without_submitting(self):
        from cqc_lem.utilities.db import EMAIL_VERIFICATION_REQUIRED_MESSAGE
        sent, reason, fill, note, submit, miss, hold, insert_log = self._invite(
            wants_email=True, recipient_email="jane@example.com", fill_result=False)
        assert sent is False and reason == EMAIL_VERIFICATION_REQUIRED_MESSAGE
        fill.assert_called_once()
        submit.assert_not_called()
        miss.assert_not_called()
        hold.assert_not_called()

    def test_a_note_transition_refills_the_email_before_submitting(self):
        sent, reason, fill, note, submit, miss, hold, insert_log = self._invite(
            wants_email=True, recipient_email="jane@example.com", message="Hi Jane", note_result=True)
        assert sent is True
        assert fill.call_count == 2
        submit.assert_called_once()

    def test_the_ordinary_variant_is_unaffected(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        sent, reason, fill, note, submit, miss, hold, insert_log = self._invite(wants_email=False)
        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        fill.assert_not_called()
        submit.assert_called_once()

    def test_unknown_reading_behaves_exactly_like_the_ordinary_variant(self):
        # Same posture as an undetectable restriction: unreadable falls through rather than skipping.
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        sent, reason, fill, note, submit, miss, hold, insert_log = self._invite(wants_email=None)
        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        fill.assert_not_called()
        submit.assert_called_once()
