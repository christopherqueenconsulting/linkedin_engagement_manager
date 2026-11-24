"""Voice exemplars in the comment and DM writers (issue #2627, Phase 1a)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_NEAREST = f"{_AI}._voice_exemplars.nearest_voice_exemplars"
_MARK = "How this user actually writes"


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = "{}"
    return p


def _dm_user_content(user_id, exemplars):
    from cqc_lem.utilities.ai import ai_helper
    with patch(_NEAREST, return_value=exemplars), \
         patch(f"{_AI}._call_llm", return_value=_resp("hi")) as call, \
         patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c):
        ai_helper.generate_nurture_dm("Sounds good", "interested", _profile(), user_id=user_id)
    return call.call_args.kwargs["messages"][1]["content"]


def test_dm_prompt_unchanged_without_exemplars_or_user():
    base = _dm_user_content(None, [])
    assert _MARK not in base
    assert _dm_user_content(7, []) == base


def test_dm_prompt_carries_exemplars_when_present():
    content = _dm_user_content(7, ["my own dm one", "my own dm two"])
    assert _MARK in content and "- my own dm one" in content


def test_dm_near_copy_of_exemplar_is_redrafted_once():
    from cqc_lem.utilities.ai import ai_helper
    with patch(_NEAREST, return_value=["my own dm one"]), \
         patch(f"{_AI}._call_llm", return_value=_resp("hi")) as call, \
         patch(f"{_AI}._humanize_text", side_effect=lambda c, **kw: c), \
         patch(f"{_AI}._voice_exemplars.too_close_to_exemplar",
               side_effect=[{"score": 0.9, "threshold": 0.82}, None]):
        ai_helper.generate_nurture_dm("Sounds good", "interested", _profile(), user_id=7)
    assert call.call_count == 2
    assert "nearly copied" in call.call_args.kwargs["messages"][0]["content"]


def _capture_comment(exemplars):
    from cqc_lem.utilities.ai import ai_helper
    seen = {}

    def fake_gate(draft, post, **kw):
        seen["kw"] = kw
        return None

    with patch(_NEAREST, return_value=exemplars), \
         patch(f"{_AI}._gated_comment", side_effect=fake_gate), \
         patch(f"{_AI}._call_llm", return_value=_resp("a comment")) as call, \
         patch(f"{_AI}.research_topic", return_value={}):
        ai_helper.generate_ai_response("A post about queues and retries.", _profile(), user_id=7)
    return seen["kw"], call


def test_comment_without_exemplars_has_no_block_and_empty_gate_list():
    kw, _ = _capture_comment([])
    assert kw["exemplars"] == []


def test_comment_with_exemplars_passes_them_to_the_gate():
    kw, _ = _capture_comment(["ex a", "ex b"])
    assert kw["exemplars"] == ["ex a", "ex b"]


def test_comment_prompt_block_is_exactly_the_flag_off_prompt_plus_block():
    from cqc_lem.utilities.ai import ai_helper, voice_exemplars as ve
    texts = {}
    for label, ex in (("off", []), ("on", ["ex a"])):
        with patch(_NEAREST, return_value=ex), \
             patch(f"{_AI}._gated_comment", side_effect=lambda d, p, **kw: d("") and None), \
             patch(f"{_AI}._call_llm", return_value=_resp("a comment")) as call, \
             patch(f"{_AI}.research_topic", return_value={}), \
             patch(f"{_AI}.random.uniform", return_value=0.5):
            ai_helper.generate_ai_response("A post about queues.", _profile(), user_id=7)
        texts[label] = call.call_args.kwargs["messages"][1]["content"][0]["text"]
    assert texts["on"] == texts["off"] + ve.exemplar_block(["ex a"])


def test_gate_adds_a_fix_entry_for_a_near_copy():
    from cqc_lem.utilities.ai import ai_helper
    fixes = []

    def draft(fix=""):
        fixes.append(fix)
        return "Retries without jitter just synchronise the failure for everyone involved."

    copied = {"score": 0.95, "threshold": 0.82}
    with patch(f"{_AI}._voice_exemplars.too_close_to_exemplar", return_value=copied), \
         patch(f"{_AI}._framework.comment_gate_max_attempts", return_value=2):
        out = ai_helper._gated_comment(draft, "post", user_id=7, exemplars=["ex"])
    assert out is None
    assert len(fixes) == 2 and "near-copy" in fixes[1]
