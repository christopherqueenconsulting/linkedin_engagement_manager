#!/usr/bin/env bash
# PARK one item for the owner. The escalation half of every budget in v2.
#
# Usage: park.sh <issue|pr> <number> <reason-slug> [one-line detail]
#
# Order is load-bearing and was decided by an incident, not by taste:
#
#   draft FIRST, then --disable-auto, then labels, then ONE comment.
#
# A draft cannot hold auto-merge or a merge-queue entry, so drafting first means a concurrently
# running v1 tick that re-arms fails CLOSED instead of quietly undoing the park (finding H6 —
# v1's merge lanes take no flock, so this race is real during coexistence). Doing it last would
# leave a window in which the PR is labelled parked and still armed.
#
# The comment is keyed on the head SHA so a park is stated once per head rather than once per
# observation: #1067 posted 561 identical comments over 47 hours by getting exactly this wrong.
# Un-parking is the owner's, through the existing answer lane — this action never un-parks.
set -uo pipefail
V2_ACTION="park"
# shellcheck disable=SC1091
. "$(dirname "$0")/common.sh"

KIND="${1:-}"; NUMBER="${2:-}"; REASON="${3:-unspecified}"; DETAIL="${4:-}"
[ -n "$KIND" ] && [ -n "$NUMBER" ] || { echo "usage: park.sh <issue|pr> <number> <reason> [detail]" >&2; exit 2; }

# Parking is a REFUSAL to keep working, so it is the one action that does not need permission to
# proceed — but it still must not spam a thread the owner already holds.
if v2_hold_present "$KIND" "$NUMBER"; then
  log "$KIND #$NUMBER is already held — park is a no-op."; exit 0
fi

SHA="none"
if [ "$KIND" = "pr" ]; then
  SHA="$(gh pr view "$NUMBER" --repo "$SLUG" --json headRefOid --jq .headRefOid 2>/dev/null)"
  [ -n "$SHA" ] || SHA="unknown"
fi

KEY="$BASE/state/v2park-$KIND-$NUMBER.sha"
if [ "$(cat "$KEY" 2>/dev/null)" = "$SHA" ]; then
  log "$KIND #$NUMBER already parked at ${SHA:0:8} — not repeating."; exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would park $KIND #$NUMBER ($REASON)."; exit 0
fi

log "PARKING $KIND #$NUMBER — $REASON. ${DETAIL:-}"
if [ "$KIND" = "pr" ]; then
  gh pr ready "$NUMBER" --repo "$SLUG" --undo >/dev/null 2>&1
  gh pr merge "$NUMBER" --repo "$SLUG" --disable-auto >/dev/null 2>&1
  gh pr edit "$NUMBER" --repo "$SLUG" \
    --add-label "needs-human" --add-label "agent:blocked" \
    --remove-label "agent:working" >/dev/null 2>&1
  gh pr edit "$NUMBER" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
  # Mirror the hold onto the issue. An issue still reading `agent:working` while its PR is parked
  # makes the two threads disagree about whether the work is in flight, and the answer lane reads
  # the issue.
  ISS="$(issue_for_pr "$NUMBER" 2>/dev/null)"
  [ -n "$ISS" ] && gh issue edit "$ISS" --repo "$SLUG" \
    --add-label "needs-human" --add-label "agent:blocked" \
    --remove-label "agent:working" >/dev/null 2>&1
else
  gh issue edit "$NUMBER" --repo "$SLUG" \
    --add-label "needs-human" --add-label "agent:blocked" \
    --remove-label "agent:working" >/dev/null 2>&1
  gh issue edit "$NUMBER" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
fi

BODY="🛑 **Human decision needed** — the pipeline has parked this ${KIND} (\`${REASON}\`).

${DETAIL:-The automated lane exhausted its budget for this work and stopped rather than repeating a run that has not been converging.}

### 1. How should we proceed?
- **A. Try again as-is** — I have fixed the underlying problem.
- **B. Rebase onto latest main and retry** — main has moved since this was last attempted.  ✅ *recommended*
- **C. Close this** — I will handle it manually.

**My recommendation: \`1B\`.** Reply with the question number and letter — e.g. \`1B\` — or \`ok\` to take the recommendation. (A bare \`B\` is NOT recognised as an answer; the answer lane needs the number.)"

if [ "$KIND" = "pr" ]; then
  gh pr comment "$NUMBER" --repo "$SLUG" --body "$BODY" >/dev/null 2>&1
else
  gh issue comment "$NUMBER" --repo "$SLUG" --body "$BODY" >/dev/null 2>&1
fi
printf '%s\n' "$SHA" > "$KEY"
exit 0
