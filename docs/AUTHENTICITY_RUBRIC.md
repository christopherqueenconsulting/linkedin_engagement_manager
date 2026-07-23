# Authenticity Rubric — A1 Anti-Slop Gate (360Brew Defense)

> **Spike deliverable for issue #405 (R2 — 360Brew authenticity-rubric deep-dive).**
> This document is the calibrated rubric that feeds the **A1 authenticity-scoring gate** (issue #382)
> and the **A3 Topic-DNA governor** (issue #384) acceptance tests. The machine-readable form of
> everything here lives in `src/cqc_lem/utilities/ai/authenticity_rubric.py` — A1 imports the
> weights, threshold, and judge-criteria from that module, and the golden set drives the tests.

## Why this exists

LinkedIn's **2026 Authenticity Update** (paired with the **360Brew** ranking model) actively demotes
generic AI content. Public reporting and Richard van der Blom's *Algorithm Insights* both describe:

- Early classifier tests flagged obviously-generic posts **~94%** of the time.
- Organic reach for low-authenticity accounts is down **~50% YoY**.
- 360Brew derives a per-author **"Topic DNA"** from the headline/about/history and **suppresses
  off-niche posts** — profile↔content consistency is now a ranking input (this is what A3 governs).

LEM is AI-content-heavy with no authenticity safeguard, so a generic-slop draft is the single biggest
reach risk. **But over-correcting is just as damaging:** demoting good, AI-*assisted*-but-personal
content would gut the product's value. The whole calibration challenge is to flag *generic* while
never demoting *personal-with-AI-help*.

## Calibration principle

> **The enemy is _generic_, not _AI-assisted_.**
> Polish, correct grammar, and the use of AI are **NEUTRAL** — they must never lower the score on
> their own. Reach is earned by lived first-person proof, checkable specificity, a consistent human
> voice, on-niche topic authority, and a non-obvious point of view. The rubric rewards those and
> penalizes only their **absence** (plus the AI-slop tells below).

This is why the two heaviest dimensions are *first-person proof* and *specificity*: they are the
signals a language model cannot fabricate for content it didn't live, and they are the same signals
the existing deterministic **A2 personal-proof detector** (`content_framework.py`) already steers
writers toward.

## Scoring dimensions

Each dimension is scored **0–100** by the A1 LLM-judge (`lem-medium`). The composite is the weighted
average (`weighted_score` in the module). Weights sum to 1.0.

| Dimension | Weight | Scores HIGH when | Scores LOW when |
|---|---|---|---|
| **First-person lived proof** | 0.28 | ≥1 concrete detail only this author could write — a real number, a moment in time, a named person/tool/company, an outcome from their own work — owned in first person. | Only abstract could-be-anyone claims; advice with no evidence the author has done it. |
| **Checkable specificity** | 0.22 | Claims grounded in figures, dates, before/after deltas, concrete scenarios. | Vague superlatives / buzzwords stand in for substance. |
| **Consistent human voice** | 0.18 | Author's established tone; natural rhythm; a real point of view. | Flat corporate-neutral register; no discernible person. |
| **On-niche topic authority (A3)** | 0.17 | Subject sits inside `focus_topics` / profile headline & about — 360Brew "Topic DNA" consistency. | Off-niche drift into a topic the profile gives no reason to trust them on. |
| **Non-obvious insight** | 0.15 | A specific, non-obvious, or mildly contrarian point. | Restates consensus; obvious-tips listicle; hollow "Thoughts?". |

### AI-slop tells (weigh DOWN)

Any single tell is weak evidence; a **pileup of them alongside no first-person proof** is the signal
the gate should act on. They mostly hurt *specificity*, *voice consistency*, and *originality*:

- "In today's fast-paced / ever-evolving world…", "In the age of AI…"
- "It's not just X, it's Y" / "This isn't about X. It's about Y." cadence on repeat.
- Rule-of-three everything ("faster, smarter, better").
- Emoji-bulleted listicle, one abstract tip per bullet, no lived example.
- Hollow engagement-bait CTA ("Thoughts?", "Agree?", "Drop a 🔥 if you relate").
- Grand hollow abstractions ("unlock/leverage/harness synergies", "game-changer", "paradigm shift").
- Definitional filler that teaches nothing ("Leadership is about people.").

## Threshold & gate action

| Band | Composite | Gate action |
|---|---|---|
| **Demote** | `< 60` | Flip `APPROVED → PENDING` (mirrors the deterministic similarity gate at `run_content_plan.py`). |
| **Caution** | `60 – 71` | Keep, but prime a personal-proof nudge on any regeneration. |
| **Pass** | `≥ 72` | Ship as-is. |

- `AUTHENTICITY_GATE_THRESHOLD = 60`
- `AUTHENTICITY_CAUTION_CEILING = 72`

The threshold was calibrated against the golden set so that **every** generic draft lands below 60
and **every** authentic / AI-assisted-personal draft lands at or above 60 — i.e. the gate flags
generic content with **no false demotion** of good AI-assisted posts (the A1 acceptance criterion).

## Golden set

The labeled reference drafts live in `GOLDEN_SET` in the module. Labels and the binary gate
expectation (`expected_action`):

| Label | Meaning | `expected_action` |
|---|---|---|
| `generic` | Known generic AI slop. | `demote` |
| `authentic` | Human, specific, first-person. | `keep` |
| `ai_assisted_personal` | Polished / AI-assisted **but** carries real lived proof and stays on-niche — the false-demotion guard. | `keep` |

Each entry also carries `expected_dimension_scores` — the calibration target the A1 judge is tuned
against. Acceptance tests assert the resulting **gate action** (via `gate_draft` / `classify_score`),
not the exact per-dimension numbers, so a re-tuned judge stays green as long as it lands in the right
band.

### How A1 and A3 consume this

- **A1 (#382)** imports `AUTHENTICITY_JUDGE_CRITERIA`, `AUTHENTICITY_DIMENSIONS`, and
  `AUTHENTICITY_GATE_THRESHOLD` into its `lem-medium` judge, calls `weighted_score` on the judge's
  per-dimension output, and gates on `gate_draft`. Its acceptance test runs the `GOLDEN_SET`.
- **A3 (#384)** already governs the `topic_authority` dimension; its tests reuse
  `golden_by_label("generic")` / `golden_by_label("authentic")` to confirm off-niche drafts score
  low and on-niche pass.

## Sources

- LinkedIn 2026 **Authenticity Update** + **360Brew** ranking model (generic-AI demotion; per-author
  "Topic DNA" / off-niche suppression).
- Richard van der Blom — **LinkedIn Algorithm Insights 2026** (authenticity & topic-consistency as
  ranking inputs; reach decline for low-authenticity accounts).
- LEM 2026 Growth Roadmap, Milestone 7 (Authenticity & Anti-Slop Guardrails): A1 #382, A2 #383,
  A3 #384, A4 #385, R2 #405.
