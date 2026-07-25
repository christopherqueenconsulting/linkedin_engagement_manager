#!/usr/bin/env python3
"""Provision the cost/margin PostHog dashboards (docs/cost-performance-margin-plan.md §E.1).

The four dashboards — Cost Explorer, Margin by Cohort, Engagement Lift and the Unit-Economics
Scorecard — are defined here as code so they are reviewable, reproducible and re-creatable if a
tile is edited or deleted in the PostHog UI. Every tile is a HogQL query over the events LEM
already emits (`llm_call`, `media_cost`, `post_outcome`, `margin_report`), so a tile resolves
even before the event it reads starts flowing — it just renders empty until then.

Split the same way as scripts/model_health_check.py: PURE spec/plan logic (unit-tested) and a thin
I/O layer (the PostHog REST client, mocked in tests).

CLI (--dry-run and --apply are mutually exclusive):
  --dry-run          Show what would be created/updated against the live project. No writes. (default)
  --apply            Create missing dashboards/insights and update drifted ones.
  --print-sql        Print every tile's HogQL (no network).
Env:
  POSTHOG_PERSONAL_API_KEY   Personal API key with insight:write + dashboard:write (required for network).
  POSTHOG_PROJECT_ID         PostHog project id (default 475262 — "CQC LEM").
  POSTHOG_APP_HOST           App host for the API (default https://us.posthog.com).
Exit: 0 in sync / applied, 2 changes pending (--dry-run), 1 error.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"

DASH_COST = "LEM Cost Explorer"
DASH_MARGIN = "LEM Margin by Cohort"
DASH_LIFT = "LEM Engagement Lift"
DASH_SCORECARD = "LEM Unit-Economics Scorecard"

TAGS = ["cost", "margin", "unit-economics"]

# Rolling windows every tile shares, so "30d" means the same thing across dashboards. Margin and
# lift read weekly/low-frequency events, hence the longer window.
SPEND_WINDOW = "INTERVAL 30 DAY"
REPORT_WINDOW = "INTERVAL 180 DAY"
LIFT_WINDOW = "INTERVAL 365 DAY"

# Shared by both lift tiles. `outcomes` is filtered FIRST so the baseline join never scans the whole
# events table (an unfiltered `JOIN events` here runs the query out of memory); `baseline` is each
# user's average engagement rate on their first day with a tracked post — the day-0 reference.
BASELINE_CTES = f"""
WITH outcomes AS (
    SELECT distinct_id, timestamp, toFloat(properties.engagement_rate) AS rate
    FROM events
    WHERE event = 'post_outcome' AND properties.engagement_rate IS NOT NULL
          AND timestamp >= now() - {LIFT_WINDOW}
),
first_day AS (
    SELECT distinct_id,
           formatDateTime(toStartOfMonth(min(timestamp)), '%Y-%m') AS cohort,
           toStartOfDay(min(timestamp)) AS day0
    FROM outcomes
    GROUP BY distinct_id
),
baseline AS (
    SELECT f.distinct_id AS distinct_id,
           any(f.cohort) AS cohort,
           avg(o.rate) AS baseline_rate
    FROM first_day AS f
    INNER JOIN outcomes AS o ON o.distinct_id = f.distinct_id
    WHERE toStartOfDay(o.timestamp) = f.day0
    GROUP BY f.distinct_id
)""".strip()

# Events the tiles read. `media_cost` and `margin_report` are emitted by later rollout steps
# (plan §F.2/§F.3) — their tiles resolve empty until those land, which is the intended behaviour.
SOURCE_EVENTS = ("llm_call", "media_cost", "post_outcome", "margin_report")

# Plan §C.3 target blended gross margin, drawn as a goal line on the margin-% tile.
TARGET_GROSS_MARGIN_PCT = 70

# Spend of a single event, whichever kind it is: LLM calls carry `cost_usd`, media renders `usd`.
_SPEND = ("coalesce(sumIf(toFloat(properties.cost_usd), event = 'llm_call'), 0)"
          " + coalesce(sumIf(toFloat(properties.usd), event = 'media_cost'), 0)")
# An LLM call is unattributed when it cannot be billed to a user or a feature — the §E.2
# instrumentation-regression signal.
_UNATTRIBUTED = ("properties.user_id IS NULL"
                 " OR coalesce(nullIf(toString(properties.feature), ''), 'system') = 'system'")


def _hogql(sql: str, display: str, chart_settings: Optional[dict] = None) -> dict:
    """A saved-insight query node wrapping raw HogQL. `display` picks the visualization; PostHog
    falls back to a table when the chart settings don't apply."""
    node: dict = {"kind": "DataVisualizationNode", "display": display,
                  "source": {"kind": "HogQLQuery", "query": sql.strip()}}
    if chart_settings:
        node["chartSettings"] = chart_settings
    return node


def _line(x: str, ys: list, goal_lines: Optional[list] = None) -> dict:
    settings: dict = {"xAxis": {"column": x}, "yAxis": [{"column": y} for y in ys],
                      "showLegend": True, "showNullsAsZero": True}
    if goal_lines:
        settings["goalLines"] = goal_lines
    return settings


def _tile(name: str, description: str, query: dict) -> dict:
    return {"name": name, "description": description, "query": query}


def cost_explorer_tiles() -> list:
    """Plan §E.1.1 — spend by user / feature / model tier / provider / day, plus cache-hit rate."""
    return [
        _tile(
            "Daily spend — LLM vs media (USD)",
            "Total attributed spend per day, split into LLM inference and media renders.",
            _hogql(f"""
SELECT toStartOfDay(timestamp) AS day,
       round(coalesce(sumIf(toFloat(properties.cost_usd), event = 'llm_call'), 0), 6) AS llm_usd,
       round(coalesce(sumIf(toFloat(properties.usd), event = 'media_cost'), 0), 6) AS media_usd,
       round({_SPEND}, 6) AS total_usd
FROM events
WHERE event IN ('llm_call', 'media_cost') AND timestamp >= now() - {SPEND_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsLineGraph", _line("day", ["llm_usd", "media_usd", "total_usd"])),
        ),
        _tile(
            "Spend by feature (30d)",
            "Where the money goes by feature bucket (content/comment/dm/newsletter/research/system).",
            _hogql(f"""
SELECT coalesce(nullIf(toString(properties.feature), ''), 'unattributed') AS feature,
       round({_SPEND}, 6) AS spend_usd,
       count() AS events
FROM events
WHERE event IN ('llm_call', 'media_cost') AND timestamp >= now() - {SPEND_WINDOW}
GROUP BY feature
ORDER BY spend_usd DESC
""", "ActionsBar", _line("feature", ["spend_usd"])),
        ),
        _tile(
            "Spend by user (30d)",
            "Per-user attributed spend — the input to contribution margin. 'system' = unattributed.",
            _hogql(f"""
SELECT coalesce(toString(properties.user_id), 'system') AS user_id,
       count() AS events,
       coalesce(sum(toFloat(properties.total_tokens)), 0) AS tokens,
       round(coalesce(sumIf(toFloat(properties.cost_usd), event = 'llm_call'), 0), 6) AS llm_usd,
       round(coalesce(sumIf(toFloat(properties.usd), event = 'media_cost'), 0), 6) AS media_usd,
       round({_SPEND}, 6) AS total_usd
FROM events
WHERE event IN ('llm_call', 'media_cost') AND timestamp >= now() - {SPEND_WINDOW}
GROUP BY user_id
ORDER BY total_usd DESC
LIMIT 100
""", "ActionsTable"),
        ),
        _tile(
            "Spend by model tier (30d)",
            "Cost and $/1K tokens per lem-* tier and underlying model — the down-routing lever (§D.1).",
            _hogql(f"""
SELECT coalesce(nullIf(toString(properties.model_tier), ''), 'raw-model') AS model_tier,
       coalesce(nullIf(toString(properties.model), ''), 'unknown') AS model,
       count() AS calls,
       coalesce(sum(toFloat(properties.total_tokens)), 0) AS tokens,
       round(coalesce(sum(toFloat(properties.cost_usd)), 0), 6) AS spend_usd,
       round(1000 * coalesce(sum(toFloat(properties.cost_usd)), 0)
             / nullIf(coalesce(sum(toFloat(properties.total_tokens)), 0), 0), 6) AS usd_per_1k_tokens
FROM events
WHERE event = 'llm_call' AND timestamp >= now() - {SPEND_WINDOW}
GROUP BY model_tier, model
ORDER BY spend_usd DESC
""", "ActionsTable"),
        ),
        _tile(
            "Spend by provider (30d)",
            "Provider mix. Media rows carry `provider`; LLM rows fall back to the model/tier they routed through.",
            _hogql(f"""
SELECT coalesce(nullIf(toString(properties.provider), ''),
                nullIf(toString(properties.model), ''),
                nullIf(toString(properties.model_tier), ''), 'unknown') AS provider,
       count() AS events,
       round({_SPEND}, 6) AS spend_usd
FROM events
WHERE event IN ('llm_call', 'media_cost') AND timestamp >= now() - {SPEND_WINDOW}
GROUP BY provider
ORDER BY spend_usd DESC
""", "ActionsTable"),
        ),
        _tile(
            "LLM cache-hit rate (daily %)",
            "Share of LLM calls served from the LiteLLM Redis cache. A collapse here precedes a cost spike (§E.2).",
            _hogql(f"""
SELECT toStartOfDay(timestamp) AS day,
       count() AS calls,
       round(100 * countIf(toBool(properties.cached)) / nullIf(count(), 0), 2) AS cache_hit_pct
FROM events
WHERE event = 'llm_call' AND timestamp >= now() - {SPEND_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsLineGraph", _line("day", ["cache_hit_pct"])),
        ),
        _tile(
            "Unattributed spend share (30d)",
            "% of LLM spend that lands on no user or no feature — an instrumentation regression, not a cost signal (§E.2).",
            _hogql(f"""
SELECT round(100 * coalesce(sumIf(toFloat(properties.cost_usd), {_UNATTRIBUTED}), 0)
             / nullIf(coalesce(sum(toFloat(properties.cost_usd)), 0), 0), 2) AS unattributed_spend_pct
FROM events
WHERE event = 'llm_call' AND timestamp >= now() - {SPEND_WINDOW}
""", "BoldNumber"),
        ),
    ]


def margin_by_cohort_tiles() -> list:
    """Plan §E.1.2 — cohort contribution margin and the system gross-margin trend, from the weekly
    `margin_report` event (utilities/margin.py). One report per run, so a daily max() reads it."""
    return [
        _tile(
            "System gross margin $ vs MRR",
            "Weekly system gross margin against MRR, both on the monthly run-rate basis the report uses.",
            _hogql(f"""
SELECT toStartOfDay(timestamp) AS day,
       round(max(toFloat(properties.system_mrr_usd)), 2) AS mrr_usd,
       round(max(toFloat(properties.system_gross_margin_usd)), 2) AS gross_margin_usd
FROM events
WHERE event = 'margin_report' AND timestamp >= now() - {REPORT_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsLineGraph", _line("day", ["mrr_usd", "gross_margin_usd"])),
        ),
        _tile(
            "Gross margin % vs target",
            f"System gross margin % against the §C.3 target of {TARGET_GROSS_MARGIN_PCT}%.",
            _hogql(f"""
SELECT toStartOfDay(timestamp) AS day,
       round(100 * max(toFloat(properties.system_gross_margin_pct)), 2) AS gross_margin_pct
FROM events
WHERE event = 'margin_report' AND timestamp >= now() - {REPORT_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsLineGraph", _line(
                "day", ["gross_margin_pct"],
                [{"label": f"Target {TARGET_GROSS_MARGIN_PCT}%", "value": TARGET_GROSS_MARGIN_PCT}])),
        ),
        _tile(
            "Cost stack — variable / semi-variable / fixed",
            "The three §C.1 cost layers over time; fixed infra is what amortizes away as users grow.",
            _hogql(f"""
SELECT toStartOfDay(timestamp) AS day,
       round(max(toFloat(properties.system_variable_cost_usd)), 4) AS variable_usd,
       round(max(toFloat(properties.system_semi_variable_cost_usd)), 4) AS semi_variable_usd,
       round(max(toFloat(properties.system_infra_fixed_usd)), 2) AS infra_fixed_usd
FROM events
WHERE event = 'margin_report' AND timestamp >= now() - {REPORT_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsAreaGraph", _line("day", ["variable_usd", "semi_variable_usd", "infra_fixed_usd"])),
        ),
        _tile(
            "Cohort margin & engagement (latest report)",
            "Signup-month cohorts from the most recent margin report: users, avg CM $/%, engagement rate and lift.",
            _hogql("""
SELECT JSONExtractString(cohort_json, 'cohort') AS cohort,
       JSONExtractInt(cohort_json, 'users') AS users,
       JSONExtractFloat(cohort_json, 'avg_cm_usd') AS avg_cm_usd,
       round(100 * JSONExtractFloat(cohort_json, 'avg_cm_pct'), 2) AS avg_cm_pct,
       JSONExtractFloat(cohort_json, 'median_engagement_rate') AS median_engagement_rate,
       JSONExtractFloat(cohort_json, 'avg_engagement_lift_pct') AS avg_engagement_lift_pct
FROM (
    SELECT arrayJoin(JSONExtractArrayRaw(latest.cohorts_json)) AS cohort_json
    FROM (
        SELECT coalesce(toString(properties.cohorts), '[]') AS cohorts_json
        FROM events
        WHERE event = 'margin_report'
        ORDER BY timestamp DESC
        LIMIT 1
    ) AS latest
)
ORDER BY cohort ASC
""", "ActionsTable"),
        ),
        _tile(
            "Contribution margin per paying user",
            "Average per-user CM alongside active/paying counts — margin should rise as the fixed base amortizes.",
            _hogql(f"""
SELECT toStartOfDay(timestamp) AS day,
       round(max(toFloat(properties.system_avg_contribution_margin_usd)), 2) AS avg_cm_usd,
       max(toInt(properties.system_active_users)) AS active_users,
       max(toInt(properties.system_paying_users)) AS paying_users
FROM events
WHERE event = 'margin_report' AND timestamp >= now() - {REPORT_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsLineGraph", _line("day", ["avg_cm_usd", "active_users", "paying_users"])),
        ),
        _tile(
            "Unattributed cost (system − per-user)",
            "System spend the report could not pin to a user. Rising = attribution regression (§E.2).",
            _hogql(f"""
SELECT toStartOfDay(timestamp) AS day,
       round(max(toFloat(properties.system_unattributed_cost_usd)), 4) AS unattributed_cost_usd
FROM events
WHERE event = 'margin_report' AND timestamp >= now() - {REPORT_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsLineGraph", _line("day", ["unattributed_cost_usd"])),
        ),
    ]


def engagement_lift_tiles() -> list:
    """Plan §E.1.3 — cohort engagement_rate over time against each user's day-0 baseline. The
    baseline is that user's first day of `post_outcome` events; the cohort is that day's month."""
    return [
        _tile(
            "Cohort engagement lift vs day-0 baseline",
            "Weekly engagement rate per signup-month cohort against that cohort's day-0 baseline. The headline 'is LEM working better over time' number.",
            _hogql(f"""
{BASELINE_CTES}
SELECT base.cohort AS cohort,
       toStartOfWeek(outcome.timestamp) AS week,
       count() AS posts,
       round(avg(outcome.rate), 6) AS engagement_rate,
       round(avg(base.baseline_rate), 6) AS day0_baseline,
       round(100 * (avg(outcome.rate) - avg(base.baseline_rate))
             / nullIf(avg(base.baseline_rate), 0), 2) AS lift_pct
FROM outcomes AS outcome
INNER JOIN baseline AS base ON outcome.distinct_id = base.distinct_id
GROUP BY cohort, week
ORDER BY cohort ASC, week ASC
""", "ActionsTable"),
        ),
        _tile(
            "Median engagement rate by week",
            "System-wide median engagement rate — the quality guardrail every cost cut must hold (§D.3).",
            _hogql("""
SELECT toStartOfWeek(timestamp) AS week,
       count() AS posts,
       round(median(toFloat(properties.engagement_rate)), 6) AS median_engagement_rate,
       round(avg(toFloat(properties.engagement_rate)), 6) AS avg_engagement_rate
FROM events
WHERE event = 'post_outcome' AND properties.engagement_rate IS NOT NULL
GROUP BY week
ORDER BY week ASC
""", "ActionsLineGraph", _line("week", ["median_engagement_rate", "avg_engagement_rate"])),
        ),
        _tile(
            "Engagement earned per week",
            "Raw reach and engagement behind the rate: impressions, reactions, comments, reposts, saves.",
            _hogql("""
SELECT toStartOfWeek(timestamp) AS week,
       coalesce(sum(toFloat(properties.impressions)), 0) AS impressions,
       coalesce(sum(toFloat(properties.reactions)), 0) AS reactions,
       coalesce(sum(toFloat(properties.comments)), 0) AS comments,
       coalesce(sum(toFloat(properties.reposts)), 0) AS reposts,
       coalesce(sum(toFloat(properties.saves)), 0) AS saves
FROM events
WHERE event = 'post_outcome'
GROUP BY week
ORDER BY week ASC
""", "ActionsLineGraph", _line("week", ["impressions", "reactions", "comments", "reposts", "saves"])),
        ),
        _tile(
            "Per-user lift — last 30d vs day-0",
            "Each user's recent engagement rate against their own day-0 baseline, so a new cohort can be read against an older one.",
            _hogql(f"""
{BASELINE_CTES}
SELECT base.cohort AS cohort,
       outcome.distinct_id AS user_id,
       count() AS posts_30d,
       round(avg(outcome.rate), 6) AS engagement_rate_30d,
       round(avg(base.baseline_rate), 6) AS day0_baseline,
       round(100 * (avg(outcome.rate) - avg(base.baseline_rate))
             / nullIf(avg(base.baseline_rate), 0), 2) AS lift_pct
FROM outcomes AS outcome
INNER JOIN baseline AS base ON outcome.distinct_id = base.distinct_id
WHERE outcome.timestamp >= now() - {SPEND_WINDOW}
GROUP BY cohort, user_id
ORDER BY lift_pct DESC
LIMIT 100
""", "ActionsTable"),
        ),
    ]


def scorecard_tiles() -> list:
    """Plan §E.1.4 / §E.3 — the KPI tree as single-number tiles, north-star first."""
    def latest(prop: str, expr: str, name: str) -> str:
        return f"""
SELECT {expr} AS {name}
FROM events
WHERE event = 'margin_report' AND properties.{prop} IS NOT NULL
ORDER BY timestamp DESC
LIMIT 1
"""

    return [
        _tile(
            "★ System gross margin $ (latest)",
            "North star (§E.3): system gross margin in $ on a monthly run-rate basis, from the latest weekly report.",
            _hogql(latest("system_gross_margin_usd",
                          "round(toFloat(properties.system_gross_margin_usd), 2)",
                          "gross_margin_usd"), "BoldNumber"),
        ),
        _tile(
            "Gross margin % (latest)",
            f"Blended system gross margin %. Target ≥ {TARGET_GROSS_MARGIN_PCT}% at 100+ active users (§C.3).",
            _hogql(latest("system_gross_margin_pct",
                          "round(100 * toFloat(properties.system_gross_margin_pct), 2)",
                          "gross_margin_pct"), "BoldNumber"),
        ),
        _tile(
            "MRR (latest)",
            "Revenue side of the KPI tree: Σ MRR across active paying users.",
            _hogql(latest("system_mrr_usd", "round(toFloat(properties.system_mrr_usd), 2)",
                          "mrr_usd"), "BoldNumber"),
        ),
        _tile(
            "Variable cost per active user (latest)",
            "Cost side of the KPI tree: system variable spend divided by active users, monthly run-rate.",
            _hogql(latest("system_variable_cost_usd",
                          "round(toFloat(properties.system_variable_cost_usd)"
                          " / nullIf(toFloat(properties.system_active_users), 0), 4)",
                          "variable_cost_per_user_usd"), "BoldNumber"),
        ),
        _tile(
            "LTV:CAC (latest)",
            "Lifetime value over CAC. Target ≥ 3:1 (§C.1); CAC comes from the marketing plan.",
            _hogql(latest("ltv_cac_ratio", "round(toFloat(properties.ltv_cac_ratio), 2)",
                          "ltv_cac_ratio"), "BoldNumber"),
        ),
        _tile(
            "CAC payback (months, latest)",
            "Months of contribution margin needed to repay CAC.",
            _hogql(latest("payback_months", "round(toFloat(properties.payback_months), 2)",
                          "payback_months"), "BoldNumber"),
        ),
        _tile(
            "Attributed spend, 30d (USD)",
            "Actual metered spend over the last 30 days — the ground truth the report's run-rate is derived from.",
            _hogql(f"""
SELECT round({_SPEND}, 4) AS spend_usd_30d
FROM events
WHERE event IN ('llm_call', 'media_cost') AND timestamp >= now() - {SPEND_WINDOW}
""", "BoldNumber"),
        ),
        _tile(
            "LLM cache-hit % (30d)",
            "Cost driver in the KPI tree — cache hits cost nothing, so this moves variable cost per action directly.",
            _hogql(f"""
SELECT round(100 * countIf(toBool(properties.cached)) / nullIf(count(), 0), 2) AS cache_hit_pct_30d
FROM events
WHERE event = 'llm_call' AND timestamp >= now() - {SPEND_WINDOW}
""", "BoldNumber"),
        ),
        _tile(
            "Quality guardrail — median engagement rate (30d)",
            "The guardrail that must hold for any cost cut to ship (§D.3).",
            _hogql(f"""
SELECT round(median(toFloat(properties.engagement_rate)), 6) AS median_engagement_rate_30d
FROM events
WHERE event = 'post_outcome' AND properties.engagement_rate IS NOT NULL
      AND timestamp >= now() - {SPEND_WINDOW}
""", "BoldNumber"),
        ),
    ]


def build_specs() -> list:
    """The full §E.1 dashboard set. Pure — the same input every run, so a diff against PostHog is
    meaningful and `--apply` is idempotent."""
    return [
        {"name": DASH_COST,
         "description": "Plan §E.1.1 — attributed LLM + media spend by user, feature, model tier, "
                        "provider and day, plus cache-hit rate and unattributed-spend share.",
         "tiles": cost_explorer_tiles()},
        {"name": DASH_MARGIN,
         "description": "Plan §E.1.2 — contribution margin and CM% by signup-month cohort, and the "
                        "system gross-margin trend from the weekly margin report.",
         "tiles": margin_by_cohort_tiles()},
        {"name": DASH_LIFT,
         "description": "Plan §E.1.3 — cohort engagement rate over time against each user's day-0 "
                        "baseline, from post_outcome.",
         "tiles": engagement_lift_tiles()},
        {"name": DASH_SCORECARD,
         "description": "Plan §E.1.4/§E.3 — north-star System Gross Margin $ plus the KPI-tree "
                        "tiles: revenue, cost per user, LTV:CAC, payback and the quality guardrail.",
         "tiles": scorecard_tiles()},
    ]


# ─────────────────────────── pure planning (unit-tested) ────────────────────────────

def plan_actions(specs: list, existing_dashboards: dict, existing_insights: dict) -> list:
    """Diff the spec against what PostHog already has.

    `existing_dashboards` maps dashboard name -> id; `existing_insights` maps insight name ->
    {"id", "query", "description", "dashboards"}. Returns ordered actions:
    `create_dashboard`, `create_insight`, `update_insight`, `unchanged`. A dashboard that doesn't
    exist yet has id None — the caller fills it in after creating it, which is why creates come first.
    `description` is optional: when a caller omits it the spec value is assumed, so only a caller
    that actually reports the stored description can trigger a description-drift update.
    """
    actions: list = []
    for spec in specs:
        dashboard_id = existing_dashboards.get(spec["name"])
        if dashboard_id is None:
            actions.append({"action": "create_dashboard", "dashboard": spec["name"],
                            "description": spec["description"]})
        for tile in spec["tiles"]:
            found = existing_insights.get(tile["name"])
            if found is None:
                actions.append({"action": "create_insight", "dashboard": spec["name"],
                                "insight": tile["name"], "tile": tile})
                continue
            attached = dashboard_id is not None and dashboard_id in (found.get("dashboards") or [])
            described = found.get("description", tile["description"]) == tile["description"]
            if found.get("query") == tile["query"] and attached and described:
                actions.append({"action": "unchanged", "dashboard": spec["name"],
                                "insight": tile["name"], "insight_id": found["id"]})
            else:
                actions.append({"action": "update_insight", "dashboard": spec["name"],
                                "insight": tile["name"], "insight_id": found["id"], "tile": tile})
    return actions


def pending(actions: list) -> list:
    return [a for a in actions if a["action"] != "unchanged"]


def summarize(actions: list) -> str:
    counts: dict = {}
    for action in actions:
        counts[action["action"]] = counts.get(action["action"], 0) + 1
    return ", ".join(f"{name}={counts[name]}" for name in sorted(counts)) or "nothing to do"


def dashboard_url(app_host: str, project_id: str, dashboard_id) -> str:
    return f"{app_host.rstrip('/')}/project/{project_id}/dashboard/{dashboard_id}"


# ──────────────────────────────── I/O (mocked in tests) ─────────────────────────────

class PostHogClient:
    """Thin PostHog REST wrapper — only the four calls this provisioner needs."""

    def __init__(self, api_key: str, project_id: str, app_host: str = DEFAULT_APP_HOST) -> None:
        self.project_id = project_id
        self.app_host = app_host.rstrip("/")
        self._base = f"{self.app_host}/api/projects/{project_id}"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        import requests
        response = requests.request(method, f"{self._base}{path}", headers=self._headers,
                                    timeout=30, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def _paged(self, path: str) -> list:
        import requests
        results, url = [], f"{self._base}{path}"
        while url:
            response = requests.get(url, headers=self._headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            results.extend(payload.get("results", []))
            url = payload.get("next")
        return results

    def list_dashboards(self) -> dict:
        return {d["name"]: d["id"] for d in self._paged("/dashboards/?limit=100") if not d.get("deleted")}

    def list_insights(self) -> dict:
        insights = {}
        for item in self._paged("/insights/?limit=100"):
            if item.get("deleted"):
                continue
            insights[item.get("name")] = {
                "id": item["id"],
                "query": item.get("query"),
                "description": item.get("description") or "",
                "dashboards": [t.get("dashboard_id") for t in (item.get("dashboard_tiles") or [])
                               ] or list(item.get("dashboards") or []),
            }
        return insights

    def create_dashboard(self, name: str, description: str) -> int:
        return self._request("POST", "/dashboards/", json={
            "name": name, "description": description, "tags": TAGS, "pinned": True})["id"]

    def create_insight(self, tile: dict, dashboard_id: int) -> int:
        return self._request("POST", "/insights/", json={
            "name": tile["name"], "description": tile["description"], "query": tile["query"],
            "tags": TAGS, "dashboards": [dashboard_id]})["id"]

    def update_insight(self, insight_id: int, tile: dict, dashboard_id: int) -> None:
        self._request("PATCH", f"/insights/{insight_id}/", json={
            "description": tile["description"], "query": tile["query"],
            "dashboards": [dashboard_id]})


def apply_actions(client: PostHogClient, actions: list, dry_run: bool = True) -> list:
    """Execute the plan. `dry_run` reports without writing. Returns the log lines emitted."""
    dashboard_ids: dict = {}
    log: list = []
    for action in actions:
        name, kind = action["dashboard"], action["action"]
        if kind == "create_dashboard":
            if dry_run:
                log.append(f"[dry-run] create dashboard '{name}'")
                continue
            dashboard_ids[name] = client.create_dashboard(name, action["description"])
            log.append(f"created dashboard '{name}' -> {dashboard_ids[name]}")
        elif kind in ("create_insight", "update_insight"):
            verb = "create" if kind == "create_insight" else "update"
            if dry_run:
                log.append(f"[dry-run] {verb} insight '{action['insight']}' on '{name}'")
                continue
            dashboard_id = dashboard_ids.get(name)
            if dashboard_id is None:
                # The dashboard already existed, so no create_dashboard action filled the cache.
                dashboard_id = client.list_dashboards().get(name)
                dashboard_ids[name] = dashboard_id
            if kind == "create_insight":
                insight_id = client.create_insight(action["tile"], dashboard_id)
                log.append(f"created insight '{action['insight']}' -> {insight_id}")
            else:
                client.update_insight(action["insight_id"], action["tile"], dashboard_id)
                log.append(f"updated insight '{action['insight']}'")
    return log


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the LEM cost/margin PostHog dashboards.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes to PostHog.")
    mode.add_argument("--dry-run", action="store_true", help="Report changes only (default).")
    parser.add_argument("--print-sql", action="store_true", help="Print every tile's HogQL and exit.")
    args = parser.parse_args(argv)

    specs = build_specs()
    if args.print_sql:
        for spec in specs:
            for tile in spec["tiles"]:
                print(f"\n-- {spec['name']} :: {tile['name']}\n{tile['query']['source']['query']}")
        return 0

    api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "")
    if not api_key:
        print("POSTHOG_PERSONAL_API_KEY is not set — cannot reach PostHog.", file=sys.stderr)
        return 1
    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)

    client = PostHogClient(api_key, project_id, app_host)
    try:
        dashboards, insights = client.list_dashboards(), client.list_insights()
    except Exception as exc:
        print(f"Failed to read PostHog state: {exc}", file=sys.stderr)
        return 1

    actions = plan_actions(specs, dashboards, insights)
    dry_run = args.dry_run or not args.apply
    for line in apply_actions(client, actions, dry_run=dry_run):
        print(line)
    print(summarize(actions))

    for spec in specs:
        dashboard_id = client.list_dashboards().get(spec["name"]) if args.apply else dashboards.get(spec["name"])
        if dashboard_id:
            print(f"{spec['name']}: {dashboard_url(app_host, project_id, dashboard_id)}")

    if dry_run and pending(actions):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
