# LinkedIn SDUI Selenium gotchas

Full detail for the SDUI DOM/composer invariant CLAUDE.md's "Known Gotchas" only states in one
line.

## Anchors are gone

The old `urn:`, `feed-shared-*`, and `comments-comment-*` DOM anchors no longer exist. Prefer
`data-testid` / `aria-label` selectors via `find_first`/`click_first`.

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
