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
from selenium.common import StaleElementReferenceException, WebDriverException

from cqc_lem.utilities.db import (
    CONNECTION_REQUEST_SENT_MESSAGE,
    INVITE_ALREADY_PENDING_MESSAGE,
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

    @pytest.mark.parametrize("copy", [
        # The alternation the production string exercises, and the wordings around it. Only the
        # first is measured (#1867); the rest are reasoned from the same sentence and are here so a
        # rotation of the pronoun or the apostrophe does not silently reopen the defect.
        "please enter their email to connect",
        "To verify this member knows you",
        "please enter his email to connect",
        "please enter her email to connect",
        "please enter the member's email to connect",
        "please enter the member\u2019s email to connect",  # curly apostrophe
        "enter an email address to connect",
    ])
    def test_each_alternation_is_reachable(self, copy):
        from cqc_lem.app.engagement.invites import _EMAIL_CHALLENGE_RE
        assert _EMAIL_CHALLENGE_RE.search(copy)

    @pytest.mark.parametrize("copy", [
        "Add a note to your invitation?",
        "Personalize your invitation by adding a note.",
        "Your email address is verified.",  # names email, asks for nothing
        "LinkedIn members are more likely to accept invitations that include a note.",
    ])
    def test_ordinary_dialog_copy_never_matches(self, copy):
        """Anti-vacuity: a regex that matched any mention of `email` would confirm nothing."""
        from cqc_lem.app.engagement.invites import _EMAIL_CHALLENGE_RE
        assert _EMAIL_CHALLENGE_RE.search(copy) is None


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
        """The false-`sent` scenario: a rail card's badge for a stranger we invited earlier."""
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target(
            "Withdraw invitation sent to Harshal Karanpuriya", "Jane Doe") is False

    @pytest.mark.parametrize("label", ["Pending, click to withdraw invitation sent to Someone Else",
                                       "Invitation sent to Someone Else",
                                       "Withdraw invitation to Someone Else"])
    def test_every_shape_of_somebody_elses_badge_is_refused(self, label):
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target(label, "Jane Doe") is False

    def test_the_invitation_sent_rotation_is_recognised(self):
        """The alternative added to `_INVITE_PENDING_LABEL_RE` for this issue."""
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target("Invitation sent to Jane Doe", "Jane Doe") is True

    def test_a_named_control_with_no_readable_title_is_refused(self):
        """Fail closed: no identity to check against is not permission to assume one."""
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target("Withdraw invitation sent to Jane Doe", "") is False

    @pytest.mark.parametrize("label", ["Connect", "Message", "Follow Jane Doe", "More", ""])
    def test_a_control_that_is_not_pending_shaped_is_refused(self, label):
        from cqc_lem.app.engagement.invites import _pending_button_names_target
        assert _pending_button_names_target(label, "Jane Doe") is False


class TestTheReadIsScopedToTheTargetsOwnCard:
    """A rail card's pending badge may never be read as our outcome.

    `main` holds the "People also viewed" / "More profiles for you" rails as well as the top card;
    their cards are visible and come LATER in document order, so no control budget over `main` is a
    fence. The card is identified by the target's own name HEADING, and a section that cannot prove
    whose it is is not read at all (#1012 committed in a read instead of a click).
    """

    def _driver(self, title="Jane Doe | LinkedIn"):
        driver = MagicMock()
        driver.title = title
        return driver

    def _page(self, card_controls, *, heading="Jane Doe", rail_controls=(), sections=None):
        """A fake page: one attributable top card, plus an unattributable rail section."""
        from cqc_lem.app.engagement import invites as ra

        card = MagicMock(name="top-card")
        rail = MagicMock(name="rail")

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if css == ra._PROFILE_TOP_CARD_CSS:
                return list(sections) if sections is not None else [card, rail]
            if css == ra._PROFILE_NAME_HEADING_CSS:
                # Only the target's own card answers with the target's name; the rail's cards name
                # the strangers on them, which is exactly why the heading is the evidence.
                if root is card:
                    return [_control(heading)] if heading else []
                return [_control("Somebody Else")]
            if root is card:
                return [_control(label) for label in card_controls]
            if root is rail:
                return [_control(label) for label in rail_controls]
            return []

        return deep, card, rail

    def test_the_targets_own_pending_badge_confirms(self):
        from cqc_lem.app.engagement import invites as ra
        deep, _card, _rail = self._page(["Message", "More", "Pending"])
        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._pending_invite_affordance(self._driver()) is True

    def test_a_bare_pending_in_the_rail_does_not_confirm(self):
        """The blocking case: somebody in the rail has a pending invite from an earlier run.

        Their badge is bare, visible, inside `main`, and would have satisfied a budget-over-`main`
        read. It must not write the irreversible row this whole PR exists to prevent.
        """
        from cqc_lem.app.engagement import invites as ra
        deep, _card, _rail = self._page(["Message", "More", "Connect"],
                                        rail_controls=["Pending", "Connect"])
        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._pending_invite_affordance(self._driver()) is False

    def test_a_card_that_cannot_be_attributed_is_never_read(self):
        """No heading naming the target: the section proves nothing, so nothing is read off it."""
        from cqc_lem.app.engagement import invites as ra
        deep, _card, _rail = self._page(["Pending"], heading="")
        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._pending_invite_affordance(self._driver()) is False

    def test_an_unreadable_page_title_refuses_before_any_scan(self):
        """No identity to check a card against is not permission to assume one."""
        from cqc_lem.app.engagement import invites as ra
        deep, _card, _rail = self._page(["Pending"])
        with patch(f"{_INV}.find_deep_elements", side_effect=deep) as find:
            assert ra._pending_invite_affordance(self._driver(title="LinkedIn")) is False
        find.assert_not_called()

    def test_a_page_with_no_sections_reads_as_not_pending(self):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.find_deep_elements", return_value=[]):
            assert ra._pending_invite_affordance(self._driver()) is False

    def test_a_card_that_yields_no_controls_reads_as_not_pending(self):
        """An empty read is not a reading — the posture `_profile_offers_follow_only` keeps."""
        from cqc_lem.app.engagement import invites as ra
        deep, _card, _rail = self._page([])
        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._pending_invite_affordance(self._driver()) is False

    def test_a_top_card_still_offering_connect_reads_as_not_pending(self):
        from cqc_lem.app.engagement import invites as ra
        deep, _card, _rail = self._page(["Connect", "Message", "More"])
        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._pending_invite_affordance(self._driver()) is False

    def test_the_card_is_found_by_its_heading_not_by_being_first(self):
        """Position is a hint for WHICH sections to check, never the evidence."""
        from cqc_lem.app.engagement import invites as ra
        card = MagicMock(name="top-card")
        decoy = MagicMock(name="decoy")

        def deep(driver, css, *, visible_only=True, limit=20, root=None):
            if css == ra._PROFILE_TOP_CARD_CSS:
                return [decoy, card]  # the target's card is NOT first
            if css == ra._PROFILE_NAME_HEADING_CSS:
                return [_control("Jane Doe")] if root is card else [_control("Promoted")]
            return [_control("Pending")] if root is card else [_control("Connect")]

        with patch(f"{_INV}.find_deep_elements", side_effect=deep):
            assert ra._pending_invite_affordance(self._driver()) is True

    def test_the_scan_asks_for_visible_elements_only(self):
        """The top card renders a 0x0 sticky-header duplicate of every control (#1790 grounding).

        `visible_only` is the only thing separating it from the real one, so every query on this
        path must ask for it — document order gets the right copy today by luck.
        """
        from cqc_lem.app.engagement import invites as ra
        deep, _card, _rail = self._page(["Pending"])
        seen: list = []

        def recording(driver, css, *, visible_only=True, limit=20, root=None):
            seen.append(visible_only)
            return deep(driver, css, visible_only=visible_only, limit=limit, root=root)

        with patch(f"{_INV}.find_deep_elements", side_effect=recording):
            ra._pending_invite_affordance(self._driver())
        assert seen and all(seen)


def _confirm(dialog_present, pending, overlay=""):
    """Run `_confirm_invite_outcome` against a given page reading, capturing its logs."""
    from cqc_lem.app.engagement import invites as ra
    overlays = overlay if callable(overlay) else (lambda _driver: overlay)
    with patch(f"{_INV}._connect_dialog_present", side_effect=dialog_present), \
         patch(f"{_INV}._pending_invite_affordance", side_effect=pending), \
         patch(f"{_INV}._overlay_notice_text", side_effect=overlays), \
         patch(f"{_INV}.WebDriverWait") as wait, \
         patch(f"{_INV}.time.sleep") as sleep, \
         patch(f"{_INV}.log_warning") as warn, \
         patch(f"{_INV}.log_debug") as debug:
        verdict = ra._confirm_invite_outcome(MagicMock(), user_id=1)
    return _Confirmed(verdict=verdict, warn=warn, debug=debug, sleep=sleep, wait=wait)


class _Confirmed:
    """The verdict plus every seam `_confirm_invite_outcome` was watched through."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestTheThreeVerdicts:
    def test_a_closed_dialog_plus_a_pending_top_card_is_the_only_send(self):
        r = _confirm(dialog_present=lambda *a, **k: False, pending=lambda *a: True)
        assert r.verdict == CONNECTION_REQUEST_SENT_MESSAGE
        r.warn.assert_not_called()

    def test_the_email_challenge_is_named_and_never_sent(self):
        r = _confirm(dialog_present=lambda *a, **k: True, pending=lambda *a: False,
                     overlay=_CHALLENGE_OVERLAY)
        assert r.verdict == INVITE_EMAIL_CHALLENGE_MESSAGE
        # DEBUG, not WARNING: an expected, named target fact. A repeated log_warning re-emits at
        # ERROR and files ONE grouped $exception (src/cqc_lem/utilities/CLAUDE.md), which would page
        # us once per unverifiable person in the queue for behaviour #1836 already owns.
        r.warn.assert_not_called()
        r.debug.assert_called_once()
        assert r.debug.call_args.kwargs["user_id"] == 1

    def test_an_unreadable_page_is_not_a_send_and_warns_once(self):
        r = _confirm(dialog_present=lambda *a, **k: False, pending=lambda *a: False)
        assert r.verdict == INVITE_UNCONFIRMED_MESSAGE
        r.warn.assert_called_once()  # genuinely anomalous — this one IS the escalating log

    def test_a_dialog_that_never_closed_is_not_a_send_even_with_a_pending_badge(self):
        """BOTH halves are required.

        A dialog still on screen after Send means the flow did not complete, and a pending badge
        from an earlier invite must not paper over that.
        """
        r = _confirm(dialog_present=lambda *a, **k: True, pending=lambda *a: True)
        assert r.verdict == INVITE_UNCONFIRMED_MESSAGE

    def test_a_closed_dialog_with_no_pending_badge_is_not_a_send(self):
        """The other half, and the one that matters most.

        An email-challenge dialog dismissing without sending lands here, and "the dialog went away"
        is exactly the click-shaped evidence this issue rejects.
        """
        r = _confirm(dialog_present=lambda *a, **k: False, pending=lambda *a: False,
                     overlay=_ORDINARY_OVERLAY)
        assert r.verdict == INVITE_UNCONFIRMED_MESSAGE

    def test_every_verdict_has_a_dashboard_word(self):
        """Anti-vacuity on the reason map: an unmapped verdict degrades silently to `error`.

        The verdicts are message constants rather than an enum (the pattern the whole rail already
        uses for its result strings), so the map is what makes the PostHog breakdown real. This is
        the check an enum would have given for free.
        """
        from cqc_lem.app.engagement import invites as ra
        for verdict in (CONNECTION_REQUEST_SENT_MESSAGE, INVITE_EMAIL_CHALLENGE_MESSAGE,
                        INVITE_UNCONFIRMED_MESSAGE, INVITE_ALREADY_PENDING_MESSAGE):
            assert ra._invite_outcome_reason(verdict) != ra.INVITE_REASON_UNMAPPED


class TestTheReadsFailClosedWhenTheyThrow:
    """A read that RAISES must be that attempt's "no evidence", never an escape.

    The confirmation reads the top card while it re-renders behind a dismissing dialog — the
    likeliest `StaleElementReferenceException` site in the flow. An escape here would skip the
    retry, skip the verdict, and lose the one `log_warning` this path owes.
    """

    def test_a_stale_read_is_retried_and_can_still_confirm(self):
        readings = iter([StaleElementReferenceException("gone"), False])

        def dialog_present(*_a, **_k):
            answer = next(readings)
            if isinstance(answer, Exception):
                raise answer
            return answer

        r = _confirm(dialog_present=dialog_present, pending=lambda *a: True)
        assert r.verdict == CONNECTION_REQUEST_SENT_MESSAGE
        r.warn.assert_not_called()

    def test_a_read_that_never_recovers_lands_on_unconfirmed(self):
        def boom(*_a, **_k):
            raise WebDriverException("session died")

        r = _confirm(dialog_present=boom, pending=lambda *a: True)
        assert r.verdict == INVITE_UNCONFIRMED_MESSAGE
        r.warn.assert_called_once()  # the anomaly log still fires — it is not swallowed
        assert r.debug.call_count == 3  # one breadcrumb per lost attempt

    def test_an_unreadable_overlay_does_not_hide_a_confirmed_send(self):
        """The overlay read is only reached when the send was NOT confirmed."""
        def boom(_driver):
            raise WebDriverException("overlay gone")

        r = _confirm(dialog_present=lambda *a, **k: False, pending=lambda *a: True, overlay=boom)
        assert r.verdict == CONNECTION_REQUEST_SENT_MESSAGE


class TestTheReadIsGivenTimeToSettle:
    def test_a_top_card_that_re_renders_late_still_confirms(self):
        """A top card that re-renders late still confirms.

        The dialog dismisses with an animation and the card re-renders behind it, so the first read
        after the click is expected to be inconclusive.
        """
        # First read: the dialog is still dismissing. Second: it is gone and the card has caught up.
        readings = iter([True, False, False])
        pendings = iter([True, True])
        r = _confirm(dialog_present=lambda *a, **k: next(readings),
                     pending=lambda *a: next(pendings))
        assert r.verdict == CONNECTION_REQUEST_SENT_MESSAGE
        assert r.sleep.call_count == 1  # one settle, not a spin
        r.warn.assert_not_called()

    def test_the_retry_budget_is_bounded(self):
        from cqc_lem.app.engagement import invites as ra
        r = _confirm(dialog_present=lambda *a, **k: False, pending=lambda *a: False)
        assert r.verdict == INVITE_UNCONFIRMED_MESSAGE
        assert r.sleep.call_args_list == [call(ra._INVITE_CONFIRM_SETTLE_SECONDS)] * (
            ra._INVITE_CONFIRM_ATTEMPTS - 1)

    def test_the_dialog_probe_uses_its_own_short_wait(self):
        """Not the session wait.

        `_connect_dialog_present` blocks on `wait.until`, so reusing the 10 s session wait would
        cost a full timeout per negative check — three of them for one unconfirmed verdict, on the
        path that fires most often when LinkedIn is challenging, against a shared Chrome pool.
        """
        from cqc_lem.app.engagement import invites as ra
        r = _confirm(dialog_present=lambda *a, **k: False, pending=lambda *a: False)
        assert r.wait.call_args.args[1] == ra._INVITE_CONFIRM_READ_TIMEOUT_SECONDS
        assert ra._INVITE_CONFIRM_READ_TIMEOUT_SECONDS < 10


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
        sent, reason, confirm, insert_log, _action = self._run(INVITE_UNCONFIRMED_MESSAGE)
        assert (sent, reason) == (False, INVITE_UNCONFIRMED_MESSAGE)
        confirm.assert_called_once()
        assert insert_log.call_args.kwargs["result"] == LogResultType.FAILURE

    @pytest.mark.parametrize("verdict", [CONNECTION_REQUEST_SENT_MESSAGE,
                                         INVITE_EMAIL_CHALLENGE_MESSAGE,
                                         INVITE_UNCONFIRMED_MESSAGE])
    def test_every_dispatched_send_charges_the_pacing_envelope(self, verdict):
        """The row fails CLOSED; the envelope fails OPEN, and they are different questions.

        The row is a claim about what LinkedIn did. The envelope is a claim about how hard we
        pushed, and a Send we could not read is still a Send we clicked — LinkedIn may well have
        counted it. Pacing UNDER the true figure is the direction that gets an account restricted,
        which is what `ACCOUNT_RESTRICTED_MESSAGE` exists for.
        """
        _sent, _reason, _confirm, _log, record_action = self._run(verdict)
        record_action.assert_called_once()

    def test_a_send_that_was_never_dispatched_charges_nothing(self):
        """No Send affordance was clickable, so nothing was pushed at LinkedIn."""
        from cqc_lem.app.engagement import invites as ra

        def click(driver, wait, xpath, label, **kwargs):
            raise Exception(f"no element for {xpath}")

        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin"), \
             patch(f"{_INV}._profile_is_first_degree", return_value=False), \
             patch(f"{_INV}._open_connect_invite_dialog", return_value=(True, None)), \
             patch(f"{_INV}.click_element_wait_retry", side_effect=click), \
             patch(f"{_INV}.time.sleep"), \
             patch(f"{_INV}.insert_new_log"), \
             patch(f"{_INV}.record_action") as record_action, \
             patch(f"{_INV}.quit_gracefully"), \
             patch(f"{_INV}.log_error"):
            ra.invite_to_connect_now(1, "https://x/in/jane")
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


def _attempt_recorded(request_id, reason, terminal=False):
    """Stand-in for `record_connection_request_attempt`, honouring its `(terminal, attempts)` shape.

    It ECHOES `terminal` the way the real statement does (`bool(terminal) or attempts >= ceiling`),
    so a caller that retires a row cannot be tested against a stub that quietly says it did not.
    """
    return bool(terminal), 1


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
                   side_effect=_attempt_recorded) as attempt, \
             patch("cqc_lem.utilities.db.update_connection_request_status") as status, \
             patch(f"{_INV}.track_invite_outcome") as track, \
             patch(f"{_INV}.log_debug"), patch(f"{_INV}.log_warning"):
            ra.send_connection_request(9)
        return attempt, status, track

    def test_an_email_challenge_never_writes_a_sent_row_and_never_requeues(self):
        """Row 9 is one of the ten falsely marked 'sent'.

        Terminal on the FIRST read, like the out-of-network reading it sits beside: until #1836
        ships, the challenge is a permanent property of that TARGET, so an attempt counter would
        only reach the same place N Chrome sessions later. The row lands 'failed' carrying
        `INVITE_EMAIL_CHALLENGE_MESSAGE`, which is how #1836 finds them to re-approve.
        """
        attempt, status, track = self._dispatch(INVITE_EMAIL_CHALLENGE_MESSAGE)
        status.assert_not_called()
        attempt.assert_called_once_with(9, INVITE_EMAIL_CHALLENGE_MESSAGE, terminal=True)
        assert track.call_args.args[1:3] == ("failed", "email_challenge")

    def test_an_unconfirmed_send_never_writes_a_sent_row_but_keeps_its_turn(self):
        """`unconfirmed` is NOT terminal: a read that failed says nothing about the target.

        If the send was real, the retry finds the invite pending and
        `INVITE_ALREADY_PENDING_MESSAGE` closes the row honestly.
        """
        attempt, status, track = self._dispatch(INVITE_UNCONFIRMED_MESSAGE)
        status.assert_not_called()
        attempt.assert_called_once_with(9, INVITE_UNCONFIRMED_MESSAGE, terminal=False)
        assert track.call_args.args[1:3] == ("deferred", "unconfirmed")

    def test_an_already_pending_invite_closes_the_row_as_sent(self):
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.db import ConnectionRequestStatus
        req = {"id": 9, "user_id": 1, "recipient_profile_url": "https://x/in/wfalcon",
               "message": "hi", "status": "approved", "attempts": 1}
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.is_invites_held", return_value=False), \
             patch(f"{_INV}.invite_to_connect_now",
                   return_value=(True, INVITE_ALREADY_PENDING_MESSAGE)), \
             patch("cqc_lem.utilities.db.update_connection_request_status") as status, \
             patch(f"{_INV}.track_invite_outcome") as track:
            ra.send_connection_request(9)
        status.assert_called_once_with(9, ConnectionRequestStatus.SENT)
        # `sent`, with its own word: the operator can still tell a confirmed dispatch from a row
        # closed by evidence found on a later visit.
        assert track.call_args.args[1:3] == ("sent", "already_pending")

    @pytest.mark.parametrize("message, word", [
        (INVITE_EMAIL_CHALLENGE_MESSAGE, "email_challenge"),
        (INVITE_UNCONFIRMED_MESSAGE, "unconfirmed"),
        (INVITE_ALREADY_PENDING_MESSAGE, "already_pending"),
    ])
    def test_each_new_reason_has_its_own_dashboard_word(self, message, word):
        """Each new reason has its own dashboard word.

        A new reason is a VALUE on the existing `invite_outcome` event, never a new capture — and
        never `error`, which is what an unmapped message would silently become.
        """
        from cqc_lem.app.engagement.invites import _invite_outcome_reason
        assert _invite_outcome_reason(message) == word


class TestTheWidenedPendingRegexDidNotMoveTheOtherCaller:
    """`_INVITE_PENDING_LABEL_RE` has two callers that want opposite things from it.

    `_pending_invite_affordance` reads it as POSITIVE evidence an invite is out.
    `_profile_offers_follow_only` reads the same words as a DISQUALIFIER: an already-pending invite
    is the ordinary `NO_CONNECT_BUTTON_MESSAGE` case, not the out-of-network target fact that
    retires a row on its first read (#1813). Adding `invitation sent` had to leave that intact.
    """

    _SLUG = "burkegriffin"

    def _driver(self, controls, title="Burke Griffin | LinkedIn"):
        from selenium.webdriver.common.by import By
        driver = MagicMock()
        driver.title = title

        def find_elements(by, value):
            return [] if by == By.XPATH else [_control(label) for label in controls]

        driver.find_elements.side_effect = find_elements
        return driver

    def test_follow_plus_nothing_else_is_still_out_of_network(self):
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = self._driver(["Follow Burke Griffin", "Message", "More actions"])
        assert _profile_offers_follow_only(driver, self._SLUG) is True

    @pytest.mark.parametrize("pending_label", ["Pending", "Withdraw", "Invitation sent"])
    def test_a_pending_badge_still_forfeits_the_follow_only_reading(self, pending_label):
        """Including the `invitation sent` alternative this PR added.

        Grading one of these as out-of-network would retire a reachable person on the first read,
        which is exactly what `_profile_offers_follow_only`'s fail-closed posture exists to prevent.
        """
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = self._driver(["Follow Burke Griffin", pending_label])
        assert _profile_offers_follow_only(driver, self._SLUG) is False


class TestAnInviteAlreadyPendingClosesTheRow:
    """The return path for a real send that read as `unconfirmed` (#1867).

    The row went back to 'approved'; the retry finds NO Connect button, because the invitation is
    pending. Without this it lands on `no_connect_affordance` forever — the same undercount this
    issue is about, one step downstream, plus a selector-shaped reason for a working invite.
    """

    def _visit(self, dialog_reason, pending):
        from cqc_lem.app.engagement import invites as ra
        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin"), \
             patch(f"{_INV}._profile_is_first_degree", return_value=False), \
             patch(f"{_INV}._open_connect_invite_dialog", return_value=(False, dialog_reason)), \
             patch(f"{_INV}._pending_invite_affordance", return_value=pending), \
             patch(f"{_INV}.record_invite_dialog_miss") as miss, \
             patch(f"{_INV}.hold_invites") as hold, \
             patch(f"{_INV}.record_action") as record_action, \
             patch(f"{_INV}.insert_new_log") as insert_log, \
             patch(f"{_INV}.quit_gracefully"), \
             patch(f"{_INV}.log_warning"):
            sent, reason = ra.invite_to_connect_now(1, "https://x/in/jane")
        return sent, reason, miss, hold, record_action, insert_log

    def test_a_visible_pending_affordance_closes_the_row_as_sent(self):
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE, LogResultType
        sent, reason, miss, _hold, record_action, insert_log = self._visit(
            NO_CONNECT_BUTTON_MESSAGE, pending=True)
        assert (sent, reason) == (True, INVITE_ALREADY_PENDING_MESSAGE)
        assert insert_log.call_args.kwargs["result"] == LogResultType.SUCCESS
        miss.assert_not_called()  # the route is not broken; the invite is simply already out
        # Nothing was pushed at LinkedIn on THIS visit, so the envelope is not charged again.
        record_action.assert_not_called()

    def test_no_pending_affordance_is_still_the_ordinary_miss(self):
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        sent, reason, miss, _hold, _action, _log = self._visit(NO_CONNECT_BUTTON_MESSAGE,
                                                               pending=False)
        assert (sent, reason) == (False, NO_CONNECT_BUTTON_MESSAGE)
        miss.assert_called_once()  # the streak still arms on a genuine selector miss (#1732)

    def test_an_account_wall_is_never_reread_as_a_pending_invite(self):
        """A wall is about the ACCOUNT and must still hold the lane.

        The pending read only ever rescues the ORDINARY miss — letting it answer a limit notice
        would turn a walled account into a queue of phantom sends.
        """
        from cqc_lem.utilities.db import INVITE_LIMIT_REACHED_MESSAGE
        sent, reason, _miss, hold, _action, _log = self._visit(INVITE_LIMIT_REACHED_MESSAGE,
                                                               pending=True)
        assert (sent, reason) == (False, INVITE_LIMIT_REACHED_MESSAGE)
        hold.assert_called_once()

    def test_a_follow_only_profile_is_never_reread_as_a_pending_invite(self):
        from cqc_lem.utilities.db import FOLLOW_ONLY_MESSAGE
        sent, reason, miss, hold, _action, _log = self._visit(FOLLOW_ONLY_MESSAGE, pending=True)
        assert (sent, reason) == (False, FOLLOW_ONLY_MESSAGE)
        miss.assert_not_called()
        hold.assert_not_called()
