Mode: refine

# Landing page design pass (2026-11-24)

The logged-out front page (`src/cqc_lem/ui/src/pages/Landing.tsx` and
`src/cqc_lem/ui/src/components/marketing/`). This pass changes how the page shows its content.
It does not change what the page claims: every claim, link, analytics name, anchor and behaviour
stays. The section order, the copy owned by `docs/marketing-page-ux-spec.md` and the honesty
tests in `pages/Landing.test.tsx` are unchanged. Rounds 2 and 3 (below) cut copy for density; the
owner reverted those copy cuts on 2026-11-24 and recorded a density override instead (Round 4,
below, and `brand/lem/DESIGN.md`). The page carries the round-1 hero and the round-2 copy
elsewhere, word for word, in the pass's layout.

- **Brand pack:** `brand/lem/DESIGN.md` and `brand/lem/assets.json`. LEM tokens only; no new
  token, colour or font. Round 3 adds three owned screenshot images, each with an `assets.json`
  entry.
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

Since round 3: a real screenshot of LEM's Content Studio queue ("Review & Edit" tab) on sample
data, as DESIGN.md Rationale's last bullet asks. Rounds 1 and 2 used a drawn panel here. The
crop starts at the tab row, so the app heading's filled "Generate Weekly Content" button is not in
the fold. Everything around the screenshot stays quiet. The nav trial button is outlined, so the
hero's filled button is the only filled action in each fold.

## Representation plan

| Section | Must understand | Representation | Focal point | Essential copy and action | Phone |
|---|---|---|---|---|---|
| Hero | "It writes my posts, and nothing publishes until I approve it." | Split: copy left, a real Content Studio screenshot right (sample posts, captioned as an example) | The screenshot's status pills | H1 (10 words), 27-word body, Start free trial, a See how it works text link, trial line | Copy, then the app's own phone-width screenshot; H1 one scale step smaller |
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
| H1 (39px, 3 lines)              | +-- screenshot: Content Studio -------+ |
| body (27 words)                 | | tabs, filters                       | |
| [Start free trial]  See how...  | | post card          [PENDING]        | |
| 14-day free trial...            | | post card          [APPROVED]       | |
|                                 | | post card          [SCHEDULED]      | |
|                                 | +-------------------------------------+ |
|                                 | Example queue with sample posts         |
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
- **Density (round 1; superseded by Round 4 below):** the page carried 1,263 visible words over
  8,339 px at 1440 wide (about 136 words per desktop screen). Before the pass it was 1,368 words
  over 9,010 px (about 137). Round 3 brought it to 709 words over 7,129 px (89.5); Round 4 restores
the copy under the density override, at 1,198 words over 8,297 px (about 130).

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
   back to its seeded entries, as designed. Image weight then: 181 KB light, 39 KB dark
   (superseded: about 30 KB after round 2's wordmark resize, about 72 KB light at 1440 after
   round 3's screenshot).
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

## Round 3 (2026-11-25): fixes from the second review

Superseded for copy by Round 4: the owner reverted every copy cut listed under "Density" below. The
hero screenshot and the other non-copy changes stay.

Reviewer O failed round 2 on two items, the hero image and density. Both are fixed below. Captures
for this round are in a new `after-r3` folder beside `after` and `after-r2`, outside the
repository.

- **Hero is a real app screenshot (O fix 1, option a).** The drawn hero panel is replaced by
  screenshots of LEM's own Content Studio "Review & Edit" tab. They were rendered from this
  repository at d1c21ed4 by the Vite dev server in headless Chromium. A loopback fixture API served
  only synthetic sample data: three sample posts (pending, approved, scheduled) and a sample user at
  example.com. Each image is one contiguous crop, from the tab row to the last post card, with
  nothing composited or retouched. There are three files in `public/product/`. The first is
  `content-studio-queue-1440.webp`: 1440x1312, 44,362 B, shown at 652 px at 1440 and 720 px at 768.
  The second is `content-studio-queue-720.webp`: 720x656, 24,494 B, the srcset 1x candidate. The
  third is `content-studio-queue-phone-780.webp`: 780x1600, 50,126 B, the app's own 390 px layout,
  used below 600 px so the phone text stays legible (358 px displayed at 390). Each has an
  `assets.json` entry with licence `owned`, source, sha256 and `max_display_px` (720, 720 and 568).
  The visible caption "Example queue with sample posts" is kept. The figure is held to 720 px when
  the hero stacks. The app has no dark theme yet, so the dark page shows the same light screenshot,
  framed by the hairline border. The drawn hero variant, and the hero-only comment pane, are
  removed from `ProductPanel.tsx`. The feature beats keep their captioned diagrams.
- **Density (O fix 2, option 1: cuts, for the spec owner's sign-off).** At 1440 the page went from
  1,185 words over 8,132 px (131 words per screen) to 709 words over 7,129 px. That is 89.5 words
  per 900 px screen, against the 90 target. These are copy changes to copy owned by
  `docs/marketing-page-ux-spec.md`, so its owner keeps or reverts them before merge:
  - Features: one sentence per beat and no bullet lists. The section lede "Four beats, in the order
    they run." is cut. The beat bodies now read: "A voice profile distilled from your own posts and
    profile drives every draft." / "Slots land in your golden and peak hours, in your own timezone,
    on the days you allow." / "LEM drafts comments, replies and follow-up DMs as suggestions; you
    send the ones you want yourself." / "The numbers come from your own LinkedIn account, not a
    vanity chart." Some cut bullets no longer appear anywhere on the page: the list of post formats,
    slop lint, weekdays by default with weekends opt-in, golden-hour placement measured from the
    real publish time, self-healing carousels and asset backfill, and the comment-targeting rules
    (topics, keywords, authors, minimum reactions). Others are now stated elsewhere: the quality
    contract (safety ledger) and appreciation DMs (folded into "follow-up DMs"). The milestone note
    is shortened and keeps its "no API" disclosure.
  - Comparison: 7 rows to 4. The DM row is folded into "Suggests comments, replies and DMs you send
    yourself". "Plans a month across a buyer journey" is cut; step 3 and the content beat's panel
    state it. "Shows how each published post performed" is cut; the measurement beat states it. The
    lede is cut to "Doing it by hand works; it just costs an hour a day." The voice and approval
    rows still concede to the alternatives.
  - Pricing: the "Included in every plan" list is removed. Its six lines restated the beats, the
    safety ledger and the comparison. Some of its items appear nowhere else on the page now:
    per-day caps, targeting rules and human-paced timing, seed comments, comment outcomes and the
    suppression tripwire. The tier-difference sentence stays. `INCLUDED_IN_EVERY_PLAN` stays in
    `constants/plans.ts` (its test still pins it); the page no longer reads it.
  - Safety ledger: each entry is one sentence. The section lede is cut. The limit now reads "no tool
    can guarantee a LinkedIn account. You can edit any draft, approve nothing or send no
    suggestion, and you remain responsible for your own account." Cut: "and anyone who tells you
    otherwise is selling" and "per column" in the encryption entry. The three sentences pinned by
    `Landing.test.tsx` are kept word for word.
  - Problem: the four statements stand alone, with no line under each. The lede is now "The same
    problem, twice a day, every weekday."
  - How it works: the lede is cut, and step 1 has no line under it. Step 2 now reads "Tone, topics,
    the authors worth engaging with, and your posting days." Step 3 now reads "A 30-day plan lands
    for review, and approved posts publish at their slots."
  - Final CTA body: "See your first plan before you decide anything."
  - Captions: "Example week, for illustration", "Example plan, for illustration", "Example
    suggestions, sample text" and "Example layout, without data".

Round 3 checks: the after-r3 capture is complete and styled, with no blocked hosts. No overflow at
390, 768 or 1440. `no_visible_focus` is empty over 29 steps. 0 console errors, page errors or
failed requests. Only DESIGN.md colour tokens, and Inter only. Longest line 68 characters at 1440.
Image weight: 74,052 B light and 56,716 B dark at 1440, 79,816 B light at 390.

## Round 4 (2026-11-25): density override, copy restored

Owner decision, 2026-11-24: revert the round 2 and round 3 copy cuts and record a density override
instead. `brand/lem/DESIGN.md` now has a "Density override (marketing landing)" section: this page
carries the full feature story owned by `docs/marketing-page-ux-spec.md` and runs above the generic
words-per-screen guide. The override covers this page only.

- **Copy restored word for word.** The hero is back to its round-1 text (the H1 and 40-word body
  before round 2's rewrite). Everything round 3 cut or reworded is back as it stood at the round-2
  head; round 2's removals of lines the beat panels already draw stay removed. Restored: the features lede and each beat's full body
  and points (post formats, the quality contract and slop lint, weekdays by default with weekends
  opt-in, golden-hour placement measured from the real publish time, self-healing carousels and
  asset backfill, the comment-targeting rules, appreciation and follow-up DMs, post and audience
  statistics); the milestone note's full sentence; all seven comparison rows and the two-sentence
  lede; the "Included in every plan" list (per-day caps, targeting rules and human-paced timing,
  seed comments, comment outcomes, the suppression tripwire); the safety lede, the five full safety
  entries and the full limit; the problem lede and the line under each statement; the how-it-works
  lede and all three step lines; the final CTA body; and the four panel captions.
  `Landing.test.tsx` goes back to matching the restored H1.
- **Kept from the pass.** The hero screenshot, the outlined nav button, the numbered step rule, the
  ruled safety ledger, the beat panels, the removed eyebrows and beat labels, the token-only colours,
  the wordmark resizes and every other layout change.
- **How the restored copy is shown.** Nothing is hidden behind a toggle, tab or hover: every line is
  visible and in the DOM, so a crawler or a screen reader reaches it. The beat points are a hairline
  ruled list, one capability per line, held to the reading measure. The "Included in every plan"
  list is two columns of ruled lines under the plan cards, not a boxed card. The comparison stays a
  table (one block per capability on a phone).
- **Captures.** A new `final` folder beside the round folders, outside the repository: landing at
  1440, 768 and 390, light and dark.

Round 4 checks: capture complete and styled, no blocked hosts. No overflow at 390, 768 or 1440.
`no_visible_focus` empty over 29 steps. 0 console errors, page errors or failed requests. Only
DESIGN.md colour tokens, and Inter only. Longest line 68 characters at 1440 and 55 at 390. 1,198
words over 8,297 px at 1440 (about 130 per 900 px screen), 1,236 words at 390, within the override.
Image weight: 72 KB light and 55 KB dark at 1440, 77 KB light at 390.
