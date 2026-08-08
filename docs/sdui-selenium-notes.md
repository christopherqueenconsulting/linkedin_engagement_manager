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
re-exported from `run_automation` under the `_`-prefixed names its call sites already used),
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
  elements inside `main`, in document order, each with tag / role / `aria-label` / `data-testid` /
  text / `href` / `aria-haspopup`. `visible_controls` alone could not re-ground this drift — it
  reports `<button>` labels, and the finding *was* that there are no buttons there. It is emitted
  before `visible_controls` because the issue body truncates the evidence blob at 6000 chars.
- **A missing control is only a WARNING once the page proves it rendered posts.** The production
  lookup passes `warn_on_miss=False` and hands the miss to `report_zero_walk` against
  `button[aria-label^='Hide post by']` — a per-post anchor the sort chain does not use. A dead
  session, a login wall and a rotated anchor all return `None` from `find_first`; only the last is
  a defect worth paging on. The returned state is `FEED_SORT_MISSING` in all three cases, so #817's
  "an unsorted scan is never read as recency-sorted" is unchanged.

**Still ungrounded:** the real live DOM of whatever now renders the sort. The widened chain is
written from what the evidence *rules out*, not from a positive sighting, so the next `--feed-sort`
probe run is what closes this section — either `feed_sort` grades `ok`, or `sort_candidates` finally
shows the element and the chain gets a precise route.

## The Catch-up feed is full SDUI — no `data-view-name`, no `<li>` cards

Live-grounded 2026-08-03 (`/mynetwork/catch-up/all/`, user 1): each card is a
`div[role='listitem']` (componentkey UUID) inside the LazyColumn
(`div[data-testid='lazy-column'][role='list']`) under
`div[data-sdui-screen='com.linkedin.sdui.flagshipnav.mynetwork.CatchUpAll']`. The page renders
**zero** `data-view-name` attributes and no `<li>` around cards, so the original
`_CATCHUP_CARD_LOCATORS` chain (data-view-name first, `main li` fallbacks) matched nothing on a
feed visibly showing ten moments — the scan reported `no_moments` daily while working "correctly".
Grounded chain now leads with `div[data-sdui-screen*='CatchUp'] div[role='listitem']`. Ads and
prompts also render as listitems; the profile-link + classifier funnel filters them. The on-card
"Say congrats" suggestion chips carry no stable anchors either — when the harvest misses, drafting
falls back to `_CATCHUP_DEFAULT_CONGRATS`, which is working as designed.

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
once above them. `_company_from_ancestors` reads it from the container's leading lines, and a
leading run that already contains a date range stops the walk — that run belongs to the previous
role, not to a company header. A header without a total-duration line (a bare "Experience" heading)
is never a company.

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
`role=textbox` too). No box of ours = skip the post; there is still no page-wide fallback.

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

**Live counts: not yet taken.** The run is `risk:live-linkedin` and belongs to the owner (see the
**linkedin-live-validation** skill); its per-locator numbers land here, replacing this line.

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
