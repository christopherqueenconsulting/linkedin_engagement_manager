"""Unit tests for the DM follow-up sequencer (enqueue, process, dispatch)."""

import pytest
from unittest.mock import MagicMock, patch

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

    def test_no_enqueue_when_no_next_template(self):
        from cqc_lem.app.run_automation import enqueue_next_followup
        with patch(f"{_RA}.get_dm_template", return_value=None), \
             patch(f"{_RA}.enqueue_followup") as enq:
            enqueue_next_followup(1, "p", "Jane", "connection_accepted", 0)
        enq.assert_not_called()


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
             patch(f"{_RA}.check_dm_replied", return_value=False), \
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
             patch(f"{_RA}.check_dm_replied", return_value=True), \
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


class TestCheckDmReplied:
    def _driver(self, last_sender):
        d = MagicMock()
        # execute_script is called twice: [click msg button, last-sender JS]
        d.execute_script.side_effect = [None, last_sender]
        return d

    def test_true_when_other_person_spoke_last(self):
        from cqc_lem.app.run_automation import check_dm_replied
        d = self._driver("Brandon Allen-Santos")
        with patch(f"{_RA}.time.sleep"), patch(f"{_RA}.find_first", return_value=MagicMock()):
            assert check_dm_replied(d, MagicMock(), "https://x/in/b", my_name="Christopher Queen") is True

    def test_false_when_we_spoke_last(self):
        from cqc_lem.app.run_automation import check_dm_replied
        d = self._driver("Christopher Queen")
        with patch(f"{_RA}.time.sleep"), patch(f"{_RA}.find_first", return_value=MagicMock()):
            assert check_dm_replied(d, MagicMock(), "https://x/in/b", my_name="Christopher Queen") is False

    def test_false_when_no_messages(self):
        from cqc_lem.app.run_automation import check_dm_replied
        d = self._driver(None)
        with patch(f"{_RA}.time.sleep"), patch(f"{_RA}.find_first", return_value=MagicMock()):
            assert check_dm_replied(d, MagicMock(), "https://x/in/b", my_name="Christopher Queen") is False

    def test_false_when_no_message_button(self):
        from cqc_lem.app.run_automation import check_dm_replied
        with patch(f"{_RA}.time.sleep"), patch(f"{_RA}.find_first", return_value=None):
            assert check_dm_replied(MagicMock(), MagicMock(), "https://x/in/b", my_name="Me") is False


class TestAutoSendDueFollowups:
    def test_dispatches_per_unique_user(self):
        from cqc_lem.app.run_scheduler import auto_send_due_followups
        with patch("cqc_lem.utilities.db.get_due_followups",
                   return_value=[{"user_id": 1}, {"user_id": 1}, {"user_id": 2}]), \
             patch("cqc_lem.app.run_automation.process_user_followups") as proc:
            result = auto_send_due_followups()
        assert proc.apply_async.call_count == 2  # users 1 and 2 (deduped)
        assert "2 user" in result
