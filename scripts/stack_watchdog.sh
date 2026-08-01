#!/usr/bin/env bash
# LEM stack watchdog — the layer that catches "the container exists but was never started".
#
# WHY THIS EXISTS (the v0.118.0 outage):
#   Docker healthchecks were already defined on all seven app services and did NOT help. A container
#   in `Created` has never run, so its healthcheck never executes; `restart:` policies don't fire
#   either (they act on containers that ran and exited). The worker converge aborted mid-deploy,
#   left celery_beat + every worker in `Created`, and the API stayed green — `/health` returns a
#   static literal and never touches Celery. Automation was dead for four hours with nothing red.
#
# WHY IT LIVES ON THE HOST, NOT IN A BEAT TASK:
#   celery_beat was itself among the dead. A watchdog inside the thing it watches cannot report its
#   own outage. This runs from a systemd timer, outside the container set, and depends on nothing in
#   the stack — it talks to PostHog and SendGrid over plain HTTPS so it still alerts when every
#   container on the box is down.
#
# Install: see docs/stack-watchdog.md (systemd units live in scripts/systemd/).
# Overridable: LEM_DIR, LEM_ENV_FILE, WATCHDOG_STATE_DIR, WATCHDOG_GRACE_SECONDS, WATCHDOG_HEAL,
#              WATCHDOG_ALERT_EMAIL.
set -uo pipefail

LEM_DIR="${LEM_DIR:-/opt/lem}"
ENVF="${LEM_ENV_FILE:-${LEM_DIR}/.env}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/var/lib/lem-watchdog}"
# How long a service may be down before it is worth waking someone. Deploys legitimately recreate
# the worker tier, and a converge plus image pull can run for minutes — alerting under that window
# would page on every release and train everyone to ignore it.
GRACE="${WATCHDOG_GRACE_SECONDS:-600}"
# One bounded self-heal per incident (`docker start`), then alert regardless of whether it worked.
# Bounded on purpose: a container that needs starting twice is not a blip, and a watchdog that
# retries forever silently papers over a real fault.
HEAL="${WATCHDOG_HEAL:-1}"

log() { echo "[watchdog $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
now() { date -u +%s; }

mkdir -p "$STATE_DIR" 2>/dev/null || { log "FATAL: cannot create $STATE_DIR"; exit 1; }

env_value() {  # $1 = key
  [[ -r "$ENVF" ]] || return 0
  sed -nE "s/^[[:space:]]*$1=[\"']?([^\"'#[:space:]]*).*/\1/p" "$ENVF" | tail -n1
}

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
TOPOLOGY="$(env_value SELENIUM_TOPOLOGY)"; TOPOLOGY="${TOPOLOGY:-grid}"
[[ "${TOPOLOGY,,}" == "grid" ]] && COMPOSE="$COMPOSE -f docker-compose.grid.yml"

cd "$LEM_DIR" 2>/dev/null || { log "FATAL: cannot cd $LEM_DIR"; exit 1; }

# ── Collect state ────────────────────────────────────────────────────────────────
# `config --services` is profile-filtered, so a standalone service parked behind a disabled profile
# is correctly absent. flyway is a one-shot (`compose run --rm`) and is never expected to be up.
expected="$($COMPOSE config --services 2>/dev/null | awk '$1 != "flyway" {print}' | sort -u)"
if [[ -z "$expected" ]]; then
  log "FATAL: could not enumerate compose services — is Docker up?"
  exit 1
fi

# ONE `ps -a` read. Space-separated: neither a service name nor a state contains a space.
states="$($COMPOSE ps -a --format '{{.Service}} {{.State}}' 2>/dev/null)"

# A replicated service (selenium-node-chrome runs 8) reports one row PER CONTAINER under the SAME
# service name. Last-write-wins would let seven dead nodes hide behind one healthy one, so the
# worst state a service has in the pool is the state we record for it.
declare -A state_of
while read -r svc state _; do
  [[ -n "$svc" ]] || continue
  state="${state,,}"
  if [[ -z "${state_of[$svc]:-}" || "${state_of[$svc]}" == "running" ]]; then
    state_of["$svc"]="$state"
  fi
done <<< "$states"

# ── Classify ─────────────────────────────────────────────────────────────────────
down=()          # services to alert about (down beyond the grace window)
healed=()        # services this run tried to start
recovered=()     # services that were down and are now fine

for svc in $expected; do
  st="${state_of[$svc]:-absent}"
  marker="$STATE_DIR/${svc//\//_}.down"

  if [[ "$st" == "running" ]]; then
    if [[ -f "$marker" ]]; then
      recovered+=("$svc")
      rm -f "$marker"
    fi
    continue
  fi

  # Not running. Record when we first saw it that way.
  if [[ ! -f "$marker" ]]; then
    echo "$(now) $st" > "$marker"
    log "NOTICE: ${svc} is '${st}' — starting grace window (${GRACE}s)"
    continue
  fi

  first_seen="$(awk '{print $1}' "$marker" 2>/dev/null)"
  [[ "$first_seen" =~ ^[0-9]+$ ]] || first_seen="$(now)"
  elapsed=$(( $(now) - first_seen ))
  (( elapsed < GRACE )) && continue   # still inside the deploy window

  # Past grace. One bounded self-heal, then alert either way.
  if [[ "$HEAL" == "1" ]] && ! grep -q ' healed$' "$marker" 2>/dev/null; then
    # `Created`/`Exited` is exactly what `docker start` fixes and what neither a healthcheck nor a
    # restart policy will ever touch. `up -d` is deliberately NOT used: it could fight a deploy
    # that is mid-converge.
    if [[ "$st" == "created" || "$st" == "exited" ]]; then
      log "HEAL: docker start ${svc} (down ${elapsed}s in state '${st}')"
      if $COMPOSE start "$svc" >/dev/null 2>&1; then
        healed+=("$svc")
      else
        log "HEAL FAILED: ${svc}"
      fi
      echo "$first_seen $st healed" > "$marker"
      sleep 5
      # Re-read: a heal that worked still gets reported, but as recovered rather than down.
      if [[ "$($COMPOSE ps -a --format '{{.Service}} {{.State}}' 2>/dev/null \
              | awk -v s="$svc" '$1==s {print tolower($2)}')" == "running" ]]; then
        continue
      fi
    fi
  fi
  down+=("$svc:${st}:${elapsed}s")
done

# ── Report ───────────────────────────────────────────────────────────────────────
# Nothing to say and nothing was wrong: stay silent. A watchdog that chats every 5 minutes gets
# filtered, and then it is not a watchdog.
if [[ ${#down[@]} -eq 0 && ${#healed[@]} -eq 0 && ${#recovered[@]} -eq 0 ]]; then
  exit 0
fi

summary=""
[[ ${#down[@]} -gt 0 ]]      && summary+="DOWN: ${down[*]}. "
[[ ${#healed[@]} -gt 0 ]]    && summary+="Auto-started: ${healed[*]}. "
[[ ${#recovered[@]} -gt 0 ]] && summary+="Recovered: ${recovered[*]}. "
log "$summary"

# PostHog — direct capture, no app dependency.
PH_KEY="$(env_value POSTHOG_API_KEY)"
PH_HOST="$(env_value POSTHOG_HOST)"; PH_HOST="${PH_HOST:-https://us.i.posthog.com}"
if [[ -n "$PH_KEY" ]]; then
  payload=$(cat <<JSON
{"api_key":"${PH_KEY}","event":"stack_watchdog_report","distinct_id":"lem-vps",
 "properties":{"down":"${down[*]:-}","healed":"${healed[*]:-}","recovered":"${recovered[*]:-}",
 "down_count":${#down[@]},"healed_count":${#healed[@]},"summary":"${summary}",
 "\$process_person_profile":false}}
JSON
)
  curl -s -m 15 -X POST "${PH_HOST%/}/i/v0/e/" -H 'Content-Type: application/json' \
    -d "$payload" >/dev/null 2>&1 \
    || log "WARN: PostHog capture failed (alerting continues by email)"
else
  log "WARN: POSTHOG_API_KEY unset — skipping PostHog"
fi

# Email — only for an actual outage or a heal. A pure recovery is worth an event, not an inbox.
if [[ ${#down[@]} -gt 0 || ${#healed[@]} -gt 0 ]]; then
  SG_KEY="$(env_value SENDGRID_API_KEY)"
  FROM="$(env_value SENDGRID_FROM_EMAIL)"
  TO="${WATCHDOG_ALERT_EMAIL:-$(env_value WATCHDOG_ALERT_EMAIL)}"
  TO="${TO:-$(env_value COST_ALERT_EMAIL)}"
  if [[ -n "$SG_KEY" && -n "$FROM" && -n "$TO" ]]; then
    subject="[LEM] stack watchdog: ${#down[@]} down, ${#healed[@]} auto-started"
    body="${summary}

Host: $(hostname)
Time: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Grace window: ${GRACE}s

Docker healthchecks cannot catch this class of fault: a container in 'Created' never runs, so its
healthcheck never executes and its restart policy never fires. Check the last deploy first.

  cd ${LEM_DIR} && ${COMPOSE} ps -a"
    curl -s -m 20 -X POST https://api.sendgrid.com/v3/mail/send \
      -H "Authorization: Bearer ${SG_KEY}" -H 'Content-Type: application/json' \
      -d "$(jq -n --arg to "$TO" --arg from "$FROM" --arg s "$subject" --arg b "$body" \
            '{personalizations:[{to:[{email:$to}]}],from:{email:$from},
              subject:$s,content:[{type:"text/plain",value:$b}]}')" >/dev/null 2>&1 \
      || log "WARN: SendGrid send failed"
  else
    log "WARN: SendGrid not configured (need SENDGRID_API_KEY, SENDGRID_FROM_EMAIL, WATCHDOG_ALERT_EMAIL)"
  fi
fi

# Non-zero only while something is still down, so `systemctl status` and any external check reflect
# reality. A successful self-heal exits 0 — it reports, but it is not an outstanding fault.
[[ ${#down[@]} -gt 0 ]] && exit 1
exit 0
