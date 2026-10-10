#!/usr/bin/env bash
# git_push.sh — the ONE way a pipeline agent pushes.
#
#   git_push.sh                      push the current branch to origin (sets upstream)
#   git_push.sh --force-with-lease   the same, with a lease (MODE=rebase after a rebase)
#
# Why a helper and not `git push` patterns: a permission rule matches the command TEXT, and a push
# can name `main` without the text saying "main" plainly — a second refspec, `x:heads/main`,
# `refs/heads/main`, a `:branch` delete, `--mirror`, a bundled `-fu`. Claude Code also reads a rule
# ending in `:*` as a trailing wildcard, so "deny any refspec colon" cannot even be written. This
# script takes NO refspec at all: it pushes exactly `refs/heads/<current>:refs/heads/<current>`,
# where <current> is the checked-out branch, and only when that branch is this run's $BRANCH (or a
# new `fix/*` branch in MODE=depfix, which opens a fix PR to main). It never pushes main/master.
# The dontAsk profile denies `git push` in every form and allows this script by its installed path.
#
# $BRANCH and $MODE are exported by the runner; an allow rule never matches past a `VAR=…` prefix,
# so the agent cannot override them. A guard rail, NOT a boundary: code an allowed command runs
# (pytest) executes as the runner's uid and can rewrite this file — see docs/agent-pipeline-v2.md.
set -euo pipefail
export LC_ALL=C

die() { echo "git_push: $*" >&2; exit 2; }

lease=()
case "$#:${1:-}" in
  0:) ;;
  1:--force-with-lease) lease=(--force-with-lease) ;;
  *) die "usage: git_push.sh [--force-with-lease] — no other arguments, no refspecs" ;;
esac

current="$(git symbolic-ref --quiet --short HEAD)" || die "HEAD is detached — check out your branch"
case "$current" in
  main|master|HEAD|-*|*..*|*:*|*' '*) die "REFUSING to push '$current'" ;;
esac
if [ "$current" = "${BRANCH:-}" ]; then
  :
elif [ "${MODE:-}" = depfix ] && [[ "$current" =~ ^fix/[A-Za-z0-9._/-]+$ ]]; then
  :
else
  die "REFUSING — '$current' is not this run's branch (BRANCH=${BRANCH:-unset}, MODE=${MODE:-unset})"
fi

exec git push "${lease[@]}" -u origin "refs/heads/$current:refs/heads/$current"
