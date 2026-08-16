# Profile freshness — on-demand LinkedIn profile re-scrape

Full posture for the on-demand refresh CLAUDE.md's Engagement configuration section only names
(issue #1076). Module: `utilities/profile_refresh.py`.

## Why it exists

A user who reorders their skills or rewrites their headline wants LEM writing from the NEW profile
today, not whenever the weekly staleness beat (`run_scheduler.auto_refresh_profile_syntheses`)
catches up. `POST /user/linkedin-profile/refresh` is the ONE on-demand path.

## The claim, taken BEFORE dispatch

The refresh spends a Chrome session slot out of the fixed pool every Selenium lane shares, so it is
bounded at claim time rather than at the endpoint: `claim_profile_refresh` allows one claim per user
per fixed 24h window, counted in Redis (`PROFILE_REFRESH_MAX_PER_DAY`).

Two properties are load-bearing:

- **It FAILS OPEN.** Redis is the broker; when it is unavailable the API must not stop answering a
  button press. An unclaimed refresh costs one browser session, a refusal costs the feature — same
  posture as `auth_rate_limit` and `human_pacing`.
- **A spent window is an EXPECTED no-op, not a failure.** The second press of the day is a person
  pressing a button twice, so it logs DEBUG and the endpoint still answers **202**, never 429 — it
  just did not queue a new scrape. Warning here would file a defect for working behaviour
  (`utilities/CLAUDE.md`).

The window is FIXED, not sliding: the TTL is set only on the first increment, so a burst of presses
cannot push the reset further out than 24h from the first one.

## What the refresh actually does

`update_stale_profile(force_refresh=True)` bypasses **both** profile caches (by-user AND by-URL) and
re-distils the voice brief from the freshly scraped profile. Without the on-demand path a profile
edit waits for the ≤7-day `auto_refresh_profile_syntheses` beat.

## Session-surface exclusion

The refresh endpoint is absent from `_AGENT_SESSION_SURFACE` (`docs/identity-and-sessions.md`), so a
headless `agent`-scoped token can never trigger it and spend a Chrome slot.
