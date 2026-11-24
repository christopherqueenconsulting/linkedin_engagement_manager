"""The comment-candidate pool (issue #2635, Phase 1): flag-off no-op, safety skip, dedup, ranking."""

from unittest.mock import patch

import pytest

from cqc_lem.utilities import comment_candidates as cc

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.comment_candidates."
_SAFE = "Great point about retries in queue workers; we saw the same with idempotency keys."


def _record(**kw):
    args = dict(user_id=1, key="feedurn://urn:li:activity:1", content=_SAFE, author="Ann",
                source=cc.SOURCE_FEED, prefs={})
    args.update(kw)
    return cc.record_comment_candidate(**args)


def _on(**over):
    """Patch every collaborator: flag on, nothing commented yet, insert succeeds."""
    base = {"flag_enabled": True, "has_commented_post": False,
            "has_user_commented_on_post_url": False, "insert_comment_candidate": True}
    base.update(over)
    stack = [patch(_M + name, return_value=value) for name, value in base.items()]
    return stack


class _Ctx:
    def __init__(self, **over):
        self.patches = _on(**over)

    def __enter__(self):
        self.mocks = [p.start() for p in self.patches]
        return {p.attribute: m for p, m in zip(self.patches, self.mocks)}

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


def test_flag_off_is_a_no_op_before_any_read():
    with _Ctx(flag_enabled=False) as m:
        assert _record() is False
    m["has_commented_post"].assert_not_called()
    m["insert_comment_candidate"].assert_not_called()


def test_a_clean_post_is_inserted_with_components():
    with _Ctx() as m:
        assert _record(post_url="https://x/y", age_minutes=30, relevant=True) is True
    args, kwargs = m["insert_comment_candidate"].call_args
    assert args[:3] == (1, "feedurn://urn:li:activity:1", "feed")
    assert kwargs["post_url"] == "https://x/y"
    assert set(kwargs["components"]) == {"roster", "topic", "recency"}


def test_a_scam_post_is_skipped():
    with _Ctx() as m, patch(_M + "classify_comment") as verdict:
        verdict.return_value.is_safe = False
        assert _record(content="send crypto now") is False
    m["insert_comment_candidate"].assert_not_called()


def test_min_reactions_skips_a_thin_post_but_not_a_zero_floor():
    with _Ctx() as m:
        assert _record(prefs={"min_reactions": 10}, reactions=3) is False
        m["insert_comment_candidate"].assert_not_called()
        assert _record(prefs={"min_reactions": 10}, reactions=10) is True
        assert _record(prefs={"min_reactions": 0}, reactions=0) is True


@pytest.mark.parametrize("ledger", ["has_commented_post", "has_user_commented_on_post_url"])
def test_an_already_commented_post_is_never_pooled(ledger):
    with _Ctx(**{ledger: True}) as m:
        assert _record() is False
    m["insert_comment_candidate"].assert_not_called()


def test_a_repeat_scan_writes_nothing_new():
    with _Ctx(insert_comment_candidate=False):
        assert _record() is False


def test_empty_key_is_skipped():
    with _Ctx() as m:
        assert _record(key="") is False
    m["insert_comment_candidate"].assert_not_called()


def test_a_pool_fault_never_raises():
    with _Ctx() as m:
        m["insert_comment_candidate"].side_effect = RuntimeError("db down")
        assert _record() is False


def test_ranking_prefers_roster_then_topic_then_recency():
    roster, _ = cc.candidate_score(cc.SOURCE_ROSTER, True, 0.0, 30)
    on_topic_feed, _ = cc.candidate_score(cc.SOURCE_FEED, True, 0.0, 30)
    off_topic_feed, _ = cc.candidate_score(cc.SOURCE_FEED, False, 0.0, 30)
    stale_feed, _ = cc.candidate_score(cc.SOURCE_FEED, True, 0.0, 3000)
    assert roster > on_topic_feed > off_topic_feed
    assert on_topic_feed > stale_feed


def test_focus_affinity_lifts_an_otherwise_off_topic_post_and_unknown_age_is_mid():
    base, _ = cc.candidate_score(cc.SOURCE_FEED, False, 0.0, None)
    lifted, comps = cc.candidate_score(cc.SOURCE_FEED, False, 0.8, None)
    assert lifted > base
    assert comps["recency"] == 0.4


def test_report_emits_pool_size_only_when_something_was_written():
    with patch(_M + "track_comment_candidates") as track, \
            patch(_M + "count_comment_candidates", return_value=42):
        cc.report_candidate_pool(1, cc.SOURCE_FEED, 0)
        track.assert_not_called()
        cc.report_candidate_pool(1, cc.SOURCE_FEED, 3)
        track.assert_called_once_with(1, "feed", 3, 42)


def test_report_never_raises():
    with patch(_M + "track_comment_candidates", side_effect=RuntimeError("x")), \
            patch(_M + "count_comment_candidates", return_value=None):
        cc.report_candidate_pool(1, cc.SOURCE_ROSTER, 1)
