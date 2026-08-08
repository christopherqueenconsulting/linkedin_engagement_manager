"""Stale-invite withdrawal — the daily beat that used to be a stub (issue #969).

Withdrawing is ONE-WAY (LinkedIn blocks re-inviting the same person for weeks), so the behaviours
worth protecting here are the fail-closed ones: an invite whose age cannot be read is never stale, a
headline that happens to contain a time unit never dates a row, and the daily spend is counted on the
CLICK rather than on the verdict.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_SI = "cqc_lem.utilities.linkedin.stale_invites"


def _row(name: str, text: str, href: str = "/in/someone/", control=None,
         label_name: str = "") -> list:
    """One row as the page-side pass hands it back: href, link text, card text, control, and the
    person the control's own label names (empty on the unlabelled fallback shape)."""
    return [href, name, text, control if control is not None else MagicMock(), label_name]

# A sent list that rendered fine and simply does not contain the row we withdrew. `[]` cannot say
# that — it is also what a JS failure, an auth wall and a mid-load page return — so verification
# reads it as "unproven" (see TestVerification).
_GONE = [{"profile_url": "/in/somebody-else/", "name": "Other", "text": "Sent 1 day ago",
          "age_days": 1.0, "control": None, "label_name": "Other", "entity_ok": True}]


class TestParseSentAge:
    def test_reads_the_sent_stamp_in_days(self):
        from cqc_lem.utilities.linkedin.stale_invites import parse_sent_age_days
        assert parse_sent_age_days("Ann Lee\nCEO\nSent 3 weeks ago\nWithdraw") == 21.0
        assert parse_sent_age_days("Sent 2 months ago") == 60.0
        assert parse_sent_age_days("Sent 5 days ago") == 5.0
        assert parse_sent_age_days("Sent a month ago") == 30.0

    def test_sub_day_stamps_are_zero_not_unreadable(self):
        from cqc_lem.utilities.linkedin.stale_invites import parse_sent_age_days
        assert parse_sent_age_days("Sent 5 hours ago") == 0.0
        assert parse_sent_age_days("Sent 30 minutes ago") == 0.0
        assert parse_sent_age_days("Sent today") == 0.0
        assert parse_sent_age_days("Sent yesterday") == 1.0

    def test_unreadable_is_none_never_zero(self):
        """None means SKIP. Returning 0 would be 'fresh' (also safe) but hides selector drift; the
        run report counts these, which is how the lane going blind becomes visible."""
        from cqc_lem.utilities.linkedin.stale_invites import parse_sent_age_days
        assert parse_sent_age_days("") is None
        assert parse_sent_age_days(None) is None
        assert parse_sent_age_days("Ann Lee\nCEO at Acme\nWithdraw") is None

    def test_a_headline_CONTAINING_sent_never_dates_the_row(self):
        """"Sales Representative", "Sentiment analysis" and "Presenting at ..." all contain "sent".
        Paired with the headline's own "12 years ago", a substring test dates a three-hour-old
        invite at 4380 days — and since stale rows sort oldest-first, that row would be withdrawn
        FIRST, permanently. Only a line that STARTS with "Sent" is the row's stamp."""
        from cqc_lem.utilities.linkedin.stale_invites import parse_sent_age_days
        assert parse_sent_age_days(
            "Jane Doe\nSales Representative | started this journey 12 years ago\n"
            "Sent 3 hours ago\nWithdraw") == 0.0
        assert parse_sent_age_days(
            "X\nNLP & Sentiment analysis | left banking 5 years ago\nSent 2 days ago") == 2.0
        assert parse_sent_age_days(
            "Y\nPresenting at SaaStr | founded 3 years ago\nSent 1 hour ago") == 0.0
        # A headline that contains "sent" and NO stamp line stays unreadable, never "old".
        assert parse_sent_age_days("Z\nSales Representative | 12 years ago\nWithdraw") is None

    def test_a_headline_time_unit_never_dates_the_row(self):
        """The regression this test exists for: the row text is the WHOLE card. A headline reading
        '10 years ago I started...' would otherwise age a two-day-old invite at a decade."""
        from cqc_lem.utilities.linkedin.stale_invites import parse_sent_age_days
        text = "Ann Lee\nI started Acme 10 years ago\nSent 2 days ago\nWithdraw"
        assert parse_sent_age_days(text) == 2.0

    def test_no_sent_line_at_all_is_unreadable_even_with_an_ago(self):
        from cqc_lem.utilities.linkedin.stale_invites import parse_sent_age_days
        assert parse_sent_age_days("Ann Lee\nI started Acme 10 years ago\nWithdraw") is None

    def test_ago_is_required(self):
        from cqc_lem.utilities.linkedin.stale_invites import parse_sent_age_days
        assert parse_sent_age_days("Sent invitation · 10 years of experience") is None


class TestPlan:
    """The allowance is decided BEFORE a browser session opens — most runs must cost no Chrome slot."""

    def test_lane_is_off_by_default(self, monkeypatch):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_DISABLED, plan_withdrawals
        monkeypatch.delenv("STALE_INVITE_WITHDRAWAL_ENABLED", raising=False)
        plan = plan_withdrawals(1, prefs={})
        assert plan["allowance"] == 0
        assert plan["status"] == WITHDRAW_STATUS_DISABLED

    def test_zero_threshold_disables_rather_than_withdrawing_everything(self, monkeypatch):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_DISABLED, plan_withdrawals
        monkeypatch.setenv("STALE_INVITE_WITHDRAWAL_ENABLED", "true")
        monkeypatch.setenv("STALE_INVITE_AGE_DAYS", "0")
        assert plan_withdrawals(1, prefs={})["status"] == WITHDRAW_STATUS_DISABLED

    def test_enabled_lane_draws_its_own_pacing_key(self, monkeypatch):
        from cqc_lem.utilities.human_pacing import ACTION_WITHDRAW_INVITE
        monkeypatch.setenv("STALE_INVITE_WITHDRAWAL_ENABLED", "true")
        monkeypatch.setenv("STALE_INVITE_WITHDRAWALS_PER_DAY", "8")
        with patch(f"{_SI}.count_invite_withdrawals_today", return_value=2), \
                patch(f"{_SI}.remaining_actions", return_value=6) as remaining:
            from cqc_lem.utilities.linkedin.stale_invites import plan_withdrawals
            plan = plan_withdrawals(7, prefs={"max_comments_per_day": 5})
        assert plan["allowance"] == 6
        assert plan["cap"] == 8
        assert plan["withdrawn_today"] == 2
        args = remaining.call_args[0]
        assert args[1] == ACTION_WITHDRAW_INVITE  # never ACTION_INVITE — it would clobber that draw
        assert args[3] == 2                       # today's spend read from the DB, not Redis

    def test_budget_already_spent_reports_why(self, monkeypatch):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_BUDGET_REACHED, plan_withdrawals
        monkeypatch.setenv("STALE_INVITE_WITHDRAWAL_ENABLED", "true")
        with patch(f"{_SI}.count_invite_withdrawals_today", return_value=10), \
                patch(f"{_SI}.remaining_actions", return_value=0):
            assert plan_withdrawals(1, prefs={})["status"] == WITHDRAW_STATUS_BUDGET_REACHED

    def test_a_bad_env_value_falls_back_to_the_default(self, monkeypatch):
        from cqc_lem.utilities.linkedin.stale_invites import STALE_INVITE_AGE_DAYS_DEFAULT, stale_after_days
        monkeypatch.setenv("STALE_INVITE_AGE_DAYS", "three weeks")
        assert stale_after_days() == STALE_INVITE_AGE_DAYS_DEFAULT


def _plan(allowance=3, threshold=21):
    return {"allowance": allowance, "status": "withdrew", "cap": 10, "withdrawn_today": 0,
            "threshold_days": threshold}


class TestWithdrawWalk:
    def _driver(self, rows_by_call):
        driver = MagicMock()
        driver.execute_script.side_effect = lambda script, *a: (
            rows_by_call.pop(0) if "querySelectorAll" in script and "isWithdraw" in script else None)
        return driver

    def test_only_rows_past_the_threshold_are_clicked(self):
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        fresh = MagicMock()
        old = MagicMock()
        invites = [{"profile_url": "/in/fresh/", "name": "Fresh", "text": "Sent 2 days ago",
                    "age_days": 2.0, "control": fresh},
                   {"profile_url": "/in/old/", "name": "Old", "text": "Sent 8 weeks ago",
                    "age_days": 56.0, "control": old}]
        driver = MagicMock()
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", side_effect=[invites, _GONE, _GONE]), \
                patch(f"{_SI}._confirm_withdrawal", return_value=True), \
                patch(f"{_SI}._record_withdrawal") as record:
            report = withdraw_stale_invites(driver, MagicMock(), 1, plan=_plan(),
                                            sleep=lambda *_: None)
        assert report["withdrawn"] == 1
        assert report["stale_seen"] == 1
        clicked = [c.args[1] for c in driver.execute_script.call_args_list if len(c.args) > 1]
        assert old in clicked and fresh not in clicked
        assert record.call_args[0][1]["name"] == "Old"

    def test_undated_rows_are_never_withdrawn(self):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_NONE_STALE, withdraw_stale_invites
        invites = [{"profile_url": "/in/x/", "name": "X", "text": "no stamp", "age_days": None,
                    "control": MagicMock()}]
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=invites), \
                patch(f"{_SI}._record_withdrawal") as record, \
                patch(f"{_SI}.log_warning") as warn:
            report = withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(),
                                            sleep=lambda *_: None)
        assert report["status"] == WITHDRAW_STATUS_NONE_STALE
        assert report["unreadable"] == 1
        record.assert_not_called()
        # Every row undated is drift, not an account with undated invites — one warning, run-level.
        assert warn.call_count == 1

    def test_oldest_first_when_the_budget_is_smaller_than_the_backlog(self):
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        invites = [{"profile_url": f"/in/{age}/", "name": str(age), "text": "", "age_days": age,
                    "control": MagicMock()} for age in (30.0, 90.0, 45.0)]
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", side_effect=[invites] + [_GONE] * 10), \
                patch(f"{_SI}._confirm_withdrawal", return_value=True), \
                patch(f"{_SI}._record_withdrawal") as record:
            withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(allowance=1),
                                   sleep=lambda *_: None)
        assert [c.args[1]["name"] for c in record.call_args_list] == ["90.0"]

    def test_the_second_withdrawal_clicks_the_re_read_control_not_the_captured_one(self):
        """A withdrawal RE-RENDERS the list, so every handle read before it is suspect. Re-using one
        is not merely a stale-element skip: the framework may have re-used that node for the row that
        shifted up, and clicking it withdraws a DIFFERENT invite — one-way, for weeks."""
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        first, captured_second, rendered_second = MagicMock(), MagicMock(), MagicMock()
        invites = [{"profile_url": "/in/a/", "name": "A", "text": "", "age_days": 90.0,
                    "control": first},
                   {"profile_url": "/in/b/", "name": "B", "text": "", "age_days": 60.0,
                    "control": captured_second}]
        reads = [invites,                                                  # the initial walk
                 [{**invites[1], "control": rendered_second}],             # A verified gone
                 [{**invites[1], "control": rendered_second}],             # B, re-read
                 _GONE]                                                    # B verified gone
        driver = MagicMock()
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", side_effect=reads), \
                patch(f"{_SI}._confirm_withdrawal", return_value=True), \
                patch(f"{_SI}._record_withdrawal"):
            report = withdraw_stale_invites(driver, MagicMock(), 1, plan=_plan(allowance=2),
                                            sleep=lambda *_: None)
        clicked = [c.args[1] for c in driver.execute_script.call_args_list if len(c.args) > 1]
        assert report["withdrawn"] == 2
        assert first in clicked and rendered_second in clicked
        assert captured_second not in clicked

    def test_a_row_that_no_longer_resolves_is_skipped_rather_than_clicked_stale(self):
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        first, captured_second = MagicMock(), MagicMock()
        invites = [{"profile_url": "/in/a/", "name": "A", "text": "", "age_days": 90.0,
                    "control": first},
                   {"profile_url": "/in/b/", "name": "B", "text": "", "age_days": 60.0,
                    "control": captured_second}]
        driver = MagicMock()
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", side_effect=[invites, _GONE, _GONE, _GONE]), \
                patch(f"{_SI}._confirm_withdrawal", return_value=True), \
                patch(f"{_SI}._record_withdrawal") as record:
            report = withdraw_stale_invites(driver, MagicMock(), 1, plan=_plan(allowance=2),
                                            sleep=lambda *_: None)
        clicked = [c.args[1] for c in driver.execute_script.call_args_list if len(c.args) > 1]
        assert captured_second not in clicked
        assert report["withdrawn"] == 1 and record.call_count == 1

    def test_an_unidentifiable_row_is_never_re_resolved_by_position(self):
        """No profile link means no way to prove which row this is on the re-rendered page. The FIRST
        click may still go out (that handle predates any re-render); a later one may not."""
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        first, anonymous = MagicMock(), MagicMock()
        invites = [{"profile_url": "/in/a/", "name": "A", "text": "", "age_days": 90.0,
                    "control": first},
                   {"profile_url": "", "name": "", "text": "", "age_days": 60.0,
                    "control": anonymous}]
        driver = MagicMock()
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", side_effect=[invites, _GONE, _GONE]), \
                patch(f"{_SI}._confirm_withdrawal", return_value=True), \
                patch(f"{_SI}._record_withdrawal"):
            withdraw_stale_invites(driver, MagicMock(), 1, plan=_plan(allowance=2),
                                   sleep=lambda *_: None)
        clicked = [c.args[1] for c in driver.execute_script.call_args_list if len(c.args) > 1]
        assert anonymous not in clicked

    def test_an_unverified_withdrawal_is_still_recorded_and_never_counted_as_success(self):
        """The click already reached LinkedIn. A lane whose verification broke must not be free to
        click every row on the page, so the spend is recorded either way — but it is not a success."""
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_FAILED, withdraw_stale_invites
        invites = [{"profile_url": "/in/old/", "name": "Old", "text": "", "age_days": 60.0,
                    "control": MagicMock()}]
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=invites), \
                patch(f"{_SI}._confirm_withdrawal", return_value=True), \
                patch(f"{_SI}._record_withdrawal") as record, \
                patch(f"{_SI}.log_warning") as warn:
            report = withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(allowance=1),
                                            sleep=lambda *_: None)
        assert report["withdrawn"] == 0 and report["unverified"] == 1
        assert report["status"] == WITHDRAW_STATUS_FAILED
        record.assert_called_once()
        assert record.call_args[0][2] is False
        assert warn.called

    def test_the_429_breaker_stands_the_run_down_before_any_navigation(self):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_PAUSED, withdraw_stale_invites
        driver = MagicMock()
        with patch(f"{_SI}._hold_reason", return_value="LinkedIn 429 breaker open"):
            report = withdraw_stale_invites(driver, MagicMock(), 1, plan=_plan(),
                                            sleep=lambda *_: None)
        assert report["status"] == WITHDRAW_STATUS_PAUSED
        driver.get.assert_not_called()

    def test_a_breaker_that_trips_mid_walk_stops_the_rest(self):
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        invites = [{"profile_url": f"/in/{n}/", "name": str(n), "text": "", "age_days": 60.0,
                    "control": MagicMock()} for n in range(3)]
        holds = ["", "", "paused"]
        with patch(f"{_SI}._hold_reason", side_effect=lambda *_: holds.pop(0)), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", side_effect=[invites] + [_GONE] * 20), \
                patch(f"{_SI}._confirm_withdrawal", return_value=True), \
                patch(f"{_SI}._record_withdrawal") as record:
            report = withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(allowance=3),
                                            sleep=lambda *_: None)
        assert record.call_count == 1
        assert report["withdrawn"] == 1

    def test_zero_budget_never_opens_the_page(self):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_DISABLED, withdraw_stale_invites
        driver = MagicMock()
        report = withdraw_stale_invites(driver, MagicMock(), 1,
                                        plan={"allowance": 0, "status": WITHDRAW_STATUS_DISABLED,
                                              "cap": 0, "withdrawn_today": 0, "threshold_days": 21},
                                        sleep=lambda *_: None)
        assert report["status"] == WITHDRAW_STATUS_DISABLED
        driver.get.assert_not_called()

    def test_no_rows_is_a_status_not_a_warning(self):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_NO_ROWS, withdraw_stale_invites
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=[]), \
                patch(f"{_SI}.log_warning") as warn:
            report = withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(),
                                            sleep=lambda *_: None)
        assert report["status"] == WITHDRAW_STATUS_NO_ROWS
        assert report["rows_seen"] == 0
        warn.assert_not_called()


class TestHardGates:
    """Pacing only slows the lane down; these are the gates that stop it. They are re-read per
    withdrawal because a breaker can trip mid-walk."""

    def test_a_manual_or_suppression_pause_names_itself(self):
        from cqc_lem.utilities.linkedin.stale_invites import _hold_reason
        with patch(f"{_SI}.is_automation_paused", return_value=True), \
                patch(f"{_SI}.automation_pause_reason", return_value="suppression tripwire"):
            assert _hold_reason(1) == "suppression tripwire"

    def test_a_pause_with_no_stored_reason_still_holds(self):
        from cqc_lem.utilities.linkedin.stale_invites import _hold_reason
        with patch(f"{_SI}.is_automation_paused", return_value=True), \
                patch(f"{_SI}.automation_pause_reason", return_value=None):
            assert _hold_reason(1) == "automation paused"

    def test_the_429_breaker_holds_on_its_own(self):
        from cqc_lem.utilities.linkedin.stale_invites import _hold_reason
        with patch(f"{_SI}.is_automation_paused", return_value=False), \
                patch(f"{_SI}.rate_limit_cooldown_remaining", return_value=90):
            assert "429" in _hold_reason(1)

    def test_every_gate_clear_is_an_empty_string(self):
        from cqc_lem.utilities.linkedin.stale_invites import _hold_reason
        with patch(f"{_SI}.is_automation_paused", return_value=False), \
                patch(f"{_SI}.rate_limit_cooldown_remaining", return_value=0):
            assert _hold_reason(1) == ""

    def test_a_zero_daily_cap_disables_the_lane(self, monkeypatch):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_DISABLED, plan_withdrawals
        monkeypatch.setenv("STALE_INVITE_WITHDRAWAL_ENABLED", "true")
        monkeypatch.setenv("STALE_INVITE_WITHDRAWALS_PER_DAY", "0")
        # No DB read either — the cap is decided before anything is looked up.
        with patch(f"{_SI}.count_invite_withdrawals_today") as count:
            assert plan_withdrawals(1, prefs={})["status"] == WITHDRAW_STATUS_DISABLED
        count.assert_not_called()


class TestListExpansion:
    """The sent list renders newest-first, so the invites this lane exists for are at the BOTTOM. A
    run that never expands can only see rows it must not touch — which looks exactly like 'nothing is
    stale'.

    The page has NO pager (grounded 2026-08-07, #1006): every control matching "show more" is a
    "… show more" headline expander INSIDE a row. Rows arrive on SCROLL, and the ROW COUNT is the
    only honest stop condition."""

    def _driver(self, counts):
        """A page whose row count follows `counts` as the list is scrolled."""
        driver = MagicMock()
        seq = list(counts)

        def script(source, *args):
            if "scrollIntoView" in source:
                return seq.pop(0) if seq else (counts[-1] if counts else 0)
            return None

        driver.execute_script.side_effect = script
        return driver

    def test_it_scrolls_until_the_list_stops_growing(self):
        from cqc_lem.utilities.linkedin.stale_invites import _load_more_rows
        # 10 → 20 → 30 → 30 → 30: two quiet rounds end it.
        driver = self._driver([10, 20, 30, 30, 30])
        assert _load_more_rows(driver, sleep=lambda *_: None) == 5

    def test_one_quiet_round_is_not_enough_to_stop(self):
        """An infinite-scroll list often needs a beat to fetch; stopping on the first unchanged
        count would leave the oldest invites — the only ones this lane wants — unloaded."""
        from cqc_lem.utilities.linkedin.stale_invites import _load_more_rows
        driver = self._driver([10, 10, 20, 20, 20])
        assert _load_more_rows(driver, sleep=lambda *_: None) == 5

    def test_it_never_clicks_anything(self):
        """The only "show more" controls on the page expand profile headlines inside the rows.
        Clicking them mutates the cards this lane is about to read and loads no invites."""
        from cqc_lem.utilities.linkedin.stale_invites import _load_more_rows
        driver = self._driver([5, 5, 5])
        _load_more_rows(driver, sleep=lambda *_: None)
        sources = [c.args[0] for c in driver.execute_script.call_args_list]
        assert not any("arguments[0].click()" in s for s in sources)

    def test_the_walk_is_bounded(self):
        from cqc_lem.utilities.linkedin.stale_invites import _load_more_rows, show_more_max_clicks
        cap = show_more_max_clicks()
        driver = self._driver(list(range(1, cap + 10)))  # always growing: only the cap stops it
        assert _load_more_rows(driver, sleep=lambda *_: None) == cap

    def test_the_bound_is_tunable_without_a_deploy(self, monkeypatch):
        """The first grounding run hit the old cap of 5 exactly — the one reading that cannot be
        told apart from the list running out. Scrolling is read-only, so the bound is config rather
        than code, and a run can say which of the two it hit."""
        from cqc_lem.utilities.linkedin.stale_invites import (
            SHOW_MORE_MAX_CLICKS_DEFAULT,
            _load_more_rows,
            show_more_max_clicks,
        )
        assert SHOW_MORE_MAX_CLICKS_DEFAULT >= 20
        monkeypatch.setenv("STALE_INVITE_SHOW_MORE_MAX_CLICKS", "3")
        assert show_more_max_clicks() == 3
        driver = self._driver(list(range(1, 20)))
        assert _load_more_rows(driver, sleep=lambda *_: None) == 3

    def test_a_js_failure_keeps_what_it_already_expanded(self):
        from cqc_lem.utilities.linkedin.stale_invites import _load_more_rows
        driver = MagicMock()
        calls = {"n": 0}

        def script(source, *args):
            calls["n"] += 1
            if calls["n"] > 3:
                raise RuntimeError("detached frame")
            return calls["n"] * 10

        driver.execute_script.side_effect = script
        assert _load_more_rows(driver, sleep=lambda *_: None) == 3

    def test_a_non_numeric_count_is_not_mistaken_for_growth(self):
        """A page-side pass that returns nothing must read as zero rows — not raise, and not scroll
        to the cap. Three rounds: one to take the baseline, two quiet ones to end it."""
        from cqc_lem.utilities.linkedin.stale_invites import _load_more_rows
        driver = MagicMock()
        driver.execute_script.side_effect = lambda source, *a: None
        assert _load_more_rows(driver, sleep=lambda *_: None) == 3


class TestConfirmationDialog:
    def test_it_clicks_the_dialogs_own_withdraw_button(self):
        from cqc_lem.utilities.linkedin.stale_invites import _confirm_withdrawal
        button = MagicMock()
        driver = MagicMock()
        driver.execute_script.side_effect = lambda source, *a: (button if "role='dialog'" in source
                                                                else None)
        assert _confirm_withdrawal(driver, lambda *_: None) is True
        clicked = [c.args[1] for c in driver.execute_script.call_args_list if len(c.args) > 1]
        assert button in clicked

    def test_no_dialog_is_not_a_failed_withdrawal(self):
        """LinkedIn has shipped both a confirmed and an unconfirmed withdrawal — False here only
        means no dialog resolved, so the caller still has to re-read the list."""
        from cqc_lem.utilities.linkedin.stale_invites import _confirm_withdrawal
        driver = MagicMock()
        driver.execute_script.return_value = None
        assert _confirm_withdrawal(driver, lambda *_: None) is False

    def test_a_lookup_that_raises_is_retried_not_fatal(self):
        from cqc_lem.utilities.linkedin.stale_invites import _confirm_withdrawal
        driver = MagicMock()
        button = MagicMock()
        answers = [RuntimeError("boom"), button, None]

        def script(source, *args):
            if "role='dialog'" in source:
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return None

        driver.execute_script.side_effect = script
        assert _confirm_withdrawal(driver, lambda *_: None) is True

    def test_a_confirm_click_that_fails_is_not_reported_as_confirmed(self):
        from cqc_lem.utilities.linkedin.stale_invites import _confirm_withdrawal
        driver = MagicMock()

        def script(source, *args):
            if "role='dialog'" in source:
                return MagicMock()
            raise RuntimeError("stale element")

        driver.execute_script.side_effect = script
        assert _confirm_withdrawal(driver, lambda *_: None) is False


class TestVerification:
    def test_a_row_with_no_profile_link_is_unverifiable_and_stays_pending(self):
        """Fail closed: unverifiable is reported as 'still pending', never as withdrawn."""
        from cqc_lem.utilities.linkedin.stale_invites import _invite_still_pending
        assert _invite_still_pending(MagicMock(), "", lambda *_: None) is True

    def test_a_row_that_is_gone_verifies(self):
        """Gone means the list RENDERED and this row was not in it. An empty read is a different
        fact and is covered by test_an_unreadable_page_is_never_proof_the_invite_is_gone."""
        from cqc_lem.utilities.linkedin.stale_invites import _invite_still_pending
        with patch(f"{_SI}.read_pending_invites", return_value=_GONE):
            assert _invite_still_pending(MagicMock(), "/in/ann/", lambda *_: None) is False

    def test_a_row_that_is_still_listed_after_every_re_read_does_not(self):
        from cqc_lem.utilities.linkedin.stale_invites import _invite_still_pending
        with patch(f"{_SI}.read_pending_invites",
                   return_value=[{"profile_url": "/in/ann/"}]):
            assert _invite_still_pending(MagicMock(), "/in/ann/", lambda *_: None) is True


class TestWalkEdgeCases:
    def test_pending_but_none_old_enough_is_none_stale_without_a_warning(self):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_NONE_STALE, withdraw_stale_invites
        invites = [{"profile_url": "/in/a/", "name": "A", "text": "Sent 2 days ago",
                    "age_days": 2.0, "control": MagicMock()}]
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=invites), \
                patch(f"{_SI}._record_withdrawal") as record, \
                patch(f"{_SI}.log_warning") as warn:
            report = withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(),
                                            sleep=lambda *_: None)
        assert report["status"] == WITHDRAW_STATUS_NONE_STALE
        assert report["stale_seen"] == 0 and report["rows_seen"] == 1
        record.assert_not_called()
        warn.assert_not_called()

    def test_a_stale_row_with_no_control_is_skipped_not_clicked(self):
        """A row we cannot resolve a Withdraw control on is left alone — the next run sees it again."""
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_FAILED, withdraw_stale_invites
        invites = [{"profile_url": "/in/old/", "name": "Old", "text": "", "age_days": 60.0,
                    "control": None}]
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=invites), \
                patch(f"{_SI}._record_withdrawal") as record:
            report = withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(allowance=1),
                                            sleep=lambda *_: None)
        record.assert_not_called()
        # Stale rows that could not be clicked is one of the two shapes selector rot takes here.
        assert report["status"] == WITHDRAW_STATUS_FAILED
        assert report["withdrawn"] == 0

    def test_a_click_that_raises_is_a_skip_and_never_spends_the_budget(self):
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        driver = MagicMock()
        driver.execute_script.side_effect = RuntimeError("element is not attached")
        invites = [{"profile_url": "/in/old/", "name": "Old", "text": "", "age_days": 60.0,
                    "control": MagicMock()}]
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=invites), \
                patch(f"{_SI}._record_withdrawal") as record:
            report = withdraw_stale_invites(driver, MagicMock(), 1, plan=_plan(allowance=1),
                                            sleep=lambda *_: None)
        record.assert_not_called()
        assert report["withdrawn"] == 0 and report["unverified"] == 0

    def test_a_breaker_that_trips_before_the_first_row_reports_paused_not_failed(self):
        """Nothing was clicked, so the run is held — calling it `failed` would send someone hunting
        for selector rot that is not there."""
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_PAUSED, withdraw_stale_invites
        invites = [{"profile_url": "/in/old/", "name": "Old", "text": "", "age_days": 60.0,
                    "control": MagicMock()}]
        holds = ["", "LinkedIn 429 breaker open"]
        with patch(f"{_SI}._hold_reason", side_effect=lambda *_: holds.pop(0)), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=invites), \
                patch(f"{_SI}._record_withdrawal") as record:
            report = withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(allowance=1),
                                            sleep=lambda *_: None)
        record.assert_not_called()
        assert report["status"] == WITHDRAW_STATUS_PAUSED


class TestRowResolution:
    def test_rows_are_parsed_into_dated_invites(self):
        from cqc_lem.utilities.linkedin.stale_invites import read_pending_invites
        control = MagicMock()
        driver = MagicMock()
        driver.execute_script.return_value = [
            _row("Ann Lee", "Ann Lee\nCEO\nSent 4 weeks ago\nWithdraw", "/in/ann/", control)]
        invites = read_pending_invites(driver)
        assert invites[0]["name"] == "Ann Lee"
        assert invites[0]["age_days"] == 28.0
        assert invites[0]["control"] is control

    def test_a_js_failure_degrades_to_no_rows(self):
        from cqc_lem.utilities.linkedin.stale_invites import read_pending_invites
        driver = MagicMock()
        driver.execute_script.side_effect = RuntimeError("boom")
        assert read_pending_invites(driver) == []

    def test_a_malformed_row_is_dropped_rather_than_half_read(self):
        """The JS hands back fixed 4- or 5-tuples; anything else means the page-side pass changed
        shape, and a half-read row must not become a withdrawal target."""
        from cqc_lem.utilities.linkedin.stale_invites import read_pending_invites
        driver = MagicMock()
        driver.execute_script.return_value = [
            ["/in/short/", "Short"], "not a row",
            _row("Ann Lee", "Sent 4 weeks ago", "/in/ann/")]
        invites = read_pending_invites(driver)
        assert [i["name"] for i in invites] == ["Ann Lee"]

    def test_the_unlabelled_fallback_row_still_reads(self):
        """The pre-#1006 shape is a 4-tuple with no label. It carries no name to contradict, so it
        stays actionable — the fallback must not become a silent no-op."""
        from cqc_lem.utilities.linkedin.stale_invites import read_pending_invites
        driver = MagicMock()
        driver.execute_script.return_value = [
            ["/in/ann/", "Ann Lee", "Ann Lee\nSent 4 weeks ago\nWithdraw", MagicMock()]]
        invites = read_pending_invites(driver)
        assert invites[0]["entity_ok"] is True
        assert invites[0]["label_name"] == ""


class TestControlNamesRow:
    """#1006: the live control's label is "Withdraw invitation sent to <Name>" — the one surface
    that says WHO a click acts on. #1012 cost ~20 invites by trusting a control that named someone
    else, and a withdrawal cannot be taken back."""

    def test_a_control_naming_this_rows_person_is_actionable(self):
        from cqc_lem.utilities.linkedin.stale_invites import control_names_row
        assert control_names_row(
            "Christa Brutus, PMP, MM, LSSGB, ASCP(C)",
            "Christa Brutus, PMP, MM, LSSGB, ASCP(C)\nAI Consultant\nSent 1 hour ago\nWithdraw")

    def test_a_control_naming_someone_else_is_refused(self):
        from cqc_lem.utilities.linkedin.stale_invites import control_names_row
        assert not control_names_row("Ann Lee", "Bob Stone\nCEO\nSent 9 weeks ago\nWithdraw")

    def test_a_name_merely_MENTIONED_in_the_card_does_not_vouch_for_the_control(self):
        """A mutual-connection blurb naming Ann Lee must not authorise a control that names Ann Lee
        while sitting beside Bob Stone's row — that IS the drift this check exists to catch."""
        from cqc_lem.utilities.linkedin.stale_invites import control_names_row
        assert not control_names_row(
            "Ann Lee", "Bob Stone\nCEO\nAnn Lee and 3 others are connections\nSent 9 weeks ago")

    def test_no_label_carries_no_contradiction(self):
        from cqc_lem.utilities.linkedin.stale_invites import control_names_row
        assert control_names_row("", "Bob Stone\nSent 9 weeks ago\nWithdraw")

    def test_whitespace_and_case_do_not_decide_a_one_way_action(self):
        from cqc_lem.utilities.linkedin.stale_invites import control_names_row
        assert control_names_row("ann   lee", "Ann Lee\nCEO\nSent 4 weeks ago")

    def test_a_mismatched_row_is_never_withdrawn(self):
        from cqc_lem.utilities.linkedin.stale_invites import WITHDRAW_STATUS_NONE_STALE, withdraw_stale_invites
        control = MagicMock()
        invites = [{"profile_url": "/in/ann/", "name": "Ann Lee", "text": "Sent 9 weeks ago",
                    "age_days": 63.0, "control": control, "label_name": "Bob Stone",
                    "entity_ok": False}]
        driver = MagicMock()
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=invites), \
                patch(f"{_SI}._confirm_withdrawal", return_value=True), \
                patch(f"{_SI}._record_withdrawal") as record:
            report = withdraw_stale_invites(driver, MagicMock(), 1, plan=_plan(),
                                            sleep=lambda *_: None)
        assert report["status"] == WITHDRAW_STATUS_NONE_STALE
        assert report["withdrawn"] == 0
        assert report["entity_mismatch"] == 1
        record.assert_not_called()
        assert control not in [c.args[1] for c in driver.execute_script.call_args_list
                               if len(c.args) > 1]

    def test_a_mismatch_never_blocks_the_rows_that_do_match(self):
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        good, bad = MagicMock(), MagicMock()
        invites = [{"profile_url": "/in/bad/", "name": "Bad", "text": "Sent 9 weeks ago",
                    "age_days": 63.0, "control": bad, "label_name": "Someone Else",
                    "entity_ok": False},
                   {"profile_url": "/in/good/", "name": "Good", "text": "Sent 8 weeks ago",
                    "age_days": 56.0, "control": good, "label_name": "Good",
                    "entity_ok": True}]
        driver = MagicMock()
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", side_effect=[invites, _GONE, _GONE]), \
                patch(f"{_SI}._confirm_withdrawal", return_value=True), \
                patch(f"{_SI}._record_withdrawal"):
            report = withdraw_stale_invites(driver, MagicMock(), 1, plan=_plan(),
                                            sleep=lambda *_: None)
        clicked = [c.args[1] for c in driver.execute_script.call_args_list if len(c.args) > 1]
        assert report["withdrawn"] == 1
        assert good in clicked and bad not in clicked

    def test_a_re_resolved_control_that_names_someone_else_is_dropped(self):
        """The re-render is exactly when a control can end up beside the wrong person's link."""
        from cqc_lem.utilities.linkedin.stale_invites import _resolve_control
        driver = MagicMock()
        rows = [{"profile_url": "/in/ann/", "control": MagicMock(), "entity_ok": False}]
        with patch(f"{_SI}.read_pending_invites", return_value=rows):
            assert _resolve_control(driver, {"profile_url": "/in/ann/"}) is None

    def test_an_unreadable_page_is_never_proof_the_invite_is_gone(self):
        """read_pending_invites answers [] for a JS failure, a navigation, an auth wall and a page
        still loading. Reading that as absence records a SUCCESS for a withdrawal that may never
        have happened — and silences the only signal that the confirm step is broken."""
        from cqc_lem.utilities.linkedin.stale_invites import _invite_still_pending
        with patch(f"{_SI}.read_pending_invites", return_value=[]):
            assert _invite_still_pending(MagicMock(), "/in/ann/", lambda *_: None) is True

    def test_absence_from_a_list_that_DID_render_is_proof(self):
        from cqc_lem.utilities.linkedin.stale_invites import _invite_still_pending
        with patch(f"{_SI}.read_pending_invites", return_value=[{"profile_url": "/in/bob/"}]):
            assert _invite_still_pending(MagicMock(), "/in/ann/", lambda *_: None) is False

    def test_rows_old_enough_but_refused_are_never_reported_as_nothing_stale(self):
        """The refused rows ARE past the threshold. Dropping them silently makes the run say "no
        invites older than N days", which is false and reads exactly like a healthy account."""
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        invites = [{"profile_url": "/in/a/", "name": "A", "text": "Sent 9 weeks ago",
                    "age_days": 63.0, "control": MagicMock(), "label_name": "Someone Else",
                    "entity_ok": False},
                   {"profile_url": "/in/b/", "name": "B", "text": "Sent 2 days ago",
                    "age_days": 2.0, "control": MagicMock(), "label_name": "B", "entity_ok": True}]
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=invites), \
                patch(f"{_SI}.log_warning") as warn:
            report = withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(),
                                            sleep=lambda *_: None)
        assert report["withdrawn"] == 0
        assert report["entity_mismatch"] == 1
        assert warn.call_count == 1
        assert "refused" in warn.call_args[0][0]

    def test_every_row_mismatching_warns_once_at_the_run_level(self):
        """Total mismatch is the label wording having moved, not 44 mislabelled rows — one warning,
        never one per row, and nothing is withdrawn."""
        from cqc_lem.utilities.linkedin.stale_invites import withdraw_stale_invites
        invites = [{"profile_url": f"/in/p{i}/", "name": f"P{i}", "text": "Sent 9 weeks ago",
                    "age_days": 63.0, "control": MagicMock(), "label_name": "Nobody Here",
                    "entity_ok": False} for i in range(3)]
        with patch(f"{_SI}._hold_reason", return_value=""), \
                patch(f"{_SI}._load_more_rows", return_value=0), \
                patch(f"{_SI}.read_pending_invites", return_value=invites), \
                patch(f"{_SI}.log_warning") as warn:
            report = withdraw_stale_invites(MagicMock(), MagicMock(), 1, plan=_plan(),
                                            sleep=lambda *_: None)
        assert report["withdrawn"] == 0
        assert warn.call_count == 1


class TestDailySpendLedger:
    """The day's spend is read back out of the immutable logs, not Redis — that is what makes a
    second run the same day, or a worker restart, unable to re-spend the allowance."""

    def _conn(self, row):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = row
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_it_counts_todays_withdrawal_rows_for_this_user(self):
        from cqc_lem.utilities.db import STALE_INVITE_WITHDRAWN_MESSAGE, LogActionType, count_invite_withdrawals_today
        conn, cursor = self._conn((4,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert count_invite_withdrawals_today(1) == 4
        sql, params = cursor.execute.call_args[0]
        assert "FROM logs" in sql and "created_at >= CURDATE()" in sql
        # No result filter: the row is written on DISPATCH, and an unverified withdrawal still cost
        # LinkedIn an action, so it still spends the day's budget.
        assert "result" not in sql
        assert params == (1, LogActionType.ENGAGED.value, STALE_INVITE_WITHDRAWN_MESSAGE)

    def test_no_rows_today_is_zero(self):
        from cqc_lem.utilities.db import count_invite_withdrawals_today
        conn, _ = self._conn((None,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert count_invite_withdrawals_today(1) == 0

    def test_a_db_failure_reads_zero_rather_than_losing_the_run(self):
        """Fail-open on the COUNT is safe: pacing and the shared envelope still bound the walk, and
        the alternative is a beat that dies on a transient DB blip."""
        from cqc_lem.utilities.db import count_invite_withdrawals_today
        conn = MagicMock()
        cursor = MagicMock()
        cursor.execute.side_effect = ValueError("bad column")
        conn.cursor.return_value = cursor
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert count_invite_withdrawals_today(1) == 0
        conn.close.assert_called_once()


class TestRecording:
    def test_a_verified_withdrawal_logs_success_and_records_its_own_pacing_action(self):
        from cqc_lem.utilities.db import STALE_INVITE_WITHDRAWN_MESSAGE, LogResultType
        from cqc_lem.utilities.human_pacing import ACTION_WITHDRAW_INVITE
        with patch(f"{_SI}.insert_new_log") as log, patch(f"{_SI}.record_action") as record:
            from cqc_lem.utilities.linkedin.stale_invites import _record_withdrawal
            _record_withdrawal(3, {"profile_url": "/in/ann/", "name": "Ann"}, True)
        assert log.call_args.kwargs["message"] == STALE_INVITE_WITHDRAWN_MESSAGE
        assert log.call_args.kwargs["result"] == LogResultType.SUCCESS
        record.assert_called_once_with(3, ACTION_WITHDRAW_INVITE)

    def test_an_unverified_withdrawal_logs_failure_but_still_spends(self):
        from cqc_lem.utilities.db import LogResultType
        with patch(f"{_SI}.insert_new_log") as log, patch(f"{_SI}.record_action") as record:
            from cqc_lem.utilities.linkedin.stale_invites import _record_withdrawal
            _record_withdrawal(3, {"profile_url": "/in/ann/", "name": "Ann"}, False)
        assert log.call_args.kwargs["result"] == LogResultType.FAILURE
        record.assert_called_once()
