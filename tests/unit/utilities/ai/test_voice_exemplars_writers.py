"""Voice exemplars in the seed, second-wave, reply, lead and group writers (issue #2627, Phase 1b)."""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import ai_helper, voice_exemplars as ve

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


def _seed(**kw):
    return ai_helper.generate_seed_comment("A post.", _profile(), **kw)


def _second_wave(**kw):
    return ai_helper.generate_second_wave_comment("A post.", _profile(), **kw)


def _thread_reply(**kw):
    return ai_helper.generate_thread_reply("A post.", "Nice point", _profile(), **kw)


def _followup(**kw):
    return ai_helper.generate_comment_reply_followup("Thanks!", _profile(), **kw)


def _lead(**kw):
    return ai_helper.generate_lead_response("What do you charge?", _profile(), channel="dm", **kw)


def _group(**kw):
    return ai_helper.generate_group_post(_profile(), group_name="AI Leaders", **kw)


WRITERS = [_seed, _second_wave, _thread_reply, _followup, _lead, _group]


def _run(writer, exemplars, user_id=7):
    with patch(_NEAREST, return_value=exemplars), \
         patch(f"{_AI}._call_llm", return_value=_resp("a fresh draft")) as call, \
         patch(f"{_AI}._humanize_comment", side_effect=lambda t, *a, **k: t), \
         patch(f"{_AI}._humanize_text", side_effect=lambda t, **k: t), \
         patch(f"{_AI}.lint_repaired", side_effect=lambda text, *a, **k: text), \
         patch(f"{_AI}._gated_comment", side_effect=lambda d, *a, **k: d("")), \
         patch(f"{_AI}.random.uniform", return_value=0.5):
        writer(user_id=user_id)
    return call


@pytest.mark.parametrize("writer", WRITERS)
def test_prompt_is_flag_off_prompt_plus_block(writer):
    off = _run(writer, []).call_args.kwargs["messages"][1]["content"]
    on = _run(writer, ["my own words"]).call_args.kwargs["messages"][1]["content"]
    assert _MARK not in off
    assert on == off + ve.exemplar_block(["my own words"])


@pytest.mark.parametrize("writer", WRITERS)
def test_no_user_means_no_lookup(writer):
    with patch(_NEAREST) as nearest, \
         patch(f"{_AI}._call_llm", return_value=_resp("x")), \
         patch(f"{_AI}._humanize_comment", side_effect=lambda t, *a, **k: t), \
         patch(f"{_AI}._humanize_text", side_effect=lambda t, **k: t), \
         patch(f"{_AI}.lint_repaired", side_effect=lambda text, *a, **k: text), \
         patch(f"{_AI}._gated_comment", side_effect=lambda d, *a, **k: d("")):
        writer(user_id=None)
    nearest.assert_not_called()


@pytest.mark.parametrize("writer", [_seed, _thread_reply, _followup, _lead, _group])
def test_near_copy_is_redrafted_once(writer):
    copied = {"score": 0.9, "threshold": 0.82}
    with patch(_NEAREST, return_value=["mine"]), \
         patch(f"{_AI}._call_llm", return_value=_resp("a draft")) as call, \
         patch(f"{_AI}._humanize_comment", side_effect=lambda t, *a, **k: t), \
         patch(f"{_AI}._humanize_text", side_effect=lambda t, **k: t), \
         patch(f"{_AI}.lint_repaired", side_effect=lambda text, *a, **k: text), \
         patch(f"{_AI}._voice_exemplars.too_close_to_exemplar", side_effect=[copied, None]):
        writer(user_id=7)
    assert call.call_count == 2
    assert "nearly copied" in call.call_args.kwargs["messages"][0]["content"]


def test_second_wave_hands_exemplars_to_the_gate():
    with patch(_NEAREST, return_value=["mine"]), \
         patch(f"{_AI}._call_llm", return_value=_resp("x")), \
         patch(f"{_AI}._gated_comment", return_value=None) as gate:
        _second_wave(user_id=7)
    assert gate.call_args.kwargs["exemplars"] == ["mine"]


def test_redraft_helper_does_not_nest_and_ignores_clean_text():
    redraft = MagicMock(return_value="new")
    with patch.object(ve, "too_close_to_exemplar", return_value={"score": 1}):
        assert ve.redraft_if_copied("t", ["e"], redraft, fix="x") == "t"
        redraft.assert_not_called()
        assert ve.redraft_if_copied("t", ["e"], redraft) == "new"
        redraft.assert_called_once_with(ve.COPY_FIX)
    with patch.object(ve, "too_close_to_exemplar", return_value=None):
        assert ve.redraft_if_copied("t", ["e"], redraft) == "t"
    assert ve.redraft_if_copied(None, ["e"], redraft) is None


def test_voice_context_empty_without_user():
    assert ve.voice_context(None, "q") == ([], "")
