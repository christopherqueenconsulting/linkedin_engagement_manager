#!/usr/bin/env python3
"""Provision the LEM business-KPI PostHog surface — funnels, the two consolidated dashboards, the
threshold alerts and the weekly report subscription (issue #650).

41 insights and 6 dashboards existed before this, but nothing that showed LEM's actual business
LOOPS end to end, and every metric watchdog was a hand-run cron. This script defines all four
things as code so they are reviewable, reproducible and re-creatable if a tile is edited away in
the UI:

  • **LEM Health** — the operational dashboard: task failures, LinkedIn 429 breaker trips, LLM
    spend per day (from PH2's `$ai_generation`, the post-fallback truth), comments/day, posts/day
    and follower delta.
  • **LEM Growth** — the funnel dashboard: the content loop (plan → approve → posted → engaged),
    the onboarding/monetization funnel, the engagement loop (comment → author reply → our reply)
    and the engagement-rate trends.
  • **Threshold alerts** (4 of PostHog's 5 free ones) — comments below a weekly floor, LLM daily
    spend over cap, Celery failure spike, 429 spike. They email the key's owner.
  • **LEM Channels** (issue #658) — marketing attribution: signups and activations by UTM
    source/campaign and first-touch channel, the brand-post → visit → signup funnel, referral
    arrivals and tagged landing visits. Plus the two web-analytics **conversion goals** (signup,
    activation), which PostHog reads from actions — so they are defined here, not clicked together.
  • **A weekly email subscription** of LEM Growth — this is what replaces the hand-run 2-week
    perf-report cron.

Alert insights are NATIVE single-series trends queries on purpose: an alert threshold is evaluated
against one series by index, so anything with a breakdown or a second series makes `series_index`
ambiguous. The rest are HogQL, like scripts/posthog_dashboards.py, and insight names are globally
unique across BOTH scripts — each plans by name, so a shared name would make the two fight over one
insight forever.

Split the same way as scripts/posthog_dashboards.py: PURE spec/plan logic (unit-tested) and a thin
I/O layer (the PostHog REST client, mocked in tests).

Also provisions the three **Endpoints** (issue #654) behind the in-SPA "your stats" panel — HogQL
queries published as versioned, cached HTTP endpoints, each scoped to ONE user via a
`{variables.distinct_id}` placeholder (PostHog is one project shared by every LEM account, so an
un-scoped query would leak cross-tenant data into a customer's own dashboard). The placeholder
resolves against a single provisioned `InsightVariable` — `endpoints` are blocked, not created,
until it exists, the same "missing prerequisite" shape `plan_alerts` already uses for a missing
insight. `utilities/posthog_endpoints.py` is the runtime half: it calls `/run` with the caller's own
`distinct_id`, server-side only, so no PostHog key ever reaches the browser.

CLI (--dry-run and --apply are mutually exclusive):
  --dry-run             Show what would be created/updated against the live project. No writes. (default)
  --apply               Create missing dashboards/insights/alerts/goals/subscription/endpoints and update drifted ones.
  --print-sql           Print every tile's and endpoint's query (no network).
  --simulate NAME=VALUE Report whether VALUE would breach the named alert's threshold. No network.
  --email ADDR          Weekly-report recipient (default $POSTHOG_REPORT_EMAIL, else the key owner).
Env:
  POSTHOG_OPERATOR_API_KEY  Personal API key (required for network), falling back to
                            POSTHOG_PERSONAL_API_KEY (posthog_keys.py owns the precedence). Scopes:
                            insight, dashboard, alert, action and subscription read+write, user:read
                            for the subscriber id, plus endpoint and insight_variable read+write for
                            the Endpoints panel.
  POSTHOG_PROJECT_ID        PostHog project id (default 475262 — "CQC LEM").
  POSTHOG_APP_HOST          App host for the API (default https://us.posthog.com).
  POSTHOG_REPORT_EMAIL      Weekly Growth-dashboard recipient. Falls back to the key owner's email.
Exit: 0 in sync / applied, 2 changes pending (--dry-run), 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

# Reached by path, not by installation: this is run by hand from a checkout, the same way
# posthog_key_check.py reaches the resolver. posthog_keys.py is stdlib-only, so this costs nothing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from cqc_lem.utilities.posthog_keys import missing_key_message, operator_api_key  # noqa: E402

DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"

DASH_HEALTH = "LEM Health"
DASH_GROWTH = "LEM Growth"
DASH_CHANNELS = "LEM Channels"

TAGS = ["lem", "kpi", "health", "growth"]

# ── Endpoints (issue #654) ───────────────────────────────────────────────────────────
ENDPOINT_TAGS = ["lem", "endpoints"]
# The one variable every per-user endpoint scopes its query on. PostHog derives `code_name` from
# `name` (lowercased, non-alnum -> "_"), so this already-lowercase name round-trips unchanged —
# `endpoint_query` still reads the code_name PostHog actually returns rather than assuming it.
DISTINCT_ID_VARIABLE_NAME = "distinct_id"
# A valid data_freshness_seconds bucket (900/1800/3600/21600/43200/86400/604800). A per-user stats
# panel doesn't need sub-hour freshness; 6h keeps materialization/cache churn low.
ENDPOINT_DATA_FRESHNESS_SECONDS = 21600

ENDPOINT_POSTS_ENGAGEMENT = "lem-posts-engagement-weekly"
ENDPOINT_COMMENT_ACTIVITY = "lem-comment-activity-weekly"
ENDPOINT_LLM_COST_BY_FEATURE = "lem-llm-cost-by-feature"

# Rolling windows, so "30d" means the same thing on every tile.
OPS_WINDOW = "INTERVAL 30 DAY"
GROWTH_WINDOW = "INTERVAL 90 DAY"
# Publishes are derived from each post's FIRST outcome reading, so the inner scan has to reach back
# further than the window it reports on — otherwise a post first measured before the window opens
# would be counted as published on day one of it.
PUBLISH_LOOKBACK = "INTERVAL 120 DAY"

# Every event these dashboards read. Server-side ones come from utilities/observability.py, the SPA
# ones from ui/src/utils/analytics.ts (PH1), and `$ai_generation` from the LiteLLM callback (PH2).
SOURCE_EVENTS = (
    "celery_task",
    "api_call",
    "rate_limit_trip",
    "sdui_selector_evidence",
    "comment_outcome",
    "post_outcome",
    "audience_snapshot",
    "onboarding_step",
    "content_plan_generated",
    "post_approved",
    "signup_started",
    "signup_completed",
    "trial_started",
    "onboarding_step_completed",
    "activated",
    "subscription_started",
    "signup_completed_web",
    "$pageview",
    "$ai_generation",
)

# ── Alert thresholds ─────────────────────────────────────────────────────────────────
# Changing one of these and re-running `--apply` updates the live alert; they are here rather than
# in env so the value that pages someone is reviewable in the diff.

# A whole WEEK under this many measured comments means feed commenting is broken, not resting.
# Deliberately weekly: human_pacing takes account-wide rest days (issue #626), so a daily floor
# would page on a healthy zero-comment day.
COMMENT_WEEKLY_FLOOR = 5
# Daily LLM spend ceiling, in USD, read from the proxy's own `response_cost` (docs/llm-analytics.md).
LLM_DAILY_COST_CAP_USD = 15.0
# A day's Celery failures. Individual failures are already `$exception` issues (#648); this is the
# "something systemic broke" line.
CELERY_FAILURES_PER_DAY_CEILING = 25
# 429 breaker trips per day. The breaker escalates its own cooldown, so more than a handful of trips
# in a day is the doom loop re-forming (see utilities/linkedin/rate_limit.py).
RATE_LIMIT_TRIPS_PER_DAY_CEILING = 5

# Weekly report cadence: Monday 09:00 UTC, matching the other weekly LEM crons.
REPORT_WEEKDAY = "monday"
REPORT_HOUR_UTC = 9
# PostHog caps a dashboard subscription's export at 6 insights and rejects the whole request past it
# ("Cannot select more than 6 insights"). LEM Growth carries 7 tiles, so the list is truncated.
SUBSCRIPTION_MAX_INSIGHTS = 6


# ── query-node builders ──────────────────────────────────────────────────────────────

def _hogql(sql: str, display: str, chart_settings: Optional[dict] = None) -> dict:
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


def _event_series(event: str, math: str = "total", math_property: Optional[str] = None,
                  properties: Optional[list] = None) -> dict:
    series: dict = {"kind": "EventsNode", "event": event, "name": event, "math": math}
    if math_property:
        series["math_property"] = math_property
    if properties:
        series["properties"] = properties
    return series


def _trends(series: dict, interval: str = "day", date_from: str = "-30d") -> dict:
    """A native trends insight. PostHog alerts read trends, funnel and SQL insights, but the
    threshold is evaluated against ONE series by index — so every alertable tile is a single-series
    trend with no breakdown, where `series_index: 0` can only mean one thing."""
    return {"kind": "InsightVizNode",
            "source": {"kind": "TrendsQuery", "series": [series], "interval": interval,
                       "dateRange": {"date_from": date_from},
                       "trendsFilter": {"display": "ActionsLineGraph"}}}


def _funnel(events: list, date_from: str = "-90d", window_days: int = 14) -> dict:
    """An ordered funnel over `events`. The conversion window has to be generous: a content plan is
    30 days long, so a plan → approve → post → engagement chain spans weeks, not a session.

    An entry may be a bare event name or `(event, properties)` — a step that has to be narrowed to
    one channel (the brand-post funnel's first pageview) cannot say so any other way."""
    series = []
    for entry in events:
        event, properties = entry if isinstance(entry, tuple) else (entry, None)
        step = {"kind": "EventsNode", "event": event, "name": event}
        if properties:
            step["properties"] = properties
        series.append(step)
    return {"kind": "InsightVizNode",
            "source": {"kind": "FunnelsQuery",
                       "series": series,
                       "dateRange": {"date_from": date_from},
                       "funnelsFilter": {"funnelVizType": "steps", "funnelOrderType": "ordered",
                                         "funnelWindowInterval": window_days,
                                         "funnelWindowIntervalUnit": "day"}}}


def _tile(name: str, description: str, query: dict) -> dict:
    return {"name": name, "description": description, "query": query}


# ── LEM Health ───────────────────────────────────────────────────────────────────────

# Alert-bearing insight names. Kept as constants because an alert references its insight BY NAME —
# a rename on one side and not the other would silently orphan the alert.
INSIGHT_CELERY_FAILURES = "Health — Celery task failures per day"
INSIGHT_RATE_LIMIT_TRIPS = "Health — LinkedIn 429 breaker trips per day"
INSIGHT_LLM_SPEND = "Health — LLM spend per day (USD, proxy-reported)"
INSIGHT_COMMENTS_WEEKLY = "Health — Comments measured per week"
INSIGHT_SDUI_SELECTOR_EVIDENCE = "Health — SDUI selector-evidence captures per day"


def health_tiles() -> list:
    """The operational dashboard: is LEM running, and what is it costing?"""
    return [
        _tile(
            INSIGHT_CELERY_FAILURES,
            "Celery tasks that ended in FAILURE, per day. Filters on `state` (a string) rather than "
            "`success` (a boolean) so the alert can never silently match nothing.",
            _trends(_event_series("celery_task", properties=[
                {"key": "state", "value": ["FAILURE"], "operator": "exact", "type": "event"}])),
        ),
        _tile(
            INSIGHT_RATE_LIMIT_TRIPS,
            "Times the LinkedIn 429 circuit breaker opened, per day. The breaker escalates its own "
            "cooldown, so a rising count is the doom loop re-forming — not just a slow day.",
            _trends(_event_series("rate_limit_trip")),
        ),
        _tile(
            INSIGHT_LLM_SPEND,
            "Daily LLM spend as the PROXY reports it ($ai_generation.$ai_total_cost_usd) — the "
            "post-fallback, post-down-route truth. Never add this to `llm_call` (docs/llm-analytics.md).",
            _trends(_event_series("$ai_generation", math="sum",
                                  math_property="$ai_total_cost_usd")),
        ),
        _tile(
            INSIGHT_COMMENTS_WEEKLY,
            "Comments the T+24h outcome sweep could measure, per week. Weekly on purpose: human "
            "pacing takes account-wide rest days, so a daily floor would page on a healthy zero day.",
            _trends(_event_series("comment_outcome"), interval="week", date_from="-90d"),
        ),
        _tile(
            INSIGHT_SDUI_SELECTOR_EVIDENCE,
            "Bounded DOM samples (track_selector_evidence, docs/observability-map.md) shipped when a "
            "Recent-sort or comment-sort locator resolves nothing on a page that rendered. Not "
            "alerted: a legitimately-absent sort control is a normal nonzero day (docs/sdui-probe-"
            "coverage.md) — the weekly drift sweep's own `drift` filing stays the actionable signal.",
            _trends(_event_series("sdui_selector_evidence")),
        ),
        _tile(
            "Health — Celery task failure rate (daily %)",
            "Share of finished Celery tasks that failed each day, with the volume behind it — a "
            "spike in the rate on falling volume is a different problem than one on rising volume.",
            _hogql(f"""
SELECT toStartOfDay(timestamp) AS day,
       count() AS tasks,
       countIf(toString(properties.state) = 'FAILURE') AS failures,
       round(100 * countIf(toString(properties.state) = 'FAILURE') / nullIf(count(), 0), 2) AS failure_pct
FROM events
WHERE event = 'celery_task' AND timestamp >= now() - {OPS_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsLineGraph", _line("day", ["failure_pct"])),
        ),
        _tile(
            "Health — Failing tasks by name (7d)",
            "Which task is actually failing. The first thing to look at when the failure-rate alert "
            "fires — one broken beat can carry the whole rate.",
            _hogql("""
SELECT coalesce(toString(properties.task), 'unknown') AS task,
       count() AS runs,
       countIf(toString(properties.state) = 'FAILURE') AS failures,
       round(100 * countIf(toString(properties.state) = 'FAILURE') / nullIf(count(), 0), 2) AS failure_pct,
       round(avg(toFloat(properties.duration_ms)), 0) AS avg_duration_ms
FROM events
WHERE event = 'celery_task' AND timestamp >= now() - INTERVAL 7 DAY
GROUP BY task
HAVING failures > 0
ORDER BY failures DESC
LIMIT 50
""", "ActionsTable"),
        ),
        _tile(
            "Health — Posts published per day",
            "Posts that reached LinkedIn, counted from each post's FIRST outcome reading (the stats "
            "sweep re-reads a post daily, so a plain count would report the backlog, not publishes).",
            _hogql(f"""
SELECT toStartOfDay(first_seen) AS day, count() AS posts_published
FROM (
    SELECT toString(properties.post_id) AS post_id, min(timestamp) AS first_seen
    FROM events
    WHERE event = 'post_outcome' AND properties.post_id IS NOT NULL
          AND timestamp >= now() - {PUBLISH_LOOKBACK}
    GROUP BY post_id
)
WHERE first_seen >= now() - {OPS_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsBar", _line("day", ["posts_published"])),
        ),
        _tile(
            "Health — Follower delta per day",
            "Day-over-day follower change per user, from the audience snapshots (issue #627). A "
            "user's FIRST reading has no previous day and is dropped — counting it would draw the "
            "whole existing audience as one day's growth.",
            _hogql(f"""
SELECT day, sum(followers - prev) AS follower_delta, sum(followers) AS total_followers
FROM (
    SELECT day, user_key, followers,
           lagInFrame(followers) OVER (PARTITION BY user_key ORDER BY day
                                       ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS prev
    FROM (
        SELECT toStartOfDay(timestamp) AS day,
               distinct_id AS user_key,
               max(toFloat(properties.follower_count)) AS followers
        FROM events
        WHERE event = 'audience_snapshot' AND properties.follower_count IS NOT NULL
              AND timestamp >= now() - {OPS_WINDOW}
        GROUP BY day, user_key
    )
)
WHERE prev > 0
GROUP BY day
ORDER BY day ASC
""", "ActionsLineGraph", _line("day", ["follower_delta"])),
        ),
        _tile(
            "Health — API error rate (daily %)",
            "5xx share of LEM's own API calls, with p95 latency beside it — the SPA-facing half of "
            "'is it up', which the Celery tiles say nothing about.",
            _hogql(f"""
SELECT toStartOfDay(timestamp) AS day,
       count() AS calls,
       round(100 * countIf(toInt(properties.status_code) >= 500) / nullIf(count(), 0), 2) AS error_pct,
       round(quantile(0.95)(toFloat(properties.latency_ms)), 0) AS p95_latency_ms
FROM events
WHERE event = 'api_call' AND timestamp >= now() - {OPS_WINDOW}
GROUP BY day
ORDER BY day ASC
""", "ActionsLineGraph", _line("day", ["error_pct"])),
        ),
    ]


# ── LEM Growth ───────────────────────────────────────────────────────────────────────

def growth_tiles() -> list:
    """The funnel dashboard: do LEM's business loops actually close?"""
    return [
        _tile(
            "Growth — Content loop funnel",
            "plan generated → post approved → post live and measured. The drop between step 2 and 3 "
            "is approved content that never shipped; between 1 and 2 is content the user rejected.",
            _funnel(["content_plan_generated", "post_approved", "post_outcome"],
                    date_from="-90d", window_days=45),
        ),
        _tile(
            "Growth — Signup → activation → subscription funnel",
            "The acquisition funnel end to end. `signup_started` is pseudonymous until the account "
            "exists — track_funnel_event aliases it onto the user id, so the funnel joins through.",
            _funnel(["signup_started", "signup_completed", "trial_started",
                     "onboarding_step_completed", "activated", "subscription_started"],
                    date_from="-180d", window_days=30),
        ),
        _tile(
            "Growth — Onboarding step drop-off",
            "Users reaching each checklist step and the median hours it took them — where activation "
            "stalls, at step granularity the funnel tile can't show.",
            _hogql(f"""
SELECT coalesce(toString(properties.step), 'unknown') AS step,
       uniqExact(distinct_id) AS users,
       round(median(toFloat(properties.hours_since_start)), 2) AS median_hours_since_start
FROM events
WHERE event = 'onboarding_step' AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY step
ORDER BY users DESC
""", "ActionsBar", _line("step", ["users"])),
        ),
        _tile(
            "Growth — Engagement loop (comment → author reply → our reply)",
            "The engagement loop as conversion rates off comment_outcome (issue #628). The "
            "connection-accepted and DM-reply steps join here once that instrumentation lands.",
            _hogql(f"""
SELECT toStartOfWeek(timestamp) AS week,
       count() AS comments_measured,
       countIf(toBool(properties.author_replied)) AS author_replies,
       countIf(toBool(properties.our_reply_sent)) AS our_replies,
       round(100 * countIf(toBool(properties.author_replied)) / nullIf(count(), 0), 2) AS author_reply_pct,
       round(100 * countIf(toBool(properties.our_reply_sent))
             / nullIf(countIf(toBool(properties.author_replied)), 0), 2) AS we_replied_back_pct
FROM events
WHERE event = 'comment_outcome' AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY week
ORDER BY week ASC
""", "ActionsLineGraph", _line("week", ["author_reply_pct", "we_replied_back_pct"])),
        ),
        _tile(
            "Growth — Comment visibility (Most-relevant demotion)",
            "Share of readable comment readings still visible under LinkedIn's default sort. NULL "
            "readings are excluded from the denominator — an unread sort control is not a demotion.",
            _hogql(f"""
SELECT toStartOfWeek(timestamp) AS week,
       countIf(properties.visible_most_relevant IS NOT NULL) AS readable,
       round(100 * countIf(toBool(properties.visible_most_relevant))
             / nullIf(countIf(properties.visible_most_relevant IS NOT NULL), 0), 2) AS visible_pct
FROM events
WHERE event = 'comment_outcome' AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY week
ORDER BY week ASC
""", "ActionsLineGraph", _line("week", ["visible_pct"])),
        ),
        _tile(
            "Growth — Engagement rate & reach per week",
            "The outcome the whole content loop exists to move: weekly median engagement rate with "
            "the impressions and reactions underneath it.",
            _hogql(f"""
SELECT toStartOfWeek(timestamp) AS week,
       count() AS readings,
       round(median(toFloat(properties.engagement_rate)), 6) AS median_engagement_rate,
       coalesce(sum(toFloat(properties.impressions)), 0) AS impressions,
       coalesce(sum(toFloat(properties.reactions)), 0) AS reactions
FROM events
WHERE event = 'post_outcome' AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY week
ORDER BY week ASC
""", "ActionsLineGraph", _line("week", ["median_engagement_rate"])),
        ),
        _tile(
            "Growth — Audience growth per week",
            "Followers, connections and profile views at each week's end — the audience side of the "
            "same loop, so a reach change can be read against the audience that produced it.",
            _hogql(f"""
SELECT toStartOfWeek(timestamp) AS week,
       max(toFloat(properties.follower_count)) AS followers,
       max(toFloat(properties.connection_count)) AS connections,
       sum(toFloat(properties.profile_views)) AS profile_views
FROM events
WHERE event = 'audience_snapshot' AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY week
ORDER BY week ASC
""", "ActionsLineGraph", _line("week", ["followers", "connections"])),
        ),
    ]


# ── Endpoints — the in-SPA "your stats" panel (issue #654) ────────────────────────────

def endpoint_specs() -> list:
    """The three per-user Endpoints behind the SPA's stats panel. Pure — no live PostHog state — so
    it's reusable for `--print-sql` and for diffing. Every query is scoped with
    `distinct_id = {variables.distinct_id}`; `endpoint_query()` binds that placeholder to a live
    variable id at apply time, the same two-step shape `alert_payload` uses for a live insight id."""
    return [
        {"name": ENDPOINT_POSTS_ENGAGEMENT,
         "description": "Weekly posts measured, median engagement rate and impressions for ONE "
                        "user (issue #654) — the SPA's own-stats panel read via /run.",
         "sql": """
SELECT toStartOfWeek(timestamp) AS week,
       count() AS posts_measured,
       round(median(toFloat(properties.engagement_rate)), 6) AS median_engagement_rate,
       coalesce(sum(toFloat(properties.impressions)), 0) AS impressions
FROM events
WHERE event = 'post_outcome' AND distinct_id = {variables.distinct_id}
      AND timestamp >= now() - INTERVAL 90 DAY
GROUP BY week
ORDER BY week ASC
"""},
        {"name": ENDPOINT_COMMENT_ACTIVITY,
         "description": "Weekly comments measured and author-reply rate for ONE user (issue #654), "
                        "off the #628 comment-outcome sweep.",
         "sql": """
SELECT toStartOfWeek(timestamp) AS week,
       count() AS comments_measured,
       countIf(toBool(properties.author_replied)) AS author_replies,
       round(100 * countIf(toBool(properties.author_replied)) / nullIf(count(), 0), 2) AS author_reply_pct
FROM events
WHERE event = 'comment_outcome' AND distinct_id = {variables.distinct_id}
      AND timestamp >= now() - INTERVAL 90 DAY
GROUP BY week
ORDER BY week ASC
"""},
        {"name": ENDPOINT_LLM_COST_BY_FEATURE,
         "description": "30-day LLM spend by feature bucket for ONE user (issue #654). Reads "
                        "`llm_call` (LEM's own cost estimate, keyed by feature) — never "
                        "`$ai_generation`, per docs/llm-analytics.md.",
         "sql": """
SELECT coalesce(nullIf(toString(properties.feature), ''), 'unattributed') AS feature,
       round(coalesce(sum(toFloat(properties.cost_usd)), 0), 6) AS spend_usd,
       count() AS calls
FROM events
WHERE event = 'llm_call' AND distinct_id = {variables.distinct_id}
      AND timestamp >= now() - INTERVAL 30 DAY
GROUP BY feature
ORDER BY spend_usd DESC
"""},
    ]


def endpoint_query(spec: dict, variable_id, code_name: str) -> dict:
    """The live HogQLQuery body for one endpoint spec. PostHog validates every `{variables.X}`
    placeholder against a declared variable at create/update time, so the definition has to carry
    the variable even though the FastAPI proxy supplies its VALUE fresh on every `/run` call."""
    return {"kind": "HogQLQuery", "query": spec["sql"].strip(),
            "variables": {str(variable_id): {"variableId": str(variable_id), "code_name": code_name,
                                             "value": None, "isNull": True}}}


def plan_variable(existing_variables: dict) -> dict:
    """Ensure the ONE InsightVariable every per-user endpoint scopes on exists. `existing_variables`
    maps name -> {"id", "code_name"}. Unlike dashboards/insights this is a single resource, so the
    action is singular rather than a list."""
    found = existing_variables.get(DISTINCT_ID_VARIABLE_NAME)
    if found is not None:
        return {"action": "unchanged_variable", "id": found["id"], "code_name": found["code_name"]}
    return {"action": "create_variable",
            "payload": {"name": DISTINCT_ID_VARIABLE_NAME, "type": "String", "default_value": None}}


def plan_endpoints(specs: list, existing_endpoints: dict, variable_id, code_name: Optional[str]) -> list:
    """Diff the endpoint spec against what PostHog already has. An endpoint is `blocked` (not
    creatable) until the distinct_id variable exists — mirrors `plan_alerts`'s missing-insight
    shape, so a dry-run never claims it can create something the apply pass actually can't yet."""
    actions: list = []
    for spec in specs:
        if variable_id is None:
            actions.append({"action": "blocked_endpoint", "endpoint": spec["name"],
                            "reason": f"'{DISTINCT_ID_VARIABLE_NAME}' insight variable does not "
                                     "exist yet"})
            continue
        query = endpoint_query(spec, variable_id, code_name)
        payload = {"name": spec["name"], "description": spec["description"], "query": query,
                  "data_freshness_seconds": ENDPOINT_DATA_FRESHNESS_SECONDS, "is_active": True,
                  "tags": ENDPOINT_TAGS}
        found = existing_endpoints.get(spec["name"])
        if found is None:
            actions.append({"action": "create_endpoint", "endpoint": spec["name"], "payload": payload})
            continue
        if (found.get("query") == query and found.get("description") == spec["description"]
                and found.get("data_freshness_seconds") == ENDPOINT_DATA_FRESHNESS_SECONDS
                and found.get("is_active", True)):
            actions.append({"action": "unchanged_endpoint", "endpoint": spec["name"]})
        else:
            actions.append({"action": "update_endpoint", "endpoint": spec["name"], "payload": payload})
    return actions


# ── LEM Channels (issue #658) ────────────────────────────────────────────────────────
# Marketing attribution. Every tile groups on the SAME vocabulary the source side writes
# (utilities/marketing/attribution.py) and the capture side normalizes
# (observability.normalize_attribution) — utm_source/utm_campaign on the event, and the first-touch
# `initial_*` person properties for the later events that cannot know the UTMs.
#
# The two signup events are never mixed: `signup_completed` is the server's (the funnel of record)
# and `signup_completed_web` is the browser's (the web-analytics conversion goal). Each tile picks
# ONE, because a person who converted produces both and summing them doubles every count.

def channel_tiles() -> list:
    """Which channels actually bring signups, and what happens to them after they arrive."""
    return [
        _tile(
            "Channels — Signups by source and campaign (90d)",
            "Every completed signup grouped by the UTMs it arrived with, plus the derived channel. "
            "An untagged arrival shows as (direct) — a row of those against a campaign we are "
            "running is the tell that a link shipped without going through build_utm_url.",
            _hogql(f"""
SELECT coalesce(nullIf(toString(properties.channel), ''), 'direct') AS channel,
       coalesce(nullIf(toString(properties.utm_source), ''), '(none)') AS source,
       coalesce(nullIf(toString(properties.utm_campaign), ''), '(none)') AS campaign,
       coalesce(nullIf(toString(properties.utm_content), ''), '(none)') AS placement,
       count() AS signups
FROM events
WHERE event = 'signup_completed' AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY channel, source, campaign, placement
ORDER BY signups DESC
LIMIT 100
""", "ActionsTable"),
        ),
        _tile(
            "Channels — Signups per week by channel",
            "The channel mix over time. A campaign that starts working shows up here as a shift in "
            "the mix, not just a bump in the total.",
            _hogql(f"""
SELECT toStartOfWeek(timestamp) AS week,
       countIf(toString(properties.channel) = 'linkedin') AS linkedin,
       countIf(toString(properties.channel) = 'newsletter') AS newsletter,
       countIf(toString(properties.channel) = 'youtube') AS youtube,
       countIf(toString(properties.channel) = 'referral') AS referral,
       countIf(toString(properties.channel) = 'seo') AS seo,
       countIf(toString(properties.channel) IN ('direct', '')) AS direct,
       count() AS signups
FROM events
WHERE event = 'signup_completed' AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY week
ORDER BY week ASC
""", "ActionsBar", _line("week", ["linkedin", "newsletter", "youtube", "referral", "seo", "direct"])),
        ),
        _tile(
            "Channels — Activations by first-touch channel (90d)",
            "Signups vs activations per channel, off the `initial_channel` person property "
            "track_funnel_event writes at first touch — an `activated` event carries no UTMs of its "
            "own, so a channel that converts well but activates badly is only visible this way.",
            _hogql(f"""
SELECT coalesce(nullIf(toString(person.properties.initial_channel), ''), 'direct') AS channel,
       uniqExactIf(distinct_id, event = 'signup_completed') AS signups,
       uniqExactIf(distinct_id, event = 'activated') AS activations,
       round(100 * uniqExactIf(distinct_id, event = 'activated')
             / nullIf(uniqExactIf(distinct_id, event = 'signup_completed'), 0), 2) AS activation_pct
FROM events
WHERE event IN ('signup_completed', 'activated') AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY channel
ORDER BY signups DESC
""", "ActionsTable"),
        ),
        _tile(
            "Channels — Brand post → site visit → signup funnel",
            "The organic-LinkedIn acquisition path end to end: a UTM-tagged brand CTA lands a "
            "pageview, the visitor starts signup, the account is created. The first step is scoped "
            "to utm_source=linkedin, so this measures the brand account's own traffic only.",
            _funnel([("$pageview", [{"key": "utm_source", "value": ["linkedin"],
                                     "operator": "exact", "type": "event"}]),
                     "signup_started", "signup_completed"],
                    date_from="-90d", window_days=30),
        ),
        _tile(
            "Channels — Referral signups by referrer (90d)",
            "Signups that arrived on a `?ref=<user id>` link. The full referral program is its own "
            "issue; this is the groundwork reading, so the link format can be proven end to end "
            "before any reward logic is built on it.",
            _hogql(f"""
SELECT coalesce(nullIf(toString(properties.ref), ''), '(none)') AS referrer_user_id,
       count() AS signups
FROM events
WHERE event = 'signup_completed' AND properties.ref IS NOT NULL
      AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY referrer_user_id
ORDER BY signups DESC
LIMIT 50
""", "ActionsTable"),
        ),
        _tile(
            "Channels — Tagged landing visits per week",
            "Pageviews arriving with UTMs, by source — the TOP of the funnel the signup tiles "
            "measure the bottom of. A source with visits and no signups is a landing-page problem, "
            "not an attribution one.",
            _hogql(f"""
SELECT toStartOfWeek(timestamp) AS week,
       coalesce(nullIf(toString(properties.utm_source), ''), '(none)') AS source,
       count() AS visits,
       uniqExact(distinct_id) AS visitors
FROM events
WHERE event = '$pageview' AND properties.utm_source IS NOT NULL
      AND timestamp >= now() - {GROWTH_WINDOW}
GROUP BY week, source
ORDER BY week ASC, visits DESC
""", "ActionsTable"),
        ),
    ]


# ── web-analytics conversion goals (issue #658) ──────────────────────────────────────
# PostHog's web analytics picks its conversion goal from an ACTION, so the two goals are defined
# here rather than clicked together in the UI — same reason as every other tile in this file.
GOAL_SIGNUP = "LEM — Signup completed (conversion goal)"
GOAL_ACTIVATION = "LEM — Activated: first post published (conversion goal)"


def conversion_goal_specs() -> list:
    """The two web-analytics conversion goals: signup and activation.

    The signup goal reads the BROWSER event (`signup_completed_web`), not the API's
    `signup_completed`. Web analytics attributes a conversion to the SESSION that produced it, and a
    server-side event has no session or pageview context — pointing the goal at it would report
    every signup as an unattributed conversion. Activation has no browser equivalent (it is decided
    by a Celery task), so it converts against the person's first-touch channel instead."""
    return [
        {"name": GOAL_SIGNUP,
         "description": "A new account was created in the browser. Fired by ui/src/utils/"
                        "analytics.ts recordSignup(); distinct from the API's `signup_completed`, "
                        "which is the funnel of record — never sum the two.",
         "steps": [{"event": "signup_completed_web"}]},
        {"name": GOAL_ACTIVATION,
         "description": "The user reached the activation aha (first post published). Server-side, "
                        "so it attributes on the person's first-touch channel rather than a session.",
         "steps": [{"event": "activated"}]},
    ]


def build_specs() -> list:
    """The full issue-#650 dashboard set. Pure — same input every run, so a diff against PostHog is
    meaningful and `--apply` is idempotent."""
    return [
        {"name": DASH_HEALTH,
         "description": "Is LEM running, and what is it costing? Celery failures, LinkedIn 429 "
                        "breaker trips, proxy-reported LLM spend, comments/posts per day, follower "
                        "delta and API error rate. The four threshold alerts page off this one.",
         "tiles": health_tiles()},
        {"name": DASH_GROWTH,
         "description": "Do LEM's loops close? The content-loop and signup→subscription funnels, "
                        "onboarding drop-off, the comment→reply engagement loop and the engagement "
                        "rate/audience trends. Emailed to the owner weekly.",
         "tiles": growth_tiles()},
        {"name": DASH_CHANNELS,
         "description": "Where signups come from (issue #658): signups and activations by UTM "
                        "source/campaign and first-touch channel, the brand-post → visit → signup "
                        "funnel, referral arrivals and tagged landing visits.",
         "tiles": channel_tiles()},
    ]


# ── alerts & subscription specs ──────────────────────────────────────────────────────

def alert_specs() -> list:
    """The four threshold alerts (PostHog's free tier allows five). `bounds` is PostHog's own shape:
    a value BELOW `lower` or ABOVE `upper` breaches."""
    return [
        {"name": "LEM — comments per week below floor",
         "insight": INSIGHT_COMMENTS_WEEKLY,
         "bounds": {"lower": COMMENT_WEEKLY_FLOOR},
         "description": f"Fewer than {COMMENT_WEEKLY_FLOOR} comments measured in a week — feed "
                        "commenting is broken, held or rate-limited."},
        {"name": "LEM — LLM spend per day over cap",
         "insight": INSIGHT_LLM_SPEND,
         "bounds": {"upper": LLM_DAILY_COST_CAP_USD},
         "description": f"Proxy-reported LLM spend over ${LLM_DAILY_COST_CAP_USD:g} in a day."},
        {"name": "LEM — Celery failure spike",
         "insight": INSIGHT_CELERY_FAILURES,
         "bounds": {"upper": CELERY_FAILURES_PER_DAY_CEILING},
         "description": f"More than {CELERY_FAILURES_PER_DAY_CEILING} task failures in a day — "
                        "something systemic, not one bad task."},
        {"name": "LEM — LinkedIn 429 spike",
         "insight": INSIGHT_RATE_LIMIT_TRIPS,
         "bounds": {"upper": RATE_LIMIT_TRIPS_PER_DAY_CEILING},
         "description": f"More than {RATE_LIMIT_TRIPS_PER_DAY_CEILING} 429 breaker trips in a day."},
    ]


def evaluate_threshold(bounds: dict, value) -> dict:
    """Would `value` breach these bounds? The same comparison PostHog runs server-side, exposed so
    `--simulate` can prove an alert fires without waiting for a real breach.

    A non-numeric value is NOT a breach — a missing reading means the insight had nothing to
    evaluate, and paging on that would make every quiet ingest gap an incident."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return {"breach": False, "reason": "no numeric value to evaluate"}
    lower, upper = bounds.get("lower"), bounds.get("upper")
    if lower is not None and numeric < float(lower):
        return {"breach": True, "reason": f"{numeric:g} is below the floor of {float(lower):g}"}
    if upper is not None and numeric > float(upper):
        return {"breach": True, "reason": f"{numeric:g} is above the cap of {float(upper):g}"}
    return {"breach": False, "reason": f"{numeric:g} is within bounds"}


def next_report_start(now: datetime) -> str:
    """The first send time for the weekly subscription: the next REPORT_WEEKDAY at REPORT_HOUR_UTC,
    always strictly in the future so PostHog doesn't backfill a send the moment it's created."""
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    target = weekdays.index(REPORT_WEEKDAY)
    start = now.astimezone(timezone.utc).replace(hour=REPORT_HOUR_UTC, minute=0, second=0,
                                                 microsecond=0)
    days_ahead = (target - start.weekday()) % 7
    if days_ahead == 0 and start <= now.astimezone(timezone.utc):
        days_ahead = 7
    return (start + timedelta(days=days_ahead)).isoformat().replace("+00:00", "Z")


def subscription_spec(email: str, now: Optional[datetime] = None) -> dict:
    """The weekly Growth-dashboard email — the automated replacement for the hand-run perf report."""
    return {"dashboard": DASH_GROWTH,
            "title": "LEM Growth — weekly report",
            "target_type": "email",
            "target_value": email,
            "frequency": "weekly",
            "interval": 1,
            "byweekday": [REPORT_WEEKDAY],
            "start_date": next_report_start(now or datetime.now(timezone.utc))}


# ── pure planning (unit-tested) ──────────────────────────────────────────────────────

def plan_actions(specs: list, existing_dashboards: dict, existing_insights: dict) -> list:
    """Diff the dashboard/insight spec against what PostHog already has.

    `existing_dashboards` maps dashboard name -> id; `existing_insights` maps insight name ->
    {"id", "query", "description", "dashboards"}. Returns ordered actions: `create_dashboard`,
    `create_insight`, `update_insight`, `unchanged`. `description` is optional on the existing
    record: when a caller omits it the spec value is assumed, so only a caller that actually reports
    the stored description can trigger a description-drift update."""
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


def plan_alerts(specs: list, existing_alerts: dict, insight_ids: dict,
                subscribed_users: Optional[list] = None) -> list:
    """Diff the alert spec. An alert whose insight does not exist yet is `blocked` rather than
    created — PostHog needs the insight id, and reporting it as creatable would make a dry-run lie
    about what a follow-up `--apply` can do in one pass.

    Subscribers are part of the diff: an alert PostHog created with an empty `subscribed_users`
    (the key lacked `user:read` on the run that made it) emails nobody, and matching only on bounds
    would call that alert healthy forever. A run that DOES know the owner repairs it. A run that
    does not (`subscribed_users` empty) never reports drift it couldn't fix."""
    actions: list = []
    wanted_subscribers = set(subscribed_users or [])
    for spec in specs:
        insight_id = insight_ids.get(spec["insight"])
        found = existing_alerts.get(spec["name"])
        if insight_id is None:
            actions.append({"action": "blocked_alert", "alert": spec["name"],
                            "reason": f"insight '{spec['insight']}' does not exist yet"})
            continue
        if found is None:
            actions.append({"action": "create_alert", "alert": spec["name"],
                            "payload": alert_payload(spec, insight_id, subscribed_users)})
            continue
        current_subscribers = set(found.get("subscribed_users") or [])
        if (found.get("bounds") == spec["bounds"] and found.get("insight_id") == insight_id
                and found.get("enabled", True)
                and wanted_subscribers <= current_subscribers):
            actions.append({"action": "unchanged_alert", "alert": spec["name"],
                            "alert_id": found["id"]})
        else:
            # Union, not replace — a recipient the owner added in the UI is not ours to drop.
            actions.append({"action": "update_alert", "alert": spec["name"],
                            "alert_id": found["id"],
                            "payload": alert_payload(
                                spec, insight_id,
                                sorted(wanted_subscribers | current_subscribers, key=str))})
    return actions


def alert_payload(spec: dict, insight_id, subscribed_users: Optional[list] = None) -> dict:
    """PostHog's alert body. `series_index` 0 is the only series an alert-eligible insight has."""
    return {"name": spec["name"],
            "insight": insight_id,
            "subscribed_users": list(subscribed_users or []),
            "config": {"type": "TrendsAlertConfig", "series_index": 0},
            "threshold": {"configuration": {"type": "absolute", "bounds": dict(spec["bounds"])}},
            "condition": {"type": "absolute_value"},
            "calculation_interval": "daily",
            "enabled": True}


def plan_conversion_goals(specs: list, existing_actions: Optional[dict]) -> list:
    """Diff the web-analytics conversion goals against PostHog's existing actions.

    `existing_actions` maps action name -> {"id", "steps", "description"}, or is None when the
    actions could not be READ at all (a key without the `action` scope). None is `blocked_goal`,
    never "missing" — mirroring `plan_endpoints`, so an unreadable state is never mistaken for an
    empty one and reported as something `--apply` could create."""
    actions: list = []
    for spec in specs:
        if existing_actions is None:
            actions.append({"action": "blocked_goal", "goal": spec["name"],
                            "reason": "PostHog actions could not be read (needs the `action` scope)"})
            continue
        found = existing_actions.get(spec["name"])
        wanted = [str(step.get("event") or "") for step in spec["steps"]]
        if found is None:
            actions.append({"action": "create_goal", "goal": spec["name"], "spec": spec})
            continue
        current = [str(step.get("event") or "") for step in (found.get("steps") or [])]
        described = found.get("description", spec["description"]) == spec["description"]
        if current == wanted and described:
            actions.append({"action": "unchanged_goal", "goal": spec["name"],
                            "goal_id": found["id"]})
        else:
            actions.append({"action": "update_goal", "goal": spec["name"],
                            "goal_id": found["id"], "spec": spec})
    return actions


def plan_subscriptions(specs: list, existing_subscriptions: list, dashboard_ids: dict) -> list:
    """Diff the email subscriptions. Matched on (dashboard, recipient, frequency) and NOT on
    `start_date`: PostHog advances that with every send, so comparing it would recreate the
    subscription on every run."""
    actions: list = []
    for spec in specs:
        dashboard_id = dashboard_ids.get(spec["dashboard"])
        if dashboard_id is None:
            actions.append({"action": "blocked_subscription", "subscription": spec["title"],
                            "reason": f"dashboard '{spec['dashboard']}' does not exist yet"})
            continue
        match = next((s for s in existing_subscriptions
                      if s.get("dashboard") == dashboard_id
                      and (s.get("target_value") or "").strip().lower()
                      == spec["target_value"].strip().lower()
                      and s.get("frequency") == spec["frequency"]
                      and not s.get("deleted")), None)
        payload = {**{k: v for k, v in spec.items() if k != "dashboard"}, "dashboard": dashboard_id}
        if match is None:
            actions.append({"action": "create_subscription", "subscription": spec["title"],
                            "payload": payload})
        else:
            actions.append({"action": "unchanged_subscription", "subscription": spec["title"],
                            "subscription_id": match.get("id")})
    return actions


UNCHANGED_ACTIONS = ("unchanged", "unchanged_alert", "unchanged_subscription", "unchanged_goal",
                    "unchanged_variable", "unchanged_endpoint")


def pending(actions: list) -> list:
    return [a for a in actions if a["action"] not in UNCHANGED_ACTIONS]


def summarize(actions: list) -> str:
    counts: dict = {}
    for action in actions:
        counts[action["action"]] = counts.get(action["action"], 0) + 1
    return ", ".join(f"{name}={counts[name]}" for name in sorted(counts)) or "nothing to do"


def dashboard_url(app_host: str, project_id: str, dashboard_id) -> str:
    return f"{app_host.rstrip('/')}/project/{project_id}/dashboard/{dashboard_id}"


# ── I/O (mocked in tests) ────────────────────────────────────────────────────────────

class PostHogClient:
    """Thin PostHog REST wrapper — only the calls this provisioner needs."""

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

    def whoami(self) -> dict:
        """The personal-API-key owner — the default alert subscriber and report recipient. Needs
        `user:read`; a key without it just means alerts are created with no subscriber."""
        import requests
        response = requests.get(f"{self.app_host}/api/users/@me/", headers=self._headers, timeout=30)
        response.raise_for_status()
        return response.json() or {}

    def list_dashboards(self) -> dict:
        return {d["name"]: d["id"] for d in self._paged("/dashboards/?limit=100")
                if not d.get("deleted")}

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

    def list_alerts(self) -> dict:
        alerts = {}
        for item in self._paged("/alerts/?limit=100"):
            configuration = ((item.get("threshold") or {}).get("configuration") or {})
            alerts[item.get("name")] = {
                "id": item.get("id"),
                "insight_id": _insight_id_of(item.get("insight")),
                "bounds": {k: v for k, v in (configuration.get("bounds") or {}).items()
                           if v is not None},
                "enabled": item.get("enabled", True),
                "subscribed_users": [_user_id_of(u) for u in (item.get("subscribed_users") or [])
                                     if _user_id_of(u) is not None],
            }
        return alerts

    def list_subscriptions(self) -> list:
        return [s for s in self._paged("/subscriptions/?limit=100") if not s.get("deleted")]

    def list_actions(self) -> dict:
        actions = {}
        for item in self._paged("/actions/?limit=100"):
            if item.get("deleted"):
                continue
            actions[item.get("name")] = {
                "id": item.get("id"),
                "steps": list(item.get("steps") or []),
                "description": item.get("description") or "",
            }
        return actions

    def create_action(self, spec: dict) -> int:
        return self._request("POST", "/actions/", json=_goal_payload(spec)).get("id")

    def update_action(self, action_id, spec: dict) -> None:
        self._request("PATCH", f"/actions/{action_id}/", json=_goal_payload(spec))

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

    def create_alert(self, payload: dict) -> int:
        return self._request("POST", "/alerts/", json=payload).get("id")

    def update_alert(self, alert_id, payload: dict) -> None:
        self._request("PATCH", f"/alerts/{alert_id}/", json=payload)

    def create_subscription(self, payload: dict) -> int:
        return self._request("POST", "/subscriptions/", json=payload).get("id")

    def dashboard_insight_ids(self, dashboard_id: int) -> list:
        """Insight ids currently tiled on a dashboard, in tile order. The dashboard LIST endpoint
        returns no tiles, so this has to read the detail route."""
        detail = self._request("GET", f"/dashboards/{dashboard_id}/")
        ids = [(tile.get("insight") or {}).get("id") for tile in (detail.get("tiles") or [])]
        return [i for i in ids if i]

    def list_insight_variables(self) -> dict:
        """Maps variable name -> {"id", "code_name"}. `code_name` is PostHog-derived (read-only on
        write), so callers always read it back here rather than assuming it matches `name`."""
        return {v["name"]: {"id": v["id"], "code_name": v.get("code_name")}
                for v in self._paged("/insight_variables/?limit=100")}

    def create_insight_variable(self, payload: dict) -> dict:
        return self._request("POST", "/insight_variables/", json=payload)

    def list_endpoints(self) -> dict:
        endpoints = {}
        for item in self._paged("/endpoints/?limit=100"):
            endpoints[item.get("name")] = {
                "query": item.get("query"),
                "description": item.get("description") or "",
                "data_freshness_seconds": item.get("data_freshness_seconds"),
                "is_active": item.get("is_active", True),
            }
        return endpoints

    def create_endpoint(self, payload: dict) -> None:
        self._request("POST", "/endpoints/", json=payload)

    def update_endpoint(self, name: str, payload: dict) -> None:
        self._request("PATCH", f"/endpoints/{name}/", json=payload)


def _goal_payload(spec: dict) -> dict:
    """PostHog's action body for a conversion goal."""
    return {"name": spec["name"], "description": spec["description"],
            "steps": [{"event": step["event"]} for step in spec["steps"]], "tags": TAGS}


def _insight_id_of(insight) -> Optional[int]:
    """PostHog returns an alert's insight as a bare id on some versions and a nested object on
    others — normalize both so the diff doesn't churn."""
    if isinstance(insight, dict):
        return insight.get("id")
    return insight


def _user_id_of(user) -> Optional[int]:
    """An alert's subscriber, which PostHog serializes as a nested user object on read and a bare id
    on write — the same two shapes as `insight`."""
    if isinstance(user, dict):
        return user.get("id")
    return user


def apply_actions(client: PostHogClient, actions: list, dry_run: bool = True) -> list:
    """Execute the plan. `dry_run` reports without writing. Returns the log lines emitted."""
    dashboard_ids: dict = {}
    log: list = []
    for action in actions:
        kind = action["action"]
        if kind == "create_dashboard":
            name = action["dashboard"]
            if dry_run:
                log.append(f"[dry-run] create dashboard '{name}'")
                continue
            dashboard_ids[name] = client.create_dashboard(name, action["description"])
            log.append(f"created dashboard '{name}' -> {dashboard_ids[name]}")
        elif kind in ("create_insight", "update_insight"):
            name = action["dashboard"]
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
        elif kind == "create_alert":
            if dry_run:
                log.append(f"[dry-run] create alert '{action['alert']}'")
                continue
            log.append(f"created alert '{action['alert']}' -> {client.create_alert(action['payload'])}")
        elif kind == "update_alert":
            if dry_run:
                log.append(f"[dry-run] update alert '{action['alert']}'")
                continue
            client.update_alert(action["alert_id"], action["payload"])
            log.append(f"updated alert '{action['alert']}'")
        elif kind in ("create_goal", "update_goal"):
            verb = "create" if kind == "create_goal" else "update"
            if dry_run:
                log.append(f"[dry-run] {verb} conversion goal '{action['goal']}'")
                continue
            if kind == "create_goal":
                goal_id = client.create_action(action["spec"])
                log.append(f"created conversion goal '{action['goal']}' -> {goal_id}")
            else:
                client.update_action(action["goal_id"], action["spec"])
                log.append(f"updated conversion goal '{action['goal']}'")
        elif kind == "create_subscription":
            if dry_run:
                log.append(f"[dry-run] create subscription '{action['subscription']}'")
                continue
            payload = dict(action["payload"])
            # PostHog rejects a dashboard subscription that doesn't name its insights explicitly
            # ("Select at least one insight"), even when the dashboard already has tiles — and caps
            # the list at SUBSCRIPTION_MAX_INSIGHTS. The ids aren't knowable at plan time (the
            # insights may be created in this same run), so they're resolved here.
            if not payload.get("dashboard_export_insights"):
                insight_ids = client.dashboard_insight_ids(payload["dashboard"])
                if not insight_ids:
                    log.append(f"skipped subscription '{action['subscription']}': its dashboard has "
                               f"no insights yet — re-run once the tiles exist")
                    continue
                payload["dashboard_export_insights"] = insight_ids[:SUBSCRIPTION_MAX_INSIGHTS]
            subscription_id = client.create_subscription(payload)
            log.append(f"created subscription '{action['subscription']}' -> {subscription_id}")
        elif kind == "create_variable":
            if dry_run:
                log.append(f"[dry-run] create insight variable '{action['payload']['name']}'")
                continue
            created = client.create_insight_variable(action["payload"])
            log.append(f"created insight variable '{action['payload']['name']}' -> {created.get('id')}")
        elif kind in ("create_endpoint", "update_endpoint"):
            verb = "create" if kind == "create_endpoint" else "update"
            if dry_run:
                log.append(f"[dry-run] {verb} endpoint '{action['endpoint']}'")
                continue
            if kind == "create_endpoint":
                client.create_endpoint(action["payload"])
            else:
                client.update_endpoint(action["endpoint"], action["payload"])
            log.append(f"{verb}d endpoint '{action['endpoint']}'")
        elif kind in ("blocked_alert", "blocked_subscription", "blocked_endpoint", "blocked_goal"):
            name = (action.get("alert") or action.get("subscription") or action.get("endpoint")
                    or action.get("goal"))
            log.append(f"skipped '{name}': {action['reason']}")
    return log


def _print_specs() -> None:
    for spec in build_specs():
        for tile in spec["tiles"]:
            query = tile["query"]
            print(f"\n-- {spec['name']} :: {tile['name']}")
            if query["kind"] == "DataVisualizationNode":
                print(query["source"]["query"])
            else:
                print(json.dumps(query["source"], indent=2))
    for spec in endpoint_specs():
        print(f"\n-- endpoint :: {spec['name']}")
        print(spec["sql"].strip())


def _simulate(expression: str) -> int:
    """`--simulate '<alert name>=<value>'` — the synthetic threshold breach the acceptance criteria
    ask for, without waiting on live data."""
    name, _, raw = expression.partition("=")
    spec = next((s for s in alert_specs() if s["name"].lower() == name.strip().lower()), None)
    if spec is None:
        print(f"Unknown alert '{name.strip()}'. Known: "
              + "; ".join(s["name"] for s in alert_specs()), file=sys.stderr)
        return 1
    verdict = evaluate_threshold(spec["bounds"], raw.strip())
    print(f"{spec['name']}: {'BREACH' if verdict['breach'] else 'ok'} — {verdict['reason']}")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision the LEM Health/Growth PostHog dashboards, alerts and weekly report.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes to PostHog.")
    mode.add_argument("--dry-run", action="store_true", help="Report changes only (default).")
    parser.add_argument("--print-sql", action="store_true",
                        help="Print every tile's query and exit.")
    parser.add_argument("--simulate", metavar="NAME=VALUE",
                        help="Report whether VALUE breaches the named alert's threshold, then exit.")
    parser.add_argument("--email", help="Weekly-report recipient (default $POSTHOG_REPORT_EMAIL).")
    args = parser.parse_args(argv)

    if args.print_sql:
        _print_specs()
        return 0
    if args.simulate:
        return _simulate(args.simulate)

    api_key = operator_api_key()
    if not api_key:
        print(f"{missing_key_message('operator')} — cannot reach PostHog.", file=sys.stderr)
        return 1
    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)
    dry_run = args.dry_run or not args.apply

    client = PostHogClient(api_key, project_id, app_host)
    specs = build_specs()
    try:
        dashboards, insights = client.list_dashboards(), client.list_insights()
        alerts, subscriptions = client.list_alerts(), client.list_subscriptions()
    except Exception as exc:
        print(f"Failed to read PostHog state: {exc}", file=sys.stderr)
        return 1

    # The conversion goals (issue #658) need an `action` scope the pre-#658 keys were never issued
    # with, so this is its OWN read: a key missing that scope must not take the dashboards, alerts
    # and endpoints down with it. Same degrade shape #654's endpoints read uses.
    try:
        existing_goals: Optional[dict] = client.list_actions()
    except Exception as exc:
        existing_goals = None
        print(f"Could not read PostHog actions ({exc}) — conversion goals not provisioned. The "
              "personal API key needs the `action` scope (read+write).", file=sys.stderr)

    try:
        me = client.whoami()
    except Exception as exc:
        me = {}
        print(f"Could not read the API key owner ({exc}) — alerts will have no subscriber.",
              file=sys.stderr)
    email = args.email or os.getenv("POSTHOG_REPORT_EMAIL") or me.get("email")
    subscribed_users = [me["id"]] if me.get("id") else []

    actions = plan_actions(specs, dashboards, insights)
    for line in apply_actions(client, actions, dry_run=dry_run):
        print(line)

    # Alerts and the subscription reference ids the insight/dashboard pass may have just minted, so
    # re-read before planning them. In a dry run nothing was written, so the pre-pass state stands.
    if not dry_run:
        dashboards, insights = client.list_dashboards(), client.list_insights()
    insight_ids = {name: found["id"] for name, found in insights.items()}

    alert_actions = plan_alerts(alert_specs(), alerts, insight_ids, subscribed_users)
    for line in apply_actions(client, alert_actions, dry_run=dry_run):
        print(line)

    goal_actions = plan_conversion_goals(conversion_goal_specs(), existing_goals)
    for line in apply_actions(client, goal_actions, dry_run=dry_run):
        print(line)

    subscription_actions: list = []
    if email:
        subscription_actions = plan_subscriptions([subscription_spec(email)], subscriptions,
                                                  dashboards)
        # The weekly email is the least important thing this script provisions, and it is the LAST
        # thing before the endpoints pass. Letting it raise meant one PostHog validation change took
        # the endpoints — and therefore the /user/posthog-stats panels — down with it. Report and
        # carry on instead.
        try:
            for line in apply_actions(client, subscription_actions, dry_run=dry_run):
                print(line)
        except Exception as exc:
            print(f"Could not provision the weekly report subscription ({exc}) — continuing.",
                  file=sys.stderr)
    else:
        print("No recipient (set POSTHOG_REPORT_EMAIL or --email) — weekly report not provisioned.",
              file=sys.stderr)

    # Endpoints (issue #654): the distinct_id variable has to exist before any endpoint referencing
    # it can be created, so it's applied first and re-read to get the live id/code_name — the same
    # create-then-reread shape the dashboard/insight pass above uses.
    try:
        existing_variables = client.list_insight_variables()
        existing_endpoints = client.list_endpoints()
    except Exception as exc:
        existing_variables, existing_endpoints = {}, {}
        print(f"Could not read PostHog Endpoints state ({exc}) — endpoints not provisioned.",
              file=sys.stderr)

    variable_action = plan_variable(existing_variables)
    variable_log = apply_actions(client, [variable_action], dry_run=dry_run)
    for line in variable_log:
        print(line)

    if variable_action["action"] == "unchanged_variable":
        variable_id, code_name = variable_action["id"], variable_action["code_name"]
    elif not dry_run:
        created = client.list_insight_variables().get(DISTINCT_ID_VARIABLE_NAME) or {}
        variable_id, code_name = created.get("id"), created.get("code_name")
    else:
        variable_id, code_name = None, None

    endpoint_actions = plan_endpoints(endpoint_specs(), existing_endpoints, variable_id, code_name)
    for line in apply_actions(client, endpoint_actions, dry_run=dry_run):
        print(line)

    all_actions = (actions + alert_actions + goal_actions + subscription_actions
                   + [variable_action] + endpoint_actions)
    print(summarize(all_actions))
    for spec in specs:
        dashboard_id = dashboards.get(spec["name"])
        if dashboard_id:
            print(f"{spec['name']}: {dashboard_url(app_host, project_id, dashboard_id)}")

    if dry_run and pending(all_actions):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
