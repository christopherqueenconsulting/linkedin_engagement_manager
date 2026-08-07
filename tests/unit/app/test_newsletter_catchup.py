"""Newsletter catch-up publishing: at most one edition per user per run, and a missed-slot backlog
shifts forward (order preserved) instead of all publishing at once.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RS = "cqc_lem.app.run_scheduler"
_DB = "cqc_lem.utilities.db"
_NL = "cqc_lem.utilities.newsletter"


class TestPublishNextDueEditionForUser:
    def test_no_due_editions_is_noop(self):
        from cqc_lem.app import run_scheduler as rs
        pending = [{"id": 6, "scheduled_for": datetime(2026, 8, 1, 13, 0)}]  # future
        dispatch = MagicMock()
        with patch(f"{_DB}.get_pending_newsletter_editions", return_value=pending):
            n = rs._publish_next_due_edition_for_user(1, datetime(2026, 7, 23, 3, 0), dispatch)
        assert n == 0
        dispatch.assert_not_called()

    def test_single_due_publishes_without_reschedule(self):
        from cqc_lem.app import run_scheduler as rs
        pending = [{"id": 5, "scheduled_for": datetime(2026, 7, 21, 13, 0)},   # due
                   {"id": 6, "scheduled_for": datetime(2026, 7, 28, 13, 0)}]   # future
        dispatch = MagicMock()
        with patch(f"{_DB}.get_pending_newsletter_editions", return_value=pending), \
             patch(f"{_RS}._reschedule_pending_editions_forward") as resched:
            n = rs._publish_next_due_edition_for_user(1, datetime(2026, 7, 22, 3, 0), dispatch)
        assert n == 1
        dispatch.assert_called_once_with(5)
        resched.assert_not_called()  # only one due -> nothing to shift

    def test_backlog_publishes_oldest_only_and_shifts_rest_in_order(self):
        from cqc_lem.app import run_scheduler as rs
        pending = [{"id": 2, "scheduled_for": datetime(2026, 7, 14, 13, 0)},   # due (oldest)
                   {"id": 3, "scheduled_for": datetime(2026, 7, 21, 13, 0)},   # due (backlog)
                   {"id": 4, "scheduled_for": datetime(2026, 7, 28, 13, 0)}]   # future
        dispatch = MagicMock()
        with patch(f"{_DB}.get_pending_newsletter_editions", return_value=pending), \
             patch(f"{_RS}._reschedule_pending_editions_forward", return_value=2) as resched:
            n = rs._publish_next_due_edition_for_user(1, datetime(2026, 7, 23, 3, 0), dispatch)
        assert n == 1
        dispatch.assert_called_once_with(2)                 # ONLY the oldest publishes now
        resched.assert_called_once()
        shifted_ids = [e["id"] for e in resched.call_args.args[1]]
        assert shifted_ids == [3, 4]                        # everything after the oldest, in order


class TestRescheduleForward:
    def test_reslots_in_order_onto_upcoming_slots(self):
        from cqc_lem.app import run_scheduler as rs
        editions = [{"id": 3, "scheduled_for": datetime(2026, 7, 21, 13, 0)},
                    {"id": 4, "scheduled_for": datetime(2026, 7, 28, 13, 0)}]
        slots = [datetime(2026, 7, 30, 13, 0), datetime(2026, 8, 6, 13, 0)]
        with patch(f"{_DB}.get_newsletter_settings",
                   return_value={"publish_day": 1, "publish_hour": 9, "cadence": "weekly"}), \
             patch(f"{_DB}.get_user_timezone", return_value="America/New_York"), \
             patch(f"{_NL}.upcoming_publish_slots", return_value=slots), \
             patch(f"{_DB}.update_newsletter_edition", return_value=True) as upd:
            moved = rs._reschedule_pending_editions_forward(1, editions, datetime(2026, 7, 23, 3, 0))
        assert moved == 2
        moves = {c.args[0]: c.kwargs["scheduled_for"] for c in upd.call_args_list}
        assert moves == {3: slots[0], 4: slots[1]}          # order preserved

    def test_skips_editions_already_on_their_slot(self):
        from cqc_lem.app import run_scheduler as rs
        editions = [{"id": 3, "scheduled_for": datetime(2026, 7, 30, 13, 0)}]  # already == slot
        slots = [datetime(2026, 7, 30, 13, 0)]
        with patch(f"{_DB}.get_newsletter_settings",
                   return_value={"publish_day": 1, "publish_hour": 9, "cadence": "weekly"}), \
             patch(f"{_DB}.get_user_timezone", return_value="America/New_York"), \
             patch(f"{_NL}.upcoming_publish_slots", return_value=slots), \
             patch(f"{_DB}.update_newsletter_edition", return_value=True) as upd:
            moved = rs._reschedule_pending_editions_forward(1, editions, datetime(2026, 7, 23, 3, 0))
        assert moved == 0
        upd.assert_not_called()

    def test_empty_is_noop(self):
        from cqc_lem.app import run_scheduler as rs
        assert rs._reschedule_pending_editions_forward(1, [], datetime(2026, 7, 23, 3, 0)) == 0


class TestAutoPublishScheduledEditions:
    def test_dispatches_at_most_one_per_user(self):
        from cqc_lem.app.run_scheduler import auto_publish_scheduled_editions
        due = [{"id": 2, "user_id": 1}, {"id": 3, "user_id": 1}, {"id": 9, "user_id": 2}]
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch("cqc_lem.app.run_automation.auto_publish_edition", MagicMock()), \
             patch(f"{_DB}.get_editions_due_to_publish", return_value=due), \
             patch(f"{_RS}._publish_next_due_edition_for_user", return_value=1) as pnext:
            res = auto_publish_scheduled_editions.run()
        assert pnext.call_count == 2                          # once per distinct user
        assert sorted(c.args[0] for c in pnext.call_args_list) == [1, 2]
        assert "2 newsletter edition(s)" in res

    def test_throttled_publishes_nothing(self):
        from cqc_lem.app.run_scheduler import auto_publish_scheduled_editions
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_RS}._publish_next_due_edition_for_user") as pnext:
            res = auto_publish_scheduled_editions.run()
        assert res == "Automation throttled"
        pnext.assert_not_called()
