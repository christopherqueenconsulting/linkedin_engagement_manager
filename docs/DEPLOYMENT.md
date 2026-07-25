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
(a PAT with `read:packages` for the VPS pull), `UI_API_TOKEN` (must equal one of
the `API_ACCESS_TOKENS` values in the server `.env`), plus existing
`GITGUARDIAN_API_KEY`, `ANTHROPIC_API_KEY`, `CODECOV_TOKEN`. GHCR **push** uses
the built-in `GITHUB_TOKEN`.

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
4. runs Flyway migrations (idempotent),
5. `docker compose up -d`,
6. waits for `/health`, **auto-rolls-back** to `.last_good_tag` on failure.

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
The weekly margin report needs no cron — Celery beat runs it (`weekly-margin-report`, Mon 12:00 UTC).

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
| 401 on the SPA | `UI_API_TOKEN` (build) ≠ `API_ACCESS_TOKENS` (server) |
| Migrations fail | `docker compose run --rm flyway`; inspect Flyway output |
| Tunnel down | `docker compose logs cloudflared`; verify `TUNNEL_TOKEN` |
| OAuth fails | `LI_REDIRECT_URL` matches the LinkedIn app + `app.<domain>` |
