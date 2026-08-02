# LinkedIn SDUI Selenium gotchas

Full detail for the SDUI DOM/composer invariant CLAUDE.md's "Known Gotchas" only states in one
line.

## Anchors are gone

The old `urn:`, `feed-shared-*`, and `comments-comment-*` DOM anchors no longer exist. Prefer
`data-testid` / `aria-label` selectors via `find_first`/`click_first`.

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
