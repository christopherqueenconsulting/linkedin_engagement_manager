#!/usr/bin/env bash
# review_threads.sh — the ONE GitHub GraphQL surface a pipeline agent gets.
#
#   review_threads.sh list <PR>                 every review thread on PR <PR>: id, isResolved,
#                                               isOutdated, path, line, and its comments (author,
#                                               body, databaseId, url)
#   review_threads.sh resolve <PR> <THREAD_ID>  resolve one review thread (a `PRRT_…` node id) —
#                                               only when <PR> is the run's exported $PR and the
#                                               thread belongs to it in this repo
#
# Why this exists: MODE=review must RESOLVE Copilot's threads (the merge gate holds while one is
# open) and MODE=revise must read the owner's inline review comments. Neither is reachable through
# a `gh pr …` subcommand, only through `gh api`. The agents' dontAsk permission profile denies
# `gh api *` outright, because a glob cannot narrow it: any allow pattern with a wildcard in it
# also admits a second `-f query=…` (gh keeps the last), `--input <file>` or `-X`, i.e. ANY call
# with the pipeline's token. So the profile allows THIS script instead, by its installed path
# under $BASE/lib. The repo is fixed, the queries are fixed, both arguments are validated before
# gh runs, and `resolve` refuses a thread that is not on the PR the agent was dispatched for — so
# one PR's run cannot clear another PR's merge gate.
#
# A guard rail, NOT a boundary: code an allowed command runs (pytest) executes as the runner's
# uid, and that uid can rewrite this file, the profile and $BASE/config.env. Root custody of
# $BASE/lib, $BASE/config and config.env is the boundary, and it is an owner action
# (docs/agent-pipeline-v2.md, "Permission profile").
set -euo pipefail

OWNER="christopherqueenconsulting"
NAME="linkedin_engagement_manager"

usage() {
  echo "usage: review_threads.sh list <PR-number> | resolve <PR-number> <PRRT_thread-id>" >&2
  exit 2
}

pr_ok() { [[ "$1" =~ ^[1-9][0-9]{0,6}$ ]] || { echo "review_threads: PR must be a number, got '$1'" >&2; exit 2; }; }

case "${1:-}" in
  list)
    [ "$#" -eq 2 ] || usage
    pr_ok "$2"
    exec gh api graphql \
      -f query='query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){reviewThreads(first:100){nodes{id isResolved isOutdated path line comments(first:50){nodes{databaseId url author{login} body}}}}}}}' \
      -f o="$OWNER" -f n="$NAME" -F p="$2"
    ;;
  resolve)
    [ "$#" -eq 3 ] || usage
    pr_ok "$2"
    [[ "$3" =~ ^PRRT_[A-Za-z0-9_-]{1,128}$ ]] || { echo "review_threads: thread id must be a PRRT_ node id, got '$3'" >&2; exit 2; }
    # The PR must be the one the runner dispatched this run for (it exports $PR). Without that, the
    # ownership check below only proves the thread is on WHATEVER PR the agent named. Unset refuses.
    if [ -z "${PR:-}" ] || [ "$2" != "$PR" ]; then
      echo "review_threads: REFUSING — PR #$2 is not this run's PR (PR=${PR:-unset})" >&2
      exit 3
    fi
    # Ownership first. Unreadable is a refusal: a thread we cannot place is never resolved.
    owner_of="$(gh api graphql \
      -f query='query($t:ID!){node(id:$t){... on PullRequestReviewThread{pullRequest{number} repository{nameWithOwner}}}}' \
      -f t="$3" --jq '"\(.data.node.repository.nameWithOwner) \(.data.node.pullRequest.number)"')" || owner_of=""
    if [ "$owner_of" != "$OWNER/$NAME $2" ]; then
      echo "review_threads: REFUSING — thread $3 is not on PR #$2 in $OWNER/$NAME (read: '${owner_of:-unreadable}')" >&2
      exit 3
    fi
    exec gh api graphql \
      -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{id isResolved}}}' \
      -f t="$3"
    ;;
  *) usage ;;
esac
