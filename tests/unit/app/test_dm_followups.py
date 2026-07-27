"""Unit tests for the DM follow-up sequencer (enqueue, process, dispatch)."""

import pytest
from unittest.mock import MagicMock, patch

from cqc_lem.utilities.linkedin.message_thread import ThreadState

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"


class TestEnqueueNextFollowup:
    def test_enqueues_when_next_template_exists(self):
        from cqc_lem.app.run_automation import enqueue_next_followup
        with patch(f"{_RA}.get_dm_template", return_value={"template_text": "hi", "delay_hours": 48, "step": 1}), \
             patch(f"{_RA}.enqueue_followup") as enq:
            enqueue_next_followup(1, "p", "Jane", "connection_accepted", 0)
        enq.assert_called_once()
        assert enq.call_args[0][4] == 1  # next_step

    def test_queues_a_reply_check_when_no_next_template(self, monkeypatch):
        # Issue #623: the stock templates are step-0 only, so this branch used to end every thread
        # the moment the first DM went out — dm_followups stayed empty and the nurture flywheel
        # never turned. Now it schedules a reply check at the same (template-less) step.
        from cqc_lem.app.run_automation import enqueue_next_followup
        monkeypatch.delenv("DM_NURTURE_ENABLED", raising=False)
        with patch(f"{_RA}.get_dm_template", return_value=None), \
             patch(f"{_RA}.enqueue_followup") as enq:
            enqueue_next_followup(1, "p", "Jane", "connection_accepted", 0)
        enq.assert_called_once()
        assert enq.call_args[0][3] == "connection_accepted"  # NOT the nurture sequence
        assert enq.call_args[0][4] == 1

    def test_no_reply_check_when_nurture_is_disabled(self, monkeypatch):
        from cqc_lem.app.run_automation import enqueue_next_followup
        monkeypatch.setenv("DM_NURTURE_ENABLED", "false")
        with patch(f"{_RA}.get_dm_template", return_value=None), \
             patch(f"{_RA}.enqueue_followup") as enq:
            enqueue_next_followup(1, "p", "Jane", "connection_accepted", 0)
        enq.assert_not_called()

    def test_nurture_sequence_does_not_get_a_second_reply_check(self, monkeypatch):
        # _nurture_after_reply enqueues its own re-check; a second one here would double the walk.
        from cqc_lem.app.run_automation import enqueue_next_followup
        monkeypatch.delenv("DM_NURTURE_ENABLED", raising=False)
        with patch(f"{_RA}.get_dm_template", return_value=None), \
             patch(f"{_RA}.enqueue_followup") as enq:
            enqueue_next_followup(1, "p", "Jane", "nurture", 0)
        enq.assert_not_called()

    def test_reply_check_delay_is_configurable(self, monkeypatch):
        from cqc_lem.app import run_automation as ra
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


class TestProcessUserFollowups:
    def _common(self):
        return {
            f"{_RA}.get_current_profile": patch(f"{_RA}.get_current_profile",
                                                return_value=(MagicMock(), MagicMock(), "e@x", MagicMock())),
            f"{_RA}.quit_gracefully": patch(f"{_RA}.quit_gracefully"),
            f"{_RA}.time.sleep": patch(f"{_RA}.time.sleep"),
            f"{_RA}.insert_new_log": patch(f"{_RA}.insert_new_log"),
        }

    def test_sends_followup_when_not_replied(self):
        from cqc_lem.app.run_automation import process_user_followups
        with patch(f"{_RA}.get_due_followups", return_value=[_due()]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.quit_gracefully"), patch(f"{_RA}.time.sleep"), patch(f"{_RA}.insert_new_log"), \
             patch(f"{_RA}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_RA}.check_dm_replied", return_value=ThreadState.NOT_REPLIED), \
             patch(f"{_RA}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_RA}.send_private_dm") as dm, \
             patch(f"{_RA}.mark_followup") as mark, \
             patch(f"{_RA}.enqueue_next_followup") as enq:
            result = process_user_followups.run(user_id=1)
        dm.apply_async.assert_called_once()
        mark.assert_called_once_with(1, "sent")
        enq.assert_called_once()
        assert "Sent 1" in result

    def test_stops_sequence_when_replied(self):
        from cqc_lem.app.run_automation import process_user_followups
        with patch(f"{_RA}.get_due_followups", return_value=[_due()]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.quit_gracefully"), patch(f"{_RA}.time.sleep"), patch(f"{_RA}.insert_new_log"), \
             patch(f"{_RA}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_RA}.check_dm_replied", return_value=ThreadState.REPLIED), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}._last_inbound_message", return_value="thanks!"), \
             patch(f"{_RA}._flag_lead_signal", return_value=None), \
             patch(f"{_RA}.stop_followups_for_profile") as stop, \
             patch(f"{_RA}.send_private_dm") as dm, \
             patch(f"{_RA}.mark_followup") as mark:
            process_user_followups.run(user_id=1)
        stop.assert_called_once_with(1, "https://x/in/jane")
        mark.assert_called_once_with(1, "stopped")
        dm.apply_async.assert_not_called()

    def test_no_due_returns_early(self):
        from cqc_lem.app.run_automation import process_user_followups
        with patch(f"{_RA}.get_due_followups", return_value=[]), \
             patch(f"{_RA}.get_current_profile") as gp:
            result = process_user_followups.run(user_id=1)
        assert "No due follow-ups" in result
        gp.assert_not_called()

    def test_unknown_defers_the_followup_instead_of_sending_blind(self):
        # Issue #731: the whole point of the third state. An unreadable thread must NOT send — and
        # must NOT be marked either, so the row stays due for the next run.
        from cqc_lem.app.run_automation import process_user_followups
        with patch(f"{_RA}.get_due_followups", return_value=[_due()]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.quit_gracefully"), patch(f"{_RA}.time.sleep"), patch(f"{_RA}.insert_new_log"), \
             patch(f"{_RA}.resolve_self_name", return_value="Christopher Queen"), \
             patch(f"{_RA}.check_dm_replied", return_value=ThreadState.UNKNOWN), \
             patch(f"{_RA}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_RA}.send_private_dm") as dm, \
             patch(f"{_RA}.stop_followups_for_profile") as stop, \
             patch(f"{_RA}.mark_followup") as mark:
            result = process_user_followups.run(user_id=1)
        dm.apply_async.assert_not_called()
        mark.assert_not_called()
        stop.assert_not_called()
        assert "skipped 1" in result


    def test_the_saved_display_name_is_what_the_reply_check_compares(self):
        # Issue #731 follow-up: the settings value (Setup & Connection, required) beats the scraped
        # profile name, and it is resolved ONCE per run rather than per person.
        from cqc_lem.app.run_automation import process_user_followups
        profile = MagicMock(full_name="C. Queen (Consultant)")
        with patch(f"{_RA}.get_due_followups", return_value=[_due(), _due(id=2)]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", profile)), \
             patch(f"{_RA}.quit_gracefully"), patch(f"{_RA}.time.sleep"), patch(f"{_RA}.insert_new_log"), \
             patch("cqc_lem.utilities.db.get_user_linkedin_display_name",
                   return_value="Christopher Queen") as saved, \
             patch(f"{_RA}.check_dm_replied", return_value=ThreadState.UNKNOWN) as check:
            process_user_followups.run(user_id=1)
        assert saved.call_count == 1
        assert {c.kwargs["my_name"] for c in check.call_args_list} == {"Christopher Queen"}

    def test_no_name_anywhere_skips_every_thread(self):
        # With nothing to compare against, every verdict is UNKNOWN — nothing may be sent.
        from cqc_lem.app.run_automation import process_user_followups
        profile = MagicMock()
        profile.full_name = ""
        with patch(f"{_RA}.get_due_followups", return_value=[_due()]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", profile)), \
             patch(f"{_RA}.quit_gracefully"), patch(f"{_RA}.time.sleep"), patch(f"{_RA}.insert_new_log"), \
             patch("cqc_lem.utilities.db.get_user_linkedin_display_name", return_value=None), \
             patch(f"{_RA}.open_message_thread") as opened, \
             patch(f"{_RA}.read_last_sender", return_value="Jane Doe"), \
             patch(f"{_RA}.build_dm_from_template", return_value="follow up msg"), \
             patch(f"{_RA}.send_private_dm") as dm, \
             patch(f"{_RA}.mark_followup") as mark:
            opened.return_value = MagicMock(opened=True, route="anchor", events=3, surface="page")
            result = process_user_followups.run(user_id=1)
        dm.apply_async.assert_not_called()
        mark.assert_not_called()
        assert "skipped 1" in result


class TestCheckDmReplied:
    """The reply verdict itself. The ladder that opens the thread is covered in
    tests/unit/utilities/linkedin/test_message_thread.py — here it is stubbed."""

    def _opened(self, opened=True, route="anchor"):
        from cqc_lem.utilities.linkedin.message_thread import ThreadOpen
        return ThreadOpen(opened=opened, route=route if opened else None,
                          events=3 if opened else 0, surface="page" if opened else None)

    def _check(self, last_sender, my_name="Christopher Queen", opened=True):
        from cqc_lem.app.run_automation import check_dm_replied
        with patch(f"{_RA}.open_message_thread", return_value=self._opened(opened)), \
             patch(f"{_RA}.read_last_sender", return_value=last_sender):
            return check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b", my_name=my_name)

    def test_replied_when_other_person_spoke_last(self):
        assert self._check("Brandon Allen-Santos") is ThreadState.REPLIED

    def test_not_replied_when_we_spoke_last(self):
        assert self._check("Christopher Queen") is ThreadState.NOT_REPLIED

    def test_unknown_when_no_messages_are_readable(self):
        assert self._check("") is ThreadState.UNKNOWN

    def test_unknown_when_no_route_opened_a_thread(self):
        assert self._check("Brandon Allen-Santos", opened=False) is ThreadState.UNKNOWN

    def test_unknown_when_our_own_name_is_missing(self):
        # Without a self-name every sender looks like 'someone else' — that used to read as a reply.
        assert self._check("Brandon Allen-Santos", my_name=None) is ThreadState.UNKNOWN

    def test_an_exception_is_unknown_not_no_reply(self):
        from cqc_lem.app.run_automation import check_dm_replied
        with patch(f"{_RA}.open_message_thread", side_effect=RuntimeError("boom")):
            assert check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b",
                                    my_name="Me") is ThreadState.UNKNOWN


class TestAutoSendDueFollowups:
    def test_dispatches_per_unique_user(self):
        from cqc_lem.app.run_scheduler import auto_send_due_followups
        with patch("cqc_lem.utilities.db.get_due_followups",
                   return_value=[{"user_id": 1}, {"user_id": 1}, {"user_id": 2}]), \
             patch("cqc_lem.app.run_automation.process_user_followups") as proc:
            result = auto_send_due_followups()
        assert proc.apply_async.call_count == 2  # users 1 and 2 (deduped)
        assert "2 user" in result
