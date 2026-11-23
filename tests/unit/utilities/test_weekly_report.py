"""Weekly 5-minute report payload (issue #2632, phase 1a)."""

import datetime as dt
from unittest.mock import patch

import pytest

from cqc_lem.utilities import weekly_report as wr

pytestmark = pytest.mark.unit

_END = dt.datetime(2026, 11, 20, 12, 0)
_START = _END - dt.timedelta(days=7)


def _row(impressions=1000, reactions=10, comments=30, own=26, reposts=0):
    return {"impressions": impressions, "reactions": reactions, "comments": comments,
            "own_comments": own, "reposts": reposts}


def _build(**over):
    kw = dict(week_start=_START, week_end=_END, this_week_rows=[_row()], last_week_rows=[],
              follower_snapshots=None, engager_icp_scores=None, pending_drafts=0)
    kw.update(over)
    return wr.build_weekly_report(**kw)


def _keys(report):
    return {m["key"] for m in report["not_measured"]}


class TestOrganicRate:
    def test_own_comments_are_excluded(self):
        # 26 of 30 own -> 4 organic comments: (10 + 2*4) / 1000
        r = _build(this_week_rows=[_row()])
        assert r["numbers"]["organic_engagement_rate"] == pytest.approx(0.018)

    def test_unknown_own_count_is_not_measured_never_unadjusted(self):
        r = _build(this_week_rows=[_row(own=None)])
        assert r["numbers"]["organic_engagement_rate"] is None
        assert "organic_engagement_rate" in _keys(r)


class TestMissingEverything:
    def test_all_none_renders_with_no_zeros(self):
        r = _build(this_week_rows=None, last_week_rows=None, pending_drafts=None)
        nums = r["numbers"]
        assert all(v is None for v in nums.values())
        assert all(v is None for v in r["deltas"].values())
        assert r["icp_share"] is None
        assert {"posts_published", "median_impressions", "organic_engagement_rate",
                "follower_delta", "icp_share", "icp_share_delta", "saves", "sends"} <= _keys(r)
        assert len(r["actions"]) == 1 and r["actions"][0]["key"] == "pending_drafts"

    def test_empty_week_is_zero_posts_but_unmeasured_rates(self):
        r = _build(this_week_rows=[])
        assert r["numbers"]["posts_published"] == 0
        assert r["numbers"]["median_impressions"] is None


class TestDeltas:
    def test_week_over_week(self):
        r = _build(this_week_rows=[_row(1000), _row(3000)], last_week_rows=[_row(500)])
        assert r["numbers"]["posts_published"] == 2
        assert r["numbers"]["median_impressions"] == 2000
        assert r["deltas"]["posts_published"] == 1
        assert r["deltas"]["median_impressions"] == 1500

    def test_follower_delta(self):
        snaps = [{"follower_count": 110, "captured_at": _END},
                 {"follower_count": 100, "captured_at": _START - dt.timedelta(hours=1)}]
        assert _build(follower_snapshots=snaps)["numbers"]["follower_delta"] == 10

    @pytest.mark.parametrize("snaps", [
        None, [],
        [{"follower_count": None, "captured_at": _END},
         {"follower_count": 100, "captured_at": _START - dt.timedelta(days=1)}],
        [{"follower_count": 110, "captured_at": _END}],
        [{"follower_count": 110, "captured_at": _END},
         {"follower_count": None, "captured_at": _START - dt.timedelta(days=1)}],
    ])
    def test_follower_delta_none_when_a_snapshot_is_missing(self, snaps):
        r = _build(follower_snapshots=snaps)
        assert r["numbers"]["follower_delta"] is None
        assert "follower_delta" in _keys(r)

    def test_aware_week_start(self):
        snaps = [{"follower_count": 5, "captured_at": _END},
                 {"follower_count": 3, "captured_at": _START - dt.timedelta(hours=1)}]
        aware = _START.replace(tzinfo=dt.timezone.utc)
        assert wr.follower_delta(snaps, aware) == 2


class TestIcpShare:
    def test_share_excludes_unread_engagers(self):
        s = wr.icp_share([80, 70, 40, None, None])
        assert (s["inside"], s["n"], s["engagers"]) == (2, 3, 5)
        assert s["share"] == pytest.approx(2 / 3)

    @pytest.mark.parametrize("scores", [None, [], [None, None]])
    def test_unreadable(self, scores):
        assert wr.icp_share(scores) is None

    def test_icp_week_over_week_is_always_not_measured(self):
        assert "icp_share_delta" in _keys(_build(engager_icp_scores=[90]))


class TestActions:
    def test_pending_drafts_required_and_first(self):
        r = _build(pending_drafts=12)
        assert r["actions"][0]["text"] == "12 drafts waiting: review"
        assert r["actions"][0]["link"] == wr.LINK_REVIEW_DRAFTS

    def test_singular(self):
        assert _build(pending_drafts=1)["actions"][0]["text"] == "1 draft waiting: review"

    def test_budget_is_three_actions_and_five_numbers(self):
        r = _build(this_week_rows=[], pending_drafts=2, engager_icp_scores=[10, 20])
        assert len(r["actions"]) <= wr.MAX_ACTIONS
        assert r["actions"][0]["key"] == "pending_drafts"
        assert len(r["numbers"]) <= 5

    def test_organic_unreadable_action(self):
        r = _build(this_week_rows=[_row(own=None)])
        assert "organic_unreadable" in {a["key"] for a in r["actions"]}


def test_load_reads_both_windows_and_builds():
    with patch.object(wr, "get_weekly_post_rows", return_value=[_row()]) as rows, \
         patch.object(wr, "get_follower_stats", return_value=[]), \
         patch.object(wr, "get_weekly_engager_icp_scores", return_value=[80]), \
         patch.object(wr, "count_pending_drafts", return_value=3):
        r = wr.load_weekly_report(1, now=_END)
    assert rows.call_count == 2
    assert r["numbers"]["posts_published"] == 1
    assert r["actions"][0]["text"] == "3 drafts waiting: review"
