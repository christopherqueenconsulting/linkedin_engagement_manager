"""A connection request is 'sent' when the invitation EXISTS, never when the click landed (#1867).

Ten `connection_requests` rows for user 1 were written 'sent' on 2026-09-01 (06:52 → 07:18) and not
one of the invitations reached LinkedIn — confirmed by the account owner against the live "Sent
invitations" list. `_submit_connect_invite` answered True the instant `WebElement.click()` did not
raise, and nothing downstream re-read the page. That is the #1013 rule inverted:

    success is the OUTCOME being present, never a click having landed

The production dialog says why the click could not have sent. `_overlay_notice_text` on
`https://www.linkedin.com/in/wfalcon` read:

    'Add a note to your invitation? To verify this member knows you, please enter their email to
     connect. You can also include a personal note. Learn why Add a note Send without a note'

`Send without a note` is present and clickable in BOTH the ordinary dialog and this email-challenge
one. Only one of them sends. So the verdict is three-valued and fails CLOSED — an unreadable page is
not a send.
"""

from unittest.mock import MagicMock, call, patch

import pytest

from cqc_lem.utilities.db import (
    CONNECTION_REQUEST_SENT_MESSAGE,
    INVITE_EMAIL_CHALLENGE_MESSAGE,
    INVITE_NOT_SENT_MESSAGE,
    INVITE_UNCONFIRMED_MESSAGE,
)

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"

# Byte-for-byte the two overlay readings the A1 diagnostic (#1813) dumped for this issue.
_CHALLENGE_OVERLAY = (
    "Dialog content start. Add a note to your invitation? To verify this member knows you, please "
    "enter their email to connect. You can also include a personal note. Learn why Add a note Send "
    "without a note Dialog content end.")
_ORDINARY_OVERLAY = (
    "Dialog content start. Add a note to your invitation? Personalize your invitation to Kaitlyn "
    "Albertoli by adding a note. LinkedIn members are more likely to accept invitations that "
    "include a note. Add a note Send without a note Dialog content end.")


def _control(label: str):
    element = MagicMock()
    element.get_attribute.side_effect = lambda name: label if name == "aria-label" else None
    element.text = label
    return element


class TestTheEmailChallengeIsRecognisedByItsOwnWords:
    """The challenge is read off the overlay's own words.

    `_overlay_notice_text` is also what `_invite_restriction_reason` matches on, so a log line and
    the verdict can never tell different stories about what the dialog said (#1813).
    """

    def test_the_production_challenge_copy_matches(self):
        from cqc_lem.app.engagement.invites import _EMAIL_CHALLENGE_RE
        assert _EMAIL_CHALLENGE_RE.search(_CHALLENGE_OVERLAY)

    def test_the_ordinary_dialog_copy_does_not(self):
        """Both dialogs offer `Send without a note`; only one of them wants an email address."""
        from cqc_lem.app.engagement.invites import _EMAIL_CHALLENGE_RE
        assert _EMAIL_CHALLENGE_RE.search(_ORDINARY_OVERLAY) is None

    def test_an_empty_overlay_is_not_a_challenge(self):
        from cqc_lem.app.engagement.invites import _EMAIL_CHALLENGE_RE
        assert _EMAIL_CHALLENGE_RE.search("") is None


class TestThePendingAffordanceIsAttributedToTheTarget:
    """The pending badge must belong to the loaded profile, not to a rail card.

    The #1012 hazard read for a badge instead of a button: somebody in "People also viewed" may
    genuinely have a pending invite from an earlier run, and reading THEIR badge as our outcome is
    a false 'sent' row.
    """

    @pytest.mark.parametrize("label", ["Pending", "Invitation sent", "Withdraw",
                                       "Pending, click to withdraw"])
    def test_a_bare_pending_control_is_trusted(self, label):
        """A label that names nobody is the top card's own.

        The rail's controls always carry a name (#1012's 2026-08-03 grounding). `Pending, click to
        withdraw` counts as bare despite the word `to` — parsing a trailing name would have read it
        as an invite pending for somebody called "withdraw".
        """
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target(label, "Jane Doe") is True

    def test_a_control_naming_the_target_is_trusted(self):
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target(
            "Pending, click to withdraw invitation sent to Jane Doe", "Jane Doe") is True

    def test_punctuation_is_not_a_reason_to_refuse_a_real_send(self):
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target(
            "Withdraw invitation sent to Jean-Luc Picard", "Jean-Luc Picard") is True

    def test_a_control_naming_somebody_else_is_refused(self):
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target(
            "Withdraw invitation sent to Harshal Karanpuriya", "Jane Doe") is False

    def test_a_named_control_with_no_readable_title_is_refused(self):
        """Fail closed: no identity to check against is not permission to assume one."""
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target("Withdraw invitation sent to Jane Doe", "") is False

    @pytest.mark.parametrize("label", ["Connect", "Message", "Follow Jane Doe", "More", ""])
    def test_a_control_that_is_not_pending_shaped_is_refused(self, label):
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target(label, "Jane Doe") is False


class TestThePendingReadIsScopedAndFailsClosed:
    def _driver(self, title="Jane Doe | LinkedIn"):
        driver = MagicMock()
        driver.title = title
        return driver

    def test_the_control_scan_is_rooted_in_main_not_the_document(self):
        """The top-card scan is rooted in `main`, never the document.

        `find_deep_elements` truncates in DOCUMENT ORDER, so an unscoped scan spends its budget on
        the global nav before it reaches the target's own card — the #1813 A3 trap, visible in this
        issue's `overlay controls=[...]` dumps, which contain only nav chrome.
        """
        from cqc_lem.app.engagement import invites as ra

        main = MagicMock(name="main")

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if css == ra._PROFILE_MAIN_CSS:
                return [main]
            assert root is main, "the top-card scan must be scoped to main"
            return [_control("Message"), _control("Pending")]

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._pending_invite_affordance(self._driver()) is True

    def test_a_page_with_no_main_reads_as_not_pending(self):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.find_deep_elements", return_value=[]):
            assert ra._pending_invite_affordance(self._driver()) is False

    def test_a_main_that_yields_no_controls_reads_as_not_pending(self):
        """An empty read is not a reading — the same posture `_profile_offers_follow_only` keeps."""
        from cqc_lem.app.engagement import invites as ra

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            return [MagicMock()] if css == ra._PROFILE_MAIN_CSS else []

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._pending_invite_affordance(self._driver()) is False

    def test_a_top_card_still_offering_connect_reads_as_not_pending(self):
        from cqc_lem.app.engagement import invites as ra

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if css == ra._PROFILE_MAIN_CSS:
                return [MagicMock()]
            return [_control("Connect"), _control("Message"), _control("More")]

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._pending_invite_affordance(self._driver()) is False


def _confirm(dialog_present, pending, overlay=""):
    """Run `_confirm_invite_outcome` against a given page reading, capturing its logs."""
    from cqc_lem.app.engagement import invites as ra
    with patch(f"{_INV}._connect_dialog_present", side_effect=dialog_present), \
         patch(f"{_INV}._pending_invite_affordance", side_effect=pending), \
         patch(f"{_INV}._overlay_notice_text", return_value=overlay), \
         patch(f"{_INV}.time.sleep") as sleep, \
         patch(f"{_INV}.log_warning") as warn, \
         patch(f"{_INV}.log_debug") as debug:
        verdict = ra._confirm_invite_outcome(MagicMock(), MagicMock(), user_id=1)
    return verdict, warn, debug, sleep


class TestTheThreeVerdicts:
    def test_a_closed_dialog_plus_a_pending_top_card_is_the_only_send(self):
        verdict, warn, _debug, _sleep = _confirm(dialog_present=lambda *a, **k: False,
                                                 pending=lambda *a: True)
        assert verdict == CONNECTION_REQUEST_SENT_MESSAGE
        warn.assert_not_called()

    def test_the_email_challenge_is_named_and_never_sent(self):
        verdict, warn, debug, _sleep = _confirm(dialog_present=lambda *a, **k: True,
                                                pending=lambda *a: False,
                                                overlay=_CHALLENGE_OVERLAY)
        assert verdict == INVITE_EMAIL_CHALLENGE_MESSAGE
        # DEBUG, not WARNING: an expected, named target fact. A repeated log_warning re-emits at
        # ERROR and files ONE grouped $exception (src/cqc_lem/utilities/CLAUDE.md), which would page
        # us once per unverifiable person in the queue for behaviour #1836 already owns.
        warn.assert_not_called()
        debug.assert_called_once()
        assert debug.call_args.kwargs["user_id"] == 1

    def test_an_unreadable_page_is_not_a_send_and_warns_once(self):
        verdict, warn, _debug, _sleep = _confirm(dialog_present=lambda *a, **k: False,
                                                 pending=lambda *a: False)
        assert verdict == INVITE_UNCONFIRMED_MESSAGE
        warn.assert_called_once()  # genuinely anomalous — this one IS the escalating log

    def test_a_dialog_that_never_closed_is_not_a_send_even_with_a_pending_badge(self):
        """BOTH halves are required.

        A dialog still on screen after Send means the flow did not complete, and a pending badge
        from an earlier invite must not paper over that.
        """
        verdict, _warn, _debug, _sleep = _confirm(dialog_present=lambda *a, **k: True,
                                                  pending=lambda *a: True)
        assert verdict == INVITE_UNCONFIRMED_MESSAGE

    def test_a_closed_dialog_with_no_pending_badge_is_not_a_send(self):
        """The other half, and the one that matters most.

        An email-challenge dialog dismissing without sending lands here, and "the dialog went away"
        is exactly the click-shaped evidence this issue rejects.
        """
        verdict, _warn, _debug, _sleep = _confirm(dialog_present=lambda *a, **k: False,
                                                  pending=lambda *a: False,
                                                  overlay=_ORDINARY_OVERLAY)
        assert verdict == INVITE_UNCONFIRMED_MESSAGE


class TestTheReadIsGivenTimeToSettle:
    def test_a_top_card_that_re_renders_late_still_confirms(self):
        """A top card that re-renders late still confirms.

        The dialog dismisses with an animation and the card re-renders behind it, so the first read
        after the click is expected to be inconclusive.
        """
        # First read: the dialog is still dismissing. Second: it is gone and the card has caught up.
        readings = iter([True, False, False])
        pendings = iter([True, True])
        verdict, warn, _debug, sleep = _confirm(
            dialog_present=lambda *a, **k: next(readings), pending=lambda *a: next(pendings))
        assert verdict == CONNECTION_REQUEST_SENT_MESSAGE
        assert sleep.call_count == 1  # one settle, not a spin
        warn.assert_not_called()

    def test_the_retry_budget_is_bounded(self):
        from cqc_lem.app.engagement import invites as ra
        verdict, _warn, _debug, sleep = _confirm(dialog_present=lambda *a, **k: False,
                                                 pending=lambda *a: False)
        assert verdict == INVITE_UNCONFIRMED_MESSAGE
        assert sleep.call_args_list == [call(ra._INVITE_CONFIRM_SETTLE_SECONDS)] * (
            ra._INVITE_CONFIRM_ATTEMPTS - 1)


class TestTheCoreRailRecordsTheConfirmedOutcome:
    """The core rail records the confirmed outcome, not the click.

    End to end through the real `invite_to_connect_now`: the click lands, and the verdict is still
    whatever the page says afterwards.
    """

    def _run(self, confirmed):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin"), \
             patch(f"{_INV}._profile_is_first_degree", return_value=False), \
             patch(f"{_INV}._open_connect_invite_dialog", return_value=(True, None)), \
             patch(f"{_INV}.click_element_wait_retry", return_value=MagicMock()), \
             patch(f"{_INV}._confirm_invite_outcome", return_value=confirmed) as confirm, \
             patch(f"{_INV}.time.sleep"), \
             patch(f"{_INV}.insert_new_log") as insert_log, \
             patch(f"{_INV}.record_action") as record_action, \
             patch(f"{_INV}.clear_invite_dialog_misses"), \
             patch(f"{_INV}.quit_gracefully"):
            sent, reason = ra.invite_to_connect_now(1, "https://x/in/jane")
        return sent, reason, confirm, insert_log, record_action

    def test_a_landed_click_that_cannot_be_confirmed_is_not_a_send(self):
        from cqc_lem.utilities.db import LogResultType
        sent, reason, confirm, insert_log, record_action = self._run(INVITE_UNCONFIRMED_MESSAGE)
        assert (sent, reason) == (False, INVITE_UNCONFIRMED_MESSAGE)
        confirm.assert_called_once()
        assert insert_log.call_args.kwargs["result"] == LogResultType.FAILURE
        # The account-level pacing governor counts real invites; a send we cannot see is not one.
        record_action.assert_not_called()

    def test_the_email_challenge_reaches_the_caller_by_name(self):
        sent, reason, _confirm, _log, _action = self._run(INVITE_EMAIL_CHALLENGE_MESSAGE)
        assert (sent, reason) == (False, INVITE_EMAIL_CHALLENGE_MESSAGE)

    def test_a_confirmed_invite_still_records_a_send(self):
        from cqc_lem.utilities.db import LogResultType
        sent, reason, _c, insert_log, record_action = self._run(CONNECTION_REQUEST_SENT_MESSAGE)
        assert (sent, reason) == (True, CONNECTION_REQUEST_SENT_MESSAGE)
        assert insert_log.call_args.kwargs["result"] == LogResultType.SUCCESS
        record_action.assert_called_once()

    def test_a_dialog_with_no_send_button_never_reaches_the_confirmation(self):
        """A dialog with no Send button never reaches the confirmation.

        `_submit_connect_invite` still owns the #573 failure — nothing was clicked, so there is no
        outcome to read and no second grouped issue to file.
        """
        from cqc_lem.app.engagement import invites as ra

        def click(driver, wait, xpath, label, **kwargs):
            raise Exception(f"no element for {xpath}")

        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin"), \
             patch(f"{_INV}._profile_is_first_degree", return_value=False), \
             patch(f"{_INV}._open_connect_invite_dialog", return_value=(True, None)), \
             patch(f"{_INV}.click_element_wait_retry", side_effect=click), \
             patch(f"{_INV}._confirm_invite_outcome") as confirm, \
             patch(f"{_INV}.time.sleep"), \
             patch(f"{_INV}.insert_new_log"), \
             patch(f"{_INV}.quit_gracefully"), \
             patch(f"{_INV}.log_error"):
            sent, reason = ra.invite_to_connect_now(1, "https://x/in/jane")

        assert (sent, reason) == (False, INVITE_NOT_SENT_MESSAGE)
        confirm.assert_not_called()


class TestTheProactiveRowAndTheEvent:
    def _dispatch(self, reason):
        from cqc_lem.app.engagement import invites as ra
        req = {"id": 9, "user_id": 1, "recipient_profile_url": "https://x/in/wfalcon",
               "message": "hi", "status": "approved", "attempts": 0}
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.is_invites_held", return_value=False), \
             patch(f"{_INV}.invite_to_connect_now", return_value=(False, reason)), \
             patch("cqc_lem.utilities.db.record_connection_request_attempt",
                   return_value=(False, 1)) as attempt, \
             patch("cqc_lem.utilities.db.update_connection_request_status") as status, \
             patch(f"{_INV}.track_invite_outcome") as track, \
             patch(f"{_INV}.log_debug"), patch(f"{_INV}.log_warning"):
            ra.send_connection_request(9)
        return attempt, status, track

    def test_an_email_challenge_never_writes_a_sent_row(self):
        """Row 9 is one of the ten falsely marked 'sent'.

        `record_connection_request_attempt` returns it to 'approved' below the ceiling, so #1836
        can still clear it — it is simply never 'sent'.
        """
        attempt, status, track = self._dispatch(INVITE_EMAIL_CHALLENGE_MESSAGE)
        status.assert_not_called()
        attempt.assert_called_once_with(9, INVITE_EMAIL_CHALLENGE_MESSAGE, terminal=False)
        assert track.call_args.args[1:3] == ("deferred", "email_challenge")

    def test_an_unconfirmed_send_never_writes_a_sent_row(self):
        attempt, status, track = self._dispatch(INVITE_UNCONFIRMED_MESSAGE)
        status.assert_not_called()
        attempt.assert_called_once_with(9, INVITE_UNCONFIRMED_MESSAGE, terminal=False)
        assert track.call_args.args[1:3] == ("deferred", "unconfirmed")

    @pytest.mark.parametrize("message, word", [
        (INVITE_EMAIL_CHALLENGE_MESSAGE, "email_challenge"),
        (INVITE_UNCONFIRMED_MESSAGE, "unconfirmed"),
    ])
    def test_each_new_reason_has_its_own_dashboard_word(self, message, word):
        """Each new reason has its own dashboard word.

        A new reason is a VALUE on the existing `invite_outcome` event, never a new capture — and
        never `error`, which is what an unmapped message would silently become.
        """
        from cqc_lem.app.engagement.invites import _invite_outcome_reason
        assert _invite_outcome_reason(message) == word
