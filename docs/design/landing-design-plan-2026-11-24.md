Mode: refine

# Landing page design pass (2026-11-24)

The logged-out front page (`src/cqc_lem/ui/src/pages/Landing.tsx` and
`src/cqc_lem/ui/src/components/marketing/`). This pass changes how the page shows its content.
It does not change what the page claims: every claim, link, analytics name, anchor and behaviour
stays. The section order, the copy owned by `docs/marketing-page-ux-spec.md` and the honesty
tests in `pages/Landing.test.tsx` are unchanged.

- **Brand pack:** `brand/lem/DESIGN.md` and `brand/lem/assets.json`. LEM tokens only; no new
  token, colour, font or image.
- **Exploration:** skipped. The two approved exploration tools were not available to this pass,
  so no divergent directions were generated. This is a refine pass on the existing page.
- **Verification:** captured in a headless Chromium at 1440x900, 768x1024 and 390x844, light and
  dark, with a keyboard focus pass, before the first edit and after the last. Captures are kept
  outside the repository.

## Ground

- **Subject:** LEM, a LinkedIn content and engagement tool, in a one-user pre-launch beta.
- **Reader:** a founder or consultant who posts on LinkedIn and is wary of automation.
- **The page's one job:** show that LEM writes and schedules posts in the reader's voice and
  publishes nothing without approval.
- **The one action:** Start free trial.

## The bold element

The hero product panel: the approval queue beside a suggested comment, drawn as a labelled
diagram of real LEM surfaces. It is the brand pack's chosen bold element (the product itself).
Everything around it stays quiet. The nav trial button is now outlined, so the hero's filled
button is the only filled action in each fold.

## Representation plan

| Section | Must understand | Representation | Focal point | Essential copy and action | Phone |
|---|---|---|---|---|---|
| Hero | "It writes my posts, and nothing publishes until I approve it." | Split: copy left, product panel right (a diagram of the queue and a suggestion) | The panel's status pills | H1, about 40-word body, Start free trial, a See how it works text link, trial line | Copy, then the panel; H1 one scale step smaller |
| Proof strip | "The safety posture in four statements." | Short text: one ruled row of four statements, each with a drawn check | The row of checks | The early-access line and four statements; the detail lives in the safety section | Stacks to one column |
| Problem | "This is my week." | Short text on hairline rules, two columns | The heading | Heading, lede, four short statements | One column |
| How it works | "Three steps, then a short daily review." | Diagram: numbered stops on one rule (a true sequence) | The three numbers | Heading, lede, three steps | Vertical rule with a connector between stops; it ends on step 3 |
| Features | "Write, schedule, engage, measure, in that order." | Four beats, alternating sides, each with its own product visual | Each beat's panel | Beat label (the sequence), heading, body, points | Text, then the panel |
| Content beat visual | "The plan follows the buyer journey and is mostly value, not pitch." | Diagram and share bar: awareness, consideration, decision; value 70, authority 20, promo 10 | The share bar | Two labels and a legend; the numbers are the beat's own stated mix | The stages stack with down arrows |
| Safety | "Nothing goes out under my name until I say so, and here is what they cannot promise." | A ruled ledger: heading left, the five mechanisms as one list right, the limit under the heading | The first mechanism | Heading, lede, five mechanisms, the limit (still last in reading order) | Heading, list, then the limit |
| Comparison | "How this differs from doing it by hand or a general AI writer." | Table (blocks on a phone) | The LEM column header, in the accent | Heading, lede, seven rows | One block per capability (unchanged) |
| Pricing | "What it costs and what every plan includes." | Two plan cards plus the shared list | The recommended plan | Unchanged | Recommended plan first (unchanged) |
| FAQ, final CTA | Unchanged | Unchanged | Unchanged | Unchanged | Unchanged |

## Token plan

- **Colour:** `background`, `surface`, `text`, `muted`, `primary`, `on-primary`, `hairline` and
  `field-border`, light and dark, through the existing `page`, `raised`, `fg`, `fg-muted`, `accent`,
  `on-accent`, `hairline` and `field` utilities. Three alpha blends (`accent/15`, `accent/30`,
  `raised/95`) are gone, so every computed colour on the page is a DESIGN.md token.
- **Type:** Inter only. H1 at 31 px on a phone and 39 px from `sm` (`text-section-lg`,
  `text-hero`). Section titles 25 and 31 px. Step and item titles 20 px (`text-title`). Body 16 px
  on a phone and 17 px on desktop. All are on the pack's 1.25 scale.
- **Spacing and shape:** section padding unchanged. `rounded-panel` (10 px) on panels,
  `rounded-control` (6 px) on buttons, and the `panel` elevation. No new radius or shadow.
- **Browser surfaces:** text selection uses `primary` and `on-primary`. In the dark scheme the
  root scroller and the overscroll ground use the dark tokens, not the app's light grey.

## Wireframes

Desktop, 1440 wide:

```
+--------------------------------------------------------------------------+
| LEM   How it works  Features  Safety  Pricing  FAQ    Log in [Start trial]| outlined
+--------------------------------------------------------------------------+
| H1 (39px, 4 lines)              | +-- Content Studio -------------------+ |
| body (~40 words)                | | queue row  [Pending]  | suggestion  | |
| [Start free trial]  See how...  | | queue row  [Approved] | you send it | |
| 14-day free trial...            | | queue row  [Scheduled]|             | |
|                                 | +-------------------------------------+ |
+--------------------------------------------------------------------------+
| Early access ...                                                          |
| ---------------------------------------------------------------------    |
| v Posts wait...  v Engagement...  v LinkedIn sign-in  v Credentials...    |
+--------------------------------------------------------------------------+
| Problem: heading + 2x2 ruled statements                                   |
+--------------------------------------------------------------------------+
| (1)-----------------(2)-----------------(3)                               |
| Sign in             Set voice           Approve posts                    |
+--------------------------------------------------------------------------+
| Beats: text | panel, panel | text, ... (content beat: stages + mix bar)   |
+--------------------------------------------------------------------------+
| SAFETY (dark)   heading           | mechanism 1 ---------------------    |
|                 lede              | mechanism 2 ---------------------    |
|                 [the limit]       | ... mechanism 5                      |
+--------------------------------------------------------------------------+
| Comparison table | Pricing | FAQ | Final CTA | Footer                     |
```

Phone, 390 wide:

```
+------------------------------+
| LEM          Log in   [menu] |
+------------------------------+
| H1 (31px, 4 lines)           |
| body                         |
| [   Start free trial   ]     |
| See how it works             |
| 14-day free trial...         |
| +-- panel (stacked) -------+ |
+------------------------------+
| Early access...              |
| v Posts wait for approval    |
| v Engagement you send...     |
| v LinkedIn sign-in only      |
| v Credentials encrypted...   |
+------------------------------+
| (1) Sign in with LinkedIn    |
|  |                           |
| (2) Set voice, topics...     |
|  |                           |
| (3) Approve posts...         |
+------------------------------+
```

## Anti-generic and density review

- **Eyebrows:** the six decorative section eyebrows are removed. The four beat labels stay,
  because they name the steps of a true sequence (write, schedule, engage, measure) that the
  heading above them states.
- **Card grids:** the three boxed step cards and the five boxed safety icon cards are now a
  numbered rule and a ruled list. No section on the page is a grid of identical cards.
- **Repeated visuals and claims:** the content beat no longer repeats the hero's queue panel.
  The proof strip no longer restates the safety section's sentences; it keeps the four statements
  as a summary. Step 1 no longer restates the password sentence that the hero and the safety
  section already carry.
- **Glass:** the nav's translucent blur is replaced by the solid surface.
- **Numbered markers:** only on the three steps, which are a true sequence.
- **Density:** the page carries 1,263 visible words over 8,339 px at 1440 wide (about 136 words
  per desktop screen). Before the pass it was 1,368 words over 9,010 px (about 137). The page is
  shorter, but the ratio is still over the default of about 80. Cutting further means removing
  claims, which is copy work, not this pass.

## Refine passes: what each changed

1. **Critique (before captures).** Findings: two filled primary buttons in the desktop and
   tablet fold (nav and hero). Hero body at 47 words. A dead gap in the hero panel's right column.
   The nav links wrapped to three lines at 768 px. The proof strip and the safety section said
   the same four things twice. The content beat repeated the hero panel. There were three grids of
   boxed cards and six decorative eyebrows. The five-card safety grid left an orphan card. A
   heading ended on a one-word line. The footer wordmark was missing from full-page renders
   because it was lazy-loaded. Three alpha colours were outside the token list. No horizontal
   overflow and no element without visible focus.
2. **Distill.** Hero body cut from 47 to about 40 words: the API and sign-in sentences are stated
   in the proof strip and the safety section, and the hero keeps the approval, suggestion and
   password statements. Proof strip reduced to its four statements, and its sentence about the
   page itself is removed. Step 1's repeated password sentence and the six section eyebrows are
   removed. The content beat's duplicate queue is replaced by a plan diagram of facts the beat
   already states.
3. **Layout.** The nav trial button is outlined, so each fold has one filled action. The hero
   panel's right column stacks from the top. The proof strip is one ruled row. How it works is a
   numbered rule. Safety is a two-column ledger, with the limit kept last in reading order. The
   LEM column header in the comparison carries the accent.
4. **Typeset.** H1 is one scale step smaller on a phone. A non-breaking space keeps the dash on
   the line with "voice". Headings use balanced line breaks, so no heading ends on one word. The
   safety list's rules run the full column, while the text keeps the measure. The longest line
   dropped from about 79 to 68 characters.
5. **Polish.** Selection colours and the dark root scroller and overscroll ground come from the
   tokens. The nav is a solid surface. The footer wordmark loads eagerly (39 KB) and decodes
   async. The panels use only token fills: solid posting-day tiles and a hairline suggestion
   border.
6. **Audit.** Every computed colour is a pack token in both themes. Every text pair is listed
   under Contrast in DESIGN.md. 0 elements without visible focus over 29 keyboard steps. 0
   console or page errors. The only failed requests are `/api/app-info`, `/api/flags` and
   `/api/faq`, which return 502 because no API runs behind the local dev server. The FAQ falls
   back to its seeded entries, as designed. Image weight: 181 KB light, 39 KB dark.
7. **Adapt.** At 768 px the inline nav links now start at `lg`, and the menu button and sheet
   carry them below that (the overlap was pre-existing). On a phone the step rule is drawn
   between stops and ends on step 3. The plan diagram's stages stack. No horizontal scroll at
   390, 768 or 1440.

## Open points (not changed here: copy or claims work)

- The hero heading is 15 words and the body about 40. The defaults are 10 and 30. Shortening
  them is a copy change.
- The early-access line says "a small first cohort". The pricing card is badged "Most popular".
  Both are existing claims. Whether they fit a pre-launch beta is a copy decision for the owner.
- Words per desktop screen remain about 136.
