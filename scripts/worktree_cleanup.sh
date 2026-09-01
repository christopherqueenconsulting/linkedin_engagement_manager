#!/usr/bin/env bash
# Worktree registration sweep (host cron, runs as `lem`) — the worktree half of docs/branch-cleanup.md.
#
# CLAUDE.md mandates one git worktree per agent, and delete_branch_on_merge + stale-branches.yml
# clean up the BRANCHES those agents push. Nothing was cleaning up the worktree REGISTRATIONS, so
# they accumulated: 292 registered on 2026-09-01, of which 261 were merged or branch-gone.
#
# A registration is removable when its branch is gone from origin (merged, or swept by the weekly
# branch cron) or already an ancestor of origin/main. Removing one deletes the working directory and
# the registration, never the local branch ref — committed work stays reachable through refs/heads.
#
# Four protections, all fail-closed. A worktree is HELD, never removed, when it:
#   1. is the primary checkout, is locked, or is the tree this script was invoked from;
#   2. has uncommitted changes — tracked modifications OR untracked files (git worktree remove is
#      called WITHOUT --force, so a dirty tree refuses at the git layer too, belt and braces);
#   3. has a live process whose cwd is inside it — a running agent lane;
#   4. has a branch tip younger than GRACE_HOURS (default 48, matching the branch cron's grace).
#   5. is a detached HEAD that is not an ancestor of origin/main — it has no branch ref to
#      outlive the removal, so only reachability from main proves the commits survive.
#
# Held-because-dirty trees are reported for a human decision; the script never resolves them itself.
#
# Usage:
#   scripts/worktree_cleanup.sh              # sweep and remove
#   scripts/worktree_cleanup.sh --dry-run    # report only, touch nothing
#   scripts/worktree_cleanup.sh --no-fetch   # skip `git fetch --prune` (offline / rate-limited)
#
# Env: GRACE_HOURS (default 48).

set -uo pipefail

GRACE_HOURS="${GRACE_HOURS:-48}"
DRY_RUN=0
FETCH=1

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-fetch) FETCH=0 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

MAIN="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || {
  echo "not inside a git repository" >&2; exit 2; }
MAIN="$(dirname "$MAIN")"
INVOKED_FROM="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

if [ "$FETCH" = "1" ]; then
  git -C "$MAIN" fetch --prune origin >/dev/null 2>&1 || echo "warning: fetch failed, using stale remote refs" >&2
fi

git -C "$MAIN" worktree prune

# Snapshot every live process cwd once; a worktree containing one is an active agent lane.
ACTIVE_CWDS="$(for p in /proc/[0-9]*; do readlink "$p/cwd" 2>/dev/null; done | sort -u)"

NOW="$(date +%s)"
GRACE_SECONDS=$((GRACE_HOURS * 3600))

removed=0; held_dirty=0; held_active=0; held_fresh=0; held_live=0; failed=0

# `-` stands in for an empty branch field: bash read treats runs of tabs as one separator, so an
# empty field would silently shift every column after it (detached trees mis-read as branch "1").
while IFS=$'\t' read -r path ref detached locked; do
  branch="${ref#refs/heads/}"
  [ "$ref" = "-" ] && branch="(detached)"

  [ "$path" = "$MAIN" ] && continue
  [ "$path" = "$INVOKED_FROM" ] && continue
  if [ "$locked" = "1" ]; then echo "HELD  locked          $path"; continue; fi
  [ -d "$path" ] || continue  # already handled by the prune above

  case "$ACTIVE_CWDS" in
    *"$path"*) echo "HELD  active process  $path"; held_active=$((held_active+1)); continue ;;
  esac

  status="$(git -C "$path" status --porcelain 2>/dev/null)" || { echo "HELD  unreadable      $path"; continue; }
  if [ -n "$status" ]; then
    echo "HELD  uncommitted     $path ($(printf '%s\n' "$status" | wc -l) entries, branch $branch)"
    held_dirty=$((held_dirty+1)); continue
  fi

  head="$(git -C "$path" rev-parse HEAD 2>/dev/null)"
  tip_date="$(git -C "$MAIN" show -s --format=%ct "$head" 2>/dev/null || echo 0)"
  if [ "$((NOW - tip_date))" -lt "$GRACE_SECONDS" ]; then
    echo "HELD  within grace    $path (tip < ${GRACE_HOURS}h old)"
    held_fresh=$((held_fresh+1)); continue
  fi

  on_origin=0
  if [ "$detached" != "1" ] && git -C "$MAIN" rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null; then
    on_origin=1
  fi
  merged=0
  git -C "$MAIN" merge-base --is-ancestor "$head" origin/main 2>/dev/null && merged=1

  if [ "$on_origin" = "1" ] && [ "$merged" = "0" ]; then
    echo "HELD  live branch     $path ($branch still on origin, not merged)"
    held_live=$((held_live+1)); continue
  fi

  # A detached HEAD has no branch ref to survive the removal, so "branch gone from origin" proves
  # nothing about it — only reachability from origin/main does. Unmerged detached trees are HELD.
  if [ "$detached" = "1" ] && [ "$merged" = "0" ]; then
    echo "HELD  detached unmerged $path (HEAD not an ancestor of origin/main)"
    held_live=$((held_live+1)); continue
  fi

  if [ "$DRY_RUN" = "1" ]; then
    echo "WOULD REMOVE          $path ($branch)"
    removed=$((removed+1)); continue
  fi

  if out="$(git -C "$MAIN" worktree remove "$path" 2>&1)"; then
    echo "REMOVED               $path ($branch)"
    removed=$((removed+1))
  else
    echo "FAILED                $path — $(printf '%s' "$out" | head -1)" >&2
    failed=$((failed+1))
  fi
done < <(git -C "$MAIN" worktree list --porcelain | awk '
  /^worktree /{p=$2; b=""; det=0; lock=0}
  /^branch /{b=$2}
  /^detached/{det=1}
  /^locked/{lock=1}
  /^$/{if(p!=""){printf "%s\t%s\t%d\t%d\n", p, (b==""?"-":b), det, lock; p=""}}
  END{if(p!="")printf "%s\t%s\t%d\t%d\n", p, (b==""?"-":b), det, lock}
')

git -C "$MAIN" worktree prune

echo
echo "worktree sweep: removed=$removed failed=$failed held(uncommitted=$held_dirty active=$held_active grace=$held_fresh live-branch=$held_live)"
echo "remaining registrations: $(git -C "$MAIN" worktree list --porcelain | grep -c '^worktree ')"
[ "$held_dirty" -gt 0 ] && echo "NOTE: uncommitted worktrees are never removed automatically — resolve them by hand."
exit 0
