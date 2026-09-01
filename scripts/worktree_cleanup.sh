#!/usr/bin/env bash
# Worktree registration sweep — the worktree half of docs/branch-cleanup.md.
#
# CLAUDE.md mandates one git worktree per agent, and delete_branch_on_merge + stale-branches.yml
# clean up the BRANCHES those agents push. Nothing was cleaning up the worktree REGISTRATIONS, so
# they accumulated: 292 registered on 2026-09-01, of which 261 were merged or branch-gone.
#
# A registration is removable when its branch is gone from origin (merged, or swept by the weekly
# branch cron) or already an ancestor of origin/main. Removing one deletes the working directory and
# the registration, NEVER the local branch ref — committed work stays reachable through refs/heads.
#
# Five protections, all fail-closed. A worktree is HELD, never removed, when it:
#   1. is the primary checkout, is locked, or is the tree this script was invoked from;
#   2. has uncommitted changes — tracked modifications OR untracked files (git worktree remove is
#      called WITHOUT --force, so a dirty tree refuses at the git layer too, belt and braces);
#   3. has a live process whose cwd is inside it — a running agent lane;
#   4. has a branch tip younger than GRACE_HOURS (default 48, matching the branch cron's grace);
#   5. is a detached HEAD that is not an ancestor of origin/main — it has no branch ref to
#      outlive the removal, so only reachability from main proves the commits survive.
#
# Held-because-dirty trees are reported for a human decision; the script never resolves them itself.
#
# Three conditions disable removals for the WHOLE run (report-only, nothing is deleted):
#   - the run is a dry run (the default — removing needs an explicit --apply);
#   - `git fetch --prune origin` failed, or --no-fetch was passed, so remote refs may be stale and
#     "branch is gone from origin" / "merged into origin/main" cannot be trusted;
#   - the live-process detector is blind (no readable /proc), which would turn protection 3 from
#     fail-closed into fail-open — "no process found" would really mean "cannot look".
#
# Usage:
#   scripts/worktree_cleanup.sh              # DEFAULT: report only, touch nothing
#   scripts/worktree_cleanup.sh --dry-run    # the same thing, said out loud
#   scripts/worktree_cleanup.sh --apply      # actually remove (what the weekly timer runs)
#   scripts/worktree_cleanup.sh --no-fetch   # skip `git fetch --prune`; forces report-only
#
# Env: GRACE_HOURS (default 48), APPLY (1 == --apply).

# Deliberately NOT `set -e`. This is a sweep: one unreadable or stubborn worktree must not abort the
# run and leave the rest unswept — with -e it would abort SILENTLY, mid-loop. Every per-worktree
# command below captures its own exit status and the loop continues, so failures are counted and
# summarised instead. `-u` and `pipefail` stay on.
set -uo pipefail

# 48h matches .github/workflows/stale-branches.yml + scripts/branch_cleanup.py. If that grace ever
# changes, change it in all three — a worktree outliving its branch is the whole failure mode here.
GRACE_HOURS="${GRACE_HOURS:-48}"
APPLY="${APPLY:-0}"
FETCH=1

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --no-fetch) FETCH=0 ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

MAIN="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || {
  echo "not inside a git repository" >&2; exit 2; }
MAIN="$(dirname "$MAIN")"
INVOKED_FROM="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

echo "[$(stamp)] worktree sweep starting: repo=$MAIN grace=${GRACE_HOURS}h"

# --- Conditions that disable removals for the whole run -------------------------------------
if [ "$FETCH" = "1" ]; then
  # Both the "branch is gone from origin" test and the merge-base ancestry test read LOCAL remote
  # refs. On a host that has not fetched in a week those are a stale answer to a live question, so
  # a failed fetch downgrades the run to report-only rather than deciding on old data.
  if ! git -C "$MAIN" fetch --prune origin >/dev/null 2>&1; then
    echo "HOLD-ALL: git fetch --prune origin failed — remote refs may be stale, removals disabled" >&2
    APPLY=0
  fi
else
  echo "HOLD-ALL: --no-fetch, remote refs not refreshed — removals disabled"
  APPLY=0
fi

# Snapshot every live process cwd once; a worktree containing one is an active agent lane.
ACTIVE_CWDS="$(for p in /proc/[0-9]*; do readlink "$p/cwd" 2>/dev/null; done | sort -u)"
# Fail-closed self-test for protection 3. This very process HAS a cwd, so the snapshot must contain
# it. If it does not — no procfs, hidepid=2, a container without host pids — the detector is blind
# and every "no process found" answer is really "cannot look", which would silently turn the ONE
# protection covering a running agent lane into fail-open. Hold everything instead of guessing.
SELF_CWD="$(readlink "/proc/$$/cwd" 2>/dev/null || true)"
if [ -z "$SELF_CWD" ] || ! printf '%s\n' "$ACTIVE_CWDS" | grep -qxF -- "$SELF_CWD"; then
  echo "HOLD-ALL: live-process detector is blind (cannot read /proc) — removals disabled" >&2
  APPLY=0
fi

MODE="dry-run (report only — pass --apply to remove)"
[ "$APPLY" = "1" ] && MODE="apply"
echo "[$(stamp)] mode: $MODE"

git -C "$MAIN" worktree prune

NOW="$(date +%s)"
GRACE_SECONDS=$((GRACE_HOURS * 3600))

removed=0; failed=0; skipped=0
held_dirty=0; held_active=0; held_fresh=0; held_live=0; held_locked=0; held_unreadable=0
DIRTY_PATHS=()

# `-` stands in for an empty branch field. TWO parsing hazards live in this loop, both real:
#   (a) bash `read` treats a RUN of tabs as one separator, so an empty branch field would silently
#       shift every column after it and a detached tree would read as branch "1" — hence the `-`
#       sentinel emitted by awk below rather than an empty field;
#   (b) a detached HEAD has no branch ref at all, so "its branch is gone from origin" is not a
#       statement about it — nothing would outlive the removal. Only ancestry from origin/main
#       proves those commits survive, which is what the detached branch of the logic tests.
while IFS=$'\t' read -r path ref detached locked; do
  branch="${ref#refs/heads/}"
  [ "$ref" = "-" ] && branch="(detached)"

  if [ "$path" = "$MAIN" ]; then
    echo "SKIP  primary          $path"; skipped=$((skipped+1)); continue
  fi
  if [ "$path" = "$INVOKED_FROM" ]; then
    echo "SKIP  invoked-from     $path"; skipped=$((skipped+1)); continue
  fi
  if [ "$locked" = "1" ]; then
    echo "HELD  locked           $path ($branch)"; held_locked=$((held_locked+1)); continue
  fi
  if [ ! -d "$path" ]; then
    # Directory gone: the prune above already dropped the registration, so there is nothing left
    # to decide. Still logged — a silent skip reads as "swept everything".
    echo "SKIP  pruned           $path"; skipped=$((skipped+1)); continue
  fi

  # Substring, not prefix-exact, on purpose: a cwd DEEP inside the tree must match too. It can
  # over-match a path that is a prefix of an unrelated one, which errs toward holding. Safe side.
  case "$ACTIVE_CWDS" in
    *"$path"*) echo "HELD  active process   $path ($branch)"; held_active=$((held_active+1)); continue ;;
  esac

  # --porcelain covers tracked modifications AND untracked files; both are unrecoverable if the
  # directory goes. Nothing else shells out here, so there is no second tool to be missing.
  status="$(git -C "$path" status --porcelain 2>/dev/null)"
  if [ $? -ne 0 ]; then
    echo "HELD  unreadable       $path ($branch)"; held_unreadable=$((held_unreadable+1)); continue
  fi
  if [ -n "$status" ]; then
    echo "HELD  uncommitted      $path ($(printf '%s\n' "$status" | wc -l) entries, branch $branch)"
    DIRTY_PATHS+=("$path")
    held_dirty=$((held_dirty+1)); continue
  fi

  head="$(git -C "$path" rev-parse HEAD 2>/dev/null)"
  if [ -z "$head" ]; then
    echo "HELD  unreadable       $path ($branch)"; held_unreadable=$((held_unreadable+1)); continue
  fi
  tip_date="$(git -C "$MAIN" show -s --format=%ct "$head" 2>/dev/null || echo 0)"
  if [ "$((NOW - tip_date))" -lt "$GRACE_SECONDS" ]; then
    echo "HELD  within grace     $path (tip < ${GRACE_HOURS}h old, branch $branch)"
    held_fresh=$((held_fresh+1)); continue
  fi

  on_origin=0
  if [ "$detached" != "1" ] && git -C "$MAIN" rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null; then
    on_origin=1
  fi
  # merge-base --is-ancestor, never a `git branch --merged` grep: this is the question the removal
  # turns on, and it must be answered by git's own reachability walk, not by string matching.
  merged=0
  git -C "$MAIN" merge-base --is-ancestor "$head" origin/main 2>/dev/null && merged=1

  if [ "$on_origin" = "1" ] && [ "$merged" = "0" ]; then
    echo "HELD  live branch      $path ($branch still on origin, not merged)"
    held_live=$((held_live+1)); continue
  fi

  if [ "$detached" = "1" ] && [ "$merged" = "0" ]; then
    echo "HELD  detached unmerged $path (HEAD not an ancestor of origin/main)"
    held_live=$((held_live+1)); continue
  fi

  if [ "$APPLY" != "1" ]; then
    echo "WOULD REMOVE           $path ($branch)"
    removed=$((removed+1)); continue
  fi

  # NO --force, and NO `git branch -d/-D` anywhere in this file, ever: `git worktree remove` drops
  # the directory and the registration and leaves refs/heads alone, which is the ONLY reason the
  # "committed work survives a removal" claim holds. Adding either flag breaks it silently.
  if out="$(git -C "$MAIN" worktree remove "$path" 2>&1)"; then
    echo "REMOVED                $path ($branch)"
    removed=$((removed+1))
  else
    echo "FAILED                 $path — $(printf '%s' "$out" | head -1)" >&2
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

verb="removed"; [ "$APPLY" = "1" ] || verb="would-remove"
echo
echo "[$(stamp)] worktree sweep: mode=$([ "$APPLY" = 1 ] && echo apply || echo dry-run)" \
     "$verb=$removed failed=$failed skipped=$skipped" \
     "held(uncommitted=$held_dirty active=$held_active grace=$held_fresh live-branch=$held_live" \
     "locked=$held_locked unreadable=$held_unreadable)"
echo "[$(stamp)] remaining registrations: $(git -C "$MAIN" worktree list --porcelain | grep -c '^worktree ')"

if [ "$held_dirty" -gt 0 ]; then
  # Copy-pasteable, because 23 held trees that take a minute each to inspect become permanent
  # noise, and noise trains everyone to stop reading the report.
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
