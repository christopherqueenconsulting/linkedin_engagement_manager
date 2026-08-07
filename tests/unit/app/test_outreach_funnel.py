"""Unit tests for the comment-first outreach funnel (issue #399): stage firing, processor, dispatch."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
_RS = "cqc_lem.app.run_scheduler"
_DB = "cqc_lem.utilities.db"


def _target(**kw):
    base = {"id": 1, "user_id": 1, "target_profile_url": "https://x/in/jane", "target_name": "Jane Doe",
            "stage": "comment", "status": "approved", "context_url": "https://x/post/1",
            "draft_text": "great post"}
    base.update(kw)
    return base


def _prefs():
    return {"max_comments_per_day": 50, "max_dms_per_day": 25}


class TestFireFunnelStage:
    def _common(self):
        return {
            "prefs": patch(f"{_RA}.get_engagement_preferences", return_value=_prefs()),
            "cc": patch(f"{_RA}.count_comments_today", return_value=0),
            "cd": patch(f"{_RA}.count_dms_sent_today", return_value=0),
            "refine": patch(f"{_RA}.get_ai_message_refinement", return_value="refined note"),
            "dm": patch(f"{_RA}.build_dm_from_template", return_value="dm text"),
            "enq": patch(f"{_RA}.enqueue_next_followup"),
            "upd": patch(f"{_RA}.update_outreach_target"),
            "ust": patch(f"{_RA}.update_outreach_target_status"),
        }

    def test_comment_fires_and_advances_to_connect(self):
        from cqc_lem.app.run_automation import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStage, OutreachStatus
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], \
             c["upd"] as upd, c["ust"], patch(f"{_RA}.comment_on_post") as cop:
            out = _fire_funnel_stage(1, _target())
        cop.apply_async.assert_called_once()
        assert cop.apply_async.call_args.kwargs["kwargs"]["post_link"] == "https://x/post/1"
        assert cop.apply_async.call_args.kwargs["kwargs"]["comment_text"] == "great post"
        assert upd.call_args.kwargs["stage"] == OutreachStage.CONNECT
        assert upd.call_args.kwargs["status"] == OutreachStatus.PENDING
        assert upd.call_args.kwargs["draft_text"] == "refined note"
        assert "connect" in out

    def test_comment_deferred_when_cap_reached(self):
        from cqc_lem.app.run_automation import _fire_funnel_stage
        c = self._common()
        with c["prefs"], patch(f"{_RA}.count_comments_today", return_value=99), c["cd"], \
             c["refine"], c["dm"], c["enq"], c["upd"], c["ust"], patch(f"{_RA}.comment_on_post") as cop:
            out = _fire_funnel_stage(1, _target())
        cop.apply_async.assert_not_called()
        assert "deferred" in out

    def test_comment_fails_without_context_url(self):
        from cqc_lem.app.run_automation import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStatus
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], c["upd"], \
             c["ust"] as ust, patch(f"{_RA}.comment_on_post") as cop:
            out = _fire_funnel_stage(1, _target(context_url=None))
        cop.apply_async.assert_not_called()
        ust.assert_called_once_with(1, OutreachStatus.FAILED)
        assert "failed" in out

    def test_connect_fires_and_advances_to_dm(self):
        from cqc_lem.app.run_automation import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], \
             c["upd"] as upd, c["ust"], patch(f"{_RA}.invite_to_connect") as inv:
            out = _fire_funnel_stage(1, _target(stage="connect", draft_text="my note"))
        inv.apply_async.assert_called_once()
        assert inv.apply_async.call_args.kwargs["kwargs"]["message"] == "my note"
        assert upd.call_args.kwargs["stage"] == OutreachStage.DM
        assert upd.call_args.kwargs["draft_text"] == "dm text"
        assert "dm" in out

    def test_dm_fires_and_completes(self):
        from cqc_lem.app.run_automation import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStage, OutreachStatus
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], \
             c["enq"] as enq, c["upd"] as upd, c["ust"], patch(f"{_RA}.send_private_dm") as spd:
            out = _fire_funnel_stage(1, _target(stage="dm", draft_text="hi jane"))
        spd.apply_async.assert_called_once()
        assert spd.apply_async.call_args.kwargs["kwargs"]["message"] == "hi jane"
        enq.assert_called_once()
        assert enq.call_args[0][3] == "funnel"  # event_type
        assert upd.call_args.kwargs["stage"] == OutreachStage.COMPLETED
        assert upd.call_args.kwargs["status"] == OutreachStatus.ACTED
        assert "completed" in out

    def test_dm_deferred_when_cap_reached(self):
        from cqc_lem.app.run_automation import _fire_funnel_stage
        c = self._common()
        with c["prefs"], c["cc"], patch(f"{_RA}.count_dms_sent_today", return_value=99), \
             c["refine"], c["dm"], c["enq"], c["upd"], c["ust"], patch(f"{_RA}.send_private_dm") as spd:
            out = _fire_funnel_stage(1, _target(stage="dm", draft_text="hi"))
        spd.apply_async.assert_not_called()
        assert "deferred" in out

    def test_no_draft_skips(self):
        from cqc_lem.app.run_automation import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStatus
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], c["upd"], \
             c["ust"] as ust, patch(f"{_RA}.comment_on_post") as cop:
            out = _fire_funnel_stage(1, _target(draft_text=""))
        cop.apply_async.assert_not_called()
        ust.assert_called_once_with(1, OutreachStatus.SKIPPED)
        assert "skipped" in out


class TestDraftFunnelStage:
    def test_connect_note_refined(self):
        from cqc_lem.app.run_automation import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_RA}.get_ai_message_refinement", return_value="voiced note"):
            got = _draft_funnel_stage(1, OutreachStage.CONNECT, _target(target_name="Jane Doe"))
        assert got == "voiced note"

    def test_connect_note_falls_back_on_error(self):
        from cqc_lem.app.run_automation import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_RA}.get_ai_message_refinement", side_effect=RuntimeError("boom")):
            got = _draft_funnel_stage(1, OutreachStage.CONNECT, _target())
        assert "connect" in got.lower() or got  # non-empty fallback

    def test_dm_uses_template(self):
        from cqc_lem.app.run_automation import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_RA}.build_dm_from_template", return_value="hi from template") as b:
            got = _draft_funnel_stage(1, OutreachStage.DM, _target())
        assert got == "hi from template"
        assert b.call_args[0][1] == "funnel"  # event_type

    def test_dm_empty_when_no_template(self):
        from cqc_lem.app.run_automation import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_RA}.build_dm_from_template", return_value=None):
            assert _draft_funnel_stage(1, OutreachStage.DM, _target()) == ""


class TestProcessOutreachFunnel:
    def test_no_targets(self):
        from cqc_lem.app.run_automation import process_outreach_funnel
        with patch(f"{_RA}.get_approved_outreach_targets", return_value=[]):
            out = process_outreach_funnel.run(user_id=1)
        assert "No approved" in out

    def test_fires_each_target(self):
        from cqc_lem.app.run_automation import process_outreach_funnel
        with patch(f"{_RA}.get_approved_outreach_targets", return_value=[_target(id=1), _target(id=2)]), \
             patch(f"{_RA}._fire_funnel_stage", side_effect=["a", "b"]) as fire:
            out = process_outreach_funnel.run(user_id=1)
        assert fire.call_count == 2
        assert out == "a; b"

    def test_marks_failed_on_error(self):
        from cqc_lem.app.run_automation import process_outreach_funnel
        from cqc_lem.utilities.db import OutreachStatus
        with patch(f"{_RA}.get_approved_outreach_targets", return_value=[_target(id=7)]), \
             patch(f"{_RA}._fire_funnel_stage", side_effect=RuntimeError("x")), \
             patch(f"{_RA}.update_outreach_target_status") as ust:
            out = process_outreach_funnel.run(user_id=1)
        ust.assert_called_once_with(7, OutreachStatus.FAILED)
        assert "error" in out


class TestDispatcher:
    def test_dispatches_per_user(self):
        from cqc_lem.app.run_scheduler import auto_process_outreach_funnel
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_DB}.get_users_with_approved_outreach", return_value=[1, 2]), \
             patch(f"{_RA}.process_outreach_funnel") as proc:
            out = auto_process_outreach_funnel()
        assert proc.apply_async.call_count == 2
        assert "2 user" in out

    def test_throttled(self):
        from cqc_lem.app.run_scheduler import auto_process_outreach_funnel
        with patch(f"{_RS}._skip_if_throttled", return_value=True):
            out = auto_process_outreach_funnel()
        assert "throttled" in out.lower()
