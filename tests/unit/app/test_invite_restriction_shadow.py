"""The invite-restriction reader crosses the shadow boundary (issue #1813, part A2).

#1733 moved the Connect dialog into an open shadow root and taught ONE reader —
`_connect_dialog_present` — to cross it. `_invite_restriction_reason` was left on
`driver.find_elements`, which cannot, so a wall notice LinkedIn mounted in that overlay was
invisible to it. And because that reader returns None on an unreadable page BY DESIGN, the miss
fell through to the ordinary "no route opened the dialog" path: a walled account and a dead
selector wrote the identical warning. Production logs carried zero `_INVITE_LIMIT_RE` matches for
the whole nineteen days the proactive lane sent nothing, which under this reading is what you would
see whether or not a wall existed.

Where a claim is MOUNTED is not evidence about whether it was made. What must not change is the
other half of the posture: an unreadable page still returns None, because a restriction is a claim,
a claim needs evidence, and a failed read must never manufacture an account-wide hold.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.db import ACCOUNT_RESTRICTED_MESSAGE, INVITE_LIMIT_REACHED_MESSAGE

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"

_LIMIT_NOTICE = ("Dialog content start. You've reached the weekly invitation limit. "
                 "Try again next week. Dialog content end.")


def _node(text: str):
    element = MagicMock()
    element.text = text
    return element


def _driver(light_text: str = ""):
    """A driver whose LIGHT-DOM reads answer with `light_text` and nothing else."""
    driver = MagicMock()
    driver.find_elements.side_effect = lambda by, value: [_node(light_text)] if light_text else []
    return driver


def _deep(overlay_text: str = ""):
    """A `find_deep_elements` stand-in answering only the dialog-container query."""
    def _fake(driver, css, **kwargs):
        if "dialog" in css and overlay_text:
            return [_node(overlay_text)]
        return []
    return _fake


class TestAWallMountedInTheOverlayIsNamed:

    def test_a_weekly_limit_only_the_shadow_scan_can_see_is_reported(self):
        """The production shape: light DOM says nothing, the overlay says the account is walled."""
        with patch(f"{_INV}.find_deep_elements", side_effect=_deep(_LIMIT_NOTICE)):
            from cqc_lem.app.engagement.invites import _invite_restriction_reason
            assert _invite_restriction_reason(_driver()) == INVITE_LIMIT_REACHED_MESSAGE

    def test_an_account_restriction_in_the_overlay_outranks_a_limit(self):
        """Same precedence the light-DOM pass has always had — the notice, not its location."""
        notice = "We've restricted your account. You've reached the weekly invitation limit."
        with patch(f"{_INV}.find_deep_elements", side_effect=_deep(notice)):
            from cqc_lem.app.engagement.invites import _invite_restriction_reason
            assert _invite_restriction_reason(_driver()) == ACCOUNT_RESTRICTED_MESSAGE

    def test_the_light_dom_reading_still_works_on_its_own(self):
        """Anti-regression: the shadow pass ADDS text, it does not replace the old one."""
        with patch(f"{_INV}.find_deep_elements", side_effect=_deep()):
            from cqc_lem.app.engagement.invites import _invite_restriction_reason
            reason = _invite_restriction_reason(_driver(light_text=_LIMIT_NOTICE))
        assert reason == INVITE_LIMIT_REACHED_MESSAGE


class TestTheNoneOnUnreadablePostureSurvives:

    def test_a_page_with_nothing_on_it_still_returns_none(self):
        """A failed read must fall through to the ordinary miss, never invent an account hold."""
        with patch(f"{_INV}.find_deep_elements", side_effect=_deep()):
            from cqc_lem.app.engagement.invites import _invite_restriction_reason
            assert _invite_restriction_reason(_driver()) is None

    def test_an_ordinary_profile_page_is_not_a_wall(self):
        """The words have to be LinkedIn's — a profile that simply offers no Connect is not walled."""
        with patch(f"{_INV}.find_deep_elements", side_effect=_deep("Follow Jane Doe Message More")):
            from cqc_lem.app.engagement.invites import _invite_restriction_reason
            assert _invite_restriction_reason(_driver(light_text="Follow Jane Doe")) is None

    def test_a_shadow_scan_that_raises_cannot_cost_the_light_dom_answer(self):
        """Best-effort, like every other overlay read on this path."""
        with patch(f"{_INV}.find_deep_elements", side_effect=RuntimeError("boom")):
            from cqc_lem.app.engagement.invites import _invite_restriction_reason
            assert _invite_restriction_reason(_driver(light_text=_LIMIT_NOTICE)) == \
                INVITE_LIMIT_REACHED_MESSAGE

    def test_a_shadow_scan_that_raises_on_an_otherwise_blank_page_is_still_none(self):
        with patch(f"{_INV}.find_deep_elements", side_effect=RuntimeError("boom")):
            from cqc_lem.app.engagement.invites import _invite_restriction_reason
            assert _invite_restriction_reason(_driver()) is None


class TestOneReaderBehindTheLogAndTheDetector:
    """The words the miss line PRINTS are the words the detector MATCHED.

    Two separate readers would let a log line say "weekly invitation limit" while the detector,
    scanning something slightly different, still answered None — which is the exact confusion #1813
    spent nineteen days inside. `_overlay_notice_text` is the single seam that makes that
    impossible, so this pins that both callers go through it.
    """

    def test_the_evidence_line_and_the_restriction_read_the_same_text(self):
        from cqc_lem.app.engagement import invites

        with patch.object(invites, "_overlay_notice_text",
                          return_value=_LIMIT_NOTICE) as notice:
            _, evidence_text = invites._overlay_evidence(_driver())
            reason = invites._invite_restriction_reason(_driver())

        assert evidence_text == _LIMIT_NOTICE
        assert reason == INVITE_LIMIT_REACHED_MESSAGE
        assert notice.call_count == 2  # both callers, one reader

    def test_the_overlay_text_is_bounded_so_a_log_line_stays_one_line(self):
        from cqc_lem.app.engagement import invites

        with patch(f"{_INV}.find_deep_elements", side_effect=_deep("x " * 5000)):
            text = invites._overlay_notice_text(_driver())
        assert 0 < len(text) <= invites._OVERLAY_TEXT_LIMIT


class TestTheWallStillHoldsTheLaneEndToEnd:
    """A2 is only worth shipping if the named wall reaches the caller that acts on it."""

    def test_a_shadow_mounted_wall_holds_invites_instead_of_counting_a_miss(self):
        from cqc_lem.app.engagement import invites

        with patch.object(invites, "get_user_password_pair_by_id", return_value=("e", "p")), \
             patch.object(invites, "get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch.object(invites, "login_to_linkedin"), \
             patch.object(invites, "_profile_is_first_degree", return_value=False), \
             patch.object(invites, "_open_connect_invite_dialog",
                          return_value=(False, INVITE_LIMIT_REACHED_MESSAGE)), \
             patch.object(invites, "insert_new_log"), \
             patch.object(invites, "quit_gracefully"), \
             patch.object(invites, "hold_invites") as hold, \
             patch.object(invites, "record_invite_dialog_miss") as miss:
            sent, reason = invites.invite_to_connect_now(1, "https://x/in/jane")

        assert (sent, reason) == (False, INVITE_LIMIT_REACHED_MESSAGE)
        hold.assert_called_once()
        miss.assert_not_called()
