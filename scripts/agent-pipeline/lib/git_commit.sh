#!/usr/bin/env bash
# git_commit.sh — the ONE way a pipeline agent commits.
#
#   git_commit.sh -m "<subject>" [-m "<paragraph>"]...   commit what is staged
#   git_commit.sh -F tmp/<name>                          commit what is staged, message from a file
#
# Why a helper: `git commit` reads files named by its flags (`-F`, `--file`, `-t`, `--template`,
# `--pathspec-from-file`, `-C`), and git accepts abbreviated long options and bundled short ones —
# `--fil=/path`, `--templ=/path`, `-aF/path` — so a deny pattern on the command text cannot keep a
# commit from reading an outside file (a token, config.env) into a commit message that is then
# pushed. This script accepts exactly two shapes and passes nothing else through: one or more
# `-m <message>`, or one `-F tmp/<name>` that is a regular, non-symlink file whose realpath is
# ./tmp/<name> in the worktree (copied before use, so it cannot be swapped after the check). The
# dontAsk profile denies `git commit` in every form and allows this script by its installed path.
#
# A guard rail, NOT a boundary: code an allowed command runs (pytest) executes as the runner's uid
# and can rewrite this file — see docs/agent-pipeline-v2.md, "Permission profile".
set -euo pipefail
export LC_ALL=C

die() { echo "git_commit: $*" >&2; exit 2; }
usage() { die "usage: git_commit.sh -m \"<message>\" [-m \"<message>\"]... | -F tmp/<name>"; }

[ "$#" -ge 2 ] || usage
msgs=(); file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -m) [ "$#" -ge 2 ] || usage; [ -z "$file" ] || die "use -m or -F, not both"
        [ -n "$2" ] || die "-m needs a non-empty message"; msgs+=(-m "$2"); shift 2 ;;
    -F) [ "$#" -ge 2 ] || usage; [ "${#msgs[@]}" -eq 0 ] && [ -z "$file" ] || die "use one -F, or -m"
        file="$2"; shift 2 ;;
    *) die "only -m <message> or -F tmp/<name> are accepted, got '$1'" ;;
  esac
done

if [ -n "$file" ]; then
  [[ "$file" =~ ^tmp/[A-Za-z0-9._-]{1,100}$ && "$file" != *..* ]] || die "-F must be tmp/<name>, got '$file'"
  [ ! -L tmp ] && [ ! -L "$file" ] && [ -f "$file" ] || die "-F '$file' must be a regular file (not a symlink) under ./tmp"
  real="$(realpath -e -- "$file")" || die "-F '$file' cannot be resolved"
  [ "$real" = "$(pwd -P)/$file" ] || die "-F '$file' resolves outside ./tmp ('$real')"
  copy="$(mktemp "${TMPDIR:-/tmp}/git_commit.XXXXXX")"
  trap 'rm -f "$copy"' EXIT
  cat -- "$real" >"$copy"
  git commit -F "$copy"
else
  git commit "${msgs[@]}"
fi
