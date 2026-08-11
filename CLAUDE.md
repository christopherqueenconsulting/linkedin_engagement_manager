# LinkedIn Engagement Manager — Claude Code Context

## Project Overview

LinkedIn Engagement Manager (LEM) automates LinkedIn engagement end to end: Selenium scraping and feed interaction, AI-generated content (LiteLLM proxy routing to OpenAI / Claude / Ollama / OpenRouter), Celery task queue, React SPA, MySQL, FastAPI backend.

Two pillars:
- **Content generation & scheduling** — a 30-day plan of buyer-journey posts auto-scheduled around peak/golden hours, with sentiment checks and preview/approval.
- **Engagement automation** — feed commenting, replies, seed comments, appreciation/outreach DMs with follow-ups, a throttled company-page invite drip — driven by per-user targeting, voice/tone, per-day caps.

Code paths in **Feature Areas** below. Subsections carry `docs/*.md` pointers holding the full posture — CLAUDE.md is the map (locations, symbols, constants, invariants, where the detail lives).

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
| Infra | Docker Compose — the ONLY supported deploy (VPS). The `aws/` CDK tree is UNSUPPORTED (#973) |
| Observability | PostHog |

## Directory Map

```
src/cqc_lem/
├── api/           FastAPI app — engagement_preferences, DM template, PIN endpoints
├── app/           Celery tasks (run_scheduler, run_content_plan, generate_variants, my_celery);
│                  run_automation.py is GONE — emptied by #1154, deleted by #1206
│   └── engagement/  every engagement cluster (#1154), one module per lane: invites, newsletter,
│                    feed (SDUI feed + groups + roster tail), posting (publish then measure),
│                    outreach (DMs, appreciation, viewer walk, connect scan, funnel, catch-up).
│                    Every task pins name='cqc_lem.app.run_automation.<fn>' — a WIRE IDENTIFIER, not
│                    a module path: moving a task RENAMES it. Still correct in
│                    celeryconfig.task_routes, never "correct" it; test_task_name_stability.py holds
│                    both halves. Posture: docs/engagement-automation.md
├── domain/        Pure types, zero I/O (#1220): PostEngagementRow (the ONE post-stat column layout,
│                  asserted at the reader, so platform/db stays domain-free) + FeedRunContext /
│                  PostDraftContext. The audit warranted THESE THREE — add a fourth only on evidence
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
├── ui/            React SPA (Account.tsx holds engagement prefs)
└── aws/           AWS CDK stacks — UNSUPPORTED deploy path (#973), kept for reference only
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
  module it re-exports (#1154, split in progress). No raw SQL anywhere else, and **importers keep
  using `cqc_lem.utilities.db`** — the facade is the stable name. Patch targets are the exception:
  once a symbol moves, patch it where it now LIVES — a sibling in that repository module calls it
  directly and never reads the re-export. `tests/unit/platform/db/test_connection_seam.py` derives
  that hazard set per module and fails the build on it.
- **Secrets:** Never hardcode. Use `.env` with `load_dotenv()`. See `.env.example` for required variables.
- **Comments & docstrings:** Only add a comment when the WHY is non-obvious — that is what a
  docstring is for. Ruff enforces **Google-convention docstrings** (`D`) alongside `E`/`F`/`I`/`T201`
  in the **Docstring & Lint Gate**; tests are exempt from the *missing*-docstring rules. The tree
  does not meet the standard yet, so the gate is a **ratchet**: it fails a PR that raises the count
  in `.ruff-baseline`. Read that count with **`scripts/ruff_count.sh`** only — `ruff … | wc -l` is 2
  high, and ratcheting on it leaves that much silent slack. Never restate the signature, never invent
  behaviour to satisfy the linter. A regression routes to `agent:docfix`. **`docs/docstring-standard.md`**.

## AI Call Pattern

All LLM calls go through LiteLLM proxy via `utilities/ai/client.py`:

```python
response = client.chat.completions.create(model="lem-simple", messages=[...])
```

`AttributedOpenAI` is the ONE client — it stamps attribution + trace ids on every endpoint, and
**rides out a proxy that is not accepting connections** (#986; the proxy is a container restarted on
deploys): ONLY a connection that was never established is retried (`LLM_CONNECT_RETRY_ATTEMPTS` /
`LLM_CONNECT_RETRY_BACKOFF_SECONDS`, ~24s default) — nothing was sent, so there is no spend to
duplicate. A timeout, 4xx or 5xx is the proxy answering, and fails as before.

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

**Cost-aware down-routing** (`utilities/routing_policy.py`, `utilities/cost_routing.py`, `docs/cost-performance-margin-plan.md` §D.1.1): routes a tier ONE step down for the treatment cohort of an active cost/quality experiment (arm resolved app-side from a PostHog flag, #652, handed over in the policy document's `arms` map; hash stays as fallback). `routing_policy.py` is the shared decision core — the app imports it AND docker-compose mounts that same file into the LiteLLM container — so it stays **stdlib-only** (no `cqc_lem.*` imports). Off unless BOTH `COST_ROUTING_ENABLED` and `COST_AWARE_ROUTING_ENABLED` are set.

Per-function assignment: `ai_helper.py`.

**Image stack (ONE engine, two modules, `docs/image-stack.md`):** `utilities/ai/image_brief.py`
authors every image prompt — a validated `lem-medium` brief (render prompt + `focal_concept`) with
per-surface presets (`newsletter`/`post_image`/`carousel`/`video`/`thumbnail`); never add a
per-content-type prompt helper, add a preset. `utilities/ai/image_gen.py` renders it
(`IMAGE_BACKEND`, cost-tracked); `render_image_gated` adds the bounded `lem-vision` check, failing
OPEN. Avatar likeness NEVER renders in `image_gen` — `generate_post_image` (ai_helper) owns the LoRA
path behind `avatar/guardrails.resolve_avatar_for`. NO text/logos in a render prompt.
`utilities/post_image.py` (#1030) is the ONE place a POST's image is validated, stored and removed —
upload OR the studio's "Generate with AI", same engine as the scheduled path. A compose-time
`image_url` is CALLER input: `/schedule_post/` takes it only when `owns_post_image_url` says it's a
preview we issued to that caller, and a stored URL never resolves outside `assets_dir`.

## Selenium Pattern

Always use `get_docker_driver()` from `selenium_util.py`. It connects to `selenium-chrome:4444`, polls readiness, sets 1920×1080. Never instantiate `webdriver.Chrome()` directly.

Use `click_element_wait_retry()` for all clicks — it handles transient DOM timing issues.

Browser capacity is a **fixed pool of Chrome session slots shared by the Celery Selenium lanes**:
`SE_NODE_MAX_SESSIONS` must always equal the summed `SELENIUM_CONCURRENCY` of those lanes —
`tests/unit/app/test_selenium_capacity.py` fails the build if they drift. The horizontal path
(`docker-compose.grid.yml`) carries the same invariant with node count as the cap.
**`selenium-node-debug` is NOT in that sum** — extra capacity, enforced since #1301: it declares
`lem:debug=true`, pool nodes `false`, and a production session ASKS for `false` (omitting it still
matches). The probe and the Selenium MCP browser REQUIRE it, so neither takes a lane slot; 2
sessions, a third refused not queued. Off the pool ≠ safe for the ACCOUNT.
`docs/SELENIUM_GRID.md`, `docs/scaling-plan.md`.

## Feature Areas

### Content generation & scheduling (`app/run_content_plan.py`, `app/run_scheduler.py`, `utilities/ai/ai_helper.py`)

AI content by buyer-journey stage (awareness / consideration / decision) — thought leadership,
industry-news commentary, personal story, engagement prompts, carousels, native video, blog
summaries — a 30-day plan auto-scheduled around golden/peak hours, with self-healing carousels and
asset backfill. `PostType` is `text` / `carousel` / `video`; `PostStatus` includes `error` for
generation/posting failures needing a manual fix.

| Area | The ONE place | The invariant that bites | Doc |
|---|---|---|---|
| **Cadence** (#621) | `POST_DAY_TYPES` | The plan is NOT one post a day — it fills `posts_per_week` slots (2–7, default 3) of a fixed calendar that also sets each post's stage and archetype. `posting_days` (#581, default Mon–Fri) is the separate, HARDER bound on which days may carry a slot — weekends opt-in | `docs/content-scheduling.md` |
| **Newsletter covers** (#893) | `utilities/newsletter_cover.py`; `_approved_cover_path` (in `app/engagement/newsletter.py`) is the ONLY thing letting a cover reach LinkedIn | An **upload** is the author's own artwork so it lands `approved`; a **generated** cover always lands `pending_review` (a public brand asset). Opt-in (`cover_image_auto`), best-effort — `STEP_COVER` is never a graded editor step | `docs/newsletter-covers.md` |
| **Blog alignment** (#967) | `resolve_blog_source` in `utilities/blog_source.py` | The ONE place `align_with_blog` (default ON) becomes source text — blog URL first, sitemap fallback. Never blocking: nothing readable → `None` and the edition writes from topic + profile. Resolved PER edition, so queued drafts repurpose DIFFERENT articles. ON with nothing configured is an expected no-op (DEBUG) | `docs/content-core.md` |
| **Occasion / milestone posts** (#1074) | `project_launch` / `educational_milestone` in `content_framework.py` | LinkedIn's "Celebrate an occasion" composer has NO API entity, so LEM drafts the copy and the author pastes it. The ONLY archetypes nothing picks automatically — absent from rotation, variety repair, the planner menu, the `POST_DAY_TYPES` cadence and the 70/20/10 mix. `posts.manual_publish` enforces it: the scheduler never returns one and `post_to_linkedin` refuses one that reaches it. `POST /user/post/mark-posted` is refused for anything NOT `manual_publish` | same |
| **Video captions** (#1278) | `utilities/video_captions.py` via `_caption_video_asset` | The post's OWN first 1-2 lines burned into the stored MP4 — never re-authored (no LLM). Runs on the PROBED file, BEFORE C2PA (a re-encode strips credentials). Avatar-led (`posts.avatar_media`) is SIDECAR-ONLY unless `avatar_caption_overlay`. Fails open | `docs/content-quality-audits/video.md` |
### Engagement automation (`app/engagement/{feed,posting,outreach,invites,newsletter}.py`)

Full posture for every row: **`docs/engagement-automation.md`**. One row per lane — the ONE place,
and the invariant that bites. Flags named here default OFF. Since #1154 every lane lives in
`app/engagement/`: the feed walk, group composer and roster tail in `feed.py`; publishing and the
sweeps that measure what a post earned (`post_to_linkedin`, reply sweep, comment follow-ups, comment
outcomes, post/audience stats) in `posting.py`; DMs and everything deciding who gets one
(appreciation, profile-viewer walk, connect-candidate scan, outreach funnel, catch-up) in
`outreach.py`. **That is where to import and patch them** — `app/run_automation.py` was deleted in
#1206, so `run_scheduler` and `api/*` import each task from the module that DEFINES it. The only
thing still spelled `cqc_lem.app.run_automation.<fn>` is the pinned task name.

| Lane | The ONE place | The invariant that bites |
|---|---|---|
| **Feed commenting** | `_score_feed_post`; selectors in `linkedin/helper.py` | Recency-dominant. `_switch_feed_to_recent` reports the run's sort state onto the feed funnel + `feed_scan` — an unsorted scan must NEVER read as recency-sorted (#817) |
| **Replies / seed comment** | `automate_reply_commenting`, `auto_seed_comment_on_post` | A seed is the user's own first comment; it counts against `SELF_COMMENT_MAX_PER_POST` |
| **Golden-hour presence** | `utilities/golden_hour.py` (#622) | ONE report per swept post, measured off REAL publish time from the POST log, never `scheduled_time` — unmeasured is never on-time. Second wave must ADD substance; seed + wave never stack (`SELF_COMMENT_MAX_PER_POST=2`) |
| **Human pacing** | `utilities/human_pacing.py` (#626) | The ONE cadence engine. Every draw seeded on (user, action, date) and persisted in Redis, so a retry never re-rolls. Fails open — pacing only slows us down; `rate_limit.py`'s 429 breaker is the separate, harder gate |
| **DM auto-nurture** | `_nurture_after_reply`, `ai/dm_nurture.py` | Approval-gated (`pending`, `source='nurture'`), ONE open draft per thread; explicit disinterest stops the thread for good |
| **Reciprocity** | `post_engagers` + `get_recent_engagers` | Boosts commenting back on people who engaged with us |
| **DMs + follow-ups** | `build_dm_from_template`, `dm_templates`, `dm_followups`, `process_user_followups` | Templated and voice-aligned; multi-touch sequences per-user paced |
| **Appreciation sources** (#968) | `APPRECIATION_SOURCES_ENABLED` | Recommendation/collaboration read STANDING lists, not event queues — an **undated card is SKIPPED** and only `APPRECIATION_LOOKBACK_DAYS` (30) counts. `appreciation_touches` is the durable CLAIM against double-thanking on the ~60s re-queue |
| **Message-thread ladder** | `linkedin/message_thread.py` (#731) | A route counts only when the thread is **provably open** — never class names, only href/aria-label/TEXT. `ThreadState` is three-valued; **UNKNOWN SKIPS**. Reading a thread and SENDING into one differ (#1030): `send_dm_now` NAVIGATES via `open_addressed_composer` and refuses unless `composer_recipient` names someone (`compose_url_for` must carry `recipient=` AND `profileUrn=`). Sent means the message LANDED (`_dm_send_landed`), never that Send took a click |
| **Owned-asset CTA loop** (#624) | `resolve_artifact_delivery` + `_queue_artifact_delivery` | The ONE map from a CTA to its asset, naming the CHANNEL — lead magnet is a comment-keyword mechanic paying out a DM, newsletter a subscribe LINK. Keyword delivery approval-gated (`source='artifact'`) |
| **Comment outcomes** (#628) | `sweep_comment_outcomes` + `utilities/comment_outcomes.py` | Read-only T+24h sweep, ONE row per comment. `visible_most_relevant` is three-valued (1 / 0 / NULL unreadable, excluded from the denominator). Demotion over threshold HOLDS that user's feed commenting + CRITICAL |
| **Suppression tripwire** (#629) | `auto_suppression_tripwire` + `utilities/suppression.py` | 2026 penalties are SILENT, so compare impressions-per-post against the user's OWN trailing 14-day median. A sustained drop pauses **engagement only** — posting is never gated. Thin baseline = `unknown`, never actioned; recovery is human |
| **Weekly group post** (#932) | `auto_draft_group_post` → `auto_post_to_group` | TWO beats with a review window between. Tue publishes the existing draft and generates NOTHING, so **a run with no READY draft publishes nothing**. Resting status is `ready` so silence ships it; ONE open draft per user, carried forward, never replaced |
| **Roster targets** (#962) | `comment_on_roster_posts`, `auto_follow_roster_target` | Posts but ZERO commentable cards records a blocked visit; a whole roster blocked records nothing. `roster_auto_follow` draws `max_follows_per_day` bounded by (never in) the account envelope, and clicks NOTHING unless the control names the page owner |
| **Roster connect escalation** (#979) | `advance_roster_connect` | blocked → follow → still blocked → `needs_connection` → (opt-in) ONE invite. `needs_connection` needs EVIDENCE (`following` + a blocked visit AFTER `followed_at`); a landed comment stands it down. Spends the SHARED `max_invites_per_day` at ≤ `ceil(remaining/3)`, ONE shot per target (`requested` written BEFORE dispatch). `ConnectStatus` is the ONE vocabulary |
| **Stale-invite withdrawal** (#969) | `linkedin/stale_invites.py`, `STALE_INVITE_WITHDRAWAL_ENABLED` | Withdrawing is ONE-WAY (~3 weeks before a re-invite), so reads fail CLOSED — an unreadable "Sent … ago" is NEVER stale, and only the row's OWN `Sent` line is parsed. `plan_withdrawals` decides the allowance BEFORE Chrome opens |
| **Company-page invites** (#732) | `linkedin/company_page_inviter.py` | A paced DAILY drip bounded by the SMALLEST of three ceilings — per-day cap, credit spread, live credit count. `plan_daily_invites` decides all of it BEFORE a Chrome session opens |
### Engagement configuration (`engagement_preferences` table, API in `api/main.py`, SPA in `ui/.../Account.tsx`)
- **Targeting:** include/exclude topics/keywords/authors, `min_reactions`, `max_post_age_hours`, LLM topic-relevance scoring.
- **Voice:** tone, `comment_length` (short/medium/long; default short), style, emoji/hashtag toggles.
- **Caps:** `max_comments_per_day`, `max_dms_per_day`; DM template editor with follow-up steps; Login Location (city/state geocoding via `utilities/geocoding.py`, admin-overridable).
- **Profile freshness** (`utilities/profile_refresh.py`, #1076): `POST /user/linkedin-profile/refresh` is the ONE on-demand re-scrape — `claim_profile_refresh` (Redis window, 1/user/day, fails OPEN) is taken BEFORE dispatch, then `update_stale_profile(force_refresh=True)` bypasses **both** profile caches (by-user AND by-URL) and re-distils the voice brief. Always **202**, never 429: a second press the same day is an expected no-op (DEBUG). Absent from `_AGENT_SESSION_SURFACE` — a headless token never spends a Chrome slot. Without it a profile edit waits for the ≤7-day `auto_refresh_profile_syntheses` beat.

### Marketing video tutorials (`utilities/marketing/video_tutorials.py`, beat `produce-feature-tutorial`)
- One declarative `TutorialFlow` per feature (routes + CSS anchors proving the screen rendered) → headless SPA capture → grounded script (`lem-medium`) → TTS → ffmpeg MP4 + `.srt` → 9:16 clip → YouTube upload. **Fail-closed, cheapest-first**: a missing anchor, unparseable script, profanity, over-cap narration or fabricated number aborts BEFORE any TTS/publish spend. Re-filmed only when the captured UI fingerprint changes (`assets/videos/tutorials/manifest.json`). OFF unless `TUTORIAL_VIDEOS_ENABLED`. `docs/marketing-video-tutorials.md`.
- **YouTube OAuth token** (`youtube_auth.py`, #742): the ONE place its state is decided — DB-first (`app_credentials`, installed via `POST /admin/youtube-token`, no deploy), `YOUTUBE_REFRESH_TOKEN` seeds it. `unknown` (Google unreachable) is NOT `needs_reauth` (4xx / lost scope — the only state that alerts). Weekly beat `youtube-token-check` IS the keep-alive vs the 6-month-disuse expiry — never drop it while the feature is off. `docs/youtube-publishing.md`.

### Anti-bot / session infra

| Surface | The ONE place | The invariant that bites | Doc |
|---|---|---|---|
| **Proxy + browser identity** | `utilities/proxy.py`, `_build_proxy_auth_extension_b64` | Per-user static residential proxy behind an in-memory MV3 auth extension — never URL-embedded credentials (MV2 background pages died in Chrome 149+) | — |
| **Cookie + PIN login** | `linkedin/verification_pin.py` | `li_at` is the DEFAULT engagement login since #745 | — |
| **Sign-in visibility** (#933) | `linkedin/login_status.py` | `_persist_session_cookies` is where both login paths meet, so it's where a sign-in is recorded. Redis-backed, fails open: `unknown` means nothing recorded, NOT a broken connection | `docs/linkedin-session-health.md` |
| **OAuth renewal** (#600) | `resolve_token_status` in `linkedin/token_refresh.py` | The ONE place token state is decided — SPA countdown and renewal beat read the same function. LinkedIn caps auth at 60 days, so the daily 08:30 beat is the only way a token outlives that. `days_remaining` is `None`, never 0, when unreadable | same |
| **429 / auth-wall** | `linkedin/rate_limit.py` | The breaker is a harder gate than pacing and is never a flag | — |
| **Secrets at rest** (#745) | `utilities/crypto.py`; `db.py` is the ONE caller | AES-256-GCM per user+column off `LEM_SECRET_KEY`, and the field-name constants are **AAD — renaming one orphans every row**. `ENCRYPTION_REQUIRED=true` in prod since 2026-08-07, so reads FAIL CLOSED; failed decrypt → None | `docs/secrets-at-rest.md` |
| **Identity + sessions** (#745 2b) | `api/main.get_session_user_id()` | `users.public_uid` is the identity; email is a movable ATTRIBUTE. `sessions.session_token` stores an **UNKEYED** `SHA-256(token)` — a rotated `LEM_SECRET_KEY` must never log everyone out — in an httpOnly cookie. **Since #914 EVERY `/api` route resolves its caller through it**: `require_session_user_id()` is it plus a 401; an `email`/`user_id`/`post_id` is a TARGET to authorise (403 + audited), never the actor; `db.user_owns_posts` FAILS CLOSED; a DB fault is **503**. **CSRF (#957):** a cookie-authenticated write must send `X-LEM-Client`. `API_ACCESS_TOKENS` is NON-BROWSER since #950 — never in the SPA build | `docs/identity-and-sessions.md` |
| **Docs surface** (#1020) | `_hide_admin_routes_from_schema()` | `/api/docs`, `/api/redoc`, `/api/openapi.json` (old paths 301). Every `/api/admin/*` operation is kept OUT of the published schema, derived from the route table so a new admin route inherits it. **Hidden ≠ gated** — auth is unchanged, Swagger just can't drive them. PUBLIC schema, so `ResponseModel[T]` (#1219): FastAPI serializes THROUGH `T`, always a CONTAINER type, and no operation may `$ref` the bare envelope. Unauthenticated `GET /health/deep` returns COUNTS only, `"status":"healthy"` first — a monitor contract | same, `docs/stack-watchdog.md` |
| **Strong auth + step-up** (#745 2c) | `utilities/auth_factors.py` (ceremonies in `webauthn_util.py`) | Once an account enrols a passkey or TOTP the email PIN is a **bootstrap** only; a passkey login is the only path arriving already stepped up. `sessions.last_verified_at` gates every credential-touching write — refusal is **403 `step_up_required`**, never 401. **The FIRST factor is free, every one after it is gated, removing one always is.** Attempts are durable and counted per ACCOUNT: 401 = wrong code, 400 = handle gone, 429 = budget spent | `docs/strong-authentication.md` |
| **Session scopes are SURFACES** (#905/#1026) | the same resolver | Refusal is 403 + audited. `extension` reaches only the ONE path the extension calls. `enroll` reaches only enrolment, which promotes it to `full` — **a hold is never a lockout**. **`agent`** is the headless credential: `_AGENT_SESSION_SURFACE` (queueing) only, TTL fixed at mint. It may queue but **NEVER approve** — THREE guards, because a row reaches APPROVED three ways. Surfaces match on PATH not method, so granting a read grants its writes — hence `PUT /user/engagement-preferences` is separately refused | `docs/identity-and-sessions.md` |
## Agent Working Method

Three practices wrap `ship-issue`'s branch → build → PR flow — one before code, one during it, one
before the PR:

- **Before writing code on a non-trivial issue** (`docs/spec-verifier-environment.md`, Karpathy's
  Spec/Verifier/Environment framework on this repo): nail testable acceptance criteria, name the
  check that proves success, locate the owning docs/skill/module — BEFORE `ship-issue` step 1.
  Skill: `spec-first`.
- **Hand token-heavy EXECUTION to Codex** (`codex@openai-codex`, enabled in `.claude/settings.json`):
  this session keeps the judgement, Codex does the grinding. Delegate a **bulk file edit** (a scripted
  `sed`/AST pass still beats both agents when the change is truly uniform), a **well-specced build**
  (sharpen it here with `spec-first` first if it is not), or a **bug still failing after 2 attempts** —
  two is a limit, because a third pass from the same context re-derives the same wrong model. NOT to
  Codex: deciding WHAT to build, reading a failure to work out what it means, anything touching a
  documented invariant, and the last look before the PR. Verify its output as you would your own — the
  CI gates do not care which model wrote the diff.
- **Agent quality gate — Gauntlet Loop** (`docs/gauntlet-loop.md`): optional pre-PR pass for a
  deliverable that needs a REAL bar, not just review — builder/critic pairs blind-compare against a
  named reference exemplar, loop until the build wins (capped at 3 rounds, then `needs-human`).
  First-class for `ui/`-touching or UX-sensitive issues (screenshot critique against this project's
  UX goals). Slots into `ship-issue` step 4, before the PR. Skill: `gauntlet-loop`.

## Testing Standards

- All new/modified code: ≥80% patch coverage enforced by Codecov.
- **TWO lanes** (#1215): **unit** (`tests/unit/`, mock ALL I/O — fixtures `mock_openai_client` / `mock_database_connection` / `mock_selenium_driver`, plus hermetic autouse guards) and **integration** (real MySQL + Redis). **No CI lane drives a browser** — `scripts/linkedin_live_validation.py` grades Selenium.
- Run: `poetry run pytest tests/unit -v --tb=short`; coverage: `poetry run pytest --cov=src/cqc_lem --cov-report=xml`.
- Markers, fixtures, lane selection: the **test-lanes** skill and `tests/README.md`.

## Observability

Track events via `utilities/observability.py` (`track_llm_call` / `track_task` / `track_api_call`).
Inside it there is ONE `posthog.capture`, in `_emit()`, and an event's property shape is declared in
the `EVENTS` registry (#1218) — a new event is an `EventSpec`, never a new capture. `label()` marks
a property a dashboard/ALERT filters on and forces it to a **string**: PostHog matches a filter on
the ingested type, so one boolean row silently stops the alert firing.
One row per surface below: the ONE module and the invariant that bites. The paragraph behind each row
is `docs/observability-map.md`; the last column holds rationale, contracts and edge cases.

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
| **Model-tier benchmarks** (#721) | `scripts/benchmark_models.py` | The suite scores a FIRST draft, production ships an n-th — `contract` checks are the floor, `repairable` advisory (#910); an all-errored run is REFUSED, never a scorecard of zeros (#923) | `docs/model-benchmarks/README.md` |
| **Content-quality telemetry** (#630) | `auto_nightly_content_quality` | The TREND LINE, not a gate — **unscored is never zero**, and it pauses nothing (safety is #629) | `docs/content-quality-telemetry.md` |
| **Surveys — NPS/CSAT** (#653) | PostHog Surveys + `utilities/surveys.py` | Type `api`, rendered headless in `PostHogSurveyModal.tsx`; ONE answer = TWO paths counted ONCE; `markSurveySeen()` advances the 30d wait | `docs/surveys.md` |

## CI Gates

The SIX contexts branch protection requires on `main` (verified against the API): `Unit Tests
(Python 3.12)`, `Integration Tests`, `UI Build`, `Migration Versions`, `GitGuardian Scan`,
`CodeQL PR Quality Gate`.

**One workflow per test lane** (`tests/README.md`), each owning the one Codecov flag `codecov.yml`
declares, all selecting `-m "not slow"`; never add a whole-suite workflow. `slow` tests are live
third-party probes — `slow-tests.yml` runs them nightly and can never gate a PR.

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
local dev → PR to main → CI gates pass → release-please tags vX.Y.Z
  → build-and-push.yml builds ghcr.io/christopherqueenconsulting/cqc-lem:vX.Y.Z → GHCR
  → SSH deploy to VPS runs scripts/deploy.sh vX.Y.Z (git checkout tag, flyway migrate,
    compose up, /health check, auto-rollback to .last_good_tag on failure)
```

- The stack launches with **both** compose files (`-f docker-compose.yml -f docker-compose.prod.yml`) — the prod overlay strips the dev bind-mount, so every app service runs the code baked into the image; editing files on disk does nothing until a new image ships. `web_app` is a tiny nginx **edge** routing to the active blue/green color; deploys are zero-downtime flips, releases batch 4x daily (05/11/17/23 UTC) — `docs/zero-downtime-deploys.md`. **`release:now`** ships a PR at merge instead of the next window (`docs/release-fast-lane.md`).
- **Runtime state (429 breaker, manual automation pause, reply-sweep cadence keys) lives in Redis**, not the DB or containers — it survives deploys.
- A **local hotfix deploy** fallback exists when CI/release is blocked; it diverges prod from `main` until the fix lands via the normal PR flow. Compose layering + image refs: `docs/DEPLOYMENT.md`.

## Known Gotchas

- **Legacy drift:** `get_docker_driver()` targets `selenium/standalone-chrome:latest` at 4444, not a
  Grid hub+node; `ai_helper.py` uses tier aliases, not a hardcoded `gpt-4o-mini`.
- **Emoji in Selenium:** ChromeDriver `send_keys` throws on non-BMP emoji — strip them first with
  `strip_non_bmp()` (`utilities/linkedin_formatter.py`).
- **ENUM columns:** `logs.action_type` and other status columns are MySQL ENUMs — adding a value
  needs a migration. New migrations use **TIMESTAMP** versions so two branches never collide; the
  **db-migration** skill, `compose/local/database/migrations/README.md`.
- **LinkedIn SDUI** (`docs/sdui-selenium-notes.md`, `docs/sdui-probe-coverage.md`): the old `urn:`,
  `feed-shared-*` and `comments-comment-*` anchors are gone — prefer `data-testid` / `aria-label`.
  Three fix invariants (#1013): **success is the OUTCOME being present, never a click having
  landed**; **never click a control whose label names a different entity than the target** (#1012);
  **zero items is not "nothing to do" until the page agrees** (`_report_zero_walk`). Every surface
  has a read-only probe flag + a weekly sweep filing ONE issue per drift. The comment composer has NO
  `<form>`; the sticky nav steals clicks from an unfocused composer; every composer lookup is scoped
  to its OWN post, and a miss is a DEBUG no-op. A PERMALINK runs the SAME engine (#966) — the card is
  picked by the permalink's URN, the reaction happens BEFORE the comment, and a comment that doesn't
  land is a FAILURE row.
- **Unified content core** (`docs/content-core.md`): newsletters, posts AND comments draw framework,
  research and alignment from `content_{framework,research,alignment}.py` — never add a per-content-type
  prompt helper. Comments carry a quality contract + similarity gate (#617) that SKIPS the post after
  `COMMENT_GATE_MAX_ATTEMPTS` failed regenerations; POSTS are graded by the same engine
  (`post_similarity_report`, #1265) — embedding cosine (`POST_EMBEDDING_SIMILARITY_MAX`) first,
  degrading to `POST_SIMILARITY_MAX` token overlap, ONE retry then keep, and the `similarity` gate
  names the measure that fired. One measure vocabulary with the nightly telemetry, never two.
  **Story bank** (#620) is the FACT half, the **deck reference
  gate** (#728) the save-worthiness half, **slop lint** (#625) BLOCKS five HARD checks,
  WARNs the rest, severity PER SURFACE (`SURFACE_SEVERITIES`): `canned_scaffold` is WARN on a post,
  HARD on a newsletter (#1285). `{POST,NEWSLETTER}_BANNED_SCAFFOLDS` are ONE list the prompt names and
  the lint greps, so the two cannot drift — `docs/content-quality-audits/{text,newsletter}.md`.
- **Content mix (70/20/10)** (same doc): every planned post carries a class in `posts.content_mix` —
  `value` 70% / `authority` 20% / `promo` 10% (forced `case_snapshot`). **A promo CTA is always an
  ARTIFACT** (lead magnet / newsletter); a meeting ask is banned in prompts, repaired
  deterministically, and any that survives HOLDS the post at PENDING via the `meeting_cta` gate.
- **Stale lazy chunks after a deploy** (#743, `docs/spa-deploy-freshness.md`): a tab open across a
  release fetches a chunk hash the new image no longer has. Three layers — asset retention from a
  shared archive volume, a loop-guarded one-shot reload on import failure, `/api/app-info` polling
  that prompts rather than reloads.
## Agent pipeline (v2)

The runner is the **`lem-agentd` daemon** (`scripts/agent-pipeline/v2/`), NOT `tick.sh` — v1 is only
a heartbeat-gated failsafe. State machine, the full `decide()` decision table, the GitHub field
combinations it is not yet defined for, and the deploy path (the pipeline is **not** in the Docker
image): **`docs/agent-pipeline-v2.md`**. `test_agent_pipeline_v2_decision_table.py` enforces that
table — a new branch without a documented row fails the build. Labels are the human contract:
`docs/AGENT_WORKFLOW_PLAYBOOK.md`.

## Git Safety & Multi-Agent Concurrency Rules
- **Every agent gets its OWN worktree — always.** `isolation: "worktree"` on the Agent call;
  `.claude/agents/*.md` frontmatter carries it too. Agents sharing a checkout WILL clobber each other
  — three once did, one switching the branch under the others inside a minute. `lib/run_lane.sh`
  enforces it for the pipeline: `cd ""` SUCCEEDS in bash, so an empty worktree path silently runs the
  agent in the shared tree instead of failing.
- **NEVER put `model:` in an agent definition.** It overrides the CLI `--model`, and a subagent
  inherits the parent's `ANTHROPIC_BASE_URL` — so on the Ollama lane (LiteLLM serves only `lem-*`) a
  pinned `opus` 400s in 7s **while the parent exits rc=0**, recorded as a healthy run. Pin tools and
  `--effort`, never the model: `scripts/agent-pipeline/docs/agent-pipeline-routing.md`.
- **One venv, many worktrees:** the editable-install `.pth` is mutable and the last `poetry install`
  anywhere wins, so `poetry run python -c "import cqc_lem…"` may read a DIFFERENT worktree. Use
  `PYTHONPATH=src` for standalone scripts and print `__file__` to prove which tree loaded. `pytest`
  is unaffected (`pythonpath` is rootdir-relative).
- **Reproduce CI locally** with an empty `.env` and `src/cqc_lem/ui/dist` moved aside. A dev `.env`
  masks real failures (unset `DB_PORT` → `int(None)` → `TypeError`, uncaught by `except
  mysql.connector.Error`); a built SPA causes a false `test_docs_surface` failure.
- **Fresh state:** before generating ANY code edit, run `git status` and read the target file. Never edit from memory — another agent may have changed it under you.
- **Micro-branching:** never edit a shared branch asynchronously; start each task on its own branch (`git checkout -b feature/claude-<task-name>`).
- **Atomic commits:** stage + commit each completed sub-task with a clean conventional-commit message.
- **Conflict avoidance:** if working-tree changes clash with your target files, halt, `git stash`, pull current state, resolve, re-apply.
- **Branch cleanup:** merged feature branches auto-delete (`delete_branch_on_merge=true`); orphans swept weekly by `.github/workflows/stale-branches.yml`, with a 48-hour grace window protecting active agent work. `docs/branch-cleanup.md`.
- **A label is not an access control** (`docs/contribution-security.md`): this repo is PUBLIC and the agent pipeline runs with the owner's credentials, so `agent:ready` / `release:now` are verified by **provenance, not presence**. The runner checks TWO independent things — the AUTHOR has standing (`author_trusted`) and the label was applied by an allowlisted actor (`label_actor_trusted`, timeline API) — plus `pr_is_upstream` on PR lanes; an unreadable answer REFUSES. Writers of `agent:ready` are gated at source: `triage_issues.py` grants it only to trusted authors; the feedback loop (unauthenticated `POST /api/feedback`) **never** does. `.github/CODEOWNERS` guards every control surface, and the pipeline's credential has **no `workflows` permission** — the hard control, since agent and owner share one identity.
