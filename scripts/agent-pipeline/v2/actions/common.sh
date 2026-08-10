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

# The same PATH v1 exports. `claude` lives in ~/.local/bin, which is on the interactive shell's PATH
# via .bashrc and on cron's only because tick.sh sets it explicitly — and is on a systemd unit's
# PATH not at all. The daemon spawns these actions, so without this line every agent run reached
# `claude` and got rc=127: budget charged, worktree built, and nothing to show for it. A missing
# interpreter is not a lane failure, but it looks exactly like one in the outcome log.
export PATH="/home/lem/.local/bin:/usr/local/bin:/usr/bin:/bin"

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
# ---------------------------------------------------------------------------- exit vocabulary
# Distinct codes so the daemon can tell "refused" from "failed" from "the agent ran and lost".
# Collapsing them would make a trust refusal look like a flaky run and get retried forever.
EX_TRUST=70        # provenance refused or unreadable — never retry without a human
EX_BUDGET=71       # this (item, mode) has spent its runs — the daemon parks
EX_BUSY=72         # another claimant holds the branch (a v1 tick, or another v2 action)
EX_SETUP=73        # worktree / environment could not be prepared
export EX_TRUST EX_BUDGET EX_BUSY EX_SETUP

if command -v gh_app_export_token >/dev/null 2>&1 && gh_app_export_token; then
  :
elif [ -n "${AGENT_GH_TOKEN:-}" ]; then
  export GH_TOKEN="$AGENT_GH_TOKEN"
else
  # NO SILENT FALLBACK. Without this branch `GH_TOKEN` was simply never exported, and every `gh`
  # call in the run used whatever ambient credential the `lem` user holds — and
  # `~/.config/gh/hosts.yml` on this box holds the OWNER's oauth token, which carries `workflow`
  # scope. That is the authority CLAUDE.md and docs/contribution-security.md call "the hard
  # control": the one thing that makes an agent rewriting `.github/workflows/**` impossible rather
  # than merely reviewable. Since AGENT_GH_TOKEN was revoked (#1311 §2) the fall-through is the
  # PERMANENT state whenever the App token cannot be minted, so the hole is not hypothetical.
  #
  # EX_SETUP, not EX_TRUST: a token that could not be minted is an environment that is not ready,
  # and the daemon retries that on the next observation. A trust refusal would park the item for
  # six hours over what is usually a one-minute gap between the mint timer and an expiring token.
  log "FATAL: no pipeline credential — the App token could not be minted and AGENT_GH_TOKEN is"
  log "       unset. REFUSING rather than falling through to the ambient owner login, which"
  log "       carries workflow scope. Check lem-gh-token.timer, GH_APP_ID and the private key."
  exit "$EX_SETUP"
fi

# ...and the allowlist entry that identity requires, exactly as tick.sh derives it. This is NOT a
# widening of the gate: the runner re-applies `agent:ready` itself in two places — the stale-claim
# reaper and the lane that requeues an issue after the owner answers its Decision Comment — and
# MODE=phasefix files follow-up issues carrying it. Under the PAT those writes were the OWNER's and
# passed; under the App they are the bot's.
#
# Omitting it here did not fail safe, it failed SILENTLY DIFFERENT: v1 accepted those issues and v2
# refused them, so the same item read as workable to one runner and untrusted to the other, and the
# rollback path would have quietly resurrected work v2 had written off. Measured live on issue
# #1292 within minutes of cutover. The outsider path this allowlist exists to close — a stranger's
# issue labelled by a non-allowlisted actor — is unchanged.
if [ "${GH_APP_IDENTITY_ACTIVE:-0}" = "1" ] && [ -n "${GH_APP_BOT_LOGIN:-}" ]; then
  case " $AGENT_LABEL_TRUSTED_ACTORS " in
    *" $GH_APP_BOT_LOGIN "*) ;;
    *) AGENT_LABEL_TRUSTED_ACTORS="$AGENT_LABEL_TRUSTED_ACTORS $GH_APP_BOT_LOGIN" ;;
  esac
fi


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

# v2_owner_answered <issue|pr> <number> -> 0 when the newest reply after the latest Decision
# Comment was written by the OWNER.
#
# This is the AUTHORSHIP half of an un-park, re-asked at execution time. The daemon classified the
# reply (`lemd/answers.py`); what matters to the trust boundary is not what it said but WHO said
# it, and a webhook-prompted decision can be minutes stale. Deliberately the same three rules the
# parser uses, so the two cannot disagree: only comments AFTER the newest Decision Comment count,
# Decision Comments themselves are skipped, and agent-authored comments are excluded by their
# trailer as well as their login — under the PAT identity the agent posted AS the owner.
v2_owner_answered() {
  local kind="$1" n="$2" who
  # NOTE the owner filter inside the jq. Selecting the newest eligible comment and THEN comparing
  # its author would let any bot bury the answer simply by commenting after it — codecov posts on
  # every push. `answers.parse` skips non-owner comments rather than stopping at them, and this
  # must make the same choice or the daemon and the action disagree about the same thread.
  who="$(gh "$kind" view "$n" --repo "$SLUG" --json comments --jq '
    def isdecision: ((.body // "") | test("Human decision needed"; "i"));
    def isagent:    ((.body // "") | test("Generated with \\[Claude Code\\]"));
    ((.comments // []) | to_entries) as $c
    | (($c | map(select(.value | isdecision)) | last | .key) // -1) as $di
    | [ $c[] | select(.key > $di) | .value
        | select((.author.login // "") == "'"$ASSIGNEE"'")
        | select(isdecision | not) | select(isagent | not) ]
    | (last // empty) | (.author.login // "")' 2>/dev/null)"
  # Unreadable is a refusal, as everywhere else in this file.
  [ "$who" = "$ASSIGNEE" ] || { log "TRUST: #$n — no readable owner reply after the Decision Comment; refusing."; return 1; }
  return 0
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
