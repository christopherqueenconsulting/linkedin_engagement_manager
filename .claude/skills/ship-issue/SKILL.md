---
name: ship-issue
description: Use when picking up a GitHub issue through to a merged PR — branch naming, flow labels, phased-issue close rules, PR conventions, CI gates, and the release:now fast-lane policy.
---

# Shipping an issue end-to-end

0. **Before step 1**, on a non-trivial issue: run the **spec-first** skill (Spec/Verifier/Environment) to pin down testable acceptance criteria, the specific check that proves success, and the owning docs/skill/module — don't start branching against a guess. Skip for trivial one-line fixes or issues whose acceptance is already testable as written.
1. Fresh state first: `git status` + re-read target files (never trust conversation memory). Branch: `feature/claude-issue-<N>` (or `feature/claude-<task-name>` for unnumbered work), from `origin/main`.
2. Flow labels are the state machine: `agent:ready` (queued) → `agent:working` (claimed) → `needs-human`/`agent:blocked` (parked). One flow label at a time. `risk:*` (`migration`/`security`/`live-linkedin`/`product-decision`) = build it, then park the finished PR with `needs-human` + a Decision Comment for owner sign-off before merge.
3. **Phase-guard before writing `Closes #N`:** scan the issue for unchecked acceptance boxes or "Phase 2 / follow-up / deferred" prose. Either file+link the follow-up issue (labelled `agent:ready` + `priority:*`) or drop the `Closes` keyword. The merge gate rejects PRs that close a phased issue with no linked follow-up (#548/#568/#647 all lost work this way).
4. Optional quality gate before drafting the PR — for `ui/`-touching or otherwise UX-sensitive issues, or when the issue names a quality bar, run the **gauntlet-loop** skill first and carry its verdict into the PR body. Before opening the PR: if step 0's spec-first pass left an `Uncertain: <reason>` line in your own notes (a genuine fork you flagged rather than silently resolved) and the issue isn't already `risk:*`, self-apply `review:copilot` alongside `agent:working` and carry the `Uncertain:` line into the PR body — the Review-economy "policy-triggered exception" in `docs/AGENT_WORKFLOW_PLAYBOOK.md` authorizes exactly this, at most once per PR. PR: Conventional-Commit title (`feat(scope): …`), body links the issue, tests included — ≥80% patch coverage (Codecov). Commits end with the Claude co-author trailer.
5. CI gates that must pass: `CI / Unit Tests`, `CI / Integration Test w/ Coverage`, CodeQL, GitGuardian, Migration Versions, PR lint, UI Build (if SPA touched). Never enable GitHub auto-merge — the pipeline's merge gate owns merging.
6. `release:now` fast lane — agents MAY self-apply for `priority:high`, user-reported bugs, user-visible breakage, or any revert/prod-fix; MUST NOT for docs/tests/refactors/dep bumps/flag-disabled work/unverified changes, and never more than one open fast-laned PR at a time. State the reason in one line in the PR body. Policy: `docs/release-fast-lane.md`.

Authoritative: `docs/AGENT_WORKFLOW_PLAYBOOK.md` (labels, Decision Comments, phased issues), `CONTRIBUTING.md` (commit/PR conventions), `docs/spec-verifier-environment.md`, `docs/gauntlet-loop.md`.
