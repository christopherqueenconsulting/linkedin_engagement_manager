"""The weekly group post's publish slot and the undo window on a skip (issue #1415).

"Skip this week" used to be a one-way door: the reporter hit it by accident and had no way back and
no way to ask for another post. The undo is bounded by the slot the draft was written for, so the
boundary itself has to be exact — a week either side of it is a post that ships when the user
thought it would not, or a recovery they should have had and did not.
"""

from datetime import datetime, timezone

import pytest

from cqc_lem.utilities.group_post_slot import (
    group_skip_undo_open,
    next_group_publish_slot,
    skip_undo_deadline,
)

pytestmark = pytest.mark.unit

# Sunday 15:00 UTC — when the draft beat writes the week's post.
_DRAFTED = "2026-08-09T15:00:00"
# The Tuesday 15:00 UTC slot that draft was written for.
_SLOT = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


class TestNextGroupPublishSlot:
    """Mirrors ui/src/utils/groupPostSlot.ts — the SPA and the API must agree on the instant."""

    @pytest.mark.parametrize("from_iso,expected", [
        # Same Tuesday, before the beat fires.
        ("2026-08-11T10:00:00", datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)),
        # Sunday's draft beat looks two days ahead.
        (_DRAFTED, _SLOT),
        # Wednesday, the day after the slot: next week's.
        ("2026-08-12T09:00:00", datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)),
    ])
    def test_picks_the_next_tuesday_1500_utc(self, from_iso, expected):
        assert next_group_publish_slot(datetime.fromisoformat(from_iso)) == expected

    def test_the_slot_instant_itself_has_passed(self):
        """15:00:00 exactly is when the beat runs, so that week is already spent."""
        assert next_group_publish_slot(_SLOT) == datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)

    def test_an_aware_timestamp_is_converted_not_assumed(self):
        # 12:00 US/Eastern on the Tuesday is 16:00 UTC — past that day's slot.
        from datetime import timedelta
        eastern_noon = datetime(2026, 8, 11, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
        assert next_group_publish_slot(eastern_noon) == datetime(2026, 8, 18, 15, 0,
                                                                 tzinfo=timezone.utc)


class TestSkipUndoDeadline:
    def test_is_the_slot_the_draft_was_written_for(self):
        assert skip_undo_deadline({"created_at": _DRAFTED}) == _SLOT

    def test_reads_a_datetime_row_as_well_as_an_iso_string(self):
        assert skip_undo_deadline({"created_at": datetime(2026, 8, 9, 15, 0)}) == _SLOT

    @pytest.mark.parametrize("created", [None, "", "not a date", 17])
    def test_unreadable_creation_time_has_no_deadline(self, created):
        assert skip_undo_deadline({"created_at": created}) is None


class TestGroupSkipUndoOpen:
    def test_open_before_the_slot(self):
        assert group_skip_undo_open({"created_at": _DRAFTED},
                                    now=datetime(2026, 8, 10, 9, 0)) is True

    def test_closed_once_the_publish_beat_has_run(self):
        assert group_skip_undo_open({"created_at": _DRAFTED},
                                    now=datetime(2026, 8, 11, 15, 0)) is False

    def test_still_closed_days_later(self):
        assert group_skip_undo_open({"created_at": _DRAFTED},
                                    now=datetime(2026, 8, 14, 12, 0)) is False

    def test_an_unreadable_creation_time_fails_open(self):
        """The bug being fixed is a user stuck with an accidental skip.

        A restore is an explicit action that publishes at the NEXT slot, so treating an unreadable
        row as undoable costs a post the user asked for — the opposite failure loses the recovery
        entirely.
        """
        assert group_skip_undo_open({"created_at": None}) is True
