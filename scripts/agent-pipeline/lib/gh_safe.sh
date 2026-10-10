#!/usr/bin/env bash
# gh_safe.sh — the ONE way a pipeline agent changes labels/assignees or files an issue.
#
#   gh_safe.sh issue-edit <ISSUE> <edit-flag>...
#   gh_safe.sh pr-edit <PR> <edit-flag>...
#       edit-flag: --add-label L | --remove-label L | --add-assignee U | --remove-assignee U
#                  (also the `--flag=value` spelling; L may be a comma list)
#   gh_safe.sh issue-create --title T --body-file tmp/<name> [--label L]...
#
# Why a helper and not deny patterns: the provenance-gated labels (`agent:ready`, `release:now`, and
# the lane labels that authorise a dispatch) can be spelled past any glob — `Agent:Ready`,
# `agent\:ready`, `agent:read''y`, `-l AGENT:READY` all reach GitHub as the same label, because the
# SHELL expands quotes and escapes and GitHub matches label names case-insensitively. A permission
# rule only ever sees the unexpanded text. This script sees the EXPANDED arguments, so it can
# normalise (trim, lower-case) and compare exactly. The dontAsk profile denies `gh issue edit`,
# `gh issue create` and every label-adding form of `gh pr edit` / `gh pr create`, and allows this
# script by its installed path under $BASE/lib.
#
# Refused: adding any PROTECTED label (removing one is fine — escalation removes agent:ready), any
# flag other than the four edit flags, label text outside [a-z0-9:_./ -], and an issue body that is
# not a regular, non-symlink file directly under ./tmp/ of the current worktree.
#
# A guard rail, NOT a boundary: code an allowed command runs (pytest) executes as the runner's uid
# and can rewrite this file. Root custody of $BASE/lib is the boundary (docs/agent-pipeline-v2.md).
#
# Edits are also scoped to THIS run's item: the target must be $ISSUE or $PR (exported by the runner
# to the agent; a permission allow rule does not match past a `VAR=…` prefix, so the agent cannot
# override them), or — for pr-edit — a PR whose head branch is $BRANCH (MODE=start labels the PR it
# just opened). So one run cannot lift another item's `needs-human` hold.
set -euo pipefail
export LC_ALL=C   # bracket ranges and ${x,,} must mean ASCII, whatever the box's locale

REPO="christopherqueenconsulting/linkedin_engagement_manager"
# Labels whose PRESENCE grants work or a deploy — the trust walk verifies who applied them, so an
# agent applying one would at best be refused there and at worst be the actor it trusts.
PROTECTED=(agent:ready release:now agent:revise agent:depfix agent:docfix)
LABEL_RE='^[a-z0-9][a-z0-9:_./ -]{0,49}$'
LOGIN_RE='^@?[A-Za-z0-9-]{1,39}$'

die() { echo "gh_safe: $*" >&2; exit 2; }

usage() {
  die "usage: gh_safe.sh issue-edit <N> <flags> | pr-edit <N> <flags> | issue-create --title T --body-file tmp/<name> [--label L]"
}

# norm_label <raw> <add|remove> -> the trimmed, lower-cased label; exits 2 on a refusal.
norm_label() {
  local l="${1,,}"
  l="${l#"${l%%[![:space:]]*}"}"; l="${l%"${l##*[![:space:]]}"}"
  [[ "$l" =~ $LABEL_RE ]] || die "label '$1' has characters outside [a-z0-9:_./ -] (or is empty/too long)"
  if [ "$2" = add ]; then
    local p
    for p in "${PROTECTED[@]}"; do
      [ "$l" = "$p" ] && die "REFUSING to add '$p' — it is provenance-gated; ask the owner in a comment"
    done
  fi
  printf '%s' "$l"
}

OUT=()
# add_labels <flag> <comma-list> <add|remove>
add_labels() {
  local part l parts=()
  IFS=, read -r -a parts <<<"$2"
  [ "${#parts[@]}" -gt 0 ] || die "$1 needs a label"
  for part in "${parts[@]}"; do
    l="$(norm_label "$part" "$3")" || exit 2
    OUT+=("$1" "$l")
  done
}

parse_edit_flags() {
  local flag val
  [ "$#" -gt 0 ] || die "nothing to change"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --add-label|--remove-label|--add-assignee|--remove-assignee)
        [ "$#" -ge 2 ] || die "$1 needs a value"; flag="$1"; val="$2"; shift 2 ;;
      --add-label=*|--remove-label=*|--add-assignee=*|--remove-assignee=*)
        flag="${1%%=*}"; val="${1#*=}"; shift ;;
      *) die "only --add-label/--remove-label/--add-assignee/--remove-assignee are allowed, got '$1'" ;;
    esac
    case "$flag" in
      --add-label)    add_labels "$flag" "$val" add ;;
      --remove-label) add_labels "$flag" "$val" remove ;;
      *) [[ "$val" =~ $LOGIN_RE ]] || die "assignee '$val' is not a GitHub login"; OUT+=("$flag" "$val") ;;
    esac
  done
}

number_ok() { [[ "$1" =~ ^[1-9][0-9]{0,6}$ ]] || die "issue/PR must be a number, got '$1'"; }

target_ok() {  # <issue|pr> <n> — refuses anything but this run's own item
  local head=""
  [ -n "${ISSUE:-}" ] && [ "$2" = "$ISSUE" ] && return 0
  [ -n "${PR:-}" ] && [ "$2" = "$PR" ] && return 0
  if [ "$1" = pr ] && [ -n "${BRANCH:-}" ]; then
    head="$(gh pr view "$2" --repo "$REPO" --json headRefName --jq .headRefName 2>/dev/null)" || head=""
    [ -n "$head" ] && [ "$head" = "$BRANCH" ] && return 0
  fi
  die "REFUSING — #$2 is not this run's item (ISSUE=${ISSUE:-} PR=${PR:-} BRANCH=${BRANCH:-})"
}

body_file_ok() {  # the file must be ./tmp/<name> in THIS worktree: no symlink, no traversal
  local f="$1" real
  [[ "$f" =~ ^tmp/[A-Za-z0-9._-]{1,100}$ && "$f" != *..* ]] || die "--body-file must be tmp/<name>, got '$f'"
  [ ! -L tmp ] && [ ! -L "$f" ] && [ -f "$f" ] || die "--body-file '$f' must be a regular file (not a symlink) under ./tmp"
  real="$(realpath -e -- "$f")" || die "--body-file '$f' cannot be resolved"
  [ "$real" = "$(pwd -P)/$f" ] || die "--body-file '$f' resolves outside ./tmp ('$real')"
}

[ "$#" -ge 1 ] || usage
cmd="$1"; shift
case "$cmd" in
  issue-edit|pr-edit)
    [ "$#" -ge 2 ] || usage
    number_ok "$1"; n="$1"; shift
    parse_edit_flags "$@"
    target_ok "${cmd%-edit}" "$n"
    if [ "$cmd" = issue-edit ]; then
      exec gh issue edit "$n" --repo "$REPO" "${OUT[@]}"
    fi
    exec gh pr edit "$n" --repo "$REPO" "${OUT[@]}"
    ;;
  issue-create)
    title=""; body=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --title)     [ "$#" -ge 2 ] || die "--title needs a value"; title="$2"; shift 2 ;;
        --title=*)   title="${1#*=}"; shift ;;
        --body-file) [ "$#" -ge 2 ] || die "--body-file needs a value"; body="$2"; shift 2 ;;
        --body-file=*) body="${1#*=}"; shift ;;
        --label|-l)  [ "$#" -ge 2 ] || die "$1 needs a value"; add_labels --label "$2" add; shift 2 ;;
        --label=*)   add_labels --label "${1#*=}" add; shift ;;
        *) die "issue-create accepts only --title, --body-file and --label, got '$1'" ;;
      esac
    done
    [ -n "$title" ] && [ "${#title}" -le 256 ] && [[ "$title" != *$'\n'* ]] || die "--title is required (one line, <= 256 chars)"
    [ -n "$body" ] || die "--body-file tmp/<name> is required"
    body_file_ok "$body"
    exec gh issue create --repo "$REPO" --title "$title" --body-file "$body" "${OUT[@]}"
    ;;
  *) usage ;;
esac
