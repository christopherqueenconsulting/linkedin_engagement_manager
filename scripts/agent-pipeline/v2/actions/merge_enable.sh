#!/usr/bin/env bash
# Arm auto-merge on ONE pull request, once.
#
# Usage: merge_enable.sh <pr> [head_sha]
#
# v1 asked the merge queue the same question every five minutes and re-armed on every answer: 45
# requests on #1120, 154 on #1067. v2 arms once and then waits for events, so the only thing this
# action must get right is not arming a PR that should not be armed, and not arming it repeatedly.
#
# Three guards, in this order:
#   1. Trust + hold, re-read at execution time (common.sh explains why nothing is cached).
#   2. Budget BEFORE the request. v1's order was inverted — it re-enqueued at :820 and checked the
#      budget at :849, so exceeding the budget never actually stopped the re-enqueue. The budget is
#      keyed on the HEAD here, and only here: the queue judges heads, so a new head is genuinely a
#      new question. (Everywhere else a per-head key would let the agent refill its own meter.)
#   3. The same `br-*` flock a v1 tick takes. v1's merge lanes take no flock today, which is how a
#      concurrent tick could re-arm auto-merge on a PR another slot was parking.
#
# Backoff is 2s/8s/30s on failure and then stop — never a sleep loop. A merge that will not arm
# after three tries is a state to observe, not a state to keep hammering.
set -uo pipefail
V2_ACTION="merge_enable"
# shellcheck disable=SC1091
. "$(dirname "$0")/common.sh"

PR="${1:-}"; SHA="${2:-}"
[ -n "$PR" ] || { echo "usage: merge_enable.sh <pr> [head_sha]" >&2; exit 2; }

MERGE_MAX_REQUEUES="${MERGE_MAX_REQUEUES:-3}"

if v2_paused; then log "PAUSED — not arming auto-merge on #$PR."; exit "$EX_TRUST"; fi
if v2_hold_present pr "$PR"; then log "PR #$PR carries a human hold — not arming."; exit "$EX_TRUST"; fi
if ! v2_trust_ok pr "$PR"; then log "TRUST refused auto-merge on #$PR."; exit "$EX_TRUST"; fi

[ -n "$SHA" ] || SHA="$(gh pr view "$PR" --repo "$SLUG" --json headRefOid --jq .headRefOid 2>/dev/null)"
# An unreadable head has no stable budget key, so "once per head" would silently become "every
# pass" — the #1120 shape exactly. Refuse instead.
[ -n "$SHA" ] || { log "PR #$PR head unreadable — refusing to arm auto-merge."; exit "$EX_TRUST"; }

# A draft cannot hold auto-merge. Arming one is not an error GitHub reports usefully, and a parked
# PR is a draft — so this is also what stops v2 undoing its own park.
if [ "$(gh pr view "$PR" --repo "$SLUG" --json isDraft --jq .isDraft 2>/dev/null)" = "true" ]; then
  log "PR #$PR is a draft — not arming auto-merge."; exit "$EX_TRUST"
fi

BRANCH="$(gh pr view "$PR" --repo "$SLUG" --json headRefName --jq .headRefName 2>/dev/null)"
[ -n "$BRANCH" ] || { log "PR #$PR branch unreadable — refusing."; exit "$EX_TRUST"; }
if ! claim_branch "$BRANCH"; then log "branch $BRANCH claimed elsewhere — deferring merge arm."; exit "$EX_BUSY"; fi

SPENT="$(ledger_count pr "$PR" merge "$SHA" 2>/dev/null || echo 0)"
if [ "$SPENT" -ge "$MERGE_MAX_REQUEUES" ]; then
  log "MERGE BUDGET: #$PR head ${SHA:0:8} has been enqueued $SPENT/$MERGE_MAX_REQUEUES times — refusing (the daemon parks)."
  exit "$EX_BUDGET"
fi
ATTEMPT="$(ledger_charge pr "$PR" merge "$SHA" 2>/dev/null || echo 1)"

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would enable auto-merge on #$PR at ${SHA:0:8} (attempt $ATTEMPT/$MERGE_MAX_REQUEUES)."
  exit 0
fi

for delay in 0 2 8 30; do
  [ "$delay" -gt 0 ] && sleep "$delay"
  if gh pr merge "$PR" --repo "$SLUG" --auto --squash >/dev/null 2>&1; then
    log "auto-merge armed on #$PR at ${SHA:0:8} (attempt $ATTEMPT/$MERGE_MAX_REQUEUES)."
    exit 0
  fi
done
log "could not arm auto-merge on #$PR after 3 retries — leaving it for the next observation."
exit 1
