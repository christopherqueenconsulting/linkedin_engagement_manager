#!/usr/bin/env bash
# Deploy a released image tag to the VPS. Invoked by CI over SSH:
#   ssh deploy@vps 'cd /opt/lem && ./scripts/deploy.sh v1.2.3'
#
# Pulls the tag from GHCR, runs Flyway migrations, recreates the stack, waits
# for health, and rolls back to the last-good tag if health never comes up.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TAG="${1:?Usage: deploy.sh <image-tag>}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
LAST_GOOD_FILE="${ROOT_DIR}/.last_good_tag"
ENV_FILE="${ROOT_DIR}/.env"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }

# Persist IMAGE_TAG into .env so a reboot or a manual `compose up` (run without
# this script) stays on the deployed tag. We only `export` IMAGE_TAG for our own
# compose calls below; without writing it back, .env drifts a release behind and
# the next unscripted `up` silently reverts every app service to the stale tag.
persist_image_tag() {
  local tag="$1"
  if [[ -f "${ENV_FILE}" ]] && grep -qE '^IMAGE_TAG=' "${ENV_FILE}"; then
    sed -i -E "s|^IMAGE_TAG=.*|IMAGE_TAG=${tag}|" "${ENV_FILE}"
  else
    echo "IMAGE_TAG=${tag}" >> "${ENV_FILE}"
  fi
  log "Persisted IMAGE_TAG=${tag} to ${ENV_FILE}"
}

# 1. Sync compose files + Flyway migrations to the released ref.
PREV_TAG="$(cat "${LAST_GOOD_FILE}" 2>/dev/null || echo "")"
log "Fetching git ref ${TAG}"
git fetch --tags --quiet origin
# Discard any transient on-box .litellm hotfix (e.g. from the weekly model-health check) so the
# release tag's config is authoritative and the checkout below can't fail on a dirty tracked file.
# The durable version of any such hotfix arrives through that check's PR, not the box edit.
git checkout -- .litellm 2>/dev/null || true
git checkout --quiet "${TAG}" 2>/dev/null || git checkout --quiet "tags/${TAG}"

# litellm reads its bind-mounted .litellm/config.yaml ONLY at startup, and `compose up -d` does
# not recreate it on a mere file change — so a config edit (e.g. dropping a retired model) would
# otherwise sit inert until a manual restart. Restart it below when the config changed vs the last
# good tag (or when there's no baseline to compare against).
LITELLM_RESTART=0
if [[ -z "${PREV_TAG}" ]] || ! git diff --quiet "${PREV_TAG}" "${TAG}" -- .litellm/config.yaml 2>/dev/null; then
  LITELLM_RESTART=1
fi

# 2. Validate env before touching containers.
./scripts/check_env.sh

# 3. GHCR login (CI passes GHCR_USER/GHCR_PAT through the SSH env, or use a
#    long-lived login already present on the box).
if [[ -n "${GHCR_PAT:-}" && -n "${GHCR_USER:-}" ]]; then
  echo "${GHCR_PAT}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
fi

export IMAGE_TAG="${TAG}"

# 4. Pull the exact app image tag (+ any updated third-party images).
log "Pulling images for IMAGE_TAG=${TAG}"
${COMPOSE} pull

# 5. Migrations first — Flyway is idempotent (repair + migrate, baselineOnMigrate).
log "Running database migrations"
${COMPOSE} up -d mysql
${COMPOSE} run --rm flyway

# 6. Recreate changed services.
log "Starting stack"
${COMPOSE} up -d --remove-orphans

# 6b. Reload litellm if its config changed (compose won't recreate it on a bind-mount edit alone).
if [[ "${LITELLM_RESTART}" == "1" ]]; then
  log "litellm config changed vs ${PREV_TAG:-<none>} — restarting litellm to reload it"
  ${COMPOSE} restart litellm || log "WARN: litellm restart failed (continuing)"
fi

# 7. Wait for the web app to report healthy; roll back on failure.
log "Waiting up to ${HEALTH_TIMEOUT}s for web_app health"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
healthy=false
while (( $(date +%s) < deadline )); do
  if docker exec web_app curl -fsS "http://localhost:${API_PORT:-8000}/health" >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 5
done

if [[ "${healthy}" != true ]]; then
  log "ERROR: web_app did not become healthy."
  if [[ -f "${LAST_GOOD_FILE}" ]]; then
    prev="$(cat "${LAST_GOOD_FILE}")"
    log "Rolling back to ${prev}"
    git checkout --quiet "${prev}" 2>/dev/null || git checkout --quiet "tags/${prev}" || true
    export IMAGE_TAG="${prev}"
    persist_image_tag "${prev}"
    ${COMPOSE} up -d --remove-orphans
  fi
  exit 1
fi

# 8. Record the new good tag and prune old artifacts.
echo "${TAG}" > "${LAST_GOOD_FILE}"
persist_image_tag "${TAG}"
log "Deploy of ${TAG} OK. Pruning old images/build cache (>168h)."
docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true
docker builder prune -af --filter "until=168h" >/dev/null 2>&1 || true
log "Done."
