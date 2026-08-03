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

## Weekly group post — draft, preview, publish (issue #932)

A group post used to be written and published inside ONE Selenium run, so the only thing the user
ever saw about it was the per-group *Post* toggle: never the text, never a chance to revise it. It
is now two beats with a review window between them.

- **`auto_draft_group_post`** (beat `group-post-drafts`, Sundays 15:00) is the ONE place a group
  post's text is written. It opens NO browser — voice comes from the cached profile
  (`load_profile_for_user`) — so a draft costs one LLM call and no Chrome session slot. Which group
  it writes for is still the least-recently-TRIED post-enabled one (`get_next_group_for_post`, #769
  / #858), so the weekly slot keeps rotating.
- **`auto_post_to_group`** (beat `group-posts`, Tuesdays 15:00) publishes that draft and generates
  nothing. **A run with no READY draft publishes NOTHING** — falling back to a fresh generation
  would ship exactly the un-previewed post the draft replaced.
- **Silence ships it.** The resting state is `ready`, not a pending approval: the default cadence is
  unchanged, and a user who never opens the SPA still gets their weekly group post. The preview is
  there for the user who *does* want to look — they can rewrite the text or skip the week outright
  (`GroupsCard`, `GET`/`PUT /user/group-post-draft`, both scoped to the caller's OWN open draft; the
  request never names a draft id).
- **One open draft per user.** The draft beat skips a user who still has one, because that draft may
  already carry their edits — carrying it forward beats replacing it with a generation they never
  asked for. A publish run that never reached LinkedIn (dead session) leaves the draft open, so the
  text the user already approved is what ships next week.
- **The draft dies with its group's turn.** It was written FOR that group, so an unpostable group
  (#858) marks it `failed` as well as stamping `last_post_run_at`, and a group whose *Post* switch
  was turned off between the two beats marks it `skipped` rather than publishing into a group the
  user opted out of. Either way the next draft is written fresh for whichever group is next.
- **Only a user may cancel a post they approved.** "No group takes posts" is what cancels a draft, so
  `get_post_enabled_group_ids` answers `None` — never `[]` — when the read FAILS: a DB hiccup that
  said "empty" would silently skip every user's reviewed draft for the week, edits and all. Unknown
  leaves the draft open and it ships at the next slot.
- **An unsaved rewrite is unsaved changes.** The editor registers with the settings page's save
  registry (`useRegisterSaveSection('group-post', …)`), so *Save All* writes it and the
  unsaved-changes guard fires on leaving. Without that the page would report a clean save having
  written only the toggles, and the text the user thought they'd replaced is what would publish.

## Roster targets LEM can't comment on + opt-in auto-follow (issue #962)

The roster opens each target's `/recent-activity/all/` page directly, so no follow is needed to SEE
their posts. Authors who restrict commenting to connections or followers render **no comment
affordance at all**, and `comment_on_roster_posts` skips them fail-closed. Before #962 that skip was
invisible: the user could never learn that following or connecting would unlock the account.

- **The signature is "posts, but nothing to comment with."** A visit that finds post text but where
  EVERY card resolves to `_card_for_textbox → None` records one blocked visit
  (`record_target_comment_blocked`). A page with no posts, or only short/reshare text nodes, is a
  plain skip and records NOTHING — "they haven't posted" says nothing about whether they accept
  comments, and badging those people would be a lie about their account.
- **The per-visit skip is DEBUG.** An author restricting comments is working behaviour on their side
  and repeats every rotation; warning on it would escalate and file a defect for a post nobody was
  ever allowed to comment on. Only the streak CROSSING `ENGAGEMENT_TARGET_BLOCKED_BADGE_STREAK`
  logs INFO, and exactly once (`streak == threshold`, not `>=`).
- **A landed comment clears the streak in the same statement.** `record_target_engagement` sets
  `comment_blocked_streak = 0` — the streak means "we could not comment here" and a comment IS the
  proof that we could. One statement, so the two can never disagree.
- **The badge names the fix, and does not overpromise.** Following unlocks a followers-only
  commenter; a connections-only one stays blocked, so the copy names both moves.
- **A truncated walk never badges anyone.** If the run's `deadline_ts` cuts the card walk short,
  "nothing offered a comment affordance" describes how far we got, not the author — that visit is
  dropped rather than recorded.
- **The run checks itself before it badges anybody** (`_record_blocked_visits`). Blocked visits are
  buffered and written after the walk, because `_card_for_textbox` drifting against LinkedIn's SDUI
  looks EXACTLY like a roster of restricted authors — and the badge would then tell the user
  something false about other people's accounts. Every visited target reading blocked, on a roster of
  3+, is treated as selector drift: `log_warning` and nothing persisted. Two out of two is an
  ordinary small roster and is recorded normally.
- **Auto-follow is OFF by default** (`roster_auto_follow`) and piggybacks on the roster pass only —
  the activity page is already open, so there is no dedicated follow session and no extra
  navigation. It runs AFTER the comment walk for that target: a follow click re-renders the top card
  and would stale the very cards the walk reads.
- **The follow lane draws its OWN paced budget** (`ACTION_FOLLOW`, cap `max_follows_per_day`,
  default 3). It is deliberately NOT in `ENVELOPE_ACTIONS`: `account_envelope` sums every envelope
  lane's budget, so joining it would ENLARGE a day's outbound allowance by the follow cap. The
  caller still passes `caps`, so the lane is **bounded by** the envelope without adding to it.
  Every hard gate applies too (`_follow_hold_reason`: `is_automation_paused` — which the #629
  suppression trip rides — and the 429 breaker), re-read per follow because a breaker can trip
  mid-run.
- **The control must NAME the page owner.** LinkedIn renders "Follow" inside feed cards and
  recommendation modules; clicking one of those follows the wrong account, which no retry undoes.
  The live probe for PR #963 proved neither anchor-walking nor geometry scopes this safely: the
  target's own `/in/<slug>` anchor also renders inside OTHER people's cards (an unbounded walk
  resolved "Follow Greg Hart" on Andrew Ng's page), and the first card's header Follow sits
  geometrically ABOVE the top-card Follow. What is stable is the display name: the page `<title>`
  ("Activity | \<Name\> | LinkedIn", read by `_activity_page_owner_name`, roster `name` as
  fallback) and every follow control's aria-label ("Follow \<Name\>") are written from the same
  string, so `_resolve_follow_control` accepts only owner-named labels (never a class name, never a
  bare nameless "Follow") — Route A prefers the control nearest the target's own exact-`/in/<slug>`
  anchor, Route B takes any owner-named control on the page. No owner name, or no owner-named
  control, returns `unknown` and clicks **nothing**.
- **`following` is written only after the control confirms it.** LinkedIn REPLACES the top card
  rather than relabelling the button, so the check POLLS (`_await_follow_flip`) instead of re-reading
  once — losing that render race would cost the target a failed attempt it never earned. A flip that
  still never confirms counts as a failed attempt, because `following` is TERMINAL and recording it
  on a click that did not register is the one failure that never self-corrects. Two failures →
  `follow_failed`. A card that ALREADY says "Following" is recorded without a click and without
  spending budget: the zero-cost catch-up that stops the lane redoing this work every run.
- **Terminal means no more CLICKS, not no more reading.** A `follow_failed` target is re-read on
  later visits by `reconcile_roster_follow_state` — read-only, no click, no budget — and an
  affirmative "Following" clears the failure. An unverified flip is recorded as a failure precisely
  because it may have landed, so the next visit has to be allowed to notice that it did.
- **The daily budget is spent on the CLICK, not the outcome.** `record_action(ACTION_FOLLOW)` fires
  the moment the click is dispatched: LinkedIn saw the action whether or not we could read the
  result, and a lane whose verification broke must not be free to click every target on the roster.
  The remaining budget is re-read before every follow rather than decremented from a per-run local,
  so two overlapping runs for one user share one allowance instead of each spending the whole of it.
- **`FollowStatus` (db.py) is the ONE vocabulary** — the MySQL ENUM, the DOM reading the resolver
  returns, and every write site. A `StrEnum`, so a raw column value compares equal to a member with
  no conversion at any boundary, and a typo is an import error rather than a MySQL error at 3am.
- **Live grounding before the clicker:**
  `python -m scripts.linkedin_live_validation --roster-follow <profile-url>` opens the activity page
  and reports which state resolves plus every visible control label. Strictly read-only — nothing is
  clicked, nobody is followed. `unknown` on an account you know you don't follow means the top-card
  control rotated and the lane has gone quiet, not that the roster is fully followed.
- **Observability:** `roster_comment_blocked` / `roster_followed` ride the feed funnel and the
  `feed_scan` event. A rising blocked count with a flat followed count is a roster the user has to
  fix by connecting, not a broken selector.

## Appreciation-DM sources: recommendations + collaborations (issue #968)

`automate_appreciation_dms_for_user` advertises three triggers; two of them were permanent
empty-dict stubs, so only new connections ever produced a DM and the
`recommendation_received` / `collaboration` templates were dead code.

- **Two sources, both STANDING lists.** `get_recent_recommendations` reads the user's own
  profile → `/details/recommendations/` (Received tab); `get_recent_collaborators` reads the
  mentions notification feed (`/notifications/?filter=mentions`) — the nearest thing LinkedIn
  exposes to a collaboration event, i.e. somebody put this user's name in their own post or
  comment. Neither is an event queue: a recommendation never leaves the profile and a mention sits
  in the feed for weeks. Everything below follows from that.
- **Undated is SKIPPED, never thanked.** A card with no readable date could be from 2018, so
  `_parse_recommendation_date` / `_parse_relative_age_days` return `None` and the card is dropped.
  Only what falls inside `APPRECIATION_LOOKBACK_DAYS` (default 30) is a moment worth reacting to.
  Cards that render but NONE of which date is the one thing that warns: that is format drift, and
  it reads in production as "no recent recommendations" forever — a silently dead trigger.
- **`appreciation_touches` is the claim, and it is what makes this safe.** The beat re-queues
  itself every ~60s inside its window, so a standing list without a durable claim is a DM a minute.
  One row per (user, person, event_type); the unique key is the guarantee. `_dispatch_appreciation_dms`
  checks `has_appreciation_touch` BEFORE writing the message (a repeat costs no LLM call) and
  `claim_appreciation_touch` AFTER (so a missing template never burns a person's one shot). The
  claim lands before the send: a thank-you that fails to send is recoverable by a human, one sent
  twenty times is not — so an unreadable ledger reads as "don't send". `connection_accepted` flows
  through the same dispatcher and gets the same protection.
- **OFF until grounded.** `APPRECIATION_SOURCES_ENABLED` (default `false`, read at the call site)
  gates both scrapers. A scraper that finds nothing is a quiet no-op; one that finds the WRONG
  cards DMs real people, so the flip belongs to the owner after a live run of
  `python -m scripts.linkedin_live_validation --appreciation-sources` — read-only, messages
  nobody, claims no ledger row, and reports per card what production would do with it.
