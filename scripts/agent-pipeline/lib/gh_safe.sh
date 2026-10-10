#!/usr/bin/env bash
# gh_safe.sh — the ONE way a pipeline agent WRITES to GitHub through gh.
#
#   gh_safe.sh issue-edit <ISSUE> <edit-flag>...
#   gh_safe.sh pr-edit <PR> <edit-flag>... [--body-file tmp/<name>]
#       edit-flag: --add-label L | --remove-label L | --add-assignee U | --remove-assignee U
#                  (also the `--flag=value` spelling; L may be a comma list)
#   gh_safe.sh issue-create --title T --body-file tmp/<name> [--label L]...
#   gh_safe.sh pr-create --title T --body-file tmp/<name> [--base main] [--head <branch>] [--draft]
#                        [--label L]...
#   gh_safe.sh pr-comment <PR> (--body-file tmp/<name> | --body "<one line>")
#   gh_safe.sh issue-comment <ISSUE> (--body-file tmp/<name> | --body "<one line>")
#   gh_safe.sh pr-ready <PR> [--undo]
#
# Why a helper and not deny patterns: rounds of review showed every pattern losing to the shell and
# to gh's own parsing — `--add-lab''el`, `--add-lab${X}el`, `--add-labe\l`, `Agent:Ready`,
# `agent\:ready`, `--re''po` all reach gh as the plain spelling, because the SHELL expands quotes,
# escapes and variables, and GitHub matches label names case-insensitively. A permission rule only
# ever sees the unexpanded text. This script sees the EXPANDED arguments and allow-lists exact
# shapes: the repo is pinned (no --repo/-R is ever passed through), labels are trimmed and
# lower-cased, the provenance-gated labels cannot be ADDED, and every write is scoped to this run's
# own issue/PR. The dontAsk profile denies the raw `gh issue edit|create|comment` and
# `gh pr create|edit|comment|ready` forms and allows this script by its installed path.
#
# Scoping: the target must be $ISSUE or $PR (exported by the runner; an allow rule never matches
# past a `VAR=…` prefix, so the agent cannot override them), or — for PR writes — a same-repo PR
# (isCrossRepository == false) whose head branch is $BRANCH (MODE=start labels the PR it just
# opened). A body file must be a regular, non-symlink file whose realpath is ./tmp/<name> in the
# worktree; it is copied before use so it cannot be swapped between the check and the read.
#
# A guard rail, NOT a boundary: code an allowed command runs (pytest) executes as the runner's uid
# and can rewrite this file. Root custody of $BASE/lib is the boundary (docs/agent-pipeline-v2.md).
set -euo pipefail
export LC_ALL=C   # bracket ranges and ${x,,} must mean ASCII, whatever the box's locale

REPO="christopherqueenconsulting/linkedin_engagement_manager"
# Labels whose PRESENCE grants work or a deploy: agent:ready (start), release:now (fast lane), and
# every lane label v2 maps to a mode (observe.LANE_LABEL_MODES). The trust walk verifies who applied
# them, so an agent applying one would at best be refused there and at worst be the actor it trusts.
PROTECTED=(agent:ready release:now agent:revise agent:phasefix agent:depfix agent:docfix)
LABEL_RE='^[a-z0-9][a-z0-9:_./ -]{0,49}$'
LOGIN_RE='^@?[A-Za-z0-9-]{1,39}$'
BRANCH_RE='^[A-Za-z0-9._/-]{1,200}$'
NL=$'\n'

die() { echo "gh_safe: $*" >&2; exit 2; }

usage() {
  die "usage: gh_safe.sh issue-edit|pr-edit <N> <flags> | issue-create|pr-create --title T --body-file tmp/<name> … | pr-comment|issue-comment <N> --body-file tmp/<name> | pr-ready <N> [--undo]"
}

# norm_label <raw> <add|remove> -> the trimmed, lower-cased label; exits 2 on a refusal.
norm_label() {
  [[ "$1" != *"$NL"* && "$1" != *$'\r'* ]] || die "label must not contain a newline"
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
  [[ "$2" != *"$NL"* ]] || die "label must not contain a newline"
  IFS=, read -r -a parts <<<"$2"
  [ "${#parts[@]}" -gt 0 ] || die "$1 needs a label"
  for part in "${parts[@]}"; do
    l="$(norm_label "$part" "$3")" || exit 2
    OUT+=("$1" "$l")
  done
}

COPIES=()
cleanup() { [ "${#COPIES[@]}" -eq 0 ] || rm -f "${COPIES[@]}"; }
trap cleanup EXIT

# body_copy <path> -> prints a private copy of ./tmp/<name> after checking it; exits 2 otherwise.
body_copy() {
  local f="$1" real copy
  [[ "$f" =~ ^tmp/[A-Za-z0-9._-]{1,100}$ && "$f" != *..* ]] || die "--body-file must be tmp/<name>, got '$f'"
  [ ! -L tmp ] && [ ! -L "$f" ] && [ -f "$f" ] || die "--body-file '$f' must be a regular file (not a symlink) under ./tmp"
  real="$(realpath -e -- "$f")" || die "--body-file '$f' cannot be resolved"
  [ "$real" = "$(pwd -P)/$f" ] || die "--body-file '$f' resolves outside ./tmp ('$real')"
  copy="$(mktemp "${TMPDIR:-/tmp}/gh_safe.XXXXXX")"
  cat -- "$real" >"$copy"
  printf '%s' "$copy"
}

one_line() {  # <what> <text>
  [ -n "$2" ] && [ "${#2}" -le 1000 ] && [[ "$2" != *"$NL"* && "$2" != *$'\r'* ]] \
    || die "$1 must be one non-empty line of at most 1000 chars (multi-line text goes in --body-file tmp/<name>)"
}

number_ok() { [[ "$1" =~ ^[1-9][0-9]{0,6}$ ]] || die "issue/PR must be a number, got '$1'"; }

target_ok() {  # <issue|pr> <n> — refuses anything but this run's own item
  local seen=""
  [ -n "${ISSUE:-}" ] && [ "$2" = "$ISSUE" ] && return 0
  [ -n "${PR:-}" ] && [ "$2" = "$PR" ] && return 0
  if [ "$1" = pr ] && [ -n "${BRANCH:-}" ]; then
    seen="$(gh pr view "$2" --repo "$REPO" --json headRefName,isCrossRepository \
              --jq '"\(.headRefName) \(.isCrossRepository)"' 2>/dev/null)" || seen=""
    [ "$seen" = "$BRANCH false" ] && return 0
  fi
  die "REFUSING — #$2 is not this run's item (ISSUE=${ISSUE:-} PR=${PR:-} BRANCH=${BRANCH:-}; read '${seen:-}')"
}

head_ok() {  # the only branches a run may open a PR from — the same rule git_push.sh applies
  [[ "$1" =~ $BRANCH_RE && "$1" != *..* ]] || die "bad head branch '$1'"
  case "$1" in main|master|HEAD|-*) die "REFUSING head '$1'" ;; esac
  [ "$1" = "${BRANCH:-}" ] && return 0
  [ "${MODE:-}" = depfix ] && [[ "$1" =~ ^fix/ ]] && return 0
  die "REFUSING — head '$1' is not this run's branch (BRANCH=${BRANCH:-unset}, MODE=${MODE:-unset})"
}

parse_edit_flags() {  # sets OUT (+ BODY for pr-edit when $1 = allow-body)
  local allow_body="$1"; shift
  local flag val
  [ "$#" -gt 0 ] || die "nothing to change"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --add-label|--remove-label|--add-assignee|--remove-assignee|--body-file)
        [ "$#" -ge 2 ] || die "$1 needs a value"; flag="$1"; val="$2"; shift 2 ;;
      --add-label=*|--remove-label=*|--add-assignee=*|--remove-assignee=*|--body-file=*)
        flag="${1%%=*}"; val="${1#*=}"; shift ;;
      *) die "only --add-label/--remove-label/--add-assignee/--remove-assignee are allowed, got '$1'" ;;
    esac
    case "$flag" in
      --add-label)    add_labels "$flag" "$val" add ;;
      --remove-label) add_labels "$flag" "$val" remove ;;
      --body-file)
        [ "$allow_body" = allow-body ] || die "--body-file is not accepted here"
        [ -z "${BODY:-}" ] || die "--body-file given twice"
        BODY="$(body_copy "$val")" || exit 2; COPIES+=("$BODY"); OUT+=(--body-file "$BODY") ;;
      *) [[ "$val" =~ $LOGIN_RE ]] || die "assignee '$val' is not a GitHub login"; OUT+=("$flag" "$val") ;;
    esac
  done
}

[ "$#" -ge 1 ] || usage
cmd="$1"; shift
BODY=""
case "$cmd" in
  issue-edit|pr-edit)
    [ "$#" -ge 2 ] || usage
    number_ok "$1"; n="$1"; shift
    if [ "$cmd" = pr-edit ]; then parse_edit_flags allow-body "$@"; else parse_edit_flags no-body "$@"; fi
    target_ok "${cmd%-edit}" "$n"
    gh "${cmd%-edit}" edit "$n" --repo "$REPO" "${OUT[@]}"
    ;;
  issue-create|pr-create)
    title=""; body=""; base="main"; head=""; draft=()
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --title)       [ "$#" -ge 2 ] || die "--title needs a value"; title="$2"; shift 2 ;;
        --title=*)     title="${1#*=}"; shift ;;
        --body-file)   [ "$#" -ge 2 ] || die "--body-file needs a value"; body="$2"; shift 2 ;;
        --body-file=*) body="${1#*=}"; shift ;;
        --label|-l)    [ "$#" -ge 2 ] || die "$1 needs a value"; add_labels --label "$2" add; shift 2 ;;
        --label=*)     add_labels --label "${1#*=}" add; shift ;;
        --base)        [ "$cmd" = pr-create ] && [ "$#" -ge 2 ] || die "--base is pr-create only"; base="$2"; shift 2 ;;
        --base=*)      [ "$cmd" = pr-create ] || die "--base is pr-create only"; base="${1#*=}"; shift ;;
        --head)        [ "$cmd" = pr-create ] && [ "$#" -ge 2 ] || die "--head is pr-create only"; head="$2"; shift 2 ;;
        --head=*)      [ "$cmd" = pr-create ] || die "--head is pr-create only"; head="${1#*=}"; shift ;;
        --draft)       [ "$cmd" = pr-create ] || die "--draft is pr-create only"; draft=(--draft); shift ;;
        *) die "$cmd accepts only --title, --body-file, --label$([ "$cmd" = pr-create ] && echo ", --base main, --head, --draft"), got '$1'" ;;
      esac
    done
    one_line --title "$title"
    [ "${#title}" -le 256 ] || die "--title must be at most 256 chars"
    [ -n "$body" ] || die "--body-file tmp/<name> is required"
    BODY="$(body_copy "$body")" || exit 2; COPIES+=("$BODY")
    if [ "$cmd" = issue-create ]; then
      gh issue create --repo "$REPO" --title "$title" --body-file "$BODY" "${OUT[@]}"
    else
      [ "$base" = main ] || die "--base must be main, got '$base'"
      [ -n "$head" ] || head="$(git symbolic-ref --quiet --short HEAD)" || die "cannot read the current branch"
      head_ok "$head"
      gh pr create --repo "$REPO" --base main --head "$head" --title "$title" --body-file "$BODY" \
        "${draft[@]}" "${OUT[@]}"
    fi
    ;;
  pr-comment|issue-comment)
    [ "$#" -eq 3 ] || die "usage: $cmd <N> --body-file tmp/<name> | --body \"<one line>\""
    number_ok "$1"; n="$1"
    case "$2" in
      --body-file) BODY="$(body_copy "$3")" || exit 2; COPIES+=("$BODY"); src=(--body-file "$BODY") ;;
      --body)      one_line --body "$3"; src=(--body "$3") ;;
      *) die "$cmd accepts only --body-file tmp/<name> or --body \"<one line>\", got '$2'" ;;
    esac
    target_ok "${cmd%-comment}" "$n"
    gh "${cmd%-comment}" comment "$n" --repo "$REPO" "${src[@]}"
    ;;
  pr-ready)
    [ "$#" -ge 1 ] && [ "$#" -le 2 ] || die "usage: pr-ready <PR> [--undo]"
    number_ok "$1"; n="$1"; undo=()
    if [ "$#" -eq 2 ]; then [ "$2" = --undo ] || die "pr-ready accepts only --undo, got '$2'"; undo=(--undo); fi
    target_ok pr "$n"
    gh pr ready "$n" --repo "$REPO" "${undo[@]}"
    ;;
  *) usage ;;
esac
