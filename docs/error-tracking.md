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
