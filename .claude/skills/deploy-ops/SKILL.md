---
name: deploy-ops
description: Manually invoked only — production deploy, early-ship of a pending release batch, manual redeploy, rollback, or emergency local hotfix on the VPS.
disable-model-invocation: true
---

# Production deploy / rollback / hotfix

Pick the path — most "deploy this" asks are (1) or (2):

1. **Normal train** (default; nothing to do): merge to main → release-please tags vX.Y.Z → `build-and-push.yml` → GHCR → SSH deploy runs `scripts/deploy.sh vX.Y.Z` (checkout tag, `check_env.sh`, flyway, blue/green flip, `/health`, auto-rollback to `.last_good_tag`). Release PRs auto-merge at 05/11/17/23 UTC.
2. **Ship the pending batch now:** `gh workflow run release-auto-merge.yml` — or label a single PR `release:now` at merge (policy in `docs/release-fast-lane.md`).
3. **Manual redeploy of a tag:** `gh workflow run deploy-vps.yml -f tag=vX.Y.Z` (or on the box as `deploy`: `cd /opt/lem && ./scripts/deploy.sh vX.Y.Z`).
4. **Rollback:** on the box, `./scripts/rollback.sh vX.Y.Z` — re-ups a prior image tag; `.last_good_tag` holds the last healthy one.
5. **Emergency local hotfix** (CI/release blocked): build a local overlay image, set `IMAGE_TAG=<hotfix-tag>` in `/opt/lem/.env`, compose up. This **diverges prod from main** — the fix must still land via the normal PR flow; keep the prior release image for instant rollback.

Gotchas: prod runs the **baked image** (prod overlay strips the dev bind-mount) — editing files on disk does nothing. Runtime state (429 breaker, pauses, sweep cadences) lives in Redis and survives deploys. `Release-As: X.Y.Z` commit footer forces a version. Never touch `/opt/lem` from an agent checkout.

Authoritative: `docs/DEPLOYMENT.md`, `docs/zero-downtime-deploys.md`, `docs/release-fast-lane.md`.
