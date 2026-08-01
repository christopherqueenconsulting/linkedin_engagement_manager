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
