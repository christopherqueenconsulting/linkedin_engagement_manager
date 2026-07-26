"""Follower & audience telemetry — label parsing and growth derivation (issue #627)."""

from datetime import datetime

import pytest

from cqc_lem.utilities.audience_stats import (build_activity_series, build_follower_series,
                                              follower_growth, parse_connection_count,
                                              parse_follower_count, parse_labeled_count,
                                              parse_profile_views, parse_search_appearances)

pytestmark = pytest.mark.unit


class TestLabelParsing:
    @pytest.mark.parametrize("text,expected", [
        ("1,234 followers", 1234),
        ("3.2K followers", 3200),
        ("1.1M followers", 1_100_000),
        ("7 followers", 7),
        ("1 follower", 1),
        ("Followers\n2,048", 2048),
    ])
    def test_follower_shapes(self, text, expected):
        assert parse_follower_count(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("500+ connections", 500),
        ("312 connections", 312),
        ("1 connection", 1),
    ])
    def test_connection_shapes(self, text, expected):
        assert parse_connection_count(text) == expected

    def test_analytics_card_layout(self):
        # LinkedIn stacks the number ABOVE its caption on the analytics cards.
        page = "48\nProfile views\nPast 90 days\n12\nSearch appearances\nPrevious week"
        assert parse_profile_views(page) == 48
        assert parse_search_appearances(page) == 12

    def test_label_first_layout_is_the_fallback(self):
        assert parse_profile_views("Profile views: 48") == 48

    def test_a_distant_number_does_not_bind_to_the_label(self):
        # Two line breaks away is another card's number, not this label's.
        assert parse_search_appearances("9,001\n\n\nSearch appearances") is None

    def test_missing_count_is_none_not_zero(self):
        # NULL means "not measured"; a 0 would read as a real collapse in a growth delta.
        assert parse_follower_count("Followers") is None
        assert parse_follower_count("") is None
        assert parse_follower_count(None) is None
        assert parse_profile_views("nothing to see") is None

    def test_unparseable_number_is_none(self):
        assert parse_labeled_count("..,, followers", r"followers?") is None

    def test_does_not_cross_wire_labels(self):
        # A page that only renders followers must not yield a connection count from the same digits.
        assert parse_connection_count("4,000 followers") is None

    def test_stacked_label_first_cards_do_not_share_one_number(self):
        # A wholly label-first page: every metric after the first would otherwise inherit the FIRST
        # card's number (288 recorded as search appearances, 4312 as connections) — a silently WRONG
        # count is worse than the NULL this module exists to prefer.
        analytics = "Profile views\n288\nSearch appearances\n88"
        assert parse_profile_views(analytics) == 288
        assert parse_search_appearances(analytics) == 88
        profile = "Followers\n4,312\nConnections\n500"
        assert parse_follower_count(profile) == 4312
        assert parse_connection_count(profile) == 500

    def test_value_first_neighbour_still_owns_the_next_number(self):
        # The mirror case: "followers" already HAS its number in front of it, so the 500 that
        # follows belongs to connections and must not be rejected as someone else's.
        assert parse_connection_count("4,312 followers\n500+ connections") == 500


class TestFollowerSeries:
    def test_collapses_same_day_captures_to_the_latest(self):
        rows = [
            {"id": 2, "captured_at": datetime(2026, 7, 20, 23, 40), "follower_count": 110,
             "connection_count": None, "profile_views": None, "search_appearances": None},
            {"id": 1, "captured_at": datetime(2026, 7, 20, 6, 0), "follower_count": 100,
             "connection_count": None, "profile_views": None, "search_appearances": None},
        ]
        series = build_follower_series(rows)
        # A re-run is a CORRECTION, not another data point — summing is the bug this issue fixes
        # on the post-stats side, and it must not be reintroduced here.
        assert len(series) == 1
        assert series[0]["follower_count"] == 110

    def test_sorted_ascending_and_keeps_nulls(self):
        rows = [
            {"id": 2, "captured_at": datetime(2026, 7, 21), "follower_count": None,
             "connection_count": 500, "profile_views": 9, "search_appearances": None},
            {"id": 1, "captured_at": datetime(2026, 7, 20), "follower_count": 100,
             "connection_count": 499, "profile_views": None, "search_appearances": 4},
        ]
        series = build_follower_series(rows)
        assert [p["date"] for p in series] == ["2026-07-20", "2026-07-21"]
        assert series[1]["follower_count"] is None
        assert series[1]["profile_views"] == 9

    def test_ignores_rows_without_a_usable_date(self):
        assert build_follower_series([None, {}, {"captured_at": "not-a-date"}]) == []

    def test_accepts_iso_string_timestamps(self):
        series = build_follower_series([{"id": 1, "captured_at": "2026-07-20T23:40:00Z",
                                        "follower_count": 42}])
        assert series[0]["date"] == "2026-07-20" and series[0]["follower_count"] == 42


def _row(day: int, followers, **extra):
    base = {"id": day, "captured_at": datetime(2026, 7, day, 23, 40), "follower_count": followers,
            "connection_count": None, "profile_views": None, "search_appearances": None}
    base.update(extra)
    return base


class TestFollowerGrowth:
    def test_deltas_measured_from_the_window_baseline(self):
        rows = [_row(1, 1000), _row(20, 1050), _row(26, 1100)]
        now = datetime(2026, 7, 26, 12, 0)
        growth = follower_growth(rows, now=now)
        assert growth["follower_count"] == 1100
        # 7-day baseline = newest snapshot on/before Jul 19 → Jul 1's 1000.
        assert growth["deltas"]["7"]["delta"] == 100
        assert growth["deltas"]["7"]["from_date"] == "2026-07-01"
        assert growth["deltas"]["30"] is None          # history doesn't reach back 30 days
        assert growth["samples"] == 3

    def test_pct_is_relative_to_the_baseline(self):
        growth = follower_growth([_row(1, 200), _row(26, 250)], now=datetime(2026, 7, 26))
        assert growth["deltas"]["7"]["pct"] == pytest.approx(0.25)

    def test_no_baseline_yields_no_delta_not_explosive_growth(self):
        # One snapshot: a delta against itself would read as +1100 from zero.
        growth = follower_growth([_row(26, 1100)], now=datetime(2026, 7, 26))
        assert growth["deltas"] == {"7": None, "30": None}
        assert growth["follower_count"] == 1100

    def test_negative_delta_is_preserved(self):
        growth = follower_growth([_row(1, 1200), _row(26, 1150)], now=datetime(2026, 7, 26))
        assert growth["deltas"]["7"]["delta"] == -50

    def test_latest_non_null_wins_per_signal(self):
        rows = [_row(20, 900, profile_views=48, search_appearances=12),
                _row(26, 950)]           # newest capture couldn't read the analytics surface
        growth = follower_growth(rows, now=datetime(2026, 7, 26))
        assert growth["follower_count"] == 950
        assert growth["profile_views"] == 48
        assert growth["search_appearances"] == 12

    def test_empty_history(self):
        growth = follower_growth([], now=datetime(2026, 7, 26))
        assert growth["follower_count"] is None and growth["series"] == []
        assert growth["deltas"]["30"] is None

    def test_baseline_skips_days_with_no_follower_reading(self):
        rows = [_row(1, 1000), _row(18, None), _row(26, 1100)]
        growth = follower_growth(rows, now=datetime(2026, 7, 26))
        assert growth["deltas"]["7"]["from"] == 1000


class TestActivitySeries:
    def test_buckets_action_types_per_day(self):
        rows = [
            {"date": datetime(2026, 7, 20).date(), "action_type": "comment", "count": 5},
            {"date": datetime(2026, 7, 20).date(), "action_type": "post", "count": 1},
            {"date": datetime(2026, 7, 21).date(), "action_type": "dm", "count": 2},
        ]
        series = build_activity_series(rows)
        assert series[0] == {"date": "2026-07-20", "posts": 1, "comments": 5, "replies": 0, "dms": 0}
        assert series[1]["dms"] == 2

    def test_ignores_unknown_action_types_and_bad_rows(self):
        rows = [None, {"date": None, "action_type": "post", "count": 3},
                {"date": datetime(2026, 7, 20).date(), "action_type": "engaged", "count": 9}]
        series = build_activity_series(rows)
        assert series == [{"date": "2026-07-20", "posts": 0, "comments": 0, "replies": 0, "dms": 0}]
