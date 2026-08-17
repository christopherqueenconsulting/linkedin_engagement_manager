# Engagement automation internals

Full design detail for the engagement subsystems that CLAUDE.md's "Engagement automation"
section only names. This doc is the load-bearing detail; CLAUDE.md keeps the one-line invariant +
pointer.

Since #1154 the code lives entirely in `app/engagement/` — the feed walk, the group composer and the
roster tail are `feed.py`, the connect rail `invites.py`, the newsletter rail `newsletter.py`,
publishing plus the post-publish sweeps (`post_to_linkedin`, the reply sweep, comment follow-ups,
comment outcomes, post/audience stats) `posting.py`, and DMs plus outreach (appreciation, the
profile-viewer walk, the connect-candidate scan, the outreach funnel, the catch-up lane)
`outreach.py`. `app/run_automation.py` is GONE — #1154 emptied it to a re-export shim and #1206
deleted it — so **patch the module that OWNS the code**; there is nothing else left to patch. Every
task still answers to its ORIGINAL wire name (`cqc_lem.app.run_automation.<fn>`), pinned in its own
decorator, so nothing about routing or the beat changed. That spelling is a wire identifier, not a
module path: it is still correct in `celeryconfig.task_routes` and must never be "corrected".

## Feed commenting on the SDUI feed (`comment_on_feed_inline`, issues #622 / #817)

The commenting engine was rebuilt for LinkedIn's SDUI: the old `urn:` / `feed-shared-*` anchors are
gone, so every lookup goes through the resilient `find_first` / `click_first` / `find_all_first`
helpers in `utilities/linkedin/helper.py`, compose+submit happens inline on the card, and every
composer lookup is scoped to its OWN post (`_post_composer_for_card` / `_reply_composer_for_comment`
— a miss is a DEBUG no-op, never a warning). Targeting, per-day caps and voice/tone come from
`engagement_preferences`. Runs pre-post (~15 min before a scheduled post) and daily at a golden hour.

### The scoring matrix is recency-DOMINANT

`_score_feed_post` ranks candidates on four weighted terms — recency (dominant), relevance,
reciprocity (the author engaged with us, or is an include-author target), and a healthy-activity
bonus off comment count. Each weight is env-overridable (`FEED_SCORE_W_RECENCY`,
`FEED_SCORE_W_RELEVANCE`, `FEED_SCORE_W_RECIPROCITY`, `FEED_SCORE_W_ACTIVITY`).

Because recency dominates, the matrix only does what #622 intended when the feed it ranks is
actually recency-ordered. With the sort control missing, the recency term re-ranks a pool LinkedIn
already reordered by engagement, and the run quietly degrades to roughly what #622 replaced.

### `_switch_feed_to_recent` — best-effort, but never silent (#817)

It flips the home feed from 'Top' to 'Recent' AND **reports what the run actually got**. The
returned state rides onto the feed funnel and the `feed_scan` event, so an unsorted scan can never
be read as recency-sorted:

| State | Meaning |
|---|---|
| `recent` | Confirmed on 'Recent' by the control itself |
| `top` | Control found, still on the algorithmic sort |
| `missing` | No sort control on the home feed at all |
| `unknown` | Control there, but which sort applies could not be read |
| `n/a` | A surface that never had one (group feed, roster activity) |

- `recent` is returned ONLY when the control confirms it **afterwards**. An unverified flip recorded
  as sorted tells the same lie the old silent no-op told.
- `_feed_sort_state` returns `''` (→ `unknown`) when the label is unreadable, **including a label
  naming BOTH sorts** — some dropdown triggers spell their options into the accessible name ("Sort
  by, currently Top, options Top and Recent"), and reading 'recent' out of one would skip the flip
  AND record the run as sorted.
- `missing` is graded against the PAGE before it is logged as drift (#1108). A dead session, a
  login wall and a rotated anchor all return the same `None` from `find_first`, so the lookup runs
  with `warn_on_miss=False` and hands the miss to `report_zero_walk` against
  `button[aria-label^='Hide post by']` — an anchor the sort chain does not use. Only a feed that
  provably rendered posts warns. **The returned state is `missing` in all three cases**: the
  cross-check moves the log level, never what the run reports it ranked. See
  `docs/sdui-selenium-notes.md` for what the live DOM measured.
- `_is_home_feed` gates the whole thing: group feeds and a roster author's recent-activity page
  reuse the same engine but never had the control, so a miss there is `n/a` at DEBUG. An
  **unreadable URL counts as NOT the home feed** (#872) — a dead session cannot say which surface it
  was on, and escalating on a guess costs a triage for working behaviour.
- Locators are an ordered fallback chain, most-stable anchor first: `aria-label` → `data-testid` →
  visible 'Sort by' text → a popup trigger whose whole label IS the current sort → any
  `role=button` carrying 'sort'. Class names are never keyed on.
- Fail-fast (`max_try=1`): this runs twice per run and each retry round burned `MAX_WAIT_RETRY` ×
  ~5s to report the same miss.

Live grounding: `scripts/linkedin_live_validation.py --feed-sort`.

## Replies on our own posts, and the seed comment (`sweep_reply_comments`, `auto_seed_comment_on_post`)

Two different jobs on the user's own post: **seed** the thread, then **answer** it.

### The seed comment is the user's own FIRST comment (`auto_seed_comment_on_post`, issue #344)

A value-adding open question or behind-the-scenes insight — no links — posted so the thread that
drives reach has somewhere to start, and so link-in-first-comment suppression is beaten by adding
real value rather than by hiding a link.

- **Posts through LinkedIn's socialActions API** (`w_member_social`, the same token that publishes
  posts), NOT Selenium. Commenting on your OWN post needs no browser and no login, so this lane is
  immune to the feed-navigation 429 that gates everything else here. Everything it needs — post
  body, voice synthesis, profile, prefs — is a DB read.
- **Grounded on the `posts` row** (`get_post_content`), falling back to the POST log only when the
  row is gone. Historical POST logs stored a *status string*, which is why seed comments once read
  as if they were about the `/posts` API instead of the post's subject.
- **Idempotent** on `has_user_commented_on_post_url`: a retried or re-dispatched task must not leave
  a SECOND comment (duplicate own-comments are what `consolidate_duplicate_comments_for_user` exists
  to clean up).
- A link **held back at publish time** (issue #392 — C3) is appended deterministically by
  `append_link_to_comment`; a link on its own still ships when the generator came back empty, since
  losing the link entirely is the worse failure.
- **No pinning.** LinkedIn exposes no pin API, and the seed's thread-starting value stands without it.

**The seed counts against the self-comment cap.** `SELF_COMMENT_MAX_PER_POST` (default 2 in
`utilities/golden_hour.py`, env-clamped to 1–5) is enforced on the **COUNT of our own comments on
that post URL**, not on which task ran — so the seed and the second wave can never stack into
thread-stuffing however either is re-dispatched, and neither task has to know the other ran.

### Replying is a SWEEP, not a per-post poll (`sweep_reply_comments`)

The default post-publish path walks new comments across the user's RECENT posts in **ONE Selenium
session**, triggered either by a forwarded comment-notification email (event mode) or by the
scheduled dispatcher. It replaced a 24h-per-post polling loop that was itself driving the 429.

- `sweep_slot` is part of the `QueueOnce` key, so the golden-hour amplifier can enqueue several
  distinct sweeps for one user while same-user-same-slot still dedups. `attempt` is the in-window
  retry counter and is deliberately **NOT** in the key, so a retry still dedups against a
  concurrently-queued sweep of the same slot.
- Every post swept emits a golden-hour report (below), so the amplifier's silence can be diagnosed
  rather than guessed at.
- 429-safe: a rate-limited session logs a clean skip and returns; a later trigger or sweep retries.

`automate_reply_commenting` (`app/engagement/posting.py`) is the **single-post** variant, retained
for the manual/API trigger and back-compat. It re-queues itself with a widening future-forward
ladder (0 / 5 / 10 / 15 / 30 / 60 min) until `loop_for_duration` runs out — the duration expiring is
its exit condition, which is why both branches of that bookkeeping log at DEBUG.

Both paths share `_reply_to_comments_on_open_post`, which is also the only writer of `post_engagers`
(see Reciprocity capture, below).

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

### Reciprocity capture — who lands in `post_engagers` (issue #1091)

`_reply_to_comments_on_open_post` is the ONLY writer of `post_engagers`, and it writes one row per
THIRD-PARTY commenter on the user's own post. Our own seed / second-wave comments are skipped before
any capture, so a post whose thread is nothing but our own comments produces no rows — correctly.

The identity comes from `composer.comment_author_identity`, which reads the card's HEADER anchors —
never `find_element("a[href*='/in/']")`, whose first hit can be the avatar link (an href with no
text) or an @mention inside the body (someone else entirely, the #478 false "mine" match). A card
that names nobody is a **countable DEBUG** ("Commenter name unreadable"), because the name-keyed
half (`upsert_engager`, `_flag_lead_signal`) has no key while the href-keyed half (own-comment skip,
reply dedup) keeps working — which is exactly how an empty table can sit under a healthy-looking
reply sweep.

An empty `post_engagers` therefore has TWO causes that look identical in the DB, and only the live
probe separates them: **no third-party commenter existed** (`--commenter-read` grades `unknown` and
says so) versus **the reader rotated** (it grades `drift` — the SHIPPED header read naming nobody).
It reports the gap against the naive first-anchor read either way, but only grades that gap `drift`
on an image predating #1091, where the naive read is still what ships.
Measured 2026-08-14 on the three freshest posts: 6 comment cards, all ours, 0 third-party — the
first case, with `post_outcome` agreeing at 1–2 comments per post.

**Settled on 2026-08-16 (owner decision on #1091), so two things are deliberate, not gaps:**

- **A commenter is the ONLY input.** Reactions on our posts are NOT a `post_engagers` source.
  Widening reciprocity to reactors was considered and declined here: reacting is a far weaker signal
  than commenting, and it is a feature with its own acceptance criteria, not a repair for an empty
  table. An empty `post_engagers` under an `unknown` grade is an AUDIENCE fact — nobody commented —
  and belongs to the growth work (`docs/engagement-growth-analysis-2026-07.md`), never to this lane.
- **`unknown` raises nothing.** It stays a probe grade in the weekly sweep JSON:
  `scripts/sdui_drift_issues.py` files only `drift`, and no telemetry event or streak alert rides on
  it. A run of posts with zero third-party comments is read off impressions/`post_outcome`, not off
  a reciprocity alarm — making the empty table louder does not create engagement.

## DM templates, sequences and follow-ups (`build_dm_from_template`, `dm_templates`, `dm_followups`, `process_user_followups`)

A DM sequence is three tables' worth of state: a per-user template per `(event_type, step)` in
`dm_templates`, a due row per prospect-step in `dm_followups`, and one Celery task that drains them.

### Rendering a message (`build_dm_from_template`)

Template text → placeholder render (`{first_name}` / `{headline}` / `{blog_url}` / `{event_detail}`,
headline falling back to `"my professional field"`) → LLM refinement to the user's voice, ≤300 chars
→ humanization pass (issue #416 — A5) → deterministic slop lint with a bounded re-refine
(`lint_repaired`, issue #625 — D1).

Two fail-open decisions in that chain, both deliberate:

- The humanization pass **keeps the pre-humanize text** if the rewrite would exceed the 300-char DM
  budget.
- **A still-slopped DM is SENT**, with the offending patterns named in the log. A DM has no review
  queue, so dropping it would silently break the outreach sequence — the louder failure is the
  quiet one. Refinement raising at all falls back to the plainly-rendered template.

`None` comes back **only** when no template exists for that `(event_type, step)`, and `None` is what
ends a sequence: the caller marks the row `stopped`.

### Scheduling the next step — and the reply check that has no step (`enqueue_next_followup`, issue #623)

If a template exists for the next step it is scheduled at `now + delay_hours`, `due_at` stored as
naive UTC to match `get_due_followups`.

When there is **NO** next step, a **reply check** is scheduled at that same step anyway. This is the
fix for a silent months-long dead end: the stock templates are step-0 only, so the no-next-step
branch used to end the thread the moment the first DM went out — nothing was queued in
`dm_followups`, `process_user_followups` therefore never ran, nobody's reply was ever read, and the
issue #485 auto-nurture that turns a reply into an approval-gated next message could not fire.
`scheduled_dms` had zero rows in production from V53 onward. The check costs one thread open: a
reply becomes a nurture draft, and silence falls into the existing "no template for this step"
branch and stops the sequence. Default wait 48h (`DM_REPLY_CHECK_DELAY_HOURS`).

### Draining the due rows (`process_user_followups`, `max_per_run=20`)

One Selenium session per run. `resolve_self_name` is resolved **once per run** — the saved display
name from Settings, with the scraped profile as fallback — and an empty result means every thread
reads `UNKNOWN` and nothing is sent, which is the intended outcome, logged as a warning naming the
setting to fix.

Per due row, `check_dm_replied` decides everything:

| `ThreadState` | What happens |
|---|---|
| `UNKNOWN` | **SKIP and leave the row due.** We could not read the thread, so we do not know whether they answered — sending anyway is the one irreversible mistake in this lane (issue #731). The next run re-reads it, and the miss is a greppable warning |
| `REPLIED` | Read the inbound message once and use it twice: buying-intent flagging (issue #483) and auto-nurture (issue #485). Stop the old sequence **FIRST** — the nurture path enqueues its own re-check, and a blanket stop afterwards would cancel it. If nurture produced no draft, the catch-up funnel (issue #482) is the fallback, and even that is skipped when the reply was an explicit stop intent |
| not replied, `event_type` is the nurture re-check | Mark `stopped`. Nurture **never** auto-sends a template — the drafted message is the operator's to approve |
| not replied, ordinary step | Render the template, dispatch `send_private_dm`, log `FOLLOWUP`, mark `sent`, and enqueue the next step |

**An empty due-list is a WARNING, not a DEBUG no-op.** This task is only dispatched for users who
already have due rows, so "nothing due" means the row was consumed between dispatch and run — or
that nothing is being enqueued at all, which is exactly how the nurture queue stayed empty for
months (issue #623).

## DM conversation auto-nurture (`_nurture_after_reply`, `utilities/ai/dm_nurture.py`)

A reply used to END a sequence — now it's classified (interested / objection / not-now /
disinterest / neutral) and becomes an **approval-gated** context-aware next message queued as a
`pending` row in `scheduled_dms` (`source='nurture'`), one open draft per thread, per-day draft
cap, explicit disinterest stops the thread for good.

### Who the recipient is comes from stored data, never a page visit (issue #1625)

The draft used to know their first name and nothing else, so a short or neutral reply left it with
nothing to be specific about and it read as filler. `dm_nurture.recipient_context()` is the ONE
place that gap is filled, and it reads only what LEM already holds — **no profile visit is opened
to write a draft**. A Chrome session per draft was the alternative and was rejected: it is an
account-risk and cost decision the draft does not justify, and the lane must keep working on the
people we have never scraped.

| Field | Source | When it is missing |
|---|---|---|
| `first_name` | the `dm_followups` row the caller already has | omitted from the prompt |
| `job_title` / `company_name` / `industry` | `db.get_profile_facts` — the by-URL `profiles` scrape cache, the same reader the nightly lead scorer uses for ICP fit | omitted; someone we never scraped is simply absent from that table |
| `thread_origin` | the follow-up row's `event_type`, mapped through `_THREAD_ORIGINS` | omitted — including on a `nurture` row, where the event type IS the sequence and the original trigger is not on the row |

**A `_THREAD_ORIGINS` phrase must say what its source actually observed, in the right direction** —
the prompt hands it to the model as ground truth, so an overstated one is a false claim about a real
relationship in a message to a real person. The two that read backwards from their names:
`connection_accepted` comes from `accept_connection_request`, so THEY invited US and the user
accepted (never "they accepted your request"); `collaboration` comes from `get_recent_collaborators`,
which walks the **mentions** feed, because LinkedIn exposes no collaboration event — #968 already had
to rewrite that event's default DM template for claiming a shared project, and the same wording must
not return through the prompt.

- **Missing is the normal case, not a fault.** The resolver returns whatever it found (`{}` is
  valid), `format_recipient_context` renders only the fields present, and a draft is never dropped
  for want of context — it is just less specific, which is the pre-#1625 behaviour. An unscraped
  profile logs DEBUG; only a failed *read* warns.
- **Context is not licence to invent.** The block is headed "everything I know about them, and
  nothing more", and the system prompt forbids inferring team size, budget, tools, problems or
  goals from a title or company, on top of the existing no-prices/no-timelines rules. The draft
  stays approval-gated either way.
- `JSON_UNQUOTE(JSON_EXTRACT(...))` hands back the four-character string `'null'` for a JSON null,
  so `_UNKNOWN_FACTS` filters it — otherwise the prompt reads "their title is null".
- When the LLM produces nothing the lane still falls back to `build_dm_from_template`, which
  ignores their reply entirely. That fallback is unchanged but now logs at INFO, so how often the
  least-relevant draft in the queue fires is readable.

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

### Reading a thread and SENDING into one are different questions (issue #1030)

The ladder answers "can I read this thread". A send needs more, so `send_dm_now` does **not** use it.

- `open_addressed_composer` is the ONE entry point for a message about to be sent. It **navigates,
  never clicks**: the URN comes off the person's own profile page, the compose URL is rebuilt from
  it, and the composer's recipient is read back. A wrong thread costs the read path a wrong verdict;
  it costs the send path a message in a stranger's inbox (the #1012 hazard class).
- The compose URL needs **`recipient=` as well as `profileUrn=`** — `profileUrn` alone selects the
  thread but adds nobody, so the page opens on an empty "Enter message recipients" field. That
  composer is *open* (so `thread_reading` reports success) and addressed to no one; `compose_url_for`
  is the one place that URL is built, for both paths.
- `composer_recipient` is the proof, and `''` is never "probably fine" — no recipient means **do not
  send**. Refusing to congratulate someone is recoverable.
- **Sent means the message LANDED**, not that Send accepted a click (`_dm_send_landed`): our text as
  the newest message confirms; text still sitting in the composer disproves; unreadable trusts the
  click and warns, because reporting a delivered message as failed invites a duplicate send.
- What broke: LinkedIn renders the affordance as `<a href='/messaging/compose/…'>`, and
  `send_dm_now` was still clicking `button[aria-label*='Message']`. It matched nothing, so **every**
  DM failed at step one — private DMs, scheduled DMs, appreciation and catch-up congratulations all
  ride this one function. It stayed invisible because the failure logged at INFO, which never
  reaches PostHog and so never escalated; it logs ERROR now.

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
- **The keyword is the CONSENT signal, so it has to be a whole WORD** (#1528). A bare substring
  test matched `AUDIT` inside "auditing our stack" and delivered a resource to someone who never
  asked — read from their inbox, that is a DM about something irrelevant to them.
- **Only a commenter we can actually DM gets a draft** (#1528). This lane OPENS a new thread, and
  LinkedIn allows that to a 1st-degree connection only, so a 2nd/3rd+ commenter's draft was
  un-sendable the moment it was approved. The gate is `can_open_dm_thread` on the badge the sweep
  already read off the card, and it **fails OPEN** — an unrendered badge is unknown, not "not
  connected". A thread that is ALREADY open (#485 nurture) is never gated this way: they replied
  to us, so the thread exists whatever the badge says.
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

- **Three-valued on purpose**: 1 present under 'Most relevant' — or present on a thread LinkedIn
  rendered no sort control for at all, since nothing is ordered there (#1117) — 0 absent under
  'Most relevant' but present under 'Most recent' (the May-2026 demotion signal), NULL when the
  sort control couldn't be read. NULL rows excluded from the demotion denominator. What separates
  the two no-label cases is the evidence scan: a row that still NAMES a sort is drift and stays
  NULL, a scan that came back blind stays NULL too (an empty capture is equally a failed read), and
  a scan that described the page without naming a sort is an affordance LinkedIn did not render.
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

## Groups sync — and reconciling the rows it already wrote (issues #1316, #1487)

`/groups/` renders the joined list AND a rotating "Groups you might be interested in" rail on one
page, with identical `/groups/<id>/` hrefs. `_enumerate_joined_groups` used to take both, so the
weekly sync INVENTED memberships and `auto_comment_in_groups` walked into groups the account never
joined. #1316 filters on the nearest preceding heading; #1487 is the other half — the rows the old
sync already wrote are still sitting `enabled=1`, and only a write can clear them.

- **`_read_groups_directory` is the ONE walk**, and it reports BOTH populations: `joined` (what the
  sync upserts) and `recommended` (ids the page positively filed under a recommendation heading).
  `_enumerate_joined_groups` is the `joined` half under its old name, because that is what the
  read-only `--group-membership` probe drives.
- **Reconciling fails CLOSED at every step**, because the cost of being wrong is a group the user
  IS in going quiet with nobody to notice. A walk that enumerated nothing reconciles nothing; a
  cross-check (`_GROUPS_DIRECTORY_CROSSCHECK_SEL`) that could not be READ reconciles nothing; a
  cross-check that matched ZERO on a walk that found rows is the tripwire going blind — a WARNING,
  and still nothing disabled. That cross-check is one control per JOINED row, so it also counts
  them: a walk that kept FEWER joined rows than the page renders controls has had its heading
  attribution file memberships as offers (a re-worded joined heading does exactly that), and no id
  is disabled on a recommendation heading that run — they drop to the ABSENT population and are
  asked on their own pages instead. Only the 60-anchor cap explains a short walk innocently, so
  `_GROUP_DIRECTORY_ANCHOR_CAP` is a named constant a test holds against the walk's own literal.
- **"Not enumerated" is not "not a member."** The walk scrolls a fixed `(600, 1200, 1800)` and caps
  at 60 anchors while user 1 already renders 55, so absence can be lazy-load. Two populations, two
  standards of proof: an id the page filed under a recommendation heading is disabled on that
  reading alone; an id merely ABSENT is asked again on the group's OWN page
  (`_confirm_group_membership`), and only a header Join control disables it. `unknown` is a real
  answer and is never actioned.
- **The group-page reading is presence-of-share-box + absence-of-Join**, never a Leave button: the
  2026-08-14 live header carried no membership control at all. Controls are scoped to the header
  because the page renders a *Join* per card in its own recommendation rail — #1012 one layer down,
  in the reading rather than the click — and a label must LEAD with the verb, or a group named
  "Join the Data Guild" reads `not_member` with its share box on screen. A lead-Join AND a share box
  together is a CONTRADICTION, not a Join that outranks it — that is what the scope having reached a
  rail card looks like — so it answers `unknown` and the group is asked again next run.
- **A disable is `enabled=0`, never a DELETE.** `disable_user_groups` is its own auditable writer
  (not the SPA's `set_groups_enabled`): it only ever writes that one column, leaves `post_enabled`
  alone, logs the user and the ids it switched off, and leaves the row for `get_user_groups` so the
  Account UI can turn it back on. Whatever `GROUP_RECONCILE_MAX_CONFIRMATIONS` leaves over is logged,
  not silently dropped — and the ids inside the cap are SAMPLED from the whole backlog, never sliced
  off its front: `get_enabled_group_ids` answers in a stable order, so a fixed head re-asks the same
  ids every week and a tail sitting behind more than ten real memberships is never reached at all.

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
- **Skipping is reversible, and a group post can carry media** (issue #1224). The studio reads the
  newest `ready` OR `skipped` draft so a mis-click no longer costs the week; the user owns exactly
  those two statuses (`GroupPostDraftStatus.user_settable()`) and a restore that would make a SECOND
  open draft is a 409. Media rides the post-image surface, is gated by `owns_post_image_url`, goes
  into the composer BEFORE the text, and fails OPEN — text alone beats no post. Full posture,
  including the best-practice list the prompt and the studio share: **`docs/group-posts.md`**.
- **The undo window closes at the publish slot** (issue #1415). `utilities/group_post_slot.py` is the
  ONE place that boundary is computed — the first Tuesday 15:00 UTC after the row was last written,
  which is the slot the draft is WAITING ON. Not `created_at` alone: a draft the publish beat carried
  forward has a first slot already in the past, and measuring from it would make a skip on that row
  irreversible the moment it was made. The draft payload carries `can_undo_skip` / `undo_deadline` so
  the SPA shows **Undo skip** only while the PUT would honour it and says the skip is final after;
  the PUT refuses a late undo with 409. Undo restores the SAME row — never a regenerated draft, which
  is what the one-open-draft invariant forbids — and an undo on a week that was never skipped is an
  expected no-op (DEBUG). Unreadable timestamps fail OPEN: the accidental skip is the bug, and a
  restore ships at the next slot.

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
  it reads in production as "no recent recommendations" forever — a silently dead trigger. That
  tripwire survives the #1007 rebuild: the JS proves only the date SHAPE, so blocks that all resolve
  and none of which `_parse_recommendation_date` can read is the reader and the parser drifting
  apart, and it still warns.
- **A recommendation card is a DATE-CARRYING BLOCK around a `/in/` anchor, not a list item (#1007).**
  The 2026-08-03 grounding run found `/details/recommendations/` renders no `<li>`, no `<time>`, no
  `[data-view-name]` and nothing with `role='tab'`, so every rung of the original ladder was
  unmatchable and the scan read zero cards forever — the mentions half worked and this half was
  silently dead. `_RECOMMENDATION_ROWS_JS` reads the page in ONE call instead: climb from each
  `/in/` anchor to the outermost ancestor still about that one person (it stops the moment a second
  profile joins the block), keep the block only if it carries a "Month D, YYYY" line. That excludes
  the "Who your viewers also viewed" rail and the footer help-links list — the page's only
  `main div[role=list]` — structurally, not by class name, which is all this DOM has left. The tab
  click stays best-effort: the bare URL already lands on Received, and `?detailScreenTabIndex=2` is
  Pending, whose rows are recommendation *requests* and never thank-worthy.
- **`page_dated` is the recommendations tripwire.** Zero cards on a page whose text plainly carries
  "Month D, YYYY" is drift and warns; zero cards on a page with no date is an account nobody has
  recommended and stays a DEBUG no-op. Without that split the two readings are the same number,
  which is precisely how the dead ladder survived a merge. It is the page-vs-reader half only —
  the reader-vs-parser half is the undated-cards warning above, and both are needed.
- **The mentions half has the same tripwire, read off the page's own sentences (#1374).** Every rung
  of `_MENTION_CARD_LOCATORS` is about the card CONTAINER, so one SDUI wrapper rename answers zero to
  all four and reads exactly like a month with no mentions — which is the reading a probe run came
  back with on 2026-08-10 (`cards: 0`, `state: unknown`) on the same surface #982 had resolved a card
  on. `_mentions_page_native_count` counts "mentioned/tagged you" in the page's text, through no part
  of that chain, and `grade_zero_walk` decides what the zero means: the page still saying it while
  nothing resolves is drift and WARNS, the page saying nothing either is a quiet day at DEBUG, and an
  unreadable page is `unknown` and grounds nothing. Counting the same sentence production requires of
  a card is what makes it evidence rather than a second guess. The probe carries the identical read
  (`page_mentions` beside `cards`, `crosscheck_source` naming whether the image or the piped script
  answered) so its zero grades the same way. **The 2026-08-16 run settles the original question: 2
  cards, 2 dated, 2 in window, `page_mentions: 2` — the chain is intact and the August 10 feed was
  genuinely empty.**
- **`appreciation_touches` is the claim, and it is what makes this safe.** The beat re-queues
  itself every ~60s inside its window, so a standing list without a durable claim is a DM a minute.
  One row per (user, person, event_type); the unique key is the guarantee. `_dispatch_appreciation_dms`
  checks `has_appreciation_touch` BEFORE writing the message (a repeat costs no LLM call) and
  `claim_appreciation_touch` AFTER (so a missing template never burns a person's one shot). The
  claim lands before the send: a thank-you that fails to send is recoverable by a human, one sent
  twenty times is not — so an unreadable ledger reads as "don't send". `connection_accepted` flows
  through the same dispatcher and gets the same protection.
- **A profile URL is percent-DECODED before it keys anything.** SDUI escapes the hyphens of a
  vanity slug (`/in/jane%2Ddoe%2D1234`), and the 2026-08-03 grounding run returned exactly that,
  so `_normalize_profile_url` decodes the path as well as stripping query/fragment/trailing slash.
  Encoded and decoded are the same person; two spellings in `appreciation_touches` would mean two
  thank-yous. This is the shared normalizer, so the catch-up and roster ledgers get the same fix.
- **A name is read from the card's sentence when the actor link has none.** The same run found a
  mention whose `/in/` link rendered with no text at all, while the card plainly read *"Utkarsh
  Tiwari mentioned you in a comment in …"*. `_mention_actor_name` recovers the name from that
  sentence, bounded to the ≤5 punctuation-free words before the verb so notification chrome
  ("Unread notification.") can never be mistaken for a name. Nothing name-like left means the DM
  opens with "Hi there" — a deliberate fallback, since a generically addressed thank-you still
  beats not thanking someone who publicly featured you. The probe applies the same fallback, so a
  blank `name` in its report means production really would say "there".
- **The age has to be a STANDALONE token.** A notification card carries the quoted post as well as
  its own timestamp, and `$5m ARR` clears a `\b` just like `2h` does — so a word-boundary match
  would read a two-year-old mention as posted minutes ago and thank the person for it.
  `_RELATIVE_AGE_RE` requires start-of-text or a whitespace/bullet/bracket before the digits.
  Prose that still parses ("10 years of experience") can only push the age OUT of the window, and
  out of the window means skip — the safe direction on a surface that DMs real people.
- **The stock `collaboration` template says what fired it.** A mention, not a project: *"thanks for
  the mention — genuinely appreciated. What are you working on at the moment?"*. It is the code
  DEFAULT only — a user who customized the template in `dm_templates` keeps theirs.
- **Appreciation DMs spend the ordinary per-day DM budget.** `_appreciation_dm_budget` is
  `remaining_actions(..., ACTION_DM, max_dms_per_day, count_dms_sent_today, caps=…)` — the same
  allowance and the same #626 account envelope `send_scheduled_dm` and the outreach funnel spend,
  computed ONCE per pass and threaded through all three triggers so they cannot each spend the cap.
  This is what the dedup ledger does not cover: the ledger stops one person being thanked twice,
  the budget stops thirty people being thanked at once, which is what the first pass after the flag
  is flipped would otherwise be. Whoever the budget cannot afford is left **unclaimed**, so a later
  pass thanks them; a spent budget also skips both scrapes rather than loading two pages for a list
  it cannot act on.
- **OFF until grounded.** `APPRECIATION_SOURCES_ENABLED` (default `false`, read at the call site)
  gates both scrapers. A scraper that finds nothing is a quiet no-op; one that finds the WRONG
  cards DMs real people, so the flip belongs to the owner after a live run of
  `python -m scripts.linkedin_live_validation --appreciation-sources` — read-only, messages
  nobody, claims no ledger row, and reports per card what production would do with it.

## Stale-invite withdrawal (`utilities/linkedin/stale_invites.py`, issue #969)

The beat `clean-up-stale-invites` had sat on the schedule at 02:00 every night returning
`{"status": "not_implemented"}`. Nothing withdrew anything, so pending invites accumulated forever —
LinkedIn caps how many may be outstanding at once, and a pile of months-old unanswered ones both eats
that ceiling and drags the acceptance rate the outreach features are judged on.

- **Withdrawing is ONE-WAY.** LinkedIn blocks re-inviting the same person for ~3 weeks afterwards, so
  every decision fails CLOSED. An invite whose "Sent … ago" stamp cannot be read is **never stale**
  (`parse_sent_age_days` returns `None`, and `None` is skipped), and a row with no resolvable
  Withdraw control is left alone.
- **Only the row's own `Sent …` line is parsed.** The text handed to the parser is the WHOLE card —
  name, headline, buttons — and a headline is free to read "10 years ago I started…". The trailing
  `ago` is required too. If LinkedIn ever stops writing "Sent", every row reads unreadable and the
  lane withdraws **nothing**; the run report's `unreadable` count (and one run-level warning when
  EVERY row is undated) is what makes that visible instead of silent.
- **ON by default since #1006 grounded it** (`STALE_INVITE_WITHDRAWAL_ENABLED`, default true;
  `false` — or any unrecognised value — still silences the beat). It shipped OFF because the
  selectors were written from the invitation manager as documented, not from a live grounding run,
  and an ungrounded Selenium lane that silently matches nothing is exactly how the catch-up lane
  (#792/#964) spent months doing nothing. The 2026-08-07 run closed that gap end to end: rows and
  their "Sent … ago" stamps read (43/43 dated, 0 unreadable), the list turned out to load on SCROLL
  rather than through a pager, and ONE owner-authorised real withdrawal proved the confirm dialog
  (a native `<dialog data-testid="dialog">` whose confirm button names the invitee). Re-ground it
  whenever the page moves:
  `python -m scripts.linkedin_live_validation --sent-invites` — strictly read-only, it resolves rows
  and describes them, **nothing is withdrawn by running it**. Zero rows has THREE readings and the
  probe separates them, because an account with nothing outstanding and a rotated row anchor report
  the same zero: it samples the page's own text (`page_text`) and reports the rendered empty state
  (`empty_state`, e.g. "no pending invitations"). Empty state ⇒ clean account, anchors **untested**;
  page text but no empty state ⇒ probable drift; no page text at all ⇒ the page never rendered and
  the run grounds nothing. The first live run (2026-08-03) returned zero rows on an account with no
  outstanding invites, which is why the distinction exists.
- **Paced and capped like every other Selenium lane.** `plan_withdrawals` decides the allowance
  BEFORE a Chrome session opens (most runs are zero and must cost no slot). The lane draws its own
  `ACTION_WITHDRAW_INVITE` budget — its own key, or `daily_budget` would overwrite the connection
  lane's stored draw with this different cap for the rest of the day — and is deliberately NOT in
  `ENVELOPE_ACTIONS`: housekeeping must never ENLARGE a day's outbound allowance. It still passes
  `caps`, so it is **bounded by** the envelope. Every hard gate (`is_automation_paused`, which the
  #629 suppression trip rides, and the 429 breaker) is re-read per withdrawal, because a breaker can
  trip mid-walk.
- **Oldest first.** The budget is small, so it is spent on the invites least likely to ever be
  accepted rather than on whichever row happened to render first. The list renders newest-first, so
  the run expands it (`_load_more_rows`, bounded) before reading — a walk that never loads past the
  first page can only see rows it must not touch, which looks exactly like "nothing is stale".
  `expansions` on the report says whether the walk ran out of road.
- **A handle read before a withdrawal is never spent after one.** A withdrawal RE-RENDERS the list —
  that is how verification works at all — so every click after the first re-reads the page and
  matches the target on its profile URL (`_resolve_control`). Re-using the original element handle
  has two failure modes and only one is harmless: the node is detached and the click raises, or the
  framework re-used that node for the row that shifted up into its place, and a one-way withdrawal
  lands on a DIFFERENT, possibly days-old invite. A row that cannot be re-identified is skipped —
  a missed withdrawal is recoverable next run, a wrong one is not. For the same reason a container
  holding more than one Withdraw control is rejected page-side rather than read as one row: it is the
  LIST, and accepting it would date every invite by the newest one's stamp.
- **The daily spend is counted on the CLICK, not the verdict.** One `logs` row per DISPATCHED
  withdrawal (`STALE_INVITE_WITHDRAWN_MESSAGE`; SUCCESS when the row was verifiably gone afterwards,
  FAILURE when it was not) and `count_invite_withdrawals_today` counts BOTH — the click already
  reached LinkedIn, and a lane whose verification broke must not be free to click every row on the
  page. Counting the immutable logs rather than Redis is what makes a second run the same day, or a
  worker restart, idempotent.
- **Only a verified withdrawal earns the success status.** A run that clicked without verifying, and
  a run whose stale rows all went stale before they could be clicked, both report `failed` — they are
  the two shapes selector rot takes here.
- **Observability:** every run emits `stale_invite_run`, including the ones that do nothing. A series
  carrying only withdrawals would reproduce exactly the invisible-stub problem this replaced;
  `rows_seen` is the tell.

## Connect escalation when following doesn't unblock commenting (issue #979)

The rung above follow. The ladder per roster target is **blocked → follow (#962) → still blocked →
needs connection → (opt-in) auto-connect**, and every step of it is evidence-driven: a
connections-only author is indistinguishable from a followers-only one until a follow has been tried
and has failed to change anything.

- **`needs_connection` is a claim we have to have evidence for.** It is set only when a target is
  `follow_status='following'`, HAS a `followed_at`, and its PREVIOUS blocked visit was already after
  that follow — i.e. this is the SECOND post-follow blocked visit. One is a render race; two is the
  account telling us following was not the missing permission. A target that was never followed is
  never escalated, so a user who leaves auto-follow off never sees this badge from automation.
- **It is decided in the SAME statement that records the blocked visit**
  (`record_target_comment_blocked`), with `connect_status` assigned FIRST: MySQL evaluates SET
  clauses left to right, so updating `last_blocked_at` first would destroy the very evidence the
  test reads. The function returns a `BlockedVisit(streak, connect_status)` so the caller can
  announce the crossing exactly once, comparing against the state the run loaded.
- **A landed comment stands the escalation back down.** `record_target_engagement` resets
  `needs_connection` → `unknown` alongside the streak: commenting just worked, so "following didn't
  unlock commenting" is no longer true. Only that state is cleared — an invite already sent is a
  fact about LinkedIn that a comment does not undo. It stands down **in the run's own loaded row
  too**, not just in the database: the connect rung reads the roster row as the walk loaded it, so a
  target commented on THIS pass would otherwise still be carrying `needs_connection` when the rung
  runs a few lines later, and would spend its one invite on an account we had just proved we can
  comment on.
- **The badge names the ONE move left.** Distinct copy from the #962 badge
  ("Following didn't unlock commenting — connect with this account"), and it SUPERSEDES it: two
  badges naming different moves for one account is how a user stops reading either.
- **Free read-only advancement, every visit** (`reconcile_roster_connect_state`). LinkedIn already
  shows a Pending control or a 1st-degree marker on the page the roster pass has open, so
  `requested` and `connected` are read for nothing — no click, no budget, and NOT gated on either
  toggle, because a user who connected by hand must see their badge clear. It only ever moves
  FORWARD: `unknown` means "we could not tell", never "the invite vanished". `failed` is re-read
  for the same reason #962 re-reads `follow_failed` — terminal means no more SENDS, not no more
  reading, and a badge that can never clear is a permanently wrong answer. Only `connected` (the
  end of the ladder) and `unknown` (nothing has escalated) skip the read.
- **The connect reading names the page owner too** (`_CONNECT_STATE_JS`), for the reason
  `_FOLLOW_CONTROL_JS` does: "Connect" and "Message" render all over an activity page. A 1st-degree
  marker counts only inside the target's OWN `/in/<slug>` card, and a Message control counts only
  when LinkedIn is not still offering to Connect — open profiles expose Message to strangers.
  **One label is SHORTENED and needs the card, not the name:** LinkedIn writes the top card's
  Message control as "Message *Harshal*" (grounded 2026-08-03 — the full-name matcher missed it and
  only the 1st-degree marker carried that reading), and can render a bare "Connect" with no
  aria-label. Those two count ONLY inside the owner's own card (`ownerCard` — grow outward from the
  control until a profile link resolves; anyone else's link means we've grown into their module),
  never page-wide, because a "More profiles for you" rail can hold another Harshal. **Pending is
  always full-name**: LinkedIn writes it that way, and a wrong `requested` freezes the ladder
  instead of merely stalling it. The shortened path only ever adds a reading where the strict one
  returned `unknown` — it cannot take one away.
- **Auto-connect is OFF by default** (`roster_auto_connect`) and INDEPENDENT of `roster_auto_follow`
  — an invite is heavier and less reversible than a follow. It fires only for `needs_connection`
  targets and every hard gate applies (`_outbound_hold_reason`, re-read per target).
- **No new invite mechanic, and NOT via Outreach/Leads.** The invite is enqueued onto the existing
  rail — `send_roster_connect_invite` (`se_outreach`) is a thin wrapper over the same
  `invite_to_connect_now` the reactive profile-viewer and proactive #398 flows use. It runs as a
  task rather than inline because that rail opens its OWN Chrome session, and a second session
  inside the roster pass's would take a slot out of the pool the Selenium lanes share. Outreach /
  Leads / Pipeline are DM-sequence flows for prospects: a curated peer needs comment access, not a
  sales journey, and a second invite path would double-spend LinkedIn's invite tolerance.
- **Roster invites take a MINORITY share of the shared budget.** There is no roster invite cap:
  `roster_connect_budget` spends the same `max_invites_per_day` (`ACTION_INVITE`, with queued-but-
  unsent requests counted as spent) and takes at most `ceil(remaining / 3)`, min 1, so #398's lanes
  are never starved. Most days that arithmetic is 0–1 invites — the ladder is slow by design.
  The budget is re-read per target AND the run's own dispatches are subtracted (`queued_this_run`).
  Both halves are needed: unlike a follow, the send is ASYNCHRONOUS, so nothing durable records the
  invite until the task reaches LinkedIn — a re-read alone would hand every target in the walk the
  same "3 left" and invite a whole roster of restricted authors in one pass.
- **ONE shot per target, ever.** `requested` is written BEFORE the dispatch: a lost dispatch or a
  worker that dies mid-send must not leave the target eligible for a second invite. Only a send that
  provably never reached LinkedIn (429 breaker / kill-switch → `LinkedInRateLimited`) hands the
  target back to the ladder. A real failure is terminal (`failed`, badged, never auto-retried), and
  an "already connected" answer records `connected` rather than badging a connection as a failure.
  `ENGAGEMENT_TARGET_CONNECT_TERMINAL` is the closed vocabulary for that, and
  `queue_roster_connect_invite` re-checks it even though its caller already did.
- **`ConnectStatus` (db.py) is the ONE vocabulary** — MySQL ENUM, DOM reading, write sites — exactly
  as `FollowStatus` is, and a unit test parses the ENUM out of the migration so a member added
  without one fails in CI.
- **Live grounding:** `python -m scripts.linkedin_live_validation --roster-connect <profile-url>`
  reports what the top card says about our connection (Pending / 1st-degree / nothing readable) plus
  every visible control label. Read-only — no invite is sent. Note this rung ships NO new clicker at
  all: the only thing that clicks is the already-grounded Connect affordance on the profile page.
- **Observability:** `roster_connect_requested` rides the feed funnel — invites the ladder sent this
  run. A `requested` state read off the card (the user invited them by hand) is deliberately NOT
  counted there; it is not a send the run made.
