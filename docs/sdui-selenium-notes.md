# LinkedIn SDUI Selenium gotchas

Full detail for the SDUI DOM/composer invariant CLAUDE.md's "Known Gotchas" only states in one
line. Which surface is covered by which read-only probe, and the weekly drift cron that grades them
all: **`docs/sdui-probe-coverage.md`**.

## The two fix invariants (issue #1013)

Every SDUI fix obeys these. They are not style — each one is the direct lesson of a shipped
incident, and both failures looked like success at the time.

### 1. Success is the OUTCOME being present, never a click having landed

A `click_first` that returns an element proves a control was clickable, nothing more. #1012's
invite path clicked a button and reported success while no dialog had opened for the target at all.
Gate on the thing you were trying to produce: `_connect_dialog_present` (the dialog's own controls),
`_composer_submitted` (the comment is in the thread), `read_pending_invites` (the row is gone). If
you cannot read the outcome, the honest verdict is `unknown` — and `unknown` must SKIP, never
proceed as if it were success.

The same rule reads backwards for a walk that finds nothing: **zero items is not "nothing to do"
until the page agrees.** Cross-check against an anchor the walk does not itself depend on
(`report_zero_walk` / `zero_walk_verdict` in `utilities/linkedin/zero_walk.py` — the ONE grader,
aliased in each `app/engagement/*` module under the `_`-prefixed names their call sites already
used; each aliases the upstream original so none imports another),
because a rotated selector
answers zero to both questions. `drift` there warns — once is a warning, repeatedly is a defect, and
repeated selector rot is exactly the defect that should file itself. An empty page and an unreadable
cross-check stay DEBUG.

### 2. Never click a control whose label names a different entity than the target

#1012's `//main//button[contains(@aria-label,"Invite ")]` was unscoped, the profile top card
carried no Connect button for the target, and the "More profiles for you" rail carried one per
suggested person — so the click sent a connection request to a **stranger**, ~20 times in a day. A
control whose label names somebody must be attributed to the target before it is clicked, and a
control that cannot be attributed is precisely the one that must never be clicked. Prefer an
addressable route (the `/preload/custom-invite/?vanityName=<slug>` URL) over hunting a button, and
scope every locator to the owning card or dialog.

## Anchors are gone

The old `urn:`, `feed-shared-*`, and `comments-comment-*` DOM anchors no longer exist. Prefer
`data-testid` / `aria-label` selectors via `find_first`/`click_first`.

## The home feed's sort control is not a `<button>` (#1108)

Measured 2026-08 by the weekly drift sweep (`--feed-sort`, `/feed/`, user 1). The probe enumerates
every **displayed `<button>` in document order** and caps the list at 40. That capture ran from the
global nav (`Home, 1 new notification` / `Me` / `For Business`) **straight into the first post's
controls** (`Open control menu for post by …`), then spent the whole cap on the next seven posts.

Read that literally: between the navigation bar and the first feed card there was **no displayed
`<button>` at all** — not the sort control, and not the share box's `Start a post` either. So the
verdict "NO sort control resolved" was not a label rotation. Every route in `_FEED_SORT_LOCATORS`
except the last required `self::button`, and the last one required `role='button'` *plus* `sort` in
the `aria-label`; nothing matching either shape exists in that region of the page any more.

What changed in response:

- `_FEED_SORT_LOCATORS` now keys on the **interactive affordance** — `self::button or self::a or
  @role='button' or @role='combobox' or @role='listbox' or @aria-haspopup` — instead of the tag, so
  the chain resolves whichever element LinkedIn shipped as long as it is still reachable. The
  option chain gained `self::a` and `@role='radio'` for the same reason. A route matching a link
  whose own `href` carries `sortBy=` / `sortType=` **and** names `/feed` was added last: navigating
  beats clicking when the page offers it (#1030), but an unguarded `sortby=` match would resolve a
  URL somebody *shared in a post* and walk the session off the page the scan is about to read.
- The exact-text route stays **exact** (`normalize-space()='top'`), never `contains`. A `contains`
  match on 'recent' would happily resolve someone's post — the #1013 wrong-entity hazard.
- `probe_feed_sort` now also reports **`sort_candidates`**: the first 20 displayed interactive
  elements inside `main`, in document order — minus the off-feed links the walk skips, and taken
  from at most `scan_limit` (200) elements — each with tag / role / `aria-label` / `data-testid` /
  text / `href` / `aria-haspopup`. `visible_controls` alone could not re-ground this drift — it
  reports `<button>` labels, and the finding *was* that there are no buttons there. It is emitted
  before `visible_controls` because the issue body truncates the evidence blob at 6000 chars.
  The filter and the bound are the 2026-08-16 grounding finding, described in full below.
- **A missing control is only a WARNING once the page proves it rendered posts.** The production
  lookup passes `warn_on_miss=False` and hands the miss to `report_zero_walk` against
  `button[aria-label^='Hide post by']` — a per-post anchor the sort chain does not use. A dead
  session, a login wall and a rotated anchor all return `None` from `find_first`; only the last is
  a defect worth paging on. The returned state is `FEED_SORT_MISSING` in all three cases, so #817's
  "an unsorted scan is never read as recency-sorted" is unchanged.

### What it actually is — positive sighting, 2026-08-16

Two `--feed-sort` runs against a live session (user 1, debug node) both graded **`ok`** and flipped
the feed to Recent — `control_found: true`, `sort_before: "top"`, `option_found: true`,
`sort_after: "recent"`. The control the widened chain resolved to:

```json
{"tag": "div", "role": "button", "text": "Sort by: Top"}
```

Read the absences, they are the finding: **no `aria-label`, no `data-testid`, and not a `<button>`**.
So the chain hangs on route 3 — the visible-`sort by` text route — with the affordance clause
(`@role='button'`) doing the work the tag used to do. Routes 1 and 2 were both blind on this reading,
and the exact-name route cannot cover it either: the label is `Sort by: Top`, not `Top`. Dropping the
text route as "redundant with the aria-label one" leaves nothing behind it, which is what
`TestTheLiveControlThisChainWasGroundedOn` (`tests/unit/app/test_feed_sort.py`) exists to fail on.

The production evidence scan (#1270) saw the same widget from the other side: four **nested** divs
all reading `Sort by: Recent`, only ONE of them carrying `role='button'`. That is why the affordance
clause is the filter and the text is only the label — a text-only route would resolve a wrapper.

**The capture that was still blind.** On the same run, `sort_candidates` — the capture written to
re-ground this very chain — did not contain the control. `main` does not open at the share box:
it opens at the **left rail**, whose off-feed links (`/in/<me>/`, two `/company/<id>/admin/`s,
Premium, Saved items, Groups, Newsletters, Events, Sales Navigator) took 17 of the 20 rows, the share
box took the last 3, and the sort control sits immediately after. `feed_sort_candidates` now drops a
link whose own `href` does not name `/feed` — the same rule the chain's link route already enforces,
since a trigger that navigates has to stay on the feed (#1030). Rows with an unreadable or absent
`href` are kept: dropping what we could not read would hide the element the capture exists to find.

Re-measured on the filtered walk (two further `--feed-sort` runs, both `ok`): the control now lands
**fifth** in `sort_candidates`, behind `Try Premium Page` and the share box's three, and ahead of the
first post's furniture —

```json
{"tag": "div", "role": "button", "text": "Sort by: Recent"}
```

— reading `Recent`, not `Top`, because the capture is taken AFTER the flip. So the next re-grounding
pass gets the element in the capture written for it, not just in `selector_evidence`.

## A short comment thread has NO sort control — the miss was never drift (#1117, follow-up of #818)

Live-grounded 2026-08-14 with four `--comment-outcome-url` runs against posts that had actually
emitted `Selector miss: Comment sort control` in production (pulled from the warning's own `url`
attribute in PostHog Logs). Threads of 1 and 2 rendered comments. Every run graded `drift` and
returned the *same* single candidate from the evidence scan's header pass:

```json
{"tag": "button", "data_testid": "", "role": "", "has_popup": "", "text": "",
 "aria_label": "Open control menu for post by <post author>", "reason": "header"}
```

Nothing anywhere in the main column named a sort — no `keyword` row on any of the four. Read that
literally: **LinkedIn renders no comment sort control on a short thread**, because there is nothing
to sort. This is the opposite of the home-feed finding above; the two surfaces drifted differently
and only the feed's is a rotation.

The chain is not rotted, and the production data says so from the other side: over the same period
`_COMMENT_SORT_LOCATORS` read `most relevant` on **21 of 22** checked readings.

What changed in response:

- **The evidence scan is the cross-check now, not the rendered thread.** `_read_comment_outcome`
  reads the label with `warn_on_miss=False` and `_report_sort_control_miss` picks the level from
  what the scan found: a `keyword` row means the page still NAMES a sort the chain cannot reach —
  drift, and it warns — while a `header`/`unanchored`/empty scan is an affordance LinkedIn did not
  render, and logs DEBUG. The `sdui_selector_evidence` event is emitted either way, so a surface
  never looks un-drifted because its evidence was dropped.
- Before this, 21 warnings and 2 grouped `$exception`s over 10 days were filed against working
  behaviour — a rendered thread was too weak a cross-check (#1063) once it turned out a normal
  1-comment revisit has no control to find.
- `probe_comment_outcome` reads the label silently too. The four runs above each wrote a
  `Selector miss` warning into production error tracking; a diagnostic that files defects is a
  diagnostic nobody can afford to run.

- **The reading follows the evidence too** (owner decision 2B on #1117). A comment we FOUND on a
  thread whose scan names no sort at all records `visible_most_relevant = 1`, not NULL: LinkedIn
  offered no ordering, so every comment is shown and there is nothing to be demoted within. NULL had
  meant this reading was excluded from the demotion denominator exactly like a genuinely unreadable
  one — the starved denominator #818 is really about (18 of 24 checked readings before 2026-08-05).
  The gate is the same `keyword` row that decides the level, so the inference never fires on a page
  that still NAMES a control we cannot reach. It is not a proof of no drift: a label rotated to
  wording with no sort word in it produces `header` rows exactly like an absent control does, which
  is why the direction matters — the inference can only UNDER-report demotion, softening the
  commenting hold, never falsely tripping it.
- **A blind scan is NOT an absent affordance, and reads NULL.** `scan_sort_control_candidates`
  returns `[]` both for "nothing describable" and for "the read failed", deliberately not telling
  them apart, so inferring from `[]` would file an `execute_script` fault as a healthy reading — the
  "we couldn't tell" → "fine" collapse the three-valued column exists to prevent. A page that
  rendered comments has header controls to describe, so `[]` is the abnormal case. The LEVEL decision
  is unaffected: evidence we do not have never warrants a warning either.

## The Catch-up feed is full SDUI — no `data-view-name`, no `<li>` cards

Live-grounded 2026-08-03 (`/mynetwork/catch-up/all/`, user 1): each card is a
`div[role='listitem']` (componentkey UUID) inside the LazyColumn
(`div[data-testid='lazy-column'][role='list']`) under
`div[data-sdui-screen='com.linkedin.sdui.flagshipnav.mynetwork.CatchUpAll']`. The page renders
**zero** `data-view-name` attributes and no `<li>` around cards, so the original
`_CATCHUP_CARD_LOCATORS` chain (data-view-name first, `main li` fallbacks) matched nothing on a
feed visibly showing ten moments — the scan reported `no_moments` daily while working "correctly".
Grounded chain now leads with `div[data-sdui-screen*='CatchUp'] div[role='listitem']`. Ads and
prompts also render as listitems; the profile-link + classifier funnel filters them.

**The "Say congrats" chip/dialog is gone too (#1774, live-grounded 2026-08-31).** The
`--catchup-cards` probe's `default_response` reading matched `_CATCHUP_SUGGESTED_TEXT_LOCATORS` and
`_CATCHUP_MESSAGE_TRIGGER_LOCATORS` on **0 of 10** classified cards that day — every touch was
silently falling back to `_CATCHUP_DEFAULT_CONGRATS`'s generic one-liner instead of LinkedIn's own
suggestion. The current render carries the full draft on the card's own `a[href*='/messaging/compose/']`
anchor: the `body` query param IS the congratulations LinkedIn wrote (`aria-label` — "Message Jane:
<text>" — is the fallback when the URL is unreadable), needing no click and no dialog at all.
`_card_message_link_suggested_text` reads it first; the old chip/trigger locators stay as a
fallback in case LinkedIn rotates back.

## The profile-views analytics list is full SDUI — no `<ul>`, TEXT is the only anchor

Live-grounded 2026-08-03 (`/analytics/profile-views/`, user 1): the page renders **zero** `<ul>`
elements and no `artdeco-entity-lockup__*` classes, so the original
`//ul[@aria-label="List of Entities"]//a[...]` walk matched nothing on a page visibly listing
136 viewers — every run warned `Could not read the profile viewers list` and engaged nobody.
A viewer row is an `/in/` anchor (componentkey UUID) whose innerText carries a
`Viewed 1h ago`-style caption line (`1h`/`20h`/`1d`/`1w`/`1mo`/`2mo`); non-viewer profile
anchors ("Interesting viewers", nav) carry no such line, so the caption IS the discriminator.
The walk reads name+href+caption in ONE `execute_script` pass (`_PROFILE_VIEWER_ROWS_JS`) —
per-element XPath reads go stale as the list re-renders. `window.scrollTo` alone never grows
the list; `scrollIntoView` on the LAST row is what triggers the lazy loader (8 → 58 rows).
Zero rows against a non-zero "N Profile viewers" headline stat is selector drift and warns;
zero rows with no stat is a quiet no-op. Re-ground with
`scripts/linkedin_live_validation.py --profile-views`.

## Recommendations Received has no list item, no `<time>` and no `role=tab` (#1007)

Live-grounded 2026-08-03 (`/in/<self>/details/recommendations/`, user 1): hit-counts on the live
page were `main li` **0**, `main time` **0**, `[data-view-name]` **0**, `[role='tab']` **0**,
`div[role='listitem']` 3, `main div[role=list]` 1, `main a[href*='/in/']` 24. So every rung of the
original card ladder and all three `_RECOMMENDATION_TAB_LOCATORS` were unmatchable —
the recommendations half of #968 read 0 cards forever while the mentions half worked. The one
`main div[role=list]` is the **footer help-links list** ("Questions? / Visit our Help Center"), the
same junk trio the catch-up grounding hit; never anchor on it.

What the page does render is one `/in/` anchor per card carrying `name · degree · headline` in its
own innerText, inside a block that also carries the card's date line
(`April 25, 2012, Uday was Christopher's client`). `_RECOMMENDATION_ROWS_JS` reads it in ONE
`execute_script` pass: climb from each anchor to the outermost ancestor still about that ONE
profile slug — stop as soon as a second slug joins, which also keeps a card whose avatar and name
are two anchors to the same person intact — and keep the block only when its text matches the
date regex. The "Who your viewers also viewed" rail drops out because its rows carry no date.
Zero cards on a page whose text DOES carry a date is drift and warns (`page_dated`); zero with no
date is a quiet no-op. Tab mapping: the bare URL defaults to **Received**, so the tab click is
only ever a correction; `?detailScreenTabIndex=2` is **Pending**, whose rows read
"Requested"/"Sent" and are recommendation *requests*, never thank-worthy. Re-ground with
`scripts/linkedin_live_validation.py --appreciation-sources`.

The probe is piped into a worker running the DEPLOYED image, so a read rebuilt on a branch is not
there to import — and a reader that can only be grounded AFTER it merges is exactly how this ladder
shipped dead. As with the feed-sort chain, the probe drives `_recommendation_reading` when the
running image has it and an identical carried copy when it does not, and the reading names which
(`read_source: image | script`; a `script` reading has grounded THIS BRANCH, not what is deployed).
`TestRecommendationReadCopy` fails the build if the copy drifts from the shipped read.

## The Connect invite is a URL, and unscoped "Invite …" buttons are a WRONG-PERSON hazard

Live-grounded 2026-08-03 (3rd-degree profile, user 1, Sales-Nav overlay): the profile top card
offers only `Save in Sales Navigator` / `More` / `Message`/`Follow` — **no Connect button for the
target at all** — while the "More profiles for you" rail renders one
`button[aria-label="Invite <someone else> to connect"]` per suggested person. The old unscoped
`//main//button[contains(@aria-label,"Invite ")]` therefore clicked the rail and **sent a
connection request to a random suggested person** (~20 strays on 2026-08-03), then failed with
`no Send button on the open Connect dialog` because no dialog ever opened for the target. Never
click an Invite control whose label names someone other than the target.
The top-card More menu (`aria-label="More"`, no longer `"More actions"`) holds Connect as an
`<a role="menuitem">` (text `Connect`, no aria-label) whose href is
`https://www.linkedin.com/preload/custom-invite/?vanityName=<slug>` — the dialog is addressable
by URL, so `_open_connect_invite_dialog` navigates there directly and only falls back to the
menu. The dialog's own controls are UNCHANGED: `Add a note` / `Send without a note` aria-labels,
`textarea#custom-message` (0/300), and `Send` (aria `Send invitation`, disabled until input, plus
a new `Write with AI` button). Success is the dialog's controls being present — never a click
having landed.

### Re-grounded 2026-08-29 (#1733): the preload URL is a ROUTE, not a page

Between 2026-08-12 and 2026-08-29 every automated connection request failed with
`No Connect option on this profile` — 20 of them, across 20 distinct profiles, on an image that
already carried all three routes (#1734's direct button included). Three profiles were probed live
(`--connect-dialog … --connect-open-more-menu`). What they said:

* **`driver.get("https://www.linkedin.com/preload/custom-invite/?vanityName=<slug>")` renders a
  completely blank document.** `page_copy_sections` came back `{"main": "", "body": "",
  "dialog": ""}` and `visible_controls` `[]` on all three. It is an in-app route, not a page, so it
  has to be CLICKED where the profile renders it. That killed route 1 outright — and route 3 too,
  because the More menu's Connect item is a link to the same place.
* **Two layouts, and neither is "the" layout.** Two of three profiles carried NO Connect control on
  the top card and an `<a role="menuitem" href="/preload/custom-invite/?vanityName=…">Connect</a>`
  inside the More menu. The third carried
  `<a aria-label="Invite <Owner> to connect" href="/preload/custom-invite/?vanityName=…">Connect</a>`
  on the top card and **no** Connect item in its More menu.
* **`_PROFILE_CONNECT_BUTTON_LOCATORS` cannot see either.** It matches `//main//button`; both
  controls are `<a>`. `visible_button_labels` shares that blind spot — it enumerates
  `By.TAG_NAME, "button"` only, the same blind spot recorded above for the feed-sort control, which
  is why `profile_controls` looked empty of Connect on a page that had one. `top_card_controls`
  exists to close it.

So the shipped route is: click the target's OWN custom-invite anchor, wherever the page put it
(top card first, then inside the More menu), and keep the URL navigation only as a last-resort
fallback for an account where it still works.

**The href is a harder #1012 guard than any label.** A "More profiles for you" anchor carries THAT
SUGGESTED PERSON's `vanityName`, so requiring the anchor's own slug to equal the target's is
machine-checkable identity, not name-matching — and it is checked in Python (`_anchor_invite_slug`),
so no locator literal names a person and the rail-hazard regression test needed no edit. Two rules
that fall out of it and must not be softened: compare the slug for **exact equality, never a
prefix** (`chris` must not match `chris-queen`), and click **the element that was validated**, never
a re-lookup of the same XPath — a re-lookup returns whichever custom-invite anchor the page yields
first, which on a profile carrying the rail is a stranger's. That re-lookup IS #1012.

### An invite limit is not selector rot

A weekly-invitation ceiling or an account restriction reads the same on every profile, so grading it
as drift files a code defect against locators that are fine and lets the scanner re-dispatch the
whole queue into the same wall — each attempt a full automated profile visit. `invite_limit_signal`
(probe) and `_invite_restriction_reason` (production) read the page's own words for it; the probe
grades a restricted reading **`unknown`, never `drift`**, and production HOLDS the invite lane
(`hold_invites`) instead of failing rows. Both fail closed on an unreadable page — a restriction is
a claim, and a claim needs evidence.

## Profile experience rows: the a11y twin, not a line index (#970)

`/details/experience/` renders most text **twice** — a visible `span[aria-hidden="true"]` beside a
`visually-hidden` twin carrying the same string. The pre-SDUI parser never knew that; it split an
`<li>`'s whole text and branched on the count of leading blank strings
(`start_identifier_map`: 20 = company, 16 = title, 7 = description), then halved each line
(`row[si][:len(row[si]) // 2]`) to undo the duplication. Both halves of that are positional: one
extra wrapper shifts every index, and the parser then emits a confidently-wrong company or title
rather than nothing. Profile JSON is dumped whole into `synthesize_profile`, so that garbage grounds
every comment and DM written for the user.

The rebuild (`parse_profile_experiences`) reads the **visible** half only, keeps entity nodes by
`data-view-name` / `role='listitem'` / `<li>` (outermost wins, so a grouped company stays one unit),
and anchors a role on its **date-range line** — no date line, no experience, and an entity it does
not understand yields nothing instead of junk. A grouped company is told from a single role
STRUCTURALLY (does the entity nest child role entities?), because
`company / title / dates` and `title / company / dates` are the same three lines in a different
order. Ground it with `--profile-experiences <profile-url>`; `entities_with_dates` is the number
that separates "page never rendered" from "line grammar moved".

### What the live run (2026-08-03) changed

The first grounding pass on a real `/details/experience/` page found three things the captured-DOM
tests could not have:

- **`data-view-name` is absent from that page entirely**, and the most specific rung that DID match
  — `div[data-sdui-screen] div[role='listitem']` — matched the **footer's help links**
  ("Questions? / Visit our Help Center."). The real entries were the 8 `main li` under
  `main div[role='list']`. Specificity alone picks chrome over content, so a rung now only wins if
  at least one of its nodes **carries a date range**: a page's chrome can out-specify its content,
  it can never out-date it. (An undated rung is still returned when no rung is dated, so the probe
  reports what did render.)
- **No doubled markup at all** on that render. Reading it through a stray decorative
  `aria-hidden` icon would have returned one icon's worth of text as the whole entity, so the
  `aria-hidden` half is used only when it actually **covers** the node's text.
- **Lines are laid out, not text-noded.** `get_text("\n")` splits `Mar 2019 - Present · 7 yrs 6 mos`
  into three lines across its inline spans and takes the date anchor with it; `_rendered_lines`
  joins inline runs and breaks on block elements instead. Skills arrive comma-separated with a
  `+9 skills` overflow chip.

The company is not always on the role: when the roles are the `li`s, the grouping names the company
once above them. `_company_from_ancestors` reads it from the container's leading lines. A header
without a total-duration line (a bare "Experience" heading) is never a company.

**Which group a role belongs to is decided POSITIONALLY, against the last date line (#1096).** The
2026-08-07 re-probe found the flat shape — 8 `main li` siblings, `main div[role='list'] li` = 0, the
company header a run of divs *beside* the `<ul>` rather than one of the `li`s — and 7 of the 8
entities came back with `company_name: ""`, because the walk stopped at the first dated leading run
(role #2's leading run is role #1, which is dated). `_company_for_leading` splits the leading lines
on their date lines instead and reads the runs between:

- a company header in the run **after the last date line** starts a NEW group — company B's roles
  must never inherit company A, which is the failure #970 exists to kill;
- **no** header since the last role means this role is that role's sibling, so the group's own
  header still applies. Requiring that run to be empty would blank every role whose predecessor
  carries a description — i.e. the live page itself;
- nothing header-shaped anywhere leaves the company blank. A blank is honest; a guessed company is
  the confidently-wrong row.

The residual risk is a multi-role company header rendered with no total-duration line: it would read
as "no header" and its roles would inherit the company above. LinkedIn always renders the total for
a grouped company, and a role that names its own company on a subtitle line never reaches this walk
at all. `--profile-experiences` reports `experiences_without_company` so the next drift is visible
in the JSON.

### The render with NO markup vocabulary at all (2026-08-14, #1465)

Probing a **2nd/3rd-degree** profile — the ones the profile-viewer lane scrapes — found a third
shape: `main li` = 0, `data-view-name` absent, and the only `role='listitem'` nodes on the page were
the same three footer help links. Each role sits in bare `div`s inside an `<a href=".../company/<id>/">`,
with no `role`, no `data-*` and no `aria-*` attribute anywhere in its ancestor chain, and its date
range written `2000 – Present` (en dash, no duration). Every rung of the ladder therefore matched
only the footer, the page parsed to nothing, and production filed a `RecurringWarning: Profile
experience page rendered dated entries but none parsed` about a page that was rendering its
experience perfectly.

The lesson is that the ladder can only ever ask for a vocabulary LinkedIn happens to be using this
week, so `_dated_block_nodes` is the fallback for when it is using none: each date line's deepest
element grows upward while its ancestor still holds exactly **one** date line — the first ancestor
holding two is the group above it, never the role — which cuts the whole role block out without
naming a tag or a class. It runs **only when no rung is dated** (a rung that matches is more precise
about where an entity starts), stops at a section heading, and is bounded to 4 ancestors so a
one-role page cannot swallow itself. `experience_entity_nodes` reports it as
`<dated-block fallback>`, and `--profile-experiences` now ships `dated_line_containers` — the
ancestor chain of each dated line, keyed on `role` / `data-*` / `aria-*` / `href` only — so the next
render's shape is in the first probe report instead of a hand-written second pass.

## The degree badge is a leaf node's TEXT, never a class

`span.dist-value` / `span.distance-badge` were confirmed dead on the same 2026-08-03 grab. Both are
class anchors, and every class anchor on the profile is now hashed. What the top card still writes
is the degree itself, as its own leaf node whose entire text is `1st` / `2nd` / `3rd+` (sometimes
`· 2nd`, sometimes spelled out as `2nd degree connection`) — so `_PROFILE_DEGREE_LOCATORS` and
`scrapper._degree_from_source` both key off that text, with the class anchors kept only as a legacy
tail. This read is load-bearing twice over: `_profile_is_first_degree` aborts a pointless invite
with it, and `LinkedInProfile.is_1st_connection` (fed by `parse_profile_header`) is what routes a
profile viewer down the comment branch instead of the connection-request branch — a dead badge made
**every** viewer look like a non-connection. A chain that matches no badge at all is cross-checked
against the page's own degree LINE (whole-line, never `\b1st\b`, which would fire on
"1st place, 2026 awards"); re-ground with `scripts/linkedin_live_validation.py --profile-scrape`
against a 2nd/3rd-degree profile and read `degree_anchors` in the report.

**Confirmed live 2026-08-14** (#1031, `--profile-scrape` against a 3rd-degree profile, deployed
build `v0.149.0`, which carries #1025 — it shipped in `v0.134.0`): `state: ok`,
`degree_grounded: true`, and a non-empty `degree_locator_matches` — the union leaf XPath, and only
it; both class anchors matched nothing. **Neither list in that report is a count**: the probe
truncates each locator's `texts` to the first five and caps `degree_anchors` at eight, so the run's
`· 3rd`, `· 3rd`, `· 3rd+`, `· 3rd`, `· 3rd` (matches) and the two `· 2nd` further down `<main>`
(anchors) are floors, not totals — a later run showing a different length is a truncation artefact,
not drift.

Two details the earlier grab did not pin: the badge renders as a **`<p>`** leaf, not a `<span>`, and
every one of its classes is hashed (`d3e5c957 _797b549d …`) — the class is unusable as an anchor.
The tag is not: the leading union's first branch is
`[self::span or self::div or self::li or self::p]`, so it is tag-**tolerant**, not tag-agnostic, and
it reads this page only because `<p>` is in that list. A badge that ever renders in some other tag
falls through to the second branch, which matches the spelled-out `degree connection` text and
nothing else — so widening the tag list is the fix if the bare `· 2nd` shape ever moves again.
`tests/unit/app/test_sdui_zero_walk_tripwires.py` (`TestTheLiveBadgeShapeStaysGrounded`) pins this
exact DOM: all three of its tests fail if `self::p` leaves the chain.

**The FIRST badge is the profile's; every later one names somebody else.** A text anchor is far
broader than the class anchor it replaced, and a profile page is full of other people's badges —
the "People also viewed" rail outside `<main>`, mutual-connection highlights inside it. So both
reads take the first match in DOCUMENT order and nothing else: `_PROFILE_DEGREE_LOCATORS` leads
with a single **union** XPath (a union returns nodes in document order, two locators would not) and
`_profile_is_first_degree` judges `texts[0]`, while `_degree_from_source` is scoped to `<main>` and
returns on the first hit. Reading "any badge on the page" is the #1012 rail hazard in a read
instead of a click: it cancels the invite to a 2nd-degree target because one of their mutuals is a
1st.

## The feed share-box composer is a non-button clickable (#1107)

Live-grounded 2026-08-08 (`/feed/`, user 1): the share-box trigger renders the text
"Start a post" on the page but is no longer a native `<button>` that the old `//button[...]`
XPath can resolve. `visible_button_labels()` therefore omits it even though `page_text_sample()`
plainly contains the label. The working chain now matches `button` OR `*[role='button']` and
checks both the element's normalized text and its `aria-label` (case-folded), because LinkedIn
commonly moves the visible label into a descendant span while the actionable wrapper carries the
role.

The production path (`auto_post_to_group`) cross-checks the page text before treating a missing
share box as "group is unpostable": if `<main>`/`<body>` still contains "Start a post",
"Start a public post", or "Create a post", the control has drifted and the run warns rather
than silently rotating past a postable group.

## The share-box composer opens inside a SHADOW ROOT on the feed (#1621)

Live-grounded 2026-08-17 (`/feed/`, user 1, debug node). The trigger above is fine — it resolves,
the click lands, and the composer opens. It just opens somewhere `driver.find_elements` cannot
look: the modal (`div[role='dialog'].artdeco-modal.share-box-v2__modal.share-box-v2__modal-phoenix-redesign`,
744×592, carrying `Dismiss / Post to Anyone / Text editor for creating content / Add media /
Create an event / Celebrate an occasion / More / Schedule post / Post`) is mounted in the OPEN
shadow root of `div#interop-outlet.theme--light`.

Nothing in the light DOM crosses that boundary — not a CSS lookup through the driver, and **not
any XPath at all**. So the shipped `//div[@role='dialog']` scope answered "no composer" against a
composer that was on screen, and every step under it (occasion affordance, editor, Post) inherited
the miss. The hit test rules the other candidate causes out: `elementFromPoint` at the trigger's
own centre returns its own descendant, so the sticky nav is not stealing this click.

The one lookup that walks shadow roots is `selenium_util.find_deep_elements`;
`share_composer.find_composer_container` is the ONE place the composer container is resolved, and
everything scoped to it matches **CSS + a Python label match** (`find_labelled`) because XPath
cannot address a shadow tree. A non-exact label match is bounded on word boundaries: LinkedIn
renders an option's title and its description in one node ("Project Launch Share a new project
milestone"), so an exact match cannot be required everywhere — and a bare substring would let
"post" match "Repost".

**The same modal is NOT shadow-mounted on a group page.** The 2026-08-17 `--group-composer` run on
group 3063585 found the identical `share-box-v2__modal` with `shadow_path: []` — light DOM — so the
group lane's editor and Post button still resolved with the old chain. Read that as a rollout
difference, not a rule: one lookup that works either way is why both lanes now go through the
container resolver.

## The occasion composer's template chooser is a permanent read-only-guard boundary (#1621, #1713)

Live-grounded 2026-08-24 (`/feed/`, user 1, debug node, both `project_launch` and
`educational_milestone`). Picking an occasion TYPE ("Project Launch", "Educational Milestone") does
not open the text editor directly — it opens a template-picker screen with no `role='textbox'` of
its own: `Dismiss / Template 1 … Template 22 / Back / Next` (dialog text: `"Project launch\nAdd a
photo\nOr select from below\nLaunched a…"`). Clicking a `Template N` card only swaps in a preview
image (`Activate link to view larger image.` / `Remove image` appear) and leaves the same
`Back`/`Next` pair — it does NOT reveal an editor either. #1621 already shipped the fix for this
shape: `publish_occasion_natively` clicks `TEMPLATE_CHOOSER_NEXT_LABELS` ("next") past the chooser
before looking for the editor, on the assumption that a template comes pre-selected and Next just
advances the wizard.

**That assumption is unverifiable by the live-validation probe, forever, on purpose.**
`install_read_only_guard()` refuses ANY click on a control labelled "next" — `_SUBMIT_LABEL_PATTERNS`
files it under "commit a form step" alongside `save`/`done`/`delete`/`remove`, and per the
**linkedin-live-validation** skill there is no override flag. So the probe can confirm the chooser
resolved (matching the exact `TEMPLATE_CHOOSER_NEXT_LABELS` anchor the shipped code clicks) and can
never confirm what is past it. Before #1713, `occasion_composer_state` required `editor_present` +
`post_button_present` unconditionally, so this KNOWN, ALREADY-HANDLED gap graded `drift` and the
weekly cron auto-filed it (issue #1713 itself) — and would keep re-filing the same unchanged finding
every week, since nothing about the guard boundary can ever change from a re-run. The grading now
treats "template chooser resolved with the shipped Next anchor present" as `ok`, same as a directly
resolved editor + Post — `template_chooser_next_present` in the report names which of the two
happened. If this ever needs deeper verification (e.g. confirming the editor really appears after
Next), that has to happen the way #1621's original chain was grounded: interactively, by a human
driving `selenium-lem` or the noVNC debug node — not through this guarded probe.

## The comment composer has no `<form>`

"Submit" means clicking the Comment/Post button next to the composer (`_composer_submitted`).
The comment overflow "…" menu is hover-hidden, not click-revealed.

## The global nav is sticky — never click a composer where the previous action left it

`_focus_composer()` centers the composer first. A top-of-viewport composer has its click stolen
by the nav's `<svg>`: `ElementClickInterceptedException ... at point (x, 9)` (issue #815).

## Every composer lookup is scoped to its OWN post

A document-wide `div[role='textbox']` returns an EARLIER post's still-mounted composer, which is
the real y=9 source AND a comment landing on the wrong post (issue #876).

`_post_composer_for_card()` is the only thing that decides which box a post's comment is typed
into. Order: a rendered box inside the card wins; otherwise `_single_post_scope()` widens to the
widest ancestor that still covers this post ALONE — the card is only the NEAREST ancestor carrying
the comment action, and LinkedIn does not always mount the comment section inside it, which is
why a card-scoped lookup missed on every post of every group run (issue #916). The widening bound
counts per-post MARKERS (`_POST_MARKER_SELECTORS`: the post-text node the feed enumerates cards
from, plus `Hide post by`), never comment ACTIONS — the composer being searched for brings its own
submit button whose text is literally `Comment`, so an action count would see two the moment the
comment section is a sibling and would never widen at all. A box starting
ABOVE the card is rejected outright (the share box, or one left open on a post we already did), and
a box labelled `creating comment` beats an unlabelled one (a reply box under someone's comment is a
`role=textbox` too). **Issue #1777** added one more step when the widened scope STILL holds
nothing: a live grounding run found a HOME-feed card (the #916 widening's own control surface)
whose composer never resolved even after widening, because the composer mounted as a SIBLING of
the marker-bounded boundary, not a descendant of it — a reshare embeds the original post's own
per-post marker, so the boundary that "still covers this post alone" sits one level BELOW where the
comment section actually renders, and no amount of ancestor-climbing reaches a sibling subtree.
The fallback mirrors `_reply_composer_for_comment`'s own answer to the identical sibling-render
problem (#883): every visible `role=textbox` on the PAGE, never above this post (the same
above-filter), nearest to its bottom edge wins. No box of ours (in card, in scope, or on the page,
all within that bound) = skip the post.

A miss is an expected no-op and is logged DEBUG *inside* the resolver, like the reply one — the
per-card `log_warning` it replaced escalated to ERROR and filed a defect for a post we skip by
design. It polls `_COMPOSER_MOUNT_POLLS` times rather than burning
`WAIT_DEFAULT_TIMEOUT x (MAX_WAIT_RETRY + 1)` (~35s) on a card that never opened one.

### The group feed is the surface this was widened FOR, and it is the one nothing had grounded (#928)

The DEBUG downgrade above is right and it is also why the group lane went quiet either way: with the
warning gone, "the widening works" and "the widening still misses every post" print the same
nothing. The three days before #916 examined **2,515** group posts and landed **one** comment.

`scripts/linkedin_live_validation.py --group-feed-composer [<group-id>]` is what answers it. It
walks a group feed with the SHIPPED chain (`_FEED_POST_TEXT_SEL` → `_card_for_textbox` →
`_COMMENT_ACTION_LOCATORS` → `_post_composer_for_card`), clicks each sampled card's own Comment
button so the box mounts, Escapes without typing, and reports per card:

- **per-locator hit counts** for `_COMMENT_ACTION_LOCATORS` (the `live count:` rows beside that
  chain were taken on the HOME feed in #816 and have never been re-taken on a group one),
- `composer_source` — `in_card`, `widened_scope` (the box existed only inside `_single_post_scope`,
  i.e. #916's widening is what found it) or `none`,
- `textboxes_in_card` / `textboxes_in_scope` / `page_textboxes_after_click`.

The **home feed runs as a control in the same session**, because "no composer in the group" is only
a statement about groups next to a feed where the same chain does resolve one. The three zero-shapes
are graded apart, since they need opposite fixes: no Comment action on any card (re-ground the
chain), a composer mounted on the page that no card claimed (re-ground the resolver / widening), and
nothing mounting anywhere (the surface has no inline composer at all, so the lane must stop
generating an `lem-medium` comment per post it can never post — #1084).

**Live counts (2026-08-31, user 1, `--group-feed-composer`, several group ids including 3063585,
3612099, 3746827):** every group id tried rendered **no `<main>` at all** — `page_text` empty,
`js_body_innertext` length 0 (read straight off `document.body`, not Selenium's own visibility
heuristic), zero visible controls, yet ~1.6MB of HTML still loaded and `document.title` stayed
`"LinkedIn"` (not a login page). `--group-composer` and `--group-membership` against the SAME ids
in the SAME session read identically blank, including group 3063585 — the one #928's own
`--group-composer` run resolved a share box on as recently as 2026-08-17. The home feed rendered
normally in every one of these sessions (full `page_text`, cards, controls), so this is not the
account being signed out or the chain having drifted — it is the `/groups/` and `/groups/<id>/`
surfaces specifically answering with something that isn't the app shell. Filed as a separate issue
(the group-feed composer fix above is independent of it and was grounded entirely against the home
feed control) rather than folded into #1777, because no locator change can fix a page that never
mounts a `<main>` to look inside. `_PAGE_SHELL_CROSSCHECK_SEL` (`app/engagement/feed.py`) is the
production-side answer: `auto_comment_in_groups` now checks for `<main>` before ever asking the
feed engine to grade the page, so this state logs as "did not render" and rotates to the next
group, instead of silently reading as an empty feed.

## The groups directory renders offers and memberships with identical hrefs (#1316)

Live-grounded 2026-08-14 (`/groups/`, user 1, `--group-membership`). One page, two lists, and every
anchor in both is `/groups/<numeric-id>/` — so `_enumerate_joined_groups`, which took every anchor as
a join, was storing groups the user had only been OFFERED. Four of them were already sitting
`enabled` in `user_groups`, and the rail rotates: three consecutive runs enumerated a DIFFERENT set
of five recommendations, so every weekly sync added five more.

What the live DOM actually looks like:

| Reading | Live value (2026-08-14) |
|---|---|
| `/groups/<id>` anchors, whole document | 55 → 54 across runs (50 joined + the day's ~5 recommendations) |
| Headings, in document order | `0 notifications total`, **`Groups listing`**, **`Groups you might be interested in`**, `Ads Banner`, `More inboxes` |
| `main a[href*='/groups/']` | 51 — the recommendation rail is OUTSIDE `<main>` |
| `main button[aria-label^='More options for ']` | **50 — exactly one per JOINED row, none on a recommendation card** |
| `main [role='listitem']` / `main h2, main [role='heading']` | 0 / 0 — neither is an anchor here |
| Directory tabs (`Your groups`, `Requested`) | `<button>`s, NOT headings — an ancestor walk attributes nothing |

Two things follow, and both are in `engagement.feed`:

- **The section is the nearest PRECEDING heading in document order**, never an ancestor: the page's
  own section headings are neither h1–h3 nor ancestors of the cards beneath them.
  `_GROUP_DIRECTORY_JS` returns `[id, name, section]` per anchor and `_is_group_recommendation_section`
  drops the offers. A row whose section could not be read is **kept** — an unreadable heading is not
  evidence that a membership is a recommendation, and dropping on absence would empty the sync the
  first time LinkedIn re-words a heading.
- **Zero joined groups is cross-checked before it counts as "this user is in no groups"**
  (`_GROUPS_DIRECTORY_CROSSCHECK_SEL` = `button[aria-label^='More options for ']`, via
  `_report_zero_walk`). It shares nothing with the walk — not the href, not the id shape, not the
  heading attribution — which is what lets it answer for a rotated id shape AND for the new failure
  mode the section filter introduces: a re-worded joined-list heading that matched a recommendation
  marker would drop every row, and this is what says so. The probe grades the shipped selector
  (`directory.crosscheck_count` / `crosscheck_blind`), so a rotated control label surfaces as a
  finding rather than as a tripwire that silently answers zero.

The group PAGE's own header carries no membership control at all: live `header_controls` were
`['Dismiss', 'Public group', 'View information on Active Group badge', 'Open about group', 'Share',
'Manage notifications', 'More options for <group>']`. Membership there reads as **share box present
+ no Join**, never as a Leave button — and `More options for <group>` is why a membership marker must
match verb-first rather than by substring (a group named "Join …" would otherwise read `not_member`).

## A post PERMALINK runs the same engine as the feed — and is not a one-post page

`comment_on_post` is the live comment task behind profile-viewer engagement and the outreach
funnel's COMMENT stage. It used to key on `comments-comment-texteditor` and
`comments-comment-box__submit-button--cr`, both removed by the SDUI rewrite, so the composer lookup
could only time out and the run fell through to a bare `Keys.ENTER` whose own log line read "might
not have worked" — a live comment path failing silently for both callers (issue #966).

It now resolves its card with `_permalink_post_card()` and hands it to the SAME
`react_to_post_inline` / `post_comment_inline` the feed walk uses, so there is no permalink-only
composer lookup that can drift on its own. Three things the permalink surface adds:

- **The page stacks recommendations under the post.** Each "More posts for you" is a full card with
  its own comment action, so the card is chosen by the URN in the permalink. The top card is used
  only when no card claims a URN at all, and a top card that provably belongs to a DIFFERENT post
  returns None — refusing to comment beats commenting on a recommendation.
- **React BEFORE commenting**, for the reason the feed walk does: submitting re-renders the card and
  stales every element resolved from it. The same `INLINE_REACTIONS_ENABLED` tourniquet applies, so
  one env flip stands both comment paths down on the next rotation.
- **A comment that does not land is a FAILURE row and a RELEASED claim.** `post_comment_inline`
  returns True only once the comment is verifiably posted, so a typed-but-unsubmitted comment is
  never recorded as one (only SUCCESS rows count as "we commented here", so the failure row can't
  self-block a retry).
- **A landed comment spends the account envelope** (`record_action(user_id, ACTION_COMMENT)`, #626),
  at the same point the feed walk spends its own. While this path posted nothing the missing call
  cost nothing; a working path the governor can't see would let the feed walk and the roster lane
  spend a full day's envelope on top of it.

`check_commented`'s LinkedIn-side half went the same way: its
`comments-comment-list__container` + `aria-label='• You'` XPath had matched nothing since the
rewrite. It now reads `_comment_items` and matches our own profile slug EXACTLY
(`_href_is_profile`), and it only runs when the caller passes `my_profile` — the slug is what
identifies our comment. It deliberately does not call `_load_comment_thread` (a 1400x3400 resize to
lazy-render a whole thread) for a check the logs ledger already covers — but it DOES poll
(`_COMMENT_THREAD_MOUNT_POLLS`, stopping as soon as the thread stops growing): the comment list
hydrates after `driver.get()` returns, so reading it on the first paint sees zero comments on a post
that plainly has them, and the rebuilt guard would silently never fire either.

Grounding: `scripts/linkedin_live_validation.py --permalink-comment <post-url>` reports card →
Comment action → composer for a real permalink. It opens the composer and Escapes it; nothing is
typed and no comment is left.

## A reply composer is resolved ONE way, for both reply paths

`_reply_composer_for_comment()` is the only thing that decides which `div[role='textbox']` a reply
is typed into — `_reply_to_comment_inline` (a comment on a feed card, #883) and
`_reply_under_comment_inline` (a reply under our own comment on someone else's post, #478/#886)
both call it. Order: a box nested in the comment's own subtree wins; otherwise the visible box
nearest the comment's bottom edge, with a box starting ABOVE the comment rejected **outright** and
a box that resolves to a DIFFERENT comment rejected too. No box of ours = skip.

The above-filter has to be a rejection, not a penalty. #478's original pick only scored an
above-composer `+1e6`, so the post's main "Add a comment" box still won whenever it was the only
visible composer (our reply box never opened, or the thread re-rendered and collapsed it) — and the
reply posted as a standalone top-level comment, the exact failure the function exists to prevent
(#886). The two helpers stay separate only because they OPEN the box differently: the #478 thread
path needs `scrollIntoView` + an `ActionChains` hover to render a hover-hidden Reply button.

A miss is an expected no-op and is logged DEBUG *inside* the resolver — never a `log_warning`,
which would re-escalate as a defect on repeat (see `docs/error-tracking.md`).

## Failure naming

Inline compose failures name the STEP that threw (e.g. `Inline comment post failed at focus
composer`). Step names stay quote- and digit-free so the error-tracking escalation dedup key
(see `docs/error-tracking.md`) keeps distinct steps apart instead of collapsing them into one
fingerprint.
