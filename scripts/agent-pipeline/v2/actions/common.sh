#!/usr/bin/env bash
# Bootstrap every v2 action shares. Sourced, never executed.
#
# The daemon decides WHEN and WHICH; these actions decide WHETHER, and they decide it here, at
# execution time, against GitHub — never against the webhook payload that prompted the decision.
# Webhook delivery order is not guaranteed, so a delayed owner `labeled` event arriving after an
# attacker's relabel would poison anything cached from the payload. One timeline walk per dispatch
# is a single API call; it is noise against the reads v2 removed, and it is the whole boundary.
#
# Fail-closed is the rule at every step: unreadable provenance refuses, a missing guard function
# refuses, an unresolvable identity refuses. A v2 action that cannot prove it may act does nothing
# and says so, because the alternative — acting on an unreadable answer — is exactly what #1082 did.
set -uo pipefail

BASE="${BASE:-/home/lem/agent-pipeline}"
V2_DIR="$BASE/v2"

# config.env first: it names REPO/SLUG/OWNER and the trust allowlists every guard reads.
# shellcheck disable=SC1091
[ -f "$BASE/config.env" ] && . "$BASE/config.env"
# shellcheck disable=SC1091
[ -f "$BASE/secrets.env" ] && . "$BASE/secrets.env"

REPO="${REPO:-/home/lem/linkedin_engagement_manager}"
SLUG="${SLUG:-christopherqueenconsulting/linkedin_engagement_manager}"
OWNER="${OWNER:-${SLUG%%/*}}"
ASSIGNEE="${ASSIGNEE:-gitchrisqueen}"
WORKROOT="${WORKROOT:-$BASE/work}"
LOGDIR="${LOGDIR:-$BASE/logs}"
RUNBOOK="${RUNBOOK:-$BASE/RUNBOOK.md}"
TRUSTED_ASSOCIATIONS="${TRUSTED_ASSOCIATIONS:-OWNER MEMBER COLLABORATOR}"
AGENT_LABEL_TRUSTED_ACTORS="${AGENT_LABEL_TRUSTED_ACTORS:-gitchrisqueen}"
AGENT_CI_LABEL_ACTORS="${AGENT_CI_LABEL_ACTORS:-github-actions[bot]}"
DRY_RUN="${DRY_RUN:-0}"
export BASE REPO SLUG OWNER WORKROOT LOGDIR RUNBOOK DRY_RUN

mkdir -p "$LOGDIR" "$BASE/locks" "$BASE/state" "$WORKROOT"
LOG="${LOG:-$LOGDIR/v2-actions-$(date +%Y%m%d).log}"
_TICK_LOG="$LOG"
export LOG _TICK_LOG
log() { echo "[$(date '+%F %T')] [v2/${V2_ACTION:-action}] $*" | tee -a "$LOG" ; }

# Best-effort libraries: losing PostHog telemetry or a lane label degrades observability, not safety.
# shellcheck disable=SC1091
for _l in posthog labels capacity dispatch run_lane gh_app_token ledger; do
  . "$BASE/lib/$_l.sh" 2>/dev/null || true
done

# guards.sh is NOT best-effort. It is the trust boundary and the worktree lifecycle; a missing
# function returns 127, which reads as "refused" for the trust calls (safe) but as a silent failure
# for add_worktree (not safe). Refuse the whole action instead of discovering which.
# shellcheck disable=SC1091
if ! . "$BASE/lib/guards.sh"; then
  log "FATAL: cannot source lib/guards.sh — refusing to act without the trust boundary."
  exit 70
fi

# IDENTITY: the App installation token, exactly as v1 resolves it. The runner never holds the
# private key — lem-gh-token.timer (root) mints a ~1h token into state/gh-app-token and this only
# reads it. An agent that reads everything this uid can read therefore reaches a credential that
# expires within the hour, not one that never does.
if command -v gh_app_export_token >/dev/null 2>&1 && gh_app_export_token; then
  :
elif [ -n "${AGENT_GH_TOKEN:-}" ]; then
  export GH_TOKEN="$AGENT_GH_TOKEN"
fi

# ---------------------------------------------------------------------------- exit vocabulary
# Distinct codes so the daemon can tell "refused" from "failed" from "the agent ran and lost".
# Collapsing them would make a trust refusal look like a flaky run and get retried forever.
EX_TRUST=70        # provenance refused or unreadable — never retry without a human
EX_BUDGET=71       # this (item, mode) has spent its runs — the daemon parks
EX_BUSY=72         # another claimant holds the branch (a v1 tick, or another v2 action)
EX_SETUP=73        # worktree / environment could not be prepared
export EX_TRUST EX_BUDGET EX_BUSY EX_SETUP

# v2_trust_ok <kind> <number> [lane-label] -> 0 when this item may be acted on RIGHT NOW.
#
# Deliberately the same two questions v1 asks, in the same order, through the same functions:
# an ISSUE is gated on its author's standing plus the actor who applied `agent:ready`; a PR is
# gated on `pr_is_upstream` plus the actor who applied its lane label. `author_trusted` is absent
# from the PR path ON PURPOSE — Dependabot PRs are CONTRIBUTOR, so adding it "for symmetry" would
# refuse every depfix, which is the dead-on-arrival state guards.sh documents at length.
v2_trust_ok() {
  local kind="$1" n="$2" label="${3:-}"
  if [ "$kind" = "issue" ]; then
    author_trusted "$n" || return 1
    label_actor_trusted "$n" "${label:-agent:ready}" || return 1
    return 0
  fi
  pr_is_upstream "$n" || return 1
  # A PR lane with no privilege-granting label (fix / review / selfreview / rebase on a PR this
  # pipeline already owns) has nothing to attribute, so `pr_is_upstream` IS the boundary: getting a
  # branch into this repo already requires write access, a stronger statement than any association.
  [ -n "$label" ] || return 0
  label_actor_trusted "$n" "$label" || return 1
}

# v2_hold_present <kind> <number> -> 0 when a human hold is on the thread RIGHT NOW.
# Re-read at execution time because the daemon's snapshot can be minutes old, and the one label
# that must never lose a race is the owner saying stop.
v2_hold_present() {
  local kind="$1" n="$2" labels
  if [ "$kind" = "issue" ]; then
    labels="$(gh issue view "$n" --repo "$SLUG" --json labels --jq '[.labels[].name]|join(" ")' 2>/dev/null)"
  else
    labels="$(gh pr view "$n" --repo "$SLUG" --json labels --jq '[.labels[].name]|join(" ")' 2>/dev/null)"
  fi
  # Unreadable labels are NOT "no hold". Treat them as held: refusing a run costs one cycle,
  # ignoring a hold costs the owner's trust in the pause switch.
  [ -n "$labels" ] || { log "#$n labels unreadable — treating as held."; return 0; }
  case " $labels " in *" needs-human "*|*" agent:blocked "*) return 0 ;; esac
  return 1
}

# v2_paused -> 0 when the owner stopped everything. PAUSED means the same thing in both worlds; a
# pause that applied to only half a pipeline would be worse than no pause at all.
v2_paused() { [ -f "$BASE/PAUSED" ]; }
