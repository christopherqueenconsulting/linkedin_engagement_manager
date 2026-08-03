# LinkedIn Engagement Manager — Claude Code Context

## Project Overview

LinkedIn Engagement Manager (LEM) automates LinkedIn engagement end to end: Selenium-based scraping and feed interaction, AI-generated content (via LiteLLM proxy routing to OpenAI / Claude / Ollama / OpenRouter), Celery task queue, React SPA frontend, MySQL persistence, and FastAPI backend.

Two pillars:
- **Content generation & scheduling** — 30-day plan of buyer-journey posts (thought leadership, industry-news commentary, personal story, engagement prompts, carousels, native video, blog summaries) auto-scheduled around peak/golden hours, with sentiment checks and preview/approval.
- **Engagement automation** — feed commenting, replies, seed first comments, appreciation/outreach DMs with multi-touch follow-ups, and a throttled company-page invite drip — driven by per-user targeting, voice/tone, per-day caps.

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

`AttributedOpenAI` is the ONE client — it stamps attribution + trace ids on every endpoint, and it
**rides out a proxy that is not accepting connections** (#986): the proxy is a container LEM restarts
on deploys/reboots, and the SDK's own retries were spent in ~1.5s, so one blip used to lose the
generation and file a defect for it. ONLY a connection that was never established is retried
(`LLM_CONNECT_RETRY_ATTEMPTS` / `LLM_CONNECT_RETRY_BACKOFF_SECONDS`, ~24s default) — nothing was sent,
so there's no spend to duplicate; a timeout, 4xx, or 5xx is the proxy answering and fails as before.

**Model tier aliases** (defined in `.litellm/config.yaml`):

| Alias | Use case |
|---|---|
| `lem-simple` | Short outputs ≤300 chars: refine, summarize briefly, comma list |
| `lem-medium` | Balanced: comments, post refinement, blog summaries |
| `lem-complex` | Long-form: thought leadership, personal story, industry news |
| `lem-image` | Image generation (gpt-image-2, gpt-image-1 in-group fallback) |
| `lem-vision` | Render quality gate — looks at a generated image (gpt-4o-mini) |
| `lem-embedding` | Embeddings for feedback dedup/clustering (`client.embeddings.create`) |
| `lem-router` | Auto-routes by prompt complexity via `LEMComplexityRouter` |

**Cost-aware down-routing** (`utilities/routing_policy.py`, `utilities/cost_routing.py`): the router can additionally route a tier ONE step down for the treatment cohort of an active cost/quality experiment. `routing_policy.py` is the shared decision core — the app imports it, and docker-compose mounts that same file into the LiteLLM container — so it must stay **stdlib-only** (no `cqc_lem.*` imports). Off unless BOTH `COST_ROUTING_ENABLED` and `COST_AWARE_ROUTING_ENABLED` are set. Since #652 the treatment cohort comes from a PostHog experiment flag resolved app-side, handed to the router via the policy document's `arms` map (hash stays as fallback) — see `docs/experiments.md`, `docs/cost-performance-margin-plan.md` §D.1.1.

See `ai_helper.py` for the per-function model assignment.

**Image stack (ONE engine, two modules):** `utilities/ai/image_brief.py` authors every image
prompt — a validated `lem-medium` JSON brief (render prompt + `focal_concept`) with per-surface
presets (`newsletter`/`post_image`/`carousel`/`video`/`thumbnail`) and a deterministic fallback;
never add a per-content-type prompt helper, add a preset. `utilities/ai/image_gen.py` renders it —
gpt-image via `lem-image` first, FLUX/Replicate fallback (`IMAGE_BACKEND`), cost-tracked via
`track_media_cost` — and `render_image_gated` adds the `lem-vision` quality check with bounded
regenerates (`IMAGE_GATE_MAX_ATTEMPTS`, fails OPEN). Avatar likeness NEVER renders in `image_gen` —
`generate_post_image` (ai_helper) owns the LoRA path behind `avatar/guardrails.resolve_avatar_for`;
newsletter covers add a fail-closed relevance classifier on the Auto path (`newsletter_cover.py`).
NO text/logos in any render prompt — enforced in the brief engine's system prompt.

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
- **Cadence (issue #621):** the plan is NOT one post a day — it fills `posts_per_week` slots (2–7, default 3) of a fixed day-type calendar (`POST_DAY_TYPES`) that also sets each post's buyer stage and archetype family. `posting_days` (#581, default Mon–Fri) is the separate, harder bound on WHICH days may carry a slot — weekends are opt-in. Full posture: `docs/content-scheduling.md`.
- Self-healing carousels (stale/errored carousels re-generated into branded slides) and asset backfill.
- **Newsletter cover images** (`utilities/newsletter_cover.py`, #893): the ONE place a cover is validated, stored, and generated. Two sources, NOT symmetric — an **upload** is the author's own artwork so it lands `approved`; a **generated** cover always lands `pending_review` (a public brand asset). `_approved_cover_path` (run_automation) is the ONLY thing deciding a cover may reach LinkedIn. Generation is opt-in (`cover_image_auto`, off by default) and reuses the SHARED image path, never a parallel helper. Attaching is best-effort: `STEP_COVER` is never a graded editor step. Full posture: `docs/newsletter-covers.md`.
- **Newsletter blog alignment** (`utilities/blog_source.py`, #967): `resolve_blog_source` is the ONE place the `align_with_blog` toggle (default ON) becomes actual source text — blog URL first, sitemap fallback, through the SAME fetchers `blog_summary`/`website_content` use. Best-effort, never blocking: unset/unreachable/empty returns `None` and the edition writes from topic + profile as before. Resolved PER edition, so a batch of queued drafts repurposes DIFFERENT articles. ON with nothing configured is an expected no-op (DEBUG) — only a configured source reading back empty warns.
- `PostType` is `text` / `carousel` / `video`; `PostStatus` includes `error` for generation/posting failures needing manual fix.

### Engagement automation (`app/run_automation.py`) — full posture for every bullet below: `docs/engagement-automation.md`
- **Feed commenting** rebuilt for LinkedIn's SDUI: resilient `find_first`/`click_first`/`find_all_first` selectors (`utilities/linkedin/helper.py`); inline compose+submit; **recency-dominant scoring matrix** (`_score_feed_post`); `_switch_feed_to_recent` sort control; targeting + per-day caps + voice/tone. Runs pre-post (~15 min before) and daily at a golden hour. **Best-effort but never silent (#817):** the matrix is recency-DOMINANT, so `_switch_feed_to_recent`'s returned state (`recent`/`top`/`missing`/`unknown`/`n/a`) rides onto the feed funnel + `feed_scan` event — an unsorted scan must never read as recency-sorted. Group feed has no control, so a miss there is `n/a`/DEBUG. Live grounding: `linkedin_live_validation.py --feed-sort`.
- **Replies** to comments on the user's own posts (`automate_reply_commenting`); **seed a first comment** on own posts (`auto_seed_comment_on_post`).
- **Golden-hour presence** (`utilities/golden_hour.py`, #622): ONE `golden_hour_report` per swept post, measured off REAL publish time from the POST log, never `scheduled_time` — unmeasured is never on-time. **Second wave**: ONE self-comment 6–8h later (`auto_second_wave_comment`) that must ADD substance (#617 contract + similarity gate + slop lint); seed + second wave can never stack (`SELF_COMMENT_MAX_PER_POST=2`).
- **DM conversation auto-nurture** (`_nurture_after_reply`, `utilities/ai/dm_nurture.py`): a reply used to END a sequence — now it's classified and becomes an **approval-gated** next message (`pending` row, `source='nurture'`), one open draft per thread; explicit disinterest stops the thread for good.
- **Reciprocity tracking** via the `post_engagers` table — boosts commenting back on people who engaged with us (`get_recent_engagers`).
- **DMs**: appreciation (connection / recommendation / collaboration), profile-viewer outreach, and **multi-touch follow-up sequences** — all templated and voice-aligned (`build_dm_from_template`, `dm_templates`, `dm_followups`, `process_user_followups`).
- **Appreciation sources** (#968): recommendation + collaboration read STANDING lists (Recommendations Received; the mentions feed), not event queues, so an **undated card is SKIPPED** and only `APPRECIATION_LOOKBACK_DAYS` (30) counts. `appreciation_touches` is the durable CLAIM (checked before write, claimed before send) that stops double-thanking on the ~60s re-queue; `_appreciation_dm_budget` (SHARED cap + #626 envelope) stops mass-thanking at once — unaffordable claims stay UNCLAIMED. Cards rendering with NO dated one is the one thing that warns. OFF until live-grounded (`APPRECIATION_SOURCES_ENABLED`).
- **Message-thread resolution ladder** (`utilities/linkedin/message_thread.py`, #731): `open_message_thread` walks SIX routes, a route counts only when the thread is **provably open** — class names never keyed on, every locator is href/aria-label/TEXT. Verdict is **three-valued** (`ThreadState.REPLIED`/`NOT_REPLIED`/`UNKNOWN`); UNKNOWN SKIPS (a missed follow-up recovers, a follow-up to a reply doesn't). Self-name is a required setting (`users.linkedin_display_name`), never scraped.
- **Owned-asset CTA loop** (`resolve_artifact_delivery` in `content_alignment.py` + `_queue_artifact_delivery`, #624): the ONE map from a CTA to its asset, naming the CHANNEL — **lead magnet** is the comment-keyword mechanic whose payload is a DM; **newsletter** is a subscribe LINK. Keyword delivery is **approval-gated** (`pending` `scheduled_dms` row, `source='artifact'`).
- **Human pacing** (`utilities/human_pacing.py`, #626): the ONE place cadence is decided — read-time delay, dispatch jitter, a per-day-cap draw, all seeded on (user, action, date) and persisted in Redis so a retry never re-rolls. Fails open (no Redis / `HUMAN_PACING_ENABLED=false`). Pacing only slows us down; the 429 breaker in `rate_limit.py` is the separate, harder gate.
- **Comment outcome tracking** (`sweep_comment_outcomes` + `utilities/comment_outcomes.py`, #628): read-only T+24h sweep writes ONE `comment_outcomes` row per comment — replies, likes, and a **three-valued** `visible_most_relevant` (1 relevant / 0 demoted / NULL unreadable, excluded from the denominator). Demotion rate over threshold **holds that user's feed commenting** and escalates CRITICAL.
- **Suppression tripwire** (`auto_suppression_tripwire` + `utilities/suppression.py`, #629): 2026 LinkedIn penalties are SILENT (reach step-collapses, no notification). A daily beat compares impressions-per-post against the user's OWN trailing 14-day median; a sustained drop `pause_automation()`s **engagement only** (posting is never gated) and escalates CRITICAL. Cold start / thin baseline = `unknown`, never actioned. Recovery is human (`POST /user/automation-resume`).
- **Weekly group post** (`auto_draft_group_post` → `auto_post_to_group`, #932): TWO beats with a review window between them. `auto_draft_group_post` (Sun) writes the text — no browser, cached profile — and `auto_post_to_group` (Tue) publishes that draft, generating NOTHING, so **a run with no READY draft publishes nothing**. Resting status is `ready` so silence ships it; the SPA (`GroupsCard`) is where a user rewrites or skips it. ONE open draft per user, carried forward, never replaced.
- **Roster targets LEM can't comment on** (`comment_on_roster_posts` + `auto_follow_roster_target`, #962): posts but ZERO commentable cards records a blocked visit; a whole roster blocked (`_card_for_textbox` drift) records nothing. Auto-follow is opt-in and OFF (`roster_auto_follow`), draws `ACTION_FOLLOW`/`max_follows_per_day` bounded by (never in) the account envelope, and clicks NOTHING unless the control names the page owner.
- **Stale-invite withdrawal** (`utilities/linkedin/stale_invites.py`, #969): the 02:00 beat was a `not_implemented` stub that LOOKED operational. Withdrawing is ONE-WAY (no re-invite for ~3 weeks), so reads fail CLOSED — an unreadable "Sent … ago" is NEVER stale, only the row's OWN `Sent` line is parsed. OFF until `STALE_INVITE_WITHDRAWAL_ENABLED` (ground: `linkedin_live_validation --sent-invites`). `plan_withdrawals` decides the allowance BEFORE Chrome opens, bounded by (never in) the envelope; spend is counted on the CLICK.
- **Company-page invitations** (`utilities/linkedin/company_page_inviter.py`, #732): a paced DAILY drip bounded by the SMALLEST of three ceilings — per-day cap, credit spread (`credits_remaining / days_left_in_month`), live credit count. `plan_daily_invites` decides all of that BEFORE a Chrome session opens — most days the allowance is zero.

### Engagement configuration (`engagement_preferences` table, API in `api/main.py`, SPA in `ui/.../Account.tsx`)
- Targeting: include/exclude topics/keywords/authors, `min_reactions`, `max_post_age_hours`, plus LLM topic-relevance scoring.
- Voice: tone, `comment_length` (short/medium/long; default short), style, emoji/hashtag toggles.
- Caps: `max_comments_per_day`, `max_dms_per_day`; DM template editor with follow-up steps; Login Location (city/state geocoding via `utilities/geocoding.py`, with admin override).

### Marketing video tutorials (`utilities/marketing/video_tutorials.py`, beat `produce-feature-tutorial`)
- One declarative `TutorialFlow` per feature (routes + CSS anchors proving the screen rendered) → headless SPA capture via `get_docker_driver()` → grounded script (`lem-medium`) → TTS (OpenAI `lem-tts` default, ElevenLabs behind `TUTORIAL_TTS_PROVIDER`) → ffmpeg MP4 with branded intro/outro + `.srt` → 9:16 clip → YouTube Data API v3 upload.
- **Fail-closed**: a missing UI anchor, an unparseable script, profanity, an over-cap narration, or a fabricated number aborts BEFORE any TTS/publish spend. Cost is attributed per part and totalled on the manifest record.
- State lives in `assets/videos/tutorials/manifest.json`; the SPA embeds it via `TutorialVideos.tsx`. Weekly cadence; a flow is re-filmed only when its captured UI fingerprint changes. OFF unless `TUTORIAL_VIDEOS_ENABLED`.
- **YouTube OAuth token** (`youtube_auth.py`, #742): the ONE place its state is decided. Read DB-first (`app_credentials`, installed via `POST /admin/youtube-token` — no deploy), `YOUTUBE_REFRESH_TOKEN` seeds it. `unknown` (Google unreachable) is NOT `needs_reauth` (4xx / lost scope — the only state that alerts). Weekly beat `youtube-token-check` IS the keep-alive vs the 6-month-disuse expiry — never drop it while the feature is off. Full posture: `docs/youtube-publishing.md`.

### Anti-bot / session infra
- Per-user static residential proxy (`utilities/proxy.py`) + an in-memory **MV3 proxy-auth extension** (`_build_proxy_auth_extension_b64` in `selenium_util.py`) — never URL-embedded credentials, since MV2 background pages are disabled in Chrome 149+.
- Cookie persistence (`li_at` is the DEFAULT engagement login since #745) + an email-PIN verification flow (`utilities/linkedin/verification_pin.py`).
- **Sign-in visibility** (`utilities/linkedin/login_status.py`, #933): the device-approval challenge is approved on LinkedIn itself, so email was the ONLY place a user could confirm LEM got it. `_persist_session_cookies` is where both login paths meet, so it's where a sign-in is recorded (`mark_signed_in`); the wait loop always closes `approval_pending` — `approval_timed_out` on giving up, `mark_signed_in` the moment it clears (a login that dies before the cookie persist must not keep asking for a tap already given). Redis-backed, fails open: `unknown` means nothing recorded, NOT a broken connection. Read via `GET /user/linkedin-signin-status` → `LinkedInSignInStatusCard.tsx`.
- **OAuth token renewal** (`utilities/linkedin/token_refresh.py`, #600): `resolve_token_status` is the ONE place token state is decided — SPA countdown and renewal beat read the same function. LinkedIn caps auth at 60 days, so a **rolling refresh is the only way it outlives that**: daily beat `refresh-linkedin-tokens` (08:30) renews everyone inside `EXPIRY_WARNING_DAYS`, where before only an SPA page view ever did. "No refresh token" is an expected no-op (DEBUG); those users get a throttled reconnect email (`LINKEDIN_TOKEN_EMAIL_THROTTLE_DAYS`, Redis, fails open). `days_remaining` is `None`, never 0, when unreadable.
- 429 / auth-wall backoff and resilience (`utilities/linkedin/rate_limit.py`).
- **Secrets at rest** (`utilities/crypto.py`, #745): `li_at`, OAuth tokens and the stored password are AES-256-GCM envelopes keyed per user+column off `LEM_SECRET_KEY`; `db.py` is the ONE caller and the field-name constants are AAD — renaming one orphans every row. Reads dual-mode until `ENCRYPTION_REQUIRED`; failed decrypt → None. Daily `auto_encrypt_secrets_at_rest` backfills AND rotates. Full: `docs/secrets-at-rest.md`.
- **LEM identity + sessions** (#745 phase 2b): `users.public_uid` is the identity; email is a movable ATTRIBUTE (`change_user_email`, PIN to the NEW address, other sessions revoked). `sessions.session_token` stores `SHA-256(token)` — UNKEYED, so a rotated `LEM_SECRET_KEY` never logs everyone out — in an **httpOnly** cookie. `api/main.get_session_user_id()` is the ONE resolver: an explicit token that RESOLVES wins, else the cookie; the SPA sends sentinel `'cookie'`. **Since #914 EVERY `/api` route resolves its caller through it** — `require_session_user_id()` is it plus a 401; an `email`/`user_id`/`post_id` is a TARGET to authorise (403 + audited `foreign_target_denied`), not the actor; `db.user_owns_posts` FAILS CLOSED; a DB fault is **503** not 403. **CSRF (#957):** a cookie-authenticated `/api` write must send `X-LEM-Client` — 403 `client_header_required`. The shared bearer (`API_ACCESS_TOKENS`) is a NON-BROWSER credential since #950 — never in the SPA build. Sessions are per-device revocable. Auth limiting is TWO layers: Redis windows (fails open) + the durable PIN lockout in `verify_pin_for_email`; all lands in `auth_audit_log`. Full: `docs/identity-and-sessions.md`.
- **Strong auth + step-up** (#745 phase 2c): `utilities/auth_factors.py` is the ONE place factor state is decided (`webauthn_util.py` holds the ceremonies). Once an account enrols a passkey or TOTP the email PIN is a **bootstrap** only. A **passkey** login is the only phishing-resistant path arriving already stepped up; a **recovery code** never does. `sessions.last_verified_at` gates every credential-touching write — refusal is **403 `step_up_required`**, never 401. **The FIRST factor is free, every one after it is gated, removing one always is**; a `scope='recovery'` session may enrol but is never stamped for it. A pending second-factor handle is burned by the `SECOND_FACTOR_MAX_ATTEMPTS`-th wrong code, counted **per account**: **401 = wrong code, retry; 400 = handle gone; 429 = budget spent**. The extension steps up once in the SPA at `/user/extension-token` for a `scope='extension'` session. `STRONG_AUTH_ENABLED=false` reverts to 2b. Full: `docs/strong-authentication.md`.
- **Session scopes are SURFACES** (#905, 2c.1): the same resolver enforces scope everywhere. `extension` reaches only the ONE path the extension calls (else 403 + audited `session_scope_denied`); `enroll` — a PIN login past `REQUIRE_STRONG_FACTOR_AFTER` on a factor-less account — reaches only enrolment, which promotes it to `full`. **A hold is never a lockout:** the PIN still signs you in, and every read goes through `enrollment_hold_active()`. Empty date (the default) = 2c behaviour exactly.

## Testing Standards

- All new/modified code: ≥80% patch coverage enforced by Codecov.
- Three lanes: **unit** (`tests/unit/`, mock ALL I/O — fixtures `mock_openai_client` / `mock_database_connection` / `mock_selenium_driver`, plus hermetic autouse guards), **integration** (real MySQL + Redis containers), **e2e** (`selenium/standalone-chrome`).
- Run: `poetry run pytest tests/unit -v --tb=short`; coverage: `poetry run pytest --cov=src/cqc_lem --cov-report=xml`.
- Markers, fixtures, and lane selection: the **test-lanes** skill and `tests/README.md`.

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
`generate_newsletter_edition`, `generate_ai_response` — supersedes `attribute_llm_cost`), `@llm_step`
on the SHARED-core step functions (never at a call site). The client sends ids two ways since
LiteLLM reads them from two places — trace id in the `x-litellm-trace-id` HEADER, parent span in
`metadata.parent_run_id`. Nested trace = span; span with no trace = no-op;
`LLM_TRACING_ENABLED=false` mints nothing. Full posture: `docs/llm-analytics.md`.

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
it → customer never billed). **The suite scores a FIRST draft, production ships an n-th** (#910), so
every check is classed by what production does with its failure: `contract` (the call site consumes
it — the ABSOLUTE floor) vs `repairable` (a regeneration gate retries it — advisory, still
champion-relative). A case whose `max_tokens` mirrors a call site (`budget_mirrors_production`) is
never retried at a bigger budget; an EMPTY completion is re-measured at the same one. Same principle
at RUN scope (#923): a run where every case of every model errored — champion included — is a harness
outage, not a scorecard of zeros, and is REFUSED (no report, exit 1); a partial failure still
publishes with an `Unmeasured cases` count. Only `recommend` becomes a swap; recommendations are
RENDERED, never written (`.litellm/model_upgrades.yaml` is the retirement map). Full posture:
`docs/model-benchmarks/README.md`.

### Content-quality telemetry (issue #630)
`auto_nightly_content_quality` is the TREND LINE (other gates are one-time verdicts). Scores
posts/comments/editions into `content_quality_scores`: weighted slop (HARD ×3), self-similarity,
**stored** authenticity (no fresh judge call), hook length, impression-weighted ER. **Unscored is
never zero** — each dimension has its own sample size. Never pauses (drift → go look at prompts;
safety is #629). Similarity batches into ONE `lem-embedding` per surface; dominant measure only.

### Feature flags (issue #651)
`utilities/flags.py` is the ONE place; **fail open to env var** (no key, disabled, undefined,
inconclusive, SDK raises → all return the flag's env var). `only_evaluate_locally=True` → ZERO
network per check, flip lands without restart. Flags must use rollout-% / distinct-ID only. Read at
CALL SITE, never at import. **Safety controls are NOT flags** (429 breaker, holds, pauses, per-day
caps stay in Redis/env). SPA bootstraps from `GET /api/flags` — not through posthog-js. Full
posture: `docs/feature-flags.md`.

### Surveys — NPS/CSAT (issue #653)
TWO owners. PostHog Surveys: NPS (30d past ACTIVATION, not signup) + post-quality CSAT (on
`post_approved` once `posts_approved >= 5`). `utilities/surveys.py` keeps the bespoke ones:
trial-T-3d review (#499, PostHog can't unlock) + fix CSAT (#502). Type **`api`**, rendered headless
in `PostHogSurveyModal.tsx`. ONE answer = TWO paths (browser native `$survey_response` + POST
`/api/survey/posthog` → `feedback` row), counted ONCE — `track_survey_response` deliberately NOT
emitted. Detractor (NPS ≤6 / CSAT ≤2) or any free text stays `new`; happy+blank → `resolved`.
`markSurveySeen()` advances the 30d wait — drop it and the throttle silently stops. Full posture:
`docs/surveys.md`.

### Endpoints panel + release annotations (issue #654)
**Endpoints** (PostHog beta): HogQL as a versioned cached HTTP route — Dashboard "Live stats"
without a MySQL reporting layer. Every query scoped with `distinct_id = {variables.distinct_id}`
(ONE shared project → un-scoped leaks across customers). Resolves against ONE `InsightVariable`;
endpoint is `blocked_endpoint` until it exists. `GET /user/posthog-stats` server-side only; personal
API key never reaches browser. Failure modes → `available: false` for that panel.
**Release annotations**: `scripts/posthog_annotate.py` posts `"vX.Y.Z deployed"` per deploy; needs
`POSTHOG_PERSONAL_API_KEY` GH secret; absent/outage → no-op, never a failed release.

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

- The stack launches with **both** compose files (`-f docker-compose.yml -f docker-compose.prod.yml`) — the prod overlay strips the dev bind-mount, so every app service runs the code baked into the image; editing files on disk does nothing until a new image ships. `web_app` is a tiny nginx **edge** routing to the active blue/green color; deploys are zero-downtime flips, releases batch 4x daily (05/11/17/23 UTC) — see `docs/zero-downtime-deploys.md`. A **`release:now`** label on a PR ships it at merge instead of the next window; agents may self-apply it for high-priority/user-visible fixes — policy in `docs/release-fast-lane.md`.
- **Runtime state (429 breaker, manual automation pause, reply-sweep cadence keys) lives in Redis**, not the DB or containers — it survives deploys.
- A **local hotfix deploy** fallback exists for when CI/release is too slow or blocked, and diverges prod from `main` until the fix lands via the normal PR flow. Full posture + compose-layering + image-ref details: `docs/DEPLOYMENT.md`.

## Known Gotchas

- `get_docker_driver()` previously connected to Selenium Grid hub+node. It now connects to `selenium/standalone-chrome:latest` at port 4444.
- `ai_helper.py` had all functions hardcoded to `model="gpt-4o-mini"` — they now use tier aliases.
- PostHog replaces Prometheus + Jaeger (both removed from docker-compose).
- `linkedin-preview` service (external) was removed — preview is now the native `LinkedInPostPreview.tsx` component.
- **LinkedIn SDUI:** the old `urn:`, `feed-shared-*`, and `comments-comment-*` DOM anchors are gone — prefer `data-testid` / `aria-label` selectors. The comment composer has NO `<form>`; the global nav is sticky and steals clicks from an unfocused composer; every composer lookup is scoped to its OWN post (`_post_composer_for_card` / `_reply_composer_for_comment`), and a miss is a DEBUG no-op, never a warning. Full posture: `docs/sdui-selenium-notes.md`.
- **Emoji in Selenium:** ChromeDriver `send_keys` throws on non-BMP emoji — strip them before typing with `_strip_non_bmp()`.
- **ENUM columns:** `logs.action_type` (and other status columns) are MySQL ENUMs — adding a value requires a migration. New migrations use **TIMESTAMP** versions so two branches never collide; the **db-migration** skill and `compose/local/database/migrations/README.md`.
- **Unified content core:** newsletters, posts, AND comments draw framework, research, and alignment from `utilities/ai/content_framework.py` / `content_research.py` / `content_alignment.py` — never add parallel per-content-type prompt helpers. Comments carry a **quality contract + similarity gate** (#617) that SKIPS the post after `COMMENT_GATE_MAX_ATTEMPTS` failed regenerations. **Story bank** (#620) is the FACT half; **deck reference gate** (#728) is the save-worthiness half; **deterministic slop lint** (#625, `slop_lint.py`) BLOCKS on five HARD checks, advises on four WARN checks. Full posture: `docs/content-core.md`.
- **Content mix (70/20/10) governor:** every planned post carries a mix class in `posts.content_mix` — `value` (70%) / `authority` (20%) / `promo` (10%, forced `case_snapshot` blueprint). **Promo CTAs are always an ARTIFACT** (lead magnet / newsletter) — a meeting ask is banned in prompts and repaired deterministically; any that survives HOLDS the post at PENDING via the `meeting_cta` gate. Full posture: `docs/content-core.md`.
- **Stale lazy chunks after a deploy (#743):** a browser tab open across a release fetches a code-split chunk on a hash the new image no longer has (404, reads as "broken" not "reload me"). Three layers: asset **retention** (both colors serve a live-bundle miss from a shared archive volume), a **one-shot reload** on import failure (loop-guarded, skipped offline), and proactive polling of `/api/app-info` that prompts rather than reloads. Full posture: `docs/spa-deploy-freshness.md`.

## Git Safety & Multi-Agent Concurrency Rules
- **Fresh State Enforcement**: Before executing or generating any code edit, you MUST explicitly run `git status` and a file read command (e.g., `cat <filename>`) to verify no hidden or uncommitted upstream modifications exist. Never rely on your internal conversation memory for file contents.
- **Micro-Branching Workflow**: Do not make edits directly on shared branches while working asynchronously. When starting a distinct task, automatically spin up a task-specific feature branch (`git checkout -b feature/claude-<task-name>`).
- **Atomic Commits**: For every completed sub-task or successful implementation block, automatically stage and commit your files with a clean, concise descriptive message (e.g., `git add . && git commit -m "feat(api): implement active sub-agent locking mechanism"`). 
- **Conflict Avoidance**: If you detect changes in the working directory that clash with your active target files, immediately halt, stash your progress (`git stash`), pull down the current state, and safely resolve the differences before re-applying your changes.
- **Branch cleanup.** Merged feature branches auto-delete (repo setting `delete_branch_on_merge=true`); orphans swept weekly by `.github/workflows/stale-branches.yml`. A 48-hour grace window protects active agent work. Full posture: `docs/branch-cleanup.md`.
