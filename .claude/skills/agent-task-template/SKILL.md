---
name: agent-task-template
description: Use when filing an agent-ready GitHub issue for the autonomous pipeline — walks the .github/ISSUE_TEMPLATE/agent-task.yml form's Context/Scope/Acceptance/Verifier/Phase fields so MODE=start and phase_guard_ok can read something structured instead of guessing from prose. Trigger on "file an agent-ready issue", "use the agent task template", or when a triage pass is deciding whether an issue is ready to hand to the pipeline.
---

# Filing an issue through the agent-task template

`gh issue create --repo christopherqueenconsulting/linkedin_engagement_manager --template agent-task.yml`
(or GitHub's "New issue" picker → **Agent Task**). Filing through this form auto-applies the
`template:agent-task` label — that label is what gates the stricter checks below; an issue written
free-form in `## Context` / `## Scope` / `## Acceptance` prose never carries it and keeps today's
softer, unenforced convention.

1. **Context** — why this matters, with evidence (link data, PRs, docs). `MODE=start` only knows
   what's written here.
2. **Scope** — concrete changes, named files/modules where known, and what is explicitly OUT of
   scope.
3. **Acceptance** — free-text `- [ ]` checkboxes (still a textarea: GitHub's structured
   `checkboxes` field type can't express a variable per-issue list). Each box should be
   independently testable — a named test, a number, a behavior — not "make it better."
4. **Verifier** — for EACH acceptance box, the specific check that proves it: a named test
   file/lane (**test-lanes**), a coverage number, a skill's checklist (**db-migration**,
   **add-feature-flag**), or "a human via the Decision Comment" for `risk:*` work. See
   `docs/spec-verifier-environment.md` — this field is that layer's structured home on a
   template-filed issue.
5. **Phase** — `single-phase`, or `phase N of M` (e.g. `phase 2 of 3`) if this is one step of a
   staged build. `tick.sh`'s `phase_guard_ok` reads this field FIRST, before falling back to
   scanning the body for phase-like prose — see "Phased work" in `docs/AGENT_WORKFLOW_PLAYBOOK.md`.
6. **Remaining phases** — only when `Phase` is multi-phase: free text on what's left. Filing this
   does NOT substitute for filing the actual follow-up issue when the current phase closes — it's
   context for that follow-up, not a replacement for it.

Then label it exactly as any other issue: `agent:ready` + one `priority:*` + topicals (+ `risk:*`
if merge needs sign-off). The template label is additive to the normal flow-label state machine,
not a replacement for it.

## What filing through this template changes downstream

- `MODE=start` gains a hard gate: on a `template:agent-task` issue, if `Acceptance` isn't
  independently testable against `Verifier`, the agent STOPs before writing code — `Uncertain:
  <reason>` + `needs-human`, instead of guessing. On any issue WITHOUT the label, `MODE=start` is
  completely unchanged.
- `MODE=selfreview` additionally walks `Acceptance` item-by-item against the diff+tests on a
  template issue, on top of its usual general defect-hunting. It still only ever fixes in place or
  escalates — filing through this template does not add a new hand-off path.
- `phase_guard_ok` (the v1 merge-time failsafe) prefers the structured `Phase` field over its prose
  regex when the field is present. Full v2-native phase enforcement is tracked separately and
  blocked on `#1396` — this template only strengthens v1's existing check.

Authoritative: `docs/AGENT_WORKFLOW_PLAYBOOK.md` (labels, phased-issue rules), `docs/spec-verifier-environment.md` (the Spec/Verifier/Environment framework this form encodes), `.github/ISSUE_TEMPLATE/agent-task.yml` (the form itself).
