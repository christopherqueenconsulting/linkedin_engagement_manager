<!--
This template is for humans, Copilot and outside contributors.

It deliberately does NOT reach agent-authored PRs: GitHub only pre-fills a template when
`gh pr create` is called without `--body`, and both `.claude/skills/ship-issue/SKILL.md` and
`scripts/agent-pipeline/runbook/start.md` always pass one. The agent path is covered by the
runbook preamble and the selfreview mode instead. Do not treat this file as enforcement.
-->

## What & why

<!-- One paragraph. Link the issue: Closes #N -->

## Verification

<!-- The command you ran and what it printed. Not "tests pass". -->

## Checklist

- [ ] Tests cover the changed behaviour (≥80% patch coverage)
- [ ] `CLAUDE.md`: edited an existing row, or added none (or N/A) — see CONTRIBUTING.md § CLAUDE.md is a fixed-shape file
- [ ] Docs updated where a `docs/*.md` owns this topic
- [ ] No guard, budget or baseline was raised to make a check pass
