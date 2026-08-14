# Marketing front page — UX target spec

The reference exemplar for the front page (`src/cqc_lem/ui/src/pages/Landing.tsx` and
`src/cqc_lem/ui/src/components/marketing/`), written for issue #1300.

**Why this file exists rather than a competitor screenshot.** The gauntlet loop needs a reference
that is named, fetchable and comparable, and for UI it accepts two forms: a screenshot of a
comparably polished existing LEM screen, or a hand-written UX spec when nothing comparable exists
(`.claude/skills/gauntlet-loop/SKILL.md`, `docs/gauntlet-loop.md`). A rival's homepage is neither —
a critic tells LEM's page from Linear's instantly, the blindness collapses, and the comparison
degrades into generic taste. So the named exemplars below are the RESEARCH; the per-piece bar is
written out here, and the bar is what a build is graded against.

Every criterion is phrased so that it can be answered yes or no from the rendered DOM, the source
diff or a measured number. "Looks nicer" is not a verdict.

## Exemplars this spec is derived from

| Exemplar | What it is the bar for |
|---|---|
| linear.app | Serious tool, calm surface: light-dominant, hierarchy carried by type not colour, real product surfaces, zero illustration |
| attio.com | A dense product that does not feel dense — the feature narrative |
| stripe.com | Proof/metrics presentation only. Not a whole-page reference: LEM cannot populate an enterprise-scale page |
| cal.com | Section order and the FAQ/CTA tail |
| authoredup.com | The safety/trust section — the category's best treatment (three pillars, plus safety in the hero subhead) |
| raycast.com | Only if a dark section is used |

## Whole-page bar

1. Exactly one `<nav>`, one `<main>`, one `<footer>`, one `<h1>`, one `sticky top-0` element.
2. No section is clipped by an application measure (`max-w-5xl`); bands are full-bleed and the
   inner measure is the section's own.
3. Every declared foreground/background pair clears WCAG 2.1 AA, asserted by
   `utils/brandTokens.test.ts` rather than by eye.
4. No colour outside the `@theme` tokens appears on the marketing surface — asserted by
   `marketingPalette.test.ts`, because Tailwind v4 keeps the default palette available.
5. No number appears that cannot be traced to a repo constant, a documented default, or a cited
   third-party source rendered on the page.
6. No claim appears whose mechanism is not in the codebase.
7. Nothing on the page is announced to a screen reader as an emoji.
8. At 320px the document does not scroll horizontally; the only permitted horizontal scroll is a
   deliberate, labelled, keyboard-reachable `TableScroll` region.

## Per-piece bar

The pieces are the smallest independently improvable units — never "the whole page".

### Hero
- Names both pillars (content, engagement) AND the safety posture within the first two blocks of
  copy. AuthoredUp's move; the old page mentioned safety nowhere.
- One primary and one secondary action, distinguishable by accessible name.
- Trial terms as microcopy directly under the actions, not in the pricing section only.
- A product visual, not an illustration and not a stock photograph.
- Fails if: the headline could be pasted onto any competitor's page unchanged.

### Proof strip
- Sits above the fold-plus-one; no scrolling past a full section before the first proof.
- Every item is mechanism proof or a cited source. Zero uncited counts.
- Fails if: it implies scale LEM does not have.

### Problem section
- Names the reader's own week, not an abstraction.
- Includes the objection (automation makes people nervous) rather than only the pain.
- Fails if: it reads as a feature list with sad adjectives.

### How it works
- Three steps, each a thing the USER does, in the order they do them.
- Each step names the real setup artefact (session, voice/targeting/caps, the first approval).
- Fails if: a step describes what the software does instead of what the person does.

### Feature beats (one per beat: content, scheduling, engagement, measurement)
- One idea per beat, with its own product visual, alternating side.
- Every bullet traceable to a shipped capability named in `CLAUDE.md` or a `docs/*.md` posture.
- Fails if: two beats could be swapped without anyone noticing.

### Safety section
- At least six named mechanisms, each with a file that owns it.
- Says what the product cannot promise, in the section itself, not in a footnote.
- Does not exceed the owner-approved ToS answer (`pages/FAQ.tsx`, fallback entry `-3`).
- Fails if: any claim is stronger than the code supports.

### Comparison
- Compares approaches, never named competitors.
- Concedes at least one row honestly to an alternative.
- Fails if: every row is a win.

### Pricing
- Prices read from one constant shared with the checkout surface.
- No capability claimed for a tier that the codebase does not gate.
- Recommended plan first when the layout is one column.
- Fails if: a feature list differs per tier without a gate behind it.

### Final CTA
- Carries its own headline and its own trial microcopy.
- Fails if: it is a bare button on a coloured band.

### The 375px rendering of the whole page
- Nav collapses to a sheet; every tap target ≥44px.
- Pricing is one column, recommended first.
- No element forces the document wider than the viewport.

## What was run against this spec, and what was not

Recorded honestly, per the loop's own rule that a fabricated round is worse than a skipped one:

- **Run:** a single reviewer pass over every piece above, judged against the rendered DOM
  (`@testing-library/react` through the whole logged-out route tree), the computed markup and the
  source diff. Findings from that pass were fixed and are listed in the PR body.
- **Not run:** the blind builder/critic pairing with a fresh critic per retry. That discipline needs
  independent agents, which the session that built this page was not permitted to spawn, and a
  self-graded "round" is exactly the thing `docs/gauntlet-loop.md` says is not a round. No rounds
  are claimed.
- **Not run:** screenshot review at 375px and 1440px. No browser is reachable from the worktree
  (`.mcp.json` is untracked and not symlinked into worktrees), and `get_docker_driver()` was not
  used as a workaround because it draws from the production Chrome pool guarded by
  `tests/unit/app/test_selenium_capacity.py`. Responsive behaviour is asserted from the source and
  the DOM instead.
