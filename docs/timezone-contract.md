# Timezone Contract

One rule, four layers. Every scheduled instant in LEM — posts, newsletter editions, scheduled DMs —
obeys it, so the time a user sees is the time the automation actually fires.

> **UTC is the only storage and transport zone. The user's IANA timezone is the only display and
> input zone. Conversion happens exactly twice: at the SPA boundary.**

Why it matters: the post scheduler *and* the pre-post engagement window key off the same
`scheduled_time` (feed commenting runs at `scheduled_time − 15 min`, profile-viewer DMs at
`− 10 min`). A display/storage disagreement of one UTC offset doesn't just mislabel a row — it fires
the whole golden-hour sequence hours away from the peak it was aimed at.

## 1. Storage — naive UTC

| Table | Column |
|---|---|
| `posts` | `scheduled_time` |
| `newsletter_editions` | `scheduled_for` |
| `scheduled_dms` | `scheduled_time` |

These are MySQL `DATETIME`s holding **UTC with no offset**. Two things keep that true:

- `get_db_connection()` pins the session with `time_zone='+00:00'`, so `NOW()`, `UTC_TIMESTAMP()`
  and `CURDATE()` are all UTC — server-side comparisons need no conversion.
- Every write goes through **`db.to_naive_utc()`**. Aware datetimes are converted to UTC and
  stripped; naive ones are assumed to already be UTC.

`to_naive_utc` is not cosmetic. `mysql-connector` serializes a `datetime` from its wall-clock fields
and **silently drops `tzinfo`** — hand it an aware `14:00-04:00` and it stores `14:00`, four hours
early, with no error. Never pass a caller-supplied datetime straight into a scheduling column.

## 2. Scheduling — compute local, store UTC

`get_post_time()` / `get_best_posting_time()` return the user's **local** wall clock (the 2026 peak
model, or the user's own learned best hour). The caller converts to UTC before storing —
see `run_content_plan.plan_content_for_user`.

`recommend_post_times()` takes a `tz=` argument and buckets each post's stored UTC time into that
zone before ranking. Omitting it yields UTC hours, which callers that feed the result back into the
scheduler must not do: `get_post_time` treats the returned hour as local and converts it to UTC, so
a UTC-bucketed hour would be shifted by the user's offset **twice**.

Newsletter slots use `newsletter.next_publish_datetime(..., tz, ...)`, which localizes the
publish-day/hour in the user's zone (DST-safe via `pytz.localize`) and returns naive UTC.

## 3. Transport — explicit `Z`

Every datetime leaving the API is serialized with `api.main._utc_iso()`, which emits a trailing
`Z`. Without the offset a browser parses the string as *local* time, and every rendered value is
wrong by the viewer's offset.

Inbound, request models declare `datetime` fields; the SPA always sends an explicit-UTC ISO string,
and `to_naive_utc` normalizes whatever arrives.

## 4. Display & input — the user's timezone, never the browser's

The user's zone comes from `GET /user/timezone` (Account → Login Location), read in the SPA via the
`useUserTimezone()` hook. It is **not** `Intl.DateTimeFormat().resolvedOptions().timeZone` — that is
only the last-resort fallback while the query is in flight. A user whose Login Location differs from
their device (travel, VPN, or deliberately targeting an audience in another zone) must still see and
set times in their configured zone.

`src/utils/datetime.ts` owns all three conversions:

| Helper | Direction |
|---|---|
| `formatInTimezone(iso, tz)` | UTC ISO → display string in `tz` |
| `toZonedInputValue(iso, tz)` | UTC ISO → `<input type="datetime-local">` value in `tz` |
| `zonedInputToUtcIso(value, tz)` | `datetime-local` value read as `tz` → UTC ISO with `Z` |

A `datetime-local` input carries **no timezone**. Its value must be the same wall clock
`formatInTimezone` renders beside it, or the editor and its own label disagree about one post. On
the way back out, `new Date(value).toISOString()` is always wrong — it interprets the value in the
browser's zone. Use `zonedInputToUtcIso`, which measures the zone's real offset at that instant
(DST-correct) via `Intl.DateTimeFormat.formatToParts`.

## 5. Execution — same instant

`auto_check_scheduled_posts`, `auto_check_scheduled_dms` and the newsletter publisher read the naive
UTC column, attach `timezone.utc`, and pass the aware datetime as the Celery `eta`. Pre-post tasks
are offset from that same instant, so the whole sequence stays anchored to the stored time.

## Adding a new scheduled thing

1. Store naive UTC; route the write through `to_naive_utc()`.
2. Serialize it out through `_utc_iso()`.
3. Render with `formatInTimezone()`; edit with `toZonedInputValue()` / `zonedInputToUtcIso()`.
4. Compute any "good time to do this" in the user's zone, then convert once for storage.

Regression coverage: `tests/unit/utilities/test_timezone_contract.py` round-trips a wall clock the
user picks → UTC storage → display → executed instant across several offsets and both DST
transitions, asserting zero drift.
