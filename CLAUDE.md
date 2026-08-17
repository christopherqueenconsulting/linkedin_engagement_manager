# LinkedIn Engagement Manager — Claude Code Context

## Project Overview

LinkedIn Engagement Manager (LEM) automates LinkedIn engagement end to end: Selenium scraping and feed interaction, AI-generated content (LiteLLM proxy routing to OpenAI / Claude / Ollama / OpenRouter), Celery task queue, React SPA, MySQL, FastAPI backend.

Two pillars, detailed under **Feature Areas**: content generation & scheduling, and engagement
automation. **CLAUDE.md is a fixed-shape map** — locations, symbols, invariants, and where the
detail lives. Every row points at the `docs/*.md` holding its full posture; the index of all of
them is `docs/README.md`.

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
| Infra | Docker Compose — the ONLY supported deploy (VPS). The AWS CDK tree was retired in #973 |
| Observability | PostHog |

## Directory Map

```
src/cqc_lem/
├── api/           FastAPI app — engagement_preferences, DM template, PIN endpoints
├── app/           Celery tasks (run_scheduler, run_content_plan, generate_variants, my_celery);
│                  run_automation.py is GONE — emptied by #1154, deleted by #1206
│   └── engagement/  one module per lane (#1154): invites, newsletter, feed, posting, outreach.
│                    Every task pins name='cqc_lem.app.run_automation.<fn>' — a WIRE IDENTIFIER, not
│                    a module path: moving a task RENAMES it. Still correct in
│                    celeryconfig.task_routes, never "correct" it; test_task_name_stability.py holds
│                    both halves. Posture: docs/engagement-automation.md
├── domain/        Pure types, zero I/O (#1220): PostEngagementRow (the ONE post-stat column layout,
│                  asserted at the reader, so platform/db stays domain-free), FeedRunContext,
│                  PostDraftContext. THESE THREE only — add a fourth only on evidence
├── utilities/
│   ├── ai/        LiteLLM helpers (ai_helper.py, client.py) + content_framework/content_research/content_alignment/story_bank/slop_lint
│   ├── linkedin/  Selenium automation (scrapper, poster, company_page_inviter, verification_pin, rate_limit, helper, profile, token_refresh)
│   │              + the shared engagement core (#1154): session (get_current_profile / browser_session), composer, cards
│   ├── marketing/ video_tutorials.py — automated SPA tutorial videos
│   ├── human_pacing.py  ONE cadence engine
│   ├── db.py      DB facade — re-exports platform/db/ (no raw SQL outside those two)
│   ├── proxy.py   Per-user static residential proxy resolution
│   ├── geocoding.py  Login Location city/state geocoding
│   ├── logger.py  Structured logger — log_debug through log_critical
│   └── selenium_util.py  get_docker_driver() + MV3 proxy-auth extension builder
└── ui/            React SPA (Account.tsx holds engagement prefs)
tests/
├── unit/          Fast tests — mock all I/O
└── integration/   Require MySQL + Redis (TWO lanes; #1215 deleted tests/e2e/)
compose/local/database/migrations/  Flyway migrations
.litellm/         config.yaml + complexity_router.py (lem-router pre-call hook)
```

## Code Conventions

- **Logging:** Never use `print()`. Use the structured logger from `cqc_lem.utilities.logger`
  (`log_debug` / `log_info` / `log_warning` / `log_error` / `log_critical`; the `myprint()` shim is
  retired — ruff bans it). Pass context as keyword args (`user_id`, `post_id`, `task_name`, `action_type`, …);
  `log_error`/`log_critical` take `exc=`. **Once is a warning, repeatedly is a defect:** a repeated
  `log_warning` re-emits at ERROR and files ONE grouped `$exception`, so never warn on an expected
  no-op — log those DEBUG. Level table + escalation contract:
  **`src/cqc_lem/utilities/CLAUDE.md`** (auto-loads in that tree), `docs/error-tracking.md`.
- **Type hints:** Required on all function signatures.
- **Enums:** Use `PostStatus`, `PostType`, `LogActionType` from `db.py` for status fields — never raw strings.
- **Imports:** Absolute imports from `cqc_lem.*` throughout.
- **Database:** All DB access goes through `utilities/db.py` or a `platform/db/repositories/*.py`
  module it re-exports (#1154). No raw SQL anywhere else, and **importers keep using
  `cqc_lem.utilities.db`** — the facade is the stable name. Patch targets are the exception: patch a
  moved symbol where it now LIVES, because a sibling calls it directly, never the re-export
  (`tests/unit/platform/db/test_connection_seam.py` fails the build on that hazard set).
- **Secrets:** Never hardcode. Use `.env` with `load_dotenv()`. See `.env.example` for required variables.
- **Comments & docstrings:** Only add a comment when the WHY is non-obvious — that is what a
  docstring is for. Ruff enforces **Google-convention docstrings** in the **Docstring & Lint Gate**,
  as a **ratchet**: it fails a PR that raises the count in `.ruff-baseline`. Read that count with
  **`scripts/ruff_count.sh`** only — `ruff … | wc -l` is 2 high. Never invent behaviour to satisfy
  the linter. **`docs/docstring-standard.md`**.

## AI Call Pattern

All LLM calls go through the LiteLLM proxy via `client.chat.completions.create(model="lem-simple",
…)` in `utilities/ai/client.py`. `AttributedOpenAI` is the ONE client — it stamps attribution + trace ids on every endpoint, and
**rides out a proxy that is not accepting connections** (#986, a container restarted on deploys):
ONLY a connection that was never established is retried (`LLM_CONNECT_RETRY_ATTEMPTS` /
`LLM_CONNECT_RETRY_BACKOFF_SECONDS`) — nothing was sent, so there is no spend to duplicate. A
timeout, 4xx or 5xx is the proxy answering, and fails as before.

**Model tier aliases** (`.litellm/config.yaml`):

| Alias | Use case |
|---|---|
| `lem-simple` | Short outputs ≤300 chars: refine, summarize, comma list |
| `lem-medium` | Balanced: comments, post refinement, blog summaries |
| `lem-complex` | Long-form: thought leadership, personal story, industry news |
| `lem-image` | Image generation (gpt-image-2, gpt-image-1 in-group fallback) |
| `lem-vision` | Render quality gate — looks at a generated image |
| `lem-embedding` | Embeddings for feedback dedup/clustering |
| `lem-router` | Auto-routes by prompt complexity via `LEMComplexityRouter` |

**Cost-aware down-routing** (`utilities/routing_policy.py`, `utilities/cost_routing.py`): routes a
tier ONE step down for the treatment cohort of an active cost/quality experiment. `routing_policy.py`
is the shared decision core — the app imports it AND docker-compose mounts that same file into the
LiteLLM container — so it stays **stdlib-only** (no `cqc_lem.*` imports). Off unless BOTH
`COST_ROUTING_ENABLED` and `COST_AWARE_ROUTING_ENABLED` are set.
`docs/cost-performance-margin-plan.md` §D.1.1. Per-function assignment: `ai_helper.py`.

**Image stack (ONE engine, two modules, `docs/image-stack.md`):** `utilities/ai/image_brief.py`
authors every image prompt — never add a per-content-type prompt helper, add a preset.
`utilities/ai/image_gen.py` renders it; `render_image_gated` adds the bounded `lem-vision` check,
failing OPEN. Avatar likeness NEVER renders in `image_gen` — `generate_post_image` owns the LoRA path
behind `avatar/guardrails.resolve_avatar_for`. NO text/logos in a render prompt.
`utilities/post_image.py` (#1030) is the ONE place a POST's image is validated, stored and removed.
A compose-time `image_url` is CALLER input — `/schedule_post/` takes it only when
`owns_post_image_url` says it is a preview we issued that caller, and a stored URL never resolves
outside `assets_dir`. `utilities/post_video.py` (#1443) is its VIDEO counterpart, with a separate
ownership gate, so `image_url` can never be handed an MP4.

## Selenium Pattern

Always use `get_docker_driver()` from `selenium_util.py` — never instantiate `webdriver.Chrome()`
directly. Use `click_element_wait_retry()` for all clicks (transient DOM timing).

Browser capacity is a **fixed pool of Chrome session slots shared by the Celery Selenium lanes**:
`SE_NODE_MAX_SESSIONS` must always equal the summed `SELENIUM_CONCURRENCY` of those lanes, and
`tests/unit/app/test_selenium_capacity.py` fails the build if they drift.
**`selenium-node-debug` is NOT in that sum** (#1301) — the probe and the Selenium MCP browser
REQUIRE it, so neither takes a lane slot. Off the pool ≠ safe for the ACCOUNT.
`docs/SELENIUM_GRID.md`, `docs/scaling-plan.md`.

## Feature Areas

### Content generation & scheduling (`app/run_content_plan.py`, `app/run_scheduler.py`, `utilities/ai/ai_helper.py`)

AI content by buyer-journey stage (awareness / consideration / decision) — a 30-day plan
auto-scheduled around golden/peak hours, with self-healing carousels and asset backfill.
`PostType` is `text` / `carousel` / `video`; `PostStatus` includes `error` for generation/posting
failures needing a manual fix. Archetypes and the plan's shape: `docs/content-scheduling.md`.

| Area | The ONE place | The invariant that bites | Doc |
|---|---|---|---|
| **Cadence** (#621) | `POST_DAY_TYPES` | The plan is NOT one post a day — it fills `posts_per_week` slots (2–7, default 3) of a fixed calendar that also sets each post's stage and archetype. `posting_days` (#581) is the separate, HARDER bound on which days may carry one | `docs/content-scheduling.md` |
| **Newsletter covers** (#893, #1432) | `utilities/newsletter_cover.py`; `_approved_cover_path` is the ONLY way a cover reaches LinkedIn | An **upload** lands `approved`, a **generated** cover always `pending_review`. An unapproved cover at the slot is notify-and-publish | `docs/newsletter-covers.md` |
| **Blog alignment** (#967) | `resolve_blog_source` in `utilities/blog_source.py` | The ONE place `align_with_blog` (default ON) becomes source text — blog URL first, sitemap fallback. Never blocking: nothing readable is `None` and the edition writes from topic + profile. Resolved PER edition | `docs/content-core.md` |
| **Occasion / milestone posts** (#1074, #1088) | `project_launch` / `educational_milestone` in `content_framework.py`; `linkedin/share_composer.py` | The ONLY archetypes nothing picks automatically, enforced by `posts.manual_publish`. A Post click the feed never confirmed is held at `error`, NEVER retried | same |
| **Video captions** (#1278) | `utilities/video_captions.py` via `_caption_video_asset` | The post's OWN first 1-2 lines burned into the stored MP4 — never re-authored (no LLM). Runs on the PROBED file, BEFORE C2PA (a re-encode strips credentials). Fails open | `docs/content-quality-audits/video.md` |
### Engagement automation (`app/engagement/{feed,posting,outreach,invites,newsletter}.py`)

Full posture for every row: **`docs/engagement-automation.md`**. One row per lane — the ONE place,
and the invariant that bites. Flags named here default OFF — the ONE exception is
`STALE_INVITE_WITHDRAWAL_ENABLED`, ON since #1006 grounded it live. Which module owns which lane,
the pinned wire identifier, and **where to import and patch a task** are in
`src/cqc_lem/app/engagement/CLAUDE.md`, which auto-loads in that tree. Two lanes have their own
doc as well: `docs/group-posts.md` (weekly group post) and `docs/AUTOMATION_COOLDOWN.md` (the 429
breaker every lane answers to).

| Lane | The ONE place | The invariant that bites |
|---|---|---|
| **Feed commenting** | `_score_feed_post`; selectors in `linkedin/helper.py` | Recency-dominant. An unsorted scan must NEVER read as recency-sorted (#817) — `_switch_feed_to_recent` reports the run's sort state onto the funnel |
| **Replies / seed comment** | `automate_reply_commenting`, `auto_seed_comment_on_post` | A seed is the user's own first comment; it counts against `SELF_COMMENT_MAX_PER_POST` |
| **Golden-hour presence** | `utilities/golden_hour.py` (#622) | ONE report per swept post, measured off REAL publish time from the POST log, never `scheduled_time` — unmeasured is never on-time. Seed + wave never stack (`SELF_COMMENT_MAX_PER_POST=2`) |
| **Human pacing** | `utilities/human_pacing.py` (#626) | The ONE cadence engine. Every draw is seeded on (user, action, date) and persisted, so a retry never re-rolls. Fails OPEN — the 429 breaker is the separate, harder gate |
| **DM auto-nurture** | `_nurture_after_reply`, `ai/dm_nurture.py` | Approval-gated (`pending`, `source='nurture'`), ONE open draft per thread; explicit disinterest stops the thread for good |
| **Reciprocity** (#1091) | `post_engagers` + `get_recent_engagers` | A third-party COMMENTER on our own post is the ONLY input — reactors never are, so an empty table is an AUDIENCE fact that raises nothing |
| **DMs + follow-ups** | `build_dm_from_template`, `dm_templates`, `dm_followups`, `process_user_followups` | Templated and voice-aligned; multi-touch sequences per-user paced |
| **Appreciation sources** (#968) | `APPRECIATION_SOURCES_ENABLED` | Recommendations and collaborations are STANDING lists, not event queues — an **undated card is SKIPPED**. `appreciation_touches` is the durable CLAIM against double-thanking |
| **Message-thread ladder** | `linkedin/message_thread.py` (#731) | A route counts only when the thread is **provably open** — never class names. `ThreadState` is three-valued and **UNKNOWN SKIPS**. Sent means the message LANDED, never that Send took a click |
| **Owned-asset CTA loop** (#624) | `resolve_artifact_delivery` + `_queue_artifact_delivery` | The ONE map from a CTA to its asset, naming the CHANNEL. The keyword is CONSENT so it must be a whole WORD; `can_open_dm_thread` fails OPEN (#1528) |
| **Comment outcomes** (#628) | `sweep_comment_outcomes` + `utilities/comment_outcomes.py` | Read-only T+24h sweep, ONE row per comment. `visible_most_relevant` is three-valued — NULL unreadable, excluded from the denominator. Demotion HOLDS that user's feed commenting |
| **Suppression tripwire** (#629) | `auto_suppression_tripwire` + `utilities/suppression.py` | 2026 penalties are SILENT, so compare against the user's OWN trailing 14-day median. Pauses **engagement only** — posting is never gated; a thin baseline is `unknown` |
| **Groups sync + reconcile** (#1316, #1487) | `_read_groups_directory`; `disable_user_groups` | Reconciling fails CLOSED: **absence from one walk is never evidence**, `unknown` is never actioned, and a disable is `enabled=0`, never a DELETE |
| **Weekly group post** (#932, #1224, #1415) | `auto_draft_group_post` → `auto_post_to_group` | TWO beats, so **a run with no READY draft publishes nothing**. ONE open draft per user. A media commit is WAITED for — a clickable control, never a clock — failing OPEN |
| **Roster targets** (#962) | `comment_on_roster_posts`, `auto_follow_roster_target` | Posts but ZERO commentable cards records a blocked visit; a whole roster blocked records nothing. Auto-follow is bounded by (never in) the account envelope |
| **Roster connect escalation** (#979) | `advance_roster_connect` | blocked → follow → `needs_connection` → (opt-in) ONE invite. `needs_connection` needs EVIDENCE; a landed comment stands it down. ONE shot per target, `requested` written BEFORE dispatch |
| **Stale-invite withdrawal** (#969) | `linkedin/stale_invites.py`, `STALE_INVITE_WITHDRAWAL_ENABLED` (the one flag here that is ON) | Withdrawing is ONE-WAY, so reads fail CLOSED — an unreadable "Sent … ago" is NEVER stale |
| **Company-page invites** (#732) | `linkedin/company_page_inviter.py` | A paced DAILY drip bounded by the SMALLEST of three ceilings. `plan_daily_invites` decides all of it BEFORE a Chrome session opens |
### Engagement configuration (`engagement_preferences` table, API in `api/main.py`, SPA in `ui/.../Account.tsx`)
- **Targeting:** include/exclude topics/keywords/authors, `min_reactions`, `max_post_age_hours`, LLM topic-relevance scoring. **Voice:** tone, `comment_length` (short/medium/long; default short), style, emoji/hashtag toggles.
- **Caps:** `max_comments_per_day`, `max_dms_per_day`; DM template editor with follow-up steps; Login Location (city/state geocoding via `utilities/geocoding.py`, admin-overridable).
- **Profile freshness** (`utilities/profile_refresh.py`, #1076): `POST /user/linkedin-profile/refresh` is the ONE on-demand re-scrape. `claim_profile_refresh` (Redis, 1/user/day, fails OPEN) is taken BEFORE dispatch. Always **202**, never 429 — a second press the same day is an expected no-op (DEBUG). `docs/engagement-automation.md`.

### Marketing video tutorials (`utilities/marketing/video_tutorials.py`, beat `produce-feature-tutorial`)
- One declarative `TutorialFlow` per feature → headless SPA capture → grounded script → TTS → MP4 + `.srt` → YouTube. **Fail-closed, cheapest-first**: a missing anchor, unparseable script, profanity, over-cap narration or fabricated number aborts BEFORE any TTS/publish spend. OFF unless `TUTORIAL_VIDEOS_ENABLED`. `docs/marketing-video-tutorials.md`.
- **YouTube OAuth token** (`youtube_auth.py`, #742): the ONE place its state is decided. `unknown` (Google unreachable) is NOT `needs_reauth` — the only state that alerts. The weekly `youtube-token-check` beat IS the keep-alive vs the 6-month-disuse expiry, so never drop it while the feature is off. `docs/youtube-publishing.md`.

## Anti-bot & session infra

How a session stays signed in and unflagged. One row per surface.

| Surface | The ONE place | The invariant that bites | Doc |
|---|---|---|---|
| **Proxy + browser identity** | `utilities/proxy.py`, `_build_proxy_auth_extension_b64` | Per-user static residential proxy behind an in-memory MV3 auth extension — never URL-embedded credentials (MV2 background pages died in Chrome 149+) | `docs/PER_USER_PROXY.md` |
| **Cookie + PIN login** | `linkedin/verification_pin.py` | `li_at` is the DEFAULT engagement login since #745, not a nicety — the password fallback is the path that draws a challenge | `docs/LINKEDIN_COOKIE.md` |
| **Sign-in visibility** (#933) | `linkedin/login_status.py` | `_persist_session_cookies` is where both login paths meet, so it is where a sign-in is recorded. Fails open: `unknown` means nothing recorded, NOT a broken connection | `docs/linkedin-session-health.md` |
| **OAuth renewal** (#600) | `resolve_token_status` in `linkedin/token_refresh.py` | The ONE place token state is decided. LinkedIn caps auth at 60 days, so the daily 08:30 beat is the only way a token outlives that. `days_remaining` is `None`, never 0, when unreadable | same |
| **429 / auth-wall** | `linkedin/rate_limit.py` | The breaker BLOCKS the navigation where pacing only delays it, and is never a flag. Both no-op without Redis | `docs/AUTOMATION_COOLDOWN.md` |
| **Secrets at rest** (#745) | `utilities/crypto.py`; `db.py` is the ONE caller | AES-256-GCM per user+column, and the field-name constants are **AAD — renaming one orphans every row**. `ENCRYPTION_REQUIRED=true` in prod, so reads FAIL CLOSED | `docs/secrets-at-rest.md` |
| **Identity + sessions** (#745 2b) | `api/main.get_session_user_id()` | `users.public_uid` is the identity; email is a movable ATTRIBUTE. EVERY `/api` route resolves its caller through it (#914) — an `email`/`user_id` is a TARGET, never the actor. A DB fault is **503** | `docs/identity-and-sessions.md` |
| **Docs surface** (#1020) | `_hide_admin_routes_from_schema()` | Every `/api/admin/*` operation is kept OUT of the published schema. **Hidden ≠ gated** — auth is unchanged. Unauthenticated `GET /health/deep` returns COUNTS only | `docs/stack-watchdog.md` |
| **Strong auth + step-up** (#745 2c) | `utilities/auth_factors.py` (ceremonies in `webauthn_util.py`) | Once an account enrols a passkey or TOTP the email PIN is a **bootstrap** only. `sessions.last_verified_at` gates every credential-touching write; refusal is **403**, never 401 | `docs/strong-authentication.md` |
| **Session scopes are SURFACES** (#905/#1026) | `get_session_user_id()` — the same resolver | Refusal is 403 + audited. **`agent`** is the headless credential: `_AGENT_SESSION_SURFACE` only, and it may queue but **NEVER approve** | `docs/identity-and-sessions.md` |
| **Admin user management** (#1450) | `/api/admin/users*` in `api/routers/admin.py` | ONE write: grant/revoke admin, step-up gated, audited against the TARGET. **A failed read is 503, never an empty answer** | `docs/admin-user-management.md` |
## Agent Working Method

Six practices wrap `ship-issue`'s branch → build → PR flow — three around the PR itself, three
session/context discipline (the dominant lever on token spend):

- **Before code on a non-trivial issue** (`docs/spec-verifier-environment.md`, skill `spec-first`):
  nail testable acceptance criteria, name the check that proves success, locate the owning
  docs/skill/module — BEFORE `ship-issue` step 1.
- **Hand token-heavy EXECUTION to Codex** (`codex@openai-codex`): this session keeps the judgement.
  Delegate a **bulk file edit** (a scripted pass still beats both agents when the change is uniform),
  a **well-specced build**, or a **bug still failing after 2 attempts** — two is a limit, because a
  third pass from the same context re-derives the same wrong model. NOT to Codex: deciding WHAT to
  build, reading a failure, anything touching a documented invariant, the last look before the PR.
- **Gauntlet Loop** (`docs/gauntlet-loop.md`, skill `gauntlet-loop`): optional pre-PR bar — builder/
  critic pairs blind-compare against a named reference exemplar, capped at 3 rounds then
  `needs-human`. First-class for `ui/`-touching issues. Slots into `ship-issue` step 4.
- **Prefer `cavecrew-investigator`/`cavecrew-reviewer` over vanilla `Explore`/`general-purpose`**
  for a bounded, read-only lookup — already ~60% fewer tokens per delegation; default to it.
- **Right-size fan-out to the minimum agent count.** A parallel wave of N pays N full cold-context
  spawns regardless of caching — default to 1 unless the work is genuinely split-able.
- **One thread, one deliverable.** End the session (or `/clear`) once a distinct piece of work is
  done, rather than stringing unrelated large tasks through one growing thread.

## Testing Standards

- All new/modified code: ≥80% patch coverage enforced by Codecov.
- **TWO lanes** (#1215): **unit** (`tests/unit/`, mock ALL I/O — fixtures `mock_openai_client` / `mock_database_connection` / `mock_selenium_driver`, plus hermetic autouse guards) and **integration** (real MySQL + Redis). **No CI lane drives a browser** — `scripts/linkedin_live_validation.py` grades Selenium.
- Run: `poetry run pytest tests/unit -v --tb=short`; coverage: `poetry run pytest --cov=src/cqc_lem --cov-report=xml`.
- Markers, fixtures, lane selection: the **test-lanes** skill and `tests/README.md`.

## Observability

Track events via `utilities/observability.py`. There is ONE `posthog.capture`, in `_emit()`, and an
event's property shape is declared in the `EVENTS` registry (#1218) — a new event is an `EventSpec`,
never a new capture. `label()` forces a filtered property to a **string**, because PostHog matches on
the ingested type and one boolean row silently stops an ALERT firing.
The paragraph behind each row is `docs/observability-map.md`.

| Surface | The ONE place | The invariant that bites | Doc |
|---|---|---|---|
| **LLM analytics** (#647, traces #746) | `utilities/ai/client.py` (+ `@llm_pipeline` / `@llm_step`) | `llm_call` (app estimate) and `$ai_generation` (provider-priced) are NEVER summed; `@llm_step` goes on the SHARED-core step function, never a call site | `docs/llm-analytics.md` |
| **Error tracking** (#648) | `logger.py` + `observability.capture_exception` | Logs ≠ `$exception`: the grouped issue is what alerts and files GitHub issues. Never capture `HTTPException` — 4xx is a response | `docs/error-tracking.md` |
| **Browser analytics** (#646) | `ui/src/utils/analytics.ts` | Never call `posthog` directly; `distinct_id = String(user_id)` so browser+Celery+proxy are ONE person; `maskProps()` on every content editor | `docs/posthog-advanced-surface.md` |
| **Session replay** (#649) | `ensureSessionRecorded()` | Rules live in the SDK: a `VITE_POSTHOG_REPLAY_SAMPLE` slice plus EVERY `$exception` session. Never set project sampling — it multiplies | `docs/session-replay.md` |
| **KPI dashboards + alerts** (#650) | `scripts/posthog_provision.py` | Alert tiles must be single-series `TrendsQuery` on STRING props — a boolean filter matches nothing, so the alert is silent | `docs/kpi-dashboards.md` |
| **Endpoints panel + release annotations** (#654) | `GET /user/posthog-stats`, `scripts/posthog_annotate.py` | Every HogQL query is scoped with `distinct_id = {variables.distinct_id}` (ONE shared project); a missing key is a no-op | `docs/kpi-dashboards.md` |
| **Personal-key scoping** (#1453) | `utilities/posthog_keys.py` | The ONE place a personal API key is resolved, BY PURPOSE, each falling back to `POSTHOG_PERSONAL_API_KEY`. Three consumers fail SILENTLY, so a green deploy proves nothing | same |
| **Experiments** (#652) | `utilities/experiments.py` | Unresolvable experiment = **CONTROL**, with no per-experiment env fallback; rollout-% / distinct-ID flags only | `docs/experiments.md` |
| **Feature flags** (#651) | `utilities/flags.py` | **Fails open to the env var** on every unresolvable path; read at CALL SITE, never at import; safety controls are NOT flags | `docs/feature-flags.md` |
| **Marketing attribution** (#658) | `utilities/marketing/attribution.py` | Only OWNED destinations are tagged; existing UTMs are never overwritten; `signup_completed_web` ≠ `signup_completed` | `docs/marketing-attribution.md` |
| **Model-tier benchmarks** (#721) | `scripts/benchmark_models.py` | The suite scores a FIRST draft, production ships an n-th — `contract` checks are the floor. An all-errored run is REFUSED, never a scorecard of zeros | `docs/model-benchmarks/README.md` |
| **Content-quality telemetry** (#630) | `auto_nightly_content_quality` | The TREND LINE, not a gate — **unscored is never zero**, and it pauses nothing (safety is #629). A render/clip reading comes off the RECEIPT; `missing` is NULL, never 0 | `docs/content-quality-telemetry.md` |
| **Surveys — NPS/CSAT** (#653) | PostHog Surveys + `utilities/surveys.py` | Type `api`, rendered headless; ONE answer = TWO paths counted ONCE; `markSurveySeen()` advances the 30d wait | `docs/surveys.md` |

## CI Gates

The SIX contexts branch protection requires on `main` (verified against the API): `Unit Tests
(Python 3.12)`, `Integration Tests`, `UI Build`, `Migration Versions`, `GitGuardian Scan`,
`CodeQL PR Quality Gate`.

**One workflow per test lane** (`tests/README.md`), each owning the one Codecov flag `codecov.yml`
declares, all selecting `-m "not slow"`; never add a whole-suite workflow. `slow` tests are live
third-party probes — nightly via `slow-tests.yml`, never a PR gate.

`CodeQL Security Analysis` and `Docstring & Lint Gate` run but are NOT required (the latter is the
ratchet under Code Conventions). **`required_approving_review_count` is 0**, so
`require_code_owner_reviews` enforces nothing (`docs/contribution-security.md`).

## Production Deployment & Environment

LEM runs as a Docker Compose stack on a **Hostinger VPS**, exposed via a Cloudflare Tunnel. Two checkouts on the box, NOT the same:

| Path | Owner | Purpose |
|---|---|---|
| `/home/lem/linkedin_engagement_manager` | `lem` | Dev/agent working checkout (where you edit + commit). Has `./src` on disk. |
| `/opt/lem` | `deploy` | **Live production stack.** Compose project workdir; `scripts/deploy.sh` checks out release tags here. |

**Standard release flow (the only path that keeps prod on the release train):**

```
local dev → PR to main → CI gates pass → release-please tags vX.Y.Z → build-and-push.yml builds
  ghcr.io/christopherqueenconsulting/cqc-lem:vX.Y.Z → SSH deploy runs scripts/deploy.sh vX.Y.Z
  (checkout tag, flyway migrate, compose up, /health check, auto-rollback to .last_good_tag)
```

- The stack launches with **both** compose files — the prod overlay strips the dev bind-mount, so every app service runs the image's code; **editing files on disk does nothing until a new image ships**. `web_app` is an nginx edge routing to the active blue/green color; releases batch 4x daily (`docs/zero-downtime-deploys.md`), and **`release:now`** ships at merge instead (`docs/release-fast-lane.md`).
- **Runtime state (429 breaker, manual automation pause, reply-sweep cadence keys) lives in Redis**, not the DB or containers — it survives deploys.
- A **local hotfix deploy** fallback exists when CI/release is blocked; it diverges prod from `main` until the fix lands via the normal PR flow. Compose layering + image refs: `docs/DEPLOYMENT.md`.

## Known Gotchas

- **Emoji in Selenium:** ChromeDriver `send_keys` throws on non-BMP emoji — `strip_non_bmp()`
  (`utilities/linkedin_formatter.py`) first.
- **ENUM columns:** `logs.action_type` and other status columns are MySQL ENUMs — adding a value
  needs a migration, and new migrations use **TIMESTAMP** versions so two branches never collide.
  The **db-migration** skill, `compose/local/database/migrations/README.md`.
- **LinkedIn SDUI** (`docs/sdui-selenium-notes.md`, `docs/sdui-probe-coverage.md`): the old `urn:`
  and `feed-shared-*` anchors are gone — prefer `data-testid` / `aria-label`. Three fix invariants
  (#1013): **success is the OUTCOME being present, never a click having landed**; **never click a
  control whose label names a different entity than the target** (#1012); **zero items is not
  "nothing to do" until the page agrees**. Every surface has a read-only probe flag.
- **Unified content core** (`docs/content-core.md`): newsletters, posts AND comments draw framework,
  research and alignment from `content_{framework,research,alignment}.py` — never add a
  per-content-type prompt helper. Comments carry a quality contract + similarity gate (#617) that
  SKIPS the post after `COMMENT_GATE_MAX_ATTEMPTS` failures; POSTS are graded by the same engine
  (`post_similarity_report`, #1265), ONE retry then **kept but HELD at PENDING** (#1452). The review
  gate is the ONLY place it is measured. **Story bank** (#620) is the FACT half, the **deck reference
  gate** (#728) the save-worthiness half, **slop lint** (#625) BLOCKS five HARD checks and WARNs the
  rest, severity PER SURFACE (`SURFACE_SEVERITIES`) — `canned_scaffold` is WARN on a post but HARD
  on a newsletter (#1285, `docs/content-quality-audits/newsletter.md`).
- **Content mix (70/20/10)** (same doc): every planned post carries a class in `posts.content_mix` —
  `value` 70% / `authority` 20% / `promo` 10%. **A promo CTA is always an ARTIFACT** (lead magnet /
  newsletter); a meeting ask is banned in prompts, repaired deterministically, and any that survives
  HOLDS the post at PENDING via the `meeting_cta` gate.
- **Stale lazy chunks after a deploy** (#743, `docs/spa-deploy-freshness.md`): a tab open across a
  release fetches a chunk hash the new image no longer has. **The same doc holds the API half**
  (#1527): the Cloudflare tunnel CACHES any `/api` GET that arrives without a `Cache-Control`, so a
  write reads as ignored. `api_cache_control_middleware` is the ONE place that is answered —
  `no-store` on every `/api` response, `/api/assets` the single exemption.

## Agent pipeline (v2)

The runner is the **`lem-agentd` daemon** (`scripts/agent-pipeline/v2/`), NOT `tick.sh` — v1 is only
a heartbeat-gated failsafe. State machine, the full `decide()` table, the GitHub field combinations
it is not yet defined for, and the deploy path (the pipeline is **not** in the Docker image):
**`docs/agent-pipeline-v2.md`**. `test_agent_pipeline_v2_decision_table.py` enforces that table — a
new branch without a documented row fails the build. Labels are the human contract:
`docs/AGENT_WORKFLOW_PLAYBOOK.md`.

## Git Safety & Multi-Agent Concurrency Rules
- **Every agent gets its OWN worktree — always.** `isolation: "worktree"` on the Agent call;
  `.claude/agents/*.md` frontmatter carries it too. Agents sharing a checkout WILL clobber each other
  — three once did, one switching the branch under the others inside a minute. `lib/run_lane.sh`
  enforces it, because `cd ""` SUCCEEDS in bash: an empty worktree path silently runs the agent in
  the shared tree instead of failing.
- **Model pins + env traps live in `.claude/agents/builder.md`**: never put `model:` in a
  definition — it inherits the parent's Ollama-lane URL and 400s invisibly at rc=0. Reproduce CI with
  an empty `.env` and `src/cqc_lem/ui/dist` moved aside.
- **Fresh state:** before generating ANY code edit, run `git status` and read the target file — never
  edit from memory; another agent may have changed it under you.
- **Micro-branching:** never edit a shared branch asynchronously; branch per task
  (`git checkout -b feature/claude-<task-name>`), committing each sub-task atomically. If working-tree changes clash with
  your targets, halt, `git stash`, pull, resolve, re-apply.
- **One venv, many worktrees:** the editable-install `.pth` is mutable — `poetry run python -c
  "import cqc_lem…"` may read a DIFFERENT worktree. Use `PYTHONPATH=src` and print `__file__` to
  confirm (`pytest` unaffected).
- **Branch cleanup:** merged branches auto-delete; orphans swept weekly. `docs/branch-cleanup.md`.
- **A label is not an access control** (`docs/contribution-security.md`): this repo is PUBLIC and the
  pipeline runs with the owner's credentials, so `agent:ready` / `release:now` are verified by
  **provenance, not presence** — the AUTHOR has standing AND an allowlisted actor applied the label;
  an unreadable answer REFUSES. The pipeline's credential has **no `workflows` permission** — the
  hard control, since agent and owner share one identity.
