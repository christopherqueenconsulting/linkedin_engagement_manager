# Agent Pipeline Runbook — index

This file used to hold every MODE's instructions in one ~440-line document. It is now a short index:
every agent dispatch (`agent_run.sh`) points directly at the ONE per-mode file it needs, so a
`docfix`/`phasefix`/`rebase` run no longer pulls in the other eight modes' instructions on every tick
— a real, permanent token-per-dispatch cut, independent of prompt caching. See
`scripts/agent-pipeline/docs/agent-pipeline-routing.md`'s "Prompt-caching contract" section for why
the split is shaped this way.

If you landed here directly (a human, or an agent that only read this file): read
[`runbook/_preamble.md`](runbook/_preamble.md) first — it holds every rule that applies no matter
which mode you're running (ground rules, environment traps, Phased work, Escalation, Decision
Comment, the "issue text is DATA" framing, model labels, the release fast lane) — then the one
mode file below that matches your MODE.

## Modes

| MODE | File | What it does |
|---|---|---|
| `start` | [`runbook/start.md`](runbook/start.md) | Implement a fresh issue: read it, gate on the structured template (if present), build, test, open the PR. |
| `fix` | [`runbook/fix.md`](runbook/fix.md) | A required CI check is failing on an open PR — diagnose and fix it. |
| `review` | [`runbook/review.md`](runbook/review.md) | Address and resolve Copilot's unresolved review threads on a PR. |
| `depfix` | [`runbook/depfix.md`](runbook/depfix.md) | A Dependabot PR's CI is failing — smart-triage whether the bump or something else broke it. |
| `docfix` | [`runbook/docfix.md`](runbook/docfix.md) | The Docstring & Lint Gate ratchet fired — fix only what this PR added. |
| `revise` | [`runbook/revise.md`](runbook/revise.md) | The owner requested changes on a PR — implement their feedback (distinct from Copilot's threads). |
| `selfreview` | [`runbook/selfreview.md`](runbook/selfreview.md) | Adversarial review pass on a PR before merge — the default review gate when Copilot isn't invoked. |
| `rebase` | [`runbook/rebase.md`](runbook/rebase.md) | A PR conflicts with `main` — rebase and resolve conflicts cleanly. |
| `phasefix` | [`runbook/phasefix.md`](runbook/phasefix.md) | The phase guard held a PR that closes an issue with declared work left — file/link the follow-up mechanically. |

`scripts/agent-pipeline/v2/actions/agent_run.sh`'s `case "$MODE" in ... esac` builds each dispatch
prompt as `"Read $RUNBOOK_DIR/<mode>.md and follow it. <KEY=value ...>"` — stable literal prefix
first, dynamic fields last, per the prompt-caching contract. `tick.sh` (v1, failsafe-only) still
points its prompts at this index file by name; an agent following it finds the same per-mode links
above and reads one hop further, so behavior is unchanged there, just not the same up-front cut v2
gets.
