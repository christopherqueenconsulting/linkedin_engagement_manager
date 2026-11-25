"""Unit tests for the comment-first outreach funnel (issue #399): stage firing, processor, dispatch."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"
# The funnel moved to `app.engagement.outreach` (#1154), and #1206 deleted the `run_automation`
# shim the beat used to import through — `auto_process_outreach_funnel`'s lazy
# `from cqc_lem.app.engagement.outreach import process_outreach_funnel` now resolves here, so this
# is the binding the dispatch patches have to target.
_RS = "cqc_lem.app.run_scheduler"
_DB = "cqc_lem.utilities.db"


def _target(**kw):
    base = {"id": 1, "user_id": 1, "target_profile_url": "https://x/in/jane", "target_name": "Jane Doe",
            "stage": "comment", "status": "approved", "context_url": "https://x/post/1",
            "draft_text": "great post"}
    base.update(kw)
    return base


def _prefs():
    return {"max_comments_per_day": 50, "max_dms_per_day": 25, "max_invites_per_day": 10}


@pytest.fixture(autouse=True)
def _open_invite_gates():
    """The connect stage now honours the invite wall and daily cap (#2623); default them open."""
    with patch(f"{_OUT}.is_invites_held", return_value=False), \
         patch(f"{_OUT}.count_invites_sent_today", return_value=0):
        yield


class TestFireFunnelStage:
    # The connect/DM stage tests use user 2: user 1 is the brand account, whose connect/DM stages
    # are held by the #2376 ruling (TestBrandAcquisitionHold below).
    def _common(self):
        return {
            "prefs": patch(f"{_OUT}.get_engagement_preferences", return_value=_prefs()),
            "cc": patch(f"{_OUT}.count_comments_today", return_value=0),
            "cd": patch(f"{_OUT}.count_dms_sent_today", return_value=0),
            "refine": patch(f"{_OUT}.get_ai_message_refinement", return_value="refined note"),
            "dm": patch(f"{_OUT}.build_dm_from_template", return_value="dm text"),
            "enq": patch(f"{_OUT}.enqueue_next_followup"),
            "upd": patch(f"{_OUT}.update_outreach_target"),
            "ust": patch(f"{_OUT}.update_outreach_target_status"),
        }

    def test_comment_fires_and_advances_to_connect(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStage, OutreachStatus
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], \
             c["upd"] as upd, c["ust"], patch(f"{_OUT}.comment_on_post") as cop:
            out = _fire_funnel_stage(1, _target())
        cop.apply_async.assert_called_once()
        assert cop.apply_async.call_args.kwargs["kwargs"]["post_link"] == "https://x/post/1"
        assert cop.apply_async.call_args.kwargs["kwargs"]["comment_text"] == "great post"
        assert upd.call_args.kwargs["stage"] == OutreachStage.CONNECT
        assert upd.call_args.kwargs["status"] == OutreachStatus.PENDING
        assert upd.call_args.kwargs["draft_text"] == "refined note"
        assert "connect" in out

    def test_comment_deferred_when_cap_reached(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        c = self._common()
        with c["prefs"], patch(f"{_OUT}.count_comments_today", return_value=99), c["cd"], \
             c["refine"], c["dm"], c["enq"], c["upd"], c["ust"], patch(f"{_OUT}.comment_on_post") as cop:
            out = _fire_funnel_stage(1, _target())
        cop.apply_async.assert_not_called()
        assert "deferred" in out

    def test_comment_fails_without_context_url(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStatus
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], c["upd"], \
             c["ust"] as ust, patch(f"{_OUT}.comment_on_post") as cop:
            out = _fire_funnel_stage(1, _target(context_url=None))
        cop.apply_async.assert_not_called()
        ust.assert_called_once_with(1, OutreachStatus.FAILED)
        assert "failed" in out

    def test_connect_fires_and_advances_to_dm(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], \
             c["upd"] as upd, c["ust"], patch(f"{_OUT}.invite_to_connect") as inv:
            out = _fire_funnel_stage(2, _target(stage="connect", draft_text="my note"))
        inv.apply_async.assert_called_once()
        assert inv.apply_async.call_args.kwargs["kwargs"]["message"] == "my note"
        assert upd.call_args.kwargs["stage"] == OutreachStage.DM
        assert upd.call_args.kwargs["draft_text"] == "dm text"
        assert "dm" in out

    def test_dm_fires_and_completes(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStage, OutreachStatus
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], \
             c["enq"] as enq, c["upd"] as upd, c["ust"], patch(f"{_OUT}.send_private_dm") as spd:
            out = _fire_funnel_stage(2, _target(stage="dm", draft_text="hi jane"))
        spd.apply_async.assert_called_once()
        assert spd.apply_async.call_args.kwargs["kwargs"]["message"] == "hi jane"
        enq.assert_called_once()
        assert enq.call_args[0][3] == "funnel"  # event_type
        assert upd.call_args.kwargs["stage"] == OutreachStage.COMPLETED
        assert upd.call_args.kwargs["status"] == OutreachStatus.ACTED
        assert "completed" in out

    def test_comment_stage_auto_approves_the_connect_under_funnel_stage_mode(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        from cqc_lem.utilities.approval_modes import FUNNEL_STAGE_APPROVER
        from cqc_lem.utilities.db import OutreachStage, OutreachStatus
        c = self._common()
        prefs = {**_prefs(), "funnel_stage_mode": "auto_approve"}
        with patch(f"{_OUT}.get_engagement_preferences", return_value=prefs), c["cc"], c["cd"], \
             c["refine"], c["dm"], c["enq"], c["upd"] as upd, c["ust"], \
             patch(f"{_OUT}.comment_on_post"):
            out = _fire_funnel_stage(2, _target(stage="comment"))
        assert upd.call_args.kwargs["stage"] == OutreachStage.CONNECT
        assert upd.call_args.kwargs["status"] == OutreachStatus.APPROVED
        assert upd.call_args.kwargs["approved_by"] == FUNNEL_STAGE_APPROVER
        assert FUNNEL_STAGE_APPROVER.startswith("system:")
        assert "auto-approved" in out

    def test_dm_stage_is_never_auto_approved_after_a_connect(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStage, OutreachStatus
        c = self._common()
        prefs = {**_prefs(), "funnel_stage_mode": "auto_approve"}
        with patch(f"{_OUT}.get_engagement_preferences", return_value=prefs), c["cc"], c["cd"], \
             c["refine"], c["dm"], c["enq"], c["upd"] as upd, c["ust"], \
             patch(f"{_OUT}.invite_to_connect"):
            _fire_funnel_stage(2, _target(stage="connect", draft_text="my note"))
        assert upd.call_args.kwargs["stage"] == OutreachStage.DM
        assert upd.call_args.kwargs["status"] == OutreachStatus.PENDING
        assert "approved_by" not in upd.call_args.kwargs

    def test_env_force_off_keeps_the_next_stage_pending(self, monkeypatch):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStatus
        monkeypatch.setenv("FUNNEL_STAGE_AUTO_APPROVE", "0")
        c = self._common()
        prefs = {**_prefs(), "funnel_stage_mode": "auto_approve"}
        with patch(f"{_OUT}.get_engagement_preferences", return_value=prefs), c["cc"], c["cd"], \
             c["refine"], c["dm"], c["enq"], c["upd"] as upd, c["ust"], \
             patch(f"{_OUT}.comment_on_post"):
            _fire_funnel_stage(2, _target(stage="comment"))
        assert upd.call_args.kwargs["status"] == OutreachStatus.PENDING
        assert "approved_by" not in upd.call_args.kwargs

    def test_an_empty_next_draft_is_never_auto_approved(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStatus
        c = self._common()
        prefs = {**_prefs(), "funnel_stage_mode": "auto_approve"}
        with patch(f"{_OUT}.get_engagement_preferences", return_value=prefs), c["cc"], c["cd"], \
             c["refine"], patch(f"{_OUT}.build_dm_from_template", return_value=None), c["enq"], \
             c["upd"] as upd, c["ust"], patch(f"{_OUT}.invite_to_connect"):
            _fire_funnel_stage(2, _target(stage="connect", draft_text="my note"))
        assert upd.call_args.kwargs["status"] == OutreachStatus.PENDING

    def test_dm_deferred_when_cap_reached(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        c = self._common()
        with c["prefs"], c["cc"], patch(f"{_OUT}.count_dms_sent_today", return_value=99), \
             c["refine"], c["dm"], c["enq"], c["upd"], c["ust"], patch(f"{_OUT}.send_private_dm") as spd:
            out = _fire_funnel_stage(2, _target(stage="dm", draft_text="hi"))
        spd.apply_async.assert_not_called()
        assert "deferred" in out

    def test_no_draft_skips(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        from cqc_lem.utilities.db import OutreachStatus
        c = self._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], c["upd"], \
             c["ust"] as ust, patch(f"{_OUT}.comment_on_post") as cop:
            out = _fire_funnel_stage(1, _target(draft_text=""))
        cop.apply_async.assert_not_called()
        ust.assert_called_once_with(1, OutreachStatus.SKIPPED)
        assert "skipped" in out


class TestBrandAcquisitionHold:
    """Owner ruling 2026-10-10 (#2376, option A): the brand's funnel connect and DM stages are held.

    The draft goes back to 'pending' for the owner to send. Its comment stage and every other
    user's stages are unaffected.
    """

    def _run(self, user_id, stage, brand_user_id=""):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        c = TestFireFunnelStage()._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"] as enq, \
             c["upd"] as upd, c["ust"] as ust, \
             patch("cqc_lem.utilities.brand_account.BRAND_USER_ID", brand_user_id), \
             patch(f"{_OUT}.invite_to_connect") as inv, \
             patch(f"{_OUT}.send_private_dm") as spd, \
             patch(f"{_OUT}.comment_on_post") as cop:
            out = _fire_funnel_stage(user_id, _target(stage=stage, draft_text="draft"))
        return out, {"inv": inv, "spd": spd, "cop": cop, "enq": enq, "upd": upd, "ust": ust}

    @pytest.mark.parametrize("stage", ["connect", "dm"])
    def test_the_brand_stage_is_held_and_returned_to_pending(self, stage):
        from cqc_lem.utilities.brand_account import ACQUISITION_HOLD_REASON
        from cqc_lem.utilities.db import OutreachStatus
        out, m = self._run(1, stage)
        m["inv"].apply_async.assert_not_called()
        m["spd"].apply_async.assert_not_called()
        m["enq"].assert_not_called()                          # no funnel follow-up either
        # Back to pending with the reason in `notes`; no `stage=` — the stage is not advanced.
        m["upd"].assert_called_once_with(1, status=OutreachStatus.PENDING,
                                         notes=ACQUISITION_HOLD_REASON)
        m["ust"].assert_not_called()
        assert "held" in out

    def test_the_hold_follows_an_overridden_brand_user(self):
        out, m = self._run(7, "connect", brand_user_id="7")
        m["inv"].apply_async.assert_not_called()
        assert "held" in out

    @pytest.mark.parametrize("stage,task", [("connect", "inv"), ("dm", "spd")])
    def test_other_users_still_fire(self, stage, task):
        out, m = self._run(2, stage)
        m[task].apply_async.assert_called_once()
        assert "held" not in out

    def _run_approved(self, approved_by, stage="connect", invites_held=False, sent_today=0):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        c = TestFireFunnelStage()._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], \
             c["upd"] as upd, c["ust"], \
             patch(f"{_OUT}.is_invites_held", return_value=invites_held), \
             patch(f"{_OUT}.count_invites_sent_today", return_value=sent_today), \
             patch("cqc_lem.utilities.brand_account.BRAND_USER_ID", ""), \
             patch(f"{_OUT}.invite_to_connect") as inv, patch(f"{_OUT}.send_private_dm") as spd:
            out = _fire_funnel_stage(1, _target(stage=stage, draft_text="draft",
                                                approved_by=approved_by))
        return out, inv, spd, upd

    @pytest.mark.parametrize("stage,task", [("connect", "inv"), ("dm", "spd")])
    def test_a_human_approval_releases_the_brand_stage(self, stage, task):
        out, inv, spd, _ = self._run_approved("user:1", stage)
        {"inv": inv, "spd": spd}[task].apply_async.assert_called_once()
        assert "held" not in out

    @pytest.mark.parametrize("approved_by", [None, "system:auto_queue"])
    def test_a_non_human_approval_stays_held(self, approved_by):
        out, inv, _, _ = self._run_approved(approved_by)
        inv.apply_async.assert_not_called()
        assert "held" in out

    def test_hard_hold_overrides_a_human_approval(self, monkeypatch):
        monkeypatch.setenv("ACQUISITION_HOLD_HARD", "1")
        out, inv, _, _ = self._run_approved("user:1")
        inv.apply_async.assert_not_called()
        assert "held" in out

    def test_a_released_connect_defers_at_the_daily_invite_cap(self):
        out, inv, _, upd = self._run_approved("user:1", sent_today=10)
        inv.apply_async.assert_not_called()
        upd.assert_not_called()  # stays APPROVED for the next run
        assert "deferred" in out

    def test_a_released_connect_defers_while_invites_are_held(self):
        out, inv, _, upd = self._run_approved("user:1", invites_held=True)
        inv.apply_async.assert_not_called()
        upd.assert_not_called()
        assert "deferred" in out

    def test_the_brand_comment_stage_is_not_held(self):
        """Comments are the owner's ordinary engagement and keep their caps."""
        out, m = self._run(1, "comment")
        m["cop"].apply_async.assert_called_once()
        assert "held" not in out


class TestDraftFunnelStage:
    def test_connect_note_refined(self):
        from cqc_lem.app.engagement.outreach import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_OUT}.get_ai_message_refinement", return_value="voiced note"):
            got = _draft_funnel_stage(1, OutreachStage.CONNECT, _target(target_name="Jane Doe"))
        assert got == "voiced note"

    def test_connect_note_falls_back_on_error(self):
        from cqc_lem.app.engagement.outreach import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_OUT}.get_ai_message_refinement", side_effect=RuntimeError("boom")):
            got = _draft_funnel_stage(1, OutreachStage.CONNECT, _target())
        assert "connect" in got.lower() or got  # non-empty fallback

    def test_dm_uses_template(self):
        from cqc_lem.app.engagement.outreach import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_OUT}.build_dm_from_template", return_value="hi from template") as b:
            got = _draft_funnel_stage(1, OutreachStage.DM, _target())
        assert got == "hi from template"
        assert b.call_args[0][1] == "funnel"  # event_type

    def test_connect_note_has_no_em_dash(self):
        from cqc_lem.app.engagement.outreach import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_OUT}.get_ai_message_refinement", return_value="Hi Jo — let's connect"):
            got = _draft_funnel_stage(1, OutreachStage.CONNECT, _target())
        assert "—" not in got and got == "Hi Jo, let's connect"

    def test_connect_fallback_note_has_no_em_dash(self):
        from cqc_lem.app.engagement.outreach import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_OUT}.get_ai_message_refinement", side_effect=RuntimeError("boom")):
            got = _draft_funnel_stage(1, OutreachStage.CONNECT, _target())
        assert "—" not in got and "--" not in got

    def test_dm_has_no_em_dash(self):
        from cqc_lem.app.engagement.outreach import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_OUT}.build_dm_from_template", return_value="Hi — quick note"):
            got = _draft_funnel_stage(1, OutreachStage.DM, _target())
        assert got == "Hi, quick note"

    def test_dm_empty_when_no_template(self):
        from cqc_lem.app.engagement.outreach import _draft_funnel_stage
        from cqc_lem.utilities.db import OutreachStage
        with patch(f"{_OUT}.build_dm_from_template", return_value=None):
            assert _draft_funnel_stage(1, OutreachStage.DM, _target()) == ""


class TestProcessOutreachFunnel:
    def test_no_targets(self):
        from cqc_lem.app.engagement.outreach import process_outreach_funnel
        with patch(f"{_OUT}.get_approved_outreach_targets", return_value=[]):
            out = process_outreach_funnel.run(user_id=1)
        assert "No approved" in out

    def test_fires_each_target(self):
        from cqc_lem.app.engagement.outreach import process_outreach_funnel
        with patch(f"{_OUT}.get_approved_outreach_targets", return_value=[_target(id=1), _target(id=2)]), \
             patch(f"{_OUT}._fire_funnel_stage", side_effect=["a", "b"]) as fire:
            out = process_outreach_funnel.run(user_id=1)
        assert fire.call_count == 2
        assert out == "a; b"

    def test_connects_queued_this_run_count_against_the_invite_cap(self):
        """invite_to_connect is async, so sent-today stays 0 while a batch dispatches (#2623)."""
        from cqc_lem.app.engagement.outreach import process_outreach_funnel
        fired = "target 1: connect fired -> dm pending approval"
        with patch(f"{_OUT}.get_approved_outreach_targets",
                   return_value=[_target(id=1), _target(id=2), _target(id=3)]), \
             patch(f"{_OUT}._fire_funnel_stage", side_effect=[fired, fired, "x"]) as fire:
            process_outreach_funnel.run(user_id=1)
        assert [c.kwargs["invites_queued"] for c in fire.call_args_list] == [0, 1, 2]

    def test_a_connect_stage_defers_once_queued_invites_reach_the_cap(self):
        from cqc_lem.app.engagement.outreach import _fire_funnel_stage
        c = TestFireFunnelStage()._common()
        with c["prefs"], c["cc"], c["cd"], c["refine"], c["dm"], c["enq"], c["upd"], c["ust"], \
             patch(f"{_OUT}.is_invites_held", return_value=False), \
             patch(f"{_OUT}.count_invites_sent_today", return_value=0), \
             patch("cqc_lem.utilities.brand_account.BRAND_USER_ID", ""), \
             patch(f"{_OUT}.invite_to_connect") as inv:
            out = _fire_funnel_stage(1, _target(stage="connect", draft_text="d", approved_by="user:1"),
                                invites_queued=10)
        inv.apply_async.assert_not_called()
        assert "deferred" in out

    def test_marks_failed_on_error(self):
        from cqc_lem.app.engagement.outreach import process_outreach_funnel
        from cqc_lem.utilities.db import OutreachStatus
        with patch(f"{_OUT}.get_approved_outreach_targets", return_value=[_target(id=7)]), \
             patch(f"{_OUT}._fire_funnel_stage", side_effect=RuntimeError("x")), \
             patch(f"{_OUT}.update_outreach_target_status") as ust:
            out = process_outreach_funnel.run(user_id=1)
        ust.assert_called_once_with(7, OutreachStatus.FAILED)
        assert "error" in out


class TestDispatcher:
    def test_dispatches_per_user(self):
        from cqc_lem.app.run_scheduler import auto_process_outreach_funnel
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_DB}.get_users_with_approved_outreach", return_value=[1, 2]), \
             patch(f"{_OUT}.process_outreach_funnel") as proc:
            out = auto_process_outreach_funnel()
        assert proc.apply_async.call_count == 2
        assert "2 user" in out

    def test_throttled(self):
        from cqc_lem.app.run_scheduler import auto_process_outreach_funnel
        with patch(f"{_RS}._skip_if_throttled", return_value=True):
            out = auto_process_outreach_funnel()
        assert "throttled" in out.lower()
