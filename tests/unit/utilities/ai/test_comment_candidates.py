"""Multi-candidate comment selection and the cross-family judge (#2751)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import comment_candidates as cc

pytestmark = pytest.mark.unit


def _resp(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class TestJudgePick:
    def test_picks_the_named_candidate_on_the_judge_tier(self):
        call = MagicMock(return_value=_resp('{"pick": 2, "reason": "plainer"}'))
        assert cc.judge_pick(["a", "b", "c"], "post", call) == 1
        assert call.call_args.kwargs["model"] == "lem-judge"

    def test_single_candidate_skips_the_call(self):
        call = MagicMock()
        assert cc.judge_pick(["a"], "post", call) == 0
        call.assert_not_called()

    @pytest.mark.parametrize("raw", ["no json", '{"pick": 9}', '{"pick": "x"}', None, '{"pick": 0}'])
    def test_unreadable_reply_keeps_the_first(self, raw):
        assert cc.judge_pick(["a", "b"], "post", MagicMock(return_value=_resp(raw))) == 0

    def test_judge_failure_fails_open(self):
        assert cc.judge_pick(["a", "b"], "post", MagicMock(side_effect=RuntimeError("x"))) == 0


class TestSelectCandidate:
    def test_judge_chooses_among_clean_drafts_only(self):
        drafts = iter(["bad", "good1", "good2"])
        seen = {}

        def pick(clean):
            seen["clean"] = list(clean)
            return 1

        out = cc.select_candidate(lambda: next(drafts), lambda d: d != "bad", pick)
        assert seen["clean"] == ["good1", "good2"]
        assert out == "good2"

    def test_one_clean_draft_skips_the_judge(self):
        drafts = iter(["bad", "good", "bad2"])
        pick = MagicMock()
        assert cc.select_candidate(lambda: next(drafts), lambda d: d == "good", pick) == "good"
        pick.assert_not_called()

    def test_no_clean_draft_returns_first_for_the_normal_gate_path(self):
        drafts = iter(["x", "y", "z"])
        assert cc.select_candidate(lambda: next(drafts), lambda d: False, MagicMock()) == "x"

    def test_nothing_drafted_is_none(self):
        assert cc.select_candidate(lambda: None, lambda d: True, MagicMock()) is None

    def test_draws_candidate_count(self):
        draw = MagicMock(return_value="t")
        cc.select_candidate(draw, lambda d: False, MagicMock())
        assert draw.call_count == cc.CANDIDATE_COUNT


def test_enabled_reads_the_flag():
    with patch.object(cc, "flag_enabled", return_value=True) as fe:
        assert cc.enabled(7) is True
    assert fe.call_args.args[0] == "comment-multi-candidate-enabled"
