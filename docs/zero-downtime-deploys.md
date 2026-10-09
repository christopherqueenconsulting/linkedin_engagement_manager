# Zero-Downtime Deploys & Batched Releases

Shipped 2026-07-26. Two changes: prod deploys no longer take the site down (blue/green behind an
nginx edge), and releases batch 4x daily instead of shipping one deploy per merged PR.

## Topology

```
Cloudflare Tunnel (dashboard ingress: http://web_app:8000  — UNCHANGED)
        │
   web_app  = nginx:1.27-alpine edge (stable name, stays up across deploys)
        │   /etc/nginx/conf.d ← bind-mount of /opt/lem/deploy/nginx/ (git-ignored, deploy-rendered)
        ├── web_api_blue   ─┐ FastAPI app containers (${DOCKER_IMAGE_NAME}:${IMAGE_TAG})
        └── web_api_green  ─┘ both always up; ONE is routed, the other is a warm standby
```

- The Cloudflare dashboard still targets `http://web_app:8000` — no tunnel changes were needed;
  the nginx edge simply took over the `web_app` service name in `docker-compose.prod.yml`.
- Ports are templated end-to-end: the edge listens on `__EDGE_PORT__` (8000 — fixed, that's the
  tunnel ingress) and proxies to `__BACKEND_PORT__`, which `deploy.sh` reads from `/opt/lem/.env`
  as `API_PORT` — the same value the FastAPI containers bind and the per-color health check probes.
- The conf uses `resolver 127.0.0.11` + a variable `proxy_pass`, so backend IPs are re-resolved
  per request — recreating a color container never leaves the edge holding a stale IP
  (verified live: backend recreated with a new IP, traffic continued without a reload).
- Dev compose is untouched: `web_app` in dev is still the FastAPI container directly.

## Deploy flow (scripts/deploy.sh)

1. Pull images, run Flyway (old code keeps serving — migrations must stay backward-compatible,
   which additive Flyway migrations are).
2. Start the INACTIVE color on the new tag (`up -d --no-deps web_api_<target>`), health-check it.
   - **Failure here costs zero downtime**: the active color was never touched; the standby is
     restored to the last-good tag and the deploy aborts.
3. Render `/opt/lem/deploy/nginx/default.conf` to the new color, `nginx -t`, graceful
   `nginx -s reload` (no dropped connections), verify `/health` through the edge, then write
   `/opt/lem/.active_color`.
4. Persist the tag baseline (`.last_good_tag`, `IMAGE_TAG`), then enter maintenance mode and drain
   the Celery workers: stop beat, pause dispatch, cancel queue consumers and wait up to
   `DRAIN_TIMEOUT` for running tasks. The site is already on the new code, so a slow drain costs
   deploy duration, not availability.
5. `up -d --remove-orphans` converges workers/beat and the now-standby color onto the new tag.
   - `litellm` is reconciled just BEFORE this converge, inside step 4's drain window, on EVERY deploy
     (`reconcile_litellm`), and the deploy then waits for `/health/readiness`
     (`LITELLM_READY_TIMEOUT`, default 90s; a timeout or a failed `up` is a WARN, not an abort).
     If `.litellm/config.yaml` changed vs the last-good tag it is `up -d --no-deps --force-recreate`
     (litellm reads the bind-mounted config only at startup); otherwise a plain `up -d --no-deps`,
     which recreates it only when step 1's `pull` brought a new `main-latest` digest (or its
     service definition changed) and is a no-op otherwise. Either way it is recreated at most once,
     and the converge then finds it already on target and leaves it alone.
   - Why: recreating it after the converge (issue #2304), or inside it alongside the new workers on
     a new digest (issue #2343), dropped live worker LLM calls, and a dropped in-flight call is
     never retried in-app because the provider may already have billed it. The guarantee holds
     only when the drain finished: a task still running after `DRAIN_TIMEOUT` can lose its
     in-flight call here, as it can to the worker recreate that follows. The probe runs `python3`
     inside the container, the same interpreter the compose healthcheck uses.
   - The abort and rollback paths exit before this step, so they never recreate litellm on the
     reverted config by this route. Residual: the legacy first-cutover rollback's own converge can
     still recreate litellm if `pull` brought a new digest — that path only runs when there is no
     serving colour to fall back behind.
   - Every reconcile adds up to `LITELLM_READY_TIMEOUT` (90s) to the deploy when the proxy is slow
     to answer.

#### litellm not ready after a deploy

The reconcile never aborts a deploy, so a broken `main-latest` digest can go live with the deploy
reporting success. Look for these lines in the deploy log (the CI SSH step, or a local
`scripts/deploy.sh` run):

- `litellm image before reconcile: <id>` and `litellm image after reconcile: <id>`. Different IDs
  mean a new digest landed on this deploy; `<none>` means no `litellm` container existed.
- `WARN: litellm not ready after <N>s (continuing; previous image <id>)`, the readiness wait ran
  out. Workers resume against the proxy anyway when maintenance mode ends.

Diagnose: `docker logs --tail 200 litellm`, then re-run the readiness probe by hand:
`docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T litellm python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health/readiness', timeout=5)"`.

Pin back to the previous image: tag the logged previous ID
(`docker tag <previous-id> ghcr.io/berriai/litellm:main-latest`), then
`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --force-recreate litellm`
from `/opt/lem` (add `-f docker-compose.grid.yml` when the box runs the Grid overlay, as
`deploy.sh` does). The next deploy's
`pull` moves the tag forward again, so pin a digest in `docker-compose.yml` by PR if the new one
stays broken. The same deploy ends with `docker image prune -af --filter until=168h`, which deletes
every image created more than 7 days ago that no container uses. If the previous digest was that
old, it is gone by the time the deploy finishes, and the pin-back needs a `docker pull` of an older
digest by its `sha256` reference instead.

### Worker-tier resilience (issue #831)

The final `up -d --remove-orphans` can hit a Docker race: compose renames the old
container before creating the replacement, and a concurrent `docker exec` (or compose's
own bookkeeping) can reference the old ID in that window, producing `No such container`.
That aborts the converge mid-tier and leaves workers in `Created` — a silent worker outage.

`scripts/deploy.sh` now:

- **Retries once** if the converge output contains `No such container` (`converge_stack`).
  Every attempt's compose output is echoed to the deploy log, success or failure — those
  per-container lines are the only evidence of what the converge did, and they are what
  identified this race both times it happened.
- **Verifies the final state** (`verify_stack_running`): `compose config --services` is the
  expectation, and any of those services sitting in `Created` / `Exited` / `Dead` / `Paused` /
  `Restarting`, or absent from `compose ps`, fails the deploy loudly instead of leaving the
  worker tier down silently. The expectation is deliberately the **profile-filtered** service
  list: `compose ps` labels containers by PROJECT, not by profile, so a leftover container of a
  profiled-out service sits `Created`/`Exited` indefinitely and must never fail a deploy it is not
  part of. `flyway` is excluded too — it is a `run --rm` one-shot.
  The one container that actually hit this — the standalone `selenium-chrome` the Grid overlay
  parks — is now *removed* by the step-3b topology guard rather than merely ignored (#1092): it
  matches on `docker ps -a`, so a standalone left in `Created` (a manual `compose up` without the
  Grid overlay, whose start fails because the hub already holds 4444) is swept on the next deploy
  instead of permanently poisoning the "any container in `Created`" outage tripwire.
- **Persists `IMAGE_TAG` / `.last_good_tag` immediately after the edge flip**, while the
  serving tier is live on the new tag. A later worker-tier failure is a partial deploy, but
  the box's recorded baseline matches what is actually running so the next deploy diffs
  against the correct starting point — and a manual `compose up -d` recovery uses the right tag.

The legacy full-rollback path recreates the **same** worker tier, so it goes through
`converge_stack` too — a rollback is the worst place to hit the race, since something has
already gone wrong by then.

Rollback still exists at every step; the only deploy with a brief blip was the first cutover
(recreating `web_app` from a FastAPI container into the nginx edge). `scripts/rollback.sh` remains
the sledgehammer (recreates in place, skips migrations); for a zero-downtime rollback prefer
`gh workflow run deploy-vps.yml -f tag=<last-good>` — it walks the same blue/green flip.

## Release cadence

`release-auto-merge.yml` no longer reacts to release-PR events; it runs on a cron —
**05:00 / 11:00 / 17:00 / 23:00 UTC** — and enables auto-merge on the accumulated release-please
PR (merges when CI is green → tag → build → deploy). Between windows, merged PRs pile into the one
open release PR.

**Manual dispatch** (owner, or a Claude session using the owner's `gh` auth):

```bash
gh workflow run release-auto-merge.yml           # ship the pending batch now
gh workflow run deploy-vps.yml -f tag=vX.Y.Z     # redeploy/rollback an existing tag
gh workflow run deploy-vps.yml -f tag=vX.Y.Z -f rollback=true   # rollback.sh path (no migrations)
```

## Stale lazy chunks in already-open tabs (issue #743)

Zero-downtime is about the SERVER. A browser tab is the other half: it holds the content-hashed
chunk filenames of the build it loaded, and a lazily-imported chunk (jszip in avatar training,
anything code-split) is only fetched when the user triggers the feature. At 4 releases a day, a tab
open across one asks for a hash the new image no longer has — a 404 that presents as "the feature is
broken", not "reload me".

Three layers, in this order — the first two are REACTIVE (something already failed to load), the
third is the proactive prompt:

1. **Asset retention (the user sees nothing).** Both colors mount the named `spa_asset_archive`
   volume at `SPA_ASSET_ARCHIVE_DIR=/app/spa_asset_archive`. On startup each FastAPI container syncs
   its own `ui/dist/assets` into it and prunes past `SPA_ASSET_ARCHIVE_KEEP` builds (default 5 —
   more than a day of releases). A miss in the live bundle falls back to the archive
   (`api/spa_assets.py`). Serving an old hash is always safe: the names are content-hashed, so the
   `immutable` cache contract is preserved rather than weakened. This lives in the app, not in
   `deploy.sh`, precisely so archive maintenance can never fail a deploy — a container that cannot
   write the volume logs a warning and serves the live bundle only.
2. **One silent reload (the fallback).** For anything older than the archive, the SPA reloads once
   on a chunk-load failure (`ui/src/utils/chunkReload.ts`). `index.html` is `no-store`, so the
   reload always lands on the current build. A `sessionStorage` marker caps it at one attempt per
   minute; a second failure inside that window shows "A new version was released — please refresh"
   (`NewVersionNotice.tsx`) instead of looping. An OFFLINE tab is never reloaded: a disconnected
   dynamic import reports the same message a stale chunk does, and reloading a `no-store` shell with
   no network replaces a working app with the browser's offline page.
3. **New-version awareness (issue #754).** The two layers above only fire once something has failed,
   and a tab several builds behind can keep WORKING while running old client code against a newer
   API. `ui/src/hooks/useNewVersion.ts` polls `/api/app-info` every 5 minutes — skipping the request
   entirely while the tab is hidden, and re-checking on `visibilitychange` so a backgrounded tab
   catches up the moment it returns — and raises the SAME `NewVersionNotice` when the reported
   version differs from the one this tab booted with (the first version it ever read). The BOOT read
   is the single poll that ignores visibility: a tab opened in the background (ctrl-click, a restored
   session) still runs this bundle while hidden, and deferring its first read would baseline it
   against whatever shipped before the user first looked at it — the tab would be running old code
   and could never be told. It is a PROMPT: this layer never reloads, so it can never race layer 2's
   one automatic reload. An unreachable endpoint or a missing/blank version raises nothing and does
   not become the baseline — an unknown version is never "new".

The `no-store` shell is load-bearing for BOTH reactive layers — if a CDN rule ever caches `index.html`, the
reload lands on the same broken build. `tests/unit/api/test_spa_asset_archive.py` guards the header
contract and the compose wiring.

## Operational notes

- `/opt/lem/deploy/` and `/opt/lem/.active_color` are runtime state (git-ignored). If routing ever
  needs a manual flip: render the conf by hand from `compose/prod/nginx/default.conf.tmpl`
  (substitute all three placeholders — `__ACTIVE_COLOR__`, `__EDGE_PORT__`, `__BACKEND_PORT__`),
  then `docker exec web_app nginx -t && docker exec web_app nginx -s reload`.
- Both colors mount the same `./logs`, `assets` and `spa_asset_archive` volumes; the standby serves
  no traffic but is warm, so a flip is instant.
- RAM cost of the standby ≈ one FastAPI container (~0.5-1 GB), well within the box's headroom.
- If a migration is ever NOT backward-compatible (rename/drop), it must ship in two releases
  (expand → migrate → contract) — the old color serves during migration.
