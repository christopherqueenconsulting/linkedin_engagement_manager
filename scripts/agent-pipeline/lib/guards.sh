#!/usr/bin/env bash
# Shared guards — the functions BOTH runners must agree on, byte for byte.
#
# Every function below was MOVED out of tick.sh, not rewritten. That is the point: these are the
# incident-hardened ones (the timeline-pagination trust walk, the fork refusal, the worktree
# reattach that stopped throwing away unpushed commits), and each carries the reasoning of the
# failure that shaped it in its own comments. v2's bash actions source this file instead of
# re-deriving the same rules in Python, because a "faithful port" of the guard set is how
# incident-specific semantics get silently rounded off — and the trust boundary is the one place
# in this pipeline where a rounded-off semantic is a security hole rather than a bug.
#
# Sourced by tick.sh (v1) and v2/actions/common.sh (v2). Both source it STRICTLY: a swallowed
# error here does not degrade the pipeline, it removes its trust boundary while leaving every log
# line looking normal.
#
# Expects from the caller, at CALL time (not source time): BASE, REPO, SLUG, OWNER, WORKROOT,
# TRUSTED_ASSOCIATIONS, AGENT_LABEL_TRUSTED_ACTORS, AGENT_CI_LABEL_ACTORS, and a log() function.

# Per-branch work claim so concurrent slots never touch the same PR/issue. Re-opening fd 10
# releases any previously held claim (fd close drops the flock).
claim_branch() {  # $1=branch -> 0 claimed, 1 busy
  exec 10>"$BASE/locks/br-$(echo "$1" | tr '/' '_').lock"
  flock -n 10
}

author_trusted() {
  # author_trusted <number> -> 0 when the AUTHOR has standing in this repo.
  #
  # REST, deliberately — NOT `gh issue view --json authorAssociation`. That field does not exist in
  # gh's issue or PR JSON ("Unknown JSON field"), so the command fails, the association reads empty,
  # and this function then refuses — correctly for an unreadable answer, but it made EVERY issue
  # unreadable and idled the whole pipeline. The REST issues endpoint does expose
  # `author_association`, and it covers PRs too, because a PR is an issue.
  local n="$1" both assoc author
  both="$(gh api "repos/$SLUG/issues/$n" \
            --jq '"\(.author_association // "")\t\(.user.login // "")"' 2>/dev/null)"
  assoc="${both%%$'\t'*}"; author="${both#*$'\t'}"
  [ -n "$assoc" ] || { log "TRUST: #$n — author_association unreadable; refusing."; return 1; }
  # An issue the PIPELINE filed. MODE=phasefix's whole job is to file the follow-up issue a held
  # merge needs, and it labels it `agent:ready` — but a GitHub App is not a repo collaborator, so
  # its author_association is never one of OWNER/MEMBER/COLLABORATOR and every follow-up it filed
  # would sit unworkable forever. Nobody but this pipeline can author as this login.
  if [ -n "${GH_APP_BOT_LOGIN:-}" ] && [ "$author" = "$GH_APP_BOT_LOGIN" ]; then return 0; fi
  case " $TRUSTED_ASSOCIATIONS " in *" $assoc "*) return 0 ;; esac
  log "TRUST: #$n authored by $assoc — not eligible for autonomous work."
  return 1
}

label_actor_trusted() {
  # label_actor_trusted <number> <label> -> 0 when the LAST actor to apply <label> is allowlisted.
  # The timeline endpoint covers PRs too (a PR is an issue). We read the LAST `labeled` event for
  # that name: a label removed and re-added by someone else is theirs, not the original applier's.
  # `--slurp` + an EXTERNAL jq, not `--paginate --jq`: gh applies --jq to each PAGE separately, so
  # `| last` emits one login PER PAGE. Past 100 timeline events $actor becomes multi-line, the
  # case-match below fails, and the gate refuses — silently, and precisely on the long-lived,
  # heavily-discussed threads. (--slurp is rejected alongside --jq, hence the pipe.) --slurp yields
  # an array OF PAGES, so `.[][]` flattens it.
  local n="$1" label="$2" actor
  actor="$(gh api "repos/$SLUG/issues/$n/timeline" --paginate --slurp \
             -H "Accept: application/vnd.github+json" 2>/dev/null \
           | jq -r "[.[][] | select(.event==\"labeled\" and .label.name==\"${label}\")
                    | .actor.login] | last // empty" 2>/dev/null)"
  [ -n "$actor" ] || { log "TRUST: #$n — no readable '$label' labeler; refusing."; return 1; }
  case " $AGENT_LABEL_TRUSTED_ACTORS " in *" $actor "*) return 0 ;; esac
  # CI-ROUTED labels are the one place our own workflows are the legitimate applier. A router runs
  # `actions/github-script` with the default GITHUB_TOKEN, so the timeline actor is
  # `github-actions[bot]` — never a human, and never in the human allowlist. Refusing it made BOTH
  # auto-fix lanes dead on arrival: `agent:depfix` has been shipped since the Dependabot router
  # landed and has dispatched exactly ZERO times.
  #
  # This is deliberately narrow, and it is not the hole the allowlist exists to close. These two
  # labels grant nothing: they say "this EXISTING pull request has failing CI", not "build this".
  # `pr_is_upstream` still gates the work onto a branch inside this repo, and the labels that DO
  # grant privilege — `agent:ready`, `release:now` — stay human-only.
  #
  # Note what is NOT in that list: `pr_admissible` is `pr_is_upstream` + `label_actor_trusted`, and
  # `author_trusted` is deliberately absent from every PR lane (it runs once, on the ISSUE lane).
  # That is not an oversight to tidy up later — it is load-bearing. Dependabot PRs carry
  # `author_association: CONTRIBUTOR`, which is not in TRUSTED_ASSOCIATIONS (OWNER MEMBER
  # COLLABORATOR), so adding `author_trusted` here to make the gate look symmetrical would refuse
  # every Dependabot PR and put the depfix lane straight back into the dead-on-arrival state the
  # paragraph above describes. The control that matters on a PR lane is `pr_is_upstream`: getting a
  # branch into this repo at all already requires write access, which is a stronger statement than
  # any association string.
  case " $label " in
    " agent:depfix "|" agent:docfix ")
      case " $AGENT_CI_LABEL_ACTORS " in
        *" $actor "*) return 0 ;;
      esac
      ;;
  esac
  log "TRUST: #$n — '$label' applied by '$actor', not in AGENT_LABEL_TRUSTED_ACTORS."
  return 1
}

pr_is_upstream() {
  # pr_is_upstream <number> -> 0 when the PR's head branch lives in THIS repo, not a fork.
  # The PR lanes push to `origin/$branch` and merge; add_worktree resolves refs/remotes/origin/...,
  # so a fork PR would silently branch from main instead of carrying the contributor's code. That
  # is a correctness bug as much as a security one — refuse rather than do something surprising.
  local n="$1" head
  head="$(gh pr view "$n" --repo "$SLUG" --json headRepositoryOwner \
            --jq '.headRepositoryOwner.login // ""' 2>/dev/null)"
  [ -n "$head" ] || { log "TRUST: PR #$n — head repository unreadable; refusing."; return 1; }
  [ "$head" = "$OWNER" ] && return 0
  log "TRUST: PR #$n head is in fork '$head' — this pipeline only works upstream branches."
  return 1
}

pr_admissible() {
  # pr_admissible <number> <lane-label> -> both halves for a PR lane.
  pr_is_upstream "$1" && label_actor_trusted "$1" "$2"
}

issue_for_pr() {
  # Issue that PR $1 closes. Uses GitHub's OWN development link, NOT a "#N" scan of the PR body —
  # that scan returns the FIRST number mentioned, so a PR citing prior work ("the lane #553 added")
  # reported the wrong issue and handed a wrong ISSUE to the fix/review/rebase prompts.
  local P="$1" I
  I="$(gh pr view "$P" --repo "$SLUG" --json closingIssuesReferences 2>/dev/null \
       | jq -r '[(.closingIssuesReferences // [])[] | .number] | (first // empty)')"
  [ -n "$I" ] && { echo "$I"; return 0; }
  # Fallbacks: the agent branch convention, then a "closes/fixes/resolves #N" keyword in the body.
  gh pr view "$P" --repo "$SLUG" --json headRefName,body 2>/dev/null | jq -r '
      (.headRefName // "") as $b
      | if ($b | test("^feature/claude-issue-[0-9]+$")) then ($b | capture("(?<n>[0-9]+)$").n)
        else (((.body // "") | capture("(?i)(clos(e|es|ed)|fix(e[sd])?|resolv(e|es|ed))[[:space:]]+#(?<n>[0-9]+)").n) // empty)
        end'
}

closing_issue_for_pr() {
  # Issue that merging PR $1 would actually CLOSE — deliberately NARROWER than issue_for_pr.
  # The branch convention says which issue the work BELONGS to; it does NOT close anything. Those
  # two answers diverge exactly when a PR lands one phase of a multi-phase issue and omits the
  # closing keyword ON PURPOSE, which is the case the phase guard exists to bless, not to block:
  # #807 (phase 2a of #745, no closing keyword, `closingIssuesReferences` empty) was parked for a
  # day and told to "remove `Closes #745` from the PR body" — text that was never there, so the
  # instruction was impossible to follow and every tick re-parked it.
  local P="$1" I
  I="$(gh pr view "$P" --repo "$SLUG" --json closingIssuesReferences 2>/dev/null \
       | jq -r '[(.closingIssuesReferences // [])[] | .number] | (first // empty)')"
  [ -n "$I" ] && { echo "$I"; return 0; }
  # Body keyword only — GitHub populates the link from it, so this is a belt-and-braces catch for
  # an API blip, never a guess from the branch name.
  gh pr view "$P" --repo "$SLUG" --json body 2>/dev/null | jq -r '
      ((.body // "") | capture("(?i)(clos(e|es|ed)|fix(e[sd])?|resolv(e|es|ed))[[:space:]]+#(?<n>[0-9]+)")? | .n) // empty' 2>/dev/null
}

pr_for_issue() {
  # Open PR belonging to issue $1. Uses GitHub's OWN linkage (the "Closes #N" development link) —
  # NOT a "#N" scan of PR bodies, which false-matches any PR that merely cites the issue as context
  # (e.g. #616's body cites "#403/#404", so a body scan wrongly claimed #616's PR owns #404).
  # Falls back to the agent branch naming convention for a PR that forgot the closing keyword.
  #
  # NEWEST ref, not the first (#1605) — matching `github.linked_pr_state()`'s own semantics. An
  # issue can accumulate refs (#1091 carries both #1592 and #1597), and reading the first let an
  # issue whose first ref was an older CLOSED PR slip past a merged NEWER one: the merged guard in
  # `unpark.sh` never saw the merge and re-parked the issue forever.
  local N="$1" P
  P="$(gh issue view "$N" --repo "$SLUG" --json closedByPullRequestsReferences 2>/dev/null \
       | jq -r '[(.closedByPullRequestsReferences // [])[] | .number] | (max // empty)')"
  if [ -n "$P" ]; then
    # Only route work that's still open.
    [ "$(gh pr view "$P" --repo "$SLUG" --json state --jq .state 2>/dev/null)" = "OPEN" ] && { echo "$P"; return 0; }
    return 0
  fi
  gh pr list --repo "$SLUG" --state open --limit 50 --json number,headRefName 2>/dev/null \
    | jq -r --arg n "$N" '[ .[] | select(.headRefName == "feature/claude-issue-" + $n) | .number ] | (first // empty)'
}

worktree_has_unsaved_work() {  # $1=path -> 0 if it holds work that is NOT on the remote
  # Two ways an agent's work can exist only here: uncommitted changes, and commits it never pushed
  # (a run killed between `git commit` and `git push`). Either one makes the directory the ONLY
  # copy, so removing it destroys work. Anything unreadable counts as unsafe.
  local wt="$1" br
  [ -d "$wt" ] || return 1
  git -C "$wt" rev-parse --git-dir >/dev/null 2>&1 || return 0     # unreadable -> treat as unsafe
  [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ] && return 0
  br="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)" || return 0
  [ -z "$br" ] || [ "$br" = "HEAD" ] && return 0                    # detached -> cannot compare
  if git -C "$wt" rev-parse --verify --quiet "refs/remotes/origin/$br" >/dev/null 2>&1; then
    [ -n "$(git -C "$wt" log --oneline "origin/$br..HEAD" 2>/dev/null)" ] && return 0
    return 1
  fi
  # No remote branch at all: unpushed by definition unless it carries nothing beyond main.
  [ -n "$(git -C "$wt" log --oneline "origin/main..HEAD" 2>/dev/null)" ] && return 0
  return 1
}

branch_lock_free() {  # $1=branch -> 0 when NO live tick holds the claim
  # claim_branch() flocks this same file for the life of a tick. Testing it non-blockingly on a
  # SEPARATE fd is how cleanup avoids racing a run that is mid-flight.
  local lf="$BASE/locks/br-$(echo "$1" | tr '/' '_').lock"
  [ -f "$lf" ] || return 0
  ( exec 9>"$lf"; flock -n 9 ) 2>/dev/null
}

release_worktree() {  # $1=path -> remove it unless it is in use or holds unsaved work
  local wt="$1" br
  [ -n "$wt" ] && [ -d "$wt" ] || return 0
  case "$wt" in "$WORKROOT"/*) ;; *) return 0 ;; esac   # never touch anything outside work/
  br="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [ -n "$br" ] && ! branch_lock_free "$br"; then return 0; fi
  # WORKTREE_MERGED=1 means GitHub confirmed the branch's PR merged, so unpushed-looking commits are
  # the squash, not lost work. An UNCOMMITTED tree is still kept either way — a merged PR says
  # nothing about edits made after it landed.
  if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
    log "worktree $wt kept: uncommitted changes."
    return 0
  fi
  if [ "${WORKTREE_MERGED:-0}" != "1" ] && worktree_has_unsaved_work "$wt"; then
    log "worktree $wt kept: it holds unpushed work and no merged PR."
    return 0
  fi
  git -C "$REPO" worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"
  git -C "$REPO" worktree prune >/dev/null 2>&1
  return 0
}

sweep_stale_worktrees() {
  # The box accumulated 255 worktrees / 40G before this existed: add_worktree only ever cleaned the
  # ONE branch it was about to recreate, and nothing swept the rest. A worktree is disposable the
  # moment its work is on the remote — add_worktree rebuilds it from origin in seconds.
  #
  # Rate-limited by a stamp file: this stats every worktree, and the tick runs every 5 minutes.
  local stamp="$BASE/locks/.worktree-sweep" now age open removed=0 wt br
  now="$(date +%s)"
  if [ -f "$stamp" ]; then
    age=$(( now - $(stat -c %Y "$stamp" 2>/dev/null || echo 0) ))
    [ "$age" -lt "${WORKTREE_SWEEP_INTERVAL:-3600}" ] && return 0
  fi
  : > "$stamp"
  # One API call, not one per worktree. An unreadable answer means we keep everything: deleting on
  # the assumption that a PR is closed, when we simply could not ask, is the wrong way to be wrong.
  open="$(gh pr list --repo "$SLUG" --state open --limit 200 --json headRefName --jq '.[].headRefName' 2>/dev/null)" || return 0
  [ -z "$open" ] && ! gh pr list --repo "$SLUG" --state open --limit 1 >/dev/null 2>&1 && return 0
  # MERGED branches must be asked about separately, because local git CANNOT tell they landed. The
  # repo squash-merges and auto-deletes the branch, so a merged worktree has no `origin/<branch>`
  # and its commits never appear on main — `origin/main..HEAD` stays non-empty forever and the
  # unsaved-work check reads shipped work as unsaved. Measured: 198 of 255 worktrees here.
  merged="$(gh pr list --repo "$SLUG" --state merged --limit 400 --json headRefName --jq '.[].headRefName' 2>/dev/null || true)"
  # Iterate GIT'S OWN inventory, not a glob. A branch name contains slashes, so a worktree lands at
  # work/feature/claude-issue-123 — two levels down. A `"$WORKROOT"/*/` glob sees only the
  # intermediate `work/feature/` directory, which is not a worktree, so the first version of this
  # swept 3 entries instead of 255 and reported success. Ask the source of truth.
  while IFS= read -r wt; do
    [ -n "$wt" ] && [ -d "$wt" ] || continue
    case "$wt" in "$WORKROOT"/*) ;; *) continue ;; esac
    # Grace period: a run that just finished may still be opening its PR.
    [ $(( now - $(stat -c %Y "$wt" 2>/dev/null || echo "$now") )) -lt "${WORKTREE_GRACE_SECONDS:-7200}" ] && continue
    br="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    [ -n "$br" ] && printf '%s\n' "$open" | grep -qxF "$br" && continue   # still has an open PR
    if [ -n "$br" ] && printf '%s\n' "$merged" | grep -qxF "$br"; then
      # GitHub says this landed, so the local divergence is the squash, not lost work.
      WORKTREE_MERGED=1 release_worktree "$wt"
    else
      release_worktree "$wt"
    fi
    [ ! -d "$wt" ] && removed=$((removed+1))
  done < <(git -C "$REPO" worktree list --porcelain | awk '/^worktree /{print $2}')
  [ "$removed" -gt 0 ] && log "worktree sweep: removed $removed stale worktree(s)."
  return 0
}

add_worktree() {  # $1=branch  $2=base(ref)  -> path on stdout
  local branch="$1" base="$2" wt="$WORKROOT/$1"
  git -C "$REPO" worktree remove --force "$wt" >/dev/null 2>&1 || true
  rm -rf "$wt"
  git -C "$REPO" worktree prune >/dev/null 2>&1
  if git -C "$REPO" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    git -C "$REPO" worktree add "$wt" "origin/$branch" >/dev/null 2>&1
    git -C "$wt" checkout -B "$branch" "origin/$branch" >/dev/null 2>&1
  else
    # Origin ref missing, so there are three cases and only one of them wants `-b`.
    if git -C "$REPO" show-ref --verify --quiet "refs/heads/$branch"; then
      local tip
      tip="$(git -C "$REPO" rev-parse --verify "$branch" 2>/dev/null)" || tip=""
      if [ -n "$tip" ] && git -C "$REPO" merge-base --is-ancestor "$tip" "$base" 2>/dev/null; then
        # Stale ref carrying nothing: delete it so `-b` can recreate it cleanly.
        log "add_worktree: deleting stale local $branch (tip on $base) before creating worktree." >&2
        git -C "$REPO" branch -D "$branch" >/dev/null 2>&1 || true
      else
        # The branch has UNPUSHED WORK. Deleting it would throw that away, but `-b` refuses to
        # recreate an existing branch — so the tick failed here every time and the issue could never
        # be worked (feature/claude-issue-744 sat with 3 unique commits and a missing directory,
        # failing on every tick until the reaper parked it). Attach the existing branch instead.
        log "add_worktree: reattaching existing $branch (has commits not on $base) to a fresh worktree." >&2
        git -C "$REPO" worktree add "$wt" "$branch" >/dev/null 2>&1
      fi
    fi
    # Only create the branch if the reattach above didn't already produce the worktree.
    [ -d "$wt" ] || git -C "$REPO" worktree add -b "$branch" "$wt" "$base" >/dev/null 2>&1
  fi
  if [ ! -d "$wt" ]; then
    log "add_worktree: FAILED to create $wt (branch=$branch base=$base). Check git worktree list." >&2
    return 1
  fi
  # Recorded so the EXIT trap can release it on EVERY path, including a crash or a kill. Set here
  # rather than at the nine call sites, for the same reason the worktree guard lives in run_lane().
  TICK_WORKTREE="$wt"
  echo "$wt"
}

# Per-issue model tier. Explicit labels always win; without one, only a NARROW class of work is
# auto-downgraded (low-priority docs/cleanup) — everything else inherits the CLI default, so the
# quality of normal feature/bug work is untouched. Owner opt-in per issue:
#   gh issue edit N --add-label agent:model:sonnet     (or :haiku / :opus)
model_for_issue() {  # $1=issue -> echoes model name or nothing (= CLI default)
  [ -n "${1:-}" ] || return 0
  local labels
  labels="$(gh issue view "$1" --repo "$SLUG" --json labels --jq '[.labels[].name]|join(" ")' 2>/dev/null)"
  # Side effect: publish the label set + a priority slug into the env so dispatch_lane's Ollama
  # tier selection (agent:tier:*) and PostHog issue_priority get them with no extra gh call.
  ISSUE_LABELS="$labels"; export ISSUE_LABELS
  ISSUE_PRIORITY="$(printf '%s' "$labels" | grep -oE 'priority:[a-z]+' | head -1)"; export ISSUE_PRIORITY
  case " $labels " in
    *" agent:model:haiku "*)  echo haiku;  return ;;
    *" agent:model:sonnet "*) echo sonnet; return ;;
    *" agent:model:opus "*)   echo opus;   return ;;
  esac
  if echo " $labels " | grep -q " priority:low " \
     && echo " $labels " | grep -qE " (documentation|cleanup) "; then
    echo sonnet
  fi
}
