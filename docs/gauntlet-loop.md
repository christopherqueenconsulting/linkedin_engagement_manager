# Gauntlet Loop (issue: agent quality-gate tooling)

Full design detail for the `gauntlet-loop` skill. CLAUDE.md keeps the one-line pointer; this doc
is where the mechanics, the origin, and LEM's adaptations of them live.

## Where this comes from

"Gauntlet Loop" is Matt Shumer's name for the orchestration prompt behind "Claude of Duty" — a
single ~150-word prompt that ran for hours and produced a browser-based FPS in Three.js by having
agents build against a **named, fetchable reference** ("at the level of [a real thing]") instead of
an abstract quality bar, and grading progress with "eleven independent adversarial critics" running
blind A/B comparisons against that reference. The two open-source packagings of it as a Claude Code
skill (`duolahypercho/gauntlet-loop`, `robonuggets/gauntlet-loop`) both center the same three
mechanics:

1. A **lead/orchestrator** agent takes a goal and a concrete quality bar that is **named**,
   **fetchable**, and **comparable** — vague bars are called out as the pattern's most common
   failure mode, because a critic with nothing concrete to compare against "invents a comparison and
   approves everything."
2. The goal is fanned out to **builders**, one per independently-improvable piece, each graded by a
   **separate critic with fresh context** that never saw the builder's reasoning — only the
   finished output.
3. The critic runs a **blind comparison**: the builder's output and the reference exemplar, labels
   stripped, and it has to say which one is better and name the gap. One implementation states the
   exit condition explicitly: *"the loop exits when your work wins the blind comparison, or when
   you stop the run — never after a fixed number of rounds. You are the brake."*

## Why this is a good fit for LEM specifically

LEM already runs on adversarial-verification patterns, not single-pass generation trusting its own
judgment — Gauntlet Loop is the same shape applied to agent deliverables instead of AI content:

- **The #617 comment quality contract + similarity gate** (`docs/content-core.md`) already refuses
  to let a comment grade itself — it regenerates against a contract and a similarity gate, capped
  at `COMMENT_GATE_MAX_ATTEMPTS`, and the post is skipped rather than shipping a comment that never
  cleared the bar.
- **`scripts/benchmark_models.py`** (`docs/model-benchmarks/README.md`) never scores a candidate
  model in isolation — every run measures it "beside the current champion" and only a
  meets-or-beats verdict becomes a swap recommendation. That IS a blind-ish comparative judgment
  against a standing reference, just for models instead of code.
- **`slop_lint.py`**'s hard/warn split and **the LLM-judge layer** in the benchmark harness are both
  separate-critic patterns: the thing that produced the draft is never the thing that clears it.

Gauntlet Loop generalizes that same discipline — separate judgment, comparative not absolute,
capped retries, never self-graded — to the one place LEM didn't already have it: the agent's OWN
code/docs/UI output, before it becomes a PR.

## Reference exemplar selection, per feature area

The exemplar has to be named and fetchable inside THIS repo or the live app — not invented. Pick it
from what the touched Feature Areas row already points to:

| Touched surface | Reference exemplar candidates |
|---|---|
| Content generation & scheduling (`app/run_content_plan.py`, `utilities/ai/*`) | An existing well-formed post/newsletter that already passed slop lint + the content-mix governor; the cadence rules in `docs/content-scheduling.md`; a comparable blueprint in `content_framework.py`. |
| Engagement automation (`app/run_automation.py`) | A subsystem that already embodies the target discipline — e.g. `utilities/human_pacing.py` for "the ONE place a cadence decision lives," `utilities/comment_outcomes.py` for a read-only T+24h sweep shape, `utilities/suppression.py` for a fail-open daily beat. `docs/engagement-automation.md`'s per-subsystem paragraph is the written half when no code twin exists yet. |
| Engagement configuration / SPA (`ui/.../Account.tsx`) | A comparably-polished existing screen in the live SPA (screenshot), or the settings-IA research in `docs/SETTINGS_IA_RESEARCH.md` when no comparable screen exists. |
| Observability | The relevant row's "The ONE place" module in CLAUDE.md's Observability table, or the doc in that row's last column. |
| Docs themselves | A doc already in the "authoritative pointer" style referenced from CLAUDE.md — `docs/model-benchmarks/README.md` and `docs/content-quality-telemetry.md` are both good twins for tone/structure. |

If nothing in the repo fits, the exemplar is a **hand-written spec** of the target behavior —
written before the loop starts, not improvised by the critic mid-run. An exemplar the critic
partially wrote is not a reference anymore, it's an opinion.

## The blind comparison, mechanically

The critic subagent is spawned fresh — a new Task invocation, not a continuation of the builder's
context — and receives exactly two things: `Output A` and `Output B` (order shuffled per round, no
metadata naming which is the draft and which is the reference). It is asked for a winner and the
single biggest gap, not a numeric score. This mirrors the origin pattern's insistence on comparative
over absolute judgment: a rubric score can be gamed by matching the rubric's letter; "which one
would you rather ship" is harder to game because it has to hold up as a whole.

## Round cap — a deliberate LEM adaptation

The upstream pattern is explicitly open-ended (*"never after a fixed number of rounds... you are
the brake"*) because it assumes a human watching an interactive session. LEM's agents run
unattended against the `ship-issue` flow, and every other bounded-retry mechanism in this repo caps
itself rather than looping forever on someone else's clock: `COMMENT_GATE_MAX_ATTEMPTS`,
`BENCHMARK_TRUNCATION_RETRIES`/`BENCHMARK_EMPTY_REPEATS`, `SELF_COMMENT_MAX_PER_POST`. The skill
caps at 3 rounds per piece for the same reason — a piece that still hasn't won after 3 rounds goes
to `needs-human` with the last critic verdict attached (same disposition `ship-issue` already uses
for anything that needs owner judgment), rather than shipping the losing draft or looping unbounded.

## Frontend UX — screenshot-based blind critique

`src/cqc_lem/ui/` changes are named as a first-class case because LEM's own stated posture treats
UX as part of the product, not chrome around it — the "preview/approval" half of the content
pillar and the targeting/caps/voice surfaces of the engagement pillar are both SPA screens users
have to trust. For a UI piece:

1. Launch the app and capture the changed screen the way the `run` skill already does in this
   environment (Playwright/Chromium is preinstalled) — one screenshot of the builder's draft render.
2. The reference exemplar is a screenshot of a comparably-polished existing LEM screen (or a
   written UX spec if nothing comparable exists yet — see the selection table above).
3. The critic receives both screenshots blind, PLUS the project's stated UX goals for that surface
   pulled from CLAUDE.md's Project Overview pillars and the relevant `docs/*.md` (e.g. the
   preview/approval flow in `docs/content-core.md`, the targeting/cap clarity implied by
   `docs/engagement-automation.md`'s per-subsystem invariants). The verdict has to cite one of
   those goals by name — "looks cleaner" is not a gap, "the per-day cap isn't visible before the
   user hits it" is.

## Discipline this pattern depends on

- **The builder never grades its own work.** Self-grading collapses the entire mechanism into
  theater — this is the same principle CLAUDE.md already states for comments ("the tree does not
  meet the standard yet" is measured externally, never asserted by the generator).
- **A critic that graded a losing draft never grades that same piece's retry.** Fresh eyes only —
  otherwise the critic anchors on its own prior verdict and stops comparing, it starts confirming.
- **No exemplar, no run.** A vague quality bar is the documented failure mode upstream; LEM's
  version of that failure is a critic approving a draft because nothing concrete disagreed with it.

## Where it plugs into `ship-issue`

Optional step between implementation and opening the PR (`ship-issue` step 4) — recommended
whenever the issue touches `ui/` or is otherwise UX-sensitive, or when the issue text itself names
a quality bar. It is not a CI gate and does not block merge on its own; it is a pre-PR pass the
agent runs itself, and its report (winning round, final critic verdict, anything parked at the
round cap) is worth carrying into the PR description as evidence the deliverable was checked
against something concrete.
