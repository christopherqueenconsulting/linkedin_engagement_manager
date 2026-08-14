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

    def test_a_carried_forward_draft_is_measured_from_its_last_write(self):
        """A carried-forward draft is measured from its last write, not its creation.

        The publish beat carries an unpublished draft forward — no session, unreadable group
        switches, no Chrome slot — so its FIRST slot is long past while the row is still live.

        Anchoring on `created_at` alone would make a skip on one of those irreversible the moment it
        was made, which is the bug this window exists to fix.
        """
        carried = {"created_at": "2026-07-12T15:00:00", "updated_at": "2026-08-13T09:00:00"}
        assert skip_undo_deadline(carried) == datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)

    def test_an_unreadable_update_time_falls_back_to_creation(self):
        assert skip_undo_deadline({"created_at": _DRAFTED, "updated_at": None}) == _SLOT

    def test_an_older_update_never_pulls_the_deadline_in(self):
        assert skip_undo_deadline(
            {"created_at": _DRAFTED, "updated_at": "2026-07-12T15:00:00"}) == _SLOT

    @pytest.mark.parametrize("created", [None, "", "not a date", 17])
    def test_unreadable_timestamps_have_no_deadline(self, created):
        assert skip_undo_deadline({"created_at": created, "updated_at": created}) is None


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

    def test_a_skip_on_a_carried_forward_draft_is_undoable(self):
        """A draft the publish beat could not ship stays live with an old `created_at`.

        The user pressing Skip on it must get the same undo window as anyone else — otherwise the
        control they just used is irreversible on the exact rows the lane holds longest.
        """
        carried = {"created_at": "2026-07-12T15:00:00", "updated_at": "2026-08-13T09:00:00"}
        assert group_skip_undo_open(carried, now=datetime(2026, 8, 14, 12, 0)) is True

    def test_an_unreadable_creation_time_fails_open(self):
        """The bug being fixed is a user stuck with an accidental skip.

        A restore is an explicit action that publishes at the NEXT slot, so treating an unreadable
        row as undoable costs a post the user asked for — the opposite failure loses the recovery
        entirely.
        """
        assert group_skip_undo_open({"created_at": None}) is True
