# Settings & Configuration — Research + IA Proposal (issue #558, Phase 1)

**Status:** Phase 1 research + Phase 2 rebuild, both shipped. The owner signed off options
**1A · 2A · 3A · 4A** on 2026-07-26; §8 records what those choices became in code.
**Scope:** every user-facing configuration knob in the SPA and its backing store, how the knobs
interact, and the information architecture that makes them understandable and conflict-safe.

Everything below is read from the code as it stands on `main` (v0.95.1), not from memory. File and
line references are given so the reviewer can check any claim.

---

## 1. Where configuration lives today

`Account.tsx` renders four tabs (`src/cqc_lem/ui/src/pages/Account.tsx:22`), each a stack of cards:

| Tab | Cards | Save model |
|---|---|---|
| Account & Billing | `SubscriptionCard`, `VideoCreditsCard`, `TimezoneCard`, `LoginLocationCard`, `InactivityCard` | 4 independent save buttons, no dirty tracking |
| LinkedIn Connection | `LinkedInLoginCard`, `CompanyPageCard` | 2 independent saves |
| Content & Profile | `ContentProfileCard`, `NewsletterCard` | 2 independent saves |
| Engagement & Automation | `EngagementSettingsCard` (3 cards in one component), `GroupsCard`, `DmTemplatesCard`, `LeadMagnetCard` | `SettingsSaveProvider` + `SaveAllBar`; **only this tab** has Save-All and an unsaved-changes guard |

Configuration also lives **outside** `/account`:

- `Avatars.tsx` — avatar training credits, train/activate an avatar.
- `ContentStudio.tsx` → `ComposePost.tsx` — per-post **Post Type**, **Video Quality**
  (`ComposePost.tsx:293`) and **Content Stage** (`:364`), which override global defaults with no
  cross-link back to where the default is set.

### 1.1 Backing stores

| Store | Rows | Written by |
|---|---|---|
| `engagement_preferences` | one per user, 38 columns | `update_engagement_preferences` (`db.py:3121`) — **one INSERT … ON DUPLICATE KEY UPDATE for the whole row** |
| `users` | timezone, blog/sitemap URL, company page URL, LinkedIn password, login location, `last_login_inactivate_delay`, `auto_schedule_posts`, `content_language`, `content_buffer_days`, `content_buffer_max_posts` | `update_user_preferences` (`db.py:2929`), `/user/`, `/user/timezone`, `/user/location`, `/user/company-page` |
| `newsletter_settings` | one per user, 12 columns | `/user/newsletter-settings` |
| `dm_templates` / `dm_followups` | many per user (11 event types × N steps) | `/user/dm-templates` |
| `lead_magnet_settings` | one per user | `/user/lead-magnet` |
| `user_groups` | one per joined LinkedIn group | `/user/groups` |
| Stripe (`users.subscription_*`) | plan/tier/trial | Stripe webhooks; UI is read + checkout links |

---

## 2. Full inventory

Defaults are the real code defaults: `_ENGAGEMENT_DEFAULTS` (`db.py:3012`), `_NEWSLETTER_DEFAULTS`
(`db.py:5309`), `get_user_preferences` (`db.py:2901`).

### 2.1 `engagement_preferences` (38 columns — the densest surface)

| Setting | Card today | Default | What it actually controls |
|---|---|---|---|
| `tone` | Voice & Tone | `NULL` | Free text steering every generated comment, DM, post and newsletter. VARCHAR(255) since V52. |
| `comment_length` | Voice & Tone | `medium` | Target words per feed comment (~30/55/90). LinkedIn weights ≥15-word comments far above one-liners. |
| `comment_style` | Voice & Tone | `NULL` | Free-text style guidance layered on `tone`. |
| `use_emojis` | Voice & Tone | `true` | Emoji in generated text (stripped to BMP before Selenium typing). |
| `use_hashtags` | Voice & Tone | `false` | Off by design — hashtag-free posts reach ~5–10% more people in 2026. |
| `focus_topics` | Content Focus & Goals | `[]` | Anchors trend-based post subjects and comment angle (`select_focus_topic`). **Not** a feed filter. |
| `business_goals` / `personal_goals` | Content Focus & Goals | `NULL` | Alignment layer for content generation. |
| `authenticity_score_min` | Review thresholds | `NULL` → 60 | A draft below this is held `pending` instead of auto-scheduled. |
| `post_similarity_max_pct` | Review thresholds | `NULL` → 55 | Word-overlap ceiling vs the user's own recent posts; over it, held `pending`. |
| `include_topics` / `include_keywords` / `include_authors` | Targeting | `[]` | If ANY is set, a feed post must match one (keyword/author literal, topic via LLM relevance). |
| `exclude_topics` / `exclude_keywords` / `exclude_authors` | Targeting | `[]` | Always win over includes. |
| `min_reactions` | Targeting | `NULL` → 0 | Hard gate before scoring (`run_automation.py:1148`). |
| `max_post_age_hours` | Targeting | `24` | Hard recency gate (`run_automation.py:1070`). |
| `feed_fallback_when_empty` | Targeting | `true` | Comment on best feed posts when includes match nothing — **only active when at least one include filter is set** (`run_automation.py:1103`). |
| `max_comments_per_day` | Targeting | `20` | Hard daily cap; run exits immediately when reached (`run_automation.py:1064`). |
| `max_dms_per_day` | Targeting | `20` | Hard daily cap across appreciation, outreach, nurture **and catch-up** DMs. |
| `max_invites_per_day` | Targeting | `10` | Combined cap for proactive connect targets + profile-viewer invites. Does **not** cover newsletter invites, and is the hard ceiling on (but not the cap for) company-page invites. |
| `max_company_page_invites_per_day` | Volume | `5` | Daily company-page invite drip (issue #732). Effective ceiling is `min(this, max_invites_per_day)`, further bounded by the Page's remaining monthly credits spread over the days left in the month. |
| `connection_request_mode` | Targeting | `auto_approve` | `pre_review` holds each connect target for approval. |
| `connection_targeting_mode` | Targeting | `suggest` | `off` / `suggest` (always drafts) / `auto_queue` (defers to the mode above). |
| `connection_target_authors` | Targeting | `[]` | Adjacent-author profile URLs to harvest engagers from. |
| `min_connection_icp_score` | Targeting | `55` | 0–100 ICP fit floor; only applied to scraped profiles. |
| `max_catchup_touches_per_day` | Targeting | `5` (ceiling 5, or 10 on Professional/Enterprise) | Catch-up congratulations per day; also counts against `max_dms_per_day`. |
| `catchup_touch_mode` | Targeting | `pre_review` | Approve each congratulations vs. queue LinkedIn's draft as-is. |
| `catchup_event_types` | Targeting | `["job_change","promotion"]` | Which milestones qualify. **Empty list = catch-up off.** |
| `catchup_message_source` | Targeting | `linkedin` | LinkedIn's own draft (no LLM) vs AI rewrite from the DM template. |
| `default_video_quality` | Targeting | `standard` | Standard / Premium (1 credit) / Premium Top (3 credits); falls back to standard with no credits. |
| `link_in_first_comment` | Targeting | `true` | Publish links in the first comment instead of the body (body links cost ~60–68% of reach). |
| `reply_check_mode` | Targeting | `event` | `event` (email-triggered) / `scheduled` (timer) / `off`. |
| `reply_sweeps_per_day` | Targeting | `2` (2–12) | Only read when `reply_check_mode='scheduled'` (`run_scheduler.py:308`). |
| `reply_max_post_age_days` | — | `2` (1–14) | How far back a reply sweep looks. **No UI control** — round-tripped only because it is in the TS type. |
| `reply_to_own_comments` | Targeting | `true` | **Dead.** Referenced only in the request model and the column list; no automation code reads it (§3, F1). |
| `post_types` | — | `[]` | **Dead column** — accepted by the API, never read (F2). |
| `default_buyer_stage` | — | `NULL` | **Dead column** — accepted by the API, never read (F2). |

Read-only fields the GET adds for the UI: `reply_inbound_address`, `gmail_forward_confirmation`,
`max_catchup_touches_allowed`, `gate_defaults`, `feed_reach` (`api/main.py:1897`).

### 2.2 `users`

| Setting | Card | Default | Effect |
|---|---|---|---|
| `timezone` | Timezone | `America/New_York` | Golden/peak-hour post scheduling and the newsletter publish hour are all resolved in this zone. |
| Login Location (city/state/country → lat/lng) | Login Location | none | Per-user proxy/session geo consistency; also the source of `effective_content_language`. |
| `last_login_inactivate_delay` | Preferences | `NULL` (never) — **the UI form initialises to 90** | If the user hasn't logged in within the window, **all** automation pauses (`db.py:2200`). |
| `auto_schedule_posts` | Preferences | `true` — **the UI form initialises to `false`** | Off = every generated post waits for manual approval. |
| `content_language` | Preferences | `NULL` (auto) | Language of generated content incl. premium video audio. |
| `blog_url` / `sitemap_url` | Content & Profile | `NULL` | Blog-summary posts and newsletter blog alignment. |
| `company_linked_in_url` | Company Page | `NULL` | Enables the daily company-page invite drip. |
| LinkedIn password | LinkedIn Connection | `NULL` | Required for Selenium login; without it engagement silently does nothing. |
| `content_buffer_days` / `content_buffer_max_posts` | — | code defaults | Read by `run_content_plan.py:1567` to decide how far ahead drafts are generated. **Unreachable in the SPA** (F4). |

### 2.3 `newsletter_settings`

`enabled` (false), `title`, `topic`, `cadence` (weekly), `publish_day` (1 = Tue), `publish_hour`
(9), `max_queued_drafts` (1), `generate_lead_days` (3), `invite_connections_enabled` (false),
`max_invites_per_run` (50), `align_with_blog` (true).

### 2.4 Other

- **DM templates** — 11 event types (`types.ts:131`): connection accepted, recommendation received,
  collaboration, profile viewer, nurture, plus six catch-up milestones. Each has step 0 + N
  follow-ups with `delay_hours`. Blank = built-in default.
- **Lead magnet** — enabled, trigger keyword, DM message.
- **Groups** — per joined group, TWO independent switches (issue #769): *Comment* (daily value-add
  comments on other members' posts, out of the daily comment cap) and *Post* (the weekly ORIGINAL
  group post — never a copy or reshare of a scheduled feed post, one group per week, rotating to
  whichever post-enabled group has gone longest without one). Both on by default; the card names the
  group the next post lands in, and (issue #932) shows that post's actual text a couple of days
  early — editable in place, or skippable for the week.
- **Billing** — plan/tier/trial, Stripe portal, video-credit and avatar-credit packs.

---

## 3. Findings from the inventory

| # | Finding | Impact |
|---|---|---|
| **F1** | `reply_to_own_comments` is a live toggle in the UI that **no automation code reads**. Reply behaviour is decided solely by `reply_check_mode`. | A user who turns it off still gets auto-replies. Trust-breaking. |
| **F2** | `post_types` and `default_buyer_stage` are columns the API accepts but nothing reads. Worse: the SPA's `EngPrefs` type omits them, so every save from the SPA rewrites them to `[]` / `NULL` (the PUT model defaults them and the upsert writes the full row). | Dead config that also silently resets. |
| **F3** | `reply_max_post_age_days` (1–14) has no control anywhere. | Unreachable setting. |
| **F4** | `content_buffer_days` / `content_buffer_max_posts` are read by the content plan but have no UI. | Users can't control how far ahead drafts are generated. |
| **F5** | "invites" means three different things — `max_invites_per_day` (connects + profile-viewer), newsletter `max_invites_per_run`, and company-page invites (own cap since #732, but ceilinged by `max_invites_per_day`). | Users assume one cap governs all outbound invites. |
| **F6** | `feed_fallback_when_empty` is presented as an unconditional toggle but is a **no-op when no include filter is set** (`run_automation.py:1103`). | The safety net looks on and isn't. |
| **F7** | The "Engagement Targeting" card is a grab-bag: it also holds catch-up config, connection targeting, video quality and link-in-first-comment. Its title describes ~1/3 of its contents. | Nothing is findable. |
| **F8** | Three separate save buttons ("Save Voice & Tone", "Save Focus & Goals", "Save Targeting") all PUT the **same** row. One over-long field fails all three at once — the V52 incident. Save-All and the unsaved-changes guard exist on the automation tab only. | Inconsistent, and a known silent-rollback class of bug. |
| **F9** | Catch-up touches consume `max_dms_per_day`; connection targeting consumes `max_invites_per_day`. Both are disclosed in a single line of grey microcopy. | Caps interact invisibly. |
| **F10** | Voice & Tone sits under "Engagement & Automation" but governs posts, comments, DMs **and** newsletters. | Users look for it under Content. |
| **F11** | `InactivityCard` initialises `autoSchedule` to `false` and the inactivity delay to `90` before the API responds; if `/user/settings` returns `preferences: null`, a save writes those values. | A user can accidentally turn auto-scheduling off and set a 90-day auto-stop. |

F1–F4 and F11 are pre-existing defects surfaced by this research, not new regressions. They are in
scope for Phase 2 only if the owner says so (decision 4).

---

## 4. Conflict matrix

Severity: **H** = automation silently does nothing; **M** = behaves differently than the user
expects; **L** = suboptimal but working.

| # | Combination | Result | How we can detect it | Proposed guidance |
|---|---|---|---|---|
| C1 | any `include_*` set **and** `min_reactions` high | Feed matches ~0 posts | `feed_reach.matched_topics == 0 && examined > 0` (already returned by the API) | Warn with the live funnel: "your last scan matched 0 of N posts — loosen topics or lower min-reactions" |
| C2 | `include_*` set **and** `feed_fallback_when_empty` off | Zero comments on sparse days | static | Warn + one-click "turn the fallback on" |
| C3 | `feed_fallback_when_empty` on **and** no `include_*` | Toggle is a no-op (F6) | static | Inform: "nothing to fall back from — this only applies once you set include filters" |
| C4 | `max_comments_per_day = 0` with targeting configured | All targeting is dead config | static | Warn: "commenting is off — your targeting has no effect" |
| C5 | `reply_check_mode = off` **and** `reply_sweeps_per_day` raised | Cadence ignored | static | Hide the cadence field (already conditional) + inform on save |
| C6 | `reply_check_mode = event` **and** Gmail forwarding never confirmed | Replies never fire, silently | `gmail_forward_confirmation.confirmed` is false/absent | Warn with the 3-step setup already in the card, promoted to a status chip |
| C7 | `reply_check_mode = scheduled` | Opens a browser session per sweep → 429 risk | static | Existing amber warning; keep, and add the cost in the preview |
| C8 | `max_catchup_touches_per_day ≥ max_dms_per_day` | Catch-up starves appreciation/outreach/nurture DMs | static | Warn: "catch-up alone can consume your entire DM budget" |
| C9 | `max_dms_per_day = 0` with DM templates/follow-ups configured | Nothing sends | static | Warn |
| C10 | `catchup_event_types = []` with a non-zero catch-up cap | Catch-up is off despite the cap | static | Inform inline on the checkbox group |
| C11 | `connection_targeting_mode = auto_queue` **and** `connection_request_mode = pre_review` | Targets sourced but nothing sends without approval (intended) | static | Inform, not a warning |
| C12 | `connection_targeting_mode = off` **and** `connection_target_authors` set | Authors ignored | static | Inform |
| C13 | `min_connection_icp_score ≥ ~80` | Almost no candidate clears it, and it only applies to scraped profiles | static | Warn above a threshold |
| C14 | `max_invites_per_day = 0` **and** newsletter `invite_connections_enabled` | Newsletter invites still send (different cap) — F5 | static | Inform, and rename the fields so the scope is explicit |
| C15 | `auto_schedule_posts` on **and** strict gates (`authenticity_score_min` high / `post_similarity_max_pct` low) | Posts still held `pending`; auto-schedule looks broken | static | Warn: "with these thresholds most drafts will still wait for review" |
| C16 | `auto_schedule_posts` off | The plan stalls until the user reviews | static | Inform + link to Content → Review |
| C17 | short inactivity auto-stop **and** infrequent login | **Every** automation setting becomes moot | static | Inform prominently at the top of Setup |
| C18 | newsletter `cadence = weekly`, `max_queued_drafts = 1`, `generate_lead_days = 0` | Draft appears the day it publishes — no review window | static | Warn |
| C19 | newsletter `publish_hour` set with an unset/default timezone | Publishes at the wrong local time | timezone still at the `America/New_York` default and never saved | Inform |
| C20 | `exclude_topics` / `exclude_authors` overlapping the user's own focus topics or ICP | Excludes always win — silently kills reciprocity | string overlap with `focus_topics` / `include_topics` | Warn, listing the overlapping terms |
| C21 | `max_post_age_hours` very low (≤4) **and** `include_*` set | Tiny candidate pool | static | Warn (folds into C1's funnel message) |
| C22 | `link_in_first_comment` off **and** lead magnet enabled | Body links cost ~60–68% of reach, contradicting the lead-magnet strategy | static | Inform |
| C23 | `default_video_quality` premium/premium_top with 0 video credits | Silently degrades to standard | credit balance | Inform with a buy-credits link |
| C24 | `max_catchup_touches_per_day = 10` on a non-premium plan | Silently clamped to 5 on save, and on downgrade | `max_catchup_touches_allowed` | Bound the input (already done) + explain the clamp |
| C25 | `use_hashtags` on | ~5–10% less reach | static | Inform (existing microcopy) |
| C26 | `comment_length = short` | Weighted ~2.5× lower than substantive comments | static | Inform (existing microcopy) |

C1, C2, C6, C8, C9, C15, C20 are the seven that produce **silent zero output** — those are the ones
worth interrupting the user for.

---

## 5. Proposed information architecture

**Shape:** a Settings hub with a persistent left rail (tabs on mobile), grouped by the question the
user is actually asking. Route stays `/account` (deep links and the readiness banner point there);
sections become `?section=<key>` so existing `?tab=` links can be mapped.

| # | Section | Contains | Why this grouping |
|---|---|---|---|
| 1 | **Setup & Connection** | LinkedIn connection + automation password + extension, company page, timezone, login location, inactivity auto-stop | "If this isn't right, nothing runs." Mirrors the existing readiness checklist. |
| 2 | **My Voice** | tone, comment length, style, emojis, hashtags, focus topics, business/personal goals, content language | One place for everything that shapes *how LEM sounds* — across posts, comments, DMs and newsletters (fixes F10). |
| 3 | **Content & Publishing** | blog/sitemap URLs, auto-schedule, review thresholds *(advanced)*, content buffer *(advanced, new — F4)*, link-in-first-comment, default video quality | Everything about producing and shipping posts. |
| 4 | **Who I Engage** | include/exclude topics · keywords · authors, min reactions, max post age, feed fallback, LinkedIn groups, **live reach preview** | Targeting only — nothing else (fixes F7). |
| 5 | **How Much & How Often** | comments/day, DMs/day, invites/day, catch-up/day, reply mode + cadence *(advanced)*, **presets** | The volume dial, in one place, with the cross-cap interactions made visible (fixes F9). |
| 6 | **Outreach & DMs** | DM templates + follow-ups, lead magnet, connection targeting + approval + ICP score, catch-up milestones + mode + message source | Everything that sends a person a message. |
| 7 | **Newsletter** | all newsletter settings | Already coherent; keep as its own section. |
| 8 | **Account & Billing** | plan/trial, Stripe portal, video credits, avatar credits | Commerce, not behaviour. |

Avatars keep their own page; section 8 links to it.

**Per-setting microcopy contract** — every control renders the same three-part block:

```
Label                                        [control]
What it does   — one plain sentence, no jargon.
Why it matters — the consequence for reach / replies / safety.
Recommended    — the default and when to move off it.
```

"Why it matters" and "Recommended" collapse behind a `?` on narrow screens so the dense sections
stay scannable.

**Progressive disclosure.** Each *card* shows ≤6 primary controls (the two dense sections — My Voice
and Who I Engage — split into two cards each); everything else sits behind one "Advanced" disclosure
per section: review thresholds, content buffer, ICP score, reply sweep cadence,
`reply_max_post_age_days` (F3).

**Save model.** One `SettingsSaveProvider` wrapping the whole hub (today it wraps only the
automation tab — F8), one Save bar, per-section dirty state. The three engagement save buttons
collapse into one, because they always wrote the same row anyway. **The single
`engagement_preferences` upsert is preserved exactly** — no field-level PATCH, no split writes.

---

## 6. Conflict prevention UX

Three tiers, evaluated by one pure function over the merged settings object so the same rules can be
unit-tested without rendering:

- **Inform** (grey, always visible) — correct but non-obvious behaviour: C3, C10, C11, C12, C14,
  C16, C17, C22–C26.
- **Warn** (amber, inline next to the offending control, non-blocking, with a one-click fix where
  there is an obvious one) — the silent-zero-output set: C1, C2, C4, C6, C8, C9, C13, C15, C18,
  C20, C21.
- **Block** — reserved for values that would fail the write itself (over-length `tone`, out-of-band
  numerics). These are already clamped server-side; the client blocks so the user sees *which*
  field, instead of losing the whole row (the V52 failure mode).

Warnings never prevent saving. A user who wants a strict configuration keeps it; they just can't
claim they weren't told.

**Impact preview.** `GET /user/engagement-preferences` already returns `feed_reach` (examined →
passed filters → matched topics → commented, plus `fallback_used`). Section 4 renders it as a funnel
directly under the targeting controls, so the conflict warning for C1 cites the user's own last
scan rather than a hypothetical.

---

## 7. Guided presets

Three presets on section 5, mapped to the outbound ramp the owner already signed off for the brand
account (`brand_account.py:37`, `PHASE_OUTBOUND_POLICY`) — so presets don't invent a second volume
policy:

| Preset | comments/day | DMs/day | invites/day | connect approval | targeting | catch-up |
|---|---|---|---|---|---|---|
| **Conservative** (= P0) | 8 | 5 | 5 | pre_review | suggest | 2/day, pre-review |
| **Balanced** (= P1, recommended) | 15 | 10 | 8 | auto_approve | auto_queue | 5/day, pre-review |
| **Aggressive** (= P2) | 20 | 15 | 10 | auto_approve | auto_queue | 5/day (10 on premium), auto-use |

Presets set a **coherent combination** and are never sticky: changing any individual value flips the
selector to "Custom". No preset exceeds the shipped defaults (comments 20 / DMs 20 / invites 10) —
the same ceiling rule `BRAND_CAP_CEILINGS` enforces. Existing users' saved values are never
overwritten; the preset row shows "Custom" until they pick one.

---

## 8. What Phase 2 shipped

No database migration was needed, and no endpoint changed shape. All of it lives under
`ui/src/pages/account/settings/`.

| File | Role |
|---|---|
| `sections.ts` | The 8 sections, the legacy `?tab=` map and `resolveSection()`. |
| `registry.ts` | One descriptor per setting: label + the three microcopy strings + `advanced`. The single source of truth every control renders from, so microcopy can't drift. |
| `conflicts.ts` | Pure `evaluateConflicts(ctx) → Finding[]` encoding every row of §4, plus `blockingIssues()` for values MySQL would reject. |
| `presets.ts` | The §7 table (mapped to `brand_account.PHASE_OUTBOUND_POLICY`), `presetValues()` and `detectPreset()`. |
| `EngagementPrefsContext.tsx` | The single `engagement_preferences` object + its one mutation, shared by the five sections that edit part of it. |
| `UserPrefsContext.tsx` | Same for the single `/user/settings` write (inactivity · auto-schedule · content buffer · language), which the hub renders three sections apart. |
| `ConflictsContext.tsx` | Evaluates §4 against live engagement/account state plus saved newsletter, lead-magnet, credit and timezone state. |
| `Field.tsx` | The microcopy contract, the three-tier conflict notice with its one-click fix, the `Advanced` disclosure. |
| `SettingsHub.tsx` | Left rail (tabs on mobile) with per-section alert badges, `?section=` routing, ONE `SettingsSaveProvider` + save bar for the whole hub. |

**Decisions as implemented**

- **1A** — the 8-section hub. `?section=` routing; `?tab=account|linkedin|content|automation` still
  resolve (→ billing · setup · content · targeting).
- **2A** — Conservative / Balanced / Aggressive, mapped to P0/P1/P2 with the catch-up cap clamped to
  the plan's ceiling. A **new** account (the GET now returns `has_saved_preferences`, backed by the
  existing `db.has_engagement_preferences`) opens on Balanced as an **unsaved** change with a banner
  saying so — nothing is written until the user saves, so no existing user's values, and no
  account's outbound posture, can change without an explicit save.
- **3A** — warn, never block. Amber inline warnings with a one-click fix where one exists; the live
  `feed_reach` funnel renders directly above the targeting controls so the C1 warning cites the
  user's own last scan. The only hard block is a value the write itself would reject (over-length
  `tone`/`comment_style`/goals, out-of-band numerics) — blocked client-side so the user sees *which*
  field instead of losing the whole row (the V52 failure mode).
- **4A** — UI-layer cleanup, no migration: the dead `reply_to_own_comments` toggle is gone (the value
  still round-trips untouched); `post_types` and `default_buyer_stage` are now in the `EngPrefs` type
  so a save stops silently resetting them (F2); `content_buffer_days` / `content_buffer_max_posts`
  (F4) and `reply_max_post_age_days` (F3) are reachable under Advanced. F11 is fixed as a
  consequence of the shared context: preferences are never written before they have loaded.

**Tests.** The SPA had no test runner; Phase 2 adds `vitest` + `@testing-library/react` (`npm test`)
and a CI step in `ui-build.yml`. 54 tests cover every row of §4 including the blocking validations,
the presets and their ceilings, the section/deep-link routing, the registry's microcopy contract, and
the `Field` / `ConflictNotice` / `Advanced` components. `tsc -b` and the production build stay clean.

**Compatibility.** Every setting from §2 is still reachable, the single-row
`engagement_preferences` upsert is unchanged, and `GET /user/engagement-preferences` only gained one
additive read-only field.
