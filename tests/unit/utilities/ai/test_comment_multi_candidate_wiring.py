"""`_gated_comment` with COMMENT_MULTI_CANDIDATE (#2751)."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_CC = f"{_AI}._comment_candidates"
_TEXT = "Retries without jitter just synchronise the failure for everyone involved."


def _run(multi_arg, flag_on):
    from cqc_lem.utilities.ai import ai_helper
    calls = []

    def draft(fix=""):
        calls.append(fix)
        return _TEXT

    with patch(f"{_CC}.enabled", return_value=flag_on), \
         patch(f"{_AI}._framework.comment_gate_max_attempts", return_value=1), \
         patch(f"{_CC}.judge_pick", return_value=0) as judge:
        ai_helper._gated_comment(draft, "post", user_id=7, multi_candidate=multi_arg)
    return calls, judge


def test_off_path_draws_one_draft():
    calls, judge = _run(True, False)
    assert len(calls) == 1
    judge.assert_not_called()


def test_caller_not_opted_in_draws_one_draft_even_with_flag_on():
    calls, _ = _run(False, True)
    assert len(calls) == 1


def test_flag_on_draws_three_drafts():
    calls, _ = _run(True, True)
    assert len(calls) == 3
