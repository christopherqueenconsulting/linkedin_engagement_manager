"""Authenticity loop (#2751): scoring, regeneration cap, comment wiring, DM approve gate."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import authenticity_loop as loop

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_TEXT = "Retries without jitter just synchronise the failure for everyone involved."


def _llm(*scores):
    """A call_llm stub returning one judge reply per score (None = unreadable)."""
    replies = iter(scores)

    def call(**_kw):
        s = next(replies)
        body = "garbage" if s is None else json.dumps({"score": s, "issues": [f"issue {s}"]})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])
    return call


def test_pre_text_empty_when_off_and_present_when_on():
    with patch.object(loop, "flag_enabled", return_value=False):
        assert loop.pre_text(1) == ""
    with patch.object(loop, "flag_enabled", return_value=True):
        assert "Authenticity" in loop.pre_text(1)


def test_below_floor_is_three_valued():
    assert loop.below_floor(49) and not loop.below_floor(50) and not loop.below_floor(None)


def test_score_draft_unreadable_and_error_fail_open():
    assert loop.score_draft("x", _llm(None)) is None
    assert loop.score_draft("x", MagicMock(side_effect=RuntimeError("down"))) is None


def test_loop_passes_first_time():
    drafts = []
    res = loop.run_loop(lambda d: drafts.append(d) or "t", _llm(80), first="t")
    assert res.passed and res.score == 80 and res.loops == 0 and drafts == []


def test_loop_regenerates_with_issues_then_passes():
    directives = []
    res = loop.run_loop(lambda d: directives.append(d) or "better", _llm(30, 70), first="v1")
    assert res.passed and res.text == "better" and res.loops == 1
    assert "issue 30" in directives[0]


def test_loop_fails_after_three_regenerations():
    calls = []
    res = loop.run_loop(lambda d: calls.append(d) or "again", _llm(10, 20, 30, 40), first="v1")
    assert not res.passed and res.score == 40 and res.loops == 3 and len(calls) == 3
    assert res.issues == ["issue 40"]


def test_loop_unscored_draft_passes():
    res = loop.run_loop(lambda d: "x", _llm(None), first="v1")
    assert res.passed and res.score is None


def test_loop_no_draft_is_not_passed():
    assert not loop.run_loop(lambda d: None, _llm(), first=None).passed


def _gated(flag_on, scores):
    from cqc_lem.utilities.ai import ai_helper
    calls = []

    def draft(fix=""):
        calls.append(fix)
        return _TEXT

    with patch(f"{_AI}._authenticity_loop.enabled", return_value=flag_on), \
         patch.object(loop, "flag_enabled", return_value=flag_on), \
         patch(f"{_AI}._framework.comment_gate_max_attempts", return_value=1), \
         patch(f"{_AI}._framework.comment_contract_report", return_value={"failures": []}), \
         patch(f"{_AI}._framework.comment_similarity_report",
               return_value={"too_similar": False, "match": None}), \
         patch(f"{_AI}._framework.comment_batch_report", return_value={"hints": []}), \
         patch(f"{_AI}._slop.lint_report", return_value={"passes": True, "hard": []}), \
         patch(f"{_AI}._call_llm", side_effect=_llm(*scores)):
        out = ai_helper._gated_comment(draft, "post", user_id=7)
    return out, calls


def test_comment_off_path_byte_identical_and_unscored():
    out, calls = _gated(False, [])
    assert out == _TEXT and calls == [""]


def test_comment_on_passes_with_pre_text():
    out, calls = _gated(True, [90])
    assert out == _TEXT and "Authenticity" in calls[0]


def test_comment_on_regenerates_then_ships():
    out, calls = _gated(True, [20, 85])
    assert out == _TEXT and len(calls) == 2 and "issue 20" in calls[1]


def test_comment_on_skips_after_three_failed_loops():
    out, calls = _gated(True, [10, 10, 10, 10])
    assert out is None and len(calls) == 4


# --- DM approve gate -------------------------------------------------------------------------
_S, _U = "tok", 5


def _put(api_client, stored, **body):
    from cqc_lem.utilities.db import ScheduledDmStatus  # noqa: F401
    with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
         patch("cqc_lem.api.main.get_scheduled_dm_user_id", return_value=_U), \
         patch("cqc_lem.api.main.get_scheduled_dm", return_value=stored), \
         patch("cqc_lem.api.main.update_scheduled_dm", return_value=True) as upd, \
         patch("cqc_lem.api.main.insert_authenticity_override", return_value=True) as audit, \
         patch.object(loop, "flag_enabled", return_value=True):
        resp = api_client.put("/api/dm", json={"session_token": _S, "dm_id": 3, "action": "approve",
                                               **body})
    return resp, upd, audit


def _stored(score):
    return {"scheduled_time": datetime.now(timezone.utc) + timedelta(hours=1),
            "authenticity_score": score, "authenticity_issues": "too generic"}


def test_plain_approve_below_floor_is_403(api_client):
    resp, upd, audit = _put(api_client, _stored(30))
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "authenticity_below_floor"
    upd.assert_not_called()
    audit.assert_not_called()


def test_override_with_reason_posts_and_audits(api_client):
    resp, upd, audit = _put(api_client, _stored(30), authenticity_override_reason="I know them")
    assert resp.status_code == 200
    upd.assert_called_once()
    assert audit.call_args.args[:5] == (_U, "scheduled_dm", 3, 30, "I know them")


def test_approve_at_floor_needs_no_override(api_client):
    resp, _, audit = _put(api_client, _stored(50))
    assert resp.status_code == 200
    audit.assert_not_called()


def test_flag_off_never_blocks(api_client):
    with patch("cqc_lem.api.main.get_session_user_id", return_value=_U), \
         patch("cqc_lem.api.main.get_scheduled_dm_user_id", return_value=_U), \
         patch("cqc_lem.api.main.get_scheduled_dm", return_value=_stored(5)), \
         patch("cqc_lem.api.main.update_scheduled_dm", return_value=True), \
         patch.object(loop, "flag_enabled", return_value=False):
        resp = api_client.put("/api/dm", json={"session_token": _S, "dm_id": 3, "action": "approve"})
    assert resp.status_code == 200


def test_bulk_approve_refuses_below_floor():
    from cqc_lem.utilities import review_actions as ra
    with patch.object(ra, "get_scheduled_dm_user_id", return_value=_U), \
         patch.object(ra, "get_scheduled_dm", return_value=_stored(10)), \
         patch.object(ra, "update_scheduled_dm") as upd, \
         patch.object(loop, "flag_enabled", return_value=True), \
         patch("cqc_lem.app.engagement.outreach.scheduled_dm_is_stale", return_value=False):
        assert ra.approve_scheduled_dm(_U, 3) == ra.AUTHENTICITY_BELOW_FLOOR
    upd.assert_not_called()


# --- DM nurture hold + notify ----------------------------------------------------------------
def test_dm_check_off_is_a_passing_noop():
    from cqc_lem.app.engagement import outreach
    with patch.object(loop, "flag_enabled", return_value=False):
        res = outreach._authenticity_check_dm(1, "hello")
    assert res.passed and res.text == "hello" and res.score is None


def test_dm_check_on_failing_draft_is_not_passed():
    from cqc_lem.app.engagement import outreach
    with patch.object(loop, "flag_enabled", return_value=True), \
         patch(f"{_AI}._call_llm", side_effect=_llm(10, 10, 10, 10)), \
         patch.object(outreach, "get_ai_message_refinement", return_value="again"):
        res = outreach._authenticity_check_dm(1, "hello")
    assert not res.passed and res.score == 10 and res.loops == 3


def test_notify_authenticity_hold_sends_reasons():
    from cqc_lem.utilities import notifications as n
    with patch.object(n, "get_user_email", return_value="a@b.c"), \
         patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as send:
        assert n.notify_authenticity_hold(1, "DM", 20, ["too <generic>"])
    assert "too &lt;generic&gt;" in send.call_args.args[2]


def test_notify_authenticity_hold_no_email_is_false():
    from cqc_lem.utilities import notifications as n
    with patch.object(n, "get_user_email", return_value=None):
        assert n.notify_authenticity_hold(1, "DM", 20, []) is False
