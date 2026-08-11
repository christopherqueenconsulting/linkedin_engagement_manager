#!/usr/bin/env bash
# DISARM auto-merge on a PR that a human just put a hold on.
#
# Usage: disarm.sh <pr-number>
#
# `park.sh` disables auto-merge as step two of parking, and for a long time that was the only place
# it happened. So a hold the PIPELINE applied was safe and a hold a HUMAN applied was not: adding
# `needs-human` to an armed PR is the natural "stop, I want to look at this" gesture, the daemon
# honoured it by refusing to act — and GitHub merged the PR anyway the moment its gate cleared. The
# hold was respected by everything except the thing that actually merges.
#
# Two guards, both about not disarming something that is not ours:
#
#   * The hold is re-read HERE. The daemon's snapshot can be minutes old, and a hold that has since
#     been lifted means the owner wants this PR to land — taking its arm off then would be us
#     fighting them.
#   * The attempt is keyed on the HEAD SHA and metered, exactly as `merge_enable.sh` meters arming.
#     `gh pr merge --disable-auto` exits 0 on a PR that was never armed, so without a bound a
#     mis-read of `autoMergeRequest` would re-dispatch this action for ever.
set -uo pipefail
V2_ACTION="disarm"
# shellcheck disable=SC1091
. "$(dirname "$0")/common.sh"

PR="${1:-}"
[ -n "$PR" ] || { echo "usage: disarm.sh <pr-number>" >&2; exit 2; }

MAX_DISARMS="${MAX_DISARMS:-3}"

if v2_paused; then log "PAUSED — refusing to disarm PR #$PR."; exit "$EX_TRUST"; fi

# Unreadable labels count as HELD (see v2_hold_present), so an unreadable read disarms. That is the
# safe direction here and the opposite of everywhere else in this file set: disarming a PR nobody
# held costs one re-arm by the merge lane, while failing to disarm a held one costs a merge.
if ! v2_hold_present pr "$PR"; then
  log "PR #$PR no longer carries a hold — not ours to disarm."
  exit 0
fi

SHA="$(gh pr view "$PR" --repo "$SLUG" --json headRefOid --jq .headRefOid 2>/dev/null)"
[ -n "$SHA" ] || SHA="unknown"

SPENT="$(ledger_count pr "$PR" disarm "$SHA")"
if [ "$SPENT" -ge "$MAX_DISARMS" ]; then
  log "PR #$PR — $SPENT disarm attempts at ${SHA:0:8} already; refusing to loop."
  exit "$EX_BUDGET"
fi

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would disarm auto-merge on PR #$PR at ${SHA:0:8}."
  exit 0
fi

ledger_charge pr "$PR" disarm "$SHA" >/dev/null
log "DISARMING auto-merge on PR #$PR (${SHA:0:8}) — a human hold is present."
if gh pr merge "$PR" --repo "$SLUG" --disable-auto >/dev/null 2>&1; then
  exit 0
fi
# Non-zero is a real failure, so the daemon re-observes and this runs again — bounded by the meter
# above. A PR that was never armed exits 0 from gh, so this branch means the call itself failed.
log "FAILED to disarm PR #$PR — the hold stands but auto-merge may still be armed."
exit 1
