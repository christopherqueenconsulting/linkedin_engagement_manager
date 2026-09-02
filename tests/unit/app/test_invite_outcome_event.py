"""`invite_outcome` makes the proactive connect lane readable from outside (issue #1813, part A5).

`connection_requests` held 59 rows and ZERO had ever reached 'sent'. Every Celery run reported
SUCCESS — `Task ...send_connection_request[2767fcda] succeeded in 88.0s: 'Connection request 16 ->
failed'` — and every early exit logged at DEBUG, which prod does not keep. From outside, a lane that
had never delivered an invite in its life was indistinguishable from a quiet week. It stayed that
way for nineteen days.

`track_stale_invite_run`'s docstring names this exact failure mode, so this event is modelled on it,
including the part that matters most: it fires on EVERY outcome. A series carrying only sends would
reproduce the bug it exists to catch — a lane that stops sending would simply stop emitting, and
silence is what nobody notices.
"""

from unittest.mock import patch

import pytest

from cqc_lem.app.engagement.invites import INVITE_OUTCOME_CHALLENGE
from cqc_lem.utilities.db import (
    ACCOUNT_RESTRICTED_MESSAGE,
    ALREADY_CONNECTED_MESSAGE,
    CONNECTION_REQUEST_SENT_MESSAGE,
    FOLLOW_ONLY_MESSAGE,
    INVITE_LIMIT_REACHED_MESSAGE,
    INVITE_NOT_SENT_MESSAGE,
    NO_CONNECT_BUTTON_MESSAGE,
)
from cqc_lem.utilities.linkedin.rate_limit import LinkedInChallengeUnsolved
from cqc_lem.utilities.observability import EVENTS

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"
_PROFILE = "https://www.linkedin.com/in/jane/"


def _req(attempts=0, status="approved"):
    return {"id": 3, "user_id": 1, "recipient_profile_url": _PROFILE, "message": "hi",
            "status": status, "attempts": attempts}


def _run(*, req=None, send=None, held=False, sent_today=0, attempt=(False, 1)):
    """Drive `send_connection_request` once and hand back the emitted outcome kwargs."""
    from cqc_lem.app.engagement import invites

    send = send if send is not None else (False, NO_CONNECT_BUTTON_MESSAGE)
    kwargs = {"side_effect": send} if isinstance(send, Exception) else {"return_value": send}
    with patch("cqc_lem.utilities.db.get_connection_request", return_value=req or _req()), \
         patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=sent_today), \
         patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
         patch(f"{_INV}.is_invites_held", return_value=held), \
         patch(f"{_INV}.invite_hold_reason", return_value="LinkedIn walled the account"), \
         patch(f"{_INV}.invite_to_connect_now", **kwargs), \
         patch("cqc_lem.utilities.db.update_connection_request_status"), \
         patch("cqc_lem.utilities.db.record_connection_request_attempt", return_value=attempt), \
         patch(f"{_INV}.log_warning"), \
         patch(f"{_INV}.track_invite_outcome") as track:
        invites.send_connection_request(3)
    assert track.call_count == 1, "exactly one outcome per dispatch, always"
    return track.call_args[0]


class TestEveryOutcomeIsEmitted:
    """Including the ones that do nothing. A lane that stops sending must not stop emitting."""

    def test_a_sent_invite_reports_sent_with_a_non_zero_denominator(self):
        user_id, result, reason, attempts = _run(send=(True, CONNECTION_REQUEST_SENT_MESSAGE))
        assert (user_id, result, reason) == (1, "sent", "sent")
        assert attempts == 1

    def test_a_retired_target_reports_failed_and_why(self):
        user_id, result, reason, attempts = _run(send=(False, FOLLOW_ONLY_MESSAGE),
                                                 attempt=(True, 1))
        assert (user_id, result, reason, attempts) == (1, "failed", "follow_only", 1)

    def test_a_miss_below_the_ceiling_is_deferred_not_failed(self):
        """A deferred row keeps its turn, so it is not a failure.

        Counting it as one would make a healthy lane running into its own cap look identical to
        one LinkedIn has walled.
        """
        _, result, reason, attempts = _run(send=(False, NO_CONNECT_BUTTON_MESSAGE),
                                           attempt=(False, 2))
        assert (result, reason, attempts) == ("deferred", "no_connect_affordance", 2)

    def test_a_miss_at_the_ceiling_is_failed(self):
        _, result, reason, attempts = _run(send=(False, NO_CONNECT_BUTTON_MESSAGE),
                                           attempt=(True, 3))
        assert (result, reason, attempts) == ("failed", "no_connect_affordance", 3)

    def test_the_invite_hold_emits_with_a_zero_denominator(self):
        """Nothing reached LinkedIn, so `attempts` stays put.

        That is the reading that separates a lane FAILING from a lane never running at all.
        """
        _, result, reason, attempts = _run(held=True)
        assert (result, reason, attempts) == ("deferred", "invites_held", 0)

    def test_the_daily_cap_emits(self):
        _, result, reason, attempts = _run(sent_today=10)
        assert (result, reason, attempts) == ("deferred", "daily_cap", 0)

    def test_the_429_breaker_emits(self):
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        _, result, reason, attempts = _run(send=LinkedInRateLimited("throttled"))
        assert (result, reason, attempts) == ("deferred", "throttled", 0)

    def test_an_unsolvable_checkpoint_defers_with_its_own_word(self):
        """It charges no attempt but gets its own wall word, not buried with throttles or `error`."""
        _, result, reason, attempts = _run(send=LinkedInChallengeUnsolved("checkpoint"))
        assert (result, reason, attempts) == ("deferred", INVITE_OUTCOME_CHALLENGE, 0)

    def test_a_row_that_already_had_attempts_carries_them_forward_on_a_defer(self):
        _, _, _, attempts = _run(req=_req(attempts=2), held=True)
        assert attempts == 2

    def test_a_failed_write_still_reports_the_dispatch_that_happened(self):
        """A failed write must not report a zero denominator.

        `record_connection_request_attempt` answers 0 when the row was gone or the write itself
        failed, and a dispatch that DID happen reading as zero is the blind spot this closes.
        """
        _, result, _, attempts = _run(req=_req(attempts=1), attempt=(False, 0))
        assert (result, attempts) == ("deferred", 2)


class TestTheReasonVocabulary:
    """`reason` is a `label()`, so PostHog matches it on the exact ingested string.

    The MESSAGE constants are prose written for a human reading a failed row in the Connections
    table. Ingesting those directly would tie every dashboard tile to their exact wording — one
    clarity edit and the breakdown silently empties. The map is the seam.
    """

    @pytest.mark.parametrize("message,word", [
        (CONNECTION_REQUEST_SENT_MESSAGE, "sent"),
        (ALREADY_CONNECTED_MESSAGE, "already_connected"),
        (NO_CONNECT_BUTTON_MESSAGE, "no_connect_affordance"),
        (FOLLOW_ONLY_MESSAGE, "follow_only"),
        (INVITE_NOT_SENT_MESSAGE, "send_failed"),
        (INVITE_LIMIT_REACHED_MESSAGE, "invite_limit"),
        (ACCOUNT_RESTRICTED_MESSAGE, "account_restricted"),
    ])
    def test_every_message_the_send_path_returns_has_its_own_word(self, message, word):
        from cqc_lem.app.engagement.invites import _invite_outcome_reason
        assert _invite_outcome_reason(message) == word

    def test_the_words_are_distinct_so_a_breakdown_can_separate_the_causes(self):
        """Three states used to write the same log line; they must not collapse again here."""
        from cqc_lem.app.engagement.invites import _INVITE_OUTCOME_REASONS
        words = list(_INVITE_OUTCOME_REASONS.values())
        assert len(set(words)) == len(words)

    def test_an_exception_string_falls_back_rather_than_ingesting_a_stack_message(self):
        """An unmapped reason falls into one bucket rather than its own.

        `invite_to_connect_now` formats unexpected failures into the reason, and ingesting those
        would shred the breakdown into one bucket per exception text.
        """
        from cqc_lem.app.engagement.invites import _invite_outcome_reason
        assert _invite_outcome_reason("Error while inviting to connect: boom") == "error"
        assert _invite_outcome_reason(None) == "error"


class TestTheEventShape:

    def test_the_registry_declares_the_shape_the_dashboards_filter_on(self):
        spec = EVENTS["invite_outcome"]
        by_name = {field.name: field for field in spec.fields}
        assert set(by_name) == {"user_id", "result", "reason", "attempts"}
        # result and reason are what a tile filters and breaks down on, so both must be forced to
        # a string on the way out — PostHog matches on the INGESTED type (docs/kpi-dashboards.md).
        assert by_name["result"].filtered and by_name["reason"].filtered

    def test_a_non_string_result_still_lands_as_a_string(self):
        from cqc_lem.utilities.observability import track_invite_outcome
        with patch("cqc_lem.utilities.observability.posthog") as posthog:
            track_invite_outcome(7, True, None, "2")
        props = posthog.capture.call_args.kwargs["properties"]
        assert props["result"] == "True"
        assert props["attempts"] == 2

    def test_a_missing_attempt_count_is_zero_rather_than_absent(self):
        """`count()` — for this field, "not reported" and "none happened" really are the same."""
        from cqc_lem.utilities.observability import track_invite_outcome
        with patch("cqc_lem.utilities.observability.posthog") as posthog:
            track_invite_outcome(7, "deferred", "daily_cap")
        assert posthog.capture.call_args.kwargs["properties"]["attempts"] == 0

    def test_the_event_lands_on_the_user_it_is_about(self):
        from cqc_lem.utilities.observability import track_invite_outcome
        with patch("cqc_lem.utilities.observability.posthog") as posthog:
            track_invite_outcome(7, "sent", "sent", 1)
        assert posthog.capture.call_args.kwargs["distinct_id"] == "7"


class TestTheLaneIsOffTheWriteOnlyDebtList:
    """The other half of the #1816 ratchet: a fixed lane comes OFF the baseline in the same PR."""

    def test_send_connection_request_is_no_longer_a_known_offender(self):
        import json
        import pathlib

        baseline = json.loads(
            (pathlib.Path(__file__).with_name("selenium_lane_event_baseline.json"))
            .read_text(encoding="utf-8"))
        assert "cqc_lem.app.run_automation.send_connection_request" not in baseline


class TestNothingIsEmittedWhenNothingWasDispatched:

    def test_a_row_that_is_not_sendable_at_all_emits_nothing(self):
        """The task never reached the lane — an outcome here would be a fabricated denominator."""
        from cqc_lem.app.engagement import invites
        with patch("cqc_lem.utilities.db.get_connection_request",
                   return_value=_req(status="sent")), \
             patch(f"{_INV}.invite_to_connect_now") as send, \
             patch(f"{_INV}.track_invite_outcome") as track:
            invites.send_connection_request(3)
        send.assert_not_called()
        track.assert_not_called()

    def test_a_missing_row_emits_nothing(self):
        from cqc_lem.app.engagement import invites
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=None), \
             patch(f"{_INV}.track_invite_outcome") as track:
            invites.send_connection_request(3)
        track.assert_not_called()


def test_the_tracker_is_reachable_from_the_module_the_task_reads():
    """Patch targets live where the symbol is USED (CLAUDE.md's facade rule, read for trackers)."""
    from cqc_lem.app.engagement import invites
    assert callable(invites.track_invite_outcome)
