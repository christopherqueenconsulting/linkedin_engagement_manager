"""Unit economics: cost → revenue → contribution margin (docs/cost-performance-margin-plan.md §C, §D.2).

Structured like `scripts/model_health_check.py`: PURE formulas + report builders first (no DB, no
network — these ARE the §C.1 formulas and are what the unit tests pin), then the DB-fed collectors,
delivery and CLI.

Two consumers:
  * the daily engagement snapshot (`scripts/perf_snapshot.sh`) appends the `margin` block to
    `metrics.jsonl`  →  `python -m cqc_lem.utilities.margin --daily-json`
  * the weekly owner report (Celery beat `auto_weekly_margin_report`, or `--weekly-report [--email]`)

Spend comes from the durable `cost_ledger` (plan §A.3). Until that table exists the collectors
report zero spend with `ledger_available: false` instead of failing, so the snapshot block and the
weekly report ship now and become exact the moment the ledger lands.
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping, Optional, Sequence

from cqc_lem.utilities.env_constants import (
    CAC_USD,
    EXPECTED_LIFETIME_MONTHS,
    INFRA_FIXED_MONTHLY,
    MARGIN_REPORT_EMAIL,
    TIER_MRR_ENTERPRISE,
    TIER_MRR_PROFESSIONAL,
    TIER_MRR_STARTER,
)
from cqc_lem.utilities.logger import log_info, log_warning
from cqc_lem.utilities.post_stats import engagement_rate, engagement_score

# ───────────────────────────── pure math (§C.1) ─────────────────────────────

# Monthly price per subscription tier. `free_trial` earns nothing yet but still costs us money, so
# it is a real $0-MRR row rather than an exclusion — that keeps trial drag visible in system margin.
TIER_MRR_USD = {
    "starter": TIER_MRR_STARTER,
    "professional": TIER_MRR_PROFESSIONAL,
    "enterprise": TIER_MRR_ENTERPRISE,
    "free_trial": 0.0,
}

# `cost_ledger.category` → the §C.1 cost kind it belongs to. An UNKNOWN category counts as variable
# so a newly added cost driver can never silently disappear from contribution margin.
SEMI_VARIABLE_CATEGORIES = frozenset({"proxy"})
FIXED_CATEGORIES = frozenset({"infra"})

# Average calendar month — used to put a 7-day window's spend on the same monthly basis as MRR.
DAYS_PER_MONTH = 30.4375

# Engagement-lift metric labels (mirrors post_stats' rate-mode gate).
METRIC_RATE = "engagement_rate"
METRIC_COUNT = "engagement"

# Column layout of `db.get_post_engagement_rows` tuples.
_IDX_SCHEDULED, _IDX_REACTIONS, _IDX_COMMENTS, _IDX_REPOSTS, _IDX_IMPRESSIONS = 0, 1, 2, 3, 9


def tier_mrr_usd(tier: Optional[str]) -> float:
    """Monthly recurring revenue for a subscription tier. Unknown/None → 0.0 (no revenue assumed)."""
    return float(TIER_MRR_USD.get((tier or "").strip().lower(), 0.0))


def split_cost_by_kind(by_category: Optional[Mapping[str, float]]) -> dict:
    """Bucket a `{category: usd}` rollup into the §C.1 kinds:
    `{"variable", "semi_variable", "fixed"}`. Proxy is semi-variable (linear-ish in users), infra is
    fixed/amortized, everything else — including categories added later — is variable."""
    out = {"variable": 0.0, "semi_variable": 0.0, "fixed": 0.0}
    for category, usd in (by_category or {}).items():
        key = ("fixed" if category in FIXED_CATEGORIES else
               "semi_variable" if category in SEMI_VARIABLE_CATEGORIES else "variable")
        out[key] += float(usd or 0.0)
    return out


def monthly_run_rate(period_usd: float, period_days: float) -> float:
    """Scale a window's spend to a monthly run rate so it is comparable with monthly MRR. A
    non-positive window returns the spend unchanged rather than dividing by zero."""
    if period_days <= 0:
        return float(period_usd or 0.0)
    return float(period_usd or 0.0) * (DAYS_PER_MONTH / float(period_days))


def contribution_margin(mrr_usd: float, variable_cost_usd: float,
                        semi_variable_cost_usd: float = 0.0) -> float:
    """§C.1 `contribution_margin(user) = MRR − variable_cost − semi_var_cost`."""
    return float(mrr_usd or 0.0) - float(variable_cost_usd or 0.0) - float(semi_variable_cost_usd or 0.0)


def margin_pct(margin_usd: float, mrr_usd: float) -> Optional[float]:
    """Margin as a fraction of revenue. None when there is no revenue — CM% is undefined then, and
    reporting 0% would read as "breaking even" for a free trial that is actually pure cost."""
    if not mrr_usd or float(mrr_usd) <= 0:
        return None
    return float(margin_usd or 0.0) / float(mrr_usd)


def allocated_fixed_per_user(infra_monthly_usd: float, active_users: int) -> float:
    """§C.1 `allocated_fixed(user) = INFRA_FIXED_MONTHLY / active_users(month)`. 0 users → 0.0."""
    if not active_users or active_users <= 0:
        return 0.0
    return float(infra_monthly_usd or 0.0) / float(active_users)


def fully_loaded_margin(contribution_margin_usd: float, allocated_fixed_usd: float) -> float:
    """§C.1 `fully_loaded_margin(user) = contribution_margin − allocated_fixed`."""
    return float(contribution_margin_usd or 0.0) - float(allocated_fixed_usd or 0.0)


def system_gross_margin(total_mrr_usd: float, total_variable_usd: float,
                        total_semi_variable_usd: float, infra_monthly_usd: float) -> dict:
    """§C.1 system block: `gross_margin$ = ΣMRR − Σvariable − Σsemi_var − INFRA_FIXED_MONTHLY` and
    `gross_margin% = gross_margin$ / ΣMRR` (None with no revenue)."""
    gross = (float(total_mrr_usd or 0.0) - float(total_variable_usd or 0.0)
             - float(total_semi_variable_usd or 0.0) - float(infra_monthly_usd or 0.0))
    return {"gross_margin_usd": gross, "gross_margin_pct": margin_pct(gross, total_mrr_usd)}


def ltv_usd(avg_monthly_cm_usd: float, expected_lifetime_months: float) -> float:
    """§C.1 `LTV = avg_monthly_contribution_margin × expected_lifetime_months`."""
    return float(avg_monthly_cm_usd or 0.0) * float(expected_lifetime_months or 0.0)


def payback_months(cac_usd: float, avg_monthly_cm_usd: float) -> Optional[float]:
    """§C.1 `payback = CAC / avg_monthly_contribution_margin`. None when CM ≤ 0 — a negative-margin
    customer never pays back — and None when CAC is unknown/0, which would otherwise report an
    instant payback."""
    if not cac_usd or float(cac_usd) <= 0:
        return None
    if not avg_monthly_cm_usd or float(avg_monthly_cm_usd) <= 0:
        return None
    return float(cac_usd or 0.0) / float(avg_monthly_cm_usd)


def ltv_cac_ratio(ltv: float, cac_usd: float) -> Optional[float]:
    """§C.1 `LTV:CAC` (target ≥ 3:1). None when CAC is unknown/0 — the ratio is meaningless then."""
    if not cac_usd or float(cac_usd) <= 0:
        return None
    return float(ltv or 0.0) / float(cac_usd)


def _cell(row: Sequence, index: int):
    return row[index] if len(row) > index else None


def _window_metric(rows: Sequence) -> dict:
    """Engagement for one set of post-stat rows: impression-normalized RATE when every row carries
    impressions, else average weighted engagement per post (the same rate-mode gate `post_stats`
    uses, so a partial-impression window is never mixed with a complete one)."""
    if not rows:
        return {"metric": None, "value": None, "samples": 0}
    if all(int(_cell(r, _IDX_IMPRESSIONS) or 0) > 0 for r in rows):
        totals = [sum(int(_cell(r, i) or 0) for r in rows)
                  for i in (_IDX_REACTIONS, _IDX_COMMENTS, _IDX_REPOSTS, _IDX_IMPRESSIONS)]
        return {"metric": METRIC_RATE, "value": engagement_rate(*totals), "samples": len(rows)}
    total = sum(engagement_score(_cell(r, _IDX_REACTIONS), _cell(r, _IDX_COMMENTS),
                                 _cell(r, _IDX_REPOSTS)) for r in rows)
    return {"metric": METRIC_COUNT, "value": total / len(rows), "samples": len(rows)}


def engagement_lift(rows: Iterable[Sequence], window_start: date) -> dict:
    """§B.2 engagement lift: the user's engagement in the report window vs. their EARLIER posts.
    `rows` are `db.get_post_engagement_rows` tuples (latest stats per post + its posted time).
    Returns `{"current", "baseline", "metric", "lift_pct", "samples", "baseline_samples"}`;
    `lift_pct` is None unless both windows have data on the SAME metric and a positive baseline."""
    current, baseline = [], []
    for row in rows or []:
        scheduled = _cell(row, _IDX_SCHEDULED) if row else None
        day = scheduled.date() if hasattr(scheduled, "date") else None
        if day is None:
            continue
        (current if day >= window_start else baseline).append(row)
    now, before = _window_metric(current), _window_metric(baseline)
    lift = None
    if (now["value"] is not None and before["value"] is not None
            and now["metric"] == before["metric"] and before["value"] > 0):
        lift = (now["value"] - before["value"]) / before["value"] * 100.0
    return {"current": now["value"], "baseline": before["value"],
            "metric": now["metric"] or before["metric"], "lift_pct": lift,
            "samples": now["samples"], "baseline_samples": before["samples"]}


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    return None if value is None else round(float(value), digits)


# ──────────────────────── pure builders (daily + weekly) ────────────────────────

def build_daily_margin_block(day: date, spend_by_feature: Optional[Mapping[str, float]],
                             mtd_cost_by_category: Optional[Mapping[str, float]],
                             total_mrr_usd: float, infra_monthly_usd: float = 0.0,
                             ledger_available: bool = True) -> dict:
    """The cost/margin block appended to the daily snapshot (§D.2 row 1).

    `spend_usd` / `cost_by_feature` are TODAY's spend; the margin figures are MONTH-TO-DATE against
    the current MRR run rate, because §C.1 margin is a monthly quantity — so the daily line trends
    toward the true month-end margin as the month fills in."""
    by_feature = {str(k): round(float(v or 0.0), 6) for k, v in (spend_by_feature or {}).items()}
    kinds = split_cost_by_kind(mtd_cost_by_category)
    cm = contribution_margin(total_mrr_usd, kinds["variable"], kinds["semi_variable"])
    system = system_gross_margin(total_mrr_usd, kinds["variable"], kinds["semi_variable"],
                                 infra_monthly_usd)
    return {
        "date": day.isoformat(),
        "spend_usd": round(sum(by_feature.values()), 6),
        "cost_by_feature": by_feature,
        "mtd_spend_usd": round(kinds["variable"] + kinds["semi_variable"] + kinds["fixed"], 6),
        "mrr_usd": _round(total_mrr_usd, 2),
        "contribution_margin_usd": _round(cm, 2),
        "gross_margin_pct": _round(system["gross_margin_pct"], 4),
        "ledger_available": bool(ledger_available),
    }


def build_margin_report(period_start: date, period_end: date, users: Sequence[Mapping],
                        system_cost_by_category: Optional[Mapping[str, float]] = None,
                        infra_monthly_usd: float = 0.0, cac_usd: float = 0.0,
                        expected_lifetime_months: float = 0.0,
                        ledger_available: bool = True) -> dict:
    """The weekly margin report (§D.2 row 3), pure: per-user CM & CM%, system gross margin, cohort
    engagement lift, LTV:CAC and payback.

    Every cost figure is put on a MONTHLY RUN-RATE basis (`period spend × 30.4375 / period_days`) so
    it is comparable with monthly MRR and the §C.1 formulas apply unchanged; the raw window spend
    rides along as `period_cost_usd`. Each `users` entry is
    `{"user_id", "tier", "cohort", "cost_by_category", "engagement"}`."""
    days = max((period_end - period_start).days + 1, 1)
    rows, margins, total_mrr, total_variable, total_semi = [], [], 0.0, 0.0, 0.0
    attributed = 0.0
    for user in users or []:
        kinds = split_cost_by_kind(user.get("cost_by_category"))
        variable = monthly_run_rate(kinds["variable"], days)
        semi = monthly_run_rate(kinds["semi_variable"], days)
        mrr = tier_mrr_usd(user.get("tier"))
        cm = contribution_margin(mrr, variable, semi)
        engagement = dict(user.get("engagement") or {})
        attributed += kinds["variable"] + kinds["semi_variable"] + kinds["fixed"]
        total_mrr += mrr
        total_variable += variable
        total_semi += semi
        margins.append(cm)
        rows.append({
            "user_id": user.get("user_id"),
            "tier": user.get("tier"),
            "cohort": user.get("cohort"),
            "mrr_usd": _round(mrr, 2),
            "variable_cost_usd": _round(variable, 4),
            "semi_variable_cost_usd": _round(semi, 4),
            "period_cost_usd": _round(kinds["variable"] + kinds["semi_variable"], 4),
            "contribution_margin_usd": _round(cm, 2),
            "cm_pct": _round(margin_pct(cm, mrr), 4),
            "engagement_rate": _round(engagement.get("current"), 6),
            "engagement_metric": engagement.get("metric"),
            "engagement_lift_pct": _round(engagement.get("lift_pct"), 2),
        })

    active_users = len(rows)
    per_user_fixed = allocated_fixed_per_user(infra_monthly_usd, active_users)
    for row, cm in zip(rows, margins):
        row["allocated_fixed_usd"] = _round(per_user_fixed, 4)
        row["fully_loaded_margin_usd"] = _round(fully_loaded_margin(cm, per_user_fixed), 2)
    paying = [(row, cm) for row, cm in zip(rows, margins) if (row["mrr_usd"] or 0.0) > 0]
    rows.sort(key=lambda r: (r["contribution_margin_usd"] is None,
                             r["contribution_margin_usd"] or 0.0))

    if system_cost_by_category is None:
        # No system-wide rollup supplied → the per-user sums ARE the system totals.
        system_variable, system_semi = total_variable, total_semi
        system_total = attributed
    else:
        # Shared/system ledger rows carry a NULL user_id, so the system rollup is a superset of the
        # per-user ones; the remainder is exactly the plan's §E.2 "unattributed spend" signal.
        system_kinds = split_cost_by_kind(system_cost_by_category)
        system_variable = monthly_run_rate(system_kinds["variable"], days)
        system_semi = monthly_run_rate(system_kinds["semi_variable"], days)
        system_total = system_kinds["variable"] + system_kinds["semi_variable"] + system_kinds["fixed"]
    gross = system_gross_margin(total_mrr, system_variable, system_semi, infra_monthly_usd)

    avg_cm = (sum(cm for _, cm in paying) / len(paying)) if paying else 0.0
    ltv = ltv_usd(avg_cm, expected_lifetime_months)

    return {
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat(), "days": days,
                   "basis": "monthly_run_rate"},
        "ledger_available": bool(ledger_available),
        "users": rows,
        "system": {
            "active_users": active_users,
            "paying_users": len(paying),
            "mrr_usd": _round(total_mrr, 2),
            "variable_cost_usd": _round(system_variable, 4),
            "semi_variable_cost_usd": _round(system_semi, 4),
            "infra_fixed_usd": _round(infra_monthly_usd, 2),
            "allocated_fixed_per_user_usd": _round(per_user_fixed, 4),
            "period_cost_usd": _round(system_total, 4),
            "unattributed_cost_usd": _round(max(system_total - attributed, 0.0), 4),
            "gross_margin_usd": _round(gross["gross_margin_usd"], 2),
            "gross_margin_pct": _round(gross["gross_margin_pct"], 4),
            "avg_contribution_margin_usd": _round(avg_cm, 2),
        },
        "cohorts": _build_cohorts(rows),
        "unit_economics": {
            "cac_usd": _round(cac_usd, 2),
            "expected_lifetime_months": _round(expected_lifetime_months, 2),
            "ltv_usd": _round(ltv, 2),
            "ltv_cac_ratio": _round(ltv_cac_ratio(ltv, cac_usd), 2),
            "payback_months": _round(payback_months(cac_usd, avg_cm), 2),
        },
    }


def _build_cohorts(rows: Sequence[Mapping]) -> list:
    """Per signup-month cohort aggregates (§B.2): CM, CM% and engagement lift, so a newer cohort can
    be read against an older one. Sorted by cohort so the trend reads top-to-bottom."""
    groups: dict = {}
    for row in rows:
        groups.setdefault(row.get("cohort") or "unknown", []).append(row)
    out = []
    for cohort in sorted(groups):
        members = groups[cohort]
        cms = [m["contribution_margin_usd"] for m in members if m["contribution_margin_usd"] is not None]
        pcts = [m["cm_pct"] for m in members if m["cm_pct"] is not None]
        lifts = [m["engagement_lift_pct"] for m in members if m["engagement_lift_pct"] is not None]
        rates = [m["engagement_rate"] for m in members if m["engagement_rate"] is not None]
        out.append({
            "cohort": cohort,
            "users": len(members),
            "avg_cm_usd": _round(statistics.fmean(cms), 2) if cms else None,
            "avg_cm_pct": _round(statistics.fmean(pcts), 4) if pcts else None,
            "median_engagement_rate": _round(statistics.median(rates), 6) if rates else None,
            "avg_engagement_lift_pct": _round(statistics.fmean(lifts), 2) if lifts else None,
        })
    return out


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _usd(value: Optional[float]) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def render_margin_report_text(report: Mapping) -> str:
    """Plain-text owner report — also the plaintext alternative of the email (a missing text/plain
    part is a spam signal, see `email._dispatch_email`)."""
    period, system, unit = report.get("period", {}), report.get("system", {}), report.get("unit_economics", {})
    lines = [f"LEM margin report — {period.get('start')} → {period.get('end')} "
             f"({period.get('days')}d, {period.get('basis')} basis)", ""]
    if not report.get("ledger_available", True):
        lines += ["NOTE: cost_ledger is not present yet — spend reads as $0 and margin is "
                  "revenue-only until it lands.", ""]
    lines += [
        f"System: {system.get('active_users')} active user(s), {system.get('paying_users')} paying",
        f"  MRR                 {_usd(system.get('mrr_usd'))}",
        f"  Variable cost       {_usd(system.get('variable_cost_usd'))}",
        f"  Semi-variable cost  {_usd(system.get('semi_variable_cost_usd'))}",
        f"  Fixed infra         {_usd(system.get('infra_fixed_usd'))}",
        f"  Gross margin        {_usd(system.get('gross_margin_usd'))} ({_pct(system.get('gross_margin_pct'))})",
        f"  Unattributed spend  {_usd(system.get('unattributed_cost_usd'))}",
        "",
        f"Unit economics: LTV {_usd(unit.get('ltv_usd'))} · CAC {_usd(unit.get('cac_usd'))} · "
        f"LTV:CAC {unit.get('ltv_cac_ratio') if unit.get('ltv_cac_ratio') is not None else 'n/a'} · "
        f"payback {unit.get('payback_months') if unit.get('payback_months') is not None else 'n/a'} mo",
        "",
        "Per user (worst margin first):",
    ]
    for row in report.get("users", []):
        lift = row.get("engagement_lift_pct")
        lines.append(f"  #{row.get('user_id')} {row.get('tier') or '-'}: "
                     f"MRR {_usd(row.get('mrr_usd'))}, cost {_usd(row.get('variable_cost_usd'))}"
                     f"+{_usd(row.get('semi_variable_cost_usd'))}, "
                     f"CM {_usd(row.get('contribution_margin_usd'))} ({_pct(row.get('cm_pct'))}), "
                     f"engagement lift {'n/a' if lift is None else f'{lift:+.1f}%'}")
    lines += ["", "Cohorts:"]
    for cohort in report.get("cohorts", []):
        lift = cohort.get("avg_engagement_lift_pct")
        lines.append(f"  {cohort.get('cohort')}: {cohort.get('users')} user(s), "
                     f"avg CM {_usd(cohort.get('avg_cm_usd'))} ({_pct(cohort.get('avg_cm_pct'))}), "
                     f"avg lift {'n/a' if lift is None else f'{lift:+.1f}%'}")
    return "\n".join(lines)


def render_margin_report_html(report: Mapping) -> str:
    """HTML body of the owner email — the text report in a <pre> block keeps it readable in every
    client without a template, and the numbers are identical to the plaintext part."""
    body = (render_margin_report_text(report)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return ("<h2>LEM margin report</h2><pre style=\"font-family:ui-monospace,monospace;"
            f"font-size:13px;line-height:1.5\">{body}</pre>")


# ─────────────────────── DB-fed collectors & delivery ───────────────────────

def _today() -> date:
    return datetime.now(timezone.utc).date()


def collect_daily_margin_block(day: Optional[date] = None) -> dict:
    """Read today's spend + current MRR run rate and build the snapshot block."""
    from cqc_lem.utilities.db import cost_ledger_available, get_cost_rollup, get_margin_users

    day = day or _today()
    users = get_margin_users()
    total_mrr = sum(tier_mrr_usd(u.get("subscription_tier")) for u in users)
    return build_daily_margin_block(
        day,
        get_cost_rollup(day, day, group_by="feature"),
        get_cost_rollup(day.replace(day=1), day, group_by="category"),
        total_mrr,
        infra_monthly_usd=INFRA_FIXED_MONTHLY,
        ledger_available=cost_ledger_available(),
    )


def collect_margin_report(end: Optional[date] = None, days: int = 7) -> dict:
    """Read the trailing `days` window per user and build the weekly report."""
    from cqc_lem.utilities.db import (cost_ledger_available, get_cost_rollup, get_margin_users,
                                      get_post_engagement_rows)

    end = end or _today()
    start = end - timedelta(days=max(days, 1) - 1)
    users = []
    for row in get_margin_users():
        user_id = row.get("id")
        users.append({
            "user_id": user_id,
            "tier": row.get("subscription_tier"),
            "cohort": row.get("cohort"),
            "cost_by_category": get_cost_rollup(start, end, group_by="category", user_id=user_id),
            "engagement": engagement_lift(get_post_engagement_rows(user_id), start),
        })
    return build_margin_report(
        start, end, users,
        system_cost_by_category=get_cost_rollup(start, end, group_by="category"),
        infra_monthly_usd=INFRA_FIXED_MONTHLY,
        cac_usd=CAC_USD,
        expected_lifetime_months=EXPECTED_LIFETIME_MONTHS,
        ledger_available=cost_ledger_available(),
    )


def send_weekly_margin_report(report: Optional[Mapping] = None,
                              to_email: Optional[str] = None) -> dict:
    """Deliver the weekly report to the owner: email + a PostHog `margin_report` event for the
    unit-economics scorecard (§E.1.4). Returns `{"emailed": bool, "tracked": bool, "report": ...}` —
    delivery failures are logged, never raised, so a beat run is not lost over an email hiccup."""
    from cqc_lem.utilities.email import _dispatch_email
    from cqc_lem.utilities.observability import track_margin_report

    report = report if report is not None else collect_margin_report()
    text = render_margin_report_text(report)
    recipient = to_email or MARGIN_REPORT_EMAIL
    emailed = False
    if recipient:
        period = report.get("period", {})
        try:
            emailed = bool(_dispatch_email(
                recipient, f"LEM margin report — {period.get('start')} → {period.get('end')}",
                render_margin_report_html(report), text_content=text))
        except Exception as e:
            log_warning("Margin report email failed", exc=e, task_name="auto_weekly_margin_report")
    else:
        log_warning("MARGIN_REPORT_EMAIL not set — margin report not emailed",
                    task_name="auto_weekly_margin_report")
    tracked = False
    try:
        track_margin_report(report)
        tracked = True
    except Exception as e:
        log_warning("Margin report PostHog capture failed", exc=e,
                    task_name="auto_weekly_margin_report")
    log_info(f"Margin report generated (emailed={emailed}, tracked={tracked})",
             task_name="auto_weekly_margin_report")
    return {"emailed": emailed, "tracked": tracked, "report": report}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LEM cost/margin reporting")
    parser.add_argument("--daily-json", action="store_true",
                        help="print today's cost/margin block as one JSON object (snapshot.sh)")
    parser.add_argument("--weekly-report", action="store_true", help="build the weekly margin report")
    parser.add_argument("--email", action="store_true", help="also email the weekly report to the owner")
    parser.add_argument("--json", action="store_true", help="print the weekly report as JSON")
    parser.add_argument("--days", type=int, default=7, help="weekly report window in days (default 7)")
    args = parser.parse_args(argv)

    if args.daily_json:
        print(json.dumps(collect_daily_margin_block()))
        return 0
    if args.weekly_report:
        report = collect_margin_report(days=args.days)
        if args.email:
            send_weekly_margin_report(report)
        print(json.dumps(report) if args.json else render_margin_report_text(report))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
