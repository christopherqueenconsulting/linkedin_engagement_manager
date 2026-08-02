# Error tracking — `$exception` → PostHog issues → GitHub issues

Issue #648 (PH3). Before this, an error reached PostHog only as a LOG line, and a nightly cron
grepped those logs, grouped them by the exact message string and hashed it into a dedup marker. Any
error whose message carried an id ("Post 41 failed to post") therefore filed a brand-new GitHub
issue every single day, and nothing grouped, counted or alerted.

PostHog Error Tracking already does the hard part: it groups `$exception` events into **issues** by
fingerprint. The issue IS the dedup key, so the whole homegrown layer collapses into "one PostHog
issue id ↔ one GitHub issue".

## What emits `$exception`

| Surface | How |
|---|---|
| Any Python process | `posthog.enable_exception_autocapture` — set in `utilities/observability.py`, catches UNCAUGHT exceptions via the excepthook |
| Anything logged with `exc=` | `log_error` / `log_critical` also call `capture_exception` (`utilities/logger.py`) |
| A **repeated** `log_warning` | `utilities/log_escalation.py` — see [Recurrence escalation](#recurrence-escalation-once-is-a-warning-repeatedly-is-a-defect) |
| Celery tasks | `task_failure` + `task_retry` handlers in `app/my_celery.py`, with `task_name`, `task_id` and the dispatched `user_id` |
| FastAPI routes | the unhandled-exception branch of `observability_middleware` in `api/main.py`, with `route` + `method` |
| The SPA | `posthog-js` `capture_exceptions` (unhandled errors + rejections), plus `captureException()` from `ui/src/utils/analytics.ts` for anything the app catches itself |

Two rules the surfaces above encode:

- **A 4xx is not an error.** A route's own `HTTPException` is turned into a response before the
  middleware sees it, so only genuine 500s file an issue.
- **`console.error` is not an error either.** It is noisy and routinely carries the user's own
  content in the message, so browser console capture stays off.

Double counting is not a concern: `posthog.capture_exception` is idempotent per exception
*instance*, so a task that does `log_error(..., exc=e)` and then re-raises produces ONE occurrence,
not one per layer that saw it.

Kill switches (both default ON, both read at import):

| Env | Effect |
|---|---|
| `POSTHOG_EXCEPTION_AUTOCAPTURE=false` | no excepthook capture |
| `POSTHOG_EXCEPTION_CAPTURE=false` | `log_error`/`log_critical` stop forwarding |

With no `POSTHOG_API_KEY` at all, `posthog.disabled` is already True and nothing is sent.

## Recurrence escalation: once is a warning, repeatedly is a defect

`log_warning` never called `_capture()` — only `log_error`/`log_critical` did, and only with `exc=`.
So a warning could not become an `$exception`, could not become an Error Tracking issue, and could
not become a GitHub issue. A LinkedIn selector that had been missing for three straight days and a
one-off network blip were, to PostHog, the same thing: a log line nobody was paged about.

The state that produced this design, measured 2026-07-31 over 48h: **2,185 `info` + 172 `warn` +
zero `error`** rows in PostHog Logs, and **zero `$exception` events**. The daily error→issue cron ran
every day and correctly filed nothing, because its input was empty. Meanwhile
`Selector miss: Open reactions menu` had fired 30 times, `Inline comment post failed` 26 times, and
`PostHog endpoint call failed` 27 times — all invisible.

`utilities/log_escalation.py` counts occurrences and promotes the repeat offenders.

**The dedup key.** The logger only ever sees the *interpolated* string, so volatile tokens are masked
before hashing — URLs, emails, UUIDs, URNs, hex ids, `[...]` lists, quoted strings, bare numbers —
then combined with the **call site** (`module.function`, never the line number: a line moving during
an unrelated edit must not reset the counter and fork a new issue). The masking is tuned so that
`Re-queueing orphaned connection request 41` and `... 42` are ONE problem, while
`Selector miss: Feed sort control` and `Selector miss: Reaction state` stay TWO — they are two
different broken selectors and collapsing them would hide one behind the other.

**The counter.** Redis (`shared_redis_client()`, the same handle the 429 breaker uses), one
non-transactional pipeline per warning. The window is **tumbling, not sliding** — `INCR` + `EXPIRE`
is one round trip with self-expiring memory, where a true sliding window would need a sorted set and
unbounded per-fingerprint storage. The cost is that a fault straddling a window boundary defers one
window; that is an acceptable trade for a logging hot path.

**Why 3-in-24h.** `LOG_ESCALATE_WINDOW_SECONDS` defaults to 24h to match the cron's own 24h lookback,
so an escalation always lands inside the next cron run. It is not arbitrary: `Selector miss: Feed
sort control` fires ~5×/day, so a 1-hour window at threshold 3 would *never* trip on exactly the
class of slow-burn breakage this exists to catch. `LOG_ESCALATE_REPEAT_EVERY` (10) re-escalates
periodically past the threshold so the issue's occurrence count tracks real magnitude — escalating
exactly once per window would make every issue read "1x" regardless of whether it happened 3 times
or 300.

**Grouping is pinned explicitly.** PostHog's default fingerprint is exception type + first in-app
stack frame. Every `Selector miss: *` is raised from the same line of `find_first`, so the default
would collapse all of them into ONE issue. `capture_exception(..., fingerprint=...)` sets
`$exception_fingerprint` to `lem-log:<hash>`, which is what keeps distinct breakages distinct and
what makes grouping survive a refactor of `find_first`'s internals.

**The exception object.** When the call site passed `exc=`, that real exception is reused — a genuine
stack groups and debugs better. Otherwise a `RecurringWarning` is constructed (never raised) whose
message is the masked text, so the PostHog issue title reads as the problem itself.

**It fails open at every step.** No Redis, `LOG_ESCALATION_ENABLED=false`, an excluded prefix, or any
internal error → `note()` returns `None` and `log_warning` behaves exactly as it did before. Two
extra guards matter in production:

- **A Redis-outage circuit.** `redis.Redis.from_url` does not connect eagerly, so a dead Redis would
  otherwise cost the 2s `socket_connect_timeout` on *every* warning across ~400 call sites. After
  `LOG_ESCALATE_REDIS_FAILURES` consecutive failures the process stops calling Redis until
  `LOG_ESCALATE_REDIS_COOLDOWN_SECONDS` passes.
- **Re-entrancy.** `capture_exception`'s own failure handler calls `log_warning`. A thread-local flag
  makes recursion structurally impossible, and `LOG_ESCALATE_EXCLUDE` defaults to that message as a
  second layer.

**What this means when you write a call site.** A warning you emit on a benign, expected path will
now file a defect. Log those at DEBUG instead. The pattern is `react_to_post_inline`, which used to
return `False` both for "already reacted" (a no-op) and for genuine failure, so the caller reported a
working skip as `Could not leave a reaction on post`; it now returns `None` for the no-op — still
falsy, so truthiness callers are unaffected — and only real failures warn.

The second half of that pattern is **warn where you detect, not where you notice**. Restating a
failure one frame up files a second defect for one fault, so that caller now logs the outcome at
DEBUG and each failing path inside `react_to_post_inline` owns its single warning (issue #878).

The same rule reads sideways for a **state-setter**: succeeding at what you were asked to do is never
a degraded path, however serious the state is. `pause_automation` warned whenever it stored the global
Selenium kill-switch, and maintenance mode sets one on every release — 4x daily — so a routine deploy
escalated to ERROR and filed `RecurringWarning: Automation PAUSED for 1800s (reason: deploy)`
(issue #917). It logs INFO now; the callers for which a pause IS the defect already say so where they
detect it (the suppression tripwire escalates CRITICAL, the 429 breaker warns in `mark_rate_limited`),
and only failing to store the pause — a kill-switch that didn't take — still warns.

| Env | Default | Purpose |
|---|---|---|
| `LOG_ESCALATION_ENABLED` | `true` | master switch; false → zero Redis calls |
| `LOG_ESCALATE_THRESHOLD` | `3` | occurrences in the window that promote to ERROR |
| `LOG_ESCALATE_WINDOW_SECONDS` | `86400` | tumbling window; matches the cron lookback |
| `LOG_ESCALATE_REPEAT_EVERY` | `10` | re-escalate every Nth past the threshold (0 = once) |
| `LOG_ESCALATE_MAX_PER_WINDOW` | `50` | ceiling across all fingerprints |
| `LOG_ESCALATE_EXCLUDE` | capture-failure message | comma-separated never-escalate prefixes |
| `LOG_ESCALATE_LOCAL_CAP` | `200` | per-process per-fingerprint cap on Redis calls |
| `LOG_ESCALATE_REDIS_FAILURES` / `_COOLDOWN_SECONDS` | `3` / `300` | the outage circuit |

## Readable stack traces (source maps)

The SPA ships minified, so a browser stack is unreadable without source maps. `vite.config.ts` emits
them (`build.sourcemap: true`) and the **image build** (`compose/local/Dockerfile`, ui-builder stage)
runs `posthog-cli sourcemap inject` + `upload` right after `npm run build`.

It has to happen there, not in a separate CI job: `inject` writes chunk ids INTO the bundle, and the
injected bundle is the one that must ship, or an uploaded map can never be matched to a stack.

Every `.map` is deleted from `dist/` afterwards, so source maps are never served — including on
builds that skip the upload entirely. The upload is best-effort: a missing token or a CLI failure
costs readable stacks, never a release.

CI wiring (`.github/workflows/build-and-push.yml`):

| Name | Kind | Value |
|---|---|---|
| `POSTHOG_CLI_TOKEN` | repo **secret** (BuildKit secret, never an ARG) | personal API key with `error_tracking:write` |
| `POSTHOG_PROJECT_ID` | repo variable → `POSTHOG_CLI_ENV_ID` | `475262` |
| `POSTHOG_APP_HOST` | repo variable → `POSTHOG_CLI_HOST` | `https://us.posthog.com` |

## error → GitHub issues (`scripts/posthog_error_issues.py`)

Daily host cron, run by `scripts/error_to_issues.sh` (as `lem`), replacing
`~/error-to-issues/scan.sh`:

```
30 8 * * * /home/lem/linkedin_engagement_manager/scripts/error_to_issues.sh
```

One HogQL query reads the error-tracking columns the `events` table exposes (`issue_id`,
`issue_name`, `issue_status`, `issue_first_seen`) and groups by `issue_id`. For each ACTIVE issue
with no GitHub issue carrying its marker, it files one `agent:ready` + `bug` issue shaped for the
pipeline's `MODE=start` (Why / Scope / Files / Acceptance), with a link to the PostHog issue for the
stack trace.

- **Browser exceptions link their replay** (issue #649): the query also reads `$session_id`, so a
  filed issue for an SPA error carries a "Watch the session replay" link. Backend exceptions have no
  session and simply omit the line. See `docs/session-replay.md`.
- **Dedup is the id**, not the message: the body carries `posthog-issue-<issue_id>`, and the next
  run searches for that literal string across open AND closed issues. Closed counts — a fixed
  exception that trickles in for one more day must not reopen the backlog item.
- **Fail closed**: if the GitHub search itself fails, the run aborts rather than treating "cannot
  read" as "nothing filed" and duplicating the whole window.
- **Resolved/suppressed PostHog issues are never filed** — triage done in PostHog stays done.
- `--max-new` (default 10) caps a bad deploy at 10 tickets per run; the rest wait for the next one.

```bash
scripts/posthog_error_issues.py --print-sql          # the HogQL, no network
scripts/posthog_error_issues.py                      # dry run (exit 2 = issues pending)
scripts/posthog_error_issues.py --apply --hours 24   # file them
```

Needs `POSTHOG_PERSONAL_API_KEY` (scope `query:read`) — the wrapper reads it from `/opt/lem/.env` —
and an authenticated `gh` CLI.

## Alerts (PostHog UI)

Alerting is configuration, not code — it lives in
[error tracking → configuration → Alerting](https://us.posthog.com/project/475262/error_tracking/configuration#selectedSetting=error-tracking-alerting):

1. **Issue created or reopened** → destination email (`christopher.queen@gmail.com`). This is the
   real-time counterpart of the daily cron: the cron files the work item, the alert tells a human.
2. **Spike detection** → same destination, configured under
   `#selectedSetting=error-tracking-spike-detection`. A spike on an ALREADY-filed issue is
   deliberately not a second GitHub issue; it is an alert.

The org also gets a weekly Error Tracking digest email per project, which needs no setup.

## Reading the data

- Issues, statuses and stack traces: `https://us.posthog.com/project/475262/error_tracking`
- The raw events: `SELECT issue_id, issue_name, count() FROM events WHERE event = '$exception'
  GROUP BY issue_id, issue_name` in SQL insights.
- Logs (`utilities/logger.py` → PostHog Logs) are still there and still carry the message and
  structured context — they are the CONTEXT around an exception. Alerting moved to issues; logs did
  not go away.
