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
`impact-report-enabled`. It emails a report for each GitHub release published 15 to 18 days ago (the last window day must be complete)
(Redis claim `impact_report:sent:<tag>` taken first; a failed send releases it) and, on the 1st, a
trailing-30-vs-prior-30 baseline (email only, no GitHub note). No Redis means nothing is sent.
`IMPACT_REPORT_USER_ID` (default 1) picks the user; `IMPACT_REPORT_ISSUE` (default 2643) the note's
issue.

Rollback: remove the `impact-report` beat entry. Nothing is written except the Redis claim.
