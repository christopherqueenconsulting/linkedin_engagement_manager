"""Unit tests for the budget-threshold / spend-anomaly alerts (issue #493, plan §E.2).

Every check is fired on a SYNTHETIC breach here (the acceptance criterion) and pinned on the
no-breach and not-enough-data paths, so a check can neither go quiet nor cry wolf unnoticed.
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from cqc_lem.utilities import cost_alerts as ca

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.cost_alerts"

DAY = date(2026, 7, 24)


def _user(user_id=1, mrr=100.0, variable=10.0, semi=0.0, tier="professional"):
    return {"user_id": user_id, "tier": tier, "mrr_usd": mrr,
            "variable_cost_usd": variable, "semi_variable_cost_usd": semi}


def _system(**overrides):
    system = {"active_users": 2, "paying_users": 1, "mrr_usd": 100.0, "gross_margin_pct": 0.8,
              "gross_margin_usd": 80.0, "period_cost_usd": 20.0, "unattributed_cost_usd": 0.0}
    system.update(overrides)
    return system


def _series(values, end=DAY):
    """`{iso_day: usd}` ending on `end`, oldest first."""
    return {(end - timedelta(days=len(values) - 1 - i)).isoformat(): v
            for i, v in enumerate(values)}


class TestUserCostCeiling:
    def test_fires_when_variable_cost_exceeds_the_share_of_tier_mrr(self):
        check = ca.evaluate_user_cost_ceiling([_user(variable=30.0)], pct_of_mrr=0.25)
        assert check["status"] == ca.STATUS_ALERT
        alert = check["alerts"][0]
        assert alert["severity"] == ca.SEVERITY_WARNING
        assert alert["user_id"] == 1 and alert["value"] == pytest.approx(0.30)
        assert alert["threshold"] == 0.25 and "30.0% of tier MRR" in alert["title"]

    def test_counts_semi_variable_cost_toward_the_ceiling(self):
        check = ca.evaluate_user_cost_ceiling([_user(variable=15.0, semi=15.0)], pct_of_mrr=0.25)
        assert check["alerts"][0]["value"] == pytest.approx(0.30)

    def test_margin_negative_user_is_critical(self):
        check = ca.evaluate_user_cost_ceiling([_user(variable=120.0)], pct_of_mrr=0.25)
        assert check["alerts"][0]["severity"] == ca.SEVERITY_CRITICAL

    def test_quiet_under_the_threshold(self):
        check = ca.evaluate_user_cost_ceiling([_user(variable=10.0)], pct_of_mrr=0.25)
        assert check["status"] == ca.STATUS_OK and check["alerts"] == []

    def test_threshold_is_configurable(self):
        users = [_user(variable=15.0)]
        assert ca.evaluate_user_cost_ceiling(users, pct_of_mrr=0.25)["status"] == ca.STATUS_OK
        assert ca.evaluate_user_cost_ceiling(users, pct_of_mrr=0.10)["status"] == ca.STATUS_ALERT

    def test_trials_are_skipped_because_percent_of_zero_mrr_is_undefined(self):
        check = ca.evaluate_user_cost_ceiling(
            [_user(user_id=2, mrr=0.0, variable=40.0, tier="free_trial")], pct_of_mrr=0.25)
        assert check["status"] == ca.STATUS_SKIPPED and check["users_judged"] == 0

    def test_no_users_at_all_is_skipped_not_ok(self):
        assert ca.evaluate_user_cost_ceiling([], pct_of_mrr=0.25)["status"] == ca.STATUS_SKIPPED


class TestGrossMarginFloor:
    def test_fires_below_the_floor(self):
        check = ca.evaluate_gross_margin_floor(_system(gross_margin_pct=0.55), floor=0.70)
        assert check["status"] == ca.STATUS_ALERT
        alert = check["alerts"][0]
        assert alert["severity"] == ca.SEVERITY_WARNING and alert["value"] == 0.55
        assert "below the 70% floor" in alert["title"]

    def test_negative_margin_is_critical(self):
        check = ca.evaluate_gross_margin_floor(
            _system(gross_margin_pct=-0.2, gross_margin_usd=-20.0), floor=0.70)
        assert check["alerts"][0]["severity"] == ca.SEVERITY_CRITICAL

    def test_quiet_at_or_above_the_floor(self):
        assert ca.evaluate_gross_margin_floor(_system(gross_margin_pct=0.70),
                                              floor=0.70)["status"] == ca.STATUS_OK

    def test_no_revenue_is_skipped_not_a_breach(self):
        check = ca.evaluate_gross_margin_floor(_system(gross_margin_pct=None), floor=0.70)
        assert check["status"] == ca.STATUS_SKIPPED and "undefined" in check["reason"]


class TestSpendAnomaly:
    def test_fires_on_a_spike_above_mu_plus_3_sigma(self):
        series = _series([10.0, 10.5, 9.5, 10.0, 10.2, 9.8, 90.0])
        check = ca.evaluate_spend_anomaly(series, DAY, sigma=3.0, daily_budget_usd=0,
                                          min_spend_usd=1.0)
        assert check["status"] == ca.STATUS_ALERT
        alert = check["alerts"][0]
        assert alert["kind"] == "sigma" and alert["value"] == 90.0
        assert alert["severity"] == ca.SEVERITY_WARNING and alert["day"] == DAY.isoformat()

    def test_quiet_on_steady_spend(self):
        check = ca.evaluate_spend_anomaly(_series([10.0, 10.5, 9.5, 10.0, 10.2, 9.8, 10.4]), DAY,
                                          sigma=3.0, daily_budget_usd=0, min_spend_usd=1.0)
        assert check["status"] == ca.STATUS_OK and check["baseline_days"] == 6

    def test_sigma_multiple_is_configurable(self):
        # μ=10, σ≈0.82 → 12.0 clears μ+1σ but not μ+3σ.
        series = _series([10.0, 11.0, 9.0, 10.0, 11.0, 9.0, 12.0])
        assert ca.evaluate_spend_anomaly(series, DAY, sigma=3.0,
                                         min_spend_usd=1.0)["status"] == ca.STATUS_OK
        assert ca.evaluate_spend_anomaly(series, DAY, sigma=1.0,
                                         min_spend_usd=1.0)["status"] == ca.STATUS_ALERT

    def test_absolute_daily_budget_breach_is_critical(self):
        check = ca.evaluate_spend_anomaly(_series([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 42.0]), DAY,
                                          sigma=3.0, daily_budget_usd=25.0, min_spend_usd=1.0)
        kinds = {a["kind"]: a for a in check["alerts"]}
        assert kinds["budget"]["severity"] == ca.SEVERITY_CRITICAL
        assert kinds["budget"]["threshold"] == 25.0

    def test_budget_breach_fires_before_a_baseline_exists(self):
        check = ca.evaluate_spend_anomaly({DAY.isoformat(): 99.0}, DAY, daily_budget_usd=25.0)
        assert check["status"] == ca.STATUS_ALERT
        assert [a["kind"] for a in check["alerts"]] == ["budget"]

    def test_noise_floor_keeps_cents_from_tripping_sigma(self):
        # Baseline σ≈0, so any movement clears μ+3σ — the floor is what stops the false alarm.
        series = _series([0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.40])
        assert ca.evaluate_spend_anomaly(series, DAY, sigma=3.0,
                                         min_spend_usd=1.0)["status"] == ca.STATUS_OK
        assert ca.evaluate_spend_anomaly(series, DAY, sigma=3.0,
                                         min_spend_usd=0.10)["status"] == ca.STATUS_ALERT

    def test_too_few_trailing_days_is_skipped(self):
        check = ca.evaluate_spend_anomaly(_series([5.0, 90.0]), DAY, sigma=3.0)
        assert check["status"] == ca.STATUS_SKIPPED and "trailing day" in check["reason"]

    def test_absent_days_are_not_counted_as_zero_spend(self):
        # Only 2 days of ledger data in an 8-day window → not enough baseline, NOT a 90 USD spike
        # against six fabricated zeros.
        series = {"2026-07-23": 5.0, DAY.isoformat(): 90.0}
        assert ca.evaluate_spend_anomaly(series, DAY, sigma=3.0)["status"] == ca.STATUS_SKIPPED

    def test_no_row_for_the_day_is_skipped(self):
        check = ca.evaluate_spend_anomaly(_series([10.0, 10.0, 10.0], end=DAY - timedelta(days=1)),
                                          DAY)
        assert check["status"] == ca.STATUS_SKIPPED and "no ledger rows" in check["reason"]


class TestCacheHitCollapse:
    def _cache(self, rate=0.2, calls=500, baseline=0.6, baseline_calls=3000):
        return {"date": DAY.isoformat(), "rate": rate, "calls": calls,
                "baseline_rate": baseline, "baseline_calls": baseline_calls}

    def test_fires_on_a_relative_drop_against_the_baseline(self):
        check = ca.evaluate_cache_hit_collapse(self._cache(), drop_pct=0.5, floor=0, min_calls=50)
        assert check["status"] == ca.STATUS_ALERT
        alert = check["alerts"][0]
        assert alert["kind"] == "drop" and alert["value"] == pytest.approx(0.6667, abs=1e-3)
        assert "cost spike" in alert["detail"]

    def test_absolute_floor_is_opt_in(self):
        cache = self._cache(rate=0.05, baseline=0.05)
        assert ca.evaluate_cache_hit_collapse(cache, drop_pct=0.5, floor=0,
                                              min_calls=50)["status"] == ca.STATUS_OK
        check = ca.evaluate_cache_hit_collapse(cache, drop_pct=0.5, floor=0.10, min_calls=50)
        assert check["alerts"][0]["kind"] == "floor"

    def test_quiet_when_the_rate_holds(self):
        check = ca.evaluate_cache_hit_collapse(self._cache(rate=0.55), drop_pct=0.5, floor=0,
                                               min_calls=50)
        assert check["status"] == ca.STATUS_OK

    def test_low_volume_day_cannot_read_as_a_collapse(self):
        check = ca.evaluate_cache_hit_collapse(self._cache(rate=0.0, calls=3), min_calls=50)
        assert check["status"] == ca.STATUS_SKIPPED and "under the 50-call minimum" in check["reason"]

    def test_low_volume_baseline_does_not_drive_a_drop(self):
        check = ca.evaluate_cache_hit_collapse(self._cache(baseline_calls=5), drop_pct=0.5,
                                               floor=0, min_calls=50)
        assert check["status"] == ca.STATUS_OK

    def test_unavailable_posthog_is_skipped_not_a_collapse(self):
        check = ca.evaluate_cache_hit_collapse(None)
        assert check["status"] == ca.STATUS_SKIPPED and "PostHog" in check["reason"]


class TestUnattributedSpend:
    def test_fires_on_spend_with_no_user(self):
        check = ca.evaluate_unattributed_spend(
            _system(period_cost_usd=100.0, unattributed_cost_usd=30.0), {"content": 100.0},
            max_share=0.10)
        assert check["status"] == ca.STATUS_ALERT
        alert = check["alerts"][0]
        assert alert["dimension"] == "user_id" and alert["value"] == pytest.approx(0.30)

    def test_fires_on_spend_with_no_feature(self):
        check = ca.evaluate_unattributed_spend(
            _system(period_cost_usd=100.0, unattributed_cost_usd=0.0),
            {"content": 60.0, "unknown": 25.0, "system": 15.0}, max_share=0.10)
        alert = check["alerts"][0]
        assert alert["dimension"] == "feature" and alert["value"] == pytest.approx(0.40)

    def test_quiet_under_the_threshold(self):
        check = ca.evaluate_unattributed_spend(
            _system(period_cost_usd=100.0, unattributed_cost_usd=5.0),
            {"content": 95.0, "system": 5.0}, max_share=0.10)
        assert check["status"] == ca.STATUS_OK and check["user_share"] == 0.05

    def test_no_spend_is_skipped(self):
        check = ca.evaluate_unattributed_spend(_system(period_cost_usd=0.0), {}, max_share=0.10)
        assert check["status"] == ca.STATUS_SKIPPED


class TestThresholds:
    def test_defaults_come_from_the_environment_constants(self):
        with patch(f"{_MOD}.COST_ALERT_MARGIN_FLOOR", 0.42), \
             patch(f"{_MOD}.COST_ALERT_DAILY_BUDGET", 12.5):
            limits = ca.default_thresholds()
        assert limits["gross_margin_floor"] == 0.42 and limits["daily_budget_usd"] == 12.5

    def test_report_overrides_merge_over_the_defaults(self):
        report = ca.build_cost_alert_report(DAY, _margin_report(), thresholds={"spend_sigma": 1.5})
        assert report["thresholds"]["spend_sigma"] == 1.5
        assert report["thresholds"]["gross_margin_floor"] == ca.COST_ALERT_MARGIN_FLOOR


def _margin_report(**system_overrides):
    return {"period": {"start": "2026-07-18", "end": "2026-07-24", "days": 7},
            "ledger_available": True, "users": [_user()], "system": _system(**system_overrides)}


class TestBuildReport:
    def test_runs_every_check_and_flattens_the_breaches(self):
        report = ca.build_cost_alert_report(
            DAY,
            _margin_report(gross_margin_pct=0.1, gross_margin_usd=10.0,
                           period_cost_usd=100.0, unattributed_cost_usd=50.0),
            cost_by_feature={"content": 100.0},
            daily_spend=_series([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 95.0]),
            cache={"rate": 0.1, "calls": 500, "baseline_rate": 0.8, "baseline_calls": 900},
            thresholds={"user_cost_pct_of_mrr": 0.05},
        )
        assert [c["check"] for c in report["checks"]] == [
            ca.CHECK_USER_COST, ca.CHECK_MARGIN_FLOOR, ca.CHECK_SPEND_ANOMALY,
            ca.CHECK_CACHE_COLLAPSE, ca.CHECK_UNATTRIBUTED]
        assert {a["check"] for a in report["alerts"]} == {
            ca.CHECK_USER_COST, ca.CHECK_MARGIN_FLOOR, ca.CHECK_SPEND_ANOMALY,
            ca.CHECK_CACHE_COLLAPSE, ca.CHECK_UNATTRIBUTED}
        assert report["alert_count"] == len(report["alerts"]) and report["date"] == "2026-07-24"
        assert report["skipped_checks"] == []

    def test_all_clear_report_has_no_alerts(self):
        report = ca.build_cost_alert_report(
            DAY, _margin_report(), cost_by_feature={"content": 20.0},
            daily_spend=_series([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]),
            cache={"rate": 0.6, "calls": 500, "baseline_rate": 0.6, "baseline_calls": 900})
        assert report["alert_count"] == 0 and report["critical_count"] == 0

    def test_counts_criticals_separately(self):
        report = ca.build_cost_alert_report(
            DAY, _margin_report(gross_margin_pct=-0.5, gross_margin_usd=-50.0))
        assert report["critical_count"] == 1

    def test_missing_data_reports_skipped_checks_not_alerts(self):
        report = ca.build_cost_alert_report(DAY, {"ledger_available": False, "users": [],
                                                  "system": {}})
        assert report["alert_count"] == 0
        assert set(report["skipped_checks"]) == {ca.CHECK_USER_COST, ca.CHECK_MARGIN_FLOOR,
                                                 ca.CHECK_SPEND_ANOMALY, ca.CHECK_CACHE_COLLAPSE,
                                                 ca.CHECK_UNATTRIBUTED}
        assert report["ledger_available"] is False

    def test_missing_ledger_skips_the_ledger_backed_checks_instead_of_passing_them(self):
        """A $0 margin report derived from empty rollups must not read as an all-clear."""
        report = ca.build_cost_alert_report(
            DAY,
            {"ledger_available": False, "users": [_user(variable=0.0)],
             "system": _system(period_cost_usd=0.0, gross_margin_pct=1.0)},
            cost_by_feature={}, daily_spend=_series([0.0] * 7),
            cache={"rate": 0.6, "calls": 500, "baseline_rate": 0.6, "baseline_calls": 900})
        by_id = {c["check"]: c for c in report["checks"]}
        for check_id in ca.LEDGER_BACKED_CHECKS:
            assert by_id[check_id]["status"] == ca.STATUS_SKIPPED
            assert by_id[check_id]["reason"] == ca.LEDGER_MISSING_REASON
        # The cache check is PostHog-fed, so it still has real data to judge.
        assert by_id[ca.CHECK_CACHE_COLLAPSE]["status"] == ca.STATUS_OK
        assert report["alert_count"] == 0


class TestRenderers:
    def _report(self):
        return ca.build_cost_alert_report(DAY, _margin_report(gross_margin_pct=0.1,
                                                              gross_margin_usd=10.0))

    def test_text_lists_alerts_and_every_check_status(self):
        text = ca.render_cost_alerts_text(self._report())
        assert text.startswith("LEM cost alerts — 2026-07-24")
        assert "[WARNING] System gross margin 10.0% is below" in text
        assert f"{ca.CHECK_SPEND_ANOMALY}: {ca.STATUS_SKIPPED}" in text

    def test_text_says_so_when_nothing_breached(self):
        report = ca.build_cost_alert_report(DAY, _margin_report())
        assert "No thresholds breached." in ca.render_cost_alerts_text(report)

    def test_text_warns_when_the_ledger_is_missing(self):
        report = ca.build_cost_alert_report(DAY, {"ledger_available": False, "system": {}})
        text = ca.render_cost_alerts_text(report)
        assert "cost_ledger is not present yet" in text
        assert "skipped, not passing" in text
        assert f"{ca.CHECK_MARGIN_FLOOR}: {ca.STATUS_SKIPPED}" in text

    def test_html_escapes_and_wraps_the_text_digest(self):
        report = self._report()
        report["alerts"][0]["title"] = "<b>margin</b>"
        html = ca.render_cost_alerts_html(report)
        assert html.startswith("<h2>LEM cost alerts</h2><pre")
        assert "&lt;b&gt;margin&lt;/b&gt;" in html


class TestCacheHitCollector:
    def _rows(self):
        return [["2026-07-22", 60, 100], ["2026-07-23", 60, 100], [DAY.isoformat(), 5, 200]]

    def test_splits_the_day_from_a_call_weighted_baseline(self):
        with patch("cqc_lem.utilities.observability.posthog_hogql_query",
                   return_value=self._rows()) as query:
            stats = ca.collect_cache_hit_stats(DAY)
        assert stats["rate"] == pytest.approx(0.025) and stats["calls"] == 200
        assert stats["baseline_rate"] == pytest.approx(0.6) and stats["baseline_calls"] == 200
        sql = query.call_args[0][0]
        assert "event = 'llm_call'" in sql and "properties.cached = true" in sql

    def test_unreadable_posthog_returns_none(self):
        with patch("cqc_lem.utilities.observability.posthog_hogql_query", return_value=None):
            assert ca.collect_cache_hit_stats(DAY) is None

    def test_malformed_rows_are_ignored(self):
        rows = [None, ["2026-07-23"], ["2026-07-23", 60, 100], [DAY.isoformat(), 5, 200]]
        with patch("cqc_lem.utilities.observability.posthog_hogql_query", return_value=rows):
            stats = ca.collect_cache_hit_stats(DAY)
        assert stats["calls"] == 200 and stats["baseline_calls"] == 100

    def test_no_events_for_the_day_leaves_the_rate_unknown(self):
        with patch("cqc_lem.utilities.observability.posthog_hogql_query", return_value=[]):
            stats = ca.collect_cache_hit_stats(DAY)
        assert stats["rate"] is None and stats["baseline_rate"] is None


class TestCollectReport:
    def test_reads_the_ledger_and_margin_report_for_the_window(self):
        with patch(f"{_MOD}.collect_cache_hit_stats", return_value=None), \
             patch("cqc_lem.utilities.margin.collect_margin_report",
                   return_value=_margin_report()) as margin, \
             patch("cqc_lem.utilities.db.get_cost_rollup", return_value={"content": 20.0}) as rollup, \
             patch("cqc_lem.utilities.db.get_daily_cost_totals",
                   return_value=_series([10.0] * 7)) as daily:
            report = ca.collect_cost_alert_report(day=DAY, days=7)

        margin.assert_called_once_with(end=DAY, days=7)
        assert rollup.call_args[0] == (date(2026, 7, 18), DAY)
        assert rollup.call_args[1]["group_by"] == "feature"
        # The anomaly baseline reaches a full window further back than the rollup window.
        assert daily.call_args[0] == (DAY - timedelta(days=ca.ANOMALY_WINDOW_DAYS), DAY)
        assert report["date"] == DAY.isoformat() and report["alert_count"] == 0

    def test_defaults_to_the_last_complete_utc_day(self):
        with patch(f"{_MOD}._today", return_value=date(2026, 7, 25)), \
             patch(f"{_MOD}.collect_cache_hit_stats", return_value=None), \
             patch("cqc_lem.utilities.margin.collect_margin_report", return_value=_margin_report()), \
             patch("cqc_lem.utilities.db.get_cost_rollup", return_value={}), \
             patch("cqc_lem.utilities.db.get_daily_cost_totals", return_value={}):
            assert ca.collect_cost_alert_report()["date"] == "2026-07-24"


def _breach_report():
    return ca.build_cost_alert_report(DAY, _margin_report(gross_margin_pct=-0.5,
                                                          gross_margin_usd=-50.0))


class TestDelivery:
    def test_emails_and_tracks_every_breach(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as send, \
             patch("cqc_lem.utilities.observability.track_cost_alert") as track:
            result = ca.send_cost_alerts(_breach_report(), to_email="owner@example.com")

        assert result["emailed"] is True and result["tracked"] is True and result["alerts"] == 1
        args, kwargs = send.call_args
        assert args[0] == "owner@example.com" and "CRITICAL" in args[1]
        assert kwargs["text_content"].startswith("LEM cost alerts")
        assert kwargs["high_priority"] is True
        assert track.call_args[1]["day"] == "2026-07-24"

    def test_all_clear_is_not_emailed(self):
        with patch("cqc_lem.utilities.email._dispatch_email") as send, \
             patch("cqc_lem.utilities.observability.track_cost_alert"):
            result = ca.send_cost_alerts(ca.build_cost_alert_report(DAY, _margin_report()),
                                         to_email="owner@example.com")
        send.assert_not_called()
        assert result["emailed"] is False and result["alerts"] == 0

    def test_critical_alerts_are_logged_as_errors(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True), \
             patch("cqc_lem.utilities.observability.track_cost_alert"), \
             patch(f"{_MOD}.log_error") as log_error:
            ca.send_cost_alerts(_breach_report(), to_email="owner@example.com")
        assert "gross_margin_floor" in log_error.call_args[0][0]

    def test_warning_alerts_are_logged_as_warnings(self):
        report = ca.build_cost_alert_report(DAY, _margin_report(gross_margin_pct=0.1,
                                                                gross_margin_usd=10.0))
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True), \
             patch("cqc_lem.utilities.observability.track_cost_alert"), \
             patch(f"{_MOD}.log_error") as log_error, patch(f"{_MOD}.log_warning") as log_warning:
            ca.send_cost_alerts(report, to_email="owner@example.com")
        log_error.assert_not_called()
        assert "gross_margin_floor" in log_warning.call_args[0][0]

    def test_falls_back_to_the_margin_report_recipient(self):
        with patch(f"{_MOD}.COST_ALERT_EMAIL", ""), \
             patch(f"{_MOD}.MARGIN_REPORT_EMAIL", "margin@example.com"), \
             patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as send, \
             patch("cqc_lem.utilities.observability.track_cost_alert"):
            ca.send_cost_alerts(_breach_report())
        assert send.call_args[0][0] == "margin@example.com"

    def test_no_recipient_still_tracks(self):
        with patch(f"{_MOD}.COST_ALERT_EMAIL", ""), patch(f"{_MOD}.MARGIN_REPORT_EMAIL", ""), \
             patch("cqc_lem.utilities.email._dispatch_email") as send, \
             patch("cqc_lem.utilities.observability.track_cost_alert"):
            result = ca.send_cost_alerts(_breach_report())
        send.assert_not_called()
        assert result["tracked"] is True

    def test_email_failure_does_not_lose_the_alerts(self):
        with patch("cqc_lem.utilities.email._dispatch_email", side_effect=RuntimeError("smtp down")), \
             patch("cqc_lem.utilities.observability.track_cost_alert"):
            result = ca.send_cost_alerts(_breach_report(), to_email="owner@example.com")
        assert result["emailed"] is False and result["tracked"] is True

    def test_tracking_failure_does_not_lose_the_email(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True), \
             patch("cqc_lem.utilities.observability.track_cost_alert",
                   side_effect=RuntimeError("posthog down")):
            result = ca.send_cost_alerts(_breach_report(), to_email="owner@example.com")
        assert result["emailed"] is True and result["tracked"] is False


class TestCli:
    def test_exit_2_on_a_breach_so_a_caller_can_act_on_it(self, capsys):
        with patch(f"{_MOD}.collect_cost_alert_report", return_value=_breach_report()):
            assert ca.main([]) == 2
        assert "LEM cost alerts" in capsys.readouterr().out

    def test_exit_0_when_nothing_breached(self, capsys):
        clear = ca.build_cost_alert_report(DAY, _margin_report())
        with patch(f"{_MOD}.collect_cost_alert_report", return_value=clear):
            assert ca.main(["--json"]) == 0
        import json as _json
        assert _json.loads(capsys.readouterr().out.strip())["alert_count"] == 0

    def test_day_and_days_flags_reach_the_collector(self):
        clear = ca.build_cost_alert_report(DAY, _margin_report())
        with patch(f"{_MOD}.collect_cost_alert_report", return_value=clear) as collect:
            ca.main(["--day", "2026-07-20", "--days", "14"])
        assert collect.call_args[1] == {"day": date(2026, 7, 20), "days": 14}

    def test_email_flag_delivers(self):
        report = _breach_report()
        with patch(f"{_MOD}.collect_cost_alert_report", return_value=report), \
             patch(f"{_MOD}.send_cost_alerts") as send:
            assert ca.main(["--email"]) == 2
        send.assert_called_once_with(report)


class TestCeleryTask:
    def test_task_collects_sends_and_summarizes(self):
        from cqc_lem.app.run_scheduler import auto_daily_cost_alerts

        report = _breach_report()
        with patch(f"{_MOD}.collect_cost_alert_report", return_value=report), \
             patch(f"{_MOD}.send_cost_alerts",
                   return_value={"emailed": True, "tracked": True, "alerts": 1,
                                 "report": report}) as send:
            summary = auto_daily_cost_alerts(days=7)
        send.assert_called_once_with(report)
        assert "2026-07-24" in summary and "1 alert(s)" in summary

    def test_beat_schedules_it_daily(self):
        from cqc_lem.app.my_celery import app as celery_app

        entry = celery_app.conf.beat_schedule["daily-cost-alerts"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_daily_cost_alerts"
        assert entry["schedule"].hour == {13} and entry["schedule"].minute == {0}
