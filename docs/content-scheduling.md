# Content plan cadence & posting-days (issue #621, #581)

Full detail for the scheduling rules CLAUDE.md's "Content generation & scheduling" section only
states as an invariant.

## Cadence is not one post a day

The 30-day plan fills the `posts_per_week` slots (2–7, default 3) of a **fixed day-type
calendar** — `POST_DAY_TYPES` in `content_framework.py` (Tue build-receipt / Wed story / Thu
spiky POV at the default). The day type also supplies each post's buyer stage AND narrows its
archetype family — a Tuesday slot doesn't draw from the full archetype menu, it draws from the
build-receipt family.

Posting times are clamped to waking hours, jittered ±15–30 min, and held ≥24h apart.

## `posting_days` is the separate, harder bound

**Which** weekdays are eligible is a distinct preference (issue #581), default Mon–Fri
`[0,1,2,3,4]`, all seven selectable:

- Cadence (`posts_per_week`) says HOW MANY slots to fill.
- `posting_days` says WHICH days may carry them.

Weekends are opt-in — the allow-list is the harder of the two bounds; a `posts_per_week=5` plan
with `posting_days` restricted to weekdays never spills into Saturday/Sunday no matter how the
day-type calendar is shaped.

Best-posting-time logic decides only the HOUR within an eligible day. An empty or invalid day set
is normalized back to Mon–Fri rather than left empty (an empty set would mean zero eligible days,
silently halting the plan).

### More days switched on than the cadence fills (issue #1526)

Because the cadence is the count, switching a day on does NOT add a post — it only makes that day
eligible. Switching on all seven with `posts_per_week=3` still publishes three times a week, which
was reported as a hard limit that could not be raised. Both halves of the plan now say so:

- The settings screen carries finding **C31** (`ui/.../settings/conflicts.ts`) naming the days the
  cadence actually fills, with a one-click fix that raises `posts_per_week` to the number of days
  switched on. It is never a block: `inform` while the day set is the untouched Mon–Fri default, and
  `warn` once the user has deliberately switched a day on that never carries a post.
- `_cadence_slots` logs the same fact at DEBUG. It is the ordinary case at the shipped default, so
  it is never a warning.

## Source rotation (issue #1526)

The archetype a text post is written FROM — `thought_leadership`, `blog_summary`,
`website_content`, `industry_news`, `personal_story`, `engagement_prompt` — used to be an
unweighted random draw per post. At 3/week that is ~13 draws a month over six sources, so a source
could go a whole month without coming up (reported as "no new story or blog-aligned posts").

`_post_source_for_slot` rotates on the planned row's id instead, so consecutive rows walk the
source list; `_next_source_in_rotation` keeps rotating when a source is missing (no blog, no
sitemap) rather than re-drawing into the same starvation. A draft with no planned row — a preview,
a one-off regeneration — has no slot to rotate on and keeps the random draw.

Two things the rotation deliberately is NOT:

- **It is not a per-user coverage guarantee.** Only the text and video rows of a plan reach
  `create_text_post`; a carousel or document spends an id on its own generator. A user's text
  drafts therefore land on a subsequence of the rotation — evenly distributed and stable per slot,
  not one-of-each-per-month.
- **The fallback is not "always the next entry".** A user with neither a blog nor a sitemap misses
  its source on both `blog_summary` and `website_content` slots, and stepping to the next entry
  every time would land both of them on `industry_news` — half that user's posts from one source.
  The replacement rotates on the slot's id too, so the misses spread across the rest of the menu.
