# LinkedIn Engagement Manager — Claude Code Context

## Project Overview

LinkedIn Engagement Manager (LEM) automates LinkedIn engagement end to end: Selenium-based scraping and feed interaction, AI-generated content (via LiteLLM proxy routing to OpenAI / Claude / Ollama / OpenRouter), Celery task queue, React SPA frontend, MySQL persistence, and FastAPI backend.

Two pillars:
- **Content generation & scheduling** — 30-day plan of buyer-journey posts (thought leadership, industry-news commentary, personal story, engagement prompts, carousels, native video, blog summaries) auto-scheduled around peak/golden hours, with sentiment checks and preview/approval.
- **Engagement automation** — feed commenting, replies on the user's own posts, seed first comments, appreciation/outreach DMs with multi-touch follow-ups, and a daily throttled company-page invite drip — driven by per-user targeting, voice/tone, per-day cap preferences.

Code paths in **Feature Areas** below. Subsections end with `docs/*.md` pointers holding the full posture — CLAUDE.md is the map (locations, symbols, constants, invariants, where to find the detail).

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| Web framework | FastAPI |
| Task queue | Celery + Redis |
| Database | MySQL 8 |
| Browser automation | Selenium 4 + `selenium/standalone-chrome` |
| AI proxy | LiteLLM (port 4000) |
| Frontend | React 18 + Vite + TailwindCSS |
| Package manager | Poetry |
| Infra | Docker Compose (local), AWS CDK (cloud) |
| Observability | PostHog |

## Directory Map

```
src/cqc_lem/
├── api/           FastAPI app — engagement_preferences, DM template, PIN endpoints
├── app/           Celery tasks (run_scheduler, run_automation, run_content_plan, generate_variants, my_celery)
├── utilities/
│   ├── ai/        LiteLLM helpers (ai_helper.py, client.py) + content_framework/content_research/content_alignment/story_bank/slop_lint
│   ├── linkedin/  Selenium automation (scrapper, poster, company_page_inviter, verification_pin, rate_limit, helper, profile, token_refresh)
│   ├── marketing/ video_tutorials.py — automated SPA tutorial videos
│   ├── human_pacing.py  ONE cadence engine
│   ├── db.py      All database access (no raw SQL outside this file)
│   ├── proxy.py   Per-user static residential proxy resolution
│   ├── geocoding.py  Login Location city/state geocoding
│   ├── logger.py  Structured logger — log_info/log_error/etc. preferred over myprint()
│   └── selenium_util.py  get_docker_driver() + MV3 proxy-auth extension builder
├── ui/            React SPA (Account.tsx holds engagement prefs)
└── aws/           AWS CDK stacks
tests/
├── unit/          Fast tests — mock all I/O
├── integration/   Require MySQL + Redis service containers
└── e2e/           Require selenium/standalone-chrome
compose/local/database/migrations/  Flyway migrations
.litellm/         config.yaml + complexity_router.py (lem-router pre-call hook)
```

## Code Conventions

- **Logging:** Never use `print()`. Use the structured logger from `cqc_lem.utilities.logger`
  (`log_debug` / `log_info` / `log_warning` / `log_error` / `log_critical`; `myprint()` is a legacy
  shim — avoid in new code). Pass context as keyword args (`user_id`, `post_id`, `task_name`,
  `action_type`, …); `log_error`/`log_critical` take `exc=`.
  **Once is a warning, repeatedly is a defect:** a repeated `log_warning` re-emits at ERROR and files
  ONE grouped `$exception`, so never warn on an expected no-op — log those DEBUG.
  Level table, the escalation contract and the rest of the conventions:
  **`src/cqc_lem/utilities/CLAUDE.md`** (auto-loads when editing that tree) and
  `docs/error-tracking.md`.

- **Type hints:** Required on all function signatures.
- **Enums:** Use `PostStatus`, `PostType`, `LogActionType` from `db.py` for status fields — never raw strings.
- **Imports:** Absolute imports from `cqc_lem.*` throughout.
- **Database:** All DB access goes through functions in `utilities/db.py`. No raw SQL elsewhere.
- **Secrets:** Never hardcode. Use `.env` with `load_dotenv()`. See `.env.example` for required variables.
- **Comments:** Only add a comment when the WHY is non-obvious. No docstring blocks.

## AI Call Pattern

All LLM calls go through LiteLLM proxy via `utilities/ai/client.py`:

```python
response = client.chat.completions.create(model="lem-simple", messages=[...])
```

**Model tier aliases** (defined in `.litellm/config.yaml`):

| Alias | Use case |
|---|---|
| `lem-simple` | Short outputs ≤300 chars: refine, summarize briefly, comma list |
| `lem-medium` | Balanced: comments, post refinement, blog summaries |
| `lem-complex` | Long-form: thought leadership, personal story, industry news |
| `lem-image` | Image generation (DALL-E 3) |
| `lem-embedding` | Embeddings for feedback dedup/clustering (`client.embeddings.create`) |
| `lem-router` | Auto-routes by prompt complexity via `LEMComplexityRouter` |

**Cost-aware down-routing** (`utilities/routing_policy.py`, `utilities/cost_routing.py`): the router can additionally route a tier ONE step down for the treatment cohort of an active cost/quality experiment. `routing_policy.py` is the shared decision core — the app imports it, and docker-compose mounts that same file into the LiteLLM container — so it must stay **stdlib-only** (no `cqc_lem.*` imports). Off unless BOTH `COST_ROUTING_ENABLED` and `COST_AWARE_ROUTING_ENABLED` are set. Since #652 the treatment cohort comes from a PostHog experiment flag resolved app-side and handed to the router in the policy document's `arms` map (the hash stays as the fallback) — see `docs/experiments.md`. Also `docs/cost-performance-margin-plan.md` §D.1.1.

See `ai_helper.py` for the per-function model assignment.

## Selenium Pattern

Always use `get_docker_driver()` from `selenium_util.py`. It connects to `selenium-chrome:4444`, polls readiness, and sets 1920×1080. Never instantiate `webdriver.Chrome()` directly.

Use `click_element_wait_retry()` for all click interactions — it handles transient DOM timing issues.

Browser capacity is a **fixed pool of Chrome session slots shared by the Celery Selenium lanes**, and
`SE_NODE_MAX_SESSIONS` must always equal the summed `SELENIUM_CONCURRENCY` of those lanes —
`tests/unit/app/test_selenium_capacity.py` fails the build if they drift. The Phase-2 horizontal path
(`docker-compose.grid.yml`: hub + N single-session nodes, optionally on a 2nd VPS) carries the same
invariant with node count as the cap, and `python -m cqc_lem.utilities.selenium_load_test` produces
the on-time/resource curve that sizes it. See `docs/SELENIUM_GRID.md` and `docs/scaling-plan.md`.

## Feature Areas

### Content generation & scheduling (`app/run_content_plan.py`, `app/run_scheduler.py`, `utilities/ai/ai_helper.py`)
- AI content by buyer-journey stage (awareness / consideration / decision): thought-leadership, industry-news commentary, personal-story, engagement-prompt posts, carousels (educational / case-study / product-demo / insights), native video, and blog summaries.
- 30-day content plan with balanced post-type distribution; auto-scheduling around golden/peak hours.
- **Cadence (issue #621):** the plan is NOT one post a day. It fills the `posts_per_week` slots (2–7, default 3) of a **fixed day-type calendar** (`POST_DAY_TYPES` in `content_framework.py` — Tue build-receipt / Wed story / Thu spiky POV at the default), which also supplies each post's buyer stage AND narrows its archetype family. Times clamped to waking hours, jittered ±15–30 min, held ≥24h apart. **Which** weekdays are eligible is the separate `posting_days` pref (issue #581, default Mon–Fri `[0,1,2,3,4]`, all seven selectable): cadence says HOW MANY slots, `posting_days` says which days may carry them — weekends are opt-in, the allow-list is the harder bound. Best posting time decides only the HOUR; an empty/invalid day set is normalized back to Mon–Fri.
- Self-healing carousels (stale/errored carousels re-generated into branded slides) and asset backfill.
- `PostType` is `text` / `carousel` / `video`; `PostStatus` includes `error` for generation/posting failures needing manual fix.

### Engagement automation (`app/run_automation.py`)
- **Feed commenting** rebuilt for LinkedIn's SDUI: resilient `find_first`/`click_first`/`find_all_first` selectors (`utilities/linkedin/helper.py`); inline compose + submit; **recency-dominant scoring matrix** (`_score_feed_post` = recency + relevance + reciprocity + activity) with post-age (`_post_age_minutes`) and social-count extraction; best-effort "Recent" feed sort (`_switch_feed_to_recent`); targeting filters + per-day caps + voice/tone. Runs pre-post (≈15 min before each scheduled post) and daily at a golden hour.
- **Replies** to comments on the user's own posts (`automate_reply_commenting`); **seed a first comment** on own posts (`auto_seed_comment_on_post`).
- **Golden-hour presence** (`utilities/golden_hour.py`, issue #622): the ONE place the first-hour amplifier's timing is decided. ONE `golden_hour_report` per swept post (comments found, replies sent, minutes since REAL publish time from the POST log — not `scheduled_time`, or a late publish reads as a late sweep), INFO in-window and WARNING out, shipped via `track_golden_hour_report`. Posts older than a day emit nothing. `latency_minutes=None` + `within_window=False` when publish time unknown — unmeasured is never on-time. A sweep that could NOT run (429, session failure) emits its OWN report (`status=rate_limited`/`session_failed`) and retries (`sweep_retry_countdown`), bounded twice — by attempts AND by the window, so a retry past minute 90 is never scheduled. Reports scoped to twice the phase's window: every sweep walks yesterday's posts too, and grading revisits would bury the on-time rate. **Second wave**: ONE self-comment 6–8h after publish (`auto_second_wave_comment`) that must ADD substance — same #617 contract + similarity gate + slop lint as a feed comment (`_gated_comment`, shared with `generate_ai_response`); ships NOTHING when no draft passes; specifics from story bank (#620); posts through socialActions API like the seed (no browser, 429-immune). 6–8h wait in HOPS (`second_wave_due_minutes` seeded on (user, post); `second_wave_hop_seconds` off `CELERY_VISIBILITY_TIMEOUT`) — `task_acks_late` would redeliver an 8h countdown. Discretionary → stands down under `is_automation_paused()`. Seed + second wave can never stack: cap on COUNT of our own comments on that post URL (`count_user_comments_on_post_url`, `SELF_COMMENT_MAX_PER_POST=2`), so neither task has to know the other ran.
- **DM conversation auto-nurture** (`_nurture_after_reply`, `utilities/ai/dm_nurture.py`): a reply used to END a sequence — now it's classified (interested / objection / not-now / disinterest / neutral) and becomes an **approval-gated** context-aware next message queued as a `pending` row in `scheduled_dms` (`source='nurture'`), one open draft per thread, per-day draft cap, explicit disinterest stops the thread for good.
- **Reciprocity tracking** via the `post_engagers` table — boosts commenting back on people who engaged with us (`get_recent_engagers`).
- **DMs**: appreciation (connection / recommendation / collaboration), profile-viewer outreach, and **multi-touch follow-up sequences** — all templated and voice-aligned (`build_dm_from_template`, `dm_templates`, `dm_followups`, `process_user_followups`).
- **Message-thread resolution ladder** (`utilities/linkedin/message_thread.py`, issue #731): the ONE way LEM opens (and reads) a 1:1 thread. `open_message_thread` walks SIX routes in order — profile anchor → legacy button → tag-agnostic 'Message' text node → top-card **More** menu → direct compose URL from the profile URN (captured BEFORE any route navigates away) → messaging search — and **a route only counts when the thread is provably open** (`msg-s-*` events readable or compose form present), on either surface (profile may yield bottom-right `msg-overlay-*` chat, not `/messaging/`). Class names never keyed on — every locator is href / aria-label / TEXT. RIGHT person: compose URN comes from the person's own compose anchor or the URN beside their `/in/<slug>` in the page model (never "first URN in the document" — that's the viewer's own Me menu); messaging-search row that links to a different slug is rejected outright. Names WHOLE-WORD (`name_matches`) — 'Chris' is a substring of 'Christine Baker' and reading her reply as ours sends the follow-up anyway. Bare compose form never ends the render wait (LinkedIn paints composer before message list); zero events = UNKNOWN. Verdict is **three-valued**: `ThreadState.REPLIED` / `NOT_REPLIED` / `UNKNOWN` — UNKNOWN makes the caller SKIP and leave the row due (missed follow-up is recoverable, follow-up to a reply is not). Self-name is a **required setting**, not a scrape: `users.linkedin_display_name` (Settings → Setup & Connection, its own `account-readiness` item) is what `resolve_self_name` compares against; scraped `profiles.data.full_name` is the fallback. One field, not first/last — message-group label is the full display name as ONE string. Winning route logged (`action_type='followup'`); `--dm-thread-url` reports the `reply_state` live.
- **Owned-asset CTA loop** (`resolve_artifact_delivery` in `content_alignment.py` + `_queue_artifact_delivery`, issue #624): the ONE map from a CTA to its asset, and it names the CHANNEL — **lead magnet** is the comment-keyword mechanic whose payload is a DM; **newsletter** is a subscribe LINK. Newsletter's `newsletter_url` rides in `artifact_cta_line`; #392's `split_link_for_first_comment` decides where it lands — OFF-platform newsletter → first comment (link in body costs 19–60% reach), linkedin.com newsletter → body (penalty is off-platform only). Attribution matches on BOTH halves (`content` OR `first_comment_link`); a first-comment-only count reads 0 forever for the mainline LinkedIn newsletter. Keyword delivery is **approval-gated**: lands as `pending` `scheduled_dms` row (`source='artifact'`); blocked by an open draft from EITHER mechanic in BOTH directions; capped on `max_dms_per_day` at drafting AND re-checked at send. `record_lead_magnet_sent` fires on QUEUE. Attribution rides on `GET /user/newsletter-subscribers` (`count_artifact_cta_deliveries`): subscriber growth reads against the CTAs that actually delivered. `newsletter_links` is None (not 0) with no URL.
- **Human pacing** (`utilities/human_pacing.py`, issue #626): the ONE place cadence is decided. Read-time delay (`pace_read` — length-scaled, floored at `PACING_READ_MIN_SECONDS`, ceilinged below `MAX_INLINE_SLEEP_SECONDS`); `dispatch_jitter_seconds` countdowns on every beat-dispatched engagement task (own-post replies use `PACE_RESPONSIVE`); `daily_budget`/`remaining_actions` turn each per-day cap into a stable random draw (weekend asymmetry + occasional rest days) under one account-level envelope. Seeded on (user, action, date) and persisted in Redis — a retry never re-rolls. Fails open — no Redis, or `HUMAN_PACING_ENABLED=false`, restores pre-#626 behaviour. Pacing only slows us down; the 429 breaker in `rate_limit.py` is the separate, harder gate.
- **Comment outcome tracking** (`sweep_comment_outcomes` + `utilities/comment_outcomes.py`, issue #628): commenting used to be write-only. Read-only T+24h sweep revisits each un-checked `logs` comment row, locates it via the #478 thread map, writes ONE `comment_outcomes` row: author replies, thread replies, likes, whether we replied, `visible_most_relevant` — **three-valued on purpose**: 1 present under 'Most relevant', 0 absent there but present under 'Most recent' (the May-2026 demotion signal), NULL when sort control couldn't be read. NULL rows excluded from the demotion denominator. Unfindable comment = SKIPPED. Weekly report (`auto_weekly_comment_quality`) ships rates to PostHog + `/user/engagement-analytics`; demotion rate > `COMMENT_DEMOTION_HOLD_RATE` on ≥`COMMENT_QUALITY_MIN_SAMPLE` readable readings **holds that user's feed commenting** (`hold_commenting` in `rate_limit.py` — narrower than `pause_automation`) and escalates as CRITICAL. Live: `scripts/linkedin_live_validation.py --comment-outcome-url`.
- **Suppression tripwire** (`auto_suppression_tripwire` + `utilities/suppression.py`, issue #629): 2026 LinkedIn penalties are SILENT — a flagged account sees its reach step-collapse (8,500→340 pattern) and stays collapsed 60–90 days, no notification. A daily beat reads each user's own `build_engagement_trend` series and compares **impressions per post** (or engagement per post when impressions weren't captured — a single impression-less day switches the whole comparison, never mixes scales) against their OWN trailing 14-day median. Days with no posts dropped BEFORE measurement — `SUPPRESSION_CONSECUTIVE_DAYS` means consecutive **posting** days, a weekend off is never a collapse. ≥`SUPPRESSION_DROP_RATIO` drop sustained, or #628's demotion verdict, `pause_automation()`s **engagement only** (posting is API-driven and never gated); read-only stat-capture lanes exempted via `is_measurement_paused` (freeze them and a recovered account can never be seen to recover). WHY recorded in Redis (`record_suppression_trip`, no TTL), emails the user, escalates as CRITICAL. Cold start / thin baseline (<`SUPPRESSION_MIN_BASELINE_POSTS`) / zero baseline = `unknown`, never actioned; one bad day = `watch`. Pause re-armed daily while the trip stands; only refreshed when the standing pause is the tripwire's own. Recovery is human: `POST /user/automation-resume` behind `SuppressionBanner.tsx` off `GET /user/automation-status` — reports a recovered reading beside the standing trip but leaves the decision to the user.
- **Company-page invitations** (`utilities/linkedin/company_page_inviter.py`, issue #732): a paced DAILY drip, not the once-a-month blast it used to be. Run bounded by the SMALLEST of three ceilings: `max_company_page_invites_per_day` clamped by `max_invites_per_day` and run through `human_pacing`; **credit spread** `credits_remaining / days_left_in_month` (renews on the 1st, REFUNDED on accept); live credit count (hard stop at 0). `plan_daily_invites` decides all of that BEFORE a Chrome session opens — most days the allowance is zero. Idempotency is durable, not Redis: today's spend SUMMED out of `logs` rows (`count_company_page_invites_sent_today` — one batched row carries a count). Every run emits `company_page_invite_run` — including the ones that send nothing, since a series carrying only sends can't tell "paced to zero" from "silently broken".

### Engagement configuration (`engagement_preferences` table, API in `api/main.py`, SPA in `ui/.../Account.tsx`)
- Targeting: include/exclude topics/keywords/authors, `min_reactions`, `max_post_age_hours`, plus LLM topic-relevance scoring.
- Voice: tone, `comment_length` (short/medium/long; default short), style, emoji/hashtag toggles.
- Caps: `max_comments_per_day`, `max_dms_per_day`; DM template editor with follow-up steps; Login Location (city/state geocoding via `utilities/geocoding.py`, with admin override).

### Marketing video tutorials (`utilities/marketing/video_tutorials.py`, beat `produce-feature-tutorial`)
- One declarative `TutorialFlow` per feature (routes + the CSS anchors that prove the screen rendered) → headless SPA capture via `get_docker_driver()` → grounded script (`lem-medium`) → TTS (OpenAI `lem-tts` default, ElevenLabs behind `TUTORIAL_TTS_PROVIDER`) → ffmpeg MP4 with branded intro/outro + `.srt` → 9:16 clip → YouTube Data API v3 upload.
- **Fail-closed**: a missing UI anchor, an unparseable script, profanity, an over-cap narration or a fabricated number aborts BEFORE any TTS/publish spend. Cost is attributed per part (script tokens, TTS characters, render minutes) and totalled on the manifest record.
- State lives in `assets/videos/tutorials/manifest.json` (no schema change); the SPA embeds it via `TutorialVideos.tsx`. Weekly cadence, and a flow is re-filmed only when its captured UI fingerprint changes. OFF unless `TUTORIAL_VIDEOS_ENABLED`.
- **YouTube OAuth token** (`youtube_auth.py`, #742): the ONE place its state is decided. Read DB-first (`app_credentials`, installed via `POST /admin/youtube-token` — no deploy), `YOUTUBE_REFRESH_TOKEN` seeds it. `unknown` (Google unreachable) is NOT `needs_reauth` (4xx / lost `youtube.upload` — the only state that alerts). Weekly beat `youtube-token-check` IS the keep-alive vs the 6-month-disuse expiry — never drop it while the feature is off; `produce_tutorial` preflights before spend. Full posture: `docs/youtube-publishing.md`.

### Anti-bot / session infra
- Per-user static residential proxy (`utilities/proxy.py`) + an in-memory **MV3 proxy-auth extension** (`_build_proxy_auth_extension_b64` in `selenium_util.py`) — never URL-embedded credentials, since MV2 background pages are disabled in Chrome 149+.
- Cookie persistence (`li_at` is the DEFAULT engagement login since #745) + an email-PIN verification flow (`utilities/linkedin/verification_pin.py`).
- 429 / auth-wall backoff and resilience (`utilities/linkedin/rate_limit.py`).
- **Secrets at rest** (`utilities/crypto.py`, #745): `li_at`, OAuth tokens and the stored password are AES-256-GCM envelopes keyed per user+column off `LEM_SECRET_KEY`; `db.py` is the ONE caller and the field-name constants are AAD — renaming one orphans every row. Reads dual-mode until `ENCRYPTION_REQUIRED`; failed decrypt → None. Daily `auto_encrypt_secrets_at_rest` backfills AND rotates. No key = pre-#745 behaviour. Full: `docs/secrets-at-rest.md`.

## Testing Standards

- All new/modified code: ≥80% patch coverage enforced by Codecov.
- **Unit tests** (`tests/unit/`): mock all external I/O.
  - Mock OpenAI: `mock_openai_client` fixture (patches `cqc_lem.utilities.ai.client.OpenAI`)
  - Mock DB: `mock_database_connection` fixture (patches `mysql.connector.connect`)
  - Mock Selenium: `mock_selenium_driver` fixture
- **Integration tests** (`tests/integration/`): use real MySQL + Redis service containers.
- **E2E tests** (`tests/e2e/`): use real `selenium/standalone-chrome` container.
- Run unit tests: `poetry run pytest tests/unit -v --tb=short`
- Run with coverage: `poetry run pytest --cov=src/cqc_lem --cov-report=xml`

## Observability

Track events via `utilities/observability.py` (`track_llm_call` / `track_task` / `track_api_call`).
Each subsection below is a one-paragraph invariant; rationale, contracts, sample code, and edge
cases live in the pointed-to doc. Plan with this section, drill in with the doc.

### LLM analytics (issue #647, traces #746)
Two streams, never summed: `llm_call` (app, cost ESTIMATE — every money question reads this) vs
`$ai_generation`/`$ai_embedding` (proxy-native, post-fallback, provider-priced — latency/error/
volume). Attribution lives in `utilities/ai/client.py` (the ONE client); `_attach_routing_metadata`
must stay. **Pipeline traces** group a post/edition/comment's 5–6 calls into ONE `$ai_trace` with
per-step `$ai_span`s: `@llm_pipeline` on the three roots (`create_text_post`,
`generate_newsletter_edition`, `generate_ai_response` — it SUPERSEDES `attribute_llm_cost`),
`@llm_step` on the SHARED-core step functions (never at a call site, so newsletters/comments inherit
it). The client sends the ids two ways because LiteLLM reads them from two places — trace id in the
`x-litellm-trace-id` HEADER (metadata would be overwritten), parent span in `metadata.parent_run_id`.
Nested trace = span; span with no trace = no-op; `LLM_TRACING_ENABLED=false` mints nothing.
Full posture: `docs/llm-analytics.md`.

### Error tracking (issue #648)
Logs (`logger.py` → PostHog Logs) carry message+context; `$exception` is the grouped/fingerprinted
ISSUE alerting + the error→GitHub-issue cron file on — NOT redundant. Use
`observability.capture_exception(...)` for caught-and-not-reraised; `posthog.capture_exception` is
idempotent. Never capture `HTTPException` (4xx is a response, not an issue). Full posture:
`docs/error-tracking.md`.

### Browser-side analytics (issue #646)
ONE SPA surface: `ui/src/utils/analytics.ts` — never call `posthog` directly. `distinct_id =
String(user_id)` matches `observability.py` so browser+Celery+proxy share ONE PostHog person.
Env-gated at BUILD time (`VITE_POSTHOG_KEY`); no key → lazy chunk never fetched → no-op.
`maskProps()` adds `ph-no-capture` + `data-ph-mask`; use it on every content editor. Full posture:
`docs/posthog-advanced-surface.md`.

### Session replay (issue #649)
Rules live in the SDK: `VITE_POSTHOG_REPLAY_SAMPLE` slice + EVERY `$exception`/feedback session,
both via one `ensureSessionRecorded()`. Local `sampleRate` takes precedence — never set project
sampling (multiplies). Canvas off; inputs masked; network capture timings only. Full posture:
`docs/session-replay.md`.

### KPI funnels, dashboards, alerts + weekly report (issue #650)
`scripts/posthog_provision.py` owns Health + Growth dashboards, four threshold alerts, weekly Growth
email. Alert tiles must be native single-series `TrendsQuery` filtering on STRING props (boolean
filters match nothing → silent alerts). Money tiles read `$ai_generation`, never `llm_call`.
`mark_rate_limited()` emits `rate_limit_trip` (WARNING log never reaches PostHog). Full posture:
`docs/kpi-dashboards.md`.

### Experiments (issue #652)
`utilities/experiments.py` is the adapter onto PostHog Experiments, NOT a third implementation.
**Unresolvable experiment = CONTROL arm** (no key, `EXPERIMENTS_ENABLED=false`, inconclusive, SDK
raises — no env fallback per experiment). Flags must use rollout-% / distinct-ID only (person-
property can't be decided locally). Three registered: `cost-routing-arm`, `comment-contract-prompt`,
`post-media-variant`. Full posture: `docs/experiments.md`.

### Marketing attribution — UTMs (issue #658)
`utilities/marketing/attribution.py` is the ONE place. Two rules: only OWNED destinations tagged
(`is_owned_link`); existing UTMs never overwritten (`build_utm_url` fills missing only;
`mark_placement` is the exception — replaces `utm_content` only). `signup_completed_web` (browser)
≠ `signup_completed` (API) — never summed. Full posture: `docs/marketing-attribution.md`.

### Model-tier evaluation harness (issue #721)
`scripts/benchmark_models.py` measures candidates vs each tier's contract suite (NO user data),
always beside the current champion. Deterministic is source of truth (in-repo linters, not a
copy); LLM judge is PostHog Evaluations filtered on `benchmark_run_id` (production never carries
it → customer never billed). Only `recommend` becomes a swap; recommendations are RENDERED, never
written (`.litellm/model_upgrades.yaml` is the retirement map). Full posture:
`docs/model-benchmarks/README.md`.

### Content-quality telemetry (issue #630)
`auto_nightly_content_quality` is the TREND LINE (other gates are one-time verdicts). Scores
posts/comments/editions into `content_quality_scores`: weighted slop (HARD ×3), self-similarity,
**stored** authenticity (no fresh judge call), hook length, impression-weighted ER. **Unscored is
never zero** — each dimension has its own sample size. Never pauses (drift → go look at prompts;
safety is #629). Similarity batches into ONE `lem-embedding` per surface under
`llm_attribution(user_id=…)`; dominant measure only.

### Feature flags (issue #651)
`utilities/flags.py` is the ONE place; **fail open to env var** (no key, disabled, undefined,
inconclusive, SDK raises → all return the flag's env var). `only_evaluate_locally=True` →
ZERO network per check, flip lands without restart. Flags must use rollout-% / distinct-ID only.
Read at CALL SITE, never at import. **Safety controls are NOT flags** (429 breaker, holds, pauses,
per-day caps stay in Redis/env). SPA bootstraps from `GET /api/flags` (server-resolved) — not
through posthog-js. Full posture: `docs/feature-flags.md`.

### Surveys — NPS/CSAT (issue #653)
TWO owners. PostHog Surveys: NPS (30d past ACTIVATION, not signup) + post-quality CSAT (on
`post_approved` once `posts_approved >= 5`). `utilities/surveys.py` keeps the bespoke ones:
trial-T-3d review (#499, PostHog can't unlock) + fix CSAT (#502). Type **`api`**, rendered
headless in `PostHogSurveyModal.tsx`. ONE answer = TWO paths (browser native `$survey_response`
+ POST `/api/survey/posthog` → `feedback` row), counted ONCE — `track_survey_response`
deliberately NOT emitted. Detractor (NPS ≤6 / CSAT ≤2) or any free text stays `new`; happy+blank
→ `resolved`. `markSurveySeen()` advances the 30d wait — drop it and the throttle silently stops.
Full posture: `docs/surveys.md`.

### Endpoints panel + release annotations (issue #654)
**Endpoints** (PostHog beta): HogQL as a versioned cached HTTP route — Dashboard "Live stats"
without a MySQL reporting layer. Every query scoped with `distinct_id = {variables.distinct_id}`
(ONE shared project → un-scoped leaks across customers). Resolves against ONE `InsightVariable`;
endpoint is `blocked_endpoint` until it exists. `GET /user/posthog-stats` server-side only;
personal API key never reaches browser. Failure modes → `available: false` for that panel.
**Release annotations**: `scripts/posthog_annotate.py` posts `"vX.Y.Z deployed"` per deploy;
needs `POSTHOG_PERSONAL_API_KEY` GH secret (annotation R+W); absent/outage → no-op, never a
failed release.

## CI Gates

Before merging any PR, all of the following must pass:
- `CI / Unit Tests`
- `CI / Integration Test w/ Coverage`
- `CodeQL Security Analysis`
- `GitGuardian Security Scan`

## Production Deployment & Environment

LEM runs as a Docker Compose stack on a **Hostinger VPS**, exposed via a Cloudflare Tunnel. There are two checkouts on the box, and they are NOT the same:

| Path | Owner | Purpose |
|---|---|---|
| `/home/lem/linkedin_engagement_manager` | `lem` | Dev/agent working checkout (where you edit + commit). Has `./src` on disk. |
| `/opt/lem` | `deploy` | **Live production stack.** Compose project workdir; `scripts/deploy.sh` checks out release tags here. |

**Standard release flow (the only path that keeps prod on the release train):**

```
local dev → PR to main → CI gates pass → release-please tags vX.Y.Z
  → build-and-push.yml builds ghcr.io/christopherqueenconsulting/cqc-lem:vX.Y.Z → GHCR
  → SSH deploy to VPS runs scripts/deploy.sh vX.Y.Z (git checkout tag, flyway migrate,
    compose up, /health check, auto-rollback to .last_good_tag on failure)
```

- The stack is launched with **both** compose files: `docker compose -f docker-compose.yml -f docker-compose.prod.yml`. `docker-compose.yml` alone is the DEV config (it bind-mounts `./src:/app/src`); **`docker-compose.prod.yml` overrides that away**, so in PRODUCTION every app service runs the code baked into the image — editing files on disk does nothing until a new image ships. Code lives at `/app/src/cqc_lem/...` inside the image.
- Image ref is `${DOCKER_IMAGE_NAME}:${IMAGE_TAG:-latest}`, both set in `/opt/lem/.env` (git-ignored); `scripts/deploy.sh` exports `IMAGE_TAG` per-deploy. App services sharing the image: `web_api_blue`/`web_api_green` (FastAPI blue/green), `celery_worker`, `celery_worker_selenium{,_prepost,_outreach,_content}`, `celery_beat`, `flower`. In PROD, `web_app` is a tiny nginx **edge** that routes to the active color; deploys are zero-downtime blue/green flips, releases batch 4x daily (05/11/17/23 UTC) — see `docs/zero-downtime-deploys.md`. A **`release:now`** label on a PR ships it at merge instead of the next window; agents may self-apply it for high-priority/user-visible fixes — policy in `docs/release-fast-lane.md`. Infra (`mysql`, `redis`, `selenium-chrome`, `litellm`, `cloudflared`, `flyway`) uses its own upstream images.
- **Runtime state (429 breaker, manual automation pause, reply-sweep cadence keys) lives in Redis**, not the DB or containers — it survives deploys. Inspect/repair it by calling the real functions in the container, e.g. `docker exec celery_worker_selenium python -c "from cqc_lem.utilities.linkedin.rate_limit import clear_rate_limit, pause_automation; ..."`, so the correct Redis URL is used.
- **Local hotfix deploy (fallback when CI/release is too slow or blocked):** build a thin overlay image `FROM` the running release tag that only `COPY`s the changed `src` files (identical deps, seconds not minutes), then on the box set `/opt/lem/.env` `IMAGE_TAG=<hotfix-tag>` and `cd /opt/lem && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --pull never <app-services>`. Keep the prior release image locally for instant rollback (`IMAGE_TAG=vX.Y.Z`). **This diverges prod from `main`** — the fix MUST still land via PR→release, or the next release will REVERT it. Requires `sudo` for the Docker socket on this box.

## Known Gotchas

- `get_docker_driver()` previously connected to Selenium Grid hub+node. It now connects to `selenium/standalone-chrome:latest` at port 4444.
- `ai_helper.py` had all functions hardcoded to `model="gpt-4o-mini"` — they now use tier aliases.
- PostHog replaces Prometheus + Jaeger (both removed from docker-compose).
- `linkedin-preview` service (external) was removed — preview is now the native `LinkedInPostPreview.tsx` component.
- **LinkedIn SDUI:** the old `urn:`, `feed-shared-*`, and `comments-comment-*` DOM anchors are gone. Prefer `data-testid` / `aria-label` selectors via `find_first`/`click_first`. The comment composer has NO `<form>` — "submit" means clicking the Comment/Post button next to the composer (`_composer_submitted`), and the comment overflow "…" menu is hover-hidden. **The global nav is sticky**, so never click a composer where the previous action left it — `_focus_composer()` centers it first (a top-of-viewport composer has its click stolen by the nav's `<svg>`: `ElementClickInterceptedException ... at point (x, 9)`, issue #815). Every composer lookup is scoped to its OWN card/comment (`parent_element=card`): the feed walk comments on several posts per page load and LinkedIn leaves each composer mounted after it submits, so a document-wide `div[role='textbox']` returns an EARLIER post's box — which is both the y=9 interception's real source and, once centered, a comment on the wrong post (issue #876). No document-wide fallback: no composer on this card means skip the post. Inline compose failures name the STEP that threw (`Inline comment post failed at focus composer`) — step names stay quote- and digit-free so the escalation dedup key keeps them apart.
- **Emoji in Selenium:** ChromeDriver `send_keys` throws on non-BMP emoji — strip them before typing with `_strip_non_bmp()`.
- **ENUM columns:** `logs.action_type` (and other status columns) are MySQL ENUMs. Adding a new value requires a migration — e.g. V37 added `'followup'` to `logs.action_type`. Migrations live in `compose/local/database/migrations/`. New migrations use **TIMESTAMP** versions (`V<YYYYMMDDHHMMSS>__name.sql`, `date -u +%Y%m%d%H%M%S`) so two branches can never collide — e.g. `V20260726170427__add_comment_outcomes.sql` (issue #628) and
`V20260726230423__add_content_quality_scores.sql` (issue #630).
- **Unified content core:** newsletters, posts, AND comments draw framework (blueprints/variety), research, and alignment from `utilities/ai/content_framework.py` / `content_research.py` / `content_alignment.py`. Do NOT add parallel per-content-type prompt helpers — extend the shared menus/engine. Comment research is OFF by default (`COMMENT_RESEARCH_ENABLED`); the target post is their grounding. Comments carry their own **quality contract + similarity gate** (issue #617): reference a specific claim, add one of {own experience, data point, respectful disagreement, genuine question}, ≥2 sentences, no validation-filler opener, no near-duplicate of the user's last 50 posted comments (`COMMENT_SIMILARITY_MAX`, embedding cosine via `lem-embedding`, token-overlap fallback). A failing draft regenerates up to `COMMENT_GATE_MAX_ATTEMPTS`, then the post is SKIPPED — `generate_ai_response` returns `None`. The **story bank** (issue #620) is the FACT half: `create_text_post` selects ONE entry per post. The TWO save-targeted archetypes (`build_receipt`, `resource_compendium` — issue #619) are `fact_anchored` with WRITER's allow-list = the ONE anchored entry, CHECKERS counting EVERY active entry — the carousel used to pass the whole bank to the writer, which is how one deck spent six receipts at once (#728). The **deck reference gate** (#728) is the save-worthiness half — every BODY slide of a `save_targeted` archetype, or any deck whose caption promises a checklist/stack/framework/numbers, must carry ≥1 reusable artifact. The **deterministic slop lint** (issue #625, `slop_lint.py`) is the cheap explainable layer under the two LLM passes (`humanize_text` #416, `score_authenticity` #382): pure regex/statistics, ~0.5ms. Five HARD checks regenerate up to `SLOP_LINT_MAX_ATTEMPTS` then BLOCK (post held PENDING behind `ai_slop`; feed comment SKIPPED; DM/newsletter/group post ships with a logged reason). Four WARN checks (em-dash density, rule-of-three, burstiness, rhetorical hook) are advisory and never hold anything. Full posture: `docs/content-core.md`.
- **Content mix (70/20/10) governor:** every planned post carries a mix class in `posts.content_mix` — `value` (70%, audience value, sells nothing) / `authority` (20%, expertise education, sells nothing) / `promo` (10%). Classes assigned deterministically in `content_alignment.assign_content_mix` (promo cadence `PROMO_EVERY_N_POSTS`, clamped to 10–30 so promo can never exceed 10%); the promo slot claims a TEXT post and is forced into the `case_snapshot` blueprint; the class rides into the prompt via `alignment_directive(..., content_mix=)`. **Promo CTAs are always an ARTIFACT** (lead magnet / newsletter) — a meeting ask is banned in the prompts (`ARTIFACT_CTA_POLICY`, injected by `cta_policy_directive`), repaired deterministically by `replace_meeting_ask_cta`, and any that survives HOLDS the post at PENDING via the `meeting_cta` quality gate. Compliance is reported on `/user/engagement-analytics` (`content_mix`) and rendered on the Dashboard.
- **Stale lazy chunks after a deploy (issue #743):** zero-downtime covers the server; a browser tab is the other half. It holds the content-hashed chunk names of the build it loaded, and a code-split chunk (jszip in avatar training, any `React.lazy` route) is only fetched when the user triggers the feature — so at 4 releases/day a tab open across one 404s on a hash the new image no longer has, and it reads as "the feature is broken", not "reload me". Three layers cover different windows. **Retention** (`api/spa_assets.py`): both colors mount the named `spa_asset_archive` volume at `SPA_ASSET_ARCHIVE_DIR`, each container syncs its own `ui/dist/assets` in at startup, keeps `SPA_ASSET_ARCHIVE_KEEP` builds (default 5) and serves a live-bundle miss out of the archive — a content-hashed name resolves to one file forever, so `immutable` is preserved. Lives in the app, NOT `deploy.sh`, so archive maintenance can never fail a deploy; an unwritable volume logs a warning and serves the live bundle only. **One reload** (`ui/src/utils/chunkReload.ts`) is the fallback for anything older: `importWithChunkRecovery` / `lazyWithChunkRecovery` wrap a dynamic import (react-query and error boundaries CATCH the rejection, so the window-level `vite:preloadError`/`unhandledrejection` handlers would never see it), and a failure reloads once — `index.html` is `no-store`, so a reload always lands on the current build. The loop guard is a sessionStorage marker: a tab that can't PERSIST it never reloads at all, and a second failure inside the cooldown shows `NewVersionNotice` instead. An OFFLINE tab is never reloaded (`navigator.onLine === false`) — a disconnected dynamic import reports the SAME message a stale chunk does, and a reload with no network turns a working app into the browser's offline page. **New-version awareness** (`ui/src/hooks/useNewVersion.ts`, #754) is the proactive third: both layers above only fire AFTER something failed, and a tab several builds behind can keep working while running old client code against a newer API. It polls `/api/app-info` every `VERSION_POLL_INTERVAL_MS` (5 min), skips the request entirely while the tab is hidden and re-checks on `visibilitychange`, and raises the SAME `NewVersionNotice` — it is a PROMPT and never reloads, so it can't race the one automatic reload. Baseline is the FIRST version this tab read (module scope, so a remount can't re-baseline onto a build that shipped after boot) and the BOOT read is the one poll that ignores visibility — a ctrl-clicked background tab runs the bundle while hidden, so deferring its first read would baseline it onto a build it isn't running. An unreachable endpoint or blank version raises nothing and never becomes the baseline.

## Git Safety & Multi-Agent Concurrency Rules
- **Fresh State Enforcement**: Before executing or generating any code edit, you MUST explicitly run `git status` and a file read command (e.g., `cat <filename>`) to verify no hidden or uncommitted upstream modifications exist. Never rely on your internal conversation memory for file contents.
- **Micro-Branching Workflow**: Do not make edits directly on shared branches while working asynchronously. When starting a distinct task, automatically spin up a task-specific feature branch (`git checkout -b feature/claude-<task-name>`).
- **Atomic Commits**: For every completed sub-task or successful implementation block, automatically stage and commit your files with a clean, concise descriptive message (e.g., `git add . && git commit -m "feat(api): implement active sub-agent locking mechanism"`). 
- **Conflict Avoidance**: If you detect changes in the working directory that clash with your active target files, immediately halt, stash your progress (`git stash`), pull down the current state, and safely resolve the differences before re-applying your changes.
- **Branch cleanup.** Merged feature branches auto-delete (repo setting `delete_branch_on_merge=true`); orphans swept weekly by `.github/workflows/stale-branches.yml`. A 48-hour grace window protects active agent work. Full posture: `docs/branch-cleanup.md`.
