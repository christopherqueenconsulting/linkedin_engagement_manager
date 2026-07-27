-- Which weekdays the content plan may publish on (issue #581). #621 made the CADENCE configurable
-- (`posts_per_week`), but the DAYS were still derived from the day-type calendar's fixed priority
-- order, so a user could pick how many posts a week and never which days — and at 6-7/week the
-- planner put slots on Sunday and Saturday with no way to opt out.
--
-- `posting_days` is the allow-list: a JSON array of weekday ints (0=Mon … 6=Sun). `posts_per_week`
-- still decides HOW MANY of them are filled; the day-type calendar picks that many days from
-- WITHIN this set, in its usual priority order. Default [0,1,2,3,4] (Mon-Fri), which leaves the
-- shipped 3/week default (Tue/Wed/Thu) exactly where it was while making weekends opt-in.
-- NULL means "never chosen" and reads as the Mon-Fri default rather than an empty set that would
-- schedule nothing. Lives on the single per-user prefs row, so it is written by the same one-row
-- upsert — the value is normalised in update_engagement_preferences (the V52 lesson: a bad value
-- must never roll back every other setting).
ALTER TABLE engagement_preferences
    ADD COLUMN posting_days JSON NULL AFTER posts_per_week;
