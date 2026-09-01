"""An out-of-network target is a target FACT, not a selector miss (issue #1813, part A4).

Measured in production: `burkegriffin`, `scott-stephenson-` and `aditabraham` render no
custom-invite anchor, no Connect button and a `Follow` control. They are out of network. Failing on
them is correct — what was not correct is what the failure DID:

* it counted toward `record_invite_dialog_miss`, whose streak arms a SIX-HOUR hold on the whole
  lane, so one unreachable row braked every reachable row behind it;
* it left the `connection_requests` row 'approved', so the scanner re-dispatched it every cycle at
  ~90 s of Chrome on the shared `se_outreach` queue, for a page that will read the same forever.

So: a distinct reason, terminal on the first read, and neither the streak nor the hold. The state is
the #979 ladder's EXISTING 'failed' — terminal for sending, still re-read every run — because these
people already have a rung: following them is the only reach they offer.

The reading is fail-CLOSED throughout. Retiring a row ends a person's chance of an invite, and that
claim needs evidence the same way `_invite_restriction_reason`'s does.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.webdriver.common.by import By

from cqc_lem.utilities.db import FOLLOW_ONLY_MESSAGE, INVITE_LIMIT_REACHED_MESSAGE

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"
_SLUG = "burkegriffin"
_PROFILE = f"https://www.linkedin.com/in/{_SLUG}/"


def _control(label: str):
    element = MagicMock()
    element.get_attribute.side_effect = lambda name: label if name == "aria-label" else None
    element.text = label
    return element


def _anchor(slug: str):
    element = MagicMock()
    href = f"https://www.linkedin.com/preload/custom-invite/?vanityName={slug}"
    element.get_attribute.side_effect = lambda name: href if name == "href" else None
    element.text = "Connect"
    return element


def _driver(controls=(), anchors=(), title="Burke Griffin | LinkedIn"):
    driver = MagicMock()
    driver.title = title
    driver.current_url = _PROFILE

    def find_elements(by, value):
        if by == By.XPATH:
            return list(anchors)
        return [_control(label) for label in controls]

    driver.find_elements.side_effect = find_elements
    return driver


class TestTheClassBReading:

    def test_follow_and_nothing_connect_shaped_reads_as_out_of_network(self):
        """The production page: a top-card Follow, a Message, a More, and no invite anywhere."""
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = _driver(controls=["Follow Burke Griffin", "Message", "More actions"])
        assert _profile_offers_follow_only(driver, _SLUG) is True

    def test_a_bare_follow_counts_because_the_rail_is_never_bare(self):
        """Same trust a bare `Connect` gets — #1012's 2026-08-03 grounding, read for Follow."""
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        assert _profile_offers_follow_only(_driver(controls=["Follow", "Message"]), _SLUG) is True

    def test_an_already_followed_target_still_reads_as_out_of_network(self):
        """The #979 follow rung may have fired already; a followed stranger is no more connectable."""
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = _driver(controls=["Following Burke Griffin", "Message"])
        assert _profile_offers_follow_only(driver, _SLUG) is True


class TestTheReadingIsFailClosed:
    """Every clause has to be positively read. This claim RETIRES a person's row."""

    def test_the_targets_own_custom_invite_anchor_forfeits_the_reading(self):
        """Class A — the affordance IS there and the dialog still did not open. That is the bug."""
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = _driver(controls=["Follow Burke Griffin"], anchors=[_anchor(_SLUG)])
        assert _profile_offers_follow_only(driver, _SLUG) is False

    def test_a_rail_strangers_invite_anchor_does_not_forfeit_it(self):
        """The href names somebody else, so it says nothing about this target (#1012)."""
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = _driver(controls=["Follow Burke Griffin"], anchors=[_anchor("someone-else")])
        assert _profile_offers_follow_only(driver, _SLUG) is True

    def test_a_connect_button_naming_the_target_forfeits_the_reading(self):
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = _driver(controls=["Follow Burke Griffin", "Invite Burke Griffin to connect"])
        assert _profile_offers_follow_only(driver, _SLUG) is False

    def test_a_bare_connect_forfeits_the_reading(self):
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        assert _profile_offers_follow_only(_driver(controls=["Follow", "Connect"]), _SLUG) is False

    def test_a_pending_invite_is_the_ordinary_miss_not_a_target_fact(self):
        """`NO_CONNECT_BUTTON_MESSAGE`'s own text already says 'invite may already be pending'."""
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = _driver(controls=["Follow Burke Griffin", "Pending, Burke Griffin"])
        assert _profile_offers_follow_only(driver, _SLUG) is False

    def test_a_rail_follow_for_a_stranger_is_not_evidence_about_this_target(self):
        """A rail Follow names a stranger, so it says nothing about this target.

        'People also viewed' ships a Follow per card; an unattributed match would read every
        rail-bearing profile as out of network.
        """
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = _driver(controls=["Follow Someone Else", "Message"])
        assert _profile_offers_follow_only(driver, _SLUG) is False

    def test_a_named_follow_must_match_the_title_exactly_never_as_a_prefix(self):
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = _driver(controls=["Follow Burke"], title="Burke Griffin | LinkedIn")
        assert _profile_offers_follow_only(driver, _SLUG) is False

    def test_a_page_that_rendered_no_controls_is_not_a_reading(self):
        """An empty read says nothing about the target — calling it out of network retires them."""
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        assert _profile_offers_follow_only(_driver(controls=[]), _SLUG) is False

    def test_a_dom_read_that_raises_falls_through_to_the_ordinary_miss(self):
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        driver = _driver()
        driver.find_elements.side_effect = RuntimeError("boom")
        assert _profile_offers_follow_only(driver, _SLUG) is False

    def test_an_unresolvable_slug_forfeits_the_reading(self):
        """Nothing can be attributed without one, and this claim is terminal."""
        from cqc_lem.app.engagement.invites import _profile_offers_follow_only
        assert _profile_offers_follow_only(_driver(controls=["Follow"]), "") is False


class TestTheDialogRouteReportsIt:

    def _open(self, driver, follow_only, restriction=None):
        from cqc_lem.app.engagement import invites
        with patch.object(invites, "_click_own_custom_invite_anchor", return_value=False), \
             patch.object(invites, "_click_own_connect_button", return_value=False), \
             patch.object(invites, "click_first", return_value=None), \
             patch.object(invites, "_connect_dialog_present", return_value=False), \
             patch.object(invites, "_miss_evidence", return_value="profile evidence"), \
             patch.object(invites, "_invite_restriction_reason", return_value=restriction), \
             patch.object(invites, "_profile_offers_follow_only", return_value=follow_only), \
             patch.object(invites, "log_warning") as warn, \
             patch.object(invites, "log_info"):
            return invites._open_connect_invite_dialog(driver, MagicMock(), 1, _PROFILE), warn

    def test_a_follow_only_profile_returns_its_own_reason(self):
        driver = _driver()
        (opened, reason), _ = self._open(driver, follow_only=True)
        assert (opened, reason) == (False, FOLLOW_ONLY_MESSAGE)

    def test_it_never_warns_because_nothing_is_broken(self):
        """One grouped defect per out-of-network person in the queue is how a working lane pages."""
        _, warn = self._open(_driver(), follow_only=True)
        warn.assert_not_called()

    def test_the_url_route_is_skipped_because_there_is_nothing_to_preload(self):
        """A page with no connect affordance has nothing for the custom-invite URL to open."""
        driver = _driver()
        self._open(driver, follow_only=True)
        driver.get.assert_not_called()

    def test_a_named_account_wall_outranks_it_because_that_one_is_about_us(self):
        """The target is still owed their turn when it was OUR account that was stopped."""
        (opened, reason), _ = self._open(_driver(), follow_only=True,
                                         restriction=INVITE_LIMIT_REACHED_MESSAGE)
        assert (opened, reason) == (False, INVITE_LIMIT_REACHED_MESSAGE)

    def test_an_ordinary_miss_still_warns_and_still_takes_the_url_route(self):
        """Anti-vacuity: the new branch must not swallow the failure it was carved out of."""
        driver = _driver()
        (opened, reason), warn = self._open(driver, follow_only=False)
        assert (opened, reason) == (False, None)
        warn.assert_called_once()
        driver.get.assert_called_once()


class TestNeitherTheStreakNorTheHold:

    def _send(self, dialog_reason):
        from cqc_lem.app.engagement import invites
        with patch.object(invites, "get_user_password_pair_by_id", return_value=("e", "p")), \
             patch.object(invites, "get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch.object(invites, "login_to_linkedin"), \
             patch.object(invites, "_profile_is_first_degree", return_value=False), \
             patch.object(invites, "_open_connect_invite_dialog",
                          return_value=(False, dialog_reason)), \
             patch.object(invites, "insert_new_log") as logged, \
             patch.object(invites, "quit_gracefully"), \
             patch.object(invites, "hold_invites") as hold, \
             patch.object(invites, "record_invite_dialog_miss") as miss:
            result = invites.invite_to_connect_now(1, _PROFILE)
        return result, hold, miss, logged

    def test_an_out_of_network_target_never_brakes_the_lane(self):
        """The whole point of A4: it must cost the reachable rows behind it nothing."""
        (sent, reason), hold, miss, _ = self._send(FOLLOW_ONLY_MESSAGE)
        assert (sent, reason) == (False, FOLLOW_ONLY_MESSAGE)
        hold.assert_not_called()
        miss.assert_not_called()

    def test_the_reason_is_still_written_to_the_log_for_the_row(self):
        _, _, _, logged = self._send(FOLLOW_ONLY_MESSAGE)
        assert logged.call_args.kwargs["message"] == FOLLOW_ONLY_MESSAGE

    def test_an_ordinary_miss_still_counts_toward_the_streak(self):
        """Anti-vacuity: #1732's brake is still armed by the failure it was written for."""
        (_, reason), hold, miss, _ = self._send(None)
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        assert reason == NO_CONNECT_BUTTON_MESSAGE
        miss.assert_called_once_with(1)
        hold.assert_not_called()


class TestTheRowGoesTerminalOnTheFirstRead:

    def test_the_proactive_row_is_retired_without_burning_the_ceiling(self):
        """A PROVEN-unreachable target has nothing to learn from two more Chrome sessions."""
        from cqc_lem.app.engagement import invites
        req = {"id": 3, "user_id": 1, "recipient_profile_url": _PROFILE, "message": None,
               "status": "approved", "attempts": 0}
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.is_invites_held", return_value=False), \
             patch(f"{_INV}.invite_to_connect_now", return_value=(False, FOLLOW_ONLY_MESSAGE)), \
             patch("cqc_lem.utilities.db.record_connection_request_attempt",
                   return_value=(True, 1)) as rec, \
             patch(f"{_INV}.track_invite_outcome"), \
             patch(f"{_INV}.log_warning") as warn:
            out = invites.send_connection_request(3)

        rec.assert_called_once_with(3, FOLLOW_ONLY_MESSAGE, terminal=True)
        assert "failed" in out.lower() and "out of network" in out.lower()
        warn.assert_not_called()  # nothing is broken; this is somebody's network, not our selector


class TestTheRosterLadderKeepsItsOwnState:

    def _roster(self, reason):
        from cqc_lem.app.engagement import invites
        with patch.object(invites, "invite_to_connect_now", return_value=(False, reason)), \
             patch.object(invites, "set_target_connect_status") as status, \
             patch.object(invites, "log_debug"):
            invites.send_roster_connect_invite(user_id=1, profile_url=_PROFILE)
        return status

    def test_it_lands_on_the_ladders_existing_terminal_state(self):
        """No new state is invented.

        'failed' is terminal for SENDING and still re-read every run, so the follow rung goes on
        owning the target and a user who connects by hand clears the badge.
        """
        from cqc_lem.utilities.db import ConnectStatus
        status = self._roster(FOLLOW_ONLY_MESSAGE)
        status.assert_called_once_with(1, _PROFILE, ConnectStatus.FAILED)

    def test_an_account_wall_still_hands_the_target_back(self):
        """Anti-vacuity: the deferral for OUR wall is untouched by the target-fact branch."""
        from cqc_lem.utilities.db import ConnectStatus
        status = self._roster(INVITE_LIMIT_REACHED_MESSAGE)
        status.assert_called_once_with(1, _PROFILE, ConnectStatus.NEEDS_CONNECTION)
