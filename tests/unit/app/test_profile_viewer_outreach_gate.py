"""`profile_viewer_dm_auto_send` gates cold profile-viewer outreach (issue #1137).

`engage_with_profile_viewer` is the one outreach lane that dispatched genuinely COLD contact with no
per-user control: a viewer we could not comment on got a templated DM sent immediately, and a
non-1st-degree viewer got a personalised connection request sent immediately. Both branches are now
approval-gated behind ONE preference, because a single visit resolves to exactly one of them.

What these tests pin:

* toggle OFF (the default) files a PENDING row and dispatches NOTHING, on both branches;
* toggle ON reproduces the pre-#1137 dispatch exactly, on both branches;
* an unreadable/absent preference row is treated as OFF — the gate fails CLOSED;
* the follow-up ladder moves to the moment the DM LANDS, so gating the lane does not silently
  delete its follow-ups;
* the roster connect escalation (T6) is untouched — the issue's explicit out-of-scope.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"
_REPO_ROOT = Path(__file__).resolve().parents[3]


_UNSET = object()


def _engage(connection: str, *, auto_send=False, prefs_row=_UNSET, activities=(),
            commented=False, message="Hi Jane", open_draft=False, requested_keys=(),
            dm_id=7, request_id=9, max_invites=10, invites_sent=0, open_requests=0):
    """Run ONE profile-viewer engagement and hand back every dispatch/queue mock.

    `prefs_row` replaces the whole preference dict (so a test can model a read that came back
    empty); left alone, the row carries the toggle plus the shared invite cap the queue path is
    bounded by.
    """
    row = ({"profile_viewer_dm_auto_send": auto_send, "max_invites_per_day": max_invites}
           if prefs_row is _UNSET else prefs_row)
    profile_data = {"full_name": "Jane Doe", "connection": connection,
                    "profile_url": "https://www.linkedin.com/in/jane-doe",
                    "recent_activities": list(activities)}
    my_profile = MagicMock()
    my_profile.full_name = "Chris Queen"
    my_profile.email = "chris@example.com"

    # An ExitStack, not stacked `with`s: CPython caps a function at 20 statically nested blocks and
    # this lane touches more collaborators than that.
    patches = {
        "has_engaged_url_with_x_days": False,
        "get_current_profile": (MagicMock(), MagicMock(), "chris@example.com", my_profile),
        "get_linkedin_profile_from_url": profile_data,
        "get_engagement_preferences": row,
        "get_or_create_profile_synthesis": "voice",
        "generate_and_post_comment": commented,
        "get_dm_history_for_profile": [],
        "get_user_blog_url": None,
        "build_dm_from_template": message,
        "ai_check_message_history": message,
        "summarize_recent_activity": "they shipped a thing",
        "get_ai_message_refinement": "Hi Jane",
        "has_open_scheduled_dm": open_draft,
        "get_requested_person_keys": set(requested_keys),
        "insert_scheduled_dm": dm_id,
        "insert_connection_request": request_id,
        "get_user_id": 1,
        "count_invites_sent_today": invites_sent,
        "count_open_connection_requests": open_requests,
    }
    with ExitStack() as stack:
        mocks = {name: stack.enter_context(patch(f"{_OUT}.{name}", return_value=value))
                 for name, value in patches.items()}
        for name in ("enqueue_next_followup", "send_private_dm", "invite_to_connect",
                     "insert_new_log", "quit_gracefully"):
            mocks[name] = stack.enter_context(patch(f"{_OUT}.{name}"))

        from cqc_lem.app.engagement.outreach import engage_with_profile_viewer

        result = engage_with_profile_viewer.run(user_id=1,
                                                viewer_url="https://www.linkedin.com/in/jane-doe",
                                                viewer_name="Jane Doe")
    sched, conn = mocks["insert_scheduled_dm"], mocks["insert_connection_request"]
    dm, invite = mocks["send_private_dm"], mocks["invite_to_connect"]
    followup, logged = mocks["enqueue_next_followup"], mocks["insert_new_log"]
    return result, {"sched": sched, "conn": conn, "dm": dm, "invite": invite,
                    "followup": followup, "log": logged}


def _log_result(mocks):
    from cqc_lem.utilities.db import LogResultType

    return mocks["log"].call_args.kwargs["result"] is LogResultType.SUCCESS


class TestTheDefaultGatesTheDmBranch:
    def test_a_first_degree_viewer_we_cannot_comment_on_gets_a_pending_draft(self):
        from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_PROFILE_VIEWER, ScheduledDmStatus

        result, mocks = _engage("1st")

        mocks["dm"].apply_async.assert_not_called()
        mocks["sched"].assert_called_once()
        assert mocks["sched"].call_args.kwargs["status"] is ScheduledDmStatus.PENDING
        assert mocks["sched"].call_args.kwargs["source"] == SCHEDULED_DM_SOURCE_PROFILE_VIEWER
        assert mocks["sched"].call_args.args[1] == "https://www.linkedin.com/in/jane-doe"
        assert "Queued a DM" in result
        assert _log_result(mocks)

    def test_the_ladder_does_not_start_on_a_draft_nobody_has_approved(self):
        _, mocks = _engage("1st")

        mocks["followup"].assert_not_called()

    def test_a_viewer_we_could_comment_on_is_never_dm_ed_at_all(self):
        """The comment branch wins as it always did — the gate only reaches the DM fallback."""
        activity = {"text": "shipped",
                    "link": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "posted": (datetime.now() - timedelta(days=2)).isoformat()}

        _, mocks = _engage("1st", activities=[activity], commented=True)

        mocks["sched"].assert_not_called()
        mocks["dm"].apply_async.assert_not_called()


class TestTheDefaultGatesTheConnectBranch:
    def test_a_non_first_degree_viewer_gets_a_pending_connection_request(self):
        from cqc_lem.utilities.db import (
            CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER,
            ConnectionRequestStatus,
        )

        result, mocks = _engage("2nd")

        mocks["invite"].apply_async.assert_not_called()
        mocks["conn"].assert_called_once()
        assert mocks["conn"].call_args.kwargs["status"] is ConnectionRequestStatus.PENDING
        assert mocks["conn"].call_args.kwargs["source"] == CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER
        assert mocks["conn"].call_args.kwargs["message"] == "Hi Jane"
        assert "Queued a connection request" in result
        assert _log_result(mocks)

    def test_the_row_reuses_the_398_table_not_a_new_one(self):
        """#1137 explicitly reuses `connection_requests` + its existing beat and review UI."""
        from cqc_lem.app.engagement import outreach as ra

        assert ra.insert_connection_request.__module__.endswith("repositories.outreach")


class TestTheToggleRestoresTodaysBehaviour:
    def test_dm_branch_dispatches_and_starts_the_ladder(self):
        result, mocks = _engage("1st", auto_send=True)

        mocks["sched"].assert_not_called()
        mocks["dm"].apply_async.assert_called_once()
        assert mocks["dm"].apply_async.call_args.kwargs["kwargs"]["profile_url"] == \
            "https://www.linkedin.com/in/jane-doe"
        mocks["followup"].assert_called_once_with(1, "https://www.linkedin.com/in/jane-doe",
                                                  "Jane", "profile_viewer", 0)
        assert "Sent DM to Jane Doe" in result

    def test_connect_branch_dispatches_the_invite(self):
        result, mocks = _engage("2nd", auto_send=True)

        mocks["conn"].assert_not_called()
        mocks["invite"].apply_async.assert_called_once()
        assert mocks["invite"].apply_async.call_args.kwargs["kwargs"]["message"] == "Hi Jane"
        assert "Sent Connection Request to Jane Doe" in result


class TestTheGateFailsClosed:
    @pytest.mark.parametrize("row", [None, {}, {"profile_viewer_dm_auto_send": None}])
    def test_an_unreadable_or_absent_preference_gates(self, row):
        """`get_engagement_preferences` fails soft to an empty answer — that must read as OFF.

        Defaulting the other way would send cold outreach unattended on exactly the runs where we
        could not read whether the user wanted it.
        """
        _, mocks = _engage("2nd", prefs_row=row)

        mocks["invite"].apply_async.assert_not_called()

    @pytest.mark.parametrize("row", [None, {}, {"profile_viewer_dm_auto_send": None}])
    def test_an_unreadable_preference_drafts_the_dm_branch_instead_of_sending(self, row):
        """Same read, the other branch: gated means DRAFTED, not silently dropped.

        The DM queue carries no invite budget, so an unreadable row still files the draft — it is
        only the invite half that a cap-less answer holds back.
        """
        _, mocks = _engage("1st", prefs_row=row)

        mocks["dm"].apply_async.assert_not_called()
        mocks["sched"].assert_called_once()


class TestDedupBeforeQueueing:
    def test_an_open_draft_on_the_thread_queues_nothing(self):
        """One open draft per conversation, shared with #485 nurture and #624 artifact."""
        result, mocks = _engage("1st", open_draft=True)

        mocks["sched"].assert_not_called()
        mocks["dm"].apply_async.assert_not_called()
        assert "Did not queue a DM" in result
        assert not _log_result(mocks)

    def test_the_open_draft_check_covers_all_three_drafting_sources(self):
        from cqc_lem.app.engagement import outreach as ra
        from cqc_lem.utilities.db import (
            SCHEDULED_DM_SOURCE_ARTIFACT,
            SCHEDULED_DM_SOURCE_NURTURE,
            SCHEDULED_DM_SOURCE_PROFILE_VIEWER,
        )

        with patch(f"{_OUT}.has_open_scheduled_dm", return_value=False) as check, \
             patch(f"{_OUT}.insert_scheduled_dm", return_value=1):
            ra._queue_profile_viewer_dm(1, "https://x/in/a", "hi", "Ada", "Ada L")

        assert {c.kwargs["source"] for c in check.call_args_list} == {
            SCHEDULED_DM_SOURCE_PROFILE_VIEWER, SCHEDULED_DM_SOURCE_NURTURE,
            SCHEDULED_DM_SOURCE_ARTIFACT}

    def test_someone_already_requested_is_never_re_filed(self):
        """The viewer list repeats people; one invite per person, ever, is the existing #398 rule."""
        from cqc_lem.utilities.lead_scoring import person_key

        key = person_key("Jane Doe", "https://www.linkedin.com/in/jane-doe")
        result, mocks = _engage("2nd", requested_keys=[key])

        mocks["conn"].assert_not_called()
        mocks["invite"].apply_async.assert_not_called()
        assert "Did not queue a connection request" in result
        assert not _log_result(mocks)


class TestTheQueuedInviteBacklogStaysInsideTheSharedCap:
    """A PENDING `connection_requests` row is counted as SPENT invite budget by two other lanes.

    `count_open_connection_requests` never ages a row out, so filing without checking the cap would
    let an unapproved viewer backlog park at cap-many rows and hold `_connect_target_budget` (#486
    sourcing) and `roster_connect_budget` (#979) at zero for good. Direct dispatch never did that —
    a sent invite counts only for the day it was sent.
    """

    def test_a_spent_budget_queues_nothing(self):
        result, mocks = _engage("2nd", max_invites=10, invites_sent=4, open_requests=6)

        mocks["conn"].assert_not_called()
        mocks["invite"].apply_async.assert_not_called()
        assert "Did not queue a connection request" in result

    def test_an_account_that_sends_no_invites_queues_nothing(self):
        _, mocks = _engage("2nd", max_invites=0)

        mocks["conn"].assert_not_called()

    def test_remaining_budget_still_queues(self):
        _, mocks = _engage("2nd", max_invites=10, invites_sent=4, open_requests=5)

        mocks["conn"].assert_called_once()

    def test_the_cap_never_holds_back_the_dm_branch(self):
        """The DM half spends `max_dms_per_day` at SEND time, not the invite cap."""
        _, mocks = _engage("1st", max_invites=0)

        mocks["sched"].assert_called_once()

    def test_direct_dispatch_is_not_bounded_by_the_queue_depth(self):
        """Toggle ON is the pre-#1137 path exactly — `invite_to_connect` carries its own caps."""
        _, mocks = _engage("2nd", auto_send=True, max_invites=10, open_requests=99)

        mocks["invite"].apply_async.assert_called_once()


class TestAFailedInsertIsNotASuccess:
    def test_a_dm_insert_that_returned_nothing_records_failure(self):
        result, mocks = _engage("1st", dm_id=None)

        assert "Did not queue a DM" in result
        assert not _log_result(mocks)

    def test_a_connection_insert_that_returned_nothing_records_failure(self):
        result, mocks = _engage("2nd", request_id=None)

        assert "Did not queue a connection request" in result
        assert not _log_result(mocks)


class TestTheLadderMovesToTheSend:
    """Gating the lane must not silently delete its follow-ups (and the reply check behind them)."""

    def _send(self, source, sent=True):
        from cqc_lem.utilities.db import ScheduledDmStatus

        dm = {"id": 3, "user_id": 1, "status": ScheduledDmStatus.APPROVED,
              "recipient_profile_url": "https://www.linkedin.com/in/jane-doe",
              "recipient_name": "Jane", "message": "Hi Jane", "source": source}
        with patch(f"{_OUT}.get_engagement_preferences", return_value={"max_dms_per_day": 10}), \
             patch(f"{_OUT}.engagement_caps_from_prefs", return_value={}), \
             patch(f"{_OUT}.remaining_actions", return_value=5), \
             patch("cqc_lem.utilities.db.get_scheduled_dm", return_value=dm), \
             patch("cqc_lem.utilities.db.update_scheduled_dm_status"), \
             patch("cqc_lem.utilities.db.count_dms_sent_today", return_value=0), \
             patch(f"{_OUT}.send_dm_now", return_value=sent), \
             patch(f"{_OUT}.enqueue_next_followup") as followup:
            from cqc_lem.app.engagement.outreach import send_scheduled_dm

            send_scheduled_dm.run(dm_id=3)
        return followup

    def test_a_landed_profile_viewer_dm_starts_the_ladder(self):
        from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_PROFILE_VIEWER

        followup = self._send(SCHEDULED_DM_SOURCE_PROFILE_VIEWER)

        followup.assert_called_once_with(1, "https://www.linkedin.com/in/jane-doe", "Jane",
                                         "profile_viewer", 0)

    def test_a_dm_that_never_landed_starts_nothing(self):
        from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_PROFILE_VIEWER

        assert not self._send(SCHEDULED_DM_SOURCE_PROFILE_VIEWER, sent=False).called

    def test_another_source_keeps_its_own_ladder(self):
        """Nurture schedules its own re-check; artifact deliveries end at the delivery."""
        from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_NURTURE

        assert not self._send(SCHEDULED_DM_SOURCE_NURTURE).called


class TestTheStoredColumn:
    def test_the_preference_defaults_to_off_and_is_a_stored_boolean_column(self):
        from cqc_lem.utilities import db

        assert db._ENGAGEMENT_DEFAULTS["profile_viewer_dm_auto_send"] is False
        assert "profile_viewer_dm_auto_send" in db._ENGAGEMENT_COLS
        assert "profile_viewer_dm_auto_send" in db._ENGAGEMENT_BOOL_FIELDS

    def test_the_migration_adds_it_additively_and_defaults_it_off(self):
        sql = (_REPO_ROOT / "compose/local/database/migrations"
               / "V20260816224626__add_profile_viewer_dm_auto_send.sql").read_text().lower()

        assert "alter table engagement_preferences" in sql
        assert "add column profile_viewer_dm_auto_send tinyint(1) not null default 0" in sql
        assert "drop" not in sql

    def test_the_put_body_carries_it_so_a_save_cannot_reset_it(self):
        from cqc_lem.api.routers.user import EngagementPreferencesRequest

        field = EngagementPreferencesRequest.model_fields["profile_viewer_dm_auto_send"]
        assert field.default is False

    @pytest.mark.parametrize("name", ["SCHEDULED_DM_SOURCE_PROFILE_VIEWER",
                                      "CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER"])
    def test_both_source_values_are_declared_exports_of_the_facade(self, name):
        """`db.__all__` is the one declaration that says these are read from another module.

        Both are used only in `app/engagement/outreach.py`; without the export CodeQL reads them as
        dead globals and the PR gate blocks on `py/unused-global-variable`, which is the same reason
        the names surrounding them in the list are there.
        """
        from cqc_lem.utilities import db

        assert db.__all__.count(name) == 1
        assert getattr(db, name) == "profile_viewer"


class TestRosterConnectEscalationIsUntouched:
    """The issue's round-2 revert: `roster_auto_connect=false` already IS the human in the loop.

    T6 must not learn about this toggle — if it ever reads it, two settings govern one lane and the
    escalation ladder's one-shot accounting stops being decidable from `connect_status` alone.
    """

    @pytest.mark.parametrize("name", ["queue_roster_connect_invite", "advance_roster_connect",
                                      "roster_connect_budget"])
    def test_the_toggle_is_invisible_to_the_roster_connect_rung(self, name):
        import inspect

        from cqc_lem.app.engagement import feed

        source = inspect.getsource(getattr(feed, name))
        assert "profile_viewer_dm_auto_send" not in source
