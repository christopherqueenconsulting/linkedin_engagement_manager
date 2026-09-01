"""Unit tests for the DM follow-up sequencer (enqueue, process, dispatch)."""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.linkedin.message_thread import ThreadState

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"  # lgtm[py/unused-global-variable]


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
