#!/usr/bin/env bash
# UN-PARK one item: the owner answered a Decision Comment, so the hold they placed comes off.
#
# Usage: unpark.sh <issue|pr> <number> [answer-id]
#
# The daemon has already read the thread and classified the reply (`lemd/answers.py`); this action
# performs the side effects, and they are ported from v1's `route_owner_answer` unchanged because
# each one was added by an incident:
#
#   * UN-DRAFT FIRST, before the labels. `park.sh` converts a PR to a draft, and a draft can neither
#     enter the merge queue nor hold auto-merge. #1236 sat "CLEAN with 21 green checks" for hours
#     because an earlier pass removed the labels and never ran `gh pr ready`. Order matters the
#     other way round too: the moment labels flip the pipeline may act on this PR, and it must act
#     on a READY one.
#   * ROUTE THE WORK, NOT THE THREAD. The answer may land on either thread — a Decision Comment
#     raised before a PR exists has only the issue. If a PR exists the work goes to the revise lane;
#     if it does not, the issue goes back on the ready queue and MODE=start reads the answer.
#   * RESET EVERY BUDGET. The owner's answer is the statement "the world changed — try again". Both
#     halves are needed: the merge markers, or a merge-parked PR re-parks on its very next merge
#     attempt, and the run ledger, or an exhausted selfreview/docfix lane never dispatches again.
#     That second half is what the six PRs parked `*_exhausted` on 2026-08-10 were waiting for.
#   * AN ISSUE WHOSE PR ALREADY MERGED STAYS PARKED. Re-queueing it redoes shipped work; that state
#     means the issue needs closing, which is a human call.
set -uo pipefail
V2_ACTION="unpark"
# shellcheck disable=SC1091
. "$(dirname "$0")/common.sh"

KIND="${1:-}"; NUMBER="${2:-}"; ANSWER_ID="${3:-}"; PARK_REASON="${4:-}"
[ -n "$KIND" ] && [ -n "$NUMBER" ] || { echo "usage: unpark.sh <issue|pr> <number> [answer-id] [park-reason]" >&2; exit 2; }

# WHICH LANE the work goes back to depends on WHY it stopped, and getting this wrong is expensive.
#
# `agent:revise` outranks the checks/review/merge ladder in `decide()`, so it is only correct when
# there is genuinely feedback to apply. A PR parked because a budget ran out has none: the un-park's
# own `ledger_reset` IS the fix. Routing it to revise dispatched an agent to act on instructions
# that did not exist, which spent the 2-run revise budget on an empty lane and re-parked the PR
# within the hour — measured on #1289 and #1296, both of which were green with a fresh review the
# whole time and merged the moment the label came off.
#
# So: only a park that ASKED A QUESTION routes to revise. Everything else goes back to
# `agent:working` and lets the ladder re-decide, which is what it is for. An unknown reason takes
# the cheap branch deliberately — a wrong `working` costs one observation, a wrong `revise` costs
# two model sessions and a re-park.
case "$PARK_REASON" in
  needs_human) LANE="agent:revise" ;;
  *)           LANE="agent:working" ;;
esac

# Resolve BOTH threads once. Which one the owner replied on is not what decides the routing.
if [ "$KIND" = "pr" ]; then
  TPR="$NUMBER"; TISS="$(issue_for_pr "$NUMBER" 2>/dev/null)"
else
  TISS="$NUMBER"; TPR="$(pr_for_issue "$NUMBER" 2>/dev/null)"
fi

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would un-park $KIND #$NUMBER (pr='${TPR:-none}' issue='${TISS:-none}', answer=${ANSWER_ID:-unknown})."
  exit 0
fi

# ---- clear the local park/budget state for whichever item the work lives on -------------------
# `ledger_reset` comes from lib/ledger.sh (sourced by common.sh) and is the ONE writer of the run
# budget; the rest are v1's marker files, removed by path because their clear helpers live in
# tick.sh and are not sourceable. A stale v2park key is not cosmetic: park.sh dedupes on it, so
# leaving it means the NEXT park on this head stays silent.
clear_item_state() {  # clear_item_state pr|issue <number>
  ledger_reset "$1" "$2" 2>/dev/null || true
  rm -f "$BASE/state/v2park-$1-$2.sha" 2>/dev/null || true
  [ "$1" = "pr" ] || return 0
  rm -f "$BASE/state/mergeattempt-$2" \
        "$BASE/state/mergestall-$2.count" \
        "$BASE/state/mergestallcycle-$2.count" \
        "$BASE/state/mergepark-$2.sha" 2>/dev/null || true
  rm -f "$BASE/state/lanepark-$2-"* 2>/dev/null || true
}

if [ -n "$TPR" ]; then
  # The trust boundary, re-asked at execution time exactly as every dispatch asks it. The daemon
  # has already established that the OWNER wrote the answer — that is the authority to un-park —
  # and this is the separate question of whether the work itself may run. Unreadable refuses.
  if ! v2_trust_ok pr "$TPR"; then
    log "TRUST refused un-park of PR #$TPR — leaving it parked."; exit "$EX_TRUST"
  fi
  log "UN-PARKING PR #$TPR (answered on $KIND #$NUMBER${TISS:+, issue #$TISS}; parked '${PARK_REASON:-unknown}') — routing to $LANE."
  gh pr ready "$TPR" --repo "$SLUG" >/dev/null 2>&1
  if ! gh pr edit "$TPR" --repo "$SLUG" --add-label "$LANE" \
        --remove-label "needs-human" --remove-label "agent:blocked" \
        --remove-label "agent:merge-parked" >/dev/null 2>&1; then
    # A failed label write is a FAILURE, not a refusal: the hold is still on and the answer must not
    # be recorded as spent. Non-zero sends the item back for re-observation, which tries again.
    log "FAILED to relabel PR #$TPR — leaving it parked so the answer is not consumed."
    exit 1
  fi
  # Mirror onto the issue so the two threads stop disagreeing about whether work is in flight.
  [ -n "$TISS" ] && gh issue edit "$TISS" --repo "$SLUG" --add-label "agent:working" \
    --remove-label "needs-human" --remove-label "agent:blocked" >/dev/null 2>&1
  clear_item_state pr "$TPR"
  [ -n "$TISS" ] && clear_item_state issue "$TISS"
  exit 0
fi

# ---- no PR: the issue itself goes back on the queue, unless its work already shipped ----------
DONEPR="$(gh issue view "$TISS" --repo "$SLUG" --json closedByPullRequestsReferences 2>/dev/null \
          | jq -r '[(.closedByPullRequestsReferences // [])[] | .number] | (first // empty)')"
if [ -n "$DONEPR" ] \
   && [ "$(gh pr view "$DONEPR" --repo "$SLUG" --json state --jq .state 2>/dev/null)" = "MERGED" ]; then
  log "Issue #$TISS answered, but PR #$DONEPR is already MERGED — leaving parked (needs closing, not redoing)."
  exit 0
fi

# `agent:ready` GRANTS work, so this path carries the stricter half of the boundary — but it asks
# the two questions that are actually load-bearing HERE, which is not what `v2_trust_ok` asks.
#
# `v2_trust_ok issue` requires an allowlisted actor to have applied `agent:ready`. For an un-park
# there may never have been one: issue #1360 was authored AND answered by the owner, was never in
# the work queue, and was refused with "no readable 'agent:ready' labeler" — so the owner's answer
# to their own issue did nothing, silently, which is the exact complaint this whole lane exists to
# fix. The label is a proxy for "who put this in the queue"; on this path the authorising act is
# the ANSWER, so ask about the answer.
#
# Both halves are required. `author_trusted` keeps a stranger's issue from becoming agent work, and
# `v2_owner_answered` keeps anyone but the owner from being the one who released it.
if ! author_trusted "$TISS" || ! v2_owner_answered issue "$TISS"; then
  log "TRUST refused un-park of issue #$TISS — leaving it parked."; exit "$EX_TRUST"
fi

log "UN-PARKING issue #$TISS — no PR yet, returning it to the ready queue."
if ! gh issue edit "$TISS" --repo "$SLUG" --add-label "agent:ready" \
      --remove-label "needs-human" --remove-label "agent:blocked" >/dev/null 2>&1; then
  log "FAILED to relabel issue #$TISS — leaving it parked so the answer is not consumed."
  exit 1
fi
clear_item_state issue "$TISS"
exit 0
