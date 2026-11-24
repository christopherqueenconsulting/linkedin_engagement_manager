"""Unit tests for the DM follow-up sequencer (enqueue, process, dispatch)."""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.linkedin.message_thread import ThreadState

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"  # lgtm[py/unused-global-variable]


@pytest.fixture(autouse=True)
def _followups_auto_approved():
    """The direct-send tests here predate `followup_step_mode` (#2664); the review path has its own class."""
    with patch(f"{_OUT}.may_auto_approve", return_value=True):
        yield


class TestEnqueueNextFollowup:
    def test_enqueues_when_next_template_exists(self):
        from cqc_lem.app.engagement.outreach import enqueue_next_followup
        with patch(f"{_OUT}.get_dm_template", return_value={"template_text": "hi", "delay_hours": 48, "step": 1}), \
             patch(f"{_OUT}.enqueue_followup") as enq:
            enqueue_next_followup(1, "p", "Jane", "connection_accepted", 0)
        enq.assert_called_once()
        assert enq.call_args[0][4] == 1  # next_step

    def test_queues_a_reply_check_when_no_next_template(self, monkeypatch):
        # Issue #623: the stock templates are step-0 only, so this branch used to end every thread
        # the moment the first DM went out — dm_followups stayed empty and the nurture flywheel
        # never turned. Now it schedules a reply check at the same (template-less) step.
        from cqc_lem.app.engagement.outreach import enqueue_next_followup
        monkeypatch.delenv("DM_NURTURE_ENABLED", raising=False)
        with patch(f"{_OUT}.get_dm_template", return_value=None), \
             patch(f"{_OUT}.enqueue_followup") as enq:
            enqueue_next_followup(1, "p", "Jane", "connection_accepted", 0)
        enq.assert_called_once()
        assert enq.call_args[0][3] == "connection_accepted"  # NOT the nurture sequence
        assert enq.call_args[0][4] == 1

    def test_no_reply_check_when_nurture_is_disabled(self, monkeypatch):
        from cqc_lem.app.engagement.outreach import enqueue_next_followup
        monkeypatch.setenv("DM_NURTURE_ENABLED", "false")
        with patch(f"{_OUT}.get_dm_template", return_value=None), \
             patch(f"{_OUT}.enqueue_followup") as enq:
            enqueue_next_followup(1, "p", "Jane", "connection_accepted", 0)
        enq.assert_not_called()

    def test_nurture_sequence_does_not_get_a_second_reply_check(self, monkeypatch):
        # _nurture_after_reply enqueues its own re-check; a second one here would double the walk.
        from cqc_lem.app.engagement.outreach import enqueue_next_followup
        monkeypatch.delenv("DM_NURTURE_ENABLED", raising=False)
        with patch(f"{_OUT}.get_dm_template", return_value=None), \
             patch(f"{_OUT}.enqueue_followup") as enq:
            enqueue_next_followup(1, "p", "Jane", "nurture", 0)
        enq.assert_not_called()

    def test_reply_check_delay_is_configurable(self, monkeypatch):
        from cqc_lem.app.engagement import outreach as ra
        monkeypatch.setenv("DM_REPLY_CHECK_DELAY_HOURS", "6")
        assert ra._reply_check_delay_hours() == 6
        monkeypatch.setenv("DM_REPLY_CHECK_DELAY_HOURS", "not-a-number")
        assert ra._reply_check_delay_hours() == ra._REPLY_CHECK_DEFAULT_DELAY_HOURS
        monkeypatch.delenv("DM_REPLY_CHECK_DELAY_HOURS")
        assert ra._reply_check_delay_hours() == ra._REPLY_CHECK_DEFAULT_DELAY_HOURS


def _due(**kw):
    base = {"id": 1, "user_id": 1, "profile_url": "https://x/in/jane", "first_name": "Jane",
            "event_type": "connection_accepted", "next_step": 1}
    base.update(kw)
    return base


class TestUnreadableBackoffCurve:
    """Issue #1815: the backoff arithmetic is the payload of the fix, so it is table-tested.

    A regression to "due immediately" (0 hours past the ceiling) is exactly the bug, and it would
    slip past any assertion that only checks a `due_at` is present.
    """

    @pytest.mark.parametrize(("reads", "hours"), [
        (0, 0),   # never read unreadably
        (1, 0),   # under the ceiling: ordinary cadence, a rotated selector usually clears next run
        (2, 0),
        (3, 0),   # AT the ceiling — still the grace period, still no push
        (4, 2),   # first step over
        (5, 4),
        (6, 8),
        (7, 16),
        (8, 32),
        (9, 48),  # capped
        (40, 48),
        (100_000, 48),  # the exponent is bounded too — no 30,000-digit intermediate
    ])
    def test_backoff_hours_curve(self, reads, hours):
        from cqc_lem.app.engagement.outreach import _unreadable_backoff_hours
        assert _unreadable_backoff_hours(reads) == hours

    def test_curve_is_pinned_to_the_module_constants(self):
        from cqc_lem.app.engagement import outreach as ra
        assert _unreadable_hours(ra.UNREADABLE_READ_CEILING) == 0
        assert _unreadable_hours(ra.UNREADABLE_READ_CEILING + 1) == ra.UNREADABLE_READ_BACKOFF_HOURS
        assert _unreadable_hours(ra.UNREADABLE_READ_CEILING + 50) == ra.UNREADABLE_READ_BACKOFF_CAP_HOURS


def _unreadable_hours(reads):
    from cqc_lem.app.engagement.outreach import _unreadable_backoff_hours
    return _unreadable_backoff_hours(reads)


class TestProcessUserFollowups:
    @pytest.fixture(autouse=True)
    def _no_recent_touch(self):
        # Issue #2115's spacing read is a DB call; these tests are about everything else.
        with patch(f"{_OUT}.seconds_since_last_dm_touch", return_value=None):
            yield

    def _common(self):
        return {
            f"{_OUT}.get_current_profile": patch(f"{_OUT}.get_current_profile",
                                                return_value=(MagicMock(), MagicMock(), "e@x", MagicMock())),
            f"{_OUT}.quit_gracefully": patch(f"{_OUT}.quit_gracefully"),
            f"{_OUT}.time.sleep": patch(f"{_OUT}.time.sleep"),
            f"{_OUT}.insert_new_log": patch(f"{_OUT}.insert_new_log"),
        }

    def test_sends_followup_when_not_replied(self):
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.NOT_REPLIED), \
             patch(f"{_OUT}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.mark_followup") as mark, \
             patch(f"{_OUT}.enqueue_next_followup") as enq:
            result = process_user_followups.run(user_id=1)
        dm.apply_async.assert_called_once()
        mark.assert_called_once_with(1, "sent")
        enq.assert_called_once()
        assert "Sent 1" in result

    def test_session_open_requests_needs_images(self):
        # Issue #1774: check_dm_replied walks open_message_thread's 6-route ladder over
        # /messaging/*, which never mounts with the bandwidth saver's images blocked.
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile",
                  return_value=(MagicMock(), MagicMock(), "e", MagicMock())) as gp, \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.NOT_REPLIED), \
             patch(f"{_OUT}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_OUT}.send_private_dm"), \
             patch(f"{_OUT}.mark_followup"), \
             patch(f"{_OUT}.enqueue_next_followup"):
            process_user_followups.run(user_id=1)
        assert gp.call_args.kwargs["needs_images"] is True

    def test_stops_sequence_when_replied(self):
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.REPLIED), \
             patch(f"{_OUT}.get_engagement_preferences", return_value={}), \
             patch(f"{_OUT}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_OUT}._last_inbound_message", return_value="thanks!"), \
             patch(f"{_OUT}._flag_lead_signal", return_value=None), \
             patch(f"{_OUT}.stop_followups_for_profile") as stop, \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.mark_followup") as mark:
            process_user_followups.run(user_id=1)
        stop.assert_called_once_with(1, "https://x/in/jane")
        mark.assert_called_once_with(1, "stopped")
        dm.apply_async.assert_not_called()

    def test_tab_crashed_getting_profile_logs_debug_not_error_or_warning(self):
        # Issue #1749: a crashed browser tab while acquiring the session is a known-transient
        # Selenium fault (no follow-up was even attempted, due rows stay untouched for the next
        # tick) — it must not file a grouped PostHog defect the way a real failure does.
        # get_current_profile (utilities/linkedin/session.py) already logs this at WARNING where
        # it's detected before re-raising; this outer catch is a wrapper re-reporting the same
        # occurrence, so it stays DEBUG rather than filing a second warning for one event.
        from selenium.common.exceptions import WebDriverException

        from cqc_lem.app.engagement.outreach import process_user_followups
        crash = WebDriverException("Message: tab crashed\n  (Session info: chrome=151.0.7922.108)")
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", side_effect=crash), \
             patch(f"{_OUT}.log_debug") as dbg, \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.log_error") as err:
            result = process_user_followups.run(user_id=1)
        assert "Failed to start follow-ups" in result
        dbg.assert_called_once()
        assert dbg.call_args.kwargs.get("exc") is crash
        warn.assert_not_called()
        err.assert_not_called()

    def _run_with_reads(self, *reads):
        """Run two due rows whose thread reads are `reads` (a state, or an exception to raise)."""
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups",
                   return_value=[_due(), _due(id=2, profile_url="https://x/in/joe")]), \
             patch(f"{_OUT}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully") as quit_driver, patch(f"{_OUT}.time.sleep"), \
             patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", side_effect=list(reads)) as check, \
             patch(f"{_OUT}.seconds_since_last_dm_touch", return_value=None), \
             patch(f"{_OUT}.outbound_refusal_reason", return_value=None), \
             patch(f"{_OUT}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.mark_followup") as mark, \
             patch(f"{_OUT}.enqueue_next_followup"), \
             patch(f"{_OUT}.log_error") as err, \
             patch("cqc_lem.utilities.selenium_util.log_warning") as crash_warn:
            result = process_user_followups.run(user_id=1)
        return result, {"quit": quit_driver, "check": check, "dm": dm, "mark": mark, "err": err,
                        "crash_warn": crash_warn}

    def test_tab_crashed_mid_run_ends_the_run_on_one_warning(self):
        # Issue #2421: the tab died after login, inside the thread-read loop. That used to warn on
        # every poll of every route and escalate into a `$exception`; it now ends the run once.
        from selenium.common.exceptions import WebDriverException
        crash = WebDriverException("tab crashed\n  (Session info: chrome=153.0.8010.47)")
        result, m = self._run_with_reads(ThreadState.NOT_REPLIED, crash)
        assert "Browser tab crashed" in result and "sent 1" in result
        m["dm"].apply_async.assert_called_once()  # the row read before the crash still went out
        m["mark"].assert_called_once_with(1, "sent")  # the crashed row stays 'pending'
        m["crash_warn"].assert_called_once()
        assert "process_user_followups" in m["crash_warn"].call_args.args[0]
        m["err"].assert_not_called()
        m["quit"].assert_called_once()

    def test_any_other_fault_mid_run_still_raises(self):
        with pytest.raises(RuntimeError):
            self._run_with_reads(RuntimeError("db gone"))

    def test_rate_limited_getting_profile_logs_debug_not_warning_or_error(self):
        # Issue #1940: `LinkedInRateLimited` is a known, self-clearing back-off (429 breaker,
        # manual pause, or a per-account challenge-unsolvable cooldown, ~175s). This task is
        # dispatched every few minutes while due rows exist, so the cooldown trips repeatedly
        # within a day — WARNING here (the #1920 fix) recurred 3x/24h and the recurrence-escalation
        # rule re-emitted it at ERROR, filing a grouped `$exception` for working back-off behaviour.
        # Nothing else logs this specific cooldown-check raise, so DEBUG is the only record kept.
        from cqc_lem.app.engagement.outreach import process_user_followups
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        breaker = LinkedInRateLimited("LinkedIn 429 circuit breaker open")
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", side_effect=breaker), \
             patch(f"{_OUT}.log_debug") as dbg, \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.log_error") as err:
            result = process_user_followups.run(user_id=1)
        assert "Skipped" in result
        dbg.assert_called_once()
        assert dbg.call_args.kwargs.get("exc") is breaker
        warn.assert_not_called()
        err.assert_not_called()

    def test_profile_unavailable_getting_profile_logs_warning_not_error(self):
        # Issue #1947: a fresh account with nothing cached yet, or a scrape that missed once, is
        # not a code defect — `get_current_profile` already logged the miss at ERROR (without
        # `exc=`, so no $exception) before raising this specific condition. This wrapper must
        # downgrade it to WARNING rather than the generic-`Exception` else branch's `log_error`, so
        # a one-off stays quiet and only a profile that stays unresolvable across repeated runs
        # escalates into a filed defect (utilities/CLAUDE.md).
        from cqc_lem.app.engagement.outreach import process_user_followups
        from cqc_lem.utilities.linkedin.session import ProfileUnavailableError
        unavailable = ProfileUnavailableError(
            "Profile unavailable: live scrape failed and no cached profile to fall back on")
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", side_effect=unavailable), \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.log_error") as err:
            result = process_user_followups.run(user_id=1)
        assert "Skipped" in result
        warn.assert_called_once()
        assert warn.call_args.kwargs.get("exc") is unavailable
        err.assert_not_called()

    def test_login_timeout_getting_profile_logs_debug_not_error_or_warning(self):
        # Issue #1919: a TimeoutException out of get_current_profile's login step is a Selenium
        # selector miss — e.g. an unrecognized challenge/checkpoint page (#1908). `login_to_linkedin`
        # already logs the page it saw at WARNING, and `get_current_profile` itself now downgrades
        # this to WARNING too (utilities/linkedin/session.py) before re-raising — a caller-only
        # downgrade left the SAME exception filing an immediate, ungrouped `$exception` 22x/day,
        # because `get_current_profile`'s own catch fired ERROR before this wrapper ever saw it.
        # This outer catch is a wrapper re-reporting the same occurrence, so it stays DEBUG rather
        # than filing a second warning for one event (same precedent as the tab-crash branch above).
        from selenium.common.exceptions import TimeoutException

        from cqc_lem.app.engagement.outreach import process_user_followups
        timeout = TimeoutException("Finding Username Field")
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", side_effect=timeout), \
             patch(f"{_OUT}.log_debug") as dbg, \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.log_error") as err:
            result = process_user_followups.run(user_id=1)
        assert "Failed to start follow-ups" in result
        dbg.assert_called_once()
        assert dbg.call_args.kwargs.get("exc") is timeout
        warn.assert_not_called()
        err.assert_not_called()

    def test_other_profile_failures_still_log_error(self):
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", side_effect=RuntimeError("boom")), \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.log_error") as err:
            result = process_user_followups.run(user_id=1)
        assert "Failed to start follow-ups" in result
        err.assert_called_once()
        warn.assert_not_called()

    def test_no_due_returns_early(self):
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[]), \
             patch(f"{_OUT}.get_current_profile") as gp:
            result = process_user_followups.run(user_id=1)
        assert "No due follow-ups" in result
        gp.assert_not_called()

    def test_unknown_defers_the_followup_instead_of_sending_blind(self):
        # Issue #731: the whole point of the third state. An unreadable thread must NOT send — and
        # must NOT be marked either, so the row stays due for the next run.
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.UNKNOWN), \
             patch(f"{_OUT}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.stop_followups_for_profile") as stop, \
             patch(f"{_OUT}.mark_followup") as mark:
            result = process_user_followups.run(user_id=1)
        dm.apply_async.assert_not_called()
        mark.assert_not_called()
        stop.assert_not_called()
        assert "skipped 1" in result

    def test_unknown_below_ceiling_only_counts_no_backoff(self):
        # Issue #1815: the first few UNKNOWN reads stay on the ordinary cadence — a rotated
        # selector usually clears on the very next run, so nothing should push due_at yet.
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[_due(unreadable_reads=1)]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.UNKNOWN), \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.record_unreadable_read") as rec:
            process_user_followups.run(user_id=1)
        warn.assert_not_called()
        rec.assert_called_once_with(1, due_at=None)

    def test_unknown_past_ceiling_backs_off_due_at_and_warns_once(self):
        # Crossing UNREADABLE_READ_CEILING is what actually stops the 48x/day re-read — the row
        # stays 'pending' (#731's UNKNOWN never sends is unaffected), only due_at moves out.
        import datetime

        from cqc_lem.app.engagement import outreach as ra
        from cqc_lem.app.engagement.outreach import process_user_followups
        before = datetime.datetime.now(datetime.timezone.utc)
        with patch(f"{_OUT}.get_due_followups",
                   return_value=[_due(unreadable_reads=ra.UNREADABLE_READ_CEILING)]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.UNKNOWN), \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.record_unreadable_read", return_value=True) as rec:
            process_user_followups.run(user_id=1)
        warn.assert_called_once()
        rec.assert_called_once()
        assert rec.call_args.args == (1,)
        # The PAYLOAD, not just "not None": a due_at that lands in the past (a clock mismatch, an
        # arithmetic slip) reintroduces the exact bug this PR fixes — due again on the next beat.
        pushed = rec.call_args.kwargs["due_at"]
        assert pushed >= before + datetime.timedelta(hours=ra.UNREADABLE_READ_BACKOFF_HOURS)
        assert pushed.tzinfo is not None  # aware; the repository seam normalizes it to naive UTC

    def test_unknown_does_not_warn_when_the_count_never_landed(self):
        # Issue #1815 review: the backoff decision is made from the count read at the start of the
        # run. If the write matched nothing, due_at did NOT move — warning anyway would announce a
        # backoff that never happened, again on every beat.
        from cqc_lem.app.engagement import outreach as ra
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups",
                   return_value=[_due(unreadable_reads=ra.UNREADABLE_READ_CEILING)]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.UNKNOWN), \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.log_error") as err, \
             patch(f"{_OUT}.record_unreadable_read", return_value=False):
            process_user_followups.run(user_id=1)
        warn.assert_not_called()
        err.assert_called_once()

    def test_readable_state_resets_the_unreadable_streak(self):
        # Issue #1815 review: the streak must describe an UNBROKEN run. A thread that goes UNKNOWN
        # a few times and then reads fine has to start from zero, or a later unreadable spell
        # inherits the old count and backs a healthy thread off by 48h.
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[_due(unreadable_reads=3)]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.NOT_REPLIED), \
             patch(f"{_OUT}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_OUT}.send_private_dm"), patch(f"{_OUT}.enqueue_next_followup"), \
             patch(f"{_OUT}.mark_followup"), \
             patch(f"{_OUT}.record_unreadable_read") as rec, \
             patch(f"{_OUT}.reset_unreadable_reads") as reset:
            process_user_followups.run(user_id=1)
        reset.assert_called_once_with(1)
        rec.assert_not_called()

    def test_readable_state_with_a_clean_row_spends_no_write(self):
        # The steady state is a readable thread with a zero streak — nothing to reset, so no UPDATE.
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.NOT_REPLIED), \
             patch(f"{_OUT}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_OUT}.send_private_dm"), patch(f"{_OUT}.enqueue_next_followup"), \
             patch(f"{_OUT}.mark_followup"), \
             patch(f"{_OUT}.reset_unreadable_reads") as reset:
            process_user_followups.run(user_id=1)
        reset.assert_not_called()

    def test_unknown_thread_does_not_double_warn(self):
        # Issue #1750: check_dm_replied (and the open_message_thread ladder underneath it) already
        # logs a warning at the point the read actually failed. A second warning here for the same
        # miss filed a duplicate grouped $exception (RecurringWarning) for one failure.
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.UNKNOWN), \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.log_debug") as debug:
            process_user_followups.run(user_id=1)
        warn.assert_not_called()
        assert any("could not read the thread" in c.args[0] for c in debug.call_args_list)


    def test_the_saved_display_name_is_what_the_reply_check_compares(self):
        # Issue #731 follow-up: the settings value (Setup & Connection, required) beats the scraped
        # profile name, and it is resolved ONCE per run rather than per person.
        from cqc_lem.app.engagement.outreach import process_user_followups
        profile = MagicMock(full_name="C. Queen (Consultant)")
        with patch(f"{_OUT}.get_due_followups", return_value=[_due(), _due(id=2)]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", profile)), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch("cqc_lem.utilities.db.get_user_linkedin_display_name",
                   return_value="Christopher Queen") as saved, \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.UNKNOWN) as check:
            process_user_followups.run(user_id=1)
        assert saved.call_count == 1
        assert {c.kwargs["my_name"] for c in check.call_args_list} == {"Christopher Queen"}

    def test_no_name_anywhere_skips_every_thread(self):
        # With nothing to compare against, every verdict is UNKNOWN — nothing may be sent.
        from cqc_lem.app.engagement.outreach import process_user_followups
        profile = MagicMock()
        profile.full_name = ""
        with patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", profile)), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch("cqc_lem.utilities.db.get_user_linkedin_display_name", return_value=None), \
             patch(f"{_OUT}.open_message_thread") as opened, \
             patch(f"{_OUT}.read_last_sender", return_value="Jane Doe"), \
             patch(f"{_OUT}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.mark_followup") as mark:
            opened.return_value = MagicMock(opened=True, route="anchor", events=3, surface="page")
            result = process_user_followups.run(user_id=1)
        dm.apply_async.assert_not_called()
        mark.assert_not_called()
        assert "skipped 1" in result


class TestUnreadableThreadTerminalStop:
    """#2622: a thread that stays unreadable ends in `stopped`, one-way, and sends nothing."""

    def _run(self, due, states, diag_for=None):
        """Run one pass.

        `states` is one ThreadState per row; `diag_for` the ids whose walk was composer-only.
        """
        from cqc_lem.app.engagement.outreach import process_user_followups
        diag_for = set(diag_for or ())
        queue = iter(zip(due, states))

        def _check(*_a, diagnostics=None, **_k):
            row, state = next(queue)
            if row["id"] in diag_for and diagnostics is not None:
                diagnostics["thread_unreadable"] = True
            return state

        with patch(f"{_OUT}.get_due_followups", return_value=due), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", side_effect=_check), \
             patch(f"{_OUT}.record_unreadable_read", return_value=True) as rec, \
             patch(f"{_OUT}.stop_followup_unreadable", return_value=True) as stop, \
             patch(f"{_OUT}.log_warning") as warn, \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.mark_followup"), patch(f"{_OUT}.enqueue_next_followup"), \
             patch(f"{_OUT}.build_dm_from_template", return_value="msg"), \
             patch(f"{_OUT}.seconds_since_last_dm_touch", return_value=None), \
             patch(f"{_OUT}.outbound_refusal_reason", return_value=None), \
             patch(f"{_OUT}.reset_unreadable_reads"):
            process_user_followups.run(user_id=1)
        return rec, stop, warn, dm

    def _at_terminal(self, **kw):
        from cqc_lem.app.engagement import outreach as ra
        return _due(thread_unreadable_reads=ra._unreadable_read_terminal() - 1, **kw)

    def test_row_at_the_terminal_count_is_stopped_with_one_warning_and_nothing_sent(self):
        due = [self._at_terminal(id=1), _due(id=2)]
        rec, stop, warn, dm = self._run(due, [ThreadState.UNKNOWN, ThreadState.NOT_REPLIED], {1})
        rec.assert_called_once_with(1, due_at=rec.call_args.kwargs["due_at"], thread_specific=True)
        stop.assert_called_once_with(1)
        assert sum("stopped after repeated unreadable" in c.args[0] for c in warn.call_args_list) == 1
        assert dm.apply_async.call_count == 1  # row 2's own follow-up — row 1 sent nothing

    def test_a_run_where_every_read_was_unknown_stops_nothing(self):
        rec, stop, _warn, _dm = self._run([self._at_terminal(id=1)], [ThreadState.UNKNOWN], {1})
        stop.assert_not_called()

    def test_a_non_thread_specific_unknown_never_counts_toward_the_stop(self):
        due = [self._at_terminal(id=1), _due(id=2)]
        rec, stop, _warn, _dm = self._run(due, [ThreadState.UNKNOWN, ThreadState.NOT_REPLIED])
        assert "thread_specific" not in rec.call_args.kwargs
        stop.assert_not_called()

    def test_below_the_terminal_count_does_not_stop(self):
        due = [_due(id=1, thread_unreadable_reads=2), _due(id=2)]
        _rec, stop, _warn, _dm = self._run(due, [ThreadState.UNKNOWN, ThreadState.NOT_REPLIED], {1})
        stop.assert_not_called()

    def test_terminal_env_is_parsed_defensively(self, monkeypatch):
        from cqc_lem.app.engagement.outreach import _unreadable_read_terminal
        monkeypatch.setenv("UNREADABLE_READ_TERMINAL", "junk")
        assert _unreadable_read_terminal() == 10
        monkeypatch.setenv("UNREADABLE_READ_TERMINAL", "4")
        assert _unreadable_read_terminal() == 4


class TestCheckDmRepliedExpectsHistory:
    def _opened(self, **kw):
        from cqc_lem.utilities.linkedin.message_thread import ThreadOpen
        return ThreadOpen(**kw)

    def test_ladder_is_called_with_expect_history_by_default(self, monkeypatch):
        from cqc_lem.app.engagement.outreach import check_dm_replied
        monkeypatch.delenv("DM_THREAD_EXPECT_HISTORY", raising=False)
        with patch(f"{_OUT}.open_message_thread", return_value=self._opened()) as ladder:
            check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b", my_name="Me")
        assert ladder.call_args.kwargs["expect_history"] is True

    def test_rollback_flag_restores_the_old_ladder(self, monkeypatch):
        from cqc_lem.app.engagement.outreach import check_dm_replied
        monkeypatch.setenv("DM_THREAD_EXPECT_HISTORY", "false")
        with patch(f"{_OUT}.open_message_thread", return_value=self._opened()) as ladder:
            check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b", my_name="Me")
        assert ladder.call_args.kwargs["expect_history"] is False

    def test_composer_only_walk_marks_the_thread_unreadable(self):
        from cqc_lem.app.engagement.outreach import check_dm_replied
        diag: dict = {}
        with patch(f"{_OUT}.open_message_thread",
                   return_value=self._opened(composer_only_routes=["overflow"])):
            assert check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b", my_name="Me",
                                    diagnostics=diag) is ThreadState.UNKNOWN
        assert diag == {"thread_unreadable": True}

    def test_a_walk_that_rendered_nothing_is_not_thread_specific(self):
        from cqc_lem.app.engagement.outreach import check_dm_replied
        diag: dict = {}
        with patch(f"{_OUT}.open_message_thread", return_value=self._opened()):
            check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b", my_name="Me",
                             diagnostics=diag)
        assert diag == {}

    def test_a_missing_self_name_is_not_thread_specific(self):
        from cqc_lem.app.engagement.outreach import check_dm_replied
        diag: dict = {}
        with patch(f"{_OUT}.open_message_thread",
                   return_value=self._opened(opened=True, route="anchor", events=3)), \
             patch(f"{_OUT}.read_last_sender", return_value="Jane"), \
             patch(f"{_OUT}.log_warning"):
            check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b", my_name="",
                             diagnostics=diag)
        assert diag == {}


class TestCheckDmReplied:
    """The reply verdict itself. The ladder that opens the thread is covered in
    tests/unit/utilities/linkedin/test_message_thread.py — here it is stubbed.
    """

    def _opened(self, opened=True, route="anchor", events=3):
        from cqc_lem.utilities.linkedin.message_thread import ThreadOpen
        return ThreadOpen(opened=opened, route=route if opened else None,
                          events=events if opened else 0, surface="page" if opened else None)

    def _check(self, last_sender, my_name="Christopher Queen", opened=True, events=3):
        from cqc_lem.app.engagement.outreach import check_dm_replied
        with patch(f"{_OUT}.open_message_thread", return_value=self._opened(opened, events=events)), \
             patch(f"{_OUT}.read_last_sender", return_value=last_sender):
            return check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b", my_name=my_name)

    def test_replied_when_other_person_spoke_last(self):
        assert self._check("Brandon Allen-Santos") is ThreadState.REPLIED

    def test_not_replied_when_we_spoke_last(self):
        assert self._check("Christopher Queen") is ThreadState.NOT_REPLIED

    def test_unknown_when_no_messages_are_readable(self):
        assert self._check("") is ThreadState.UNKNOWN

    def test_bare_composer_with_no_messages_is_debug_not_a_warning(self):
        # A route that opens a bare compose overlay has zero message events by design, so there is
        # no sender to read and UNKNOWN is the correct #731 outcome — an expected no-op, not a
        # defect. Warning here filed a RecurringWarning $exception against working behaviour.
        from cqc_lem.app.engagement.outreach import check_dm_replied
        with patch(f"{_OUT}.open_message_thread", return_value=self._opened(events=0)), \
             patch(f"{_OUT}.read_last_sender", return_value=""), \
             patch(f"{_OUT}.log_warning") as warn, patch(f"{_OUT}.log_debug") as debug:
            assert check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b",
                                    my_name="Me") is ThreadState.UNKNOWN
        warn.assert_not_called()
        assert "bare composer" in debug.call_args.args[0]

    def test_messages_present_but_unreadable_sender_still_warns(self):
        # A thread that carries message events but yields no sender is a real read failure (the
        # sender selector rotated), so the warning — and its escalation — must stay.
        from cqc_lem.app.engagement.outreach import check_dm_replied
        with patch(f"{_OUT}.open_message_thread", return_value=self._opened(events=3)), \
             patch(f"{_OUT}.read_last_sender", return_value=""), \
             patch(f"{_OUT}.log_warning") as warn, patch(f"{_OUT}.log_debug"):
            assert check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b",
                                    my_name="Me") is ThreadState.UNKNOWN
        assert "message events" in warn.call_args.args[0]

    def test_unknown_when_no_route_opened_a_thread(self):
        assert self._check("Brandon Allen-Santos", opened=False) is ThreadState.UNKNOWN

    def test_a_name_that_only_prefixes_the_sender_is_a_reply(self):
        # 'Chris' is a substring of 'Christine Baker'. Reading her reply as OUR last message is how
        # a follow-up goes out to somebody who already answered — the exact #731 failure.
        assert self._check("Christine Baker", my_name="Chris") is ThreadState.REPLIED

    def test_unknown_when_our_own_name_is_missing(self):
        # Without a self-name every sender looks like 'someone else' — that used to read as a reply.
        assert self._check("Brandon Allen-Santos", my_name=None) is ThreadState.UNKNOWN

    def test_an_exception_is_unknown_not_no_reply(self):
        from cqc_lem.app.engagement.outreach import check_dm_replied
        with patch(f"{_OUT}.open_message_thread", side_effect=RuntimeError("boom")):
            assert check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b",
                                    my_name="Me") is ThreadState.UNKNOWN

    def test_a_crashed_tab_is_not_an_unknown_thread(self):
        # Issue #2421: a dead tab says nothing about THIS thread — it ends the run, so it re-raises
        # for `process_user_followups` rather than warning once per remaining row.
        from selenium.common.exceptions import WebDriverException

        from cqc_lem.app.engagement.outreach import check_dm_replied
        with patch(f"{_OUT}.open_message_thread", side_effect=WebDriverException("tab crashed")), \
             patch(f"{_OUT}.log_warning") as warn, pytest.raises(WebDriverException):
            check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b", my_name="Me")
        warn.assert_not_called()

    def test_a_tab_crashed_on_the_sender_read_is_not_an_unknown_thread(self):
        # Issue #2490: the thread opened, then the tab died on the sender read. Reading that as ''
        # warned twice and counted an unreadable read against the row; it now ends the run.
        from selenium.common.exceptions import WebDriverException

        from cqc_lem.app.engagement.outreach import check_dm_replied
        from cqc_lem.utilities.linkedin import message_thread as mt
        driver = MagicMock()
        driver.execute_script.side_effect = WebDriverException("tab crashed")
        opened = MagicMock(opened=True, events=3, route="overlay")
        with patch(f"{_OUT}.open_message_thread", return_value=opened), \
             patch(f"{_OUT}.log_warning") as warn, patch.object(mt, "log_warning") as read_warn, \
             pytest.raises(WebDriverException):
            check_dm_replied(driver, MagicMock(), "https://x/in/b", my_name="Me")
        warn.assert_not_called()
        read_warn.assert_not_called()


class _EmptyComposeDriver:
    """A driver that renders nothing but an empty full-page compose screen, on every route."""

    def __init__(self):
        self.page_source = ""
        self.urls = []

    def get(self, url):
        self.urls.append(url)

    def find_elements(self, _by, _value):
        return []

    def execute_script(self, script, *_args):
        from cqc_lem.utilities.linkedin import message_thread as mt
        if script is mt._THREAD_STATE_JS:
            return {"events": 0, "composer": True, "overlay": False}
        return None


class TestExhaustedLadderStillSkips:
    """NO-REGRESSION for issue #1851 acceptance criterion 3, through the REAL ladder.

    `open_message_thread` is deliberately not stubbed here: now that an empty compose page stops
    counting as an open thread, an account with no reachable thread at all must still land on
    UNKNOWN and still skip. That is the property #731 settled and neither #1853 nor this follow-up
    may disturb, so it passes before and after — by design.
    """

    def test_every_route_exhausted_is_unknown_and_sends_nothing(self, monkeypatch):
        from cqc_lem.app.engagement import outreach
        from cqc_lem.utilities.linkedin import message_thread as mt
        monkeypatch.setattr(mt.time, "sleep", lambda *_a, **_k: None)
        driver = _EmptyComposeDriver()
        with patch.object(mt, "find_first", return_value=None), \
             patch.object(outreach, "read_last_sender", return_value="") as sender:
            state = outreach.check_dm_replied(driver, MagicMock(), "https://x/in/jane-doe",
                                              my_name="Christopher Queen",
                                              person_name="Jane Doe")
        assert state is ThreadState.UNKNOWN
        sender.assert_not_called()  # no thread was ever open to read a sender from


class TestAutoSendDueFollowups:
    def test_dispatches_per_unique_user(self):
        # `auto_send_due_followups` does a lazy `from cqc_lem.app.engagement.outreach import
        # process_user_followups` INSIDE the beat, so `outreach` IS the binding it reads. It went
        # through the `run_automation` re-export until #1206 deleted that shim (#1154).
        from cqc_lem.app.run_scheduler import auto_send_due_followups
        with patch("cqc_lem.utilities.db.get_due_followups",
                   return_value=[{"user_id": 1}, {"user_id": 1}, {"user_id": 2}]), \
             patch("cqc_lem.app.engagement.outreach.process_user_followups") as proc:
            result = auto_send_due_followups()
        assert proc.apply_async.call_count == 2  # users 1 and 2 (deduped)
        assert "2 user" in result


class TestFollowupLedgerTruth:
    """Issue #2115: the ledger row, the returned count and the recipient spacing tell the truth."""

    def _run(self, due, msgs, since_last=None):
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.get_due_followups", return_value=due), \
             patch(f"{_OUT}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), \
             patch(f"{_OUT}.insert_new_log") as log_row, \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.NOT_REPLIED), \
             patch(f"{_OUT}.build_dm_from_template", side_effect=msgs), \
             patch(f"{_OUT}.seconds_since_last_dm_touch", return_value=since_last), \
             patch(f"{_OUT}.defer_followup") as defer, \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.mark_followup") as mark, \
             patch(f"{_OUT}.enqueue_next_followup") as enq:
            result = process_user_followups.run(user_id=1)
        return result, log_row, defer, dm, mark, enq

    def test_a_refused_send_writes_failure_with_its_reason(self):
        from cqc_lem.utilities.db import LogResultType
        result, log_row, _defer, dm, mark, enq = self._run(
            [_due()], ["Hi {first_name}, following up on {blog_url}"])
        dm.apply_async.assert_not_called()
        kwargs = log_row.call_args.kwargs
        assert kwargs["result"] is LogResultType.FAILURE
        assert kwargs["message"].startswith("Refused: ")
        assert len(kwargs["message"]) > len("Refused: ")
        mark.assert_called_once_with(1, "failed")
        enq.assert_called_once()  # nothing was sent, so the next step is still owed
        assert "Sent 0" in result
        assert "refused 1" in result

    def test_the_returned_count_equals_the_landed_sends(self):
        from cqc_lem.utilities.db import LogResultType
        due = [_due(id=1, profile_url="https://x/in/a"), _due(id=2, profile_url="https://x/in/b"),
               _due(id=3, profile_url="https://x/in/c")]
        msgs = ["Thanks again for connecting, Jane. Curious what you are working on this quarter.",
                "Hi {first_name}, following up on {blog_url}",
                "Good to be connected, Jane. Happy to swap notes on scaling ops teams any time."]
        result, log_row, _defer, dm, _mark, _enq = self._run(due, msgs)
        assert dm.apply_async.call_count == 2
        successes = [c for c in log_row.call_args_list if c.kwargs["result"] is LogResultType.SUCCESS]
        assert len(successes) == dm.apply_async.call_count
        assert f"Sent {dm.apply_async.call_count} " in result

    def test_a_second_step_within_48h_of_a_prior_touch_is_skipped(self):
        from cqc_lem.app.engagement.outreach import FOLLOWUP_MIN_SPACING_HOURS
        assert FOLLOWUP_MIN_SPACING_HOURS == 48
        with patch(f"{_OUT}.datetime") as dt:
            import datetime as real
            now = real.datetime(2026, 9, 24, 12, 0, tzinfo=real.timezone.utc)
            dt.now.return_value = now
            result, log_row, defer, dm, mark, enq = self._run(
                [_due()], ["unused"], since_last=24 * 3600)
        dm.apply_async.assert_not_called()
        log_row.assert_not_called()
        mark.assert_not_called()  # stays pending — deferred, not dropped
        enq.assert_not_called()
        defer.assert_called_once_with(1, now + real.timedelta(hours=24))
        assert "deferred 1" in result

    def test_a_touch_outside_the_window_sends(self):
        _result, _log_row, defer, dm, _mark, _enq = self._run(
            [_due()], ["Thanks again for connecting, Jane. Curious what you are working on."],
            since_last=None)
        defer.assert_not_called()
        dm.apply_async.assert_called_once()


class TestBrandFunnelFollowupHold:
    """Owner ruling 2026-10-10 (#2376, option A): a brand funnel follow-up never sends.

    The funnel's DM ladder is acquisition outbound. A due 'funnel' row for the brand account is
    STOPPED without a send; a non-brand 'funnel' row and the brand's ordinary ladders still send.
    """

    def _run(self, user_id, event_type, brand_user_id=""):
        from cqc_lem.app.engagement.outreach import process_user_followups
        row = _due(user_id=user_id, event_type=event_type, next_step=1)
        with patch("cqc_lem.utilities.brand_account.BRAND_USER_ID", brand_user_id), \
             patch(f"{_OUT}.seconds_since_last_dm_touch", return_value=None), \
             patch(f"{_OUT}.get_due_followups", return_value=[row]), \
             patch(f"{_OUT}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), \
             patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.NOT_REPLIED), \
             patch(f"{_OUT}.build_dm_from_template", return_value="step-1 follow up") as build, \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.mark_followup") as mark, \
             patch(f"{_OUT}.enqueue_next_followup") as enq:
            result = process_user_followups.run(user_id=user_id)
        return result, build, dm, mark, enq

    def test_a_brand_funnel_followup_sends_nothing_and_is_stopped(self):
        result, build, dm, mark, enq = self._run(1, "funnel")
        dm.apply_async.assert_not_called()
        build.assert_not_called()                             # not even drafted into a send
        mark.assert_called_once_with(1, "stopped")
        enq.assert_not_called()                               # and the ladder does not continue
        assert "Sent 0" in result

    def test_the_hold_follows_an_overridden_brand_user(self):
        _result, _build, dm, _mark, _enq = self._run(7, "funnel", brand_user_id="7")
        dm.apply_async.assert_not_called()

    def test_a_non_brand_funnel_followup_still_sends(self):
        result, _build, dm, mark, _enq = self._run(2, "funnel")
        dm.apply_async.assert_called_once()
        mark.assert_called_once_with(1, "sent")
        assert "Sent 1" in result

    @pytest.mark.parametrize("event_type", ["profile_viewer", "connection_accepted"])
    def test_the_brands_ordinary_followups_still_send(self, event_type):
        result, _build, dm, mark, _enq = self._run(1, event_type)
        dm.apply_async.assert_called_once()
        mark.assert_called_once_with(1, "sent")
        assert "Sent 1" in result


class TestFollowupStepModeReview:
    """Issue #2664: `followup_step_mode=pre_review` files a PENDING draft instead of sending."""

    def _run(self, mode_allows: bool, open_draft: bool = False, dm_id=7):
        from cqc_lem.app.engagement.outreach import process_user_followups
        with patch(f"{_OUT}.may_auto_approve", return_value=mode_allows), \
             patch(f"{_OUT}.get_due_followups", return_value=[_due()]), \
             patch(f"{_OUT}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_OUT}.quit_gracefully"), patch(f"{_OUT}.time.sleep"), \
             patch(f"{_OUT}.insert_new_log"), \
             patch(f"{_OUT}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_OUT}.check_dm_replied", return_value=ThreadState.NOT_REPLIED), \
             patch(f"{_OUT}.seconds_since_last_dm_touch", return_value=None), \
             patch(f"{_OUT}.outbound_refusal_reason", return_value=None), \
             patch(f"{_OUT}.build_dm_from_template", return_value="Good to be connected, Jane."), \
             patch(f"{_OUT}.has_open_scheduled_dm", return_value=open_draft), \
             patch(f"{_OUT}.insert_scheduled_dm", return_value=dm_id) as ins, \
             patch(f"{_OUT}.defer_followup") as defer, \
             patch(f"{_OUT}.send_private_dm") as dm, \
             patch(f"{_OUT}.mark_followup") as mark, \
             patch(f"{_OUT}.enqueue_next_followup") as enq:
            result = process_user_followups.run(user_id=1)
        return result, ins, defer, dm, mark, enq

    def test_pre_review_files_a_pending_draft_and_sends_nothing(self):
        from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_FOLLOWUP, ScheduledDmStatus
        result, ins, _defer, dm, mark, enq = self._run(False)
        dm.apply_async.assert_not_called()
        assert ins.call_args.kwargs["status"] is ScheduledDmStatus.PENDING
        assert ins.call_args.kwargs["source"] == SCHEDULED_DM_SOURCE_FOLLOWUP
        mark.assert_called_once_with(1, "sent")
        enq.assert_called_once()  # the ladder keeps its place
        assert "Sent 0" in result and "filed 1 for review" in result

    def test_auto_approve_still_sends_directly(self):
        _result, ins, _defer, dm, _mark, _enq = self._run(True)
        dm.apply_async.assert_called_once()
        ins.assert_not_called()

    def test_an_open_draft_defers_the_step_instead_of_stacking(self):
        result, ins, defer, dm, mark, _enq = self._run(False, open_draft=True)
        ins.assert_not_called()
        dm.apply_async.assert_not_called()
        defer.assert_called_once()
        mark.assert_not_called()
        assert "deferred 1" in result

    def test_a_failed_insert_leaves_the_row_pending(self):
        _result, _ins, _defer, dm, mark, enq = self._run(False, dm_id=None)
        dm.apply_async.assert_not_called()
        mark.assert_not_called()
        enq.assert_not_called()
