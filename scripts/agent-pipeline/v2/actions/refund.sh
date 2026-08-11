#!/usr/bin/env bash
# Give back one run charged to an item the DAEMON ended.
#
# Usage: refund.sh <issue|pr> <number> <mode>
#
# A separate script, and a very small one, because `lib/ledger.sh` is the single writer of the run
# budget by construction — v1 and v2 both charge through it, so the file format survives a rollback
# in both directions. Reaching into the TSV from Python would make that untrue.
#
# The decision of WHETHER to refund lives in `policy.refundable()`. This performs it.
set -uo pipefail
V2_ACTION="refund"
# shellcheck disable=SC1091
. "$(dirname "$0")/common.sh"

KIND="${1:-}"; NUMBER="${2:-}"; MODE="${3:-}"
[ -n "$KIND" ] && [ -n "$NUMBER" ] && [ -n "$MODE" ] || {
  echo "usage: refund.sh <issue|pr> <number> <mode>" >&2; exit 2; }

if ledger_refund "$KIND" "$NUMBER" "$MODE"; then
  log "refunded one $MODE run to $KIND #$NUMBER (the daemon ended it, not the agent)."
else
  # Refused is the normal case on a second interrupt, and it is not an error: the cap exists so a
  # run that reliably dies early cannot spin. Logged at the same level so the ratio is readable.
  log "no $MODE refund for $KIND #$NUMBER (already refunded, or nothing charged)."
fi
exit 0
