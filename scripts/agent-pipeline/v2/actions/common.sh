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
# Linuxbrew goes LAST so it only adds commands (graft, #2039) and never shadows a system one.
export PATH="/home/lem/.local/bin:/usr/local/bin:/usr/bin:/bin:/home/linuxbrew/.linuxbrew/bin"

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

# v2_owner_answered <issue|pr> <number> <answer-id> -> 0 when <answer-id> IS the newest reply from
# a TRUSTED author after the latest Decision Comment: the owner, or an account with
# admin/maintain/write on this repo (`comment_author_trusted` in lib/guards.sh — the owner ruling on
# comment authority), judged on a FRESH lookup — or a LEMD_TRUSTED_ANSWER_LOGINS login, which never
# includes the App (`answer_author_trusted`, the same rule as the daemon's `answers.read_thread`).
#
# This is the AUTHORSHIP half of an un-park, re-asked at execution time and BOUND to the comment
# the daemon routed. The daemon classified that reply (`lemd/answers.py`) from a permission cache
# up to 15 minutes old; finding "some" trusted reply here is not enough — if the routed author lost
# access, an older trusted comment would otherwise vouch for their answer. Refused: an empty
# answer id, a routed comment that is gone, or a newest trusted reply that is a different comment.
#
# The walk is `answers.parse`'s, newest first, so the two name the same comment:
#   * a Decision Comment closes the window only if its author may ask — the App's slug, the owner,
#     or a trusted author (a boundary can only NARROW the window, so the untyped slug is safe here);
#     anyone else's is an ordinary comment;
#   * replies carrying the agent trailer, or from the App's slug, never answer;
#   * an untrusted reply is skipped, never terminal — a bot commenting after the answer (codecov
#     posts on every push) cannot bury it.
#
# An answer id of the form `review:<id>` is an owner APPROVAL (`answers.approval`), re-checked by
# `v2_owner_approved` instead — through this one function, so both un-park paths keep a single gate.
v2_owner_answered() {
  local kind="$1" n="$2" want="${3:-}" rows id login url dec agent candidate="" asked=0
  if [ -z "$want" ]; then
    log "TRUST: #$n — no answer id routed with this un-park; refusing."
    return 1
  fi
  case "$want" in review:*) v2_owner_approved "$kind" "$n" "${want#review:}"; return $? ;; esac
  rows="$(gh "$kind" view "$n" --repo "$SLUG" --json comments --jq '
    (.comments // []) | reverse | .[]
    | "\(.id // .url // "")|\(.author.login // "")|\(.url // .html_url // "")|\(if ((.body // "") | test("Human decision needed"; "i")) then 1 else 0 end)|\(if ((.body // "") | test("Generated with \\[Claude Code\\]")) then 1 else 0 end)"' 2>/dev/null)" || rows=""
  while IFS='|' read -r id login url dec agent; do
    [ -n "$id$login" ] || continue
    login="$(typed_answer_login "$login" "$url")"
    if [ "$dec" = 1 ]; then
      if [ -n "$login" ] && { comment_login_names_app "$login" || answer_author_trusted "$login"; }; then
        asked=1; break
      fi
      continue
    fi
    [ -z "$candidate" ] || continue
    [ "$agent" = 1 ] && continue
    [ -n "$login" ] || continue
    comment_login_names_app "$login" && continue
    answer_author_trusted "$login" || continue
    candidate="$id"
  done <<< "$rows"
  # Unreadable is a refusal, as everywhere else in this file — and so is an unreadable permission.
  if [ "$asked" != 1 ] || [ -z "$candidate" ]; then
    log "TRUST: #$n — no readable reply from a trusted author after the Decision Comment; refusing."
    return 1
  fi
  if [ "$candidate" != "$want" ]; then
    log "TRUST: #$n — the routed answer $want is not the newest trusted reply ($candidate); refusing."
    return 1
  fi
  return 0
}

# v2_owner_approved pr <number> <review-id> -> 0 when <review-id> IS the owner approval that answers
# the latest Decision Comment as `ok` RIGHT NOW (`v2_owner_approval` names it). Issues refuse
# outright: approvals exist only on pull requests.
v2_owner_approved() {
  local kind="$1" n="$2" want="${3:-}"
  if [ "$kind" != "pr" ] || [ -z "$want" ]; then
    log "TRUST: #$n — an approval answers only a pull request; refusing."
    return 1
  fi
  v2_owner_approval "$n" || return 1
  if [ "$V2_APPROVAL" != "$want" ]; then
    log "TRUST: #$n — review $want is not the approval answering the menu now (${V2_APPROVAL}); refusing."
    return 1
  fi
  return 0
}

# v2_owner_approval <pr> -> 0 and sets V2_APPROVAL=<review-id> when an owner approval answers the
# PR's latest Decision Comment as `ok` — `answers.approval`'s rules, asked at execution time:
#   * the approver's NEWEST submitted review, read over REST so its author is TYPED: a `User` who is
#     the owner ($ASSIGNEE) or a LEMD_TRUSTED_ANSWER_LOGINS login — never a Bot, never the App;
#   * state APPROVED and commit_id equal to the PR's head RIGHT NOW (a push since makes it stale);
#   * submitted after the latest Decision Comment from someone who may ask;
#   * NO actionable trusted reply (an option answer or a directive, `answers.verdict_for` — twinned
#     in jq below) anywhere after that menu: an explicit answer is what routes, and the approval is
#     then only a sign-off, whichever came first;
#   * no reply at or after the approval from a trusted author OR one whose permission lookup was
#     unreadable ("hold off" typed after approving still holds; a reply we cannot attribute blocks).
# Every unreadable read refuses. Sets V2_APPROVAL="" on refusal. It is a global, not stdout, because
# `log` tees to stdout and the permission cache only lives in the calling shell.
v2_owner_approval() {
  local n="$1" head reviews rid login type state commit at newest="" rows url dec agent act
  local asked_at="" blocked=0
  V2_APPROVAL=""
  head="$(gh pr view "$n" --repo "$SLUG" --json headRefOid --jq '.headRefOid // ""' 2>/dev/null)" || head=""
  reviews="$(gh api "repos/$SLUG/pulls/$n/reviews" --paginate --slurp 2>/dev/null \
    | jq -r '[.[][] | select(.submitted_at != null)] | sort_by(.submitted_at) | reverse | .[]
        | "\(.id // "")|\(.user.login // "")|\(.user.type // "")|\(.state // "")"
          + "|\(.commit_id // "")|\(.submitted_at // "")"' 2>/dev/null)" || reviews=""
  if [ -z "$head" ] || [ -z "$reviews" ]; then
    log "TRUST: #$n — head or reviews unreadable; an approval is not an answer; refusing."
    return 1
  fi
  # The approver's newest review decides, so take the first eligible row of a newest-first list.
  while IFS='|' read -r rid login type state commit at; do
    [ "$type" = "User" ] && [ -n "$login" ] || continue
    comment_login_names_app "$login" && continue
    if [ "$(printf '%s' "$login" | tr '[:upper:]' '[:lower:]')" != \
         "$(printf '%s' "${ASSIGNEE:-}" | tr '[:upper:]' '[:lower:]')" ]; then
      trusted_answer_login "$login" || continue
    fi
    newest="$rid|$state|$commit|$at"; break
  done <<< "$reviews"
  IFS='|' read -r rid state commit at <<< "$newest"
  if [ -z "${rid:-}" ] || [ "$state" != "APPROVED" ] || [ "$commit" != "$head" ]; then
    log "TRUST: #$n — no owner's newest review is an APPROVAL of head ${head:0:7}; refusing."
    return 1
  fi
  # The window, walked as `v2_owner_answered` walks it. `act` is `answers.verdict_for(body)` in
  # (answer, directive): first non-empty line an option answer or a directive, no hold phrase
  # anywhere, not a directive ending in `?`, under MAX_BODY. Kept in step by a parity test.
  rows="$(gh pr view "$n" --repo "$SLUG" --json comments --jq '
    def act: (. // "") as $b
      | if ($b | length) == 0 or ($b | length) >= 8000 then false
        else ([$b | gsub("\r"; "") | split("\n") | .[] | select(test("\\S"))] | first // "") as $f
          | (if ($f | test("^\\s*(?:ok\\b|okay\\b|\\d+\\s*[A-Za-z](?:[^A-Za-z0-9]|$))"; "i")) then "answer"
             elif ($f | test("^\\s*(?:@claude\\b|decision:|go:)"; "i")) then "directive"
             else "" end) as $s
          | $s != ""
            and ($b | test("(don\u0027?t|do not) (merge|land|ship|start)|hold (off|on|this|it)\\b|\\bon hold\\b|\\bnot yet\\b|wait (for|on|until|till)\\b|stand ?by\\b"; "i") | not)
            and (($s == "directive" and ($b | sub("\\s+$"; "") | endswith("?"))) | not)
        end;
    (.comments // []) | reverse | .[]
    | "\(.createdAt // "")|\(.author.login // "")|\(.url // .html_url // "")"
      + "|\(if ((.body // "") | test("Human decision needed"; "i")) then 1 else 0 end)"
      + "|\(if ((.body // "") | test("Generated with \\[Claude Code\\]")) then 1 else 0 end)"
      + "|\(if (.body | act) then 1 else 0 end)"' 2>/dev/null)" || rows=""
  while IFS='|' read -r id login url dec agent act; do
    [ -n "$id$login" ] || continue
    login="$(typed_answer_login "$login" "$url")"
    if [ "$dec" = 1 ]; then
      if [ -n "$login" ] && { comment_login_names_app "$login" || answer_author_trusted "$login"; }; then
        asked_at="$id"; break
      fi
      continue
    fi
    [ "$blocked" = 1 ] && continue
    [ "$agent" = 1 ] && continue
    [ -n "$login" ] || continue
    comment_login_names_app "$login" && continue
    # ISO-8601 UTC stamps from one API compare correctly as strings; an unreadable one is "now".
    if answer_author_trusted "$login"; then
      if [ "$act" = 1 ] || [ -z "$id" ] || [[ ! "$id" < "$at" ]]; then blocked=1; fi
    elif comment_author_lookup_failed "$login"; then
      if [ -z "$id" ] || [[ ! "$id" < "$at" ]]; then blocked=1; fi
    fi
  done <<< "$rows"
  if [ -z "$asked_at" ] || [ -z "$at" ] || [[ ! "$at" > "$asked_at" ]] || [ "$blocked" = 1 ]; then
    log "TRUST: #$n — approval $rid is older than the Decision Comment, or a reply in its window outranks it; refusing."
    return 1
  fi
  V2_APPROVAL="$rid"
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
