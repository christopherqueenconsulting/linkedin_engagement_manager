#!/usr/bin/env bash
# Run-budget ledger — the ONE counter for "how many agent runs has this item consumed in this
# mode". Charged at DISPATCH, before the run starts, so a timeout, a pre-commit death, and a
# max-turns exhaustion all consume budget. The counters it replaces charged only on success
# shapes: `rev-list --count origin/main..` counted EVERY branch commit as a fix attempt (a
# feature branch with 4 commits of legitimate work arrived pre-exhausted), and the depfix/docfix
# `--grep='Co-Authored-By: Claude'` count was free for any run that died before committing —
# which is exactly the run a budget exists to stop repeating.
#
# Storage: one TSV per item at $BASE/state/ledger/<kind>-<number>.tsv, rows:
#   mode \t count \t last_charge_ts \t reset_key
# The reset_key makes a budget renewable by an event the AGENT CANNOT PRODUCE (an owner answer
# id, a Dependabot rebase) — pass a new key and the count restarts at 1. Keys must never be
# derived from agent-producible facts like the head SHA: agents push heads, so a per-head key
# refills the meter on the agent's own commits (finding H1 of the v2 design review). `-` means
# "no key": the budget spans the item's lifetime until ledger_reset.
#
# Single-writer by construction: every charging lane holds the per-branch claim_branch flock,
# so two slots never charge the same item concurrently. Writes are tmp+mv anyway.
#
# Shared byte-for-byte with the v2 daemon (its actions source this same file), so v1→v2 cutover
# and rollback carry budget state in both directions with no translation.

BASE="${BASE:-/home/lem/agent-pipeline}"
LEDGER_DIR="$BASE/state/ledger"

_ledger_file() { echo "$LEDGER_DIR/$1-$2.tsv"; }   # <kind> <number>

# ledger_count <kind> <number> <mode> [reset_key] -> current count (0 when absent, garbage, or
# the stored reset_key differs from the given one — a rotated key IS the reset).
ledger_count() {
  local f row c k key="${4:--}"
  f="$(_ledger_file "$1" "$2")"
  [ -f "$f" ] || { echo 0; return 0; }
  row="$(awk -F'\t' -v m="$3" '$1==m{print; exit}' "$f" 2>/dev/null)"
  [ -n "$row" ] || { echo 0; return 0; }
  c="$(printf '%s' "$row" | cut -f2)"
  k="$(printf '%s' "$row" | cut -f4)"
  [ "$k" = "$key" ] || { echo 0; return 0; }
  case "$c" in ''|*[!0-9]*) echo 0 ;; *) echo "$c" ;; esac
}

# ledger_charge <kind> <number> <mode> [reset_key] -> the new count (i.e. THIS attempt's number).
ledger_charge() {
  local f n key="${4:--}"
  mkdir -p "$LEDGER_DIR"
  f="$(_ledger_file "$1" "$2")"
  n="$(( $(ledger_count "$1" "$2" "$3" "$key") + 1 ))"
  {
    [ -f "$f" ] && awk -F'\t' -v m="$3" '$1!=m' "$f" 2>/dev/null
    printf '%s\t%s\t%s\t%s\n' "$3" "$n" "$(date +%s)" "$key"
  } > "$f.new" && mv "$f.new" "$f"
  echo "$n"
}

# ledger_reset <kind> <number> [mode] — drop one mode's row, or the whole item when no mode is
# given. Called on the owner's un-park ("the world changed — try again") and on item close.
ledger_reset() {
  local f
  f="$(_ledger_file "$1" "$2")"
  [ -f "$f" ] || return 0
  if [ -n "${3:-}" ]; then
    awk -F'\t' -v m="$3" '$1!=m' "$f" 2>/dev/null > "$f.new" && mv "$f.new" "$f"
  else
    rm -f "$f"
  fi
}

# ledger_refund <kind> <number> <mode> [reset_key] -> 0 when a run was given back, 1 when refused.
#
# A run the DAEMON ended did not measure whether the work converges, and charging for it taxes every
# in-flight item on every restart. `RC_VANISHED` (an orphan adopted after a bounce, or crash
# recovery) is that case: 16 daemon restarts in one night on 2026-08-10 each cost the whole backlog.
#
# A deadline kill is NOT refunded, deliberately. A run that consumed its entire wall-clock ceiling
# and produced nothing is precisely what a budget exists to stop repeating, and refunding it is an
# unbounded loop. The rule lives in `policy.refundable()`; this function only performs it.
#
# ONE refund per (item, mode), tracked in a 5th TSV field. That cap is what defuses the remaining
# risk: a process that reliably dies early gets exactly one extra attempt and then converges as
# before. Extra fields are safe for both readers — `ledger_count` reads field 2 and field 4, and
# `policy.ledger_count` slices parts[0..3] — and a test asserts that rather than assuming it.
ledger_refund() {
  local f row c k r key="${4:--}"
  f="$(_ledger_file "$1" "$2")"
  [ -f "$f" ] || return 1
  row="$(awk -F'\t' -v m="$3" '$1==m{print; exit}' "$f" 2>/dev/null)"
  [ -n "$row" ] || return 1
  c="$(printf '%s' "$row" | cut -f2)"; k="$(printf '%s' "$row" | cut -f4)"
  r="$(printf '%s' "$row" | cut -f5)"
  case "$c" in ''|*[!0-9]*) return 1 ;; esac
  case "$r" in ''|*[!0-9]*) r=0 ;; esac
  [ "$k" = "$key" ] || return 1            # a rotated key already reset the count
  [ "$r" -lt 1 ] || return 1               # one refund per (item, mode), ever
  [ "$c" -gt 0 ] || return 1               # nothing to give back
  {
    awk -F'\t' -v m="$3" '$1!=m' "$f" 2>/dev/null
    printf '%s\t%s\t%s\t%s\t%s\n' "$3" "$(( c - 1 ))" "$(date +%s)" "$key" "$(( r + 1 ))"
  } > "$f.new" && mv "$f.new" "$f"
}
