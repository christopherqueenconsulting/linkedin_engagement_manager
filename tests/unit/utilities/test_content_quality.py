"""Unit tests for content-quality telemetry — issue #630 / D6.

Covers the per-item scoring, the batched self-similarity (including the self-match exclusion and the
lexical fallback), the period aggregation with its per-dimension denominators, the current-vs-prior
split, all three regression alert conditions, and the optional external detector's sampling + silent
no-op behaviour. Nothing here touches the network: the one embedding call is mocked.
"""

from datetime import date
from unittest.mock import patch

import pytest

from cqc_lem.utilities import content_quality as cq

pytestmark = pytest.mark.unit

CQ = "cqc_lem.utilities.content_quality"


def _row(**kw):
    """A scored row with everything measured, so each test only states what it cares about."""
    base = {"surface": cq.SURFACE_POST, "ref_id": "1", "shipped_on": "2026-07-26",
            "slop_hard": 0, "slop_warn": 0, "slop_score": 0.0, "similarity": 0.2,
            "similarity_measure": cq.MEASURE_EMBEDDING, "authenticity_score": 90,
            "hook_chars": 80, "hook_within_budget": True, "engagement_rate": 0.04,
            "impressions": 1000, "detector_score": None,
            "video_render_ok": None, "video_model_tier": None,
            "video_duration_seconds": None, "video_aspect_ratio": None,
            "video_asset_probe": None}
    base.update(kw)
    return base


class TestSlopSeverityScore:
    def test_hard_violations_weigh_triple(self):
        report = {"checked": True, "hard": [{"check": "banned_lexicon"}], "warnings": []}
        assert cq.slop_severity_score(report) == cq.SLOP_HARD_WEIGHT

    def test_warnings_add_one_each(self):
        report = {"checked": True, "hard": [], "warnings": [{"check": "burstiness"}, {"check": "em_dash_density"}]}
        assert cq.slop_severity_score(report) == 2 * cq.SLOP_WARN_WEIGHT

    def test_a_clean_checked_draft_scores_zero(self):
        assert cq.slop_severity_score({"checked": True, "hard": [], "warnings": []}) == 0.0

    def test_an_unchecked_draft_has_no_score_rather_than_zero(self):
        # A disabled lint must not report a clean week — that is the whole point of the None.
        assert cq.slop_severity_score({"checked": False, "hard": [], "warnings": []}) is None
        assert cq.slop_severity_score(None) is None


class TestScoreItem:
    def test_scores_the_slop_checks_that_fired(self):
        text = "Not just a tool, it's a system. Here's the kicker: we shipped it in a week."
        score = cq.score_item(surface=cq.SURFACE_POST, ref_id="7", text=text,
                              shipped_on="2026-07-26")
        assert score["slop_checked"] is True
        assert score["slop_hard"] == 2
        assert set(score["slop_checks"]) >= {"contrastive_frame", "tada_transition"}
        assert score["slop_score"] == 2 * cq.SLOP_HARD_WEIGHT

    def test_hook_is_graded_against_the_mobile_budget(self):
        long_hook = "x" * (cq.MOBILE_HOOK_MAX_CHARS + 5)
        over = cq.score_item(surface=cq.SURFACE_POST, ref_id="1", text=long_hook,
                             shipped_on="2026-07-26")
        under = cq.score_item(surface=cq.SURFACE_POST, ref_id="2", text="Short hook.\nBody.",
                             shipped_on="2026-07-26")
        assert over["hook_chars"] == cq.MOBILE_HOOK_MAX_CHARS + 5
        assert over["hook_within_budget"] is False
        assert under["hook_within_budget"] is True
        assert under["hook_budget"] == cq.MOBILE_HOOK_MAX_CHARS

    def test_passed_through_measurements_are_recorded(self):
        video = {"video_render_ok": True, "video_model_tier": "veo3.1",
                 "video_duration_seconds": 6, "video_aspect_ratio": "9:16",
                 "video_asset_probe": cq.VIDEO_PROBE_OK}
        score = cq.score_item(surface=cq.SURFACE_POST, ref_id="1", text="A plain sentence.",
                              shipped_on="2026-07-26", authenticity=77,
                              similarity={"score": 0.61, "measure": cq.MEASURE_EMBEDDING},
                              engagement_rate=0.012, impressions=500, detector=0.9,
                              video=video)
        assert score["authenticity_score"] == 77
        assert score["similarity"] == 0.61
        assert score["similarity_measure"] == cq.MEASURE_EMBEDDING
        assert score["engagement_rate"] == 0.012
        assert score["impressions"] == 500
        assert score["detector_score"] == 0.9
        assert score["detector_provider"] == cq.detector_provider()
        assert score["video_render_ok"] is True
        assert score["video_model_tier"] == "veo3.1"
        assert score["video_duration_seconds"] == 6
        assert score["video_aspect_ratio"] == "9:16"
        assert score["video_asset_probe"] == cq.VIDEO_PROBE_OK

    def test_unmeasured_dimensions_are_none_not_zero(self):
        score = cq.score_item(surface=cq.SURFACE_COMMENT, ref_id="9", text="A plain comment body.",
                              shipped_on="2026-07-26")
        assert score["authenticity_score"] is None
        assert score["similarity"] is None
        assert score["similarity_measure"] == cq.MEASURE_NONE
        assert score["engagement_rate"] is None
        assert score["impressions"] is None
        assert score["detector_score"] is None
        assert score["detector_provider"] is None

    def test_a_disabled_lint_reports_unscored_slop(self):
        with patch.dict("os.environ", {"SLOP_LINT_ENABLED": "false"}):
            score = cq.score_item(surface=cq.SURFACE_POST, ref_id="1",
                                  text="Here's the kicker: nothing.", shipped_on="2026-07-26")
        assert score["slop_checked"] is False
        assert score["slop_hard"] is None
        assert score["slop_score"] is None

    def test_the_lead_magnet_keyword_is_not_graded_as_bait(self):
        text = "We shipped the migration checklist.\nComment YES and I'll send it over."
        baited = cq.score_item(surface=cq.SURFACE_POST, ref_id="1", text=text,
                               shipped_on="2026-07-26")
        exempt = cq.score_item(surface=cq.SURFACE_POST, ref_id="1", text=text,
                               shipped_on="2026-07-26", exempt_keyword="YES")
        assert "bait_closer" in baited["slop_checks"]
        assert "bait_closer" not in exempt["slop_checks"]


class TestSimilarityReports:
    def test_no_history_means_unmeasured_not_unique(self):
        assert cq.similarity_reports(["some text here"], []) == [
            {"score": None, "measure": cq.MEASURE_NONE, "match": None}]

    def test_empty_input_returns_empty(self):
        assert cq.similarity_reports([], ["history"]) == []

    def test_an_items_own_text_is_excluded_from_its_history(self):
        # A shipped comment is already in the log it is compared against; matching itself would score
        # every single item at 1.0 and make the metric meaningless.
        text = "Latency went from four hundred milliseconds to ninety after the index change."
        with patch(f"{CQ}.embed_comments", return_value=None):
            reports = cq.similarity_reports([text], [text])
        assert reports == [{"score": None, "measure": cq.MEASURE_NONE, "match": None}]

    def test_self_exclusion_ignores_whitespace_and_case(self):
        text = "Same body different row"
        with patch(f"{CQ}.embed_comments", return_value=None):
            reports = cq.similarity_reports([text], ["  same   BODY different row  "])
        assert reports[0]["score"] is None

    def test_falls_back_to_token_overlap_when_embeddings_are_unavailable(self):
        with patch(f"{CQ}.embed_comments", return_value=None):
            reports = cq.similarity_reports(["alpha beta gamma delta"],
                                            ["alpha beta gamma delta epsilon", "nothing alike"])
        assert reports[0]["measure"] == cq.MEASURE_LEXICAL
        assert reports[0]["score"] == 1.0
        assert reports[0]["match"] == "alpha beta gamma delta epsilon"

    def test_uses_one_embedding_call_for_the_whole_batch(self):
        items = ["first body", "second body"]
        history = ["older one", "older two"]
        vectors = [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]]
        with patch(f"{CQ}.embed_comments", return_value=vectors) as embed:
            reports = cq.similarity_reports(items, history)
        assert embed.call_count == 1
        assert embed.call_args.args[0] == items + history
        assert [r["measure"] for r in reports] == [cq.MEASURE_EMBEDDING] * 2
        assert reports[0]["score"] == 1.0 and reports[0]["match"] == "older one"
        assert reports[1]["score"] == 1.0 and reports[1]["match"] == "older two"

    def test_duplicate_history_entries_are_collapsed(self):
        with patch(f"{CQ}.embed_comments", return_value=None) as embed:
            cq.similarity_reports(["body"], ["dupe", "dupe", "", None, "other"])
        assert embed.call_args.args[0] == ["body", "dupe", "other"]

    def test_history_is_capped_at_the_shared_limit(self):
        history = [f"entry number {i}" for i in range(cq.COMMENT_HISTORY_LIMIT + 20)]
        with patch(f"{CQ}.embed_comments", return_value=None) as embed:
            cq.similarity_reports(["body"], history)
        assert len(embed.call_args.args[0]) == cq.COMMENT_HISTORY_LIMIT + 1

    def test_a_blank_item_is_never_embedded_or_scored(self):
        with patch(f"{CQ}.embed_comments", return_value=None) as embed:
            reports = cq.similarity_reports(["   "], ["history entry"])
        assert reports[0]["score"] is None
        assert not embed.called  # an empty body would fail the batch call anyway


class TestSummarizeScores:
    def test_empty_window_measures_nothing(self):
        summary = cq.summarize_scores([])
        assert summary["items"] == 0
        assert summary["slop_score_avg"] is None
        assert summary["engagement_rate"] is None
        assert summary["similarity_avg"] is None

    def test_counts_by_surface(self):
        summary = cq.summarize_scores([
            _row(surface=cq.SURFACE_POST), _row(surface=cq.SURFACE_COMMENT),
            _row(surface=cq.SURFACE_COMMENT)])
        assert summary["items"] == 3
        assert summary["by_surface"] == {cq.SURFACE_POST: 1, cq.SURFACE_COMMENT: 2}

    def test_engagement_rate_is_impression_weighted(self):
        # 0.10 on 100 impressions and 0.01 on 9,900 must not average to ~5.5%: the small post is
        # 1% of the reach.
        summary = cq.summarize_scores([
            _row(engagement_rate=0.10, impressions=100),
            _row(engagement_rate=0.01, impressions=9900)])
        assert summary["engagement_rate"] == pytest.approx(0.0109, abs=1e-4)
        assert summary["engagement_rate_sample"] == 2
        assert summary["impressions"] == 10000

    def test_posts_without_impressions_are_excluded_from_the_rate(self):
        summary = cq.summarize_scores([
            _row(engagement_rate=0.04, impressions=1000),
            _row(engagement_rate=None, impressions=None)])
        assert summary["engagement_rate"] == 0.04
        assert summary["engagement_rate_sample"] == 1

    def test_a_zero_impression_row_is_not_a_zero_rate(self):
        summary = cq.summarize_scores([_row(engagement_rate=0.0, impressions=0)])
        assert summary["engagement_rate"] is None
        assert summary["engagement_rate_sample"] == 0

    def test_each_dimension_keeps_its_own_denominator(self):
        summary = cq.summarize_scores([
            _row(slop_score=3.0, slop_hard=1, similarity=None, authenticity_score=None),
            _row(slop_score=None, slop_hard=None, similarity=0.4, authenticity_score=80)])
        assert summary["slop_sample"] == 1 and summary["slop_score_avg"] == 3.0
        assert summary["slop_hard_rate"] == 1.0  # over the ONE row the lint actually ran on
        assert summary["similarity_sample"] == 1 and summary["similarity_avg"] == 0.4
        assert summary["authenticity_sample"] == 1 and summary["authenticity_avg"] == 80.0

    def test_hook_compliance_rate(self):
        summary = cq.summarize_scores([
            _row(hook_within_budget=True, hook_chars=100),
            _row(hook_within_budget=False, hook_chars=200),
            _row(hook_within_budget=None, hook_chars=None)])
        assert summary["hook_sample"] == 2
        assert summary["hook_budget_rate"] == 0.5
        assert summary["hook_chars_avg"] == 150.0

    def test_similarity_max_is_reported_next_to_the_mean(self):
        summary = cq.summarize_scores([_row(similarity=0.2), _row(similarity=0.9)])
        assert summary["similarity_avg"] == 0.55
        assert summary["similarity_max"] == 0.9

    def test_the_dominant_similarity_measure_is_reported(self):
        summary = cq.summarize_scores([
            _row(similarity=0.2, similarity_measure=cq.MEASURE_EMBEDDING),
            _row(similarity=0.3, similarity_measure=cq.MEASURE_EMBEDDING),
            _row(similarity=0.4, similarity_measure=cq.MEASURE_LEXICAL),
            _row(similarity=None, similarity_measure=cq.MEASURE_NONE)])
        assert summary["similarity_measure"] == cq.MEASURE_EMBEDDING
        assert summary["similarity_measures"] == {cq.MEASURE_EMBEDDING: 2, cq.MEASURE_LEXICAL: 1}

    def test_the_mean_never_mixes_two_similarity_scales(self):
        # Each surface embeds in its own batch, so ONE failed lem-embedding call leaves a lexical
        # minority inside an otherwise-cosine period. Folding it into the mean would move
        # similarity_avg by the gap between the scales while the dominant LABEL stayed 'embedding' —
        # which is exactly the move the cross-period guard would then wave through.
        summary = cq.summarize_scores([
            _row(similarity=0.2, similarity_measure=cq.MEASURE_EMBEDDING),
            _row(similarity=0.3, similarity_measure=cq.MEASURE_EMBEDDING),
            _row(similarity=0.9, similarity_measure=cq.MEASURE_LEXICAL)])
        assert summary["similarity_measure"] == cq.MEASURE_EMBEDDING
        assert summary["similarity_sample"] == 2
        assert summary["similarity_avg"] == 0.25
        assert summary["similarity_max"] == 0.3

    def test_a_wholly_lexical_period_is_summarized_on_its_own_scale(self):
        summary = cq.summarize_scores([
            _row(similarity=0.5, similarity_measure=cq.MEASURE_LEXICAL),
            _row(similarity=0.7, similarity_measure=cq.MEASURE_LEXICAL)])
        assert summary["similarity_measure"] == cq.MEASURE_LEXICAL
        assert summary["similarity_sample"] == 2 and summary["similarity_avg"] == 0.6

    def test_no_measured_similarity_has_no_measure(self):
        assert cq.summarize_scores([_row(similarity=None)])["similarity_measure"] is None

    def test_unparseable_values_are_dropped_not_crashed_on(self):
        summary = cq.summarize_scores([_row(similarity="not-a-number"), _row(similarity=0.5)])
        assert summary["similarity_sample"] == 1 and summary["similarity_avg"] == 0.5

    def test_an_unparseable_engagement_row_is_skipped(self):
        summary = cq.summarize_scores([_row(engagement_rate="oops", impressions="also-oops"),
                                       _row(engagement_rate=0.03, impressions=500)])
        assert summary["engagement_rate_sample"] == 1
        assert summary["engagement_rate"] == 0.03


class TestSplitPeriods:
    def test_splits_into_two_equal_windows_ending_today(self):
        today = date(2026, 7, 26)
        rows = [_row(shipped_on="2026-07-26"), _row(shipped_on="2026-07-20"),
                _row(shipped_on="2026-07-19"), _row(shipped_on="2026-07-14")]
        current, prior = cq.split_periods(rows, 7, today=today)
        assert [r["shipped_on"] for r in current] == ["2026-07-26", "2026-07-20"]
        assert [r["shipped_on"] for r in prior] == ["2026-07-19", "2026-07-14"]

    def test_rows_older_than_both_windows_are_dropped(self):
        current, prior = cq.split_periods([_row(shipped_on="2026-06-01")], 7,
                                          today=date(2026, 7, 26))
        assert current == [] and prior == []

    def test_unreadable_dates_are_dropped(self):
        current, prior = cq.split_periods([_row(shipped_on=None), _row(shipped_on="nonsense")], 7,
                                          today=date(2026, 7, 26))
        assert current == [] and prior == []

    def test_accepts_date_objects(self):
        current, _ = cq.split_periods([_row(shipped_on=date(2026, 7, 25))], 7,
                                      today=date(2026, 7, 26))
        assert len(current) == 1

    def test_accepts_datetimes(self):
        from datetime import datetime
        current, _ = cq.split_periods([_row(shipped_on=datetime(2026, 7, 25, 14, 30))], 7,
                                      today=date(2026, 7, 26))
        assert len(current) == 1


class TestEvaluateAlerts:
    def _summary(self, **kw):
        base = {"slop_sample": 10, "slop_score_avg": 1.0, "similarity_sample": 10,
                "similarity_avg": 0.3, "similarity_measure": cq.MEASURE_EMBEDDING,
                "engagement_rate_sample": 10, "engagement_rate": 0.05}
        base.update(kw)
        return base

    def test_no_alerts_when_everything_is_steady(self):
        assert cq.evaluate_alerts(self._summary(), self._summary()) == []

    def test_slop_regression_fires_on_a_week_over_week_rise(self):
        alerts = cq.evaluate_alerts(self._summary(slop_score_avg=3.0), self._summary(slop_score_avg=1.0))
        assert [a["name"] for a in alerts] == [cq.ALERT_SLOP_REGRESSION]
        assert alerts[0]["delta"] == 2.0
        assert "model or prompt change" in alerts[0]["reason"]

    def test_slop_improvement_never_alerts(self):
        assert cq.evaluate_alerts(self._summary(slop_score_avg=0.2),
                                  self._summary(slop_score_avg=4.0)) == []

    def test_slop_regression_needs_a_real_sample_on_both_sides(self):
        thin_now = cq.evaluate_alerts(self._summary(slop_score_avg=3.0, slop_sample=2),
                                      self._summary(slop_score_avg=1.0))
        thin_prior = cq.evaluate_alerts(self._summary(slop_score_avg=3.0),
                                        self._summary(slop_score_avg=1.0, slop_sample=1))
        assert thin_now == [] and thin_prior == []

    def test_no_prior_baseline_never_alerts_on_a_regression(self):
        # "We have no baseline" must not read as "nothing changed" OR as a regression.
        alerts = cq.evaluate_alerts(self._summary(slop_score_avg=5.0),
                                    self._summary(slop_score_avg=None, slop_sample=0))
        assert [a["name"] for a in alerts] == []

    def test_engagement_floor_fires_without_a_prior_period(self):
        alerts = cq.evaluate_alerts(self._summary(engagement_rate=0.001),
                                    {"engagement_rate": None})
        assert [a["name"] for a in alerts] == [cq.ALERT_ENGAGEMENT_FLOOR]
        assert alerts[0]["threshold"] == cq.engagement_floor()

    def test_engagement_floor_respects_the_sample_minimum(self):
        assert cq.evaluate_alerts(self._summary(engagement_rate=0.001, engagement_rate_sample=1),
                                  self._summary()) == []

    def test_engagement_floor_is_reachable_at_the_default_posting_cadence(self):
        # Only POSTS carry impressions and DEFAULT_POSTS_PER_WEEK is 3, so gating this on the
        # piece-count minimum (5) would make the floor unreachable for every default-plan account —
        # an alert that can never fire reads as "engagement is fine".
        alerts = cq.evaluate_alerts(
            self._summary(engagement_rate=0.001, engagement_rate_sample=3), {})
        assert [a["name"] for a in alerts] == [cq.ALERT_ENGAGEMENT_FLOOR]
        assert alerts[0]["sample"] == 3

    def test_a_week_short_of_the_engagement_minimum_stays_quiet(self):
        assert cq.evaluate_alerts(self._summary(engagement_rate=0.001, engagement_rate_sample=2),
                                  {}) == []

    def test_an_unmeasured_engagement_rate_never_trips_the_floor(self):
        assert cq.evaluate_alerts(self._summary(engagement_rate=None), self._summary()) == []

    def test_similarity_creep_fires_when_the_writing_converges(self):
        alerts = cq.evaluate_alerts(self._summary(similarity_avg=0.42),
                                    self._summary(similarity_avg=0.30))
        assert [a["name"] for a in alerts] == [cq.ALERT_SIMILARITY_CREEP]
        assert "converging on one template" in alerts[0]["reason"]

    def test_similarity_creep_never_compares_across_measures(self):
        # A week the embedding endpoint was down is scored on the token-overlap scale; comparing the
        # two would read as a large move in whichever direction the scales differ.
        alerts = cq.evaluate_alerts(
            self._summary(similarity_avg=0.42, similarity_measure=cq.MEASURE_LEXICAL),
            self._summary(similarity_avg=0.30, similarity_measure=cq.MEASURE_EMBEDDING))
        assert alerts == []

    def test_similarity_creep_fires_when_both_weeks_are_lexical(self):
        alerts = cq.evaluate_alerts(
            self._summary(similarity_avg=0.42, similarity_measure=cq.MEASURE_LEXICAL),
            self._summary(similarity_avg=0.30, similarity_measure=cq.MEASURE_LEXICAL))
        assert [a["name"] for a in alerts] == [cq.ALERT_SIMILARITY_CREEP]

    def test_all_three_can_fire_together(self):
        alerts = cq.evaluate_alerts(
            self._summary(slop_score_avg=4.0, similarity_avg=0.5, engagement_rate=0.001),
            self._summary())
        assert [a["name"] for a in alerts] == [cq.ALERT_SLOP_REGRESSION,
                                               cq.ALERT_ENGAGEMENT_FLOOR,
                                               cq.ALERT_SIMILARITY_CREEP]

    def test_thresholds_are_configurable(self):
        with patch.dict("os.environ", {"CONTENT_QUALITY_SLOP_REGRESSION_DELTA": "5.0",
                                       "CONTENT_QUALITY_ENGAGEMENT_FLOOR": "0.9",
                                       "CONTENT_QUALITY_SIMILARITY_DELTA": "0.9"}):
            alerts = cq.evaluate_alerts(self._summary(slop_score_avg=3.0, similarity_avg=0.5),
                                        self._summary())
        assert [a["name"] for a in alerts] == [cq.ALERT_ENGAGEMENT_FLOOR]

    def test_a_percent_shaped_floor_is_clamped_to_a_share(self):
        with patch.dict("os.environ", {"CONTENT_QUALITY_ENGAGEMENT_FLOOR": "2"}):
            assert cq.engagement_floor() == 1.0


class TestQualityRollup:
    def test_reports_both_periods_and_the_deltas(self):
        today = date(2026, 7, 26)
        rows = ([_row(shipped_on="2026-07-25", slop_score=4.0) for _ in range(6)]
                + [_row(shipped_on="2026-07-15", slop_score=1.0) for _ in range(6)])
        rollup = cq.quality_rollup(rows, days=7, today=today)
        assert rollup["days"] == 7
        assert rollup["current"]["items"] == 6 and rollup["prior"]["items"] == 6
        assert rollup["deltas"]["slop_score_avg"] == 3.0
        assert [a["name"] for a in rollup["alerts"]] == [cq.ALERT_SLOP_REGRESSION]
        assert rollup["config"]["min_sample"] == cq.min_alert_sample()

    def test_an_empty_history_rolls_up_to_nothing_rather_than_alerting(self):
        rollup = cq.quality_rollup([], days=7, today=date(2026, 7, 26))
        assert rollup["current"]["items"] == 0
        assert rollup["alerts"] == []
        assert rollup["deltas"]["slop_score_avg"] is None

    def test_window_comes_from_env_when_not_passed(self):
        with patch.dict("os.environ", {"CONTENT_QUALITY_ROLLUP_DAYS": "3"}):
            assert cq.quality_rollup([], today=date(2026, 7, 26))["days"] == 3


class TestConfig:
    def test_telemetry_is_on_by_default_and_can_be_switched_off(self):
        assert cq.content_quality_enabled() is True
        with patch.dict("os.environ", {"CONTENT_QUALITY_TELEMETRY_ENABLED": "false"}):
            assert cq.content_quality_enabled() is False

    def test_nightly_window_looks_back_two_days(self):
        assert cq.window_days() == cq.DEFAULT_WINDOW_DAYS == 2

    def test_the_engagement_minimum_tracks_the_posting_cadence(self):
        assert cq.min_engagement_sample() == cq.DEFAULT_MIN_ENGAGEMENT_SAMPLE == 3
        assert cq.min_engagement_sample() < cq.min_alert_sample()

    def test_the_engagement_minimum_never_exceeds_the_general_one(self):
        # Lowering the general minimum must lower this too; it is a floor on a subset of the same
        # content, so it can never be the stricter of the two.
        with patch.dict("os.environ", {"CONTENT_QUALITY_MIN_SAMPLE": "1"}):
            assert cq.min_engagement_sample() == 1

    def test_the_engagement_minimum_can_be_set_explicitly(self):
        with patch.dict("os.environ", {"CONTENT_QUALITY_MIN_ER_SAMPLE": "6"}):
            assert cq.min_engagement_sample() == 6

    def test_garbage_env_values_fall_back_to_defaults(self):
        with patch.dict("os.environ", {"CONTENT_QUALITY_MIN_SAMPLE": "abc",
                                       "CONTENT_QUALITY_SIMILARITY_DELTA": "xyz"}):
            assert cq.min_alert_sample() == cq.DEFAULT_MIN_ALERT_SAMPLE
            assert cq.similarity_regression_delta() == cq.DEFAULT_SIMILARITY_REGRESSION_DELTA


class TestExternalDetector:
    def test_disabled_by_default(self):
        assert cq.detector_enabled() is False
        assert cq.detector_sampled(cq.SURFACE_POST, "1") is False
        assert cq.detector_score("some body text") is None

    def test_the_flag_alone_is_not_enough_without_a_key(self):
        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true"}, clear=False):
            assert cq.detector_enabled() is False

    def test_sampling_is_stable_for_the_same_item(self):
        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_SAMPLE_RATE": "1"}):
            assert cq.detector_sampled(cq.SURFACE_POST, "42") is True
            assert cq.detector_sampled(cq.SURFACE_POST, "42") is True
        assert cq.stable_fraction("a") == cq.stable_fraction("a")
        assert cq.stable_fraction("a") != cq.stable_fraction("b")

    def test_a_zero_rate_or_zero_cap_samples_nothing(self):
        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_SAMPLE_RATE": "0"}):
            assert cq.detector_sampled(cq.SURFACE_POST, "1") is False
        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_SAMPLE_RATE": "1", "AI_DETECTOR_DAILY_MAX": "0"}):
            assert cq.detector_sampled(cq.SURFACE_POST, "1") is False

    def test_a_missing_url_is_a_silent_no_op(self):
        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_URL": ""}):
            assert cq.detector_score("body") is None

    def test_parses_a_score_out_of_the_response(self):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ai_probability": 0.8123}

        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_URL": "https://example.test/score"}):
            with patch("requests.post", return_value=_Resp()) as post:
                assert cq.detector_score("body text") == 0.8123
        assert post.call_args.kwargs["json"]["text"] == "body text"

    def test_a_partial_rate_samples_only_some_items(self):
        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_SAMPLE_RATE": "0.1"}):
            sampled = [cq.detector_sampled(cq.SURFACE_COMMENT, str(i)) for i in range(200)]
        assert 0 < sum(sampled) < 200

    def test_a_non_numeric_score_is_ignored(self):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"score": "very likely"}

        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_URL": "https://example.test/score"}):
            with patch("requests.post", return_value=_Resp()):
                assert cq.detector_score("body") is None

    def test_a_score_outside_zero_to_one_is_clamped(self):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"score": 4.2}

        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_URL": "https://example.test/score"}):
            with patch("requests.post", return_value=_Resp()):
                assert cq.detector_score("body") == 1.0

    def test_a_failing_detector_never_breaks_the_pass(self):
        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_URL": "https://example.test/score"}):
            with patch("requests.post", side_effect=RuntimeError("boom")):
                assert cq.detector_score("body") is None

    def test_an_unrecognised_payload_scores_nothing(self):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"verdict": "human"}

        with patch.dict("os.environ", {"AI_DETECTOR_ENABLED": "true", "AI_DETECTOR_API_KEY": "k",
                                       "AI_DETECTOR_URL": "https://example.test/score"}):
            with patch("requests.post", return_value=_Resp()):
                assert cq.detector_score("body") is None


class TestVideoAssetScoring:
    def test_resolve_local_video_path_returns_none_for_missing_or_external_urls(self):
        assert cq.resolve_local_video_path("") is None
        assert cq.resolve_local_video_path("https://example.com/vid.mp4") is None
        assert cq.resolve_local_video_path("/api/assets?file_name=../etc/passwd") is None
        assert cq.resolve_local_video_path("/api/assets?file_name=image.png") is None

    def test_resolve_local_video_path_maps_asset_url_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cq, "assets_dir", str(tmp_path))
        videos = tmp_path / "videos" / "runwayml"
        videos.mkdir(parents=True)
        expected = str(videos / "clip.mp4")
        url = "/api/assets?file_name=videos/runwayml/clip.mp4"
        assert cq.resolve_local_video_path(url) == expected

    def test_probe_video_asset_reports_missing_file(self):
        assert cq.probe_video_asset("/no/such/file.mp4") == {
            "duration_seconds": None, "asset_probe": cq.VIDEO_PROBE_MISSING,
            "has_video_stream": False}

    def test_probe_video_asset_reports_empty_file(self, tmp_path):
        path = tmp_path / "empty.mp4"
        path.write_text("")
        result = cq.probe_video_asset(str(path))
        assert result["asset_probe"] == cq.VIDEO_PROBE_EMPTY
        assert result["has_video_stream"] is False

    def test_probe_video_asset_reports_unreadable_ffprobe_output(self, tmp_path):
        path = tmp_path / "bad.mp4"
        path.write_text("not a video")
        result = cq.probe_video_asset(str(path))
        assert result["asset_probe"] == cq.VIDEO_PROBE_UNREADABLE
        assert result["has_video_stream"] is False

    def test_probe_video_asset_parses_duration_and_video_stream(self, tmp_path, monkeypatch):
        import json

        path = tmp_path / "dummy.mp4"
        path.write_text("ignored")
        sample = {"streams": [{"codec_type": "video"}], "format": {"duration": "4.735"}}

        class _Run:
            returncode = 0
            stdout = json.dumps(sample)

        monkeypatch.setattr(cq.subprocess, "run", lambda *a, **kw: _Run())
        result = cq.probe_video_asset(str(path))
        assert result["asset_probe"] == cq.VIDEO_PROBE_OK
        assert result["has_video_stream"] is True
        assert result["duration_seconds"] == 5

    def test_video_model_tier_passes_through_known_model(self):
        assert cq.video_model_tier("gen4_turbo") == "gen4_turbo"
        assert cq.video_model_tier("veo3.1") == "veo3.1"

    def test_video_model_tier_falls_back_to_url_path(self):
        assert cq.video_model_tier(
            None, "https://host/assets?file_name=videos/pexels/123.mp4") == cq.VIDEO_MODEL_PEXELS
        assert cq.video_model_tier(
            None, "https://host/assets?file_name=videos/runwayml/x.mp4") == "runway"
        assert cq.video_model_tier(
            None, "https://host/assets?file_name=videos/other/x.mp4") is None

    def test_score_video_asset_records_render_ok_when_file_has_video_stream(self, tmp_path, monkeypatch):
        videos = tmp_path / "videos" / "runwayml"
        videos.mkdir(parents=True)
        path = videos / "clip.mp4"
        path.write_text("fake")
        monkeypatch.setattr(cq, "assets_dir", str(tmp_path))
        monkeypatch.setattr(cq, "probe_video_asset", lambda p: {
            "duration_seconds": 5, "asset_probe": cq.VIDEO_PROBE_OK, "has_video_stream": True})
        result = cq.score_video_asset(
            video_url="/api/assets?file_name=videos/runwayml/clip.mp4",
            model="gen4_turbo", ratio="9:16")
        assert result["video_render_ok"] is True
        assert result["video_model_tier"] == "gen4_turbo"
        assert result["video_duration_seconds"] == 5
        assert result["video_aspect_ratio"] == "9:16"
        assert result["video_asset_probe"] == cq.VIDEO_PROBE_OK

    def test_score_video_asset_reports_missing_asset(self):
        result = cq.score_video_asset(video_url=None, ratio="9:16")
        assert result["video_render_ok"] is False
        assert result["video_model_tier"] is None
        assert result["video_duration_seconds"] is None
        assert result["video_aspect_ratio"] == "9:16"
        assert result["video_asset_probe"] == cq.VIDEO_PROBE_MISSING
