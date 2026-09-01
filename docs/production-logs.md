# Production log files — where they are, and what they don't have

Issue #1817. A production investigation with `sudo docker` access concluded that **no persistent
log file existed** for the app services — the reason it could not root-cause two of its own
findings. It did: `/opt/lem/logs/` holds 14+ days of exactly those lines. Read this BEFORE
concluding a line is missing from prod.

## Where

`/opt/lem/logs/cqc_lem_YYYY_MM_DD.log` — one file per UTC day.

- **Writer:** `DatedRotatingFileHandler` (`src/cqc_lem/utilities/logger.py`), which re-resolves the
  dated filename **per record**, not at import — a long-lived container does not keep appending to
  the day it started on (#1093).
- **Rotation:** 250 MB × 10 backups within a day, as `<dated name>.log.1`, `.log.2`, …
- **In the container:** the same directory is mounted at `/app/logs`, so `docker exec <service>
  tail -f /app/logs/cqc_lem_$(date -u +%Y_%m_%d).log` reads the live file from inside any service.

**Prefer grep over `docker logs` for anything older than a few hours.** Containers are recreated on
every deploy (4×/day, `docs/zero-downtime-deploys.md`), so `docker logs` typically only reaches back
to the last recreate — the dated files on disk are the durable record.

## What's NOT in these files

- **`LOG_LEVEL` is unset in the production `.env`, so the effective level is INFO.** DEBUG lines
  never reach the file. This is why the silent-skip paths behind several engagement lanes (connect,
  follow-up, outreach-funnel) are unrecoverable after the fact from these logs alone — each of them
  exits at `log_debug` by design (an expected no-op is DEBUG, never a repeat-triggering `log_warning`
  — see `docs/error-tracking.md`).
- **`POSTHOG_LOG_LEVEL=WARNING` in production** — WARNING and above also reach PostHog; INFO does
  not. PostHog is not a superset of the log file or vice versa; check both when triaging.

## The prod volume mount, so it doesn't look like dead config

`docker-compose.prod.yml` mounts `./logs:/app/logs` **explicitly** on `web_api_blue`/`_green` and on
every `celery_worker*` / `celery_beat` / `flower` service. The base `docker-compose.yml` also sets
`volumes_from: [web_app]` on those same worker services, and the prod overlay never resets that key —
but in prod `web_app` is the nginx edge container (`volumes: !override` to only
`./deploy/nginx:/etc/nginx/conf.d:ro`), so that inherited `volumes_from` no longer carries logs or
assets. **The explicit `./logs:/app/logs` mounts in the prod overlay are what actually deliver the
logs to every worker** — the `volumes_from: [web_app]` inherited from the base file is harmless but
vestigial for this purpose in prod. Don't remove either one without checking both: removing the
explicit mount breaks logging; removing `volumes_from` is a no-op today but touches dev too.

## See also

- `docs/error-tracking.md` — `$exception` → PostHog issues, and the warning-escalation contract
- `docs/observability-map.md` — the per-surface invariants these logs feed
