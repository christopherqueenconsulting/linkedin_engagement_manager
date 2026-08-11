#!/usr/bin/env bash
# ABANDON one item: the pipeline has asked this question enough times and is going to stop.
#
# Usage: abandon.sh <issue|pr> <number> <reason-slug>
#
# Reached when an item has been parked for the SAME reason `LEMD_MAX_PARK_LAPS` times. Each lap
# costs the owner a decision and the pipeline N model sessions, and the un-park resets the ledger so
# the next lap is identical to the last. A fourth identical Decision Comment has never once been the
# thing that unblocked anything.
#
# THIS NEVER CLOSES ANYTHING. Closing an issue or a PR is a judgement about the work; this is only a
# statement about the pipeline's ability to make progress on it. The two are not the same, and
# conflating them would let a loop in the runner quietly discard someone's issue.
#
# The one thing that matters more than stopping is being SEEN to stop. An item that silently stops
# asking is worse than one that asks too often, so this assigns the owner, labels it, says exactly
# how many laps it ran, and names both ways out.
set -uo pipefail
V2_ACTION="abandon"
# shellcheck disable=SC1091
. "$(dirname "$0")/common.sh"

KIND="${1:-}"; NUMBER="${2:-}"; REASON="${3:-unknown}"
[ -n "$KIND" ] && [ -n "$NUMBER" ] || { echo "usage: abandon.sh <issue|pr> <number> <reason>" >&2; exit 2; }

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would abandon $KIND #$NUMBER (parked repeatedly for '$REASON')."
  exit 0
fi

# A missing label makes `gh --add-label` fail the WHOLE edit, silently (#1228) — which for this
# action would mean the item is never labelled, never assigned, and the abandon is invisible. That
# is the one failure mode this action cannot have, so create it first; `|| true` because a label
# that already exists is the normal case.
gh label create --repo "$SLUG" "agent:abandoned" --color "b60205" \
  --description "The pipeline stopped asking: parked repeatedly for the same reason" \
  >/dev/null 2>&1 || true

log "ABANDONING $KIND #$NUMBER — parked for '$REASON' too many times; the pipeline stops asking."

if [ "$KIND" = "pr" ]; then
  # Draft and disarm for the same reason `park.sh` does: an abandoned PR must not be able to merge
  # on a gate that clears later, and a draft can hold neither auto-merge nor a queue entry.
  gh pr ready "$NUMBER" --repo "$SLUG" --undo >/dev/null 2>&1
  gh pr merge "$NUMBER" --repo "$SLUG" --disable-auto >/dev/null 2>&1
  gh pr edit "$NUMBER" --repo "$SLUG" \
    --add-label "agent:abandoned" --add-label "needs-human" \
    --remove-label "agent:working" --remove-label "agent:revise" \
    --remove-label "agent:depfix" --remove-label "agent:docfix" >/dev/null 2>&1
  gh pr edit "$NUMBER" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
else
  gh issue edit "$NUMBER" --repo "$SLUG" \
    --add-label "agent:abandoned" --add-label "needs-human" \
    --remove-label "agent:ready" --remove-label "agent:working" >/dev/null 2>&1
  gh issue edit "$NUMBER" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
fi

# `needs-human` STAYS on. The item is still the owner's to decide about, and dropping the hold would
# take it out of every "what is waiting on me" query they already run.
BODY="🛑 **The pipeline is going to stop asking about this one.**

It has been parked for \`${REASON}\` repeatedly. Each time, the un-park reset the run budget and the
work came back to the same place — so asking the same question again is not going to be what moves
it. Rather than post a fourth identical Decision Comment, it is now labelled \`agent:abandoned\` and
no automated lane will pick it up.

**Nothing has been closed.** That is your call, not the runner's.

Two ways forward:
- **Restart it** — remove the \`agent:abandoned\` label. The lap history is cleared and it goes back
  on the queue with a clean budget. Worth doing if the underlying cause has actually changed.
- **Close it** — if the approach is wrong, closing is the honest outcome and costs nothing further.

If it keeps coming back after a genuine fix, that is worth a bug report about the lane itself rather
than another retry."

if [ "$KIND" = "pr" ]; then
  gh pr comment "$NUMBER" --repo "$SLUG" --body "$BODY" >/dev/null 2>&1
else
  gh issue comment "$NUMBER" --repo "$SLUG" --body "$BODY" >/dev/null 2>&1
fi

posthog_capture "lemd_item_abandoned" "agent-pipeline" "{\"kind\":\"$KIND\",\"number\":$NUMBER,\"reason\":\"$REASON\"}" 2>/dev/null || true
exit 0
