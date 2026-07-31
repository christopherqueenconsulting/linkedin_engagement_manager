#!/usr/bin/env bash
# PostHog server-side telemetry for the agent pipeline — best-effort, NEVER breaks a tick.
# Reuses the EXISTING PostHog project keys from /opt/lem/.env (copied into secrets.env by setup)
# — this is the SAME PostHog project the LEM app + SPA report to, so agent-pipeline events sit
# alongside llm_call / $ai_generation / browser events for the same accounts. No second project.
#
# Usage:
#   posthog_capture "<event>" "<distinct_id>" "<json-properties>"   # emits one event
#   posthog_metric ...                                              # (reserved)
#
# Properties are a JSON object string. We add common props (lem_component, environment, repo,
# execution_id, worker_id, timestamp) on every event so dashboards can filter by lane/model/tier.
# Failures (no key, network, PostHog down) are logged to the tick log and swallowed — telemetry
# must never cost the pipeline a run.
BASE="${BASE:-/home/lem/agent-pipeline}"
[ -f "$BASE/secrets.env" ] && . "$BASE/secrets.env"

POSTHOG_API_KEY="${POSTHOG_API_KEY:-}"
POSTHOG_HOST="${POSTHOG_HOST:-https://us.i.posthog.com}"
# Strip a trailing slash so "$POSTHOG_HOST/capture" is always well-formed.
POSTHOG_HOST="${POSTHOG_HOST%/}"

# Shared identity + run context. execution_id is per-tick-process (set by tick.sh); worker_id is
# the slot number; both propagate into every event for tracing a run across its lifecycle.
LEM_COMPONENT="${LEM_COMPONENT:-agent-pipeline}"
ENVIRONMENT="${ENVIRONMENT:-production}"
REPO="${REPO:-christopherqueenconsulting/linkedin_engagement_manager}"
EXECUTION_ID="${EXECUTION_ID:-}"
WORKER_ID="${WORKER_ID:-}"
_TICK_LOG="${_TICK_LOG:-/dev/null}"
_ph_log() { echo "[posthog] $*" >> "$_TICK_LOG" 2>/dev/null || true; }

posthog_capture() {
  local event="$1" distinct_id="$2" props="${3:-{\}}"
  [ -n "$POSTHOG_API_KEY" ] || { _ph_log "no POSTHOG_API_KEY — skipping '$event'"; return 0; }
  [ -n "$event" ] || return 0
  distinct_id="${distinct_id:-agent-pipeline}"
  # Merge common props into the caller's props. python3 is on the PATH (LEM uses it); if absent,
  # fall back to sending props as-is — never fail the event over a merge.
  local body
  if command -v python3 >/dev/null 2>&1; then
    body="$(python3 -c '
import json, sys, os
event, did, props = sys.argv[1], sys.argv[2], sys.argv[3]
common = {
    "lem_component": os.environ.get("LEM_COMPONENT","agent-pipeline"),
    "environment":   os.environ.get("ENVIRONMENT","production"),
    "repo":          os.environ.get("REPO","christopherqueenconsulting/linkedin_engagement_manager"),
    "execution_id":  os.environ.get("EXECUTION_ID",""),
    "worker_id":     os.environ.get("WORKER_ID",""),
}
try:
    p = json.loads(props) if props else {}
    if not isinstance(p, dict): p = {}
except Exception:
    p = {}
p = {**common, **p}
print(json.dumps({"api_key": os.environ["POSTHOG_API_KEY"], "event": event,
                  "distinct_id": did, "properties": p}))
' "$event" "$distinct_id" "$props" 2>/dev/null)" || body=""
  fi
  if [ -z "$body" ]; then
    # Minimal fallback: send the caller's props verbatim wrapped with api_key/event/distinct_id.
    body="{\"api_key\":\"$POSTHOG_API_KEY\",\"event\":\"$event\",\"distinct_id\":\"$distinct_id\",\"properties\":$props}"
  fi
  # Fire-and-forget with a short timeout. 2s is enough for PostHog's capture endpoint; a slow
  # PostHog must not stall a tick. Errors go to the tick log only.
  curl -fsS --max-time 3 -X POST "$POSTHOG_HOST/capture" \
    -H "content-type: application/json" -d "$body" >/dev/null 2>&1 \
    || _ph_log "capture failed for '$event' (host=$POSTHOG_HOST)"
  return 0
}