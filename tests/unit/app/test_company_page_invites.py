"""Company-page invites: the paced daily drip that replaced the monthly credit blast (issue #732).

The behaviours worth protecting are the ones that made the old path bot-like: it selected as many
invitees as there were credits, recursed into itself until the pool was empty, ignored
`max_invites_per_day` entirely, and did all of it on one crontab at 05:00 UTC on the 1st.
"""

import random
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_CPI = "cqc_lem.utilities.linkedin.company_page_inviter"
_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"
_DB = "cqc_lem.utilities.db"


class TestCreditSpread:
    """Credits are a CEILING, not a target — the pool has to last the month."""

    def test_days_left_counts_today(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import days_left_in_month
        assert days_left_in_month(date(2026, 7, 1)) == 31
        assert days_left_in_month(date(2026, 7, 31)) == 1
        assert days_left_in_month(date(2026, 2, 28)) == 1  # 2026 is not a leap year

    def test_full_pool_on_the_first_is_spread_across_the_month(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import credit_spread_budget
        assert credit_spread_budget(250, date(2026, 7, 1)) == 8   # not 250
        assert credit_spread_budget(50, date(2026, 7, 1)) == 1     # the reduced 2026 allowance

    def test_a_thin_pool_late_in_the_month_still_drips(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import credit_spread_budget
        assert credit_spread_budget(3, date(2026, 7, 20)) == 1

    def test_no_credits_is_zero_never_the_floor_of_one(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import credit_spread_budget
        assert credit_spread_budget(0, date(2026, 7, 10)) == 0
        assert credit_spread_budget(None, date(2026, 7, 10)) == 0
        assert credit_spread_budget(-5, date(2026, 7, 10)) == 0

    def test_the_last_day_of_the_month_may_spend_what_is_left(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import credit_spread_budget
        # The spread stops holding credits back once there is no month left to hold them for; the
        # user's own daily cap is what keeps this from becoming the old blast.
        assert credit_spread_budget(40, date(2026, 7, 31)) == 40


class TestInviteCap:
    def test_the_account_wide_invite_cap_is_the_harder_ceiling(self):
        """`max_invites_per_day` is what brand_account's phase policy clamps, so it must win."""
        from cqc_lem.utilities.linkedin.company_page_inviter import invite_cap_for_user
        assert invite_cap_for_user({"max_company_page_invites_per_day": 25,
                                    "max_invites_per_day": 5}) == 5
        assert invite_cap_for_user({"max_company_page_invites_per_day": 3,
                                    "max_invites_per_day": 10}) == 3

    def test_a_zero_on_either_cap_turns_the_lane_off(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import invite_cap_for_user
        assert invite_cap_for_user({"max_company_page_invites_per_day": 0,
                                    "max_invites_per_day": 10}) == 0
        assert invite_cap_for_user({"max_company_page_invites_per_day": 5,
                                    "max_invites_per_day": 0}) == 0

    def test_a_null_page_cap_reads_as_the_default_not_as_off(self):
        """Every row predating the migration has NULL here — reading it as 0 would silently switch
        page invites off for the whole fleet."""
        from cqc_lem.utilities.linkedin.company_page_inviter import invite_cap_for_user
        from cqc_lem.utilities.db import COMPANY_PAGE_INVITES_PER_DAY_DEFAULT
        assert invite_cap_for_user({"max_company_page_invites_per_day": None,
                                    "max_invites_per_day": 10}) == COMPANY_PAGE_INVITES_PER_DAY_DEFAULT

    def test_a_garbage_value_falls_back_rather_than_raising(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import invite_cap_for_user
        assert invite_cap_for_user({"max_company_page_invites_per_day": "lots",
                                    "max_invites_per_day": 10}) == 5
        assert invite_cap_for_user(None) == 0  # no prefs -> no account-wide cap -> nothing sends

    def test_the_brand_accounts_p0_phase_ceiling_is_respected(self):
        from cqc_lem.utilities.brand_account import brand_outbound_policy
        from cqc_lem.utilities.linkedin.company_page_inviter import invite_cap_for_user
        policy = brand_outbound_policy("P0")
        assert invite_cap_for_user({"max_company_page_invites_per_day": 50, **policy}) == \
            policy["max_invites_per_day"]


class TestPlanDailyInvites:
    def _plan(self, prefs, remaining=4, sent_today=0):
        from cqc_lem.utilities.linkedin.company_page_inviter import plan_daily_invites
        with patch(f"{_CPI}.count_company_page_invites_sent_today", return_value=sent_today), \
             patch(f"{_CPI}.remaining_actions", return_value=remaining) as rem:
            return plan_daily_invites(1, prefs=prefs), rem

    def test_the_allowance_comes_from_the_pacing_engine_not_the_raw_cap(self):
        from cqc_lem.utilities.human_pacing import ACTION_INVITE
        plan, rem = self._plan({"max_company_page_invites_per_day": 5, "max_invites_per_day": 10},
                               remaining=2, sent_today=3)
        assert plan["allowance"] == 2
        assert plan["cap"] == 5
        assert plan["sent_today"] == 3
        args = rem.call_args[0]
        assert args[1] == ACTION_INVITE and args[2] == 5 and args[3] == 3
        assert rem.call_args[1]["caps"]  # the account-level envelope is engaged

    def test_a_zero_cap_never_asks_the_pacing_engine(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_DISABLED
        plan, rem = self._plan({"max_company_page_invites_per_day": 0, "max_invites_per_day": 10})
        assert plan["allowance"] == 0
        assert plan["status"] == INVITE_STATUS_DISABLED
        rem.assert_not_called()

    def test_a_spent_budget_reports_why(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_BUDGET_REACHED
        plan, _ = self._plan({"max_company_page_invites_per_day": 5, "max_invites_per_day": 10},
                             remaining=0, sent_today=5)
        assert plan["allowance"] == 0
        assert plan["status"] == INVITE_STATUS_BUDGET_REACHED

    def test_a_rest_day_reads_back_as_zero(self):
        """human_pacing's rest day zeroes the lane — the plan must carry that through untouched."""
        plan, _ = self._plan({"max_company_page_invites_per_day": 5, "max_invites_per_day": 10},
                             remaining=-1)
        assert plan["allowance"] == 0


class TestAutomateInvitations:
    """The Selenium half: one pass, bounded by the smallest of the three ceilings."""

    credit_reads = 0

    def _run(self, allowance=5, credits=(200, 250), selected=None, day=date(2026, 7, 10),
             invite_ok=True, page_url="https://www.linkedin.com/company/acme",
             count_credit_reads=False):
        from cqc_lem.utilities.linkedin import company_page_inviter as cpi
        real_spread = cpi.credit_spread_budget
        self.credit_reads = 0

        def _credits(*_a, **_kw):
            self.credit_reads += 1
            return credits
        driver, wait = MagicMock(), MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/"
        plan = {"allowance": allowance, "cap": 5, "sent_today": 0,
                "status": "sent" if allowance > 0 else "budget_reached"}
        picked = MagicMock(side_effect=lambda d, w, limit: limit if selected is None else selected)
        with patch(f"{_CPI}.get_user_password_pair_by_id", return_value=("a@b.c", "pw")), \
             patch(f"{_CPI}.get_company_linked_in_url_for_user", return_value=page_url), \
             patch(f"{_CPI}.login_to_linkedin"), \
             patch(f"{_CPI}.get_available_credits", side_effect=_credits), \
             patch(f"{_CPI}.select_connection_checkboxes", picked), \
             patch(f"{_CPI}.invite_selected_connections", return_value=invite_ok), \
             patch(f"{_CPI}.dismiss_prompt", return_value=False), \
             patch(f"{_CPI}.insert_new_log") as log, \
             patch(f"{_CPI}.record_action") as rec, \
             patch(f"{_CPI}.credit_spread_budget",
                   side_effect=lambda c, d=None: real_spread(c, day)), \
             patch(f"{_CPI}.time.sleep"):
            report = cpi.automate_invitations(driver, wait, 1, plan=plan)
        return report, picked, log, rec

    def test_it_selects_at_most_the_daily_budget_never_all_the_credits(self):
        report, picked, _, _ = self._run(allowance=3, credits=(200, 250))
        assert picked.call_args[1]["limit"] == 3 if picked.call_args[1] else True
        assert picked.call_args[0][2] == 3
        assert report["invites_sent"] == 3
        assert report["credits_remaining"] == 200

    def test_the_credit_spread_binds_when_it_is_smaller_than_the_cap(self):
        # 40 credits with 22 days left in July -> 1/day, even though the user allows 5.
        report, picked, _, _ = self._run(allowance=5, credits=(40, 250), day=date(2026, 7, 10))
        assert picked.call_args[0][2] == 1
        assert report["credit_spread"] == 1

    def test_the_live_credit_count_is_the_last_ceiling(self):
        report, picked, _, _ = self._run(allowance=5, credits=(2, 250), day=date(2026, 7, 31))
        assert picked.call_args[0][2] == 2
        assert report["invites_sent"] == 2

    def test_zero_credits_stops_before_anything_is_selected(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_CREDITS_EXHAUSTED
        report, picked, log, rec = self._run(credits=(0, 250))
        assert report["status"] == INVITE_STATUS_CREDITS_EXHAUSTED
        picked.assert_not_called()
        log.assert_not_called()
        rec.assert_not_called()

    def test_it_never_recurses(self):
        """The old path called itself while selected_count < credits, draining the pool in one
        sitting. Selecting fewer than the budget used to be the exact trigger; one run is now
        exactly one batch — one credit read, one selection pass, one log row."""
        report, picked, log, _ = self._run(allowance=5, credits=(250, 250),
                                           selected=2, day=date(2026, 7, 1), count_credit_reads=True)
        assert self.credit_reads == 1
        assert picked.call_count == 1
        assert log.call_count == 1
        assert report["invites_sent"] == 2

    def test_a_spent_budget_never_opens_the_invite_page(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_BUDGET_REACHED
        report, picked, log, _ = self._run(allowance=0)
        assert report["status"] == INVITE_STATUS_BUDGET_REACHED
        picked.assert_not_called()
        log.assert_not_called()

    def test_the_success_log_carries_the_countable_marker(self):
        from cqc_lem.utilities.db import COMPANY_PAGE_INVITE_SENT_MESSAGE
        _, _, log, _ = self._run(allowance=4, credits=(200, 250))
        assert log.call_args[1]["message"] == f"{COMPANY_PAGE_INVITE_SENT_MESSAGE}: 4"

    def test_the_batch_is_reported_to_the_account_governor(self):
        from cqc_lem.utilities.human_pacing import ACTION_INVITE
        _, _, _, rec = self._run(allowance=4, credits=(200, 250))
        rec.assert_called_once_with(1, ACTION_INVITE, 4)

    def test_nothing_selected_is_reported_rather_than_logged_as_a_send(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_NO_CANDIDATES
        report, _, log, rec = self._run(allowance=4, selected=0)
        assert report["status"] == INVITE_STATUS_NO_CANDIDATES
        assert report["invites_sent"] == 0
        log.assert_not_called()
        rec.assert_not_called()

    def test_a_missing_invite_button_is_a_failure_not_a_silent_send(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_FAILED
        report, _, log, rec = self._run(allowance=4, invite_ok=False)
        assert report["status"] == INVITE_STATUS_FAILED
        assert log.call_args[1]["message"].startswith("Failed to invite")
        rec.assert_not_called()

    def test_no_company_page_stops_before_login(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_NO_PAGE
        report, picked, _, _ = self._run(page_url=None)
        assert report["status"] == INVITE_STATUS_NO_PAGE
        picked.assert_not_called()


class TestSelectionPacing:
    def test_selections_are_spaced_by_a_bounded_human_pause(self):
        from cqc_lem.utilities.linkedin import company_page_inviter as cpi
        with patch(f"{_CPI}.pacing_enabled", return_value=True), \
             patch(f"{_CPI}.time.sleep") as slept:
            for seed in range(25):
                delay = cpi._pause_between_selections(random.Random(seed))
                assert cpi.SELECTION_PAUSE_MIN_SECONDS <= delay <= cpi.SELECTION_PAUSE_MAX_SECONDS
        assert slept.call_count == 25

    def test_pacing_disabled_restores_the_old_no_pause_behaviour(self):
        from cqc_lem.utilities.linkedin import company_page_inviter as cpi
        with patch(f"{_CPI}.pacing_enabled", return_value=False), \
             patch(f"{_CPI}.time.sleep") as slept:
            assert cpi._pause_between_selections() == 0.0
        slept.assert_not_called()


class TestInviteTask:
    def _run_task(self, plan, paused=False):
        from cqc_lem.app.run_automation import automate_invites_to_company_page_for_user
        with patch(f"{_RA}.is_automation_paused", return_value=paused), \
             patch(f"{_RA}.plan_daily_invites", return_value=plan), \
             patch(f"{_RA}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())) as drv, \
             patch(f"{_RA}.quit_gracefully"), \
             patch(f"{_RA}.automate_invitations",
                   return_value={"status": "sent", "invites_sent": 3}) as run, \
             patch(f"{_RA}.track_company_page_invite_run") as track:
            result = automate_invites_to_company_page_for_user.run(user_id=1)
        return result, drv, run, track

    def test_a_zero_allowance_never_costs_a_chrome_slot(self):
        _, drv, run, track = self._run_task({"allowance": 0, "status": "budget_reached",
                                             "cap": 5, "sent_today": 5})
        drv.assert_not_called()
        run.assert_not_called()
        assert track.call_args[0][1]["status"] == "budget_reached"

    def test_a_paused_account_stands_down_and_says_so(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_PAUSED
        _, drv, run, track = self._run_task({"allowance": 5, "status": "sent"}, paused=True)
        drv.assert_not_called()
        run.assert_not_called()
        assert track.call_args[0][1]["status"] == INVITE_STATUS_PAUSED

    def test_a_real_budget_runs_the_batch_and_emits_the_run(self):
        result, drv, run, track = self._run_task({"allowance": 5, "status": "sent",
                                                  "cap": 5, "sent_today": 0})
        drv.assert_called_once()
        assert run.call_args[1]["plan"]["allowance"] == 5
        assert track.call_args[0][1]["invites_sent"] == 3
        assert "3 people" in result

    def test_a_selenium_failure_still_emits_a_run_and_quits_the_driver(self):
        from cqc_lem.app.run_automation import automate_invites_to_company_page_for_user
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_FAILED
        with patch(f"{_RA}.is_automation_paused", return_value=False), \
             patch(f"{_RA}.plan_daily_invites",
                   return_value={"allowance": 5, "status": "sent", "cap": 5, "sent_today": 0}), \
             patch(f"{_RA}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_RA}.quit_gracefully") as quit_driver, \
             patch(f"{_RA}.automate_invitations", side_effect=RuntimeError("boom")), \
             patch(f"{_RA}.log_error"), \
             patch(f"{_RA}.track_company_page_invite_run") as track:
            automate_invites_to_company_page_for_user.run(user_id=1)
        quit_driver.assert_called_once()
        assert track.call_args[0][1]["status"] == INVITE_STATUS_FAILED


class TestDailyBeat:
    def test_the_beat_dispatches_only_users_whose_slot_is_due(self):
        from cqc_lem.app.run_scheduler import auto_invite_to_company_pages, STAGGER_COMPANY_INVITE
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1, 2, 3]), \
             patch(f"{_RS}.get_company_linked_in_url_for_user", return_value="https://x/company/a"), \
             patch(f"{_RS}._stagger_due", side_effect=lambda u, *_: u == 2) as due, \
             patch(f"{_RS}.dispatch_jitter_seconds", return_value=900), \
             patch(f"{_RS}.automate_invites_to_company_page_for_user") as task:
            result = auto_invite_to_company_pages()
        assert task.apply_async.call_count == 1
        assert task.apply_async.call_args[1]["kwargs"]["user_id"] == 2
        assert task.apply_async.call_args[1]["countdown"] == 900
        assert due.call_args[0][1] is STAGGER_COMPANY_INVITE
        assert "1 user(s)" in result

    def test_a_user_without_a_company_page_does_not_burn_their_slot(self):
        from cqc_lem.app.run_scheduler import auto_invite_to_company_pages
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_active_user_ids", return_value=[9]), \
             patch(f"{_RS}.get_company_linked_in_url_for_user", return_value=None), \
             patch(f"{_RS}._stagger_due", return_value=True) as due, \
             patch(f"{_RS}.automate_invites_to_company_page_for_user") as task:
            auto_invite_to_company_pages()
        due.assert_not_called()
        task.apply_async.assert_not_called()

    def test_a_throttled_account_is_not_probed(self):
        from cqc_lem.app.run_scheduler import auto_invite_to_company_pages
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_RS}.get_active_user_ids") as users:
            assert auto_invite_to_company_pages() == "Automation throttled"
        users.assert_not_called()

    def test_the_monthly_crontab_is_gone(self):
        """A day_of_month schedule is exactly the once-a-month blast this issue removed."""
        from cqc_lem.app.my_celery import app
        from cqc_lem.utilities.engagement_window import STAGGER_TICK_MINUTES
        schedule = app.conf.beat_schedule["invite_to_company_pages"]["schedule"]
        assert schedule.day_of_month == set(range(1, 32))
        assert schedule.minute == set(range(0, 60, STAGGER_TICK_MINUTES))
        assert schedule.hour == set(range(24))


class TestCountCompanyPageInvitesToday:
    def _conn(self, row):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = row
        conn.cursor.return_value = cur
        return conn, cur

    def test_it_sums_the_batch_counts_rather_than_counting_rows(self):
        from cqc_lem.utilities.db import count_company_page_invites_sent_today
        conn, cur = self._conn((7,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert count_company_page_invites_sent_today(1) == 7
        sql, params = cur.execute.call_args[0]
        assert "SUM(" in sql and "created_at >= CURDATE()" in sql
        assert params[-1].endswith(":%")

    def test_no_rows_today_is_zero(self):
        from cqc_lem.utilities.db import count_company_page_invites_sent_today
        conn, _ = self._conn((None,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert count_company_page_invites_sent_today(1) == 0


class TestPreferenceColumn:
    def test_the_cap_round_trips_through_the_api_model(self):
        from cqc_lem.api.main import EngagementPreferencesRequest as P

        def prefs(**over):
            return P(session_token="t", **over)

        assert prefs().max_company_page_invites_per_day == 5
        assert prefs(max_company_page_invites_per_day=999).max_company_page_invites_per_day == 50
        assert prefs(max_company_page_invites_per_day=-3).max_company_page_invites_per_day == 0

    def test_the_column_is_written_by_the_one_row_upsert(self):
        from cqc_lem.utilities.db import _ENGAGEMENT_COLS, _ENGAGEMENT_DEFAULTS
        assert "max_company_page_invites_per_day" in _ENGAGEMENT_COLS
        assert _ENGAGEMENT_DEFAULTS["max_company_page_invites_per_day"] == 5
