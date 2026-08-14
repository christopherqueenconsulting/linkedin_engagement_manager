# Content-quality telemetry (issue #630)

Full design detail for `utilities/content_quality.py` and the two beats that drive it. CLAUDE.md
keeps the one-line invariant + this pointer.

Every other quality control in LEM is a **one-time verdict**: #625's slop lint blocks a bad draft,
#617's contract throws away a bad comment, #382 scores authenticity once at generation. None of
them can answer *"is the writing getting worse?"* — and it silently can, because the weekly
model-retirement cron swaps the model underneath unchanged prompts. This subsystem is the **trend
line**.

## The two beats

| Beat | Task | Cadence (UTC) | Does |
|---|---|---|---|
| `nightly-content-quality` | `auto_nightly_content_quality` | 02:40 daily | Scores everything SHIPPED in the window into `content_quality_scores` |
| `weekly-content-quality` | `auto_weekly_content_quality` | Mon 09:45 | Rolls the readings into a period comparison + alerts |

Nightly runs at 02:40 because it needs the 23:00 post-stats scrape and 23:40 follower capture to
have landed — yesterday's posts only have impressions (and therefore an engagement rate) after
those. Weekly runs last of the Monday reports so it reads a fully scored week.

Split exactly like `comment_outcomes` (#628) and `suppression` (#629): `content_quality.py` is the
pure arithmetic half, the beat tasks own the DB writes and the PostHog emission.

## What one reading contains (`score_item`)

Per shipped piece — surface is `post` / `comment` / `newsletter`:

- **Weighted slop score** — `slop_severity_score`, `SLOP_HARD_WEIGHT = 3.0` vs
  `SLOP_WARN_WEIGHT = 1.0`. A HARD violation is what actually blocks a post or drops a comment, so
  it carries most of the weight; WARN checks only move the score enough to show drift.
- **Self-similarity** — against that surface's OWN recent history (`similarity_reports`), with the
  measure recorded alongside (`embedding` / `lexical` / `none`). Those three names are
  `content_framework.SIMILARITY_MEASURE_*`, which `MEASURE_*` here aliases: the generation-time gates
  (#617 comments, #1265 posts) grade the same content on the same two measures, so the trend line and
  a gate hold on one post can never name the measure differently.
- **Authenticity** — #382's **stored** `posts.authenticity_score`, never a fresh judge call: the
  gate already paid for it at generation and the number cannot have changed. Surfaces with no
  stored score report `None`.
- **Hook length** against `MOBILE_HOOK_MAX_CHARS` (140).
- **Engagement rate + impressions**, once the stats scrape captured them.
- **Optional external AI-detector score** (see below).

Read-only over content that already shipped: it never edits, holds, or re-generates anything.

## The two rules that run through all of it

1. **Unscored is never zero.** A post with no impressions yet has no engagement rate; a draft the
   lint was disabled for has no slop score; an account with no history has no self-similarity. Each
   is `None` and is excluded from *its own* denominator — `summarize_scores` carries a separate
   sample size per dimension (`slop_sample`, `similarity_sample`, `authenticity_sample`,
   `hook_sample`, `engagement_rate_sample`). One shared denominator would let a dimension nobody
   could measure drag the others down; charting `None` as 0 would invent a collapse (or, for slop,
   invent a clean week).
2. **A regression is measured against the account's OWN prior period**, never an absolute target.
   The engagement floor is the single exception, and it is the only one with a benchmark behind it.

`engagement_rate` is impression-**WEIGHTED** (total engagement / total impressions), not a mean of
per-post rates: a post seen by 50 people must not count the same as one seen by 5,000.

## Similarity: dominant measure only

Cosine and token-overlap scores are **not the same scale** (ceilings ~0.82 vs ~0.55). Each surface
embeds in its own batch, so one failed `lem-embedding` call drops that surface to lexical while the
rest of the period stays cosine. `summarize_scores` therefore averages over the **dominant measure
only** and records the full `similarity_measures` breakdown; `evaluate_alerts` additionally
requires both periods to share the same dominant measure before `similarity_creep` can fire.

Similarity is graded **WITHIN** a surface — a post compared against the user's comments would score
as unique no matter how templated it is. Newsletter editions got their body-history reader
(`get_recent_newsletter_bodies`) in #1284, so all three surfaces are measured; an account with no
history on a surface still reports unmeasured rather than against the wrong scale.

### The pooled mean moves with the surface MIX (#1433)

The surfaces sit at **different baselines by design**: LEM's newsletter editions measure 0.68–0.83
cosine against each other (a newsletter has ONE subject — that is what a subscriber signed up for),
a post is gated at `POST_EMBEDDING_SIMILARITY_MAX` 0.78, a comment lower again. So `similarity_avg`,
which pools all three, moves when the week's **composition** changes even if no surface moved at
all — a week that published two editions clears the 0.05 creep delta on the mix alone.

`summarize_scores` therefore also reports `similarity_by_surface` (`{surface: {sample, avg}}`, same
dominant-measure rule), and `evaluate_alerts` grades **`mix_adjusted_similarity_delta`**: each
surface's own week-over-week move, weighted by THIS period's per-surface samples, over the surfaces
measured on both sides. The pooled number is unchanged for the dashboard and rides along on the
alert as `pooled_delta`; the rollup carries both as `deltas.similarity_avg` and
`deltas.similarity_avg_mix_adjusted`. Two periods with **no surface in common** produce `None` — no
verdict — and the pooled delta is never consulted as a second chance.

The threshold itself is unchanged and stays surface-agnostic: `CONTENT_QUALITY_SIMILARITY_DELTA`
thresholds a *move*, which is comparable across surfaces in a way a *level* is not. **No surface has
an absolute self-similarity ceiling here**, and #1433 decided the newsletter surface does not get
one either — a threshold that fires on normal editorial repetition is worse than none, and the
corpus that could calibrate one (20+ editions across 2+ accounts) does not exist. The sampler that
would produce it is `scripts/sample_newsletter_similarity.py`, which prints `NOT ENOUGH` below those
floors; the full decision is `docs/content-quality-audits/newsletter.md` §8.

## LLM spend

`similarity_reports` is the ONLY spend in the nightly pass: **one `lem-embedding` call per surface**
per user. The task loops over users rather than taking a `user_id` kwarg, so the embedding runs
inside an explicit `llm_attribution(user_id=..., feature=FEATURE_CONTENT)` scope — without it every
embedding would bill the `system` sentinel instead of the account whose content it scored.

`CONTENT_QUALITY_MAX_ITEMS` (60) caps items per user per run. A silent truncation would read as
"a quiet week" in the rollup, so exceeding it logs a WARNING naming what was dropped.

## The three alerts (`evaluate_alerts`)

In the order they cost the user money. Each needs a real sample on **both** sides
(`CONTENT_QUALITY_MIN_SAMPLE`, default 5) — a week with two posts in it has no trend, and a false
alert trains the owner to ignore the next one.

| Alert | Fires when | Threshold env |
|---|---|---|
| `slop_regression` | mean weighted slop rose week-over-week | `CONTENT_QUALITY_SLOP_REGRESSION_DELTA` (1.0 — one extra HARD violation every three pieces) |
| `engagement_floor` | engagement per impression below the floor | `CONTENT_QUALITY_ENGAGEMENT_FLOOR` (0.02) |
| `similarity_creep` | self-similarity rose **per surface** (mix-adjusted, #1433), same measure both periods | `CONTENT_QUALITY_SIMILARITY_DELTA` (0.05) |

`engagement_floor` is the exception twice over: it needs **no prior period** (an account below the
floor is below it regardless of last week) and it counts against its own
`min_engagement_sample()` — the general minimum counts scored *pieces*, and only posts carry
impressions, so a piece-count threshold would gate a post-only dimension on comment volume it can
never reach.

Alerts ride the EXISTING pipeline — `log_error` forwards to PostHog at the default
`POSTHOG_LOG_LEVEL`, so a regression becomes a grouped `$exception` issue without a second alerting
path (`track_content_quality_rollup` carries the numbers).

**This subsystem never pauses anything.** Drift here means *go look at the prompts*; automatic
safety action is #629's suppression tripwire.

## `quality_rollup` is the ONE shape

`quality_rollup(rows, days)` returns `{days, current, prior, deltas, alerts, config}` and is shared
by the weekly PostHog event, the analytics endpoint, and the dashboard panel — so the number the
user reads and the number that raised the alert can never diverge. `split_periods` drops rows
outside both windows (or with an unreadable `shipped_on`) rather than folding them into the nearest
period, so a stale row can never move a verdict. `_delta` returns `None` when either side was never
measured: "we have no baseline" must not read as "no change".

## The external AI-detector hook

OFF by default and a **regression signal ONLY**, per the #416 humanization policy: nothing here is
an evasion target, and no score ever rewrites, holds, or steers text.

- `detector_enabled()` is `AI_DETECTOR_ENABLED` **plus a non-empty `AI_DETECTOR_API_KEY`** — a
  missing key is a silent no-op, never a warning loop. `AI_DETECTOR_URL` is required too, but it is
  checked at call time (no URL → `None`). Also `AI_DETECTOR_PROVIDER`, `AI_DETECTOR_TIMEOUT` (10s).
- Sampled at `AI_DETECTOR_SAMPLE_RATE` (0.1) via `stable_fraction` — a **stable** hash draw, so a
  retried nightly run picks the SAME items and never re-bills for a second reading of the same
  piece.
- `AI_DETECTOR_DAILY_MAX` (5) budget, spent on the CALL whether or not it answered.
- Any failure — no key, no endpoint, timeout, unparseable body, missing `requests` — returns `None`
  quietly. An optional signal must never break the nightly job.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `CONTENT_QUALITY_TELEMETRY_ENABLED` | `true` | Master switch for both beats |
| `CONTENT_QUALITY_WINDOW_DAYS` | 2 | How far back the nightly pass scores |
| `CONTENT_QUALITY_ROLLUP_DAYS` | 7 | Period length for the weekly comparison |
| `CONTENT_QUALITY_MAX_ITEMS` | 60 | Per-user cap per nightly run |
| `CONTENT_QUALITY_MIN_SAMPLE` | 5 | Minimum sample on both sides of an alert |
| `CONTENT_QUALITY_SLOP_REGRESSION_DELTA` | 1.0 | Slop rise that reads as regression |
| `CONTENT_QUALITY_ENGAGEMENT_FLOOR` | 0.02 | Absolute ER floor |
| `CONTENT_QUALITY_SIMILARITY_DELTA` | 0.05 | Similarity rise that reads as creep |
