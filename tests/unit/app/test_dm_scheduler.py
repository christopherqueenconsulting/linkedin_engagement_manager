"""Unit tests for the DM scheduler task + scanner (issue #306)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from cqc_lem.app.task_outcome import LaneTaskFailed

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
             patch("cqc_lem.utilities.db.update_scheduled_dm_status") as upd, \
             pytest.raises(LaneTaskFailed, match="failed"):
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

    def test_dm_three_days_past_its_slot_returns_to_pending_unsent(self):
        # Issue #2103: five approved drafts went out 3-9 days late in one burst. An approval was
        # for a message at a time, so a DM that far past its slot goes back for re-approval.
        from cqc_lem.app.engagement import outreach as ra
        from cqc_lem.utilities.db import ScheduledDmStatus
        dm = dict(self._dm(status="scheduled"),
                  scheduled_time=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3))
        with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=dm), \
             patch(f"{_OUT}.get_engagement_preferences") as prefs, \
             patch(f"{_OUT}.send_dm_now") as send, \
             patch("cqc_lem.utilities.db.update_scheduled_dm_status") as upd:
            out = ra.send_scheduled_dm(3)
        send.assert_not_called()
        prefs.assert_not_called()
        upd.assert_called_once_with(3, ScheduledDmStatus.PENDING)
        assert "pending" in out

    def test_dm_a_little_past_its_slot_still_sends(self):
        from cqc_lem.app.engagement import outreach as ra
        dm = dict(self._dm(), scheduled_time=datetime.now(timezone.utc) - timedelta(hours=6))
        with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=dm), \
             patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0), \
             patch(f"{_OUT}.get_engagement_preferences", return_value={"max_dms_per_day": 10}), \
             patch(f"{_OUT}.send_dm_now", return_value=True) as send, \
             patch("cqc_lem.utilities.db.update_scheduled_dm_status"):
            ra.send_scheduled_dm(3)
        send.assert_called_once()

    def test_skips_non_sendable_status(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=self._dm(status="sent")), \
             patch(f"{_OUT}.send_dm_now") as send:
            out = ra.send_scheduled_dm(3)
        send.assert_not_called()
        assert "not sendable" in out


class TestScheduledDmIsStale:
    def test_naive_slot_is_read_as_utc(self):
        from cqc_lem.app.engagement.outreach import scheduled_dm_is_stale
        now = datetime(2026, 9, 16, 0, 1, tzinfo=timezone.utc)
        assert scheduled_dm_is_stale(datetime(2026, 9, 13, 0, 0), now=now)
        assert not scheduled_dm_is_stale(datetime(2026, 9, 15, 0, 0), now=now)

    def test_no_slot_is_never_stale(self):
        from cqc_lem.app.engagement.outreach import scheduled_dm_is_stale
        assert not scheduled_dm_is_stale(None)


def _recent(minutes_ago: int = 5) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


class TestAutoCheckScheduledDms:
    def test_dispatches_for_active_user(self):
        from cqc_lem.app import run_scheduler as rs
        due = [(3, _recent(), 1)]
        with patch(f"{_RS}.get_due_scheduled_dms", return_value=due), \
             patch(f"{_RS}.get_orphaned_scheduled_dms", return_value=[]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.get_user_ids_with_dms_in_flight", return_value=set()), \
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
        due = [(3, _recent(), 99)]
        with patch(f"{_RS}.get_due_scheduled_dms", return_value=due), \
             patch(f"{_RS}.get_orphaned_scheduled_dms", return_value=[]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.get_user_ids_with_dms_in_flight", return_value=set()), \
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

    def _run(self, due, in_flight=frozenset(), gap=600):
        from cqc_lem.app import run_scheduler as rs
        with patch(f"{_RS}.get_due_scheduled_dms", return_value=due), \
             patch(f"{_RS}.get_orphaned_scheduled_dms", return_value=[]), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_RS}.get_user_ids_with_dms_in_flight", return_value=set(in_flight)), \
             patch(f"{_RS}.dm_send_gap_seconds", return_value=gap), \
             patch(f"{_RS}.update_scheduled_dm_status") as upd, \
             patch(f"{_RS}.send_scheduled_dm") as task:
            out = rs.auto_check_scheduled_dms()
        etas = {c.kwargs["kwargs"]["dm_id"]: c.kwargs["eta"] for c in task.apply_async.call_args_list}
        return out, etas, upd

    def test_consecutive_sends_in_one_run_are_separated_by_the_drawn_gap(self):
        # Issue #2103: seven overdue DMs left in 4m50s. One user's backlog is now staggered by the
        # human_pacing draw rather than every eta landing in the past at once.
        due = [(10, _recent(60), 1), (11, _recent(50), 1), (12, _recent(40), 1)]
        _, etas, _ = self._run(due, gap=600)
        ordered = [etas[10], etas[11], etas[12]]
        assert all((b - a).total_seconds() >= 600 for a, b in zip(ordered, ordered[1:]))

    def test_stagger_is_per_user(self):
        slot = _recent(30)
        due = [(10, slot, 1), (20, slot, 2)]
        _, etas, _ = self._run(due, gap=600)
        assert etas[10] == etas[20]  # a second user's DM is not queued behind the first user's

    def test_future_slot_is_kept_when_later_than_the_gap(self):
        slot = datetime.now(timezone.utc) + timedelta(hours=1)
        due = [(10, _recent(), 1), (11, slot, 1)]
        _, etas, _ = self._run(due, gap=60)
        assert etas[11] == slot

    def test_stale_due_dm_returns_to_pending_and_is_not_dispatched(self):
        from cqc_lem.utilities.db import ScheduledDmStatus
        due = [(10, datetime.now(timezone.utc) - timedelta(days=3), 1), (11, _recent(), 1)]
        out, etas, upd = self._run(due)
        assert list(etas) == [11]
        upd.assert_any_call(10, ScheduledDmStatus.PENDING)
        assert "returned 1 stale" in out

    def test_user_with_a_batch_in_flight_gets_no_new_dispatch(self):
        due = [(10, _recent(), 1), (20, _recent(), 2)]
        _, etas, upd = self._run(due, in_flight={1})
        assert list(etas) == [20]
        assert all(c.args[0] != 10 for c in upd.call_args_list)  # left 'approved'

    def test_dm_past_the_spacing_window_stays_approved(self):
        # An eta beyond the orphan reaper's window would be re-queued early by the reaper, so the
        # run stops staggering there and leaves the rest for a later scan.
        due = [(i, _recent(), 1) for i in range(10, 20)]
        _, etas, upd = self._run(due, gap=30 * 60)
        assert 0 < len(etas) < 10
        dispatched = set(etas)
        assert all(c.args[0] in dispatched for c in upd.call_args_list)
