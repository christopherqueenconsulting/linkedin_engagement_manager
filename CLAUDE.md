# LinkedIn Engagement Manager — Claude Code Context

## Project Overview

LinkedIn Engagement Manager (LEM) automates LinkedIn engagement end to end: Selenium-based scraping and feed interaction, AI-generated content (via LiteLLM proxy routing to OpenAI / Claude / Ollama / OpenRouter), Celery task queue, React SPA frontend, MySQL persistence, and FastAPI backend.

Two pillars:
- **Content generation & scheduling** — a 30-day plan of buyer-journey posts auto-scheduled around peak/golden hours, with sentiment checks and preview/approval.
- **Engagement automation** — feed commenting, replies, seed comments, appreciation/outreach DMs with follow-ups, and a throttled company-page invite drip — driven by per-user targeting, voice/tone, per-day caps.

Code paths in **Feature Areas** below. Subsections carry `docs/*.md` pointers holding the full posture — CLAUDE.md is the map (locations, symbols, constants, invariants, where to find the detail).

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
  shim). Pass context as keyword args (`user_id`, `post_id`, `task_name`, `action_type`, …);
  `log_error`/`log_critical` take `exc=`. **Once is a warning, repeatedly is a defect:** a repeated
  `log_warning` re-emits at ERROR and files ONE grouped `$exception`, so never warn on an expected
  no-op — log those DEBUG. Level table + escalation contract:
  **`src/cqc_lem/utilities/CLAUDE.md`** (auto-loads in that tree) and `docs/error-tracking.md`.
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
**rides out a proxy that is not accepting connections** (#986; the proxy is a container LEM restarts
on deploys): ONLY a connection that was never established is retried (`LLM_CONNECT_RETRY_ATTEMPTS` /
`LLM_CONNECT_RETRY_BACKOFF_SECONDS`, ~24s default) — nothing was sent, so there is no spend to
duplicate. A timeout, 4xx or 5xx is the proxy answering, and fails as before.

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

**Cost-aware down-routing** (`utilities/routing_policy.py`, `utilities/cost_routing.py`, `docs/cost-performance-margin-plan.md` §D.1.1): the router can route a tier ONE step down for the treatment cohort of an active cost/quality experiment (arm resolved app-side from a PostHog flag, #652, handed over in the policy document's `arms` map; hash stays as fallback). `routing_policy.py` is the shared decision core — the app imports it AND docker-compose mounts that same file into the LiteLLM container — so it must stay **stdlib-only** (no `cqc_lem.*` imports). Off unless BOTH `COST_ROUTING_ENABLED` and `COST_AWARE_ROUTING_ENABLED` are set.

See `ai_helper.py` for the per-function model assignment.

**Image stack (ONE engine, two modules, `docs/image-stack.md`):** `utilities/ai/image_brief.py`
authors every image prompt — a validated `lem-medium` brief (render prompt + `focal_concept`) with
per-surface presets (`newsletter`/`post_image`/`carousel`/`video`/`thumbnail`); never add a
per-content-type prompt helper, add a preset. `utilities/ai/image_gen.py` renders it
(`IMAGE_BACKEND`, cost-tracked); `render_image_gated` adds the bounded `lem-vision` check, failing
OPEN. Avatar likeness NEVER renders in `image_gen` — `generate_post_image` (ai_helper) owns the LoRA
path behind `avatar/guardrails.resolve_avatar_for`. NO text/logos in a render prompt.
`utilities/post_image.py` (#1030) is the ONE place a POST's image is validated, stored and removed —
upload OR the studio's "Generate with AI", the SAME engine the scheduled path uses. A compose-time
`image_url` is CALLER input: `/schedule_post/` takes it only when `owns_post_image_url` says it's a
preview we issued to that caller, and a stored URL never resolves outside `assets_dir`.

## Selenium Pattern

Always use `get_docker_driver()` from `selenium_util.py`. It connects to `selenium-chrome:4444`, polls readiness, and sets 1920×1080. Never instantiate `webdriver.Chrome()` directly.

Use `click_element_wait_retry()` for all click interactions — it handles transient DOM timing issues.

Browser capacity is a **fixed pool of Chrome session slots shared by the Celery Selenium lanes**:
`SE_NODE_MAX_SESSIONS` must always equal the summed `SELENIUM_CONCURRENCY` of those lanes —
`tests/unit/app/test_selenium_capacity.py` fails the build if they drift. The horizontal path
(`docker-compose.grid.yml`) carries the same invariant with node count as the cap:
`docs/SELENIUM_GRID.md`, `docs/scaling-plan.md`.

## Feature Areas

### Content generation & scheduling (`app/run_content_plan.py`, `app/run_scheduler.py`, `utilities/ai/ai_helper.py`)
- AI content by buyer-journey stage (awareness / consideration / decision): thought-leadership, industry-news commentary, personal-story, engagement-prompt posts, carousels (educational / case-study / product-demo / insights), native video, blog summaries — a 30-day plan with balanced post-type distribution, auto-scheduled around golden/peak hours.
- **Cadence (#621, `docs/content-scheduling.md`):** the plan is NOT one post a day — it fills `posts_per_week` slots (2–7, default 3) of the fixed `POST_DAY_TYPES` calendar, which also sets each post's buyer stage and archetype family. `posting_days` (#581, default Mon–Fri) is the separate, harder bound on WHICH days may carry a slot — weekends are opt-in.
- Self-healing carousels (stale/errored carousels re-generated into branded slides) and asset backfill.
- **Newsletter cover images** (`utilities/newsletter_cover.py`, #893, `docs/newsletter-covers.md`): the ONE place a cover is validated, stored, and generated. An **upload** is the author's own artwork so it lands `approved`; a **generated** cover always lands `pending_review` (a public brand asset). `_approved_cover_path` (run_automation) is the ONLY thing deciding a cover may reach LinkedIn. Opt-in (`cover_image_auto`, off), SHARED image path, best-effort — `STEP_COVER` is never a graded editor step.
- **Newsletter blog alignment** (`utilities/blog_source.py`, #967, `docs/content-core.md`): `resolve_blog_source` is the ONE place the `align_with_blog` toggle (default ON) becomes source text — blog URL first, sitemap fallback, SAME fetchers `blog_summary`/`website_content` use. Never blocking: nothing readable → `None` and the edition writes from topic + profile. Resolved PER edition, so queued drafts repurpose DIFFERENT articles. ON with nothing configured is an expected no-op (DEBUG).
- `PostType` is `text` / `carousel` / `video`; `PostStatus` includes `error` for generation/posting failures needing manual fix.

### Engagement automation (`app/run_automation.py`) — full posture for every bullet below: `docs/engagement-automation.md`
- **Feed commenting** on LinkedIn's SDUI: resilient `find_first`/`click_first`/`find_all_first` selectors (`utilities/linkedin/helper.py`), inline compose+submit, **recency-dominant `_score_feed_post`**, targeting + per-day caps + voice/tone. Runs pre-post (~15 min before) and daily at a golden hour. **Never silent (#817):** `_switch_feed_to_recent` reports the run's sort state (`recent`/`top`/`missing`/`unknown`/`n/a`) onto the feed funnel + `feed_scan` — an unsorted scan must never read as recency-sorted.
- **Replies** to comments on the user's own posts (`automate_reply_commenting`); **seed a first comment** on own posts (`auto_seed_comment_on_post`).
- **Golden-hour presence** (`utilities/golden_hour.py`, #622): ONE `golden_hour_report` per swept post, measured off REAL publish time from the POST log, never `scheduled_time` — unmeasured is never on-time. **Second wave**: ONE self-comment 6–8h later (`auto_second_wave_comment`) that must ADD substance (#617 contract + similarity gate + slop lint); seed + second wave never stack (`SELF_COMMENT_MAX_PER_POST=2`).
- **Human pacing** (`utilities/human_pacing.py`, #626): the ONE place cadence is decided — read-time delay, dispatch jitter, per-day-cap draw, all seeded on (user, action, date) and persisted in Redis so a retry never re-rolls. Fails open. Pacing only slows us down; the 429 breaker in `rate_limit.py` is the separate, harder gate.
- **DM conversation auto-nurture** (`_nurture_after_reply`, `utilities/ai/dm_nurture.py`): a reply becomes an **approval-gated** next message (`pending` row, `source='nurture'`), one open draft per thread; explicit disinterest stops the thread for good.
- **Reciprocity tracking** via the `post_engagers` table — boosts commenting back on people who engaged with us (`get_recent_engagers`).
- **DMs**: appreciation (connection / recommendation / collaboration), profile-viewer outreach, **multi-touch follow-up sequences** — templated and voice-aligned (`build_dm_from_template`, `dm_templates`, `dm_followups`, `process_user_followups`).
- **Appreciation sources** (#968): recommendation + collaboration read STANDING lists, not event queues, so an **undated card is SKIPPED** and only `APPRECIATION_LOOKBACK_DAYS` (30) counts. `appreciation_touches` is the durable CLAIM against double-thanking on the ~60s re-queue; `_appreciation_dm_budget` (SHARED cap + #626 envelope) bounds it. OFF until live-grounded (`APPRECIATION_SOURCES_ENABLED`).
- **Message-thread ladder** (`utilities/linkedin/message_thread.py`, #731): `open_message_thread` walks SIX routes; a route counts only when the thread is **provably open** — never class names, only href/aria-label/TEXT. `ThreadState` is **three-valued** (`REPLIED`/`NOT_REPLIED`/`UNKNOWN`); UNKNOWN SKIPS. Self-name is a setting (`users.linkedin_display_name`), never scraped. **Reading a thread and SENDING into one are different questions** (#1030): `send_dm_now` goes through `open_addressed_composer`, which NAVIGATES rather than clicks (a wrong thread costs a read a verdict, a send a stranger's inbox) and refuses unless `composer_recipient` names someone — `compose_url_for` must carry **`recipient=` as well as `profileUrn=`**, or the composer opens addressed to nobody. **Sent means the message LANDED** (`_dm_send_landed`), never that Send took a click.
- **Owned-asset CTA loop** (`resolve_artifact_delivery` in `content_alignment.py` + `_queue_artifact_delivery`, #624): the ONE map from a CTA to its asset, naming the CHANNEL — **lead magnet** is a comment-keyword mechanic paying out a DM, **newsletter** a subscribe LINK. Keyword delivery is **approval-gated** (`pending` `scheduled_dms`, `source='artifact'`).
- **Comment outcome tracking** (`sweep_comment_outcomes` + `utilities/comment_outcomes.py`, #628): read-only T+24h sweep, ONE `comment_outcomes` row per comment — replies, likes, **three-valued** `visible_most_relevant` (1 relevant / 0 demoted / NULL unreadable, excluded from the denominator). Demotion over threshold **holds that user's feed commenting** + CRITICAL.
- **Suppression tripwire** (`auto_suppression_tripwire` + `utilities/suppression.py`, #629): 2026 LinkedIn penalties are SILENT, so a daily beat compares impressions-per-post against the user's OWN trailing 14-day median; a sustained drop `pause_automation()`s **engagement only** (posting is never gated) + CRITICAL. Thin baseline = `unknown`, never actioned. Recovery is human (`POST /user/automation-resume`).
- **Weekly group post** (`auto_draft_group_post` → `auto_post_to_group`, #932): TWO beats with a review window between — Sun writes the text (no browser), Tue publishes that draft and generates NOTHING, so **a run with no READY draft publishes nothing**. Resting status is `ready` so silence ships it; ONE open draft per user, carried forward, never replaced.
- **Roster targets LEM can't comment on** (`comment_on_roster_posts` + `auto_follow_roster_target`, #962): posts but ZERO commentable cards records a blocked visit; a whole roster blocked (`_card_for_textbox` drift) records nothing. Auto-follow is opt-in/OFF (`roster_auto_follow`), draws `ACTION_FOLLOW`/`max_follows_per_day` bounded by (never in) the account envelope, and clicks NOTHING unless the control names the page owner.
- **Roster connect escalation** (`advance_roster_connect`, #979): blocked → follow → STILL blocked → `needs_connection` → (opt-in `roster_auto_connect`, OFF) ONE invite. `needs_connection` needs EVIDENCE (`following` + a blocked visit AFTER `followed_at`); a landed comment stands it back down. The invite rides the EXISTING rail (`send_roster_connect_invite` → `invite_to_connect_now`), spends the SHARED `max_invites_per_day` at ≤ `ceil(remaining/3)`, ONE shot per target (`requested` written BEFORE dispatch). `ConnectStatus` is the ONE vocabulary.
- **Stale-invite withdrawal** (`utilities/linkedin/stale_invites.py`, #969): withdrawing is ONE-WAY (~3 weeks before a re-invite), so reads fail CLOSED — an unreadable "Sent … ago" is NEVER stale, only the row's OWN `Sent` line is parsed. `plan_withdrawals` decides the allowance BEFORE Chrome opens, bounded by (never in) the envelope. OFF until `STALE_INVITE_WITHDRAWAL_ENABLED`.
- **Company-page invitations** (`utilities/linkedin/company_page_inviter.py`, #732): a paced DAILY drip bounded by the SMALLEST of three ceilings — per-day cap, credit spread (`credits_remaining / days_left_in_month`), live credit count. `plan_daily_invites` decides all of that BEFORE a Chrome session opens.

### Engagement configuration (`engagement_preferences` table, API in `api/main.py`, SPA in `ui/.../Account.tsx`)
- **Targeting:** include/exclude topics/keywords/authors, `min_reactions`, `max_post_age_hours`, plus LLM topic-relevance scoring.
- **Voice:** tone, `comment_length` (short/medium/long; default short), style, emoji/hashtag toggles.
- **Caps:** `max_comments_per_day`, `max_dms_per_day`; DM template editor with follow-up steps; Login Location (city/state geocoding via `utilities/geocoding.py`, admin-overridable).

### Marketing video tutorials (`utilities/marketing/video_tutorials.py`, beat `produce-feature-tutorial`)
- One declarative `TutorialFlow` per feature (routes + CSS anchors proving the screen rendered) → headless SPA capture → grounded script (`lem-medium`) → TTS → ffmpeg MP4 + `.srt` → 9:16 clip → YouTube upload. **Fail-closed, cheapest-first**: a missing anchor, an unparseable script, profanity, an over-cap narration, or a fabricated number aborts BEFORE any TTS/publish spend. Re-filmed only when the captured UI fingerprint changes (`assets/videos/tutorials/manifest.json`). OFF unless `TUTORIAL_VIDEOS_ENABLED`. Full: `docs/marketing-video-tutorials.md`.
- **YouTube OAuth token** (`youtube_auth.py`, #742): the ONE place its state is decided. Read DB-first (`app_credentials`, installed via `POST /admin/youtube-token` — no deploy), `YOUTUBE_REFRESH_TOKEN` seeds it. `unknown` (Google unreachable) is NOT `needs_reauth` (4xx / lost scope — the only state that alerts). Weekly beat `youtube-token-check` IS the keep-alive vs the 6-month-disuse expiry — never drop it while the feature is off. Full: `docs/youtube-publishing.md`.

### Anti-bot / session infra
- Per-user static residential proxy (`utilities/proxy.py`) + an in-memory **MV3 proxy-auth extension** (`_build_proxy_auth_extension_b64` in `selenium_util.py`) — never URL-embedded credentials, since MV2 background pages are disabled in Chrome 149+.
- Cookie persistence (`li_at` is the DEFAULT engagement login since #745) + an email-PIN verification flow (`utilities/linkedin/verification_pin.py`).
- **Sign-in visibility** (`utilities/linkedin/login_status.py`, #933, `docs/linkedin-session-health.md`): `_persist_session_cookies` is where both login paths meet, so it's where a sign-in is recorded (`mark_signed_in`); the device-approval wait loop always closes `approval_pending` (never keep asking for a tap already given). Redis-backed, fails open: `unknown` means nothing recorded, NOT a broken connection.
- **OAuth token renewal** (`utilities/linkedin/token_refresh.py`, #600, same doc): `resolve_token_status` is the ONE place token state is decided — SPA countdown and renewal beat read the same function. LinkedIn caps auth at 60 days, so the daily beat `refresh-linkedin-tokens` (08:30) renewing everyone inside `EXPIRY_WARNING_DAYS` is the only way a token outlives that. `days_remaining` is `None`, never 0, when unreadable.
- 429 / auth-wall backoff and resilience (`utilities/linkedin/rate_limit.py`).
- **Secrets at rest** (`utilities/crypto.py`, #745): `li_at`, OAuth tokens and the stored password are AES-256-GCM envelopes keyed per user+column off `LEM_SECRET_KEY`; `db.py` is the ONE caller and the field-name constants are AAD — renaming one orphans every row. Reads dual-mode until `ENCRYPTION_REQUIRED`; failed decrypt → None. Daily `auto_encrypt_secrets_at_rest` backfills AND rotates. Full: `docs/secrets-at-rest.md`.
- **LEM identity + sessions** (#745 phase 2b, `docs/identity-and-sessions.md`): `users.public_uid` is the identity; email is a movable ATTRIBUTE (`change_user_email`, PIN to the NEW address, other sessions revoked). `sessions.session_token` stores an UNKEYED `SHA-256(token)` — a rotated `LEM_SECRET_KEY` must never log everyone out — in an **httpOnly** cookie. `api/main.get_session_user_id()` is the ONE resolver (explicit token that RESOLVES wins, else the cookie; the SPA sends sentinel `'cookie'`), and **since #914 EVERY `/api` route resolves its caller through it**: `require_session_user_id()` is it plus a 401; an `email`/`user_id`/`post_id` is a TARGET to authorise (403 + audited `foreign_target_denied`), never the actor; `db.user_owns_posts` FAILS CLOSED; a DB fault is **503**, not 403. **CSRF (#957):** a cookie-authenticated `/api` write must send `X-LEM-Client` (403 `client_header_required`). The shared bearer (`API_ACCESS_TOKENS`) is a NON-BROWSER credential since #950 — never in the SPA build. Auth limiting is Redis windows (fails open) + the durable PIN lockout in `verify_pin_for_email`, all audited in `auth_audit_log`.
- **The docs surface is INSIDE `/api`** (#1020): `/api/docs`, `/api/redoc`, `/api/openapi.json` (old paths 301), re-opened as leaf entries in `_PUBLIC_API_PREFIXES`; `_hide_admin_routes_from_schema()` keeps every `/api/admin/*` operation OUT of the published schema, derived from the route table so a new admin route inherits it. Hidden ≠ gated — their auth is unchanged, Swagger just can't drive them (curl/Postman per `docs/TESTING_ENGAGEMENT_API.md`). The unauthenticated `GET /health/deep` returns COUNTS only, `"status":"healthy"` first — a monitor contract (`docs/stack-watchdog.md`). Mechanism: `docs/identity-and-sessions.md`.
- **Strong auth + step-up** (#745 phase 2c, `docs/strong-authentication.md`): `utilities/auth_factors.py` is the ONE place factor state is decided (`webauthn_util.py` holds the ceremonies). Once an account enrols a passkey or TOTP the email PIN is a **bootstrap** only; a **passkey** login is the only path arriving already stepped up, a **recovery code** never does. `sessions.last_verified_at` gates every credential-touching write — refusal is **403 `step_up_required`**, never 401. **The FIRST factor is free, every one after it is gated, removing one always is.** Second-factor attempts are durable (`auth_challenges.attempts`), counted **per account** over `SECOND_FACTOR_ATTEMPT_WINDOW_MINUTES` and carried into the next handle: **401 = wrong code, retry; 400 = handle gone; 429 = budget spent**. `STRONG_AUTH_ENABLED=false` reverts to 2b.
- **Session scopes are SURFACES** (#905 / #1026, `docs/identity-and-sessions.md`): the same resolver enforces scope everywhere; refusal is 403 + audited `session_scope_denied`. `extension` reaches only the ONE path the extension calls; `enroll` — a PIN login past `REQUIRE_STRONG_FACTOR_AFTER` on a factor-less account — reaches only enrolment, which promotes it to `full` (**a hold is never a lockout:** the PIN still signs you in, every read goes through `enrollment_hold_active()`; empty date = 2c behaviour exactly). **`agent`** is the headless credential — minted once by a human, `_AGENT_SESSION_SURFACE` (queueing) only, TTL fixed at mint (the ONE scope `resolve_session` never slides). It may queue but NEVER approve — THREE guards, because a row reaches APPROVED three ways: `action="approve"`, `status="approved"` at create, the `auto_approve` account default. Surfaces match on PATH not method, so granting a read grants its writes — hence `PUT /user/engagement-preferences` is separately refused (`agent_may_not_configure`).

## Testing Standards

- All new/modified code: ≥80% patch coverage enforced by Codecov.
- Three lanes: **unit** (`tests/unit/`, mock ALL I/O — fixtures `mock_openai_client` / `mock_database_connection` / `mock_selenium_driver`, plus hermetic autouse guards), **integration** (real MySQL + Redis containers), **e2e** (`selenium/standalone-chrome`).
- Run: `poetry run pytest tests/unit -v --tb=short`; coverage: `poetry run pytest --cov=src/cqc_lem --cov-report=xml`.
- Markers, fixtures, and lane selection: the **test-lanes** skill and `tests/README.md`.

## Observability

Track events via `utilities/observability.py` (`track_llm_call` / `track_task` / `track_api_call`).
One row per surface below: the ONE module and the invariant that bites. The paragraph behind each
row is `docs/observability-map.md`; the doc in the last column holds rationale, contracts and edge
cases. Plan with this table, drill in with the doc.

| Surface | The ONE place | The invariant that bites | Doc |
|---|---|---|---|
| **LLM analytics** (#647, traces #746) | `utilities/ai/client.py` (+ `@llm_pipeline` / `@llm_step`) | `llm_call` (app estimate — every money question) and `$ai_generation` (provider-priced) are NEVER summed; both client hooks must stay; `@llm_step` goes on the SHARED-core step function, never a call site | `docs/llm-analytics.md` |
| **Error tracking** (#648) | `logger.py` + `observability.capture_exception` | Logs ≠ `$exception`: the grouped issue is what alerts and files GitHub issues. Never capture `HTTPException` — 4xx is a response | `docs/error-tracking.md` |
| **Browser analytics** (#646) | `ui/src/utils/analytics.ts` | Never call `posthog` directly; `distinct_id = String(user_id)` so browser+Celery+proxy are ONE person; build-time gated (`VITE_POSTHOG_KEY`); `maskProps()` on every content editor | `docs/posthog-advanced-surface.md` |
| **Session replay** (#649) | `ensureSessionRecorded()` | Rules live in the SDK: `VITE_POSTHOG_REPLAY_SAMPLE` slice + EVERY `$exception`/feedback session. Never set project sampling — it multiplies | `docs/session-replay.md` |
| **KPI dashboards + alerts** (#650) | `scripts/posthog_provision.py` | Alert tiles must be native single-series `TrendsQuery` on STRING props (a boolean filter matches nothing → silent alert); money tiles read `$ai_generation` | `docs/kpi-dashboards.md` |
| **Endpoints panel + release annotations** (#654) | `GET /user/posthog-stats`, `scripts/posthog_annotate.py` | Every HogQL query scoped with `distinct_id = {variables.distinct_id}` (ONE shared project); the personal API key stays server-side; a missing key is a no-op, never a failed release | `docs/kpi-dashboards.md` |
| **Experiments** (#652) | `utilities/experiments.py` | Unresolvable experiment = **CONTROL** (no env fallback per experiment); rollout-% / distinct-ID flags only. Registered: `cost-routing-arm`, `comment-contract-prompt`, `post-media-variant` | `docs/experiments.md` |
| **Feature flags** (#651) | `utilities/flags.py` | **Fails open to the env var** on every unresolvable path; read at CALL SITE, never at import; safety controls (429 breaker, holds, caps) are NOT flags | `docs/feature-flags.md` |
| **Marketing attribution** (#658) | `utilities/marketing/attribution.py` | Only OWNED destinations tagged (`is_owned_link`); existing UTMs never overwritten; `signup_completed_web` ≠ `signup_completed` | `docs/marketing-attribution.md` |
| **Model-tier benchmarks** (#721) | `scripts/benchmark_models.py` | The suite scores a FIRST draft, production ships an n-th — `contract` checks are the floor, `repairable` advisory (#910); an all-errored run is REFUSED, not a scorecard of zeros (#923) | `docs/model-benchmarks/README.md` |
| **Content-quality telemetry** (#630) | `auto_nightly_content_quality` | The TREND LINE, not a gate — **unscored is never zero**, and it never pauses anything (safety is #629) | `docs/content-quality-telemetry.md` |
| **Surveys — NPS/CSAT** (#653) | PostHog Surveys + `utilities/surveys.py` | Type `api`, rendered headless in `PostHogSurveyModal.tsx`; ONE answer = TWO paths counted ONCE; `markSurveySeen()` is what advances the 30d wait | `docs/surveys.md` |

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

- The stack launches with **both** compose files (`-f docker-compose.yml -f docker-compose.prod.yml`) — the prod overlay strips the dev bind-mount, so every app service runs the code baked into the image; editing files on disk does nothing until a new image ships. `web_app` is a tiny nginx **edge** routing to the active blue/green color; deploys are zero-downtime flips, releases batch 4x daily (05/11/17/23 UTC) — `docs/zero-downtime-deploys.md`. A **`release:now`** label ships a PR at merge instead of the next window (`docs/release-fast-lane.md`).
- **Runtime state (429 breaker, manual automation pause, reply-sweep cadence keys) lives in Redis**, not the DB or containers — it survives deploys.
- A **local hotfix deploy** fallback exists for when CI/release is blocked; it diverges prod from `main` until the fix lands via the normal PR flow. Full posture + compose layering + image refs: `docs/DEPLOYMENT.md`.

## Known Gotchas

- **Legacy drift:** `get_docker_driver()` targets `selenium/standalone-chrome:latest` at 4444, not a Grid hub+node; `ai_helper.py` uses tier aliases, not a hardcoded `gpt-4o-mini`; PostHog replaced Prometheus + Jaeger (both gone from docker-compose); the external `linkedin-preview` service is gone — preview is the native `LinkedInPostPreview.tsx`.
- **LinkedIn SDUI** (`docs/sdui-selenium-notes.md`): the old `urn:`, `feed-shared-*` and `comments-comment-*` anchors are gone — prefer `data-testid` / `aria-label`. Three fix invariants (#1013): **success is the OUTCOME being present, never a click having landed**; **never click a control whose label names a different entity than the target** (the #1012 rail hazard); **zero items is not "nothing to do" until the page agrees** — cross-check an anchor the walk doesn't use (`_report_zero_walk`). Every surface maps to a read-only probe flag + a weekly `ok`/`drift`/`unknown` sweep filing ONE issue per drift (`docs/sdui-probe-coverage.md`). The comment composer has NO `<form>`; the sticky global nav steals clicks from an unfocused composer; every composer lookup is scoped to its OWN post (`_post_composer_for_card` / `_reply_composer_for_comment`), and a miss is a DEBUG no-op. A post PERMALINK runs that SAME engine (`comment_on_post` → `_permalink_post_card`, #966) — NOT a one-post page, so the card is picked by the permalink's URN, the reaction happens BEFORE the comment, and a comment that doesn't land is a FAILURE row, never a SUCCESS.
- **Emoji in Selenium:** ChromeDriver `send_keys` throws on non-BMP emoji — strip them before typing with `_strip_non_bmp()`.
- **ENUM columns:** `logs.action_type` (and other status columns) are MySQL ENUMs — adding a value requires a migration. New migrations use **TIMESTAMP** versions so two branches never collide; the **db-migration** skill and `compose/local/database/migrations/README.md`.
- **Unified content core** (`docs/content-core.md`): newsletters, posts, AND comments draw framework, research, and alignment from `utilities/ai/content_framework.py` / `content_research.py` / `content_alignment.py` — never add parallel per-content-type prompt helpers. Comments carry a **quality contract + similarity gate** (#617) that SKIPS the post after `COMMENT_GATE_MAX_ATTEMPTS` failed regenerations. **Story bank** (#620) is the FACT half; **deck reference gate** (#728) the save-worthiness half; **slop lint** (#625, `slop_lint.py`) BLOCKS on five HARD checks, advises on four WARN.
- **Content mix (70/20/10) governor** (same doc): every planned post carries a mix class in `posts.content_mix` — `value` (70%) / `authority` (20%) / `promo` (10%, forced `case_snapshot` blueprint). **Promo CTAs are always an ARTIFACT** (lead magnet / newsletter) — a meeting ask is banned in prompts and repaired deterministically; any that survives HOLDS the post at PENDING via the `meeting_cta` gate.
- **Stale lazy chunks after a deploy (#743, `docs/spa-deploy-freshness.md`):** a tab open across a release fetches a code-split chunk on a hash the new image no longer has. Three layers: asset **retention** (both colors serve a live-bundle miss from a shared archive volume), a **one-shot reload** on import failure (loop-guarded, skipped offline), and polling of `/api/app-info` that prompts rather than reloads.

## Git Safety & Multi-Agent Concurrency Rules
- **Fresh state:** before generating ANY code edit, run `git status` and read the target file. Never edit from conversation memory — another agent may have changed it under you.
- **Micro-branching:** never edit a shared branch asynchronously; start each distinct task on its own branch (`git checkout -b feature/claude-<task-name>`).
- **Atomic commits:** stage + commit each completed sub-task with a clean conventional-commit message.
- **Conflict avoidance:** if working-tree changes clash with your target files, halt, `git stash`, pull the current state, resolve, then re-apply.
- **Branch cleanup:** merged feature branches auto-delete (repo setting `delete_branch_on_merge=true`); orphans swept weekly by `.github/workflows/stale-branches.yml`, with a 48-hour grace window protecting active agent work. Full posture: `docs/branch-cleanup.md`.
- **A label is not an access control** (`docs/contribution-security.md`): this repo is PUBLIC and the agent pipeline runs with the owner's credentials, so `agent:ready` / `release:now` are verified by **provenance, not presence**. `tick.sh` checks TWO independent things — the AUTHOR has standing (`author_trusted`) and the label was applied by an allowlisted actor (`label_actor_trusted`, timeline API) — plus `pr_is_upstream` on PR lanes; an unreadable answer REFUSES. The three writers of `agent:ready` are gated at source: `triage_issues.py` grants it only to trusted authors, and the feedback loop (unauthenticated `POST /api/feedback`) **never** grants it. `.github/CODEOWNERS` guards every control surface, and the pipeline's credential has **no `workflows` permission** — the hard control, since agent and owner currently share one identity.
