"""Unit tests for the cost/margin unit-economics layer (issue #491).

The §C.1 formulas are pinned numerically here — if the margin math drifts, these fail.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import margin as m

pytestmark = pytest.mark.unit

_MARGIN = "cqc_lem.utilities.margin"


def _row(days_ago: int, reactions=10, comments=2, reposts=1, impressions=1000):
    """A `db.get_post_engagement_rows` tuple: scheduled_time first, impressions at index 9."""
    posted = datetime(2026, 7, 20) - timedelta(days=days_ago)
    return (posted, reactions, comments, reposts, None, None, None, None, None, impressions)


class TestTierMrr:
    def test_maps_tiers_and_treats_trial_as_zero(self):
        assert m.tier_mrr_usd("professional") == m.TIER_MRR_PROFESSIONAL
        assert m.tier_mrr_usd("  Starter ") == m.TIER_MRR_STARTER
        assert m.tier_mrr_usd("enterprise") == m.TIER_MRR_ENTERPRISE
        assert m.tier_mrr_usd("free_trial") == 0.0

    def test_unknown_and_none_earn_nothing(self):
        assert m.tier_mrr_usd(None) == 0.0
        assert m.tier_mrr_usd("legacy_plan") == 0.0


class TestSplitCostByKind:
    def test_buckets_by_category(self):
        split = m.split_cost_by_kind({"llm": 1.5, "media": 2.0, "proxy": 3.0, "infra": 10.0})
        assert split == {"variable": 3.5, "semi_variable": 3.0, "fixed": 10.0}

    def test_unknown_category_counts_as_variable_so_spend_never_vanishes(self):
        assert m.split_cost_by_kind({"quantum_gpu": 4.0})["variable"] == 4.0

    def test_empty_and_none(self):
        assert m.split_cost_by_kind(None) == {"variable": 0.0, "semi_variable": 0.0, "fixed": 0.0}


class TestFormulas:
    def test_contribution_margin_and_pct(self):
        cm = m.contribution_margin(79.0, 12.0, 3.0)
        assert cm == 64.0
        assert m.margin_pct(cm, 79.0) == pytest.approx(64.0 / 79.0)

    def test_margin_pct_undefined_without_revenue(self):
        # A $0-MRR trial that costs money is NOT "0% margin" — the ratio has no meaning.
        assert m.margin_pct(-5.0, 0.0) is None

    def test_allocated_fixed_amortizes_and_survives_zero_users(self):
        assert m.allocated_fixed_per_user(100.0, 8) == 12.5
        assert m.allocated_fixed_per_user(100.0, 0) == 0.0

    def test_fully_loaded_margin_subtracts_allocated_fixed(self):
        assert m.fully_loaded_margin(64.0, 12.5) == 51.5

    def test_system_gross_margin(self):
        result = m.system_gross_margin(1000.0, 200.0, 50.0, 100.0)
        assert result["gross_margin_usd"] == 650.0
        assert result["gross_margin_pct"] == pytest.approx(0.65)

    def test_system_gross_margin_pct_none_without_revenue(self):
        assert m.system_gross_margin(0.0, 10.0, 0.0, 0.0)["gross_margin_pct"] is None

    def test_ltv_payback_and_ratio(self):
        ltv = m.ltv_usd(50.0, 12)
        assert ltv == 600.0
        assert m.ltv_cac_ratio(ltv, 150.0) == pytest.approx(4.0)
        assert m.payback_months(150.0, 50.0) == pytest.approx(3.0)

    def test_payback_none_when_margin_not_positive(self):
        assert m.payback_months(150.0, 0.0) is None
        assert m.payback_months(150.0, -4.0) is None

    def test_payback_none_without_cac_instead_of_reporting_instant(self):
        assert m.payback_months(0.0, 50.0) is None

    def test_ltv_cac_ratio_none_without_cac(self):
        assert m.ltv_cac_ratio(600.0, 0.0) is None

    def test_monthly_run_rate_scales_a_window(self):
        assert m.monthly_run_rate(7.0, 7) == pytest.approx(7.0 * m.DAYS_PER_MONTH / 7)
        assert m.monthly_run_rate(5.0, 0) == 5.0  # no division by zero


class TestEngagementLift:
    def test_rate_mode_lift_against_earlier_posts(self):
        rows = [_row(1, reactions=20, comments=4, reposts=2, impressions=1000),   # in window
                _row(30, reactions=10, comments=2, reposts=1, impressions=1000)]  # baseline
        lift = m.engagement_lift(rows, date(2026, 7, 14))
        assert lift["metric"] == m.METRIC_RATE
        assert lift["current"] == pytest.approx(0.032)   # (20 + 2*4 + 2*2)/1000
        assert lift["baseline"] == pytest.approx(0.016)  # (10 + 2*2 + 2*1)/1000
        assert lift["lift_pct"] == pytest.approx(100.0)
        assert (lift["samples"], lift["baseline_samples"]) == (1, 1)

    def test_falls_back_to_counts_when_impressions_incomplete(self):
        rows = [_row(1, impressions=None), _row(30, impressions=1000)]
        lift = m.engagement_lift(rows, date(2026, 7, 14))
        assert lift["metric"] == m.METRIC_COUNT
        assert lift["current"] == pytest.approx(16.0)  # 10 + 2*2 + 2*1

    def test_no_baseline_means_no_lift(self):
        lift = m.engagement_lift([_row(1)], date(2026, 7, 14))
        assert lift["current"] is not None and lift["baseline"] is None
        assert lift["lift_pct"] is None

    def test_rows_without_a_timestamp_are_ignored(self):
        assert m.engagement_lift([(None, 5, 1, 0, None, None, None, None, None, 100)],
                                 date(2026, 7, 14))["samples"] == 0

    def test_empty_rows(self):
        lift = m.engagement_lift([], date(2026, 7, 14))
        assert lift == {"current": None, "baseline": None, "metric": None, "lift_pct": None,
                        "samples": 0, "baseline_samples": 0}


class TestDailyBlock:
    def test_block_carries_spend_features_and_margin(self):
        block = m.build_daily_margin_block(
            date(2026, 7, 25),
            {"content": 0.42, "comment": 0.08},
            {"llm": 6.0, "media": 2.0, "proxy": 2.0, "infra": 30.0},
            total_mrr_usd=79.0, infra_monthly_usd=30.0)
        assert block["date"] == "2026-07-25"
        assert block["spend_usd"] == pytest.approx(0.5)
        assert block["cost_by_feature"] == {"content": 0.42, "comment": 0.08}
        assert block["mtd_spend_usd"] == pytest.approx(40.0)
        assert block["mrr_usd"] == 79.0
        # 79 − (6+2 variable) − 2 semi-var = 69
        assert block["contribution_margin_usd"] == 69.0
        # gross margin also nets the fixed infra: (69 − 30) / 79
        assert block["gross_margin_pct"] == pytest.approx(0.4937, abs=1e-4)
        assert block["ledger_available"] is True

    def test_no_ledger_yet_reports_zero_spend_and_flags_it(self):
        block = m.build_daily_margin_block(date(2026, 7, 25), {}, {}, 79.0,
                                           ledger_available=False)
        assert block["spend_usd"] == 0.0
        assert block["cost_by_feature"] == {}
        assert block["contribution_margin_usd"] == 79.0
        assert block["ledger_available"] is False


def _users():
    return [
        {"user_id": 1, "tier": "professional", "cohort": "2026-06",
         "cost_by_category": {"llm": 7.0, "proxy": 3.5},
         "engagement": {"current": 0.03, "baseline": 0.02, "metric": m.METRIC_RATE,
                        "lift_pct": 50.0}},
        {"user_id": 2, "tier": "free_trial", "cohort": "2026-07",
         "cost_by_category": {"llm": 14.0},
         "engagement": {"current": 0.01, "baseline": None, "metric": m.METRIC_RATE,
                        "lift_pct": None}},
    ]


class TestWeeklyReport:
    def test_per_user_margin_on_a_monthly_run_rate_basis(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), _users(),
                                       system_cost_by_category={"llm": 21.0, "proxy": 3.5},
                                       infra_monthly_usd=60.0, cac_usd=150.0,
                                       expected_lifetime_months=12)
        assert report["period"] == {"start": "2026-07-19", "end": "2026-07-25", "days": 7,
                                    "basis": "monthly_run_rate"}
        by_id = {row["user_id"]: row for row in report["users"]}
        scale = m.DAYS_PER_MONTH / 7
        assert by_id[1]["variable_cost_usd"] == pytest.approx(7.0 * scale, abs=1e-3)
        assert by_id[1]["semi_variable_cost_usd"] == pytest.approx(3.5 * scale, abs=1e-3)
        expected_cm = m.TIER_MRR_PROFESSIONAL - 10.5 * scale
        assert by_id[1]["contribution_margin_usd"] == pytest.approx(round(expected_cm, 2))
        assert by_id[1]["cm_pct"] == pytest.approx(expected_cm / m.TIER_MRR_PROFESSIONAL, abs=1e-4)
        assert by_id[1]["engagement_lift_pct"] == 50.0
        # A trial earns nothing, so CM% is undefined while its cost is still fully counted.
        assert by_id[2]["mrr_usd"] == 0.0 and by_id[2]["cm_pct"] is None
        assert by_id[2]["contribution_margin_usd"] == pytest.approx(round(-14.0 * scale, 2))

    def test_worst_margin_first(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), _users())
        assert [row["user_id"] for row in report["users"]] == [2, 1]

    def test_system_block_amortizes_fixed_and_flags_unattributed_spend(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), _users(),
                                       system_cost_by_category={"llm": 25.0, "proxy": 3.5},
                                       infra_monthly_usd=60.0)
        system = report["system"]
        assert (system["active_users"], system["paying_users"]) == (2, 1)
        assert system["mrr_usd"] == pytest.approx(m.TIER_MRR_PROFESSIONAL)
        assert system["allocated_fixed_per_user_usd"] == 30.0
        # System llm spend (25) exceeds what is attributed to users (21 llm + 3.5 proxy = 24.5).
        assert system["unattributed_cost_usd"] == pytest.approx(4.0)
        scale = m.DAYS_PER_MONTH / 7
        expected = m.TIER_MRR_PROFESSIONAL - 25.0 * scale - 3.5 * scale - 60.0
        assert system["gross_margin_usd"] == pytest.approx(round(expected, 2))
        assert system["gross_margin_pct"] == pytest.approx(expected / m.TIER_MRR_PROFESSIONAL,
                                                           abs=1e-4)

    def test_system_totals_fall_back_to_per_user_sums(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), _users())
        scale = m.DAYS_PER_MONTH / 7
        assert report["system"]["variable_cost_usd"] == pytest.approx(21.0 * scale, abs=1e-3)
        assert report["system"]["unattributed_cost_usd"] == 0.0

    def test_unit_economics_from_paying_users_only(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), _users(),
                                       cac_usd=150.0, expected_lifetime_months=12)
        scale = m.DAYS_PER_MONTH / 7
        avg_cm = m.TIER_MRR_PROFESSIONAL - 10.5 * scale  # the trial is excluded from the average
        unit = report["unit_economics"]
        assert report["system"]["avg_contribution_margin_usd"] == pytest.approx(round(avg_cm, 2))
        assert unit["ltv_usd"] == pytest.approx(round(avg_cm * 12, 2))
        assert unit["ltv_cac_ratio"] == pytest.approx(round(avg_cm * 12 / 150.0, 2))
        assert unit["payback_months"] == pytest.approx(round(150.0 / avg_cm, 2))

    def test_cohorts_aggregate_margin_and_lift(self):
        cohorts = {c["cohort"]: c for c in
                   m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), _users())["cohorts"]}
        assert set(cohorts) == {"2026-06", "2026-07"}
        assert cohorts["2026-06"]["users"] == 1
        assert cohorts["2026-06"]["avg_engagement_lift_pct"] == 50.0
        assert cohorts["2026-06"]["median_engagement_rate"] == 0.03
        # The trial cohort has no CM% and no lift — reported as unknown, not as zero.
        assert cohorts["2026-07"]["avg_cm_pct"] is None
        assert cohorts["2026-07"]["avg_engagement_lift_pct"] is None

    def test_empty_user_set_is_safe(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), [])
        assert report["users"] == [] and report["cohorts"] == []
        assert report["system"]["mrr_usd"] == 0.0
        assert report["system"]["gross_margin_pct"] is None
        assert report["unit_economics"]["payback_months"] is None


class TestRenderers:
    def test_text_report_includes_headline_numbers(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), _users(),
                                       infra_monthly_usd=60.0, cac_usd=150.0,
                                       expected_lifetime_months=12)
        text = m.render_margin_report_text(report)
        assert "2026-07-19 → 2026-07-25" in text
        assert "Gross margin" in text and "Unit economics" in text
        assert "#1 professional" in text and "#2 free_trial" in text
        assert "2026-06:" in text

    def test_text_report_warns_when_ledger_missing(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), [],
                                       ledger_available=False)
        assert "cost_ledger is not present yet" in m.render_margin_report_text(report)

    def test_html_escapes_and_wraps_the_text_report(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), _users())
        report["users"][0]["tier"] = "<b>pro</b>"
        html = m.render_margin_report_html(report)
        assert html.startswith("<h2>LEM margin report</h2><pre")
        # Report content is escaped (so a tier/label can't inject markup) but stays readable.
        assert "&lt;b&gt;pro&lt;/b&gt;" in html and "→" in html


class TestCollectors:
    def _patch_db(self, rollups=None, users=None, rows=None, ledger=True):
        """Patch the db layer the collectors import; returns (context, rollup_mock)."""
        rollups = rollups or {}
        rollup = MagicMock(side_effect=lambda s, e, group_by="feature", user_id=None:
                           rollups.get((group_by, user_id), {}))
        return patch.multiple(
            "cqc_lem.utilities.db",
            get_cost_rollup=rollup,
            get_margin_users=MagicMock(return_value=users if users is not None else []),
            get_post_engagement_rows=MagicMock(return_value=rows or []),
            cost_ledger_available=MagicMock(return_value=ledger),
        ), rollup

    def test_daily_block_sums_mrr_over_active_subscribers(self):
        rollups = {("feature", None): {"content": 1.0}, ("category", None): {"llm": 4.0}}
        users = [{"subscription_tier": "professional"}, {"subscription_tier": "starter"},
                 {"subscription_tier": "free_trial"}]
        with self._patch_db(rollups=rollups, users=users)[0]:
            block = m.collect_daily_margin_block(date(2026, 7, 25))
        assert block["spend_usd"] == 1.0
        assert block["mrr_usd"] == pytest.approx(m.TIER_MRR_PROFESSIONAL + m.TIER_MRR_STARTER)
        assert block["ledger_available"] is True

    def test_daily_block_flags_a_missing_ledger(self):
        with self._patch_db(ledger=False)[0]:
            assert m.collect_daily_margin_block(date(2026, 7, 25))["ledger_available"] is False

    def test_weekly_report_window_and_per_user_costs(self):
        rollups = {("category", 5): {"llm": 7.0}, ("category", None): {"llm": 9.0}}
        users = [{"id": 5, "subscription_tier": "professional", "cohort": "2026-06"}]
        context, rollup = self._patch_db(rollups=rollups, users=users, rows=[_row(1), _row(30)])
        with context:
            report = m.collect_margin_report(end=date(2026, 7, 25), days=7)
        assert report["period"]["start"] == "2026-07-19"
        assert rollup.call_args_list[0][0] == (date(2026, 7, 19), date(2026, 7, 25))
        row = report["users"][0]
        assert row["user_id"] == 5 and row["cohort"] == "2026-06"
        assert row["variable_cost_usd"] == pytest.approx(7.0 * m.DAYS_PER_MONTH / 7, abs=1e-3)
        assert row["engagement_metric"] == m.METRIC_RATE
        assert report["system"]["unattributed_cost_usd"] == pytest.approx(2.0)


class TestDelivery:
    def _report(self):
        return m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), _users())

    def test_emails_and_tracks(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as send, \
             patch("cqc_lem.utilities.observability.track_margin_report") as track:
            result = m.send_weekly_margin_report(self._report(), to_email="owner@example.com")
        assert result == {"emailed": True, "tracked": True, "report": result["report"]}
        args, kwargs = send.call_args
        assert args[0] == "owner@example.com"
        assert "2026-07-19 → 2026-07-25" in args[1]
        assert kwargs["text_content"].startswith("LEM margin report")
        track.assert_called_once()

    def test_no_recipient_still_tracks(self):
        with patch(f"{_MARGIN}.MARGIN_REPORT_EMAIL", ""), \
             patch("cqc_lem.utilities.email._dispatch_email") as send, \
             patch("cqc_lem.utilities.observability.track_margin_report"):
            result = m.send_weekly_margin_report(self._report())
        send.assert_not_called()
        assert result["emailed"] is False and result["tracked"] is True

    def test_email_failure_does_not_lose_the_report(self):
        with patch("cqc_lem.utilities.email._dispatch_email", side_effect=RuntimeError("smtp down")), \
             patch("cqc_lem.utilities.observability.track_margin_report"):
            result = m.send_weekly_margin_report(self._report(), to_email="owner@example.com")
        assert result["emailed"] is False and result["tracked"] is True

    def test_tracking_failure_does_not_lose_the_email(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True), \
             patch("cqc_lem.utilities.observability.track_margin_report",
                   side_effect=RuntimeError("posthog down")):
            result = m.send_weekly_margin_report(self._report(), to_email="owner@example.com")
        assert result["emailed"] is True and result["tracked"] is False


class TestCli:
    def test_daily_json_prints_one_object(self, capsys):
        with patch(f"{_MARGIN}.collect_daily_margin_block", return_value={"spend_usd": 1.25}):
            assert m.main(["--daily-json"]) == 0
        import json as _json
        assert _json.loads(capsys.readouterr().out.strip()) == {"spend_usd": 1.25}

    def test_weekly_report_renders_text_without_emailing(self, capsys):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), [])
        with patch(f"{_MARGIN}.collect_margin_report", return_value=report) as collect, \
             patch(f"{_MARGIN}.send_weekly_margin_report") as send:
            assert m.main(["--weekly-report", "--days", "14"]) == 0
        collect.assert_called_once_with(days=14)
        send.assert_not_called()
        assert "LEM margin report" in capsys.readouterr().out

    def test_weekly_report_email_flag_delivers(self):
        report = m.build_margin_report(date(2026, 7, 19), date(2026, 7, 25), [])
        with patch(f"{_MARGIN}.collect_margin_report", return_value=report), \
             patch(f"{_MARGIN}.send_weekly_margin_report") as send:
            assert m.main(["--weekly-report", "--email", "--json"]) == 0
        send.assert_called_once_with(report)

    def test_no_flags_prints_help_and_fails(self, capsys):
        assert m.main([]) == 1
        assert "usage:" in capsys.readouterr().out
