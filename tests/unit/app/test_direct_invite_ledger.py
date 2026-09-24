"""Every invite rail writes a `connection_requests` row (issue #2101).

A confirmed invite on 2026-09-22 had no ledger row: the direct rails (`invite_to_connect`, used by
the profile viewer on auto-send and the outreach funnel's connect stage, and the roster ladder)
sent without ever filing one, so the invite was invisible to the Connections view and to the
sourcing scan's dedup.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"
_OUT = "cqc_lem.app.engagement.outreach"
_URL = "https://www.linkedin.com/in/jane-doe"


class TestInviteToConnectLedger:
    def _invite(self, sent: bool, source=None, insert_id=9):
        from cqc_lem.app.engagement import invites
        with patch(f"{_INV}.invite_to_connect_now", return_value=(sent, "why")), \
             patch(f"{_INV}.insert_connection_request", return_value=insert_id) as insert, \
             patch(f"{_INV}.log_warning") as warn:
            out = invites.invite_to_connect.run(user_id=1, profile_url=_URL, message="Hi Jane",
                                                source=source)
        return out, insert, warn

    def test_a_sent_profile_viewer_invite_writes_a_sent_row(self):
        from cqc_lem.utilities.db import (
            CONNECTION_REQUEST_SENT_MESSAGE,
            CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER,
            ConnectionRequestStatus,
        )
        out, insert, warn = self._invite(True, source=CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER)
        assert out == CONNECTION_REQUEST_SENT_MESSAGE
        insert.assert_called_once_with(1, _URL, message="Hi Jane",
                                       status=ConnectionRequestStatus.SENT,
                                       source=CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER)
        warn.assert_not_called()

    def test_an_unsent_invite_writes_nothing(self):
        _, insert, _ = self._invite(False, source="profile_viewer")
        insert.assert_not_called()

    def test_a_message_queued_before_source_existed_still_records(self):
        from cqc_lem.app.engagement import invites
        with patch(f"{_INV}.invite_to_connect_now", return_value=(True, "ok")), \
             patch(f"{_INV}.insert_connection_request", return_value=9) as insert:
            invites.invite_to_connect.run(user_id=1, profile_url=_URL)
        assert insert.call_args.kwargs["source"] is None

    def test_a_failed_ledger_insert_warns_but_the_send_still_reports_sent(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        out, _, warn = self._invite(True, source="profile_viewer", insert_id=None)
        assert out == CONNECTION_REQUEST_SENT_MESSAGE
        warn.assert_called_once()


class TestRosterInviteLedger:
    def test_a_sent_roster_invite_writes_a_sent_row(self):
        from cqc_lem.app.engagement import invites
        from cqc_lem.utilities.connection_targeting import SOURCE_ROSTER
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE, ConnectionRequestStatus
        with patch(f"{_INV}.invite_to_connect_now",
                   return_value=(True, CONNECTION_REQUEST_SENT_MESSAGE)), \
             patch(f"{_INV}.set_target_connect_status"), \
             patch(f"{_INV}.insert_connection_request", return_value=9) as insert:
            invites.send_roster_connect_invite.run(user_id=1, profile_url=_URL, message="note")
        insert.assert_called_once_with(1, _URL, message="note",
                                       status=ConnectionRequestStatus.SENT, source=SOURCE_ROSTER)


class TestDispatchersNameTheSource:
    def test_the_profile_viewer_auto_send_branch_passes_its_source(self):
        from cqc_lem.app.engagement import outreach
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER
        my_profile = MagicMock(full_name="Chris Queen", email="chris@example.com")
        profile_data = {"full_name": "Jane Doe", "connection": "2nd", "profile_url": _URL,
                        "recent_activities": []}
        with patch(f"{_OUT}.has_engaged_url_with_x_days", return_value=False), \
             patch(f"{_OUT}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "chris@example.com", my_profile)), \
             patch(f"{_OUT}.get_linkedin_profile_from_url", return_value=profile_data), \
             patch(f"{_OUT}.get_engagement_preferences",
                   return_value={"profile_viewer_dm_auto_send": True}), \
             patch(f"{_OUT}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_OUT}.generate_and_post_comment", return_value=True), \
             patch(f"{_OUT}.summarize_recent_activity", return_value="they shipped a thing"), \
             patch(f"{_OUT}.get_ai_message_refinement", return_value="Hi Jane"), \
             patch(f"{_OUT}.get_user_id", return_value=1), \
             patch(f"{_OUT}.invite_to_connect") as invite, \
             patch(f"{_OUT}.insert_new_log"), patch(f"{_OUT}.quit_gracefully"):
            outreach.engage_with_profile_viewer.run(user_id=1, viewer_url=_URL,
                                                    viewer_name="Jane Doe")
        kwargs = invite.apply_async.call_args.kwargs["kwargs"]
        assert kwargs["source"] == CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER
