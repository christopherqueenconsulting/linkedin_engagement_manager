Mode: refine

# Landing page design pass (2026-11-24)

The logged-out front page (`src/cqc_lem/ui/src/pages/Landing.tsx` and
`src/cqc_lem/ui/src/components/marketing/`). This pass changes how the page shows its content.
It does not change what the page claims: every claim, link, analytics name, anchor and behaviour
stays. The section order, the copy owned by `docs/marketing-page-ux-spec.md` and the honesty
tests in `pages/Landing.test.tsx` are unchanged. Round 2 (below) changes the H1 wording and trims
restated sentences, and those copy changes are listed there for the owner's sign-off.

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
| Hero | "It writes my posts, and nothing publishes until I approve it." | Split: copy left, product panel right (a diagram of the queue and a suggestion, captioned as an example) | The panel's status pills | H1 (10 words), 27-word body, Start free trial, a See how it works text link, trial line | Copy, then the panel; H1 one scale step smaller |
| Proof strip | "The safety posture in four statements." | Short text: one ruled row of four statements, each with a drawn check | The row of checks | The early-access line and four statements; the detail lives in the safety section | Stacks to one column |
| Problem | "This is my week." | Short text on hairline rules, two columns | The heading | Heading, lede, four short statements | One column |
| How it works | "Three steps, then a short daily review." | Diagram: numbered stops on one rule (a true sequence) | The three numbers | Heading, lede, three steps | Vertical rule with a connector between stops; it ends on step 3 |
| Features | "Write, schedule, engage, measure, in that order." | Four beats, alternating sides, each with its own product visual and an example caption | Each beat's panel | Heading, body, points (no beat label since round 2) | Text, then the panel |
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
| H1 (39px, 3 lines)              | +-- Content Studio -------------------+ |
| body (27 words)                 | | queue row  [Pending]  | suggestion  | |
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
| H1 (31px, 3 lines)           |
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

- **Eyebrows:** the six decorative section eyebrows are removed. The four beat labels were kept
  in round 1 and removed in round 2: the section heading already names the sequence, and
  density.md allows at most two eyebrows on a page. The page now has none.
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
   tablet fold (nav and hero). Hero body at 49 words. A dead gap in the hero panel's right column.
   The nav links wrapped to three lines at 768 px. The proof strip and the safety section said
   the same four things twice. The content beat repeated the hero panel. There were three grids of
   boxed cards and six decorative eyebrows. The five-card safety grid left an orphan card. A
   heading ended on a one-word line. The footer wordmark was missing from full-page renders
   because it was lazy-loaded. Three alpha colours were outside the token list. No horizontal
   overflow and no element without visible focus.
2. **Distill.** Hero body cut from 49 to 40 words: the API and sign-in sentences are stated
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

- Round 1 left the hero heading at 13 words (14 if the em dash counts) and the body at 40,
  against defaults of 10 and 30. Round 2 brought them to 10 and 27 (see below).
- The early-access line says "a small first cohort". The pricing card is badged "Most popular".
  Both are existing claims. Whether they fit a pre-launch beta is a copy decision for the owner.
- Words per desktop screen: about 136 after round 1, about 131 after round 2 (see below).

## Round 2 (2026-11-25): fixes from the first review

Reviewer D (claims) and reviewer O (art director) failed round 1. Each fix below names its check.
Captures for this round are in a new `after-r2` folder beside the round-1 `after` folder, outside
the repository.

- **Word counts corrected (D).** The round-1 hero body was 49 words before the pass, not 47. The
  round-1 H1 was 13 words (14 counting the em dash), not 15. The corrections are made above.
- **Hero copy (O, owner sign-off needed).** H1: "Your LinkedIn content, written in your voice —
  published only when you approve it" (13 words) is now "LinkedIn posts in your voice, published
  only after your approval" (10 words, close to the footer's existing wording). Body: 40 words to
  27, "LEM publishes each post only after you approve it. It suggests comments and replies, which
  you review and send yourself. LEM never asks for your LinkedIn password." Scheduling, the
  month-long plan and follow-ups are dropped from the hero; the feature beats state each in full.
  The body is 16 px on a phone (the pack's phone body size). Result: H1 3 lines at 1440 and 390,
  body 4 lines at 390. These are copy changes to spec-owned hero copy, so the owner of
  `docs/marketing-page-ux-spec.md` keeps or reverts them before merge. The spec's hero bar still
  holds: content (H1), engagement (body) and the safety posture (H1 and body).
- **Engagement panel no longer repeats the hero (O).** The engagement beat used to repeat the
  hero's comment card. It now draws the beat's other two suggestion kinds, a reply to a comment on
  your post and a follow-up DM, each "Waiting for you". No panel content appears twice.
- **Beat labels removed (O).** The four labels above the beat headings are gone.
- **Example captions (O).** Every drawn panel carries a visible 13 px caption in the muted token
  (for example "Example queue with sample posts and a sample comment"). The fake window dots are
  removed from the panel title bar, which keeps the "Content Studio" name.
- **Status pills (O residual).** Approved and Scheduled are outlined in the primary instead of
  filled, so the hero trial button is the only filled blue shape in the fold.
- **Wordmarks (O).** `lem-wordmark.png` (an exact copy of the logo package's
  `02_Main_Full_Horizontal_Transparent.png`) and `lem-wordmark-on-dark-v2.png` are resized from
  1340x300 to 286x64, 2x the 143x32 display, with macOS `sips`: 145,054 B to 17,336 B and
  40,485 B to 12,354 B. Both have `assets.json` entries with source, sha256 and `max_display_px`.
- **Density (O, partial).** Cut only what is stated again on the page: the content beat's
  30-day-plan and 70/20/10 points (its panel draws both), the engagement beat's reply point (its
  panel draws it and its body states it), the scheduling beat's two-to-seven point (moved into its
  panel's note), and the second and third sentences of the problem statements (they restate the
  heading's "daily job", the comparison's "an hour a day" and the safety section's answer). Words
  per desktop screen went from about 136 to about 131 (1,185 words over 8,132 px). Reaching 90
  needs claims removed, which is copy work for the owner. Comparison cells were already at most 8
  words, so they are unchanged.
- **API in captures (O).** A loopback fixture server answered `/api/app-info`, `/api/flags` and
  `/api/faq` behind the dev-server proxy. The flags are `bootstrap_payload()` from
  `utilities/flags.py` with PostHog off (the env defaults), and the FAQ is the six rows the
  `faq_entries` migration seeds. `failed_requests` is empty in every view.
- **Not done: a real app screenshot as the hero (O).** DESIGN.md asks for a real screenshot of the
  app as the bold element. This round keeps the drawn panel, now captioned as an example and
  without window dots. A screenshot needs the logged-in Content Studio running on synthetic data,
  plus light and dark image assets, and the brand owner may instead rule that the captioned
  diagram stays. That choice is the owner's.

Round 2 checks: captures at 1440, 768 and 390, light and dark, complete and styled, no blocked
hosts. No overflow. `no_visible_focus` empty over 29 steps. 0 console errors, page errors or
failed requests. Only DESIGN.md colour tokens. Inter only. Longest line 68 characters at 1440.
