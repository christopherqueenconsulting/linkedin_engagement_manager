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

# menu_posted_on_thread <issue|pr> <number> -> prints "yes" when a Decision Comment is on the thread,
# "no" when the thread is readable and carries none, and "" when it could not be read.
#
# The SAME body marker `lemd/answers.py` and `v2_owner_answered` key on, by body and not by author:
# the pipeline has posted under two logins, and a menu from the PAT era is still the question the
# answer lane reads. Re-read at execution time, exactly as `v2_hold_present` re-reads the labels,
# because the daemon's snapshot can be minutes old and the one thing this path must never do is
# post a second menu.
menu_posted_on_thread() {
  local kind="$1" n="$2" count
  count="$(gh "$kind" view "$n" --repo "$SLUG" --json comments --jq '
    [ (.comments // [])[] | select((.body // "") | test("Human decision needed"; "i")) ] | length' 2>/dev/null)"
  case "$count" in
    "") printf '' ;;
    0)  printf 'no' ;;
    *)  printf 'yes' ;;
  esac
}

# Parking is a REFUSAL to keep working, so it is the one action that does not need permission to
# proceed — but it still must not spam a thread the owner already holds.
#
# `human_hold_unasked` (#1736) is the ONE reason for which "already held" is the premise rather than
# the stop: the item ARRIVED carrying `needs-human` — the triage cron, a hand-filed issue — so no
# park ever ran and no menu was ever posted, and the hold had no way out. For that reason the early
# exit inverts: a hold that has since been LIFTED means nobody is waiting on a question, so it is
# the no-op, and a hold still present goes on to ask. Exactly once, keyed on the THREAD rather than
# the head-SHA state file, because the thread is the evidence the daemon decided on: a state file
# left by a menu that never landed would otherwise silence this for ever, and one that reads as
# fresh would let a second menu through. Unreadable REFUSES — an unreadable thread is never
# evidence that no menu exists.
if [ "$REASON" = "human_hold_unasked" ]; then
  if ! v2_hold_present "$KIND" "$NUMBER"; then
    log "$KIND #$NUMBER hold lifted since the decision — nothing to ask."; exit 0
  fi
  case "$(menu_posted_on_thread "$KIND" "$NUMBER")" in
    yes) log "$KIND #$NUMBER already carries a Decision Comment — not asking twice."; exit 0 ;;
    no)  ;;
    *)   log "$KIND #$NUMBER thread unreadable — refusing to post a menu on it."; exit 0 ;;
  esac
elif v2_hold_present "$KIND" "$NUMBER"; then
  log "$KIND #$NUMBER is already held — park is a no-op."; exit 0
fi

SHA="none"
if [ "$KIND" = "pr" ]; then
  SHA="$(gh pr view "$NUMBER" --repo "$SLUG" --json headRefOid --jq .headRefOid 2>/dev/null)"
  [ -n "$SHA" ] || SHA="unknown"
fi

KEY="$BASE/state/v2park-$KIND-$NUMBER.sha"
if [ "$REASON" != "human_hold_unasked" ] && [ "$(cat "$KEY" 2>/dev/null)" = "$SHA" ]; then
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

# WHICH option carries the ✅ depends on why we stopped. `B` (rebase and retry) is right for a lane
# that ran out of budget — the default, and every park `act()` raises. It is wrong advice for the
# parks `decide()` raises (#1405): a merged linked PR means the work already shipped and a
# closed-unmerged one means the approach was turned down, and retrying either is the one thing not
# to do. An unrecognised reason keeps the retry recommendation, so this can only ever soften.
# `human_hold_unasked` (#1736) has attempted nothing, so there is nothing to rebase: `A` (as-is)
# is the honest release, and its detail says so.
REC="B"
case "$REASON" in
  work_shipped_needs_close|approach_rejected) REC="C" ;;
  human_hold_unasked) REC="A" ;;
esac
MARK=" ✅ *recommended*"
A_MARK=""; B_MARK=""; C_MARK=""
case "$REC" in
  A) A_MARK="$MARK" ;;
  C) C_MARK="$MARK" ;;
  *) B_MARK="$MARK" ;;
esac

BODY="🛑 **Human decision needed** — the pipeline has parked this ${KIND} (\`${REASON}\`).

${DETAIL:-The automated lane exhausted its budget for this work and stopped rather than repeating a run that has not been converging.}

### 1. How should we proceed?
- **A. Try again as-is** — I have fixed the underlying problem.${A_MARK}
- **B. Rebase onto latest main and retry** — main has moved since this was last attempted.${B_MARK}
- **C. Close this** — I will handle it manually.${C_MARK}

**My recommendation: \`1${REC}\`.** Reply with the question number and letter — e.g. \`1B\` — or \`ok\` to take the recommendation. (A bare \`B\` is NOT recognised as an answer; the answer lane needs the number.)"

if [ "$KIND" = "pr" ]; then
  gh pr comment "$NUMBER" --repo "$SLUG" --body "$BODY" >/dev/null 2>&1
else
  gh issue comment "$NUMBER" --repo "$SLUG" --body "$BODY" >/dev/null 2>&1
fi
printf '%s\n' "$SHA" > "$KEY"
exit 0
