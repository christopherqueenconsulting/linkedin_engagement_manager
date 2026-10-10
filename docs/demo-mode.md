# Demo mode

`DEMO_MODE` exists so a screen recording or a demo stack can never reach LinkedIn (#2372).

> **Never enable this on the production stack.** It stops posting and every engagement lane for
> **every** user, not just the one being recorded. A demo stack also needs **its own MySQL and its
> own Redis**. Refusals write failure state: posts are left at `scheduled` and task results read
> FAILURE. A demo worker on production's Redis would also take production's queued tasks and
> refuse them.

## Turning it on

The flag is off by default. `true`, `1`, `t`, `y` and `yes` turn it on, in any case. Anything else,
or leaving it unset, keeps it off. It is a **safety control, not a feature flag**
(`docs/feature-flags.md`): it is never resolved through PostHog, and only the environment turns it
off.

1. In the **demo** stack's `.env`, set `DEMO_MODE=true`. `.env.example` lists it, blank (off).
2. Recreate every service that runs LinkedIn code. Every app service reads the same `env_file:
   .env`, so the one line reaches all of them. The services that need it are:
   - both API colours, `web_api_blue` and `web_api_green` (in the dev-only single-file stack,
     `web_app` is the API);
   - every Celery worker: `celery_worker`, `celery_worker_selenium`,
     `celery_worker_selenium_prepost`, `celery_worker_selenium_outreach`,
     `celery_worker_selenium_content`;
   - `celery_beat`.
3. From the demo stack's compose directory, run the same compose files the stack was started
   with. For the production layout:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --pull never \
     --force-recreate web_api_blue web_api_green celery_worker celery_worker_selenium \
     celery_worker_selenium_prepost celery_worker_selenium_outreach \
     celery_worker_selenium_content celery_beat
   ```

   `docker compose restart` is not enough, because it keeps the old environment. The code reads
   `DEMO_MODE` on every call, so a process sees a new value on its next call. A container, though,
   only gets a new value when it is **recreated**.

## Verifying it is on

Check each service. The badge proves nothing about the workers:

```bash
for s in web_api_blue web_api_green celery_worker celery_worker_selenium \
         celery_worker_selenium_prepost celery_worker_selenium_outreach \
         celery_worker_selenium_content celery_beat; do
  printf '%-34s ' "$s"
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T "$s" printenv DEMO_MODE \
    || echo '<unset>'
done
```

Every line must print `true`. Then `GET /api/app-info` must answer `"demo_mode": true`.

That field, and the "Demo data" badge drawn from it, reflect **only the API container that answered
the request**. A worker recreated without the variable would still reach LinkedIn while the badge
shows. The per-service check above is the real proof.

## Turning it off (rollback)

1. Blank or remove `DEMO_MODE` in the demo stack's `.env`.
2. Run the same recreate command.
3. Re-run the per-service check: every line must be empty or `<unset>`. Confirm
   `GET /api/app-info` answers `"demo_mode": false`.

What was refused while it was on. The scheduler runs five orphan reapers, and none of them has a
limit on how old a row may be. Here is what each one finds after a refusal:

- **Posts (`get_orphaned_scheduled_posts`).** A refused `post_to_linkedin` leaves the post at
  `scheduled`, and the reaper re-queues it on every 10-minute run once it is two hours past its
  slot, so **the first run after turn-off publishes it, however old**. Move such posts out of
  `scheduled` before turning demo mode off. Posts are not held automatically, because `pending`
  can be auto-approved again by the content re-score.
- **Scheduled DMs (`get_orphaned_scheduled_dms`).** A refused `send_scheduled_dm` holds the DM at
  `pending`. The reaper only reads `scheduled`, so it never re-queues it; the DM needs a human to
  approve it again.
- **Connection requests (`get_orphaned_connection_requests`).** A refused
  `send_connection_request` holds the request at `pending` without charging an attempt. The reaper
  only reads `sending`, so it never re-queues it.
- **Catch-up touches (`get_orphaned_catchup_touches`).** A refused `send_catchup_touch` gives back
  its send claim, holds the touch at `pending` and reports `demo_held`. The reaper only reads
  `sending`, so it never re-queues it.
- **Occasion posts (`get_orphaned_occasion_claims`).** A refused `auto_publish_occasion_post` holds
  its claim at `error` instead of releasing it to `approved`. That reaper never re-queues anything
  either: it moves an abandoned claim to `error` for the author to resolve. A real publish failure
  also lands at `error`. To tell them apart, look for the INFO line
  `Occasion post <id> held: demo mode`: it is written only for a demo hold.

**Re-approving a held row.** A DM, connection request or catch-up touch held at `pending` goes back
out only when someone approves it again:

- **In the SPA:** Content Studio (`/content`), on the **DMs** (`?tab=dms`), **Connections**
  (`?tab=connections`) or **Catch-up** (`?tab=catchup`) tab, where the row shows as pending.
- **Through the API:** the approve action on `PUT /api/dm`, `PUT /api/connection_request` or
  `PUT /api/catchup/touch`.

An agent session can queue, but it can never approve.

Beyond the reapers, the rule is that anything still approved and due keeps its schedule.
Follow-up DMs, newsletter editions, group posts and hot-lead replies that come due while demo mode
is on are refused, and can still go out on their normal schedule once it is off. Beat-driven lanes
that hold no queued row (feed commenting, reply sweeps and the like) simply run again on their
next tick. This is why a demo stack must use its own database.

## What it blocks

When it is on, every LinkedIn client raises `DemoModeError` (`utilities/demo_mode.py`) before any
connection opens. Each transport has one choke point:

| Transport | Choke point | Covers |
|---|---|---|
| HTTP | `install_requests_guard` wraps `requests.Session.send`. The package root installs it, so every process that imports `cqc_lem` has it | every `requests` call to `linkedin.com`, `licdn.com` or `lnkd.in`, including the `linkedin_api` SDK's `RestliClient` / `AuthClient`. The host is matched on the parsed name, never a substring, and is read by both the stdlib and urllib3: if either sees LinkedIn, the call is refused |
| Selenium | `selenium_util.get_docker_driver`, before the Grid is contacted | every lane, because all of them open their browser there (`get_driver_wait_pair` included) |
| urllib (scripts) | `_guard_linkedin()` before every `urlopen` in `scripts/linkedin_version_check.py` and `scripts/linkedin_post_stats_api_probe.py` | both print a `skipped` JSON result and exit **3** before reading a token. The weekly version-check wrapper reads `skipped` as "nothing learned": no bump, no `.env` write, no recreate, no alert |
| Debug browser | `tools/selenium_mcp_server.start_browser` | refuses to open the watchable debug-node browser |
| Raw socket | `egress_probe.probe_proxy` | answers `unknown` without opening a socket |

The publish and comment helpers in `linkedin/poster.py` and `linkedin/reshare.py`, and
`token_refresh.attempt_token_refresh`, also call `guard_linkedin` as their first statement. Several
of them wrap their LinkedIn calls in a broad `except Exception`. Without the entry guard, that
`except` would turn the refusal into a quiet `None`.

What a refusal looks like:

- **API.** The LinkedIn OAuth routes (`/auth/linkedin/` and `/auth/linkedin/callback`) answer
  **503** with `Demo mode: LinkedIn is disabled.`. A `DemoModeError` raised anywhere else in a
  request gets the same 503.
- **Reads keep working.** `resolve_token_status` still answers the SPA's token countdown from the
  database; only the renewal is skipped.
- **`/health/deep`.** `egress` reads `unknown`. That is not a failing state, so it never degrades
  `status`, and an uptime monitor matching `"status":"healthy"` stays green.

## A refusal is not a defect

Under demo mode, a refusal is the expected outcome. It must never file an error-tracking issue.
Two places make sure of that:

- **The logger** (`utilities/logger.py`, `_log_demo_refusal`) is the one place a refusal is
  demoted. A `log_warning` / `log_error` / `log_critical` called with `exc=DemoModeError` is written
  at **INFO** instead. It does not escalate, does not reach PostHog as an ERROR log, and does not
  file `$exception`. This covers every broad `except Exception: log_error(..., exc=e)` around a
  driver or publish call.
- **The Celery signal hooks** (`my_celery.on_task_failure`, `on_task_retry`) skip `DemoModeError`,
  the same way they skip `LaneTaskFailed`. A refused task still ends in FAILURE, and Celery's own
  log line for that failure goes to the container log, not to PostHog.

Both apply only while `DEMO_MODE` is actually on. A `DemoModeError` that surfaces with demo mode
off should be impossible, so it is treated as a defect: it keeps its level, escalates and is filed.

The guard itself logs one INFO line per refusal (`action_type="demo_mode"`).

Not covered: a caller that logs a refusal **without** `exc=` (for example
`log_error(f"... {e}")`) still writes at its own level.

## Keeping it complete

`tests/unit/utilities/test_demo_mode.py` scans every `*.py` under **`src/cqc_lem/`, `scripts/`
and `tools/`**. It skips `src/cqc_lem/ui/`, `src/cqc_lem/browser_extension/` and
`utilities/demo_mode.py` itself. The scan fails the build when:

- a file that talks to a LinkedIn API (`api.linkedin.com`, `linkedin.com/oauth|v2|rest`,
  `RestliClient`, `AuthClient`) is not in the guarded set `_LINKEDIN_HTTP_CLIENTS`, or never calls
  `guard_linkedin`;
- a guarded publish entry point does not open with `guard_linkedin(...)`;
- in a file that names a LinkedIn host, a non-`requests` HTTP call (`urlopen`, `httpx.*`,
  `aiohttp.*`, `urllib3.*`, `http.client.*`, `pycurl.*`) has no guard before it in its function;
- in `scripts/` or `tools/`, a `requests` call in a LinkedIn-naming file has no guard before it.
  A script may call `requests` before anything has imported `cqc_lem`, so the package guard may not
  be installed yet;
- a raw socket to LinkedIn exists anywhere except the egress probe;
- a webdriver is constructed anywhere except `selenium_util.py` and `tools/selenium_mcp_server.py`,
  or without a guard before it;
- anything other than the tutorial recorder passes `demo_safe`.

`tests/unit/scripts/test_demo_mode_scripts.py` runs the weekly version-check wrapper for real, with
`sudo` faked, and pins the `skipped` path.

A new LinkedIn client calls `guard_linkedin` at its entry and is added to `_LINKEDIN_HTTP_CLIENTS`.

### Deliberately not covered

- **`src/cqc_lem/browser_extension/`** runs in the operator's own Chrome to hand over a session
  cookie. It is not part of the stack.
- **Host crons that never import `cqc_lem`** run on the host, not in a demo container. Only the
  version-check wrapper reaches LinkedIn, and it does so through the guarded planner.
- **The LiteLLM container** does not load `cqc_lem` and makes no LinkedIn calls.
- **Non-Python code**, and anything an operator types into `docker exec`, is outside the scan.
- **IP literals.** The guard matches host names, so a request to a bare LinkedIn IP address is not
  recognised. Nothing in the codebase addresses LinkedIn by IP.

## The badge

`/api/app-info` reports `demo_mode`. When it is `true`, the SPA renders a small "Demo data" badge
(`ui/src/components/DemoBadge.tsx`) fixed to the bottom-left corner. It sits clear of the floating
dock (bottom-right) and the nav, so a recording can crop it out with one rectangle. It is
`pointer-events-none`, so it never covers a control. When the field is absent, false or still
loading, nothing renders.

The one Selenium exemption is `get_docker_driver(..., demo_safe=True)`. The tutorial recorder
(`utilities/marketing/video_tutorials.py`) uses it to film our own SPA. The scan pins that file as
the only caller, and `spa_base_url()` refuses a LinkedIn origin, so that browser can never be
pointed at LinkedIn.
