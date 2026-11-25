# Impact report (#2643)

How one change to LEM performed for the owner's account, 14 days before against 14 days after.

**Private.** The repo is public, so the numbers never go to GitHub. They are read through
`GET /api/admin/impact-report?user_id=&change_at=&days=14[&exclude_days=YYYY-MM-DD,…]` (admin
secret, read-only, hidden from the published schema; a failed read is 503, never an empty report)
and emailed to `IMPACT_REPORT_EMAIL` (falls back to `MARGIN_REPORT_EMAIL`). GitHub gets one line on
the tracker issue, `Impact report ready for <tag>.`, built only by `impact_report.release_note`,
and only for a plain `vX.Y.Z` tag.

## Method

- Days are America/New_York days. The change's own day belongs to neither window; `exclude_days`
  (outages) are dropped from both. Rates are per counted day.
- Posts use the LATEST `post_stats` row per post; organic engagement excludes `own_comments`
  (`post_stats.third_party_engagement_rate`), impression-weighted. An unknown own-comment count is
  "not measured", never zero.
- `inconclusive` whenever a side is unread or n is below the floor (5 posts, 10 events, 2 follower
  snapshots, 5 cost days). Otherwise `up` / `down`, or `flat` inside ±5%.
- Always unmeasured, and named in `not_measured`: author replies (#2621), saves, catch-up sends,
  authenticity score, meetings and won.

## Beat

`impact-report` (`run_scheduler.auto_impact_report`, daily 14:30 UTC), default OFF behind
`impact-report-enabled`. It reads GitHub releases newest first, paging `releases?per_page=100&page=N` (at most 10 pages)
and stopping once a page's oldest release is past the due window, so a busy release week never
pushes a due release off the page. Releases published 15 to 18 days ago (the last window day must
be complete) are grouped by America/New_York publish day: ONE email per day, claimed in Redis first
as `impact_report:sent:release-day:<YYYY-MM-DD>` (a failed send releases it), with `change_at` the
day's last release and the label naming its first..last tag. The tracker note names the day's last
tag only. On the 1st it also emails a trailing-30-vs-prior-30 baseline (no GitHub note). Every
report passes `KNOWN_OUTAGE_DAYS` (in `impact_report_job.py`; initially 2026-11-22 and 2026-11-23,
from #2643) as `exclude_days`, and the email lists them as excluded. An unreadable releases read
logs a warning and sends nothing. No Redis means nothing is sent.
`IMPACT_REPORT_USER_ID` (default 1) picks the user; `IMPACT_REPORT_ISSUE` (default 2643) the note's
issue.

Rollback: remove the `impact-report` beat entry. Nothing is written except the Redis claim.
