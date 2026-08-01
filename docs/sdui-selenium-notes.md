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

## Every composer lookup is scoped to its OWN card

`parent_element=card` — a document-wide `div[role='textbox']` returns an EARLIER post's
still-mounted composer, which is the real y=9 source AND a comment landing on the wrong post.
There is no fallback: no composer on this card = skip it (issue #876).

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
