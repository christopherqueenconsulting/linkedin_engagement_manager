"""Unit tests for the DM scheduler task + scanner (issue #306)."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"
_RS = "cqc_lem.app.run_scheduler"


class TestSendScheduledDm:
    def _dm(self, status="approved"):
        return {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/jane",
                "message": "hi jane", "status": status}

    def test_sends_and_marks_sent(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=self._dm()), \
             patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0), \
             patch(f"{_OUT}.get_engagement_preferences", return_value={"max_dms_per_day": 10}), \
             patch(f"{_OUT}.send_dm_now", return_value=True) as send, \
             patch("cqc_lem.utilities.db.update_scheduled_dm_status") as upd:
            out = ra.send_scheduled_dm(3)
        send.assert_called_once_with(1, "https://x/in/jane", "hi jane")
        from cqc_lem.utilities.db import ScheduledDmStatus
        upd.assert_called_once_with(3, ScheduledDmStatus.SENT)
        assert "sent" in out

    def test_failed_send_marks_failed(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=self._dm()), \
             patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0), \
             patch(f"{_OUT}.get_engagement_preferences", return_value={"max_dms_per_day": 10}), \
             patch(f"{_OUT}.send_dm_now", return_value=False), \
             patch("cqc_lem.utilities.db.update_scheduled_dm_status") as upd:
            ra.send_scheduled_dm(3)
        from cqc_lem.utilities.db import ScheduledDmStatus
        upd.assert_called_once_with(3, ScheduledDmStatus.FAILED)

    def test_defers_when_cap_reached(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=self._dm()), \
             patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=10), \
             patch(f"{_OUT}.get_engagement_preferences", return_value={"max_dms_per_day": 10}), \
             patch(f"{_OUT}.send_dm_now") as send, \
             patch("cqc_lem.utilities.db.update_scheduled_dm_status") as upd:
            out = ra.send_scheduled_dm(3)
        send.assert_not_called()  # over cap → never sends
        from cqc_lem.utilities.db import ScheduledDmStatus
        upd.assert_called_once_with(3, ScheduledDmStatus.APPROVED)  # deferred for next scan
        assert "cap" in out.lower()

    def test_skips_non_sendable_status(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=self._dm(status="sent")), \
             patch(f"{_OUT}.send_dm_now") as send:
            out = ra.send_scheduled_dm(3)
        send.assert_not_called()
        assert "not sendable" in out


class TestAutoCheckScheduledDms:
    def test_dispatches_for_active_user(self):
        from cqc_lem.app import run_scheduler as rs
        due = [(3, datetime(2026, 8, 1, 9, tzinfo=timezone.utc), 1)]
        with patch(f"{_RS}.get_due_scheduled_dms", return_value=due), \
             patch(f"{_RS}.get_orphaned_scheduled_dms", return_value=[]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.update_scheduled_dm_status") as upd, \
             patch(f"{_RS}.send_scheduled_dm") as task:
            out = rs.auto_check_scheduled_dms()
        task.apply_async.assert_called_once()
        assert task.apply_async.call_args.kwargs["kwargs"] == {"dm_id": 3}
        from cqc_lem.utilities.db import ScheduledDmStatus
        upd.assert_called_once_with(3, ScheduledDmStatus.SCHEDULED)
        assert "Scheduled 1" in out

    def test_skips_inactive_user(self):
        from cqc_lem.app import run_scheduler as rs
        due = [(3, datetime(2026, 8, 1, 9, tzinfo=timezone.utc), 99)]
        with patch(f"{_RS}.get_due_scheduled_dms", return_value=due), \
             patch(f"{_RS}.get_orphaned_scheduled_dms", return_value=[]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.update_scheduled_dm_status") as upd, \
             patch(f"{_RS}.send_scheduled_dm") as task:
            out = rs.auto_check_scheduled_dms()
        task.apply_async.assert_not_called()
        upd.assert_not_called()
        assert "No DMs" in out

    def test_requeues_orphaned_scheduled_dms(self):
        # A DM stuck in 'scheduled' (send task lost, e.g. container restart) gets re-queued
        # immediately — no eta, no second status flip. Mirrors the orphaned-post recovery.
        from cqc_lem.app import run_scheduler as rs
        orphan = [(7, datetime(2026, 7, 1, 9, tzinfo=timezone.utc), 1)]
        with patch(f"{_RS}.get_due_scheduled_dms", return_value=[]), \
             patch(f"{_RS}.get_orphaned_scheduled_dms", return_value=orphan) as orph, \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.update_scheduled_dm_status") as upd, \
             patch(f"{_RS}.send_scheduled_dm") as task:
            out = rs.auto_check_scheduled_dms()
        orph.assert_called_once_with(lookback_hours=2)
        task.apply_async.assert_called_once_with(kwargs={"dm_id": 7})
        upd.assert_not_called()
        assert "re-queued 1 orphaned" in out
