#!/usr/bin/env bash
# Take over ONE PostHog self-driving PR (docs/posthog-pr-takeover.md).
#
# Usage: posthog_takeover.sh <pr-number>
#
# The daemon spawns this when a `pull_request` delivery looks like PostHog's (`lemd/posthog.py`),
# or when its reconcile pass finds an open PostHog PR. Neither is trusted: `posthog_takeover.py
# --pr N` re-reads the PR from the REST API and applies the full identity check (login, numeric
# id, account type, head repository, branch prefix, base) before it writes anything, and every
# step it takes is idempotent, so a duplicate spawn for the same PR is a no-op.
#
# It runs under the pipeline's App identity, like every other v2 action (common.sh refuses rather
# than falling through to the owner's login). That is also what makes the takeover PR's CI run: a
# PR opened by an App installation token triggers workflows, one opened by GITHUB_TOKEN does not.
set -uo pipefail
V2_ACTION="posthog_takeover"
# shellcheck disable=SC1091
. "$(dirname "$0")/common.sh"

PR="${1:-}"
case "$PR" in
  ''|*[!0-9]*) echo "usage: posthog_takeover.sh <pr-number>" >&2; exit 2 ;;
esac

if v2_paused; then log "PAUSED — not taking over PostHog PR #$PR."; exit "$EX_TRUST"; fi

MODE_FLAG="--apply"
if [ "$DRY_RUN" = "1" ]; then MODE_FLAG=""; log "DRY_RUN: planning the takeover of #$PR only."; fi

log "PostHog takeover: #$PR"
# shellcheck disable=SC2086  # MODE_FLAG is deliberately empty-or-one-word
python3 "$(dirname "$0")/../posthog_takeover.py" $MODE_FLAG --pr "$PR" --repo "$SLUG" >>"$LOG" 2>&1
rc=$?
log "PostHog takeover: #$PR finished rc=$rc"
exit "$rc"
