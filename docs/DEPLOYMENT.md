# VPS Deployment Runbook

Live/dev instance of LEM on a Hostinger VPS, running the same Docker stack as
local, exposed through a Cloudflare Tunnel, deployed automatically from GitHub
releases.

```
local dev → PR to main → CI gates → release-please tags vX.Y.Z
   → build-and-push.yml builds image → GHCR → SSH deploy to VPS → migrate + up
```

## Architecture

| Concern | Choice |
|---|---|
| Registry | GHCR — `ghcr.io/gitchrisqueen/cqc-lem:<tag>` |
| Delivery | GitHub Action SSHes to the VPS, pulls the tag, `docker compose up -d` |
| Ingress | Cloudflare Tunnel (`cloudflared` container) — no inbound ports |
| Releases | release-please (Conventional Commits → release PR → tag) |
| Prod overlay | `docker-compose.prod.yml` on top of `docker-compose.yml` |

Public surface:

| Hostname | Service | Protection |
|---|---|---|
| `app.<domain>` | web_app:8000 (SPA + API) | Public; API routes require a bearer token |
| `flower.<domain>` | flower:8555 | Cloudflare Access + Flower basic auth |
| `litellm.<domain>` | litellm:4000 | Cloudflare Access + `LITELLM_MASTER_KEY` |
| `vnc.<domain>` | selenium-chrome:7900 | Cloudflare Access |

## Sizing

Selenium/Chrome reserves 2 vCPU / 4 GB on its own; with MySQL, Redis, two Celery
workers, LiteLLM and FastAPI, target **8 vCPU / 16 GB** (Hostinger KVM 8). 4 vCPU
/ 8 GB is the bare minimum. `vps_bootstrap.sh` adds a 4 GB swapfile.

## One-time setup

### 1. Provision the VPS

```bash
# As root on a fresh Ubuntu VPS:
scp scripts/vps_bootstrap.sh root@<vps>:/root/
ssh root@<vps> 'REPO_URL=https://github.com/gitchrisqueen/linkedin_engagement_manager.git bash /root/vps_bootstrap.sh'
```

This installs Docker + Compose, creates the `deploy` user, clones the repo to
`/opt/lem`, locks down SSH + ufw (SSH-only inbound), enables Docker log
rotation, and adds swap.

### 2. CI deploy key

Generate a dedicated keypair; add the **public** key to
`/home/deploy/.ssh/authorized_keys`, and store the **private** key as the
`VPS_SSH_KEY` repo secret.

### 3. Server env

```bash
ssh deploy@<vps>
cd /opt/lem
cp .env.prod.example .env
nano .env          # fill in real secrets
chmod 600 .env
```

### 4. Cloudflare Tunnel

1. Zero Trust → Networks → Tunnels → **Create a tunnel** (named `lem`).
2. Copy the tunnel **token** into `TUNNEL_TOKEN` in `/opt/lem/.env`.
3. Add public hostnames mapping to the internal services in the table above
   (e.g. `app.<domain>` → `http://web_app:8000`).
4. Zero Trust → Access → Applications → add self-hosted apps for
   `flower/litellm/vnc.<domain>` restricted to your email/SSO.
5. Register `https://app.<domain>/auth/linkedin/callback` in the LinkedIn
   Developer Console and set it as `LI_REDIRECT_URL`. Keep `NGROK_PLAN=off`.

Alternatively manage ingress from the repo via `cloudflared/config.yml` (see the
header of that file) instead of the dashboard.

### 5. GitHub repo configuration

**Secrets:** `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `GHCR_PAT`
(a PAT with `read:packages` for the VPS pull), plus existing
`GITGUARDIAN_API_KEY`, `ANTHROPIC_API_KEY`, `CODECOV_TOKEN`. GHCR **push** uses
the built-in `GITHUB_TOKEN`.

> `UI_API_TOKEN` is **retired** (issue #950). The SPA used to be built with it as
> `VITE_API_TOKEN`, which inlined one of the server's `API_ACCESS_TOKENS` into a public
> bundle. The SPA authenticates on its httpOnly session cookie; `API_ACCESS_TOKENS` is now a
> non-browser credential and can be rotated in the server `.env` alone, with no rebuild. Delete
> the repo secret — nothing reads it.

**Environment:** create a `production` environment with required reviewers to
gate the deploy job.

**Branch protection (main):** require `CI / Unit Tests`,
`CI / Integration Test w/ Coverage`, `CodeQL Security Analysis`,
`GitGuardian Security Scan`, and ≥1 review.

### 6. First deploy

```bash
ssh deploy@<vps> 'cd /opt/lem && ./scripts/deploy.sh latest'
```

## Routine deploys

Merge work to `main` with Conventional Commit messages → release-please opens a
"chore: release X.Y.Z" PR. Merge it → a `vX.Y.Z` tag + GitHub Release →
`Build & Deploy Release` builds/pushes the image and (after `production`
approval) SSHes in and runs `scripts/deploy.sh vX.Y.Z`, which:

1. checks out the tag (syncs compose + Flyway migrations),
2. validates the server `.env` (`check_env.sh`),
3. pulls the GHCR image,
4. **enters maintenance mode** — stops `celery_beat`, pauses dispatch, cancels each worker's
   queue consumers, then drains in-flight tasks (see below),
5. runs Flyway migrations (idempotent),
6. `docker compose up -d`,
7. waits for `/health`, **auto-rolls-back** to `.last_good_tag` on failure,
8. **leaves maintenance mode** — restores consumers and resumes dispatch (on the rollback path too).

## Version milestones and owner-triggered major releases

LEM's version number signals product stage, not just changelog mechanics:

- **0.x** — pre-launch / open beta. Breaking changes bump the minor under release-please's `python` release type.
- **1.0.0** — cut when the marketing engine goes live (brand account + affiliate program running, per `docs/launch-and-marketing-plan.md` P0→P1).
- **2.0.0** — cut after the user-testing phase and its resulting fixes are complete.

These two major releases are **owner-triggered milestones**, not automatic. No CI job or commit should cut 1.0.0 or 2.0.0 on its own.

### Forcing a major version with `Release-As`

release-please honors a `Release-As: X.Y.Z` footer in a commit body, which overrides the calculated next version for the release PR it opens. To cut 1.0.0, make an empty release-forcing commit on `main` after the final pre-1.0 PR has merged:

```bash
git checkout main
git pull origin main
git commit --allow-empty -m "chore(main): release 1.0.0

Release-As: 1.0.0"
git push origin main
```

release-please will open a release PR for exactly `1.0.0`. Merging that PR tags `v1.0.0`, builds `ghcr.io/christopherqueenconsulting/cqc-lem:v1.0.0`, and deploys it the same way every other release does.

The same footer works for 2.0.0 (or any other explicit version) when the owner decides the time is right.

### Before cutting 1.0.0

Verify the places that care about major-version semantics:

- **Image tag / `IMAGE_TAG` flow:** `scripts/deploy.sh` takes a tag argument and writes it to `IMAGE_TAG` in `.env`; it is version-agnostic and works the same for `v0.116.0` and `v1.0.0`.
- **Rollback path:** `scripts/rollback.sh` and the deployer's `.last_good_tag` mechanism also use the raw tag string, with no special-casing for major versions.
- **`/api/app-info`:** the SPA footer reads the installed package version via `get_app_version()` in `src/cqc_lem/utilities/env_constants.py`. After 1.0.0 it reports `1.0.0` automatically.
- **Conventional Commit discipline:** once the repo is on `1.x`, a breaking change must use `feat!:` or a `BREAKING CHANGE:` footer to bump the major version. Pre-1.0 breaking changes only bump the minor, so the commit convention has not mattered for major bumps yet.

## Deploys and in-flight Celery tasks (issue #549)

A deploy recreates the app containers, which SIGTERMs the workers. Three layers keep long tasks
(6-min video generation, commenting loops, DM sweeps) from being killed and lost:

1. **Warm shutdown reaches Celery.** `compose/local/celery/run-as-celery` `exec`s the worker
   (dropping to `celeryworker` via `setpriv`, falling back to `su`) so the celery process — not a
   wrapper shell — receives SIGTERM. It then stops consuming and finishes what is running.
   `stop_grace_period` (`CELERY_STOP_GRACE_PERIOD`, default **8m**) gives it room before SIGKILL.
2. **Nothing new is picked up during the window.** Maintenance mode pauses dispatch through the
   existing Redis kill-switch (`pause_automation`, reason `deploy`) and cancels each worker's own
   queue consumers, then polls `inspect active` until idle. Tune with `MAINT_PAUSE_SECONDS`
   (default 1800 — a TTL, so a crashed deploy can't leave automation off) and `DRAIN_TIMEOUT`
   (default 480s). A pre-existing 429/manual pause is **not** lifted at the end.
3. **Anything still interrupted is re-run.** `task_acks_late` + `task_reject_on_worker_lost` are
   global, so an un-finished task is re-delivered instead of dropped. Re-runs are safe:
   `post_to_linkedin` short-circuits on `PostStatus.POSTED`, comments go through the
   `commented_posts` claim ledger (a claim abandoned by a killed worker is taken over after
   `CLAIM_STALE_MINUTES`), group posts and newsletter editions gate on a durable DB status, and
   appreciation claims each recipient in `appreciation_touches` before dispatch.
   **`QueueOnce` is not part of that guarantee** — it locks in `apply_async`, on the PRODUCER
   side, and `Task.__call__` only clears a lock rather than checking one, so it stops two
   dispatchers racing but does nothing about a broker redelivery. `CELERY_VISIBILITY_TIMEOUT`
   (default: longest task + 15m) must stay above the longest task so acks_late can't hand a
   still-running task to a second worker.

Inspect or drive it by hand from the box:

```bash
docker compose exec -T celery_worker python -m cqc_lem.utilities.maintenance status
docker compose exec -T celery_worker python -m cqc_lem.utilities.maintenance begin --pause-seconds 900
docker compose exec -T celery_worker python -m cqc_lem.utilities.maintenance drain --timeout 300
docker compose exec -T celery_worker python -m cqc_lem.utilities.maintenance end
```

## Manual redeploy / rollback

Use the **Redeploy / Rollback VPS** workflow (Actions → Run workflow) with a
tag, ticking `rollback` to skip migrations. Or on the box:

```bash
cd /opt/lem
./scripts/rollback.sh v1.2.2      # re-up a prior tag
```

## Backups

Cron on the VPS:

```cron
0 3 * * * cd /opt/lem && ./scripts/backup.sh >> logs/backup.log 2>&1
```

Dumps `linkedin_manager` (gzipped) + the `chrome-profile` volume to
`/opt/lem/backups`, retains `RETAIN_DAYS` (default 7), and optionally `rclone`s
to `BACKUP_REMOTE` (e.g. Cloudflare R2). Restore:

```bash
gunzip -c backups/db-<stamp>.sql.gz | docker exec -i mysql_db \
  mysql -u root -p"$MYSQL_ROOT_PASSWORD" linkedin_manager
```

## Performance / margin snapshot

Daily engagement + cost/margin snapshot (issue #491) — runs as `lem`, appends one JSON line per day
to `/home/lem/perf-tracking/metrics.jsonl`:

```cron
30 23 * * * /home/lem/<repo-clone>/scripts/perf_snapshot.sh
```

The script reads MySQL directly and shells into `web_app` for the `margin` block
(`python -m cqc_lem.utilities.margin --daily-json`), so it needs `sudo -n docker` and a deployed
image that contains `cqc_lem.utilities.margin`. Overridable: `PERF_DIR`, `LEM_ENV_FILE`,
`MARGIN_CONTAINER`. `"margin": {"ledger_available": false}` means `cost_ledger` isn't capturing yet.
## Daily issue triage

Organizes uncategorized open issues into milestones with an impact-first rubric (issue #748).
Runs as `lem` from a dedicated cron clone, writes a dated report to `docs/triage/<date>.md`, and
optionally applies safe label/milestone edits when invoked with `--apply`:

```cron
0 9 * * * /home/lem/<repo-clone>/scripts/triage_issues.sh --apply
```

Default mode is `--dry-run`, so a bare invocation prints the plan without mutating GitHub. The
script uses `lem-medium` for priority/milestone grouping but still adds value if the LLM is
unavailable (deterministic missing-label, staleness, and phase-drop checks). Env overrides:
`TRIAGE_REPO`, `TRIAGE_DIR`, `REPO`, `LITELLM_MASTER_KEY`/`OPENAI_API_KEY`.

The weekly margin report needs no cron — Celery beat runs it (`weekly-margin-report`, Mon 12:00 UTC).

## Weekly SDUI drift sweep

One read-only probe sweep of every Selenium surface (issue #1013), grading each `ok` / `drift` /
`unknown` and filing ONE deduped `agent:ready` issue per `drift`. Runs as `lem`, off-peak, inside
the selenium worker's session:

```cron
40 6 * * 1 /home/lem/<repo-clone>/scripts/weekly_sdui_drift_check.sh
```

Sends no invite, posts nothing, ticks no checkbox and clicks no Send/Post/Invite control. Env
overrides: `SDUI_PROBE_CONTAINER`, `SDUI_PROBE_USER_ID`, `SDUI_PROBE_PROFILE_URL` (use a
2nd/3rd-degree profile so the degree badge is actually grounded), `SDUI_DRIFT_DIR`,
`SDUI_DRIFT_REPO`, `DRY_RUN=1`. Full posture + the coverage matrix: `docs/sdui-probe-coverage.md`.

## Persistent state

Named volumes survive deploys: `db_data` (MySQL), `redis_data`, `flower_db`,
`chrome-profile` (LinkedIn session — losing it forces re-login/2FA). Generated
media under `src/cqc_lem/assets/` lives **inside the image** in prod; if it must
persist across deploys, add a named volume for `/app/src/cqc_lem/assets` to the
prod overlay.

## Observability

- PostHog already receives `log_error`/`log_critical`; set `POSTHOG_API_KEY`.
- Container logs: `docker compose logs -f <service>` (rotated, 20 MB × 5).
- Queue/tasks: `flower.<domain>`. Live browser: `vnc.<domain>`.
- Uptime: monitor `https://app.<domain>/health`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Deploy rolls back | `docker compose logs web_app`; `/health` not reachable |
| 401 on the SPA | dead/absent session cookie — check `SESSION_COOKIE_SECURE` matches the origin's scheme (the SPA holds no API token since #950) |
| Migrations fail | `docker compose run --rm flyway`; inspect Flyway output |
| Tunnel down | `docker compose logs cloudflared`; verify `TUNNEL_TOKEN` |
| OAuth fails | `LI_REDIRECT_URL` matches the LinkedIn app + `app.<domain>` |

## Compose file layering — why editing files on disk does nothing in prod

The stack is always launched with **both** compose files:
`docker compose -f docker-compose.yml -f docker-compose.prod.yml`.

`docker-compose.yml` alone is the DEV config — it bind-mounts `./src:/app/src`, so local edits are
live immediately. `docker-compose.prod.yml` overrides that bind-mount away, so in PRODUCTION every
app service runs the code baked into the image; editing files on disk under `/opt/lem` does
nothing until a new image ships. Code lives at `/app/src/cqc_lem/...` inside the image.

The image ref is `${DOCKER_IMAGE_NAME}:${IMAGE_TAG:-latest}`, both set in `/opt/lem/.env`
(git-ignored); `scripts/deploy.sh` exports `IMAGE_TAG` per-deploy. App services that share the
image: `web_api_blue`/`web_api_green` (FastAPI blue/green), `celery_worker`,
`celery_worker_selenium{,_prepost,_outreach,_content}`, `celery_beat`, `flower`. In prod, `web_app`
is the nginx edge described above, not the FastAPI container. Infra (`mysql`, `redis`,
`selenium-chrome`, `litellm`, `cloudflared`, `flyway`) uses its own upstream images and is
unaffected by this layering.

Runtime state (429 breaker, manual automation pause, reply-sweep cadence keys) lives in **Redis**,
not the DB or a container, so it survives every deploy. Inspect/repair it by calling the real
functions inside a running container so the correct Redis URL is used, e.g.:

```bash
docker exec celery_worker_selenium python -c \
  "from cqc_lem.utilities.linkedin.rate_limit import clear_rate_limit, pause_automation; ..."
```

## Local hotfix deploy (fallback when CI/release is too slow or blocked)

Build a thin overlay image `FROM` the currently running release tag that only `COPY`s the changed
`src` files (identical deps, seconds not minutes). Then on the box:

1. Set `/opt/lem/.env` → `IMAGE_TAG=<hotfix-tag>`.
2. `cd /opt/lem && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --pull never <app-services>`.
3. Keep the prior release image locally for instant rollback (`IMAGE_TAG=vX.Y.Z`).

**This diverges prod from `main`** — the fix MUST still land via the normal PR → release flow, or
the next release will REVERT it. Requires `sudo` for the Docker socket on this box.
