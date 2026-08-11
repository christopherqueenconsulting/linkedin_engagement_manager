# KPI funnels, dashboards, alerts and the weekly report (issue #650)

LEM's PostHog project had 41 insights across 6 dashboards and still could not answer two questions:
*do the business loops close?* and *did something break overnight?* Everything was a point metric,
every watchdog was a hand-run cron, and the 2-week perf report was a human remembering to run it.

`scripts/posthog_provision.py` defines the answer as code: the consolidated dashboards, the funnels
over LEM's actual loops, four threshold alerts and one weekly email. Re-running it re-creates
anything edited away in the UI, and does nothing at all when the project already matches.

Issue #658 added a third dashboard (**LEM Channels**) and the two web-analytics conversion goals to
the same script — where signups come from, and which link brought them. That surface has its own
doc: `docs/marketing-attribution.md`. Everything below still describes Health and Growth.

```bash
# what would change (default; no writes)
poetry run python scripts/posthog_provision.py --dry-run
# create/update dashboards, insights, alerts and the weekly subscription
poetry run python scripts/posthog_provision.py --apply
# every tile's query, no network
poetry run python scripts/posthog_provision.py --print-sql
# would this reading page someone?
poetry run python scripts/posthog_provision.py --simulate 'LEM — LinkedIn 429 spike=9'
```

Exit codes: `0` in sync / applied, `2` changes pending (dry run), `1` error.

Env: `POSTHOG_PERSONAL_API_KEY` (scopes: `insight`, `dashboard`, `alert`, `action` and
`subscription` read+write, `user:read` so alerts get a subscriber, plus `endpoint` and
`insight_variable` read+write for the Endpoints panel below), `POSTHOG_PROJECT_ID`,
`POSTHOG_APP_HOST`, `POSTHOG_REPORT_EMAIL` (weekly recipient; falls back to the key owner's email,
overridable with `--email`).

## The two dashboards

**LEM Health** — is LEM running, and what is it costing? Celery failure count and rate, failing
tasks by name, LinkedIn 429 breaker trips, LLM spend per day, posts published per day, follower
delta and API error rate. The four alerts page off this dashboard's tiles.

**LEM Growth** — do the loops close? The content-loop and signup→subscription funnels, onboarding
step drop-off, the comment→reply engagement loop, comment visibility, engagement rate and audience
growth. This is the dashboard that gets emailed weekly.

These sit ON TOP of the existing cost/margin set (`scripts/posthog_dashboards.py`) rather than
replacing it — Health/Growth is where you look first, Cost Explorer / Margin by Cohort / Engagement
Lift / Unit-Economics Scorecard are the drill-downs. Both scripts plan by insight NAME against the
same project, so **every insight name must stay unique across the two**; a unit test fails the build
if they ever collide.

## Funnels

| Funnel | Steps | Window |
|---|---|---|
| Content loop | `content_plan_generated` → `post_approved` → `post_outcome` | 45 days |
| Signup → subscription | `signup_started` → `signup_completed` → `trial_started` → `onboarding_step_completed` → `activated` → `subscription_started` | 30 days |

The conversion windows are deliberately long: a content plan is 30 days of scheduled posts, so a
plan → approve → posted → engaged chain spans weeks, not a session. The signup funnel joins across
the pre-account boundary because `track_funnel_event` aliases the pseudonymous `signup_started`
person onto the real user id.

The **engagement loop** (comment → author reply → connection accepted → DM reply) is a HogQL
conversion tile rather than a funnel: only the first two steps are instrumented today
(`comment_outcome`, issue #628). Connection-accepted and DM-reply join it when that instrumentation
lands — as steps, not as a rewrite.

## Alerts

Four of PostHog's five free alerts. Thresholds live in the script (not in env) so the value that
pages someone is reviewable in a diff; change one and re-run `--apply`.

| Alert | Reads | Fires when |
|---|---|---|
| comments per week below floor | `comment_outcome`, weekly | < `COMMENT_WEEKLY_FLOOR` (5) in a week |
| LLM spend per day over cap | `$ai_generation.$ai_total_cost_usd`, daily | > `LLM_DAILY_COST_CAP_USD` ($15) |
| Celery failure spike | `celery_task` where `state = FAILURE`, daily | > `CELERY_FAILURES_PER_DAY_CEILING` (25) |
| LinkedIn 429 spike | `rate_limit_trip`, daily | > `RATE_LIMIT_TRIPS_PER_DAY_CEILING` (5) |

Three details that are not arbitrary:

- **Alert insights are native single-series trends queries.** PostHog can alert on trends, funnel
  and SQL insights, but the threshold is evaluated against ONE series by index — a breakdown or a
  second series makes `series_index: 0` ambiguous. The HogQL tiles beside them are for reading, not
  paging.
- **The comment floor is weekly, not daily.** Human pacing takes account-wide rest days (issue
  #626); a daily floor would page on a perfectly healthy zero-comment day.
- **The Celery filter is on `state` (a string), not `success` (a boolean).** A boolean property
  filter that silently matches nothing produces an alert that never fires, which is worse than no
  alert at all.

The emitting half of that last rule is enforced since #1218: a property a tile or alert filters on
is declared `label()` in `observability.EVENTS` and is coerced to a string on the way out, and every
pair listed here is pinned in `ALERT_FILTERED` (`tests/unit/utilities/test_observability_events.py`)
so demoting one fails CI rather than silencing an alert. A number an alert COMPARES
(`status_code >= 500`) is the opposite case and must stay numeric — see `docs/observability-map.md`.
**Add a filter here and add its property to that list**, or the guard cannot see it.

Alerts are diffed on bounds, insight, enabled state **and subscribers**. An alert created by a run
whose key lacked `user:read` has an empty `subscribed_users` and therefore emails nobody; matching
on bounds alone would call that alert healthy forever, so a later run that can name the owner
repairs it (adding to, never replacing, whoever is already subscribed).

`--simulate 'NAME=VALUE'` runs the same comparison PostHog runs server-side, so a breach can be
proven without waiting for one. A non-numeric/missing reading is deliberately NOT a breach: an
ingest gap is not an incident.

### The 429 signal

The breaker's trip only ever produced a WARNING log, and PostHog forwards `ERROR` and up by default
— so the one event that says *LinkedIn is throttling us* was invisible to both dashboards and
alerts. `mark_rate_limited()` now also emits `rate_limit_trip` (`cooldown_seconds`,
`consecutive_trips`, `reason`) via `observability.track_rate_limit_trip`. It is system-scoped:
LinkedIn rate-limits by egress IP, so a trip is an account-wide condition, never one user's. It
never raises — the breaker must open even when analytics is down.

## The weekly report

One PostHog dashboard subscription emails **LEM Growth** every Monday 09:00 UTC. That is what
replaces the hand-run perf-report cadence — no cron, no box, no one remembering.

Subscriptions are matched on (dashboard, recipient, frequency) and explicitly **not** on
`start_date`: PostHog advances that with every send, so diffing it would recreate the subscription
on every run and mail the owner a duplicate each time.

PostHog renders at most **6 tiles** into a subscription email (`MAX_ASSET_COUNT`), so Growth's tile
order is the email's priority order — the funnels and the engagement loop come first, audience
growth is the one that falls off the bottom. Free plans allow 5 subscriptions total; this uses one.

## What renders empty, and why that's fine

Several tiles read events that exist in code but have not flowed into this project yet
(`comment_outcome`, `audience_snapshot`, the funnel events, and the SPA's `post_approved` /
`content_plan_generated`). They resolve empty until those land — the same deliberate behaviour the
cost/margin dashboards have. **The alerts are the exception**: a floor alert on an event that never
arrives reads 0 and pages. Run `--apply` once the events are actually flowing, or expect the
comments-floor alert to fire on its first evaluation.

## Which LLM cost number

`$ai_generation` (the proxy's own `response_cost`) — never `llm_call`, and never both summed. See
`docs/llm-analytics.md` for the split: `llm_call` is LEM's estimate keyed by the tier alias asked
for, `$ai_generation` is what actually served it. A unit test asserts no tile here reads `llm_call`.

## Endpoints — the in-SPA "your stats" panel (issue #654)

The same `--apply` also provisions three PostHog **Endpoints** (beta) — HogQL queries published as
versioned, cached HTTP routes — behind the Dashboard's own "Live stats" card: weekly posts +
engagement, weekly comment activity, and 30-day LLM cost by feature (reading `llm_call`, same
money-question rule as everywhere else here). Endpoints were the fast path to that panel instead of
a bespoke MySQL reporting layer — PostHog already caches and versions the query.

Every query is scoped with `distinct_id = {variables.distinct_id}` and NOTHING is un-scoped:
PostHog is one project shared by every LEM account, so a project-wide read would leak one
customer's numbers into another's Dashboard. The placeholder resolves against a single provisioned
`InsightVariable` (`distinct_id`); an endpoint is reported `blocked_endpoint`, not creatable, until
that variable exists — the same shape `plan_alerts` already uses for a missing insight, so a dry
run never claims it can create something an apply pass actually can't yet.

`src/cqc_lem/utilities/posthog_endpoints.py` is the runtime half: `GET /user/posthog-stats` calls
each endpoint's `/run` with the CALLER's own `str(user_id)` bound to `distinct_id`, server-side
only — the personal API key never reaches the browser. Every failure mode (no key, endpoint not
yet provisioned, PostHog unreachable) degrades to `available: false` for that one panel rather than
breaking the response or the page; `PostHogStatsPanel.tsx` renders nothing at all once loaded if
every panel came back unavailable.

## Release annotations (issue #654)

`scripts/posthog_annotate.py`, called from `build-and-push.yml`'s deploy job right after
`deploy.sh`'s own health check passes, posts one project-scoped annotation — `"vX.Y.Z deployed"` —
so every insight graph shows exactly when a release shipped. It needs its own GH Actions secret,
`POSTHOG_PERSONAL_API_KEY` (scope: `annotation` read+write) — distinct from the env var of the same
name used locally by the provisioning scripts. Absent, the script prints why and exits 0; the step
also runs with `continue-on-error: true`, so a PostHog outage can never fail a release that already
shipped.
