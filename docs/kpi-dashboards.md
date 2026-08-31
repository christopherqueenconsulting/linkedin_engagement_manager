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

### SDUI selector-evidence visibility (issues #1117, #1270)

`track_selector_evidence` (`docs/observability-map.md`) had zero downstream visibility until now —
the event flowed into PostHog but nothing rendered it. `--apply` provisions **Health — SDUI
selector-evidence captures per day** (`INSIGHT_SDUI_SELECTOR_EVIDENCE`), a plain single-series
trend counting `sdui_selector_evidence` events, so a locator quietly going blind between Monday
sweeps shows up as a moving line before the next drift issue would even file. It is deliberately
**not** wired to a threshold alert: a nonzero day is an expected reading, not a breach — a short
comment thread legitimately has no sort control at all (`docs/sdui-probe-coverage.md`) — so a
threshold here would page on noise. The weekly sweep's own `drift` filing (`sdui_drift_issues.py`)
stays the one signal that actually pages.

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
`POSTHOG_ANNOTATION_API_KEY` (scope: `annotation` read+write), falling back to
`POSTHOG_PERSONAL_API_KEY` — distinct from the env var of the same
name used locally by the provisioning scripts. Absent, the script prints why and exits 0; the step
also runs with `continue-on-error: true`, so a PostHog outage can never fail a release that already
shipped.

## Purpose-scoped personal keys (issue #1453)

One account-scoped `POSTHOG_PERSONAL_API_KEY` used to do three unrelated jobs, so its scope was the
union of all of them. `src/cqc_lem/utilities/posthog_keys.py` is the ONE place that resolution now
lives: each purpose reads its own env var and falls back to `POSTHOG_PERSONAL_API_KEY`.

| Purpose | Env var | Consumers | Scope |
|---|---|---|---|
| `annotation` | `POSTHOG_ANNOTATION_API_KEY` | `scripts/posthog_annotate.py` (GH Actions) | `annotation:write` |
| `runtime` | `POSTHOG_RUNTIME_API_KEY` | `flags.py`, `posthog_endpoints.py`, `observability.posthog_hogql_query` (app containers) | `feature_flag:read`, `query:read` |
| `query` | `POSTHOG_QUERY_API_KEY` | `scripts/posthog_error_issues.py` via `error_to_issues.sh` (host cron) | `query:read` |
| `benchmark` | `POSTHOG_BENCHMARK_API_KEY` | `scripts/benchmark_models.py` via `weekly_model_check.sh` (host cron, Sun) | `evaluation:read`, `evaluation:write`, `query:read` |
| `operator` | `POSTHOG_OPERATOR_API_KEY` | `posthog_provision`, `posthog_dashboards`, `posthog_flags`, `posthog_surveys`, `posthog_experiments`, `posthog_ops_destination`, `slop_retry_clear_rate` (all hand-run) | insight/dashboard/survey/experiment/flag/hog_function read+write, `query:read` |

The provisioning scripts (`posthog_provision`, `posthog_dashboards`, `posthog_flags`,
`posthog_surveys`, `posthog_experiments`, `posthog_ops_destination`, `slop_retry_clear_rate`) are
run by hand, never stored in an environment the app or a cron owns. They share ONE `operator`
purpose rather than five separate keys nobody would provision — an operator already holds account
access to every scope those six write scopes name, so splitting them buys no extra containment.
`POSTHOG_OPERATOR_API_KEY` is exported into a shell for the run and stored nowhere; revoking the
shared key leaves these scripts a NAMED var to export instead of a silent break (issue #1453,
2026-08-22 follow-up).

**Why the benchmark is a purpose and not an operator key.** `scripts/benchmark_models.py` is not
hand-run: `scripts/weekly_model_check.sh` (host cron, Sun) sources its key out of `/opt/lem/.env`,
so that key is a *stored* credential like the other three. It reads PostHog's LLM-evaluation API —
a scope none of the others covers — so folding it into `runtime` would widen the one key that lives
in the app containers. It needs `query:read` **as well**: the run creates and triggers the judge
evaluations over the evaluation API but reads the verdicts back over HogQL
(`PostHogEvals.query` / `hogql_for_run`), so an evaluation-only key scores nothing — which is why
the preflight checks that half separately. It gets `POSTHOG_BENCHMARK_API_KEY` instead (issue #1453, owner decision
`1A`). It still degrades to the in-runner judge without a key, but no longer silently: `main()`
prints `Neither POSTHOG_BENCHMARK_API_KEY nor POSTHOG_PERSONAL_API_KEY is set — PostHog evaluations
are unavailable; using the in-runner judge` to stderr, which lands in `/home/lem/model-check/model_check.log`.

**The fallback was the rollout, and the rollout is DONE.** Nothing changed in an environment until
a scoped key existed there, so the five keys were created alongside the shared one, populated one
consumer at a time, verified, and the shared `POSTHOG_PERSONAL_API_KEY` was **revoked on
2026-08-31**. The fallback branch stays in `posthog_keys.py` because it costs nothing, but it is
dead everywhere LEM runs: a `via POSTHOG_PERSONAL_API_KEY` line in the preflight now means an
unpopulated consumer holding a revoked credential, which answers 401. Nothing on the box or in CI
should source that var any more.

**Verify per surface, because they fail silently.** A wrong key in `flags.py` just makes every flag
read its env default; in `posthog_endpoints.py` the SPA stats panel goes empty; in the error cron
nothing gets filed and absence looks exactly like a quiet day; in the benchmark the scorecard still
renders, scored by the fallback judge. A green deploy is not evidence.

`scripts/posthog_key_check.py` is that verification as one command. It resolves each purpose's key
exactly as its consumer does, performs ONE **read-only** request against that consumer's surface,
and prints PASS/FAIL naming the env var that supplied the key — which is how you tell a populated
scoped key from the shared fallback still doing the work:

```bash
python scripts/posthog_key_check.py --list              # what it will check; no network
python scripts/posthog_key_check.py                     # every purpose
python scripts/posthog_key_check.py --purpose runtime   # one consumer, after populating it
```

Exit 0 only when every checked surface passed; 1 if any failed (including "no key at all"), 2 on an
unknown `--purpose`. It writes nothing to PostHog — every request is a GET, or a POST to `/query/`
or an endpoint `/run/`, and `tests/unit/scripts/test_posthog_key_check.py` fails the build if a
surface is ever added that could write. Run it after each consumer is populated and again
immediately before revoking the shared key.

**`benchmark` "LLM-evaluation API" was a stale path, not a missing scope.** For eleven days this
surface returned `HTTP 404` and was recorded here as a permanent ceiling — first against the shared
key (2026-08-20), then again against a `POSTHOG_BENCHMARK_API_KEY` carrying `llm_playground:read` /
`llm_prompt:read` / `llm_skill:read` (2026-08-22). Two identical 404s across two different keys is
what made "no scope will fix this" look proven; what it actually proved is that scope was never the
variable. **A 404 is the path answering, not the key** — a scope gap answers `403`. Checking
PostHog's published OpenAPI schema (2026-08-31) settled it in one read: the collection moved off the
`llm_analytics/` prefix, and the per-evaluation `/run/` action became a collection of its own.

| Call | Old path (404) | Current path |
|---|---|---|
| list / create evaluations | `/llm_analytics/evaluations/` | `/evaluations/` |
| trigger one judge run | `POST /llm_analytics/evaluations/{id}/run/` | `POST /evaluation_runs/` |

The create-then-trigger-per-event flow `benchmark_models.PostHogEvals` is built around is intact —
only the addresses changed. The trigger body changed shape with the move: `{"event_id": ...}` became
`{"evaluation_id", "target_event_id", "timestamp", "event", "distinct_id"}`, and **`timestamp` is
required** because the run is enqueued as an async workflow that has to locate the target event in
ClickHouse. It must be the emitted event's OWN timestamp, so `emit_generation` stamps the capture
explicitly and carries that value forward rather than letting either side take its own clock
reading. The scopes are `evaluation:read` (list) and `evaluation:write` (create + run) — real
scopes of their own, which the `llm_playground` / `llm_prompt` / `llm_skill` trio does not cover.

**The lesson worth keeping is the diagnosis, not the paths.** A read-only preflight that reports
PASS/FAIL per surface still cannot distinguish "credential lacks scope" from "we are asking the
wrong URL", and the write-up above chose the first reading twice and hardened it into documentation.
When a surface FAILs, read the HTTP status before reaching for scopes: `401` is the key, `403` is
the scope, `404` is the path.


The manual equivalents, if you want to check a surface by hand: `flags.local_evaluation_available()`,
`GET /user/posthog-stats` returning populated panels, `scripts/posthog_error_issues.py --dry-run`
returning rows over a window known to contain exceptions, and a weekly benchmark report whose cases
carry `posthog-evals` rather than `in-runner-judge` scoring.
