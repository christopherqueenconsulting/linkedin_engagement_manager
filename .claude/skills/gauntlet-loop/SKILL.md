---
name: gauntlet-loop
description: Use when a deliverable (code, docs, or a UI change) needs to clear a REAL quality bar instead of just passing review — spin up isolated builder/critic pairs, blind-compare against a named reference exemplar, and loop until the build wins. Optional quality gate between implementation and opening a PR; especially for ui/-touching or otherwise UX-sensitive issues.
---

# Gauntlet Loop: builder/critic iteration against a named reference

The pattern (Matt Shumer's "aim prompt", the mechanic behind the "Claude of Duty" build): a lead
agent takes a goal + a REAL, named, fetchable reference of what "great" looks like, splits the goal
into the smallest independently-improvable pieces, and runs each piece through a builder and a
**separate critic with fresh context** until the builder's output wins a **blind** comparison
against the reference. The critic is a brake, not a rubber stamp — a vague quality bar is the
single biggest failure mode of this pattern; if you cannot name a concrete exemplar, don't run this.

1. **Pick the reference exemplar per piece — named, fetchable, comparable.** Not "make it good."
   Use an existing gold-standard in THIS repo when one exists (e.g. `utilities/human_pacing.py` for
   "the ONE place a cadence decision lives", `utilities/comment_outcomes.py` for a read-only sweep
   shape, a specific screen in the live SPA for a UI pattern) or, absent one, a hand-written spec
   of the target behavior. The exemplar must be something the critic can actually put side by side
   with the builder's output — a doc paragraph, a diff, a rendered screenshot.

2. **Split the goal into the smallest independently-improvable pieces.** One function, one screen,
   one doc section — never "the whole issue" as one piece, or the critic has nothing precise to
   compare. Each piece gets its own builder/critic pair.

3. **Builder** (one Task subagent per piece): given the piece, the NAMED reference exemplar, and the
   relevant CLAUDE.md/`docs/*.md` invariants for the feature area it touches (see step 5). Produces
   one finished draft. It does not grade itself and never sees the critic's rubric in advance.

4. **Critic** (a SEPARATE, freshly-spawned Task subagent — never the builder, never a critic that
   graded an earlier draft of this SAME piece): receives ONLY the two finished outputs — the
   builder's draft and the reference exemplar — with identifying labels stripped or randomized
   (`Output A` / `Output B`, order shuffled). It never sees the builder's reasoning, prompt, or
   process. It must pick a winner and name the SINGLE biggest remaining gap — not a scored rubric,
   a comparative verdict. If it can't tell which output is which, that's the point; if it invents a
   confident verdict on a vague bar, the bar was the problem (go back to step 1).

5. **Ground the rubric in THIS project's actual invariants, not generic taste.** The critic's
   verdict must cite the two CLAUDE.md pillars (content generation & scheduling; engagement
   automation) and whichever `docs/*.md` the piece's Feature Areas row points to — e.g. a comment
   surface is graded against the #617 comment quality contract in `docs/content-core.md`, a
   scheduling change against `docs/content-scheduling.md`'s cadence rules, a DM surface against the
   approval-gating invariants in `docs/engagement-automation.md`. "Better" that ignores those
   invariants is not a win.

6. **Loop.** If the builder loses the blind comparison, the critic's single named gap goes back to
   the SAME builder for one more draft — never silently "fix everything the critic mentioned," fix
   the one gap named, then re-run steps 4 onward with a NEW critic instance (fresh eyes on the
   retry; the critic that saw the losing draft is retired). Cap at 3 rounds per piece, matching this
   repo's existing bounded-retry convention (`COMMENT_GATE_MAX_ATTEMPTS`,
   `BENCHMARK_TRUNCATION_RETRIES`) — this pattern is explicitly open-ended upstream ("you are the
   brake"), but an unattended LEM agent run needs a hard stop, not a human watching. Hitting the cap
   parks the piece `needs-human` with the last critic verdict attached; it does not ship the loser.

7. **Frontend UX changes (`src/cqc_lem/ui/`) are a first-class case, not an afterthought.** When the
   piece is a rendered screen:
   - Launch the app and capture the changed screen the same way the **`run`** skill would (this
     environment has Playwright/Chromium preinstalled) — screenshot the builder's draft screen.
   - The reference exemplar is EITHER a screenshot of a comparably-polished existing LEM screen
     (e.g. the content calendar for a new scheduling UI, `Account.tsx`'s engagement-prefs layout for
     a new settings panel) OR a hand-described target if no comparable screen exists yet.
   - The critic gets both screenshots blind (unlabeled, order shuffled) PLUS the project's stated UX
     goals for that surface — the "preview/approval" and per-day-cap/targeting clarity called out
     under this project's own pillars and the relevant `docs/*.md` (e.g. `docs/content-core.md`'s
     preview/approval flow, `docs/engagement-automation.md`'s targeting/cap surfaces) — and must
     name the gap in terms of an actual UX goal, not "looks nicer."

8. **Report the outcome plainly**: which piece(s) won on which round, the final critic verdict,
   and anything parked at the round cap. This is the artifact that ships alongside the PR — not a
   pass/fail bit.

## Where this plugs into `ship-issue`

Optional, recommended step between finishing implementation and drafting the PR (ship-issue step 4)
— run it when the issue is `ui/`-touching or otherwise UX-sensitive, or when the issue itself names
a quality bar ("as good as X"). Not a CI gate; it's a pre-PR quality pass the agent runs itself.

## Discipline (do not skip)

- The builder never grades its own work.
- A critic that has seen a PREVIOUS draft of a piece never grades the RETRY of that same piece —
  fresh critic instance every round.
- The comparison is blind: labels stripped/shuffled, critic doesn't know which side is the
  reference.
- No reference exemplar you can name and point at → do not run this skill; write the exemplar
  first or fall back to ordinary code review.

Authoritative: `docs/gauntlet-loop.md` (pattern origin, why it fits LEM, reference-exemplar
selection per feature area, the UI-screenshot mechanics in detail).
