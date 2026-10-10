# Demo mode

`DEMO_MODE` exists so a screen recording or a demo stack can never reach LinkedIn (#2372).

## Turning it on

Set `DEMO_MODE` in the environment of every service: API, Celery workers, beat. The values
`true`, `1`, `t`, `y` and `yes` turn it on, in any case. Anything else, or leaving it unset, leaves
it off, and off is the default. It is read on every call, never once at import, so setting it on a
running process takes effect on the next call.

It is a **safety control, not a feature flag** (`docs/feature-flags.md`). It is never resolved
through PostHog, and nothing turns it off except the environment.

## What it blocks

When it is on, every LinkedIn client raises `DemoModeError` (`utilities/demo_mode.py`) before any
connection opens. Each transport has one choke point:

| Transport | Choke point | Covers |
|---|---|---|
| HTTP | `install_requests_guard`, which wraps `requests.Session.send`. The package root installs it, so every process that imports `cqc_lem` has it | every `requests` call to `linkedin.com`, `licdn.com` or `lnkd.in` (matched on the parsed host, never a substring), including the `linkedin_api` SDK's `RestliClient` / `AuthClient` |
| Selenium | `selenium_util.get_docker_driver`, before the Grid is contacted | every lane, since all of them open their browser there (`get_driver_wait_pair` included) |
| Raw socket | `egress_probe.probe_proxy` | the `/health/deep` egress probe answers `unknown` and opens no socket |

The publish and comment helpers in `linkedin/poster.py` and `linkedin/reshare.py`, and
`token_refresh.attempt_token_refresh`, also call `guard_linkedin` as their first statement. Several
of them wrap their LinkedIn calls in a broad `except Exception`. Without the entry guard, the
transport's refusal would come back from them as a quiet `None` instead of an exception.

What a refusal looks like:

- **API**: the LinkedIn OAuth routes (`/auth/linkedin/`, `/auth/linkedin/callback`) answer **503**
  with `Demo mode: LinkedIn is disabled.`. A `DemoModeError` raised anywhere else in a request gets
  the same 503 from the app's exception handler.
- **Celery**: the task raises, so a scheduled post lands in the scheduler's ordinary failure
  handling.
- **Reads keep working**: `resolve_token_status` still answers the SPA's token countdown from the
  database. It only skips the renewal attempt.
- **Logs**: each refusal is logged at INFO (`action_type="demo_mode"`). Under demo mode a refusal
  is expected, so it is never logged as a warning.

The one Selenium exemption is `get_docker_driver(..., demo_safe=True)`. The tutorial recorder
(`utilities/marketing/video_tutorials.py`) uses it to film our own SPA. A test pins that file as
the only caller.

## The badge

`/api/app-info` reports `demo_mode`. When it is `true`, the SPA renders a small "Demo data" badge
(`ui/src/components/DemoBadge.tsx`) fixed to the bottom-left corner. It sits clear of the floating
dock (bottom-right) and the nav, so a recording can crop it out with one rectangle. It is
`pointer-events-none`, so it never covers a control. When the field is absent, `false`, or still
loading, the SPA renders nothing.

## Keeping it complete

`tests/unit/utilities/test_demo_mode.py` scans `src/` and fails the build when:

- a file that talks to a LinkedIn API (`api.linkedin.com`, `linkedin.com/oauth|v2|rest`,
  `RestliClient`, `AuthClient`) is not in the guarded set, or does not call `guard_linkedin`;
- a guarded publish entry point does not open with `guard_linkedin(...)`;
- a file that names a LinkedIn host imports an HTTP stack other than `requests` (`httpx`,
  `aiohttp`, `urllib3`, `urllib.request`, `http.client`, `pycurl`), which would go around the
  `Session.send` guard;
- a raw socket to LinkedIn exists anywhere except the egress probe;
- anything other than `selenium_util.py` constructs a browser, or anything other than the tutorial
  recorder passes `demo_safe`.

A new LinkedIn client goes through `requests` and calls `guard_linkedin` at its entry. Add it to
`_LINKEDIN_HTTP_CLIENTS` in that test.
