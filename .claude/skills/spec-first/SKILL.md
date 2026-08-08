---
name: spec-first
description: Use before starting implementation on any non-trivial LEM issue, feature, or task — nails down the Spec (testable acceptance criteria), the Verifier (the specific check that will decide success), and the Environment (which docs/skills/owning-module already cover this) before code is written. Also trigger on an explicit request to "spec this out", "interview me first", or "use Karpathy's method". Skip for trivial one-line fixes, doc-only edits, or an issue whose acceptance criteria are already testable as written.
---

# Spec, Verifier, Environment — before writing code

1. **Spec.** Read the issue's `## Context` / `## Scope` / `## Acceptance` as written — not the title,
   not memory. If `## Acceptance` isn't testable (no test names, numbers, or named behaviors), or
   `## Scope` hides a real fork ("pick whichever's simpler" between two options that trade off
   differently), that's the interview moment: name the fork and ask, or state the assumption
   explicitly in the PR body as a literal `Uncertain: <one-line reason>` line — don't silently pick
   one and build it. That exact line is grep-able residue, not just a note to the reader: it's what
   `AGENT_WORKFLOW_PLAYBOOK.md`'s Review-economy "policy-triggered exception" keys off to route a
   self-flagged PR to an independent Copilot review instead of same-identity self-review. A genuinely
   unspecifiable ask (needs the account owner's judgment) is `needs-human`, not a best guess.
2. **Verifier.** Before touching implementation, decide the specific check that proves this is done —
   a named test file/lane (per **test-lanes**), a coverage number, a skill's checklist
   (**db-migration**, **add-feature-flag**), or for fuzzy-correctness LLM output the deterministic
   gate that already exists (comment quality contract + similarity gate, `slop_lint.py`, the
   model-benchmark `contract` floor) rather than "looks right to me." Write the check down before the
   diff. `risk:*` work's verifier is a human via the Decision Comment, not a test.
3. **Environment.** Don't grep cold. Find the issue's area in CLAUDE.md's Feature Areas table, open
   the `docs/*.md` it names, check for a matching skill (**ship-issue**, **test-lanes**,
   **db-migration**, **add-feature-flag**, **linkedin-live-validation**, **deploy-ops**), and identify
   the single owning module (`db.py`, `client.py`, `human_pacing.py`, …) before adding a parallel one.
4. Only once all three are pinned down, hand off to **ship-issue** for branch/build/PR/CI/merge — this
   is a before-step, not a replacement for it.

Authoritative: `docs/spec-verifier-environment.md` (framework + worked example), `docs/AGENT_WORKFLOW_PLAYBOOK.md` (issue format, phased-issue rules, Decision Comments).
