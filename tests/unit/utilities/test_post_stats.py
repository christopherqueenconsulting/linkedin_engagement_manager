"""Unit tests for post-time recommendation and content-attribution ranking logic."""

import datetime as dt

import pytest

pytestmark = pytest.mark.unit

_NOW = dt.datetime(2026, 8, 1, 12, 0)


def _stat_row(scheduled_time, reactions=0, comments=0, reposts=0, archetype=None,
              hook_style=None, fmt=None, topic=None, buyer_stage=None, impressions=None):
    """A `db.get_post_engagement_rows` tuple."""
    return (scheduled_time, reactions, comments, reposts, archetype, hook_style, fmt, topic,
            buyer_stage, impressions)


class TestEngagementScore:
    def test_weights_comments_and_reposts(self):
        from cqc_lem.utilities.post_stats import engagement_score
        assert engagement_score(10, 0, 0) == 10
        assert engagement_score(0, 5, 0) == 10          # comments ×2
        assert engagement_score(0, 0, 3) == 6           # reposts ×2


class TestEngagementRate:
    def test_normalizes_by_impressions(self):
        from cqc_lem.utilities.post_stats import engagement_rate
        # score = 20 + 2*10 + 2*5 = 50 over 1000 impressions
        assert engagement_rate(20, 10, 5, 1000) == pytest.approx(0.05)

    def test_none_without_impressions(self):
        from cqc_lem.utilities.post_stats import engagement_rate
        assert engagement_rate(20, 10, 5, None) is None
        assert engagement_rate(20, 10, 5, 0) is None

    def test_small_reach_post_beats_big_reach_post(self):
        """The whole point of B3: 30 signals on 300 views outranks 100 on 100k views."""
        from cqc_lem.utilities.post_stats import engagement_rate
        assert engagement_rate(30, 0, 0, 300) > engagement_rate(100, 0, 0, 100_000)


class TestRecencyWeight:
    def test_halves_every_half_life(self):
        from cqc_lem.utilities.post_stats import recency_weight
        assert recency_weight(_NOW - dt.timedelta(days=30), now=_NOW,
                              half_life_days=30) == pytest.approx(0.5)
        assert recency_weight(_NOW - dt.timedelta(days=60), now=_NOW,
                              half_life_days=30) == pytest.approx(0.25)

    def test_recent_and_future_are_unweighted(self):
        from cqc_lem.utilities.post_stats import recency_weight
        assert recency_weight(_NOW, now=_NOW) == 1.0
        assert recency_weight(_NOW + dt.timedelta(days=5), now=_NOW) == 1.0

    def test_missing_or_mismatched_timestamp_degrades_to_one(self):
        from cqc_lem.utilities.post_stats import recency_weight
        aware = dt.datetime(2026, 7, 1, 9, 0, tzinfo=dt.timezone.utc)
        assert recency_weight(None, now=_NOW) == 1.0
        assert recency_weight(aware, now=_NOW) == 1.0   # naive vs aware → no weighting
        assert 0.0 < recency_weight(aware) <= 1.0       # default `now` matches the row's tz


class TestRecommendPostTimes:
    def _row(self, weekday, hour, reactions, comments):
        # 2026-07-06 is a Monday; add days to hit the desired weekday
        base = dt.datetime(2026, 7, 6, hour, 0)  # Monday
        return (base + dt.timedelta(days=weekday), reactions, comments, 0)

    def test_empty_below_min_posts(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        assert recommend_post_times([self._row(0, 9, 5, 1)], min_posts=3) == []

    def test_ranks_best_bucket_first(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        rows = [
            self._row(2, 16, 50, 10),   # Wed 16:00 — high
            self._row(2, 16, 40, 8),
            self._row(0, 9, 5, 0),      # Mon 09:00 — low
            self._row(0, 9, 6, 1),
        ]
        recs = recommend_post_times(rows, top_n=2, min_posts=3)
        assert recs[0]["weekday"] == "Wednesday" and recs[0]["hour"] == 16
        assert recs[0]["avg_engagement"] > recs[1]["avg_engagement"]

    def test_none_scheduled_time_skipped(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        rows = [(None, 100, 100, 0)] + [self._row(3, 17, 20, 5) for _ in range(3)]
        recs = recommend_post_times(rows, min_posts=3)
        assert recs and recs[0]["weekday"] == "Thursday"

    def test_impression_normalized_when_every_row_has_impressions(self):
        """Raw counts favor the wide-reach slot; engagement RATE flips it to the efficient one."""
        from cqc_lem.utilities.post_stats import METRIC_RATE, recommend_post_times
        rows = [
            # Mon 09:00 — big raw numbers, poor rate (100/50_000 = 0.002)
            _stat_row(dt.datetime(2026, 7, 27, 9, 0), reactions=100, impressions=50_000),
            _stat_row(dt.datetime(2026, 7, 20, 9, 0), reactions=100, impressions=50_000),
            # Tue 10:00 — small raw numbers, strong rate (30/500 = 0.06)
            _stat_row(dt.datetime(2026, 7, 28, 10, 0), reactions=30, impressions=500),
            _stat_row(dt.datetime(2026, 7, 21, 10, 0), reactions=30, impressions=500),
        ]
        recs = recommend_post_times(rows, top_n=2, min_posts=3, now=_NOW)
        assert recs[0]["weekday"] == "Tuesday" and recs[0]["hour"] == 10
        assert recs[0]["metric"] == METRIC_RATE
        assert recs[0]["avg_engagement"] == pytest.approx(0.06)

    def test_falls_back_to_counts_when_impressions_incomplete(self):
        from cqc_lem.utilities.post_stats import METRIC_COUNT, recommend_post_times
        rows = [
            _stat_row(dt.datetime(2026, 7, 27, 9, 0), reactions=100, impressions=50_000),
            _stat_row(dt.datetime(2026, 7, 20, 9, 0), reactions=100, impressions=None),
            _stat_row(dt.datetime(2026, 7, 28, 10, 0), reactions=30, impressions=500),
            _stat_row(dt.datetime(2026, 7, 21, 10, 0), reactions=30, impressions=500),
        ]
        recs = recommend_post_times(rows, top_n=2, min_posts=3, now=_NOW)
        assert recs[0]["weekday"] == "Monday" and recs[0]["metric"] == METRIC_COUNT
        assert recs[0]["avg_engagement"] == pytest.approx(100.0)

    def test_recent_slot_outranks_equal_but_stale_slot(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        stale = [_stat_row(dt.datetime(2026, 1, 5, 9, 0) + dt.timedelta(days=7 * i), reactions=40)
                 for i in range(2)]                                        # Mondays, ~7 months old
        fresh = [_stat_row(dt.datetime(2026, 7, 21, 10, 0) - dt.timedelta(days=7 * i), reactions=40)
                 for i in range(2)]                                        # Tuesdays, ~2 weeks old
        recs = recommend_post_times(stale + fresh, top_n=2, min_posts=3, now=_NOW)
        assert recs[0]["weekday"] == "Tuesday"
        # Same average engagement either way — recency support is what separates them.
        assert recs[0]["avg_engagement"] == recs[1]["avg_engagement"] == pytest.approx(40.0)
        assert recs[0]["score"] > recs[1]["score"] and recs[0]["support"] > recs[1]["support"]

    def test_single_stale_post_does_not_top_a_sampled_slot(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        rows = [
            _stat_row(dt.datetime(2026, 1, 7, 15, 0), reactions=90),        # lone old Wed spike
            _stat_row(dt.datetime(2026, 7, 30, 11, 0), reactions=60),       # steady recent Thu
            _stat_row(dt.datetime(2026, 7, 23, 11, 0), reactions=60),
            _stat_row(dt.datetime(2026, 7, 16, 11, 0), reactions=60),
        ]
        recs = recommend_post_times(rows, min_posts=3, now=_NOW)
        assert recs[0]["weekday"] == "Thursday" and recs[0]["hour"] == 11

    def test_display_rounding_does_not_flip_order(self):
        """Ranking is on the unrounded score: these two slots tie at display precision, and the
        weekday tiebreak would otherwise put the WORSE (Monday) slot first.
        """
        from cqc_lem.utilities.post_stats import recommend_post_times
        rows = [
            _stat_row(dt.datetime(2026, 7, 27, 9, 0), reactions=1, impressions=1_000_000),
            _stat_row(dt.datetime(2026, 7, 28, 9, 0), reactions=2, impressions=1_000_000),
        ]
        recs = recommend_post_times(rows, min_posts=2, now=_NOW)
        assert [r["weekday"] for r in recs] == ["Tuesday", "Monday"]
        assert recs[0]["score"] == recs[1]["score"] == 0.0


class TestRankContentAttributes:
    def _rows(self):
        recent = dt.datetime(2026, 7, 25, 9, 0)
        return [
            # story/question/text/"AI onboarding" — the winner
            _stat_row(recent, reactions=40, comments=20, archetype="personal_story",
                      hook_style="question", fmt="text", topic="AI onboarding",
                      buyer_stage="awareness"),
            _stat_row(recent - dt.timedelta(days=3), reactions=36, comments=22,
                      archetype="personal_story", hook_style="question", fmt="text",
                      topic="AI onboarding", buyer_stage="awareness"),
            # tactical_list/bold_claim/carousel/"hiring" — the loser
            _stat_row(recent - dt.timedelta(days=1), reactions=5, comments=1,
                      archetype="tactical_list", hook_style="bold_claim", fmt="carousel",
                      topic="hiring", buyer_stage="consideration"),
            _stat_row(recent - dt.timedelta(days=5), reactions=4, comments=0,
                      archetype="tactical_list", hook_style="bold_claim", fmt="carousel",
                      topic="hiring", buyer_stage="consideration"),
        ]

    def test_ranks_every_attribute_best_first(self):
        from cqc_lem.utilities.post_stats import rank_content_attributes
        ranking = rank_content_attributes(self._rows(), now=_NOW)
        assert [e["key"] for e in ranking["archetype"]] == ["personal_story", "tactical_list"]
        assert [e["key"] for e in ranking["hook_style"]] == ["question", "bold_claim"]
        assert [e["key"] for e in ranking["format"]] == ["text", "carousel"]
        assert [e["key"] for e in ranking["topic"]] == ["AI onboarding", "hiring"]
        assert [e["key"] for e in ranking["buyer_stage"]] == ["awareness", "consideration"]
        winner = ranking["archetype"][0]
        assert winner["samples"] == 2 and winner["score"] > 0

    def test_impression_normalized_ranking_flips_raw_count_order(self):
        from cqc_lem.utilities.post_stats import METRIC_RATE, rank_content_attributes
        recent = dt.datetime(2026, 7, 25, 9, 0)
        rows = [
            _stat_row(recent, reactions=200, topic="broad", impressions=100_000),
            _stat_row(recent, reactions=200, topic="broad", impressions=100_000),
            _stat_row(recent, reactions=40, topic="niche", impressions=800),
            _stat_row(recent, reactions=40, topic="niche", impressions=800),
        ]
        ranking = rank_content_attributes(rows, attributes=["topic"], now=_NOW)
        assert [e["key"] for e in ranking["topic"]] == ["niche", "broad"]
        assert ranking["topic"][0]["metric"] == METRIC_RATE
        assert ranking["topic"][0]["avg_engagement"] == pytest.approx(0.05)

    def test_display_rounding_does_not_flip_order(self):
        """Same guarantee as the time buckets: the alphabetical tiebreak must not promote the
        weaker key just because both scores round to the same displayed value.
        """
        from cqc_lem.utilities.post_stats import rank_content_attributes
        recent = dt.datetime(2026, 7, 25, 9, 0)
        rows = [
            _stat_row(recent, reactions=1, topic="a_weaker", impressions=1_000_000),
            _stat_row(recent, reactions=2, topic="b_stronger", impressions=1_000_000),
        ]
        ranking = rank_content_attributes(rows, attributes=["topic"], now=_NOW)
        assert [e["key"] for e in ranking["topic"]] == ["b_stronger", "a_weaker"]
        assert ranking["topic"][0]["score"] == ranking["topic"][1]["score"] == 0.0

    def test_min_samples_drops_thin_keys_and_top_n_truncates(self):
        from cqc_lem.utilities.post_stats import rank_content_attributes
        rows = self._rows() + [
            _stat_row(dt.datetime(2026, 7, 26, 9, 0), reactions=500, archetype="one_off"),
        ]
        ranking = rank_content_attributes(rows, attributes=["archetype"], min_samples=2, now=_NOW)
        assert [e["key"] for e in ranking["archetype"]] == ["personal_story", "tactical_list"]
        capped = rank_content_attributes(rows, attributes=["archetype"], top_n=1, now=_NOW)
        assert len(capped["archetype"]) == 1 and capped["archetype"][0]["key"] == "one_off"

    def test_unknown_attribute_and_empty_rows(self):
        from cqc_lem.utilities.post_stats import rank_content_attributes
        assert rank_content_attributes([], attributes=["topic"]) == {"topic": []}
        assert rank_content_attributes(self._rows(), attributes=["nonsense"]) == {"nonsense": []}

    def test_accepts_a_one_shot_iterable(self):
        from cqc_lem.utilities.post_stats import rank_content_attributes
        ranking = rank_content_attributes(iter(self._rows()), now=_NOW)
        assert ranking["topic"][0]["key"] == "AI onboarding"
        assert ranking["hook_style"][0]["key"] == "question"


def _perf_row(post_id, scheduled_time, reactions=0, comments=0, reposts=0, saves=0,
              impressions=None, archetype=None, hook_style=None, fmt=None, topic=None,
              buyer_stage=None):
    """A `db.get_post_performance_rows` dict."""
    return {"post_id": post_id, "scheduled_time": scheduled_time, "reactions": reactions,
            "comments": comments, "reposts": reposts, "saves": saves, "impressions": impressions,
            "archetype": archetype, "hook_style": hook_style, "format": fmt, "topic": topic,
            "buyer_stage": buyer_stage}


class TestBuildPerformanceTable:
    def test_derives_engagement_and_rate_and_sorts_newest_first(self):
        from cqc_lem.utilities.post_stats import build_performance_table
        rows = [
            _perf_row(1, dt.datetime(2026, 7, 20, 9, 0), reactions=20, comments=10, reposts=5,
                      saves=3, impressions=1000, fmt="carousel", hook_style="question"),
            _perf_row(2, dt.datetime(2026, 7, 22, 9, 0), reactions=4, comments=1),
        ]
        table = build_performance_table(rows)
        assert [r["post_id"] for r in table] == [2, 1]          # newest first
        first = table[1]
        assert first["engagement"] == 50                         # 20 + 2*10 + 2*5
        assert first["engagement_rate"] == pytest.approx(0.05)
        assert first["scheduled_time"] == "2026-07-20T09:00:00"  # ISO-serialized
        assert first["format"] == "carousel" and first["saves"] == 3

    def test_rate_none_without_impressions(self):
        from cqc_lem.utilities.post_stats import build_performance_table
        table = build_performance_table([_perf_row(1, dt.datetime(2026, 7, 20, 9, 0),
                                                    reactions=5, comments=1)])
        assert table[0]["engagement_rate"] is None
        assert table[0]["impressions"] is None

    def test_zero_impressions_normalized_to_none(self):
        # 0 impressions is unknown/invalid to engagement_rate — the table must not show
        # `impressions: 0` beside a null rate.
        from cqc_lem.utilities.post_stats import build_performance_table
        table = build_performance_table([_perf_row(1, dt.datetime(2026, 7, 20, 9, 0),
                                                    reactions=5, comments=1, impressions=0)])
        assert table[0]["impressions"] is None
        assert table[0]["engagement_rate"] is None

    def test_ignores_empty_rows(self):
        from cqc_lem.utilities.post_stats import build_performance_table
        assert build_performance_table([None, {}]) == build_performance_table([])
        assert build_performance_table([]) == []


class TestBuildEngagementTrend:
    def test_buckets_by_day_ascending_and_sums_signals(self):
        from cqc_lem.utilities.post_stats import build_engagement_trend
        rows = [
            _perf_row(1, dt.datetime(2026, 7, 20, 9, 0), reactions=10, comments=2, impressions=500),
            _perf_row(2, dt.datetime(2026, 7, 20, 18, 0), reactions=6, comments=0, impressions=500),
            _perf_row(3, dt.datetime(2026, 7, 22, 9, 0), reactions=4, comments=1, impressions=200),
        ]
        trend = build_engagement_trend(rows)
        assert [p["date"] for p in trend] == ["2026-07-20", "2026-07-22"]  # ascending
        day0 = trend[0]
        assert day0["posts"] == 2
        assert day0["reactions"] == 16 and day0["comments"] == 2
        assert day0["impressions"] == 1000
        assert day0["engagement"] == 20                                    # (10+6) + 2*2
        assert day0["engagement_rate"] == pytest.approx(0.02)              # 20 / 1000

    def test_rate_none_when_any_post_that_day_lacks_impressions(self):
        from cqc_lem.utilities.post_stats import build_engagement_trend
        rows = [
            _perf_row(1, dt.datetime(2026, 7, 20, 9, 0), reactions=10, impressions=500),
            _perf_row(2, dt.datetime(2026, 7, 20, 18, 0), reactions=6),  # no impressions
        ]
        trend = build_engagement_trend(rows)
        assert trend[0]["engagement_rate"] is None
        assert trend[0]["impressions"] is None                            # incomplete → hidden

    def test_rate_none_when_a_post_that_day_has_zero_impressions(self):
        # A 0-impression post makes the day's impression total unknown, same as a missing one.
        from cqc_lem.utilities.post_stats import build_engagement_trend
        rows = [
            _perf_row(1, dt.datetime(2026, 7, 20, 9, 0), reactions=10, impressions=500),
            _perf_row(2, dt.datetime(2026, 7, 20, 18, 0), reactions=6, impressions=0),
        ]
        trend = build_engagement_trend(rows)
        assert trend[0]["engagement_rate"] is None
        assert trend[0]["impressions"] is None

    def test_skips_rows_without_a_timestamp(self):
        from cqc_lem.utilities.post_stats import build_engagement_trend
        rows = [_perf_row(1, None, reactions=5), _perf_row(2, dt.datetime(2026, 7, 20, 9, 0),
                                                           reactions=3)]
        trend = build_engagement_trend(rows)
        assert len(trend) == 1 and trend[0]["date"] == "2026-07-20"

    def test_empty_input(self):
        from cqc_lem.utilities.post_stats import build_engagement_trend
        assert build_engagement_trend([]) == []

    def test_ignores_empty_rows(self):
        from cqc_lem.utilities.post_stats import build_engagement_trend
        rows = [None, _perf_row(1, dt.datetime(2026, 7, 20, 9, 0), reactions=3)]
        trend = build_engagement_trend(rows)
        assert len(trend) == 1 and trend[0]["posts"] == 1

    def test_non_numeric_signal_degrades_to_zero(self):
        from cqc_lem.utilities.post_stats import build_engagement_trend
        rows = [_perf_row(1, dt.datetime(2026, 7, 20, 9, 0), reactions="oops", comments=2)]
        trend = build_engagement_trend(rows)
        assert trend[0]["reactions"] == 0 and trend[0]["comments"] == 2


class TestScrapeStatsTask:
    def test_records_each_post(self):
        from unittest.mock import MagicMock, patch
        # The stats sweep moved to `app.engagement.posting` (#1154).
        _RA = "cqc_lem.app.engagement.posting"
        with patch(f"{_RA}.time.sleep"), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[9, 10]), \
             patch(f"{_RA}.get_uncaptured_posted_post_ids", return_value=[]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_RA}._post_social_counts", return_value={"reactions": 12, "comments": 3}), \
             patch(f"{_RA}.get_shipped_variant_keys", return_value={}), \
             patch(f"{_RA}.record_post_stats") as rec, patch(f"{_RA}.quit_gracefully"):
            from cqc_lem.app.engagement.posting import auto_scrape_post_stats
            result = auto_scrape_post_stats.run(user_id=1)
        assert rec.call_count == 2 and "Scraped stats for 2" in result

    def test_rate_limited_login_defers_without_error_log(self):
        """Issue #1945: an unsolvable-challenge cooldown must degrade to a WARNING, not an ERROR.

        get_current_profile already warned once at the shared session gate, so this task
        re-logging LinkedInRateLimited as an ERROR filed a fresh, ungrouped PostHog $exception
        on every scheduled beat of a cooldown that was already expected to clear on its own.
        """
        from unittest.mock import patch

        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited

        _RA = "cqc_lem.app.engagement.posting"
        with patch(f"{_RA}.get_recent_posted_post_ids", return_value=[9]), \
             patch(f"{_RA}.get_uncaptured_posted_post_ids", return_value=[]), \
             patch(f"{_RA}.get_current_profile", side_effect=LinkedInRateLimited("cooling down")), \
             patch(f"{_RA}.log_warning") as warn, \
             patch(f"{_RA}.log_error") as err:
            from cqc_lem.app.engagement.posting import auto_scrape_post_stats
            result = auto_scrape_post_stats.run(user_id=1)
        assert result == "Skipped — rate limited"
        err.assert_not_called()
        warn.assert_called_once()
        assert warn.call_args.kwargs.get("user_id") == 1
        assert warn.call_args.kwargs.get("task_name") == "auto_scrape_post_stats"


def _variant_row(variant_key, scheduled_time, reactions=0, comments=0, reposts=0, impressions=None):
    """A `db.get_variant_outcome_rows` dict."""
    return {"variant_key": variant_key, "scheduled_time": scheduled_time, "reactions": reactions,
            "comments": comments, "reposts": reposts, "impressions": impressions}


class TestSelectVariantWinners:
    def test_winner_reflects_seeded_results(self):
        from cqc_lem.utilities.post_stats import select_variant_winners
        rows = [
            _variant_row("A", _NOW - dt.timedelta(days=2), reactions=5, comments=1),
            _variant_row("B", _NOW - dt.timedelta(days=2), reactions=40, comments=10),
        ]
        out = select_variant_winners(rows, now=_NOW)
        assert out["winner"] == "B"
        assert [r["key"] for r in out["ranking"]] == ["B", "A"]
        assert out["ranking"][0]["metric"] == "engagement"

    def test_rate_mode_when_all_have_impressions(self):
        """Small-reach variant with a far higher RATE wins over a big-reach loser."""
        from cqc_lem.utilities.post_stats import select_variant_winners
        rows = [
            _variant_row("small", _NOW - dt.timedelta(days=1), reactions=30, impressions=300),
            _variant_row("big", _NOW - dt.timedelta(days=1), reactions=100, impressions=100_000),
        ]
        out = select_variant_winners(rows, now=_NOW)
        assert out["winner"] == "small"
        assert out["ranking"][0]["metric"] == "engagement_rate"

    def test_recent_evidence_beats_stale_fluke(self):
        from cqc_lem.utilities.post_stats import select_variant_winners
        rows = [
            _variant_row("stale", _NOW - dt.timedelta(days=400), reactions=200),
            _variant_row("fresh", _NOW - dt.timedelta(days=1), reactions=30),
            _variant_row("fresh", _NOW - dt.timedelta(days=2), reactions=28),
        ]
        out = select_variant_winners(rows, now=_NOW)
        assert out["winner"] == "fresh"

    def test_min_samples_drops_thin_variants(self):
        from cqc_lem.utilities.post_stats import select_variant_winners
        rows = [
            _variant_row("A", _NOW - dt.timedelta(days=1), reactions=5),
            _variant_row("B", _NOW - dt.timedelta(days=1), reactions=99),
            _variant_row("B", _NOW - dt.timedelta(days=2), reactions=90),
        ]
        out = select_variant_winners(rows, min_samples=2, now=_NOW)
        assert [r["key"] for r in out["ranking"]] == ["B"]

    def test_top_n_truncates(self):
        from cqc_lem.utilities.post_stats import select_variant_winners
        rows = [_variant_row(k, _NOW - dt.timedelta(days=1), reactions=n)
                for k, n in (("A", 30), ("B", 20), ("C", 10))]
        out = select_variant_winners(rows, top_n=2, now=_NOW)
        assert [r["key"] for r in out["ranking"]] == ["A", "B"]

    def test_empty_and_keyless_rows(self):
        from cqc_lem.utilities.post_stats import select_variant_winners
        assert select_variant_winners([]) == {"winner": None, "ranking": []}
        out = select_variant_winners([None, {"variant_key": None, "reactions": 5},
                                      {"variant_key": "", "reactions": 5}])
        assert out == {"winner": None, "ranking": []}
