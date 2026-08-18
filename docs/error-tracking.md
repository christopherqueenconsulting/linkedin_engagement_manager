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

### The test suite never reaches the project (#1451, #1460, #1498)

A key arrives through the process ENVIRONMENT as readily as through a file — `lem-agentd` loads
`agent-pipeline/secrets.env` as a systemd `EnvironmentFile` for the pipeline's own telemetry, and
every pytest it spawns inherits it — so "no key in CI" is not the guard. `logger._running_under_pytest()`
is, and **all three** hops off that key read it:

| Hop | Refuses where |
|---|---|
| The PostHog SDK (`$exception`, every `track_*` event) | `observability.py` sets `posthog.disabled` at import; `tests/conftest.py` sets it again |
| The OTLP exporter into PostHog Logs | `logger._build_posthog_handler` returns None, so no handler is ever attached |
| The uncaught-exception excepthook | `observability._exception_autocapture_enabled()` reads `posthog.disabled`, so autocapture is never armed (#1498) |

The first two leaks were measured, not theorised: a mocked cursor raising `mysql.connector.Error("db down")`
filed a GitHub issue against production code that was working, and the log hop put fixture ERRORs
("SMTP send failed for test@example.com") into the same Logs stream prod writes real warnings to.
Once test data is ingested it is indistinguishable from production data, which is why this refuses
rather than filters.

### An operator CLI never reaches the project either (#1661)

Same key, same argument, a different non-production caller. The `scripts/` corpus samplers
(`sample_newsletter_scaffolds.py`, `sample_newsletter_similarity.py`, `sample_shipped_videos.py`,
`measure_proof_gate_impact.py`) each document that they must be run where a database is reachable,
which an agent worktree is not — and `lem-agentd` supplies the run a real `POSTHOG_API_KEY` but no
MySQL credentials. So one sampler run on the host had `get_active_user_ids()` catch
`ProgrammingError: 1045 … Access denied for user 'lem_user'`, publish it as a grouped `$exception`,
and the cron below file it as a GitHub issue against production code that was working.

The guard is `logger.telemetry_muted()`, read off `LEM_TELEMETRY_MUTED`, and each of those CLIs sets
it on ITSELF (`os.environ.setdefault`) beside its `sys.path` bootstrap — before `cqc_lem` is
imported, because the Logs handler is built at import. It covers every hop off the key, `_emit`
included, so a muted run ingests no ANALYTICS event either — the pytest sibling gets that from
`posthog.disabled`, and a sampler that DOES reach a database would otherwise still write `llm_call`
rows into the production project under a real user. Nothing authoritative is lost: the priced cost
ledger is the proxy's `$ai_generation`, which the process cannot mute. It is deliberately **not** inferred from
"is this a script?": `posthog_annotate.py` and `benchmark_models.py` also live in `scripts/` and
their telemetry is the point. For the same reason `capture_exception` reads it per CALL instead of
setting the process-wide `posthog.disabled`, which would silence the tooling that drives `posthog`
directly with its own key.

Set `LEM_TELEMETRY_MUTED=0` to opt a run back in, and add the same `setdefault` line to any new
operator CLI that reads production data — `test_operator_cli_telemetry_mute.py` fails the build on
one that does not, because that sentence used to be prose nothing checked. It DISCOVERS every
`scripts/*.py` crossing the DB facade and requires it in exactly one of two lists: muted, or
allowlisted with the reason its telemetry is wanted. The four allowlisted today all run INSIDE a
production container or as a production cron (`linkedin_live_validation.py`,
`linkedin_post_stats_api_probe.py`, `linkedin_version_check.py`, `reseed_own_post_comments.py`), so
their `$exception` IS a production signal — which is why the list stores the reason and not just the
name. **The failure being muted here is the CLI's environment, never the app's:** the `log_error`
inside `get_active_user_ids` stays an error, because in a Celery or API process a database that
refuses the credentials means automation silently does nothing.

### A group whose last occurrence predates the guard is history, not a live defect (#1673)

The three guards above stop new leakage; they do not retract what was already ingested. A PostHog
error-tracking group keeps its `active` status until someone resolves it, so every group the suite
filled before 2026-08-14 still shows up on the error list — and gets re-filed by hand as a
production bug against code that was working. #1673 was exactly that: `OSError: broker unreachable`,
91 occurrences, read as a Celery broker outage.

Three tells, checkable from the group itself before any code is read:

| Tell | What to look at |
|---|---|
| The message is a FIXTURE string | `grep -rn "<the exception message>" tests/` finds it as a `side_effect`, and `src/` never raises it. `broker unreachable`, `broker down`, `boom`, `db down`, `fail` are mocks, not products |
| The actor is a TEST user | `distinct_id` on the sampled events is `42` — `SESSION_USER_ID` in `tests/unit/api/conftest.py`. Production is one real user, and it is not 42 |
| It stopped when the guard landed | `last_seen` is at or before **2026-08-14T02:44Z** — #1451 merged at 02:37Z and nothing of this shape has been ingested since |

All three held on #1673: the string exists only as `chain.return_value.apply_async.side_effect =
OSError("broker unreachable")` in `tests/unit/api/test_content_generation_status_api.py`, which
drives the real `except` in `create_weekly_content` into `log_error(exc=...)`. There was no broker
incident to find.

**Resolve the group in PostHog** — that is the fix, and the only one. `posthog_error_issues.py`
already skips a resolved issue ("triage done in PostHog stays done"), so resolving is what stops a
dead group costing triage time again; a code change would be inventing behaviour for a failure that
never happened.

The residue is large: measured 2026-08-18, **200+ groups are still `active` with no occurrence since
the guard landed**, the biggest being `APIConnectionError: Connection error.` at 6,146 occurrences
sourced literally at `tests/unit/conftest.py`. The daily cron cannot re-file any of them (it counts
occurrences inside a 24h window), so they cost nothing automatically — they cost a human reading the
error list. Resolve them in bulk from the PostHog UI rather than one GitHub issue at a time.

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
  makes recursion structurally impossible, and `BUILTIN_EXCLUDED_PREFIXES` carries that message as a
  second layer. `LOG_ESCALATE_EXCLUDE` **adds** to those built-ins rather than replacing them — an
  env override that silently dropped the loop guard would be the worst kind of configuration bug.

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

**An ALERTER is the state-setter rule read one step further: reporting a bad measurement is not
failing.** `cost_alerts.send_cost_alerts` logs one line per threshold breach, and a breach is exactly
what the daily beat exists to surface — it already ships as a `cost_alert` PostHog event and an owner
email. But a cost profile stays over its ceiling for as many days as it takes someone to change it,
so `Cost alert [user_cost_ceiling]: User #1 variable cost is …% of tier MRR` recurred on schedule,
crossed the threshold and filed a code defect against tooling that was working (issue #1071). Nothing
in that issue is fixable in code. The digest line's prefix (`cost_alerts.ALERT_LOG_PREFIX`) is
therefore pinned in `BUILTIN_EXCLUDED_PREFIXES`; the level stays WARNING, so prod's
`POSTHOG_LOG_LEVEL=WARNING` still keeps it in Logs. Note the split: **failing to DELIVER an alert**
(`Cost alert email failed`, `Cost alert PostHog capture failed`) is a real fault and still escalates.
The test for this shape is whether recurrence carries new information — for a selector miss it does,
for a measurement it does not.

**A release that interrupts an in-flight task is the same shape one level up: an uncaught exception.**
The deploy drains the workers for `DEFAULT_DRAIN_TIMEOUT_SECONDS` (8 min) and then recreates the
containers regardless, so a long Selenium task still holding a browser has its session quit out from
under it — and the next WebDriver call raised `InvalidSessionIdException`, crashed the task, and the
`task_failure` signal filed it as a defect for a routine release (issue #988). `selenium_util.
is_session_lost(exc)` is the ONE place that fault is recognised; a task that hits it ends on what
already shipped and logs INFO. A **best-effort catch site on the way there has to recognise it too**:
the roster walk warns per target when an activity page won't open, so a dead session warned once per
remaining target and crossed the escalation threshold — filing the same release as a defect through
the other door. It stops the walk at INFO instead. Deliberately NOT covered by `is_session_lost`: a
hub that refuses connections — an unreachable Grid is a different fault and must stay loud.

| Env | Default | Purpose |
|---|---|---|
| `LOG_ESCALATION_ENABLED` | `true` | master switch; false → zero Redis calls |
| `LOG_ESCALATE_THRESHOLD` | `3` | occurrences in the window that promote to ERROR |
| `LOG_ESCALATE_WINDOW_SECONDS` | `86400` | tumbling window; matches the cron lookback |
| `LOG_ESCALATE_REPEAT_EVERY` | `10` | re-escalate every Nth past the threshold (0 = once) |
| `LOG_ESCALATE_MAX_PER_WINDOW` | `50` | ceiling across all fingerprints |
| `LOG_ESCALATE_EXCLUDE` | *(empty)* | comma-separated never-escalate prefixes, ADDED to `BUILTIN_EXCLUDED_PREFIXES` |
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
with no GitHub issue carrying its marker **and no open issue already tracking the same warning
string**, it files one `agent:ready` + `bug` issue shaped for the pipeline's `MODE=start` (Why /
Scope / Files / Acceptance), with a link to the PostHog issue for the stack trace.

- **Browser exceptions link their replay** (issue #649): the query also reads `$session_id`, so a
  filed issue for an SPA error carries a "Watch the session replay" link. Backend exceptions have no
  session and simply omit the line. See `docs/session-replay.md`.
- **Dedup is the id**, not the message: the body carries `posthog-issue-<issue_id>`, and the next
  run searches for that literal string across open AND closed issues — in bodies **and comments**.
  Closed counts — a fixed exception that trickles in for one more day must not reopen the backlog
  item.
- **Second layer, for the trackers this script did not write** (issue #1083): the marker is invisible
  to a human who filed an issue for the same defect first, so an ESCALATED warning also dedups on its
  text. `RecurringWarning` is the only exception type with a usable one — `log_escalation` masks the
  volatile tokens before capture, so its description is the stable template a person quotes verbatim.
  When that string appears in an OPEN issue's **title or body**, the occurrence data is COMMENTED
  there and nothing is opened; the comment carries the marker, so from the next run the id layer
  skips the row and the comment never repeats.
  - **The comment IS the record — do not delete it.** It is the only durable trace that this
    PostHog id was handled (the script keeps no state of its own), and the text match does not go
    away when the comment does: the matched issue still carries the warning string, so the next run
    matches it again and re-posts. A wrong match is corrected by opening a separate issue for the
    distinct defect, never by deleting the comment.
  - **Conservative by construction**: escalated warnings only, ≥16 chars and ≥3 words, exact
    (casefolded) substring, open issues only, lowest-numbered match wins. *A false merge hides a
    distinct defect; a false miss only files the duplicate we file today.* Comments are deliberately
    NOT searched — a warning quoted in a comment usually belongs to a different issue's problem — and
    a CLOSED tracker never matches, because "declared fixed" makes a recurrence news.
  - Measured cost of not having it: **#1063** duplicated hand-filed **#818** (same warning, same
    task) and spawned PR #1066 against work already parked; the **#874/#875/#877/#878** cluster filed
    four issues against the one outage **#816** tracked.
- **Fail closed**: if the GitHub search itself fails, the run aborts rather than treating "cannot
  read" as "nothing filed" and duplicating the whole window.
- **Resolved/suppressed PostHog issues are never filed** — triage done in PostHog stays done.
- `--max-new` (default 10) caps a bad deploy at 10 tickets per run; the rest wait for the next one.
  It caps NEW issues only — a comment on an existing tracker adds nothing to the backlog, and each
  one happens once per PostHog issue id anyway.

```bash
scripts/posthog_error_issues.py --print-sql          # the HogQL, no network
scripts/posthog_error_issues.py                      # dry run (exit 2 = issues pending)
scripts/posthog_error_issues.py --apply --hours 24   # file them
```

Needs `POSTHOG_QUERY_API_KEY` (scope `query:read`), falling back to `POSTHOG_PERSONAL_API_KEY` when
unset (issue #1453, `docs/kpi-dashboards.md`) — the wrapper reads either from `/opt/lem/.env` —
and an authenticated `gh` CLI. A key this lane can't use fails SILENTLY: no issues are filed, and
absence looks exactly like a quiet day, so verify it against a window known to contain exceptions.

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
