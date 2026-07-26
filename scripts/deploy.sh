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
# Maintenance window (issue #549): how long dispatch stays paused (TTL — it self-clears if this
# script dies) and how long we wait for in-flight tasks to finish before recreating the workers.
MAINT_PAUSE_SECONDS="${MAINT_PAUSE_SECONDS:-1800}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-480}"

log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }

# Read a key from the compose .env (the same file the containers get via env_file), so this script
# can never disagree with what the app actually bound. A value already exported into this shell
# wins, mirroring how compose resolves variables.
env_value() {  # $1 = key, $2 = default
  local val=""
  if [[ -f "${ENV_FILE}" ]]; then
    val="$(sed -nE "s/^[[:space:]]*$1=[\"']?([^\"'#[:space:]]*).*/\1/p" "${ENV_FILE}" | tail -n1)"
  fi
  echo "${val:-$2}"
}

# The FastAPI containers listen on ${API_PORT} (compose/local/fastapi/start-cloud), so both the
# per-color health check and the nginx upstream must use it. The EDGE port stays 8000 regardless:
# the Cloudflare tunnel ingress is the fixed `http://web_app:8000`.
API_PORT="${API_PORT:-$(env_value API_PORT 8000)}"
EDGE_PORT=8000

# Run a maintenance-mode subcommand inside whichever app container is up. Best-effort by design:
# a stack that can't answer must not block the deploy (warm shutdown + acks_late still protect
# in-flight work), so every call site tolerates a non-zero exit.
maint() {
  local sub="$1"; shift
  local svc
  for svc in celery_worker web_app; do
    if docker ps --format '{{.Names}}' | grep -qx "${svc}"; then
      ${COMPOSE} exec -T "${svc}" python -m cqc_lem.utilities.maintenance "${sub}" "$@"
      return $?
    fi
  done
  log "WARN: no running app container — skipping maintenance ${sub}"
  return 0
}

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

# 4b. Enter maintenance mode BEFORE anything touches the workers: stop beat so no new schedule
#     fires, pause dispatch, cancel each worker's queue consumers, then wait for what is already
#     running (video generation, commenting loops, DM sweeps) to finish. Without this the
#     recreate below killed live tasks mid-flight (issue #549).
log "Entering maintenance mode (pause ${MAINT_PAUSE_SECONDS}s); stopping celery_beat"
${COMPOSE} stop celery_beat || log "WARN: could not stop celery_beat (continuing)"
maint begin --pause-seconds "${MAINT_PAUSE_SECONDS}" || log "WARN: maintenance begin failed (continuing)"
log "Draining in-flight Celery tasks (up to ${DRAIN_TIMEOUT}s)"
maint drain --timeout "${DRAIN_TIMEOUT}" \
  || log "WARN: drain timed out — remaining tasks get re-queued on shutdown (task_acks_late)"

# 5. Migrations first — Flyway is idempotent (repair + migrate, baselineOnMigrate).
log "Running database migrations"
${COMPOSE} up -d mysql
${COMPOSE} run --rm flyway

# 6. Zero-downtime web flip (blue/green). The prod `web_app` service is an nginx edge that the
#    Cloudflare tunnel targets; the FastAPI app runs in web_api_blue/web_api_green behind it.
#    Bring the NEW color up on the new tag, health-check it, then flip nginx with a graceful
#    reload — the site never stops serving. Workers are recreated afterwards as before.
STATE_FILE="${ROOT_DIR}/.active_color"
NGINX_DIR="${ROOT_DIR}/deploy/nginx"
mkdir -p "${NGINX_DIR}"

render_nginx() {  # $1 = color to route to
  sed -e "s/__ACTIVE_COLOR__/web_api_$1/" \
      -e "s/__EDGE_PORT__/${EDGE_PORT}/" \
      -e "s/__BACKEND_PORT__/${API_PORT}/" \
    "${ROOT_DIR}/compose/prod/nginx/default.conf.tmpl" > "${NGINX_DIR}/default.conf"
}

color_healthy() {  # $1 = color, $2 = timeout seconds
  local deadline=$(( $(date +%s) + $2 ))
  while (( $(date +%s) < deadline )); do
    if docker exec "web_api_$1" curl -fsS "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

ACTIVE="$(cat "${STATE_FILE}" 2>/dev/null || echo "")"
case "${ACTIVE}" in blue|green) ;; *) ACTIVE="" ;; esac
if [[ -n "${ACTIVE}" ]] && docker ps --format '{{.Names}}' | grep -qx "web_api_${ACTIVE}"; then
  TARGET="$([[ "${ACTIVE}" == "blue" ]] && echo green || echo blue)"
else
  # First blue/green deploy (or recovery with no color running): bring up blue and route to it.
  ACTIVE=""
  TARGET="blue"
fi
# A conf must exist before the nginx edge can start; keep routing to the CURRENT color until the
# new one proves healthy. Only on a first cutover/recovery (no ACTIVE color) does the seed conf
# point at TARGET — otherwise an edge restart mid-deploy would send traffic to an unproven color.
[[ -f "${NGINX_DIR}/default.conf" ]] || render_nginx "${ACTIVE:-${TARGET}}"

log "Blue/green: active=${ACTIVE:-<none>} -> deploying ${TAG} to ${TARGET}"
${COMPOSE} up -d --no-deps "web_api_${TARGET}"

if ! color_healthy "${TARGET}" "${HEALTH_TIMEOUT}"; then
  log "ERROR: web_api_${TARGET} did not become healthy on ${TAG}."
  if [[ -n "${ACTIVE}" && -n "${PREV_TAG}" ]]; then
    # The active color was never touched — the site is still up on ${PREV_TAG}. Just restore the
    # standby to the last good tag and abort; no user-facing downtime.
    log "Active color '${ACTIVE}' untouched — restoring standby to ${PREV_TAG} and aborting."
    IMAGE_TAG="${PREV_TAG}" ${COMPOSE} up -d --no-deps "web_api_${TARGET}" \
      || log "WARN: standby restore failed — fix web_api_${TARGET} manually"
    git checkout --quiet "${PREV_TAG}" 2>/dev/null || git checkout --quiet "tags/${PREV_TAG}" || true
    maint end || log "WARN: could not clear maintenance mode (pause TTL will expire it)"
    exit 1
  fi
  # No serving color to fall back behind (first cutover) — legacy full rollback.
  if [[ -n "${PREV_TAG}" ]]; then
    log "Rolling back to ${PREV_TAG}"
    git checkout --quiet "${PREV_TAG}" 2>/dev/null || git checkout --quiet "tags/${PREV_TAG}" || true
    export IMAGE_TAG="${PREV_TAG}"
    persist_image_tag "${PREV_TAG}"
    ${COMPOSE} up -d --remove-orphans
  fi
  maint end || log "WARN: could not clear maintenance mode (pause TTL will expire it)"
  exit 1
fi

# 6b. Flip the edge to the healthy new color. `nginx -s reload` is graceful — no dropped
#     connections. On the very first cutover this recreates web_app from the old FastAPI container
#     into the nginx edge (the one deploy where a brief blip is unavoidable).
render_nginx "${TARGET}"
${COMPOSE} up -d --no-deps web_app
if docker exec web_app nginx -t >/dev/null 2>&1; then
  docker exec web_app nginx -s reload || log "WARN: nginx reload failed (fresh container already serves the new conf)"
else
  log "ERROR: rendered nginx conf failed validation — routing left unchanged."
  render_nginx "${ACTIVE:-${TARGET}}"
  maint end || true
  exit 1
fi

# 6c. Confirm the edge serves the new color end-to-end. nginx:alpine ships no curl, so fall back
#     to busybox wget rather than mistaking a missing binary for an unhealthy edge.
log "Verifying edge -> web_api_${TARGET}"
edge_ok=false
edge_probe="curl -fsS http://localhost:${EDGE_PORT}/health || wget -q -O /dev/null http://localhost:${EDGE_PORT}/health"
for _ in 1 2 3 4 5 6; do
  if docker exec web_app sh -c "${edge_probe}" >/dev/null 2>&1; then
    edge_ok=true; break
  fi
  sleep 5
done
if [[ "${edge_ok}" != true ]]; then
  log "ERROR: edge health failed after flip — flipping back to ${ACTIVE:-blue}."
  render_nginx "${ACTIVE:-blue}"
  docker exec web_app nginx -s reload || true
  maint end || true
  exit 1
fi
echo "${TARGET}" > "${STATE_FILE}"
log "Edge now routing to web_api_${TARGET}"

# 7. Converge the rest of the stack on the new tag (workers, beat, the standby color). The active
#    color and the edge are already at their target state, so this doesn't touch routing.
log "Recreating remaining services"
${COMPOSE} up -d --remove-orphans

# 7a. Reload litellm if its config changed (compose won't recreate it on a bind-mount edit alone).
if [[ "${LITELLM_RESTART}" == "1" ]]; then
  log "litellm config changed vs ${PREV_TAG:-<none>} — restarting litellm to reload it"
  ${COMPOSE} restart litellm || log "WARN: litellm restart failed (continuing)"
fi

# 7b. Healthy on the new tag — lift the pause and restore consumers.
maint end || log "WARN: could not clear maintenance mode (pause TTL will expire it)"

# 8. Record the new good tag and prune old artifacts.
echo "${TAG}" > "${LAST_GOOD_FILE}"
persist_image_tag "${TAG}"
log "Deploy of ${TAG} OK. Pruning old images/build cache (>168h)."
docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true
docker builder prune -af --filter "until=168h" >/dev/null 2>&1 || true
log "Done."
