#!/usr/bin/env bash
# review_threads.sh — the ONE GitHub GraphQL surface a pipeline agent gets.
#
#   review_threads.sh list <PR>            every review thread on PR <PR>: id, isResolved,
#                                          isOutdated, path, line, and its comments (author, body,
#                                          databaseId, url)
#   review_threads.sh resolve <THREAD_ID>  resolve one review thread (a `PRRT_…` node id)
#
# Why this exists: MODE=review must RESOLVE Copilot's threads (the merge gate holds while one is
# open) and MODE=revise must read the owner's inline review comments. Neither is reachable through
# a `gh pr …` subcommand, only through `gh api`. The agents' dontAsk permission profile denies
# `gh api *` outright, because a glob cannot narrow it: any allow pattern with a wildcard in it
# also admits a second `-f query=…` (gh keeps the last), `--input <file>` or `-X`, i.e. ANY call
# with the pipeline's token. So the profile allows THIS script instead, by its installed path
# under $BASE/lib, which the profile gives the agent no way to edit. The repo is fixed, the two
# queries are fixed, and both arguments are validated before gh runs.
set -euo pipefail

OWNER="christopherqueenconsulting"
NAME="linkedin_engagement_manager"

usage() {
  echo "usage: review_threads.sh list <PR-number> | resolve <PRRT_thread-id>" >&2
  exit 2
}

[ "$#" -eq 2 ] || usage

case "$1" in
  list)
    [[ "$2" =~ ^[1-9][0-9]{0,6}$ ]] || { echo "review_threads: PR must be a number, got '$2'" >&2; exit 2; }
    exec gh api graphql \
      -f query='query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){reviewThreads(first:100){nodes{id isResolved isOutdated path line comments(first:50){nodes{databaseId url author{login} body}}}}}}}' \
      -f o="$OWNER" -f n="$NAME" -F p="$2"
    ;;
  resolve)
    [[ "$2" =~ ^PRRT_[A-Za-z0-9_-]{1,128}$ ]] || { echo "review_threads: thread id must be a PRRT_ node id, got '$2'" >&2; exit 2; }
    exec gh api graphql \
      -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{id isResolved}}}' \
      -f t="$2"
    ;;
  *) usage ;;
esac
