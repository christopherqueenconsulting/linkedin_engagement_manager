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
