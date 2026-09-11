#!/usr/bin/env bash
# Opt-in sweep of Claude Code's OWN worktrees under <repo>/.claude/worktrees/ (#2041).
#
# Claude Code (sessions and `isolation: "worktree"` subagents) registers a worktree there per run
# and removes it only when the session leaves it unchanged. A run that committed and opened a PR
# leaves its tree behind, and neither of the other sweeps takes it: the pipeline's
# `sweep_stale_worktrees` refuses anything outside its own `work/` by design, and the weekly
# `scripts/worktree_cleanup.sh` decides on "branch gone from origin", which is the wrong evidence
# for a tree a human may still be sitting in. Measured 2026-09-11: 33 trees, 6.1 GB, 21 of them
# clean with a merged PR.
#
# The evidence here is GitHub's, per tree: a worktree is removed ONLY when ALL of
#   1. it is clean — no tracked modifications and no untracked files;
#   2. its branch has a MERGED pull request (asked of GitHub, never inferred from local refs — this
#      repo squash-merges, so a merged branch's commits are never ancestors of main);
#   3. neither the directory nor its HEAD commit has been touched for GRACE_HOURS (default 48).
# Everything else is HELD, with the reason on its line: dirty; an OPEN PR on the branch; no merged PR
# (which covers "unpushed commits and no PR" — nothing without a merge is ever removed, whether or
# not the commits are on origin); a detached HEAD (no branch to ask about); a live process whose cwd
# is inside it; locked; or anything unreadable, INCLUDING a gh call that failed. "Could not ask" is
# never "no PR".
#
# Removal is `git worktree remove` WITHOUT --force and never a `git branch -d`: the directory and the
# registration go, `refs/heads/<branch>` stays, so a squash-merged branch's commits remain reachable.
#
# Report-only is the default and needs no gh at all; removing needs an explicit --apply. Nothing is
# scheduled — this is a tool an operator runs, which is what "opt-in" means here.
#
# Usage:
#   scripts/claude_worktree_cleanup.sh                 # DEFAULT: report only, touch nothing
#   scripts/claude_worktree_cleanup.sh --apply         # actually remove
#   scripts/claude_worktree_cleanup.sh --slug o/r      # the GitHub repo; default parsed from origin
#
# Env: GRACE_HOURS (default 48), APPLY (1 == --apply), SLUG (== --slug), PROC_ROOT (default /proc,
# a test seam for the blind-detector hold-all).
#
# Exit codes, pinned like the sibling's: 0 swept (held trees and a NEEDS-A-HUMAN list are reports,
# not errors); 1 a removal this run decided on failed; 2 usage error — nothing was examined.

# Deliberately NOT `set -e`: one stubborn tree must not abort the sweep mid-loop, silently.
set -uo pipefail

GRACE_HOURS="${GRACE_HOURS:-48}"
APPLY="${APPLY:-0}"
SLUG="${SLUG:-}"
PROC_ROOT="${PROC_ROOT:-/proc}"

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --slug) shift; SLUG="${1:-}"; [ -n "$SLUG" ] || { echo "--slug needs owner/repo" >&2; exit 2; } ;;
    -h|--help) awk 'NR>1{if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

MAIN="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || {
  echo "not inside a git repository" >&2; exit 2; }
MAIN="$(dirname "$MAIN")"
ROOT="$MAIN/.claude/worktrees"
INVOKED_FROM="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

echo "[$(stamp)] claude-worktree sweep starting: root=$ROOT grace=${GRACE_HOURS}h"

# --- What GitHub can be asked ----------------------------------------------------------------
# Without a working gh and a repo to ask about, no tree can produce the merged-PR evidence, so every
# candidate holds as pr-unreadable. Said out loud once here rather than 33 times below.
GH_OK=1
if ! command -v gh >/dev/null 2>&1; then
  echo "HOLD-ALL: gh is not installed — no tree can show a merged PR, nothing is removable" >&2
  GH_OK=0
fi
if [ -z "$SLUG" ]; then
  # ssh (git@github.com:o/r.git) and https (https://github.com/o/r) forms; `.git` stripped after
  # the match, because ERE has no lazy quantifier to keep it out of the capture.
  origin_url="$(git -C "$MAIN" remote get-url origin 2>/dev/null || true)"
  SLUG="$(printf '%s' "${origin_url%/}" | sed -nE 's#^.*github\.com[:/]([^/]+/[^/]+)$#\1#p')"
  SLUG="${SLUG%.git}"
fi
if [ -z "$SLUG" ]; then
  echo "HOLD-ALL: cannot resolve the GitHub repo (pass --slug owner/repo) — nothing is removable" >&2
  GH_OK=0
fi

# --- Live-process detector, same fail-closed self-test as worktree_cleanup.sh -----------------
PROC_CWDS=""
PROC_UNREADABLE=0
for p in "$PROC_ROOT"/[0-9]*; do
  if cwd="$(readlink "$p/cwd" 2>/dev/null)"; then
    PROC_CWDS+="$cwd"$'\n'
  else
    PROC_UNREADABLE=$((PROC_UNREADABLE + 1))
  fi
done
ACTIVE_CWDS="$(printf '%s' "$PROC_CWDS" | sort -u)"
SELF_CWD="$(readlink "$PROC_ROOT/$$/cwd" 2>/dev/null || true)"
if [ -z "$SELF_CWD" ] || ! printf '%s\n' "$ACTIVE_CWDS" | grep -qxF -- "$SELF_CWD"; then
  echo "HOLD-ALL: live-process detector is blind (cannot read /proc) — removals disabled" >&2
  APPLY=0
fi

MODE="dry-run (report only — pass --apply to remove)"
[ "$APPLY" = "1" ] && MODE="apply"
echo "[$(stamp)] mode: $MODE repo: ${SLUG:-unresolved}"

[ "$APPLY" = "1" ] && git -C "$MAIN" worktree prune

NOW="$(date +%s)"
GRACE_SECONDS=$((GRACE_HOURS * 3600))

removed=0; failed=0; skipped=0; outside=0; prunable=0
held_dirty=0; held_open=0; held_nomerge=0; held_fresh=0; held_active=0
held_detached=0; held_locked=0; held_unreadable=0; held_pr_unreadable=0
DIRTY_PATHS=()

# pr_states <branch> -> every PR state GitHub holds for that head, one per line; rc 1 = could not ask.
pr_states() {
  [ "$GH_OK" = "1" ] || return 1
  gh pr list --repo "$SLUG" --head "$1" --state all --limit 20 --json state --jq '.[].state' 2>/dev/null
}

# The `-` sentinel and substr() slicing are the same two parsing hazards worktree_cleanup.sh
# documents: bash `read` collapses a run of tabs, and a path may contain a space.
while IFS=$'\t' read -r path ref detached locked; do
  branch="${ref#refs/heads/}"
  [ "$ref" = "-" ] && branch="(detached)"

  case "$path" in
    "$ROOT"/*) ;;
    *) outside=$((outside+1)); continue ;;   # the main checkout and every other sweep's trees
  esac
  if [ "$path" = "$INVOKED_FROM" ]; then
    echo "SKIP  invoked-from     $path"; skipped=$((skipped+1)); continue
  fi
  if [ "$locked" = "1" ]; then
    echo "HELD  locked           $path ($branch)"; held_locked=$((held_locked+1)); continue
  fi
  if [ ! -d "$path" ]; then
    echo "WOULD PRUNE            $path (directory no longer exists)"; prunable=$((prunable+1)); continue
  fi
  case "$ACTIVE_CWDS" in
    *"$path"*) echo "HELD  active process   $path ($branch)"; held_active=$((held_active+1)); continue ;;
  esac

  status="$(git -C "$path" status --porcelain 2>/dev/null)"
  if [ $? -ne 0 ]; then
    echo "HELD  unreadable       $path ($branch)"; held_unreadable=$((held_unreadable+1)); continue
  fi
  if [ -n "$status" ]; then
    echo "HELD  uncommitted      $path ($(printf '%s\n' "$status" | wc -l) entries, branch $branch)"
    DIRTY_PATHS+=("$path")
    held_dirty=$((held_dirty+1)); continue
  fi

  # "Not modified for 48h" is the NEWER of the directory's own mtime and its HEAD commit. The
  # directory mtime alone misses an edit deep inside the tree, and a fresh commit is the clearest
  # sign a human or agent is still there.
  dir_mtime="$(stat -c %Y "$path" 2>/dev/null || echo "$NOW")"
  head_time="$(git -C "$path" log -1 --format=%ct 2>/dev/null || echo "$NOW")"
  newest="$dir_mtime"; [ "$head_time" -gt "$newest" ] && newest="$head_time"
  if [ "$((NOW - newest))" -lt "$GRACE_SECONDS" ]; then
    echo "HELD  within grace     $path (touched < ${GRACE_HOURS}h ago, branch $branch)"
    held_fresh=$((held_fresh+1)); continue
  fi

  if [ "$detached" = "1" ]; then
    echo "HELD  detached         $path (no branch, so no PR to ask about)"
    held_detached=$((held_detached+1)); continue
  fi

  if ! states="$(pr_states "$branch")"; then
    echo "HELD  pr-unreadable    $path ($branch — could not ask GitHub; unreadable is never 'no PR')"
    held_pr_unreadable=$((held_pr_unreadable+1)); continue
  fi
  if printf '%s\n' "$states" | grep -qx OPEN; then
    echo "HELD  open-pr          $path ($branch has an open PR)"
    held_open=$((held_open+1)); continue
  fi
  if ! printf '%s\n' "$states" | grep -qx MERGED; then
    echo "HELD  no-merged-pr     $path ($branch: no merged PR, so its commits may exist only here)"
    held_nomerge=$((held_nomerge+1)); continue
  fi

  if [ "$APPLY" != "1" ]; then
    echo "WOULD REMOVE           $path ($branch, PR merged)"
    removed=$((removed+1)); continue
  fi

  # NO --force and NO `git branch -d/-D`, ever — see the header. git refuses a tree that turned
  # dirty between the status check and here, which is the second gate behind protection 1.
  if out="$(git -C "$MAIN" worktree remove "$path" 2>&1)"; then
    echo "REMOVED                $path ($branch, PR merged)"
    removed=$((removed+1))
  else
    echo "FAILED                 $path — $(printf '%s' "$out" | head -1)" >&2
    failed=$((failed+1))
  fi
done < <(git -C "$MAIN" worktree list --porcelain | awk '
  /^worktree /{p=substr($0, 10); b=""; det=0; lock=0}
  /^branch /{b=substr($0, 8)}
  /^detached/{det=1}
  /^locked/{lock=1}
  /^$/{if(p!=""){printf "%s\t%s\t%d\t%d\n", p, (b==""?"-":b), det, lock; p=""}}
  END{if(p!="")printf "%s\t%s\t%d\t%d\n", p, (b==""?"-":b), det, lock}
')

[ "$APPLY" = "1" ] && git -C "$MAIN" worktree prune

verb="removed"; [ "$APPLY" = "1" ] || verb="would-remove"
echo
echo "[$(stamp)] claude-worktree sweep: mode=$([ "$APPLY" = 1 ] && echo apply || echo dry-run)" \
     "$verb=$removed failed=$failed skipped=$skipped prunable=$prunable outside-scope=$outside" \
     "held(uncommitted=$held_dirty open-pr=$held_open no-merged-pr=$held_nomerge grace=$held_fresh" \
     "active=$held_active detached=$held_detached locked=$held_locked unreadable=$held_unreadable" \
     "pr-unreadable=$held_pr_unreadable) cwd-unreadable=$PROC_UNREADABLE"
echo "[$(stamp)] remaining under $ROOT: $(git -C "$MAIN" worktree list --porcelain | grep -c "^worktree $ROOT/")"

if [ "$held_dirty" -gt 0 ]; then
  echo
  echo "NEEDS A HUMAN — $held_dirty worktree(s) hold uncommitted work and are never removed automatically:"
  for p in "${DIRTY_PATHS[@]}"; do
    echo "  $p"
    git -C "$p" status --short 2>/dev/null | head -5 | sed 's/^/    /'
    echo "    inspect:  git -C $p status"
    echo "    discard:  git worktree remove --force $p   # DESTRUCTIVE, deletes the uncommitted work"
  done
fi

[ "$failed" -gt 0 ] && exit 1
exit 0
