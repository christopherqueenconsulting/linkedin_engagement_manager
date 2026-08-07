"""Unit tests for DM conversation auto-nurture (issue #485) — a lead's reply becomes an
APPROVAL-GATED next message instead of the end of the thread.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.linkedin.message_thread import ThreadState

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


def _followup(**kw):
    base = {"id": 3, "user_id": 1, "profile_url": "https://x/in/jane", "first_name": "Jane",
            "event_type": "connection_accepted", "next_step": 1}
    base.update(kw)
    return base


def _nurture_patches(**overrides):
    """Every collaborator of _nurture_after_reply, defaulted to the happy path."""
    defaults = {
        "classify_reply_intent": {"intent": "interested", "matched": [], "method": "heuristic"},
        "has_open_scheduled_dm": False,
        "count_scheduled_dms_created_today": 0,
        "get_dm_template": {"template_text": "tmpl", "delay_hours": 0, "step": 0},
        "generate_nurture_dm": "Worth a quick call?",
        "build_dm_from_template": "fallback text",
        "insert_scheduled_dm": 99,
        "get_dm_history_for_profile": [],
    }
    defaults.update(overrides)
    return {name: patch(f"{_RA}.{name}", return_value=val) for name, val in defaults.items()}


def _run_nurture(followup=None, **overrides):
    from cqc_lem.app.run_automation import _nurture_after_reply
    patches = _nurture_patches(**overrides)
    started = {name: p.start() for name, p in patches.items()}
    enq = patch(f"{_RA}.enqueue_followup").start()
    try:
        got = _nurture_after_reply(1, followup or _followup(), "Sounds good, let's talk",
                                   MagicMock(), prefs={}, profile_synthesis="voice")
    finally:
        patch.stopall()
    return got, started, enq


class TestNurtureAfterReply:
    def test_queues_an_approval_gated_draft(self):
        got, mocks, enq = _run_nurture()
        assert got == 99
        kwargs = mocks["insert_scheduled_dm"].call_args.kwargs
        assert kwargs["status"] == "pending"      # nothing sends without a human
        assert kwargs["source"] == "nurture"
        assert kwargs["recipient_name"] == "Jane"
        assert mocks["insert_scheduled_dm"].call_args[0][2] == "Worth a quick call?"

    def test_schedules_by_intent(self):
        from datetime import datetime, timedelta, timezone

        from cqc_lem.utilities.ai.dm_nurture import nurture_delay_hours
        got, mocks, _enq = _run_nurture(classify_reply_intent={"intent": "not_now", "matched": [],
                                                               "method": "heuristic"})
        due = mocks["insert_scheduled_dm"].call_args[0][3]
        expected = (datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(hours=nurture_delay_hours("not_now")))
        assert abs((due - expected).total_seconds()) < 120
        assert got == 99

    def test_explicit_disinterest_drafts_nothing_and_never_rechecks(self):
        got, mocks, enq = _run_nurture(classify_reply_intent={"intent": "disinterest", "matched": [],
                                                              "method": "heuristic"})
        assert got is None
        mocks["insert_scheduled_dm"].assert_not_called()
        mocks["generate_nurture_dm"].assert_not_called()
        enq.assert_not_called()

    def test_dedups_one_draft_per_thread(self):
        got, mocks, _enq = _run_nurture(has_open_scheduled_dm=True)
        assert got is None
        mocks["generate_nurture_dm"].assert_not_called()
        mocks["insert_scheduled_dm"].assert_not_called()

    def test_an_open_artifact_draft_also_blocks_a_nurture_draft(self):
        """Issue #624: the owned-asset delivery writes to the SAME thread, so the one-open-draft
        rule has to hold across both mechanics — otherwise the person gets two pending messages.
        """
        from cqc_lem.app.run_automation import _nurture_after_reply
        from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_ARTIFACT

        def _open(user_id, url, source=None):
            return source == SCHEDULED_DM_SOURCE_ARTIFACT

        patches = _nurture_patches()
        started = {name: p.start() for name, p in patches.items()}
        patch(f"{_RA}.enqueue_followup").start()
        started["has_open_scheduled_dm"].side_effect = _open
        try:
            got = _nurture_after_reply(1, _followup(), "Sounds good", MagicMock(), prefs={},
                                       profile_synthesis="voice")
        finally:
            patch.stopall()
        assert got is None
        started["insert_scheduled_dm"].assert_not_called()
        started["generate_nurture_dm"].assert_not_called()

    def test_daily_cap_stops_drafting(self, monkeypatch):
        monkeypatch.setenv("DM_NURTURE_MAX_PER_DAY", "2")
        got, mocks, _enq = _run_nurture(count_scheduled_dms_created_today=2)
        assert got is None
        mocks["generate_nurture_dm"].assert_not_called()

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("DM_NURTURE_ENABLED", "false")
        got, mocks, _enq = _run_nurture()
        assert got is None
        mocks["classify_reply_intent"].assert_not_called()

    def test_blank_reply_is_ignored(self):
        from cqc_lem.app.run_automation import _nurture_after_reply
        with patch(f"{_RA}.classify_reply_intent") as cls:
            assert _nurture_after_reply(1, _followup(), "   ", MagicMock()) is None
        cls.assert_not_called()

    @pytest.mark.parametrize("value", ["true", "TRUE", " yes ", "1", "on"])
    def test_auto_approve_env_skips_the_queue(self, monkeypatch, value):
        monkeypatch.setenv("DM_NURTURE_AUTO_APPROVE", value)
        _got, mocks, _enq = _run_nurture()
        assert mocks["insert_scheduled_dm"].call_args.kwargs["status"] == "approved"

    @pytest.mark.parametrize("value", ["", "   ", "false", "no", "off", "0", "maybe"])
    def test_only_an_explicit_yes_opens_the_approval_gate(self, monkeypatch, value):
        # Blank/whitespace and anything unrecognized must fail CLOSED — a typo in this flag would
        # otherwise send a machine-written message to a prospect without a human ever seeing it.
        monkeypatch.setenv("DM_NURTURE_AUTO_APPROVE", value)
        _got, mocks, _enq = _run_nurture()
        assert mocks["insert_scheduled_dm"].call_args.kwargs["status"] == "pending"

    def test_falls_back_to_the_template_when_the_llm_fails(self):
        from cqc_lem.app.run_automation import _nurture_after_reply
        patches = _nurture_patches()
        started = {n: p.start() for n, p in patches.items()}
        patch(f"{_RA}.enqueue_followup").start()
        started["generate_nurture_dm"].side_effect = RuntimeError("llm down")
        try:
            got = _nurture_after_reply(1, _followup(), "Sounds good", MagicMock())
        finally:
            patch.stopall()
        assert got == 99
        assert started["insert_scheduled_dm"].call_args[0][2] == "fallback text"

    def test_no_draft_at_all_queues_nothing(self):
        got, mocks, enq = _run_nurture(generate_nurture_dm=None, build_dm_from_template=None)
        assert got is None
        mocks["insert_scheduled_dm"].assert_not_called()
        enq.assert_not_called()

    def test_failed_insert_does_not_recheck(self):
        got, _mocks, enq = _run_nurture(insert_scheduled_dm=None)
        assert got is None
        enq.assert_not_called()

    def test_first_reply_starts_the_nurture_sequence_at_step_zero(self):
        _got, mocks, enq = _run_nurture()
        assert mocks["get_dm_template"].call_args[0][2] == 0
        # A re-check is queued so a further reply gets its own next message.
        assert enq.call_args[0][3] == "nurture" and enq.call_args[0][4] == 0

    def test_a_nurture_followup_advances_the_step(self):
        _got, mocks, enq = _run_nurture(followup=_followup(event_type="nurture", next_step=0))
        assert mocks["get_dm_template"].call_args[0][2] == 1
        assert enq.call_args[0][4] == 1

    def test_the_sequence_is_bounded(self):
        _got, _mocks, enq = _run_nurture(followup=_followup(event_type="nurture", next_step=1))
        enq.assert_not_called()  # step 2 is the last touch — no further re-check

    def test_unexpected_errors_are_swallowed(self):
        from cqc_lem.app.run_automation import _nurture_after_reply
        with patch(f"{_RA}.classify_reply_intent", side_effect=RuntimeError("boom")):
            assert _nurture_after_reply(1, _followup(), "Sounds good", MagicMock()) is None


class TestProcessUserFollowupsNurture:
    def _run(self, due_rows, extra: dict):
        """Run the follow-up task with the driver/DB collaborators stubbed, returning
        (result, {name: mock}) for the caller's own patches.
        """
        from cqc_lem.app.run_automation import process_user_followups
        base = {
            "get_due_followups": patch(f"{_RA}.get_due_followups", return_value=due_rows),
            "get_current_profile": patch(f"{_RA}.get_current_profile",
                                         return_value=(MagicMock(), MagicMock(), "e", MagicMock())),
            "quit_gracefully": patch(f"{_RA}.quit_gracefully"),
            "sleep": patch(f"{_RA}.time.sleep"),
            "insert_new_log": patch(f"{_RA}.insert_new_log"),
            "get_engagement_preferences": patch(f"{_RA}.get_engagement_preferences", return_value={}),
            "get_or_create_profile_synthesis": patch(f"{_RA}.get_or_create_profile_synthesis",
                                                     return_value="voice"),
            "_flag_lead_signal": patch(f"{_RA}._flag_lead_signal"),
            "stop_followups_for_profile": patch(f"{_RA}.stop_followups_for_profile"),
            "mark_followup": patch(f"{_RA}.mark_followup"),
            "send_private_dm": patch(f"{_RA}.send_private_dm"),
        }
        base.update(extra)
        mocks = {name: p.start() for name, p in base.items()}
        try:
            return process_user_followups.run(user_id=1), mocks
        finally:
            patch.stopall()

    def test_a_reply_nurtures_the_thread_after_stopping_the_old_sequence(self):
        order = []
        result, mocks = self._run([_followup()], {
            "check_dm_replied": patch(f"{_RA}.check_dm_replied", return_value=ThreadState.REPLIED),
            "_last_inbound_message": patch(f"{_RA}._last_inbound_message",
                                           return_value="Sounds good, let's talk"),
            "stop_followups_for_profile": patch(f"{_RA}.stop_followups_for_profile",
                                                side_effect=lambda *a: order.append("stop")),
            "_nurture_after_reply": patch(f"{_RA}._nurture_after_reply",
                                          side_effect=lambda *a, **k: order.append("nurture") or 5),
        })
        # The blanket stop must come FIRST or it would cancel the re-check the nurture just queued.
        assert order == ["stop", "nurture"]
        assert mocks["_nurture_after_reply"].call_args[0][2] == "Sounds good, let's talk"
        mocks["send_private_dm"].apply_async.assert_not_called()
        assert "drafted 1 nurture" in result

    def test_the_inbound_text_is_read_once_and_used_by_both_lead_detection_and_nurture(self):
        _result, mocks = self._run([_followup()], {
            "check_dm_replied": patch(f"{_RA}.check_dm_replied", return_value=ThreadState.REPLIED),
            "_last_inbound_message": patch(f"{_RA}._last_inbound_message", return_value="How much?"),
            "_nurture_after_reply": patch(f"{_RA}._nurture_after_reply", return_value=None),
        })
        mocks["_last_inbound_message"].assert_called_once()
        assert mocks["_flag_lead_signal"].call_args[0][1] == "How much?"

    def test_a_nurture_followup_never_auto_sends_a_template(self):
        result, mocks = self._run([_followup(event_type="nurture", next_step=0)], {
            "check_dm_replied": patch(f"{_RA}.check_dm_replied", return_value=ThreadState.NOT_REPLIED),
            "build_dm_from_template": patch(f"{_RA}.build_dm_from_template"),
        })
        mocks["build_dm_from_template"].assert_not_called()
        mocks["send_private_dm"].apply_async.assert_not_called()
        mocks["mark_followup"].assert_called_once_with(3, "stopped")
        assert "Sent 0" in result
