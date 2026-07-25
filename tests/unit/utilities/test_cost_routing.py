"""Unit tests for the cost-aware routing optimization loop (issue #494)."""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import cost_routing as cr
from cqc_lem.utilities import routing_policy as rp

TODAY = date(2026, 8, 1)


def _rows(count, engagement, impressions=1000, authenticity=80, user_offset=0, day="2026-07-20"):
    """`count` posts with a fixed weighted engagement score (reactions carry weight 1)."""
    return [{"user_id": user_offset + i, "post_id": user_offset + i, "day": day,
             "reactions": engagement, "comments": 0, "reposts": 0,
             "impressions": impressions, "authenticity_score": authenticity,
             "feature": "content"}
            for i in range(count)]


def _bucket(**overrides):
    bucket = cr.new_bucket("content", rp.TIER_COMPLEX, date(2026, 7, 1), 0.1)
    bucket.update(overrides)
    return bucket


# ── metric selection & summaries ──

def test_pick_metric_needs_impressions_on_every_row():
    assert cr.pick_metric(_rows(3, 10)) == cr.METRIC_RATE
    assert cr.pick_metric(_rows(3, 10, impressions=None)) == cr.METRIC_COUNT
    assert cr.pick_metric(_rows(2, 10) + _rows(1, 10, impressions=0)) == cr.METRIC_COUNT
    assert cr.pick_metric([]) == cr.METRIC_COUNT


def test_summarize_arm_uses_post_stats_weighting():
    summary = cr.summarize_arm([{"reactions": 1, "comments": 2, "reposts": 1, "impressions": None,
                                 "authenticity_score": 70}])
    assert summary["mean"] == 7.0  # 1 + 2*2 + 2*1
    assert summary["authenticity_median"] == 70.0


def test_summarize_arm_ignores_unscored_posts_in_the_median():
    rows = _rows(2, 10, authenticity=90) + _rows(2, 10, authenticity=None, user_offset=50)
    summary = cr.summarize_arm(rows)
    assert summary["n"] == 4
    assert summary["authenticity_n"] == 2
    assert summary["authenticity_median"] == 90.0


# ── the non-inferiority judgement ──

def test_compare_arms_needs_enough_samples_per_arm():
    verdict = cr.compare_arms(cr.summarize_arm(_rows(5, 10)), cr.summarize_arm(_rows(50, 10)))
    assert verdict["verdict"] == cr.VERDICT_INSUFFICIENT


def test_compare_arms_accepts_indistinguishable_quality():
    treatment, control = _rows(40, 10), _rows(40, 10, user_offset=100)
    verdict = cr.compare_arms(cr.summarize_arm(treatment, cr.METRIC_RATE),
                              cr.summarize_arm(control, cr.METRIC_RATE))
    assert verdict["verdict"] == cr.VERDICT_NON_INFERIOR


def test_compare_arms_flags_a_real_engagement_drop():
    treatment, control = _rows(40, 4), _rows(40, 10, user_offset=100)
    verdict = cr.compare_arms(cr.summarize_arm(treatment, cr.METRIC_RATE),
                              cr.summarize_arm(control, cr.METRIC_RATE))
    assert verdict["verdict"] == cr.VERDICT_DEGRADED


def test_compare_arms_is_inconclusive_when_the_interval_is_too_wide():
    """Noisy arms whose means match must NOT pass as "indistinguishable" — the CI has to clear the
    equivalence margin, which is what stops an underpowered experiment auto-adopting."""
    noisy = [{**row, "reactions": 100 if i % 2 else 1} for i, row in enumerate(_rows(30, 10))]
    control = [{**row, "reactions": 100 if i % 2 else 1}
               for i, row in enumerate(_rows(30, 10, user_offset=100))]
    verdict = cr.compare_arms(cr.summarize_arm(noisy, cr.METRIC_RATE),
                              cr.summarize_arm(control, cr.METRIC_RATE))
    assert verdict["verdict"] == cr.VERDICT_INCONCLUSIVE


def test_authenticity_floor_overrides_good_engagement():
    treatment = _rows(40, 20, authenticity=40)
    control = _rows(40, 10, authenticity=85, user_offset=100)
    verdict = cr.compare_arms(cr.summarize_arm(treatment, cr.METRIC_RATE),
                              cr.summarize_arm(control, cr.METRIC_RATE))
    assert verdict["verdict"] == cr.VERDICT_DEGRADED
    assert "floor" in verdict["reason"]


def test_authenticity_drop_overrides_good_engagement():
    treatment = _rows(40, 20, authenticity=70)
    control = _rows(40, 10, authenticity=90, user_offset=100)
    verdict = cr.compare_arms(cr.summarize_arm(treatment, cr.METRIC_RATE),
                              cr.summarize_arm(control, cr.METRIC_RATE))
    assert verdict["verdict"] == cr.VERDICT_DEGRADED
    assert "authenticity" in verdict["reason"]


# ── ramp, promotion and auto-rollback ──

def test_cohort_ramp_skips_steps_below_the_configured_start():
    assert cr.cohort_ramp(0.1) == (0.1, 0.5, cr.ADOPTED_COHORT_PCT)
    assert cr.cohort_ramp(0.6) == (0.6, cr.ADOPTED_COHORT_PCT)
    assert cr.next_cohort_pct(0.1, 0.1) == 0.5
    assert cr.next_cohort_pct(0.5, 0.1) == cr.ADOPTED_COHORT_PCT
    assert cr.next_cohort_pct(cr.ADOPTED_COHORT_PCT, 0.1) is None


def test_the_ramp_never_reaches_full_traffic():
    """A bucket at 100% has no control arm left, so nothing could ever detect a regression — the
    top of the ramp keeps a permanent holdout on the incumbent tier."""
    assert cr.ADOPTED_COHORT_PCT < 1.0
    assert max(cr.cohort_ramp(0.1)) < 1.0


def test_passing_experiment_climbs_the_ramp():
    outcome = cr.evaluate_bucket(_bucket(cohort_pct=0.1),
                                 {"verdict": cr.VERDICT_NON_INFERIOR, "reason": "held"}, TODAY)
    assert outcome["action"] == cr.ACTION_PROMOTE
    assert outcome["bucket"]["cohort_pct"] == 0.5
    assert outcome["bucket"]["state"] == rp.STATE_EXPERIMENT


def test_experiment_at_the_top_of_the_ramp_is_adopted():
    outcome = cr.evaluate_bucket(_bucket(cohort_pct=0.5),
                                 {"verdict": cr.VERDICT_NON_INFERIOR, "reason": "held"}, TODAY)
    assert outcome["action"] == cr.ACTION_ADOPT
    assert outcome["bucket"]["state"] == rp.STATE_ADOPTED
    assert outcome["bucket"]["cohort_pct"] == cr.ADOPTED_COHORT_PCT


def test_an_adopted_bucket_still_has_a_control_arm_to_be_judged_on():
    """The holdout is what keeps the gate live: at the adopted cohort both arms still get posts."""
    bucket = _bucket(state=rp.STATE_ADOPTED, cohort_pct=cr.ADOPTED_COHORT_PCT,
                     started_on="2026-07-01")
    observations = [row for user in range(400) for row in _rows(1, 10, user_offset=user)]
    arms = cr.split_observations(observations, bucket)
    assert len(arms[rp.ARM_TREATMENT]) > 0 and len(arms[rp.ARM_CONTROL]) > 0


def test_degraded_quality_rolls_back_with_a_cooldown():
    outcome = cr.evaluate_bucket(_bucket(cohort_pct=0.5),
                                 {"verdict": cr.VERDICT_DEGRADED, "reason": "engagement fell"},
                                 TODAY, {"cooldown_days": 14})
    assert outcome["action"] == cr.ACTION_ROLLBACK
    assert outcome["bucket"]["state"] == rp.STATE_ROLLED_BACK
    assert outcome["bucket"]["cohort_pct"] == 0.0
    assert outcome["bucket"]["cooldown_until"] == "2026-08-15"


def test_an_adopted_bucket_still_rolls_back():
    """Adoption is not a graduation — a regression that only shows at full cohort must still revert."""
    outcome = cr.evaluate_bucket(_bucket(state=rp.STATE_ADOPTED, cohort_pct=cr.ADOPTED_COHORT_PCT),
                                 {"verdict": cr.VERDICT_DEGRADED, "reason": "quality fell"}, TODAY)
    assert outcome["action"] == cr.ACTION_ROLLBACK
    assert outcome["bucket"]["state"] == rp.STATE_ROLLED_BACK


@pytest.mark.parametrize("verdict", [cr.VERDICT_INCONCLUSIVE, cr.VERDICT_INSUFFICIENT])
def test_thin_or_noisy_data_holds_the_bucket_where_it_is(verdict):
    outcome = cr.evaluate_bucket(_bucket(cohort_pct=0.1), {"verdict": verdict, "reason": "wait"},
                                 TODAY)
    assert outcome["action"] == cr.ACTION_HOLD
    assert outcome["bucket"]["cohort_pct"] == 0.1
    assert outcome["bucket"]["state"] == rp.STATE_EXPERIMENT


# ── observation splitting ──

def test_split_observations_ignores_posts_from_before_the_experiment():
    bucket = _bucket(cohort_pct=1.0, started_on="2026-07-15")
    arms = cr.split_observations(_rows(3, 10, day="2026-07-01") + _rows(2, 10, day="2026-07-20",
                                                                       user_offset=50), bucket)
    assert len(arms[rp.ARM_TREATMENT]) == 2
    assert arms[rp.ARM_CONTROL] == []


# ── policy assembly ──

def test_build_routing_policy_opens_one_experiment_when_enabled():
    result = cr.build_routing_policy(None, _rows(40, 10), TODAY, {"content": 12.0}, enabled=True)
    buckets = result["policy"]["buckets"]
    assert list(buckets) == ["content:lem-complex"]  # most expensive tier first
    assert buckets["content:lem-complex"]["cohort_pct"] == cr.COST_ROUTING_INITIAL_COHORT
    assert result["policy"]["enabled"] is True
    assert result["changes"][0]["action"] == cr.ACTION_START


def test_build_routing_policy_opens_nothing_when_disabled():
    result = cr.build_routing_policy(None, _rows(40, 10), TODAY, enabled=False)
    assert result["policy"]["buckets"] == {}
    assert result["policy"]["enabled"] is False


def test_build_routing_policy_will_not_experiment_on_thin_data():
    result = cr.build_routing_policy(None, _rows(3, 10), TODAY, enabled=True)
    assert result["policy"]["buckets"] == {}


def test_build_routing_policy_will_not_downroute_content_already_failing_the_gate():
    result = cr.build_routing_policy(None, _rows(40, 10, authenticity=45), TODAY, enabled=True)
    assert result["policy"]["buckets"] == {}


def test_build_routing_policy_respects_the_experiment_cap():
    previous = {"version": 1, "enabled": True,
                "buckets": {"content:lem-complex": _bucket(cohort_pct=0.1)}}
    result = cr.build_routing_policy(previous, _rows(40, 10), TODAY, enabled=True)
    assert list(result["policy"]["buckets"]) == ["content:lem-complex"]


def test_build_routing_policy_rolls_a_degraded_bucket_back_end_to_end():
    """The whole gate, from raw posts to a published policy: the treatment cohort engages half as
    well, so the bucket must come back to the incumbent tier on its own."""
    bucket = _bucket(cohort_pct=0.5, started_on="2026-07-01")
    previous = {"version": 1, "enabled": True, "buckets": {"content:lem-complex": bucket}}
    observations = []
    for user in range(400):
        arm = rp.assign_arm(user, bucket)
        observations += _rows(1, 3 if arm == rp.ARM_TREATMENT else 10, user_offset=user)
    result = cr.build_routing_policy(previous, observations, TODAY, enabled=True)
    updated = result["policy"]["buckets"]["content:lem-complex"]
    assert updated["state"] == rp.STATE_ROLLED_BACK
    assert updated["cohort_pct"] == 0.0
    assert any(c["action"] == cr.ACTION_ROLLBACK for c in result["changes"])
    # And the rolled-back policy no longer routes anything down.
    assert rp.resolve_tier(rp.TIER_COMPLEX, "content", 1, result["policy"])["applied"] is False


def test_rolled_back_bucket_is_not_reproposed_until_the_cooldown_expires():
    rolled_back = _bucket(state=rp.STATE_ROLLED_BACK, cohort_pct=0.0,
                          cooldown_until="2026-09-01", generation=1)
    previous = {"version": 1, "enabled": True, "buckets": {"content:lem-complex": rolled_back}}
    result = cr.build_routing_policy(previous, _rows(40, 10), TODAY, enabled=True)
    assert result["policy"]["buckets"]["content:lem-complex"]["state"] == rp.STATE_ROLLED_BACK

    result = cr.build_routing_policy(previous, _rows(40, 10), date(2026, 9, 2), enabled=True)
    reopened = result["policy"]["buckets"]["content:lem-complex"]
    assert reopened["state"] == rp.STATE_EXPERIMENT
    assert reopened["generation"] == 2  # fresh cohort salt, not a re-test of the same users


def test_unmeasurable_features_are_recommended_not_auto_routed():
    recommendations = cr.recommend_unmeasurable({"content": 20.0, "comment": 9.0, "dm": 0.0})
    assert [r["feature"] for r in recommendations] == ["comment"]
    assert "human product call" in recommendations[0]["recommendation"]


# ── collectors, publication and delivery ──

def test_load_policy_survives_junk_in_redis():
    client = MagicMock()
    client.get.return_value = b"{not json"
    with patch.object(cr, "_redis_client", return_value=client):
        assert cr.load_policy() == {}
    client.get.return_value = None
    with patch.object(cr, "_redis_client", return_value=client):
        assert cr.load_policy() == {}
    with patch.object(cr, "_redis_client", return_value=None):
        assert cr.load_policy() == {}


def test_publish_policy_writes_the_shared_key():
    client = MagicMock()
    with patch.object(cr, "_redis_client", return_value=client):
        assert cr.publish_policy({"version": 1, "enabled": True, "buckets": {}}) is True
    key, payload = client.set.call_args[0]
    assert key == rp.POLICY_REDIS_KEY
    assert '"enabled": true' in payload


def test_publish_policy_reports_failure_without_raising():
    client = MagicMock()
    client.set.side_effect = RuntimeError("redis down")
    with patch.object(cr, "_redis_client", return_value=client):
        assert cr.publish_policy({}) is False
    with patch.object(cr, "_redis_client", return_value=None):
        assert cr.publish_policy({}) is False


def test_collect_quality_observations_tags_the_content_feature():
    with patch("cqc_lem.utilities.db.get_post_quality_rows",
               return_value=[{"user_id": 1, "post_id": 2}]) as rows:
        observations = cr.collect_quality_observations(days=7, end=TODAY)
    assert observations == [{"user_id": 1, "post_id": 2, "feature": "content"}]
    assert rows.call_args[0][0] == date(2026, 7, 25)


def test_collect_routing_report_skips_spend_without_a_ledger():
    with patch("cqc_lem.utilities.db.cost_ledger_available", return_value=False), \
         patch("cqc_lem.utilities.db.get_cost_rollup") as rollup, \
         patch.object(cr, "collect_quality_observations", return_value=[]), \
         patch.object(cr, "load_policy", return_value={}):
        report = cr.collect_routing_report(days=7, today=TODAY)
    rollup.assert_not_called()
    assert report["spend_by_feature"] == {}
    assert report["date"] == "2026-08-01"


def test_apply_routing_report_publishes_and_emails_only_on_change():
    report = {"date": "2026-08-01", "policy": {"version": 1, "enabled": True, "buckets": {}},
              "changes": [], "holds": [], "recommendations": []}
    with patch.object(cr, "publish_policy", return_value=True) as publish, \
         patch("cqc_lem.utilities.observability.track_routing_policy"), \
         patch("cqc_lem.utilities.email._dispatch_email") as dispatch:
        result = cr.apply_routing_report(report)
    assert result["published"] is True and result["emailed"] is False
    publish.assert_called_once()
    dispatch.assert_not_called()


def test_apply_routing_report_emails_and_logs_a_rollback():
    report = {"date": "2026-08-01", "policy": {"version": 1, "enabled": True, "buckets": {}},
              "changes": [{"bucket": "content:lem-complex", "action": cr.ACTION_ROLLBACK,
                           "reason": "engagement fell"}], "holds": [], "recommendations": []}
    with patch.object(cr, "publish_policy", return_value=True), \
         patch("cqc_lem.utilities.observability.track_routing_policy"), \
         patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch, \
         patch.object(cr, "log_error") as logged, \
         patch.object(cr, "COST_ALERT_EMAIL", "owner@example.com"):
        result = cr.apply_routing_report(report)
    assert result["emailed"] is True and result["rollbacks"] == 1
    assert "ROLLBACK" in dispatch.call_args[0][1]
    logged.assert_called_once()


def test_render_routing_text_lists_changes_and_buckets():
    report = {"date": "2026-08-01",
              "policy": {"enabled": True,
                         "buckets": {"content:lem-complex": {"state": "experiment",
                                                             "to_tier": "lem-medium",
                                                             "cohort_pct": 0.1}}},
              "changes": [{"bucket": "content:lem-complex", "action": cr.ACTION_START,
                           "reason": "opened", "bucket_state": {"state": "experiment"}}],
              "holds": [], "recommendations": [{"recommendation": "comment needs a human call"}]}
    text = cr.render_routing_text(report)
    assert "[START] content:lem-complex → experiment" in text
    assert "content:lem-complex: experiment → lem-medium @ 10% cohort" in text
    assert "RECOMMENDATION: comment needs a human call" in text
    assert "&" not in cr.render_routing_html(report).split("<pre")[0][:20]


def test_main_exits_2_on_a_rollback():
    report = {"date": "2026-08-01", "policy": {"buckets": {}},
              "changes": [{"action": cr.ACTION_ROLLBACK, "bucket": "content:lem-complex",
                           "reason": "fell"}], "holds": [], "recommendations": []}
    with patch.object(cr, "collect_routing_report", return_value=report):
        assert cr.main(["--json"]) == 2
    report["changes"] = []
    with patch.object(cr, "collect_routing_report", return_value=report):
        assert cr.main([]) == 0
