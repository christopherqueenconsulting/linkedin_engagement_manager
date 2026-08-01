# Engagement automation internals

Full design detail for the `app/run_automation.py` subsystems that CLAUDE.md's "Engagement
automation" section only names. This doc is the load-bearing detail; CLAUDE.md keeps the
one-line invariant + pointer.

## Golden-hour presence & second wave (`utilities/golden_hour.py`, issue #622)

The ONE place the first-hour amplifier's timing is decided.

- ONE `golden_hour_report` per swept post (comments found, replies sent, minutes since REAL
  publish time from the POST log — not `scheduled_time`, or a late publish reads as a late
  sweep), INFO in-window and WARNING out, shipped via `track_golden_hour_report`.
- Posts older than a day emit nothing.
- `latency_minutes=None` + `within_window=False` when publish time unknown — unmeasured is
  never on-time.
- A sweep that could NOT run (429, session failure) emits its OWN report
  (`status=rate_limited`/`session_failed`) and retries (`sweep_retry_countdown`), bounded twice —
  by attempts AND by the window, so a retry past minute 90 is never scheduled.
- Reports scoped to twice the phase's window: every sweep walks yesterday's posts too, and
  grading revisits would bury the on-time rate.

**Second wave**: ONE self-comment 6–8h after publish (`auto_second_wave_comment`) that must ADD
substance — same #617 contract + similarity gate + slop lint as a feed comment (`_gated_comment`,
shared with `generate_ai_response`); ships NOTHING when no draft passes; specifics from story bank
(#620); posts through socialActions API like the seed (no browser, 429-immune). 6–8h wait in HOPS
(`second_wave_due_minutes` seeded on (user, post); `second_wave_hop_seconds` off
`CELERY_VISIBILITY_TIMEOUT`) — `task_acks_late` would redeliver an 8h countdown. Discretionary →
stands down under `is_automation_paused()`. Seed + second wave can never stack: cap on COUNT of
our own comments on that post URL (`count_user_comments_on_post_url`,
`SELF_COMMENT_MAX_PER_POST=2`), so neither task has to know the other ran.

## DM conversation auto-nurture (`_nurture_after_reply`, `utilities/ai/dm_nurture.py`)

A reply used to END a sequence — now it's classified (interested / objection / not-now /
disinterest / neutral) and becomes an **approval-gated** context-aware next message queued as a
`pending` row in `scheduled_dms` (`source='nurture'`), one open draft per thread, per-day draft
cap, explicit disinterest stops the thread for good.

## Message-thread resolution ladder (`utilities/linkedin/message_thread.py`, issue #731)

The ONE way LEM opens (and reads) a 1:1 thread.

- `open_message_thread` walks SIX routes in order: profile anchor → legacy button → tag-agnostic
  'Message' text node → top-card **More** menu → direct compose URL from the profile URN
  (captured BEFORE any route navigates away) → messaging search.
- A route only counts when the thread is **provably open** (`msg-s-*` events readable or compose
  form present), on either surface (profile may yield bottom-right `msg-overlay-*` chat, not
  `/messaging/`).
- Class names never keyed on — every locator is href / aria-label / TEXT.
- RIGHT person: compose URN comes from the person's own compose anchor or the URN beside their
  `/in/<slug>` in the page model (never "first URN in the document" — that's the viewer's own Me
  menu); a messaging-search row that links to a different slug is rejected outright.
- Names WHOLE-WORD (`name_matches`) — 'Chris' is a substring of 'Christine Baker' and reading her
  reply as ours sends the follow-up anyway.
- Bare compose form never ends the render wait (LinkedIn paints composer before message list);
  zero events = UNKNOWN.
- Verdict is **three-valued**: `ThreadState.REPLIED` / `NOT_REPLIED` / `UNKNOWN` — UNKNOWN makes
  the caller SKIP and leave the row due (a missed follow-up is recoverable, a follow-up to a
  reply is not).
- Self-name is a **required setting**, not a scrape: `users.linkedin_display_name`
  (Settings → Setup & Connection, its own `account-readiness` item) is what `resolve_self_name`
  compares against; scraped `profiles.data.full_name` is the fallback. One field, not
  first/last — message-group label is the full display name as ONE string.
- Winning route logged (`action_type='followup'`); `--dm-thread-url` reports the `reply_state`
  live.

## Owned-asset CTA loop (`resolve_artifact_delivery` in `content_alignment.py` + `_queue_artifact_delivery`, issue #624)

The ONE map from a CTA to its asset, and it names the CHANNEL — **lead magnet** is the
comment-keyword mechanic whose payload is a DM; **newsletter** is a subscribe LINK.

- Newsletter's `newsletter_url` rides in `artifact_cta_line`; #392's `split_link_for_first_comment`
  decides where it lands — OFF-platform newsletter → first comment (link in body costs 19–60%
  reach), linkedin.com newsletter → body (penalty is off-platform only).
- Attribution matches on BOTH halves (`content` OR `first_comment_link`); a first-comment-only
  count reads 0 forever for the mainline LinkedIn newsletter.
- Keyword delivery is **approval-gated**: lands as a `pending` `scheduled_dms` row
  (`source='artifact'`); blocked by an open draft from EITHER mechanic in BOTH directions; capped
  on `max_dms_per_day` at drafting AND re-checked at send. `record_lead_magnet_sent` fires on
  QUEUE.
- Attribution rides on `GET /user/newsletter-subscribers` (`count_artifact_cta_deliveries`):
  subscriber growth reads against the CTAs that actually delivered. `newsletter_links` is None
  (not 0) with no URL.

## Human pacing (`utilities/human_pacing.py`, issue #626)

The ONE place cadence is decided.

- Read-time delay (`pace_read` — length-scaled, floored at `PACING_READ_MIN_SECONDS`, ceilinged
  below `MAX_INLINE_SLEEP_SECONDS`).
- `dispatch_jitter_seconds` countdowns on every beat-dispatched engagement task (own-post replies
  use `PACE_RESPONSIVE`).
- `daily_budget`/`remaining_actions` turn each per-day cap into a stable random draw (weekend
  asymmetry + occasional rest days) under one account-level envelope.
- Seeded on (user, action, date) and persisted in Redis — a retry never re-rolls.
- Fails open — no Redis, or `HUMAN_PACING_ENABLED=false`, restores pre-#626 behaviour.
- Pacing only slows us down; the 429 breaker in `rate_limit.py` is the separate, harder gate.

## Comment outcome tracking (`sweep_comment_outcomes` + `utilities/comment_outcomes.py`, issue #628)

Commenting used to be write-only. Read-only T+24h sweep revisits each un-checked `logs` comment
row, locates it via the #478 thread map, writes ONE `comment_outcomes` row: author replies,
thread replies, likes, whether we replied, `visible_most_relevant`.

- **Three-valued on purpose**: 1 present under 'Most relevant', 0 absent there but present under
  'Most recent' (the May-2026 demotion signal), NULL when sort control couldn't be read. NULL
  rows excluded from the demotion denominator.
- Unfindable comment = SKIPPED.
- Weekly report (`auto_weekly_comment_quality`) ships rates to PostHog + `/user/engagement-analytics`.
- Demotion rate > `COMMENT_DEMOTION_HOLD_RATE` on ≥`COMMENT_QUALITY_MIN_SAMPLE` readable readings
  **holds that user's feed commenting** (`hold_commenting` in `rate_limit.py` — narrower than
  `pause_automation`) and escalates as CRITICAL.
- Live: `scripts/linkedin_live_validation.py --comment-outcome-url`.

## Suppression tripwire (`auto_suppression_tripwire` + `utilities/suppression.py`, issue #629)

2026 LinkedIn penalties are SILENT — a flagged account sees its reach step-collapse
(8,500→340 pattern) and stays collapsed 60–90 days, no notification.

- A daily beat reads each user's own `build_engagement_trend` series and compares **impressions
  per post** (or engagement per post when impressions weren't captured — a single impression-less
  day switches the whole comparison, never mixes scales) against their OWN trailing 14-day median.
- Days with no posts dropped BEFORE measurement — `SUPPRESSION_CONSECUTIVE_DAYS` means consecutive
  **posting** days, a weekend off is never a collapse.
- ≥`SUPPRESSION_DROP_RATIO` drop sustained, or #628's demotion verdict, `pause_automation()`s
  **engagement only** (posting is API-driven and never gated); read-only stat-capture lanes
  exempted via `is_measurement_paused` (freeze them and a recovered account can never be seen to
  recover).
- WHY recorded in Redis (`record_suppression_trip`, no TTL), emails the user, escalates as
  CRITICAL.
- Cold start / thin baseline (<`SUPPRESSION_MIN_BASELINE_POSTS`) / zero baseline = `unknown`,
  never actioned; one bad day = `watch`.
- Pause re-armed daily while the trip stands; only refreshed when the standing pause is the
  tripwire's own.
- Recovery is human: `POST /user/automation-resume` behind `SuppressionBanner.tsx` off
  `GET /user/automation-status` — reports a recovered reading beside the standing trip but leaves
  the decision to the user.

## Company-page invitations (`utilities/linkedin/company_page_inviter.py`, issue #732)

A paced DAILY drip, not the once-a-month blast it used to be.

- Run bounded by the SMALLEST of three ceilings: `max_company_page_invites_per_day` clamped by
  `max_invites_per_day` and run through `human_pacing`; **credit spread**
  `credits_remaining / days_left_in_month` (renews on the 1st, REFUNDED on accept); live credit
  count (hard stop at 0).
- `plan_daily_invites` decides all of that BEFORE a Chrome session opens — most days the
  allowance is zero.
- Idempotency is durable, not Redis: today's spend SUMMED out of `logs` rows
  (`count_company_page_invites_sent_today` — one batched row carries a count).
- Every run emits `company_page_invite_run` — including the ones that send nothing, since a
  series carrying only sends can't tell "paced to zero" from "silently broken".
