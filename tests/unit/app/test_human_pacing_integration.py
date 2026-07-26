"""The pacing engine wired into the engagement lanes (issue #626).

test_human_pacing.py covers the engine itself; this file covers the seams — that the dispatchers
actually pass a jittered countdown, that the lanes spend the paced budget rather than the raw cap,
and that the governor's accounting is fed by the code paths that really post.
"""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"


@pytest.fixture
def pacing_on(monkeypatch):
    monkeypatch.setenv("HUMAN_PACING_ENABLED", "true")
    monkeypatch.setenv("PACING_SEED", "issue-626-integration")
    monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")


class TestBeatDispatchJitter:
    """Every beat-dispatched engagement task must start off the crontab minute."""

    def test_golden_hour_commenting_is_dispatched_with_a_jitter_countdown(self, pacing_on):
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_RS}._stagger_due", return_value=True), \
             patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.automate_commenting") as ac:
            auto_daily_engagement()
        assert 30 * 60 <= ac.apply_async.call_args[1]["countdown"] <= 90 * 60

    def test_appreciation_dms_are_dispatched_with_a_jitter_countdown(self, pacing_on):
        from cqc_lem.app.run_scheduler import auto_appreciate_dms
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_RS}._stagger_due", return_value=True), \
             patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.automate_appreciation_dms_for_user") as dm:
            auto_appreciate_dms()
        assert 30 * 60 <= dm.apply_async.call_args[1]["countdown"] <= 90 * 60

    def test_own_post_reply_sweeps_stay_fast(self, pacing_on):
        """G7 exemption: replying to comments on YOUR OWN post is human, so the sweep keeps the
        RESPONSIVE profile — jittered by seconds, never delayed by an hour."""
        from cqc_lem.app.run_scheduler import dispatch_scheduled_reply_sweeps
        with patch(f"{_RS}.get_users_with_reply_mode", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_engagement_preferences", return_value={"reply_sweeps_per_day": 2}), \
             patch(f"{_RS}.sweep_reply_comments") as sweep:
            dispatch_scheduled_reply_sweeps()
        assert 0 <= sweep.apply_async.call_args[1]["countdown"] <= 180

    def test_pacing_off_restores_the_immediate_dispatch(self, monkeypatch):
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        monkeypatch.setenv("HUMAN_PACING_ENABLED", "false")
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_RS}._stagger_due", return_value=True), \
             patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.automate_commenting") as ac:
            auto_daily_engagement()
        assert ac.apply_async.call_args[1]["countdown"] == 0


class TestGoldenHourSweepJitter:
    def test_sweeps_are_jittered_but_never_reordered(self, pacing_on):
        from cqc_lem.app.run_automation import (_golden_hour_sweep_countdowns, _GOLDEN_HOUR_MINUTES,
                                                _GOLDEN_HOUR_MAX_SWEEPS)
        for n in (1, 3, 6, _GOLDEN_HOUR_MAX_SWEEPS):
            cds = _golden_hour_sweep_countdowns(n)
            step = _GOLDEN_HOUR_MINUTES * 60 / n
            assert len(cds) == n
            assert cds == sorted(cds) and len(set(cds)) == n     # ordered, no collisions
            for i, cd in enumerate(cds, start=1):
                assert step * i <= cd <= step * i + step / 2      # forward-only, ≤ half a step

    def test_a_misconfigured_jitter_cannot_push_a_sweep_past_the_next_one(self, pacing_on,
                                                                          monkeypatch):
        from cqc_lem.app.run_automation import _golden_hour_sweep_countdowns
        monkeypatch.setenv("PACING_RESPONSIVE_JITTER_MAX_SECONDS", "100000")
        cds = _golden_hour_sweep_countdowns(3)
        assert cds == sorted(cds) and len(set(cds)) == 3

    def test_the_publish_time_no_longer_predicts_the_sweep_minute(self, pacing_on):
        from cqc_lem.app.run_automation import _golden_hour_sweep_countdowns
        runs = {tuple(_golden_hour_sweep_countdowns(3)) for _ in range(30)}
        assert len(runs) > 1  # landing on 20:00/40:00/60:00 after every publish is a signature


class TestPacedBudgetsAtTheLanes:
    def test_feed_commenting_stops_when_the_paced_budget_is_spent(self):
        """The cap says 20 and nothing has been spent, but today's DRAW is 0 (rest day) — the run
        must stop, which the raw-cap check could never do."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.count_comments_today", return_value=0), \
             patch(f"{_RA}.remaining_actions", return_value=0) as remaining, \
             patch(f"{_RA}.get_recent_engagers", return_value=set()):
            posted = ra.comment_on_feed_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1,
                                               prefs={"max_comments_per_day": 20})
        assert posted == 0
        assert remaining.call_args[0][2] == 20                      # the cap it drew against
        assert remaining.call_args[1]["caps"]                       # governor engaged

    def test_scheduled_dms_defer_on_the_paced_budget(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.db import ScheduledDmStatus
        dm = {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/jane",
              "message": "hi jane", "status": "approved"}
        with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=dm), \
             patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0), \
             patch(f"{_RA}.get_engagement_preferences", return_value={"max_dms_per_day": 10}), \
             patch(f"{_RA}.remaining_actions", return_value=0), \
             patch(f"{_RA}.send_dm_now") as send, \
             patch("cqc_lem.utilities.db.update_scheduled_dm_status") as upd:
            out = ra.send_scheduled_dm(3)
        send.assert_not_called()
        upd.assert_called_once_with(3, ScheduledDmStatus.APPROVED)  # deferred, not failed
        assert "deferred" in out

    def test_connection_requests_spend_the_paced_invite_budget(self):
        from cqc_lem.app.run_scheduler import auto_check_connection_requests
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_connection_requests", return_value=[(11, 7), (12, 7)]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[7]), \
             patch(f"{_RS}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_RS}.count_invites_sent_today", return_value=0), \
             patch(f"{_RS}.remaining_actions", return_value=1), \
             patch(f"{_RS}.update_connection_request_status"), \
             patch(f"{_RS}.get_orphaned_connection_requests", return_value=[]), \
             patch(f"{_RS}.send_connection_request") as send:
            result = auto_check_connection_requests()
        assert send.apply_async.call_count == 1   # budget of 1, not the cap of 10
        assert "1 connection request" in result

    def test_connection_request_dispatch_is_jittered(self, pacing_on):
        from cqc_lem.app.run_scheduler import auto_check_connection_requests
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_approved_connection_requests", return_value=[(11, 7)]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[7]), \
             patch(f"{_RS}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_RS}.count_invites_sent_today", return_value=0), \
             patch(f"{_RS}.update_connection_request_status"), \
             patch(f"{_RS}.get_orphaned_connection_requests", return_value=[]), \
             patch(f"{_RS}.send_connection_request") as send:
            auto_check_connection_requests()
        assert 30 * 60 <= send.apply_async.call_args[1]["countdown"] <= 90 * 60


class TestGovernorAccounting:
    def test_a_landed_comment_is_reported_to_the_governor(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT
        with patch(f"{_RA}.claim_post_for_comment", return_value=True), \
             patch(f"{_RA}.select_blueprint", return_value={"format": "expander"}), \
             patch(f"{_RA}.generate_ai_response", return_value="A real comment."), \
             patch(f"{_RA}._author_is_me", return_value=False), \
             patch(f"{_RA}.react_to_post_inline", return_value=True), \
             patch(f"{_RA}.mark_post_reacted"), \
             patch(f"{_RA}.post_comment_inline", return_value=True), \
             patch(f"{_RA}.mark_post_commented"), \
             patch(f"{_RA}.insert_new_log"), \
             patch(f"{_RA}.pace_read", return_value=0.0), \
             patch(f"{_RA}.time.sleep"), \
             patch(f"{_RA}.record_action") as recorded:
            ok = ra._engage_card(MagicMock(), MagicMock(), MagicMock(), 1, MagicMock(),
                                 "feedurn://x", "a post body", "Jane", {}, "synth", [], [])
        assert ok is True
        recorded.assert_called_once_with(1, ACTION_COMMENT)

    def test_a_skipped_comment_is_not_reported(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.claim_post_for_comment", return_value=True), \
             patch(f"{_RA}.select_blueprint", return_value={"format": "expander"}), \
             patch(f"{_RA}.generate_ai_response", return_value=None), \
             patch(f"{_RA}.release_post_claim"), \
             patch(f"{_RA}.record_action") as recorded:
            ok = ra._engage_card(MagicMock(), MagicMock(), MagicMock(), 1, MagicMock(),
                                 "feedurn://x", "a post body", "Jane", {}, "synth", [], [])
        assert ok is False
        recorded.assert_not_called()

    def test_a_sent_invite_is_reported_and_a_failed_one_is_not(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.human_pacing import ACTION_INVITE
        for sent in (True, False):
            connect = MagicMock() if sent else MagicMock(side_effect=Exception("no button"))
            with patch(f"{_RA}.get_user_password_pair_by_id", return_value=("e", "p")), \
                 patch(f"{_RA}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
                 patch(f"{_RA}.login_to_linkedin"), \
                 patch(f"{_RA}._profile_is_first_degree", return_value=False), \
                 patch(f"{_RA}.click_element_wait_retry", connect), \
                 patch(f"{_RA}.log_error"), \
                 patch(f"{_RA}.insert_new_log"), \
                 patch(f"{_RA}.quit_gracefully"), \
                 patch(f"{_RA}.record_action") as recorded:
                invite_sent, _reason = ra.invite_to_connect_now(1, "https://x/in/jane")
            assert invite_sent is sent
            if sent:
                recorded.assert_called_once_with(1, ACTION_INVITE)
            else:
                recorded.assert_not_called()

    def test_a_sent_dm_is_reported_and_a_failed_one_is_not(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.human_pacing import ACTION_DM
        for sent in (True, False):
            with patch(f"{_RA}.get_user_password_pair_by_id", return_value=("e", "p")), \
                 patch(f"{_RA}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
                 patch(f"{_RA}.login_to_linkedin"), \
                 patch(f"{_RA}.click_element_wait_retry"), \
                 patch(f"{_RA}.get_element_wait_retry",
                       return_value=MagicMock() if sent else None), \
                 patch(f"{_RA}.simulate_typing"), \
                 patch(f"{_RA}.time.sleep"), \
                 patch(f"{_RA}.insert_new_log"), \
                 patch(f"{_RA}.quit_gracefully"), \
                 patch(f"{_RA}.record_action") as recorded:
                result = ra.send_dm_now(1, "https://x/in/jane", "hi")
            assert result is sent
            if sent:
                recorded.assert_called_once_with(1, ACTION_DM)
            else:
                recorded.assert_not_called()
