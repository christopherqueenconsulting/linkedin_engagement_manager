#!/usr/bin/env bash
# Deploy a released image tag to the VPS. Invoked by CI over SSH:
#   ssh deploy@vps 'cd /opt/lem && ./scripts/deploy.sh v1.2.3'
#
# Pulls the tag from GHCR, runs Flyway migrations, recreates the stack, waits
# for health, and rolls back to the last-good tag if health never comes up.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

LAST_GOOD_FILE="${ROOT_DIR}/.last_good_tag"
ENV_FILE="${ROOT_DIR}/.env"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
# Maintenance window (issue #549): how long dispatch stays paused (TTL — it self-clears if this
# script dies) and how long we wait for in-flight tasks to finish before recreating the workers.
MAINT_PAUSE_SECONDS="${MAINT_PAUSE_SECONDS:-1800}"
# 180s, down from 480s. The old value was almost always spent in full — v0.113.0 burned the entire
# 480s waiting on a SINGLE task — and a drain that times out is not a failure: `task_acks_late`
# re-queues whatever is still running when the worker shuts down. So the timeout only buys the
# chance to finish cleanly, and the marginal value of minutes 3-8 is very low. Raise it per-deploy
# with DRAIN_TIMEOUT=... if a release lands during a long video-generation run.
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-180}"

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

# Selenium topology. The Grid overlay (hub + N single-session nodes) became the deployed default at
# the 2026-07-27 cutover; without composing it in here, EVERY deploy would silently revert the box
# to the single standalone container — the stack would come up healthy and simply have the old
# topology, which is exactly the kind of drift that is invisible until capacity matters.
# Set SELENIUM_TOPOLOGY=standalone (env, or in the box's .env) to fall back; the overlay parks the
# standalone behind a compose profile rather than deleting it, so the fallback stays one flag.
# Resolved through env_value (NOT a second hand-rolled .env parser) so an inline comment, quotes or
# a stray CR can't turn `standalone` into an unrecognised value.
SELENIUM_TOPOLOGY="${SELENIUM_TOPOLOGY:-$(env_value SELENIUM_TOPOLOGY grid)}"
SELENIUM_TOPOLOGY="$(printf '%s' "${SELENIUM_TOPOLOGY}" | tr '[:upper:]' '[:lower:]')"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
case "${SELENIUM_TOPOLOGY}" in
  standalone) ;;
  grid) COMPOSE="${COMPOSE} -f docker-compose.grid.yml" ;;
  # Anything else is a typo. Deploying the OTHER topology on one is precisely the silent drift this
  # block exists to end, so name it and take the documented default rather than guessing quietly.
  *) log "WARN: unrecognised SELENIUM_TOPOLOGY='${SELENIUM_TOPOLOGY}' — using 'grid'"
     SELENIUM_TOPOLOGY="grid"
     COMPOSE="${COMPOSE} -f docker-compose.grid.yml" ;;
esac
log "Selenium topology: ${SELENIUM_TOPOLOGY}"

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

# Quiesce the workers before anything recreates them: stop beat so no new schedule fires, pause
# dispatch, cancel each worker's queue consumers, then wait for what is already running (video
# generation, commenting loops, DM sweeps) to finish. Issue #549: without this, the recreate killed
# live tasks mid-flight.
#
# A FUNCTION, not inline, because there are TWO paths that recreate workers — the normal deploy and
# the legacy full rollback — and only one of them used to be covered. When the drain moved after the
# blue/green flip (see §5) the rollback path was left recreating workers with no drain at all;
# tests/unit/app/test_worker_shutdown.py caught it.
drain_workers() {
  log "Entering maintenance mode (pause ${MAINT_PAUSE_SECONDS}s); stopping celery_beat"
  ${COMPOSE} stop celery_beat || log "WARN: could not stop celery_beat (continuing)"
  maint begin --pause-seconds "${MAINT_PAUSE_SECONDS}" || log "WARN: maintenance begin failed (continuing)"
  log "Draining in-flight Celery tasks (up to ${DRAIN_TIMEOUT}s)"
  maint drain --timeout "${DRAIN_TIMEOUT}" \
    || log "WARN: drain timed out — remaining tasks get re-queued on shutdown (task_acks_late)"
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

# Converge the worker/standby tier, retrying once on the Docker "No such container"
# race that can abort a mid-tier recreate (issue #831).
converge_stack() {
  local attempts=0
  local max_attempts=2
  local logfile
  while true; do
    attempts=$((attempts + 1))
    logfile="$(mktemp)"
    log "Recreating remaining services (attempt ${attempts}/${max_attempts})"
    if ${COMPOSE} up -d --remove-orphans >"${logfile}" 2>&1; then
      rm -f "${logfile}"
      return 0
    fi
    if [[ ${attempts} -lt ${max_attempts} ]] && grep -qi "no such container" "${logfile}"; then
      log "WARN: compose hit 'No such container' race — retrying converge"
      rm -f "${logfile}"
      sleep 5
      continue
    fi
    log "ERROR: compose converge failed on attempt ${attempts}"
    cat "${logfile}" >&2 || true
    rm -f "${logfile}"
    return 1
  done
}

# Verify every expected compose service has at least one running container and that no
# container is stuck in Created/Exited after a converge (issue #831).
verify_stack_running() {
  local expected_services running_services created exited missing

  # awk (unlike grep -v) exits 0 even when it selects no rows, so these pipelines
  # don't trigger set -e pipefail aborts when the stack is healthy.
  expected_services="$(${COMPOSE} config --services 2>/dev/null \
    | awk '$1 != "flyway" {print}' | sort -u)"
  if [[ -z "${expected_services}" ]]; then
    log "ERROR: could not enumerate expected compose services"
    return 1
  fi

  created="$(${COMPOSE} ps -a --format '{{.Service}}\t{{.State}}' 2>/dev/null \
    | awk 'tolower($2) == "created" {print $1}' | sort -u)"
  if [[ -n "${created}" ]]; then
    log "ERROR: services stuck in Created state: $(echo "${created}" | tr '\n' ' ')"
    return 1
  fi

  exited="$(${COMPOSE} ps -a --format '{{.Service}}\t{{.State}}' 2>/dev/null \
    | awk 'tolower($2) ~ /^(exited|dead)$/ && $1 != "flyway" {print $1}' | sort -u)"
  if [[ -n "${exited}" ]]; then
    log "ERROR: services in Exited/Dead state: $(echo "${exited}" | tr '\n' ' ')"
    return 1
  fi

  running_services="$(${COMPOSE} ps --format '{{.Service}}' 2>/dev/null | sort -u)"
  missing="$(comm -23 <(echo "${expected_services}") <(echo "${running_services}"))"
  if [[ -n "${missing}" ]]; then
    log "ERROR: expected services not running: $(echo "${missing}" | tr '\n' ' ')"
    return 1
  fi

  log "Verified all expected services are running"
  return 0
}

main() {
TAG="${1:?Usage: deploy.sh <image-tag>}"

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

# 3b. Topology transition guard. A compose PROFILE stops a service from being STARTED; it does not
# stop one that is already running. So on the first grid deploy of a box still running the
# standalone, `selenium-chrome` keeps holding 127.0.0.1:4444 and the hub fails to bind — and the
# half-created hub is left RUNNING WITH NO NETWORK ATTACHED, which presents as "hub unhealthy,
# 0 nodes registered" (nodes cannot resolve `selenium-hub`) rather than as a port error. Evict the
# standalone first so the transition is clean. Live-verified during the 2026-07-27 cutover.
if [[ "${SELENIUM_TOPOLOGY}" == "grid" ]] && docker ps --format '{{.Names}}' | grep -qx "selenium-chrome"; then
  log "Grid topology: evicting the running standalone selenium-chrome so the hub can bind 4444"
  docker rm -f selenium-chrome >/dev/null 2>&1 || log "WARN: could not remove selenium-chrome (continuing)"
fi
# The SAME transition in reverse, which is the rollback path (`SELENIUM_TOPOLOGY=standalone`) and so
# runs when something is already wrong. Compose only removes the hub/nodes as orphans during the
# `up` in step 7, and while both exist the hub still answers to the `selenium-chrome` network alias
# — so that name would resolve to two containers and half the workers would drive a hub with no
# nodes. Take the Grid down first, by compose (it knows the project-prefixed replica names).
if [[ "${SELENIUM_TOPOLOGY}" != "grid" ]] && docker ps --format '{{.Names}}' | grep -qx "selenium-hub"; then
  log "Standalone topology: removing the Grid (hub + nodes) so the standalone can bind 4444"
  docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.grid.yml \
    rm -sf selenium-hub selenium-node-chrome selenium-node-debug >/dev/null 2>&1 \
    || log "WARN: could not remove the grid containers (continuing)"
fi

# 4. Pull the exact app image tag (+ any updated third-party images).
log "Pulling images for IMAGE_TAG=${TAG}"
${COMPOSE} pull

# 5. Migrations before the flip — the new web code may depend on the new schema. Flyway is
#    idempotent (repair + migrate, baselineOnMigrate).
#
#    Ordering note (2026-07-31): the Celery drain used to run HERE, before migrations and the flip,
#    which meant the web cutover waited on it. Measured on v0.113.0: drain 482s (the full timeout,
#    blocking on ONE task) and the flip itself 22s — so 96% of a ~9-minute deploy was the web tier
#    waiting for workers it does not depend on. The drain now runs AFTER the flip, immediately
#    before the worker recreate it actually protects (issue #549's contract is unchanged: nothing
#    recreates a worker until its in-flight tasks have drained).
#
#    This does mean migrations now run while OLD workers are still processing. That is the same
#    risk the OLD order already accepted for the web tier — the drain never drained web_api, so the
#    old FastAPI code has always served traffic against the new schema during a deploy. The standing
#    requirement is unchanged and applies to workers too: migrations must be backward-compatible
#    (expand/contract) — add columns/tables, never rename or drop in the same release as the code
#    that stops using them.
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
  # No serving color to fall back behind (first cutover) — legacy full rollback. This recreates the
  # workers, so it has to drain first, exactly like the success path (issue #549).
  if [[ -n "${PREV_TAG}" ]]; then
    log "Rolling back to ${PREV_TAG}"
    git checkout --quiet "${PREV_TAG}" 2>/dev/null || git checkout --quiet "tags/${PREV_TAG}" || true
    export IMAGE_TAG="${PREV_TAG}"
    persist_image_tag "${PREV_TAG}"
    drain_workers
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
log "Web tier is LIVE on ${TAG} — the rest of this deploy is workers and is not user-facing."

# Persist the tag baseline NOW, while the serving tier is live on the new tag. A later
# worker-tier failure is a partial deploy, but IMAGE_TAG / .last_good_tag must match what is
# actually running or the next deploy starts from a stale baseline (issue #831).
echo "${TAG}" > "${LAST_GOOD_FILE}"
persist_image_tag "${TAG}"

# 6b. Only NOW enter maintenance mode and drain: stop beat so no new schedule fires, pause dispatch,
#     cancel each worker's queue consumers, then wait for what is already running (video generation,
#     commenting loops, DM sweeps) to finish before the recreate below kills them mid-flight
#     (issue #549). Everything from here on is invisible to users — the site is already on the new
#     code — so a slow drain costs deploy duration, not availability.
drain_workers

# 7. Converge the rest of the stack on the new tag (workers, beat, the standby color). The active
#    color and the edge are already at their target state, so this doesn't touch routing.
log "Recreating remaining services"
if ! converge_stack; then
  log "ERROR: worker/standby converge failed — stack left partially deployed"
  maint end || true
  exit 1
fi

if ! verify_stack_running; then
  log "ERROR: stack verification failed — at least one expected service is not running"
  maint end || true
  exit 1
fi

# 7a. Reload litellm if its config changed (compose won't recreate it on a bind-mount edit alone).
if [[ "${LITELLM_RESTART}" == "1" ]]; then
  log "litellm config changed vs ${PREV_TAG:-<none>} — restarting litellm to reload it"
  ${COMPOSE} restart litellm || log "WARN: litellm restart failed (continuing)"
fi

# 7b. Healthy on the new tag — lift the pause and restore consumers.
maint end || log "WARN: could not clear maintenance mode (pause TTL will expire it)"

# 8. Prune old artifacts.
log "Deploy of ${TAG} OK. Pruning old images/build cache (>168h)."
docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true
docker builder prune -af --filter "until=168h" >/dev/null 2>&1 || true
log "Done."
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
