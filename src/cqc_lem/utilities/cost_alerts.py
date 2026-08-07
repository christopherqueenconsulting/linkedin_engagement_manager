"""Budget-threshold alerts + spend anomaly detection (docs/cost-performance-margin-plan.md §E.2).

Structured like `utilities/margin.py`: PURE evaluators first (no DB, no network — these are what the
unit tests pin with synthetic breaches), then the DB/PostHog-fed collectors, delivery and CLI.

Five checks, each with an env-configurable threshold (`COST_ALERT_*`, see `.env.example`):

| Check | Source | Fires when |
|---|---|---|
| `user_cost_ceiling`   | `cost_ledger` via the margin report | a user's variable+semi-variable cost > X% of their tier MRR |
| `gross_margin_floor`  | `cost_ledger` via the margin report | system gross_margin% < the target floor |
| `spend_anomaly`       | `cost_ledger` daily totals          | a day's spend > μ+Nσ of the trailing window, or > an absolute daily budget |
| `cache_hit_collapse`  | PostHog `llm_call` events           | LLM cache-hit% drops vs its trailing baseline (or under an absolute floor) |
| `unattributed_spend`  | `cost_ledger` rollups               | share of spend with no user or no feature rises above a threshold |

Every check reports `ok`, `alert` or **`skipped` with a reason** — an unreadable ledger or an
unconfigured PostHog read path is unknown data, never a $0 "all clear" and never a false alarm.
Runs daily via Celery beat (`run_scheduler.auto_daily_cost_alerts`) over the last COMPLETE UTC day.
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Optional, Sequence

from cqc_lem.utilities.env_constants import (
    COST_ALERT_CACHE_DROP,
    COST_ALERT_CACHE_FLOOR,
    COST_ALERT_CACHE_MIN_CALLS,
    COST_ALERT_DAILY_BUDGET,
    COST_ALERT_EMAIL,
    COST_ALERT_MARGIN_FLOOR,
    COST_ALERT_MIN_SPEND,
    COST_ALERT_SPEND_SIGMA,
    COST_ALERT_UNATTRIBUTED,
    COST_ALERT_USER_COST_PCT,
    MARGIN_REPORT_EMAIL,
)
from cqc_lem.utilities.logger import log_error, log_info, log_warning

# ───────────────────────────── vocabulary ─────────────────────────────

CHECK_USER_COST = "user_cost_ceiling"
CHECK_MARGIN_FLOOR = "gross_margin_floor"
CHECK_SPEND_ANOMALY = "spend_anomaly"
CHECK_CACHE_COLLAPSE = "cache_hit_collapse"
CHECK_UNATTRIBUTED = "unattributed_spend"

STATUS_OK = "ok"
STATUS_ALERT = "alert"
STATUS_SKIPPED = "skipped"

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

# Prefix of the per-breach log line `send_cost_alerts` emits. `log_escalation.
# BUILTIN_EXCLUDED_PREFIXES` pins this exact string so a breach never escalates into a grouped
# $exception: a threshold breach is a measurement, not a code fault, and it repeats daily for as
# long as the spend does (#1071).
ALERT_LOG_PREFIX = "Cost alert ["

# Trailing days the anomaly baseline is drawn from, and the minimum number of them that must carry
# ledger rows before μ/σ mean anything.
ANOMALY_WINDOW_DAYS = 7
ANOMALY_MIN_DAYS = 3

# Rollup keys that mean "we could not attribute this" on either dimension.
UNATTRIBUTED_KEYS = frozenset({"unknown", "system", "", "none"})

# Checks fed by `cost_ledger` (through the margin report or the daily rollups). Without the table the
# margin report derives its cost fields from empty rollups, so these would read a confident $0 "ok" —
# indistinguishable from a genuinely cheap day. They are reported skipped instead: unknown ≠ zero.
LEDGER_BACKED_CHECKS = frozenset({CHECK_USER_COST, CHECK_MARGIN_FLOOR, CHECK_SPEND_ANOMALY,
                                  CHECK_UNATTRIBUTED})
LEDGER_MISSING_REASON = "cost_ledger not present — spend is unknown, not $0"


def default_thresholds() -> dict:
    """Thresholds from the environment (`COST_ALERT_*`). Every caller merges over this, so a test —
    or a CLI run — can override one threshold without restating the rest.
    """
    return {
        "user_cost_pct_of_mrr": COST_ALERT_USER_COST_PCT,
        "gross_margin_floor": COST_ALERT_MARGIN_FLOOR,
        "spend_sigma": COST_ALERT_SPEND_SIGMA,
        "daily_budget_usd": COST_ALERT_DAILY_BUDGET,
        "min_spend_usd": COST_ALERT_MIN_SPEND,
        "cache_drop_pct": COST_ALERT_CACHE_DROP,
        "cache_hit_floor": COST_ALERT_CACHE_FLOOR,
        "cache_min_calls": COST_ALERT_CACHE_MIN_CALLS,
        "unattributed_pct": COST_ALERT_UNATTRIBUTED,
    }


def _check(check_id: str, status: str, alerts: Optional[Sequence[Mapping]] = None,
           reason: Optional[str] = None, **detail) -> dict:
    return {"check": check_id, "status": status, "alerts": [dict(a) for a in (alerts or [])],
            "reason": reason, **detail}


def _alert(check_id: str, severity: str, title: str, detail: str, value: Optional[float],
           threshold: Optional[float], **extra) -> dict:
    return {"check": check_id, "severity": severity, "title": title, "detail": detail,
            "value": value, "threshold": threshold, **extra}


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    return None if value is None else round(float(value), digits)


# ───────────────────────────── pure evaluators ─────────────────────────────

def evaluate_user_cost_ceiling(users: Optional[Sequence[Mapping]],
                               pct_of_mrr: float = COST_ALERT_USER_COST_PCT) -> dict:
    """§E.2 per-user cost ceiling over the margin report's `users` rows (costs already on a monthly
    run-rate basis, so they are comparable with monthly MRR). Users with $0 MRR — free trials — are
    skipped: "% of MRR" is undefined against no revenue, and their drag is caught by the system
    gross-margin floor instead. A user whose cost exceeds their whole MRR is critical: that user is
    margin-negative, not merely expensive.
    """
    alerts, judged = [], 0
    for row in users or []:
        mrr = float(row.get("mrr_usd") or 0.0)
        if mrr <= 0:
            continue
        judged += 1
        cost = float(row.get("variable_cost_usd") or 0.0) + float(row.get("semi_variable_cost_usd") or 0.0)
        ratio = cost / mrr
        if ratio <= pct_of_mrr:
            continue
        alerts.append(_alert(
            CHECK_USER_COST,
            SEVERITY_CRITICAL if ratio >= 1.0 else SEVERITY_WARNING,
            f"User #{row.get('user_id')} variable cost is {ratio * 100:.1f}% of tier MRR",
            f"${cost:,.2f}/mo variable+semi-variable against ${mrr:,.2f}/mo MRR "
            f"({row.get('tier') or 'unknown tier'}); ceiling is {pct_of_mrr * 100:.0f}%.",
            _round(ratio, 4), pct_of_mrr,
            user_id=row.get("user_id"), tier=row.get("tier"),
            cost_usd=_round(cost, 4), mrr_usd=_round(mrr, 2),
        ))
    if not judged:
        return _check(CHECK_USER_COST, STATUS_SKIPPED, reason="no paying users to judge",
                      users_judged=0)
    return _check(CHECK_USER_COST, STATUS_ALERT if alerts else STATUS_OK, alerts,
                  users_judged=judged)


def evaluate_gross_margin_floor(system: Optional[Mapping],
                                floor: float = COST_ALERT_MARGIN_FLOOR) -> dict:
    """§E.2 system gross-margin floor. `gross_margin_pct` is None with no revenue (see
    `margin.margin_pct`) — that is undefined, not a breach, so the check skips.
    """
    pct = (system or {}).get("gross_margin_pct")
    if pct is None:
        return _check(CHECK_MARGIN_FLOOR, STATUS_SKIPPED, reason="no revenue — margin % undefined")
    pct = float(pct)
    if pct >= floor:
        return _check(CHECK_MARGIN_FLOOR, STATUS_OK, value=_round(pct, 4), threshold=floor)
    return _check(CHECK_MARGIN_FLOOR, STATUS_ALERT, [_alert(
        CHECK_MARGIN_FLOOR,
        SEVERITY_CRITICAL if pct < 0 else SEVERITY_WARNING,
        f"System gross margin {pct * 100:.1f}% is below the {floor * 100:.0f}% floor",
        f"Gross margin ${float((system or {}).get('gross_margin_usd') or 0.0):,.2f} on "
        f"${float((system or {}).get('mrr_usd') or 0.0):,.2f} MRR across "
        f"{(system or {}).get('active_users')} active user(s).",
        _round(pct, 4), floor,
        gross_margin_usd=(system or {}).get("gross_margin_usd"),
        mrr_usd=(system or {}).get("mrr_usd"),
    )], value=_round(pct, 4), threshold=floor)


def _day_key(day) -> str:
    return day.isoformat() if hasattr(day, "isoformat") else str(day)


def evaluate_spend_anomaly(daily_spend: Optional[Mapping], day: date,
                           sigma: float = COST_ALERT_SPEND_SIGMA,
                           daily_budget_usd: float = COST_ALERT_DAILY_BUDGET,
                           min_spend_usd: float = COST_ALERT_MIN_SPEND,
                           min_days: int = ANOMALY_MIN_DAYS) -> dict:
    """§E.2 spend anomaly: `day`'s spend against μ+Nσ of the trailing days in `daily_spend`, plus an
    optional hard daily budget.

    Days ABSENT from `daily_spend` are not counted as $0 — a ledger that started capturing mid-window
    would otherwise show a zero baseline and flag its first real day as a spike. `min_spend_usd` is
    the noise floor: on a near-idle ledger σ collapses to ~0 and any movement clears μ+Nσ, so a day
    under the floor never alerts. The absolute budget is judged independently of the baseline, since
    it is meaningful on day one.
    """
    series = {str(k): float(v or 0.0) for k, v in (daily_spend or {}).items()}
    key = _day_key(day)
    today = series.get(key)
    if today is None:
        return _check(CHECK_SPEND_ANOMALY, STATUS_SKIPPED, reason=f"no ledger rows for {key}")

    alerts = []
    if daily_budget_usd and daily_budget_usd > 0 and today > daily_budget_usd:
        alerts.append(_alert(
            CHECK_SPEND_ANOMALY, SEVERITY_CRITICAL,
            f"Daily spend ${today:,.2f} exceeded the ${daily_budget_usd:,.2f} budget",
            f"{key} spend is {today / daily_budget_usd:.1f}× the absolute daily budget.",
            _round(today, 4), daily_budget_usd, day=key, kind="budget",
        ))

    baseline = [usd for other, usd in series.items() if other < key]
    if len(baseline) < min_days:
        status = STATUS_ALERT if alerts else STATUS_SKIPPED
        return _check(CHECK_SPEND_ANOMALY, status, alerts,
                      reason=None if alerts else f"only {len(baseline)} trailing day(s) of ledger data",
                      spend_usd=_round(today, 4), baseline_days=len(baseline))

    mean = statistics.fmean(baseline)
    stdev = statistics.pstdev(baseline)
    threshold = mean + (sigma * stdev)
    if today > threshold and today >= min_spend_usd:
        alerts.append(_alert(
            CHECK_SPEND_ANOMALY, SEVERITY_WARNING,
            f"Daily spend ${today:,.2f} is above μ+{sigma:g}σ (${threshold:,.2f})",
            f"{key} spend vs a {len(baseline)}-day trailing mean of ${mean:,.2f} "
            f"(σ ${stdev:,.2f}).",
            _round(today, 4), _round(threshold, 4), day=key, kind="sigma",
            mean_usd=_round(mean, 4), stdev_usd=_round(stdev, 4),
        ))
    return _check(CHECK_SPEND_ANOMALY, STATUS_ALERT if alerts else STATUS_OK, alerts,
                  spend_usd=_round(today, 4), mean_usd=_round(mean, 4), stdev_usd=_round(stdev, 4),
                  threshold=_round(threshold, 4), baseline_days=len(baseline))


def evaluate_cache_hit_collapse(cache: Optional[Mapping],
                                drop_pct: float = COST_ALERT_CACHE_DROP,
                                floor: float = COST_ALERT_CACHE_FLOOR,
                                min_calls: int = COST_ALERT_CACHE_MIN_CALLS) -> dict:
    """§E.2 cache-hit-rate collapse. `cache` is `{"rate", "calls", "baseline_rate", "baseline_calls"}`
    from `collect_cache_hit_stats` (None when PostHog isn't readable → skipped).

    Alerts on a RELATIVE drop against the trailing baseline — the absolute rate is workload-dependent,
    the drop is what predicts a cost spike — and optionally on an absolute floor (0 disables it).
    Both are gated on `min_calls` so a quiet day of a handful of calls can't read as a collapse.
    """
    if not cache:
        return _check(CHECK_CACHE_COLLAPSE, STATUS_SKIPPED,
                      reason="PostHog llm_call data unavailable")
    calls = int(cache.get("calls") or 0)
    rate = cache.get("rate")
    if rate is None or calls < min_calls:
        return _check(CHECK_CACHE_COLLAPSE, STATUS_SKIPPED,
                      reason=f"only {calls} llm_call event(s) — under the {min_calls}-call minimum",
                      calls=calls, rate=_round(rate, 4))
    rate = float(rate)
    baseline = cache.get("baseline_rate")
    baseline_calls = int(cache.get("baseline_calls") or 0)
    alerts = []
    if floor and floor > 0 and rate < floor:
        alerts.append(_alert(
            CHECK_CACHE_COLLAPSE, SEVERITY_WARNING,
            f"LLM cache-hit rate {rate * 100:.1f}% is under the {floor * 100:.0f}% floor",
            f"{calls} llm_call event(s) on the day — every miss is billed spend.",
            _round(rate, 4), floor, kind="floor", calls=calls,
        ))
    if baseline is not None and baseline_calls >= min_calls and float(baseline) > 0:
        baseline = float(baseline)
        drop = (baseline - rate) / baseline
        if drop >= drop_pct:
            alerts.append(_alert(
                CHECK_CACHE_COLLAPSE, SEVERITY_WARNING,
                f"LLM cache-hit rate fell {drop * 100:.1f}% vs the trailing baseline",
                f"{rate * 100:.1f}% today vs {baseline * 100:.1f}% baseline over "
                f"{baseline_calls} call(s) — expect a proportional cost spike.",
                _round(drop, 4), drop_pct, kind="drop",
                rate=_round(rate, 4), baseline_rate=_round(baseline, 4), calls=calls,
            ))
    return _check(CHECK_CACHE_COLLAPSE, STATUS_ALERT if alerts else STATUS_OK, alerts,
                  rate=_round(rate, 4), baseline_rate=_round(baseline, 4), calls=calls)


def evaluate_unattributed_spend(system: Optional[Mapping], cost_by_feature: Optional[Mapping],
                                max_share: float = COST_ALERT_UNATTRIBUTED) -> dict:
    """§E.2 unattributed spend — an instrumentation regression detector, on BOTH dimensions:
    spend carrying no `user_id` (the margin report's `unattributed_cost_usd`, i.e. system/shared
    ledger rows) and spend whose `feature` is missing or `system`. The larger share drives the alert
    so a regression on either dimension surfaces.
    """
    total = float((system or {}).get("period_cost_usd") or 0.0)
    if total <= 0:
        return _check(CHECK_UNATTRIBUTED, STATUS_SKIPPED, reason="no spend in the window")
    by_user = float((system or {}).get("unattributed_cost_usd") or 0.0) / total
    featureless = sum(float(usd or 0.0) for key, usd in (cost_by_feature or {}).items()
                      if str(key).strip().lower() in UNATTRIBUTED_KEYS)
    by_feature = featureless / total
    worst = max(by_user, by_feature)
    if worst <= max_share:
        return _check(CHECK_UNATTRIBUTED, STATUS_OK, user_share=_round(by_user, 4),
                      feature_share=_round(by_feature, 4), threshold=max_share)
    dimension = "user_id" if by_user >= by_feature else "feature"
    return _check(CHECK_UNATTRIBUTED, STATUS_ALERT, [_alert(
        CHECK_UNATTRIBUTED, SEVERITY_WARNING,
        f"{worst * 100:.1f}% of spend has no {dimension}",
        f"${total * worst:,.2f} of ${total:,.2f} window spend is unattributed by {dimension} "
        f"(threshold {max_share * 100:.0f}%) — cost cannot be sliced, check the instrumentation.",
        _round(worst, 4), max_share, dimension=dimension,
        user_share=_round(by_user, 4), feature_share=_round(by_feature, 4),
        unattributed_usd=_round(total * worst, 4), total_usd=_round(total, 4),
    )], user_share=_round(by_user, 4), feature_share=_round(by_feature, 4), threshold=max_share)


# ───────────────────────────── pure report builder ─────────────────────────────

def _ledger_missing(check_id: str) -> dict:
    return _check(check_id, STATUS_SKIPPED, reason=LEDGER_MISSING_REASON)


def build_cost_alert_report(day: date, margin_report: Optional[Mapping],
                            cost_by_feature: Optional[Mapping] = None,
                            daily_spend: Optional[Mapping] = None,
                            cache: Optional[Mapping] = None,
                            thresholds: Optional[Mapping] = None) -> dict:
    """Run all five §E.2 checks and collect them into one report. `margin_report` is a
    `margin.build_margin_report` result (the ledger-backed spend/MRR join); the rest are the raw
    per-day and per-feature rollups and the PostHog cache stats.

    When the margin report says `ledger_available: false` the `LEDGER_BACKED_CHECKS` are not run at
    all — their inputs would be zeros from empty rollups, not measurements. Only the PostHog-fed
    cache check still has real data to judge.
    """
    limits = {**default_thresholds(), **(thresholds or {})}
    report = margin_report or {}
    system = report.get("system") or {}
    ledger = bool(report.get("ledger_available", False))
    checks = [
        evaluate_user_cost_ceiling(report.get("users"), limits["user_cost_pct_of_mrr"])
        if ledger else _ledger_missing(CHECK_USER_COST),
        evaluate_gross_margin_floor(system, limits["gross_margin_floor"])
        if ledger else _ledger_missing(CHECK_MARGIN_FLOOR),
        evaluate_spend_anomaly(daily_spend, day, limits["spend_sigma"],
                               limits["daily_budget_usd"], limits["min_spend_usd"])
        if ledger else _ledger_missing(CHECK_SPEND_ANOMALY),
        evaluate_cache_hit_collapse(cache, limits["cache_drop_pct"], limits["cache_hit_floor"],
                                    limits["cache_min_calls"]),
        evaluate_unattributed_spend(system, cost_by_feature, limits["unattributed_pct"])
        if ledger else _ledger_missing(CHECK_UNATTRIBUTED),
    ]
    alerts = [alert for check in checks for alert in check["alerts"]]
    return {
        "date": day.isoformat(),
        "period": report.get("period") or {},
        "ledger_available": ledger,
        "thresholds": limits,
        "checks": checks,
        "alerts": alerts,
        "alert_count": len(alerts),
        "critical_count": sum(1 for a in alerts if a.get("severity") == SEVERITY_CRITICAL),
        "skipped_checks": [c["check"] for c in checks if c["status"] == STATUS_SKIPPED],
    }


def render_cost_alerts_text(report: Mapping) -> str:
    """Plain-text alert digest — also the plaintext alternative of the email (a missing text/plain
    part is a spam signal, see `email._dispatch_email`).
    """
    lines = [f"LEM cost alerts — {report.get('date')} "
             f"({report.get('alert_count', 0)} alert(s), "
             f"{report.get('critical_count', 0)} critical)", ""]
    if not report.get("ledger_available", True):
        lines += ["NOTE: cost_ledger is not present yet — spend is UNKNOWN (not $0), so the " +
                  "ledger-backed checks below are skipped, not passing.", ""]
    for alert in report.get("alerts") or []:
        lines.append(f"[{str(alert.get('severity', '')).upper()}] {alert.get('title')}")
        lines.append(f"    {alert.get('detail')}")
    if not report.get("alerts"):
        lines.append("No thresholds breached.")
    lines += ["", "Checks:"]
    for check in report.get("checks") or []:
        reason = f" — {check.get('reason')}" if check.get("reason") else ""
        lines.append(f"  {check.get('check')}: {check.get('status')}{reason}")
    return "\n".join(lines)


def render_cost_alerts_html(report: Mapping) -> str:
    """HTML body of the owner email — the text digest in a <pre> block reads correctly in every
    client without a template, and the numbers are identical to the plaintext part.
    """
    body = (render_cost_alerts_text(report)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return ("<h2>LEM cost alerts</h2><pre style=\"font-family:ui-monospace,monospace;"
            f"font-size:13px;line-height:1.5\">{body}</pre>")


# ─────────────────────── DB/PostHog-fed collectors & delivery ───────────────────────

def _today() -> date:
    return datetime.now(timezone.utc).date()


def _last_complete_day() -> date:
    """Yesterday (UTC). The checks score a WHOLE day — today is still filling up, and a partial day
    would read as a spend collapse rather than a real signal.
    """
    return _today() - timedelta(days=1)


CACHE_HIT_SQL = """
SELECT toDate(timestamp) AS day,
       countIf(properties.cached = true) AS hits,
       count() AS calls
FROM events
WHERE event = 'llm_call' AND timestamp >= toDateTime('{start}') AND timestamp < toDateTime('{end}')
GROUP BY day
ORDER BY day
""".strip()


def collect_cache_hit_stats(day: Optional[date] = None,
                            window_days: int = ANOMALY_WINDOW_DAYS) -> Optional[dict]:
    """LLM cache-hit rate for `day` and the trailing `window_days` baseline, read from PostHog
    `llm_call` events (the only place the `cached` flag lives — `cost_ledger` stores net spend).
    None when the PostHog read path isn't configured or the query fails, which the check reports as
    skipped. The baseline is call-weighted, not a mean of daily rates, so one low-volume day can't
    swing it.
    """
    from cqc_lem.utilities.observability import posthog_hogql_query

    day = day or _last_complete_day()
    start = day - timedelta(days=max(window_days, 1))
    rows = posthog_hogql_query(CACHE_HIT_SQL.format(start=start.isoformat(),
                                                    end=(day + timedelta(days=1)).isoformat()))
    if rows is None:
        return None
    key = day.isoformat()
    hits = calls = baseline_hits = baseline_calls = 0
    for row in rows:
        if not row or len(row) < 3:
            continue
        row_day = _day_key(row[0])[:10]
        row_hits, row_calls = int(row[1] or 0), int(row[2] or 0)
        if row_day == key:
            hits, calls = row_hits, row_calls
        elif row_day < key:
            baseline_hits += row_hits
            baseline_calls += row_calls
    return {
        "date": key,
        "rate": (hits / calls) if calls else None,
        "calls": calls,
        "baseline_rate": (baseline_hits / baseline_calls) if baseline_calls else None,
        "baseline_calls": baseline_calls,
    }


def collect_cost_alert_report(day: Optional[date] = None, days: int = ANOMALY_WINDOW_DAYS,
                              thresholds: Optional[Mapping] = None) -> dict:
    """Read the ledger + PostHog for the last complete day and run every §E.2 check.

    The margin report is reused wholesale rather than re-deriving spend-vs-MRR here — it already
    computes the §C.1 per-user and system figures the ceiling, margin-floor and unattributed checks
    need, from the same `cost_ledger`.
    """
    from cqc_lem.utilities.db import get_cost_rollup, get_daily_cost_totals
    from cqc_lem.utilities.margin import collect_margin_report

    day = day or _last_complete_day()
    window = max(days, 1)
    start = day - timedelta(days=window - 1)
    return build_cost_alert_report(
        day,
        collect_margin_report(end=day, days=window),
        cost_by_feature=get_cost_rollup(start, day, group_by="feature"),
        daily_spend=get_daily_cost_totals(day - timedelta(days=ANOMALY_WINDOW_DAYS), day),
        cache=collect_cache_hit_stats(day, window_days=ANOMALY_WINDOW_DAYS),
        thresholds=thresholds,
    )


def send_cost_alerts(report: Optional[Mapping] = None,
                     to_email: Optional[str] = None) -> dict:
    """Deliver breaches over the existing notification path: an owner email (only when something
    actually fired — a daily "all clear" would train the owner to ignore it), one PostHog
    `cost_alert` event per alert, and a structured log line per alert (`log_error` for critical,
    which the logger forwards to PostHog on its own). Delivery failures are logged, never raised, so
    a beat run isn't lost over an email hiccup.

    The per-alert line is deliberately kept out of recurrence escalation (`ALERT_LOG_PREFIX`): a
    breach is what this function was asked to REPORT, and it fires again every day the spend stays
    over the ceiling — filing it as a recurring code defect describes nothing anyone can fix here.
    A failure to DELIVER an alert is a different thing and still warns normally.
    """
    from cqc_lem.utilities.email import _dispatch_email
    from cqc_lem.utilities.observability import track_cost_alert

    report = report if report is not None else collect_cost_alert_report()
    alerts = list(report.get("alerts") or [])
    day = report.get("date")

    for alert in alerts:
        message = f"{ALERT_LOG_PREFIX}{alert.get('check')}]: {alert.get('title')}"
        context = {"user_id": alert.get("user_id"), "task_name": "auto_daily_cost_alerts"}
        if alert.get("severity") == SEVERITY_CRITICAL:
            log_error(message, **context)
        else:
            log_warning(message, **context)

    tracked = False
    try:
        for alert in alerts:
            track_cost_alert(alert, day=day)
        tracked = True
    except Exception as e:
        log_warning("Cost alert PostHog capture failed", exc=e, task_name="auto_daily_cost_alerts")

    recipient = to_email or COST_ALERT_EMAIL or MARGIN_REPORT_EMAIL
    emailed = False
    if alerts and recipient:
        try:
            severity = "CRITICAL" if report.get("critical_count") else "WARNING"
            emailed = bool(_dispatch_email(
                recipient, f"LEM cost alert ({severity}) — {len(alerts)} threshold(s) breached {day}",
                render_cost_alerts_html(report), text_content=render_cost_alerts_text(report),
                high_priority=bool(report.get("critical_count"))))
        except Exception as e:
            log_warning("Cost alert email failed", exc=e, task_name="auto_daily_cost_alerts")
    elif alerts and not recipient:
        log_warning("COST_ALERT_EMAIL/MARGIN_REPORT_EMAIL not set — cost alerts not emailed",
                    task_name="auto_daily_cost_alerts")

    log_info(f"Cost alerts for {day}: {len(alerts)} alert(s), "
             f"{report.get('critical_count', 0)} critical, "
             f"skipped {report.get('skipped_checks') or []} (emailed={emailed}, tracked={tracked})",
             task_name="auto_daily_cost_alerts")
    return {"emailed": emailed, "tracked": tracked, "alerts": len(alerts), "report": report}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LEM cost budget alerts / anomaly detection")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--email", action="store_true", help="deliver breaches (email + PostHog)")
    parser.add_argument("--day", help="day to evaluate (YYYY-MM-DD, default: yesterday UTC)")
    parser.add_argument("--days", type=int, default=ANOMALY_WINDOW_DAYS,
                        help=f"margin/rollup window in days (default {ANOMALY_WINDOW_DAYS})")
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.day) if args.day else None
    report = collect_cost_alert_report(day=day, days=args.days)
    if args.email:
        send_cost_alerts(report)
    print(json.dumps(report) if args.json else render_cost_alerts_text(report))
    # Exit 2 on a breach so a cron/CI caller can act on it without parsing the output.
    return 2 if report.get("alert_count") else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
