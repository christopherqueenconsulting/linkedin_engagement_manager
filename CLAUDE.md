# LinkedIn Engagement Manager — Claude Code Context

## Project Overview

LinkedIn Engagement Manager (LEM) automates LinkedIn engagement end to end: Selenium-based scraping and feed interaction, AI-generated content (via LiteLLM proxy routing to OpenAI / Claude / Ollama / OpenRouter), Celery task queue, React SPA frontend, MySQL persistence, and FastAPI backend.

Two pillars:

- **Content generation & scheduling** — a 30-day content plan of buyer-journey-staged posts (thought leadership, industry-news commentary, personal story, engagement prompts, carousels, native video, blog summaries) auto-scheduled around peak/golden hours, with sentiment checks and a preview/approval workflow.
- **Engagement automation** — feed commenting, replies on the user's own posts, seed first comments, appreciation/outreach DMs with multi-touch follow-ups, and monthly company-page invitations — all driven by per-user targeting, voice/tone, and per-day cap preferences.

See **Feature Areas** below for the code paths behind each capability.

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
├── api/           FastAPI app (main.py, routers) — engagement_preferences, DM template, PIN endpoints
├── app/           Celery tasks
│   ├── run_scheduler.py     post scheduling around golden/peak hours
│   ├── run_automation.py    feed commenting, replies, seed/pin, DMs + follow-ups
│   ├── run_content_plan.py  30-day buyer-journey content plan
│   ├── generate_variants.py media variant generation
│   └── my_celery.py         Celery app + beat schedule
├── utilities/
│   ├── ai/        LiteLLM-backed AI helpers (ai_helper.py, client.py)
│   │   ├── content_framework.py  ONE blueprint core (archetype/hook/CTA menus per content type + shared variety engine)
│   │   ├── content_research.py   ONE research layer (lem-research→Perplexity fallback; per-type cost toggles)
│   │   ├── content_alignment.py  ONE alignment core (voice synthesis + prefs + LEM purpose + promo policy)
│   │   ├── story_bank.py         ONE fact layer (the user's own anecdotes/numbers — the only permitted specifics)
│   │   └── slop_lint.py          ONE deterministic AI-slop lint (no LLM) run on every surface
│   ├── linkedin/  Selenium automation
│   │   ├── scrapper.py            profile/feed scraping
│   │   ├── poster.py              publishing posts/carousels/video
│   │   ├── company_page_inviter.py  monthly company-page invites
│   │   ├── verification_pin.py    email-PIN LinkedIn verification flow
│   │   ├── rate_limit.py          429/auth-wall backoff
│   │   └── helper.py, profile.py, token_refresh.py
│   ├── marketing/ Outbound production
│   │   └── video_tutorials.py  automated SPA tutorial videos (capture→script→TTS→ffmpeg→YouTube)
│   ├── db.py      All database access (no raw SQL outside this file)
│   ├── proxy.py   Per-user static residential proxy resolution
│   ├── geocoding.py  Login Location city/state geocoding
│   ├── logger.py  Structured logger — log_info/log_error/etc. preferred over myprint()
│   └── selenium_util.py  get_docker_driver() + MV3 proxy-auth extension builder
├── ui/            React SPA (src/, dist/ is built output) — Account.tsx holds engagement prefs
└── aws/           AWS CDK stacks
tests/
├── unit/          Fast tests — mock all I/O
├── integration/   Require MySQL + Redis service containers
└── e2e/           Require selenium/standalone-chrome
compose/local/database/migrations/  Flyway migrations (through V50)
.litellm/
├── config.yaml    LiteLLM model aliases and routing config
└── complexity_router.py  Pre-call hook for lem-router model
```

## Code Conventions

- **Logging:** Never use `print()`. Use the structured logger from `cqc_lem.utilities.logger`. Prefer the typed helpers over the legacy `myprint()` shim:

  | Function | Level | When to use |
  |---|---|---|
  | `log_debug(msg, **ctx)` | DEBUG | Verbose detail: LLM calls, Selenium steps, DB queries |
  | `log_info(msg, **ctx)` | INFO | Normal task progress and state transitions |
  | `log_warning(msg, exc=None, **ctx)` | WARNING | Recoverable failures, fallbacks, degraded paths |
  | `log_error(msg, exc=None, **ctx)` | ERROR | Task-level failures — automatically sent to PostHog |
  | `log_critical(msg, exc=None, **ctx)` | CRITICAL | Fatal conditions — automatically sent to PostHog |
  | `myprint(msg, debug=False)` | INFO/DEBUG | Legacy shim — still works, avoid in new code |

  Pass structured context as keyword args. Supported fields: `user_id`, `task_id`, `task_name`, `post_id`, `action_type`, `duration_ms`, `ai_model`, `api_provider`, `http_status`. `log_error` / `log_critical` accept `exc=` to capture the full exception and stack trace.

  ```python
  from cqc_lem.utilities.logger import log_info, log_warning, log_error

  log_info("Scheduled post", post_id=post_id, user_id=user_id, task_name="auto_check_scheduled_posts")
  log_warning("Perplexity unavailable, falling back to GoogleNews", exc=e, api_provider="perplexity")
  log_error("Automation task failed", exc=e, user_id=user_id, task_name="automate_commenting")
  ```

  Log level and PostHog threshold are configurable via env vars:
  - `LOG_LEVEL` — overall logging level (default: `INFO`)
  - `POSTHOG_LOG_LEVEL` — minimum level forwarded to PostHog (default: `ERROR`)

- **Type hints:** Required on all function signatures.
- **Enums:** Use `PostStatus`, `PostType`, `LogActionType` from `db.py` for status fields — never raw strings.
- **Imports:** Absolute imports from `cqc_lem.*` throughout.
- **Database:** All DB access goes through functions in `utilities/db.py`. No raw SQL in other modules.
- **Secrets:** Never hardcode. Use `.env` with `load_dotenv()`. See `.env.example` for required variables.
- **Comments:** Only add a comment when the WHY is non-obvious. No docstring blocks.

## AI Call Pattern

All LLM calls go through LiteLLM proxy via `utilities/ai/client.py`:

```python
from cqc_lem.utilities.ai.client import client
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

**Cost-aware down-routing** (`utilities/routing_policy.py`, `utilities/cost_routing.py`): the router
can additionally route a tier ONE step down for the treatment cohort of an active cost/quality
experiment. `routing_policy.py` is the shared decision core — the app imports it, and docker-compose
mounts that same file into the LiteLLM container — so it must stay **stdlib-only** (no `cqc_lem.*`
imports). Off unless BOTH `COST_ROUTING_ENABLED` and `COST_AWARE_ROUTING_ENABLED` are set. See
`docs/cost-performance-margin-plan.md` §D.1.1.

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
- **Cadence (issue #621):** the plan is NOT one post a day. It fills the `posts_per_week` slots (2–7, default 3) of a **fixed day-type calendar** (`POST_DAY_TYPES` in `content_framework.py` — Tue build-receipt / Wed story / Thu spiky POV at the default), which also supplies each post's buyer stage AND narrows its archetype family. Times are clamped to waking hours (`POST_HOUR_MIN/MAX` in `utilities/utils.py`), jittered ±15–30 min, and held ≥24h apart.
- Self-healing carousels (stale/errored carousels re-generated into branded slides) and asset backfill.
- `PostType` is `text` / `carousel` / `video`; `PostStatus` includes `error` for generation/posting failures needing manual fix.

### Engagement automation (`app/run_automation.py`)
- **Feed commenting** rebuilt for LinkedIn's SDUI: resilient `find_first`/`click_first`/`find_all_first` selectors (`utilities/linkedin/helper.py`); inline compose + submit; **recency-dominant scoring matrix** (`_score_feed_post` = recency + relevance + reciprocity + activity) with post-age (`_post_age_minutes`) and social-count (`_post_social_counts`) extraction, best-effort "Recent" feed sort (`_switch_feed_to_recent`); targeting filters + per-day caps + voice/tone. Runs pre-post (≈15 min before each scheduled post) and daily at a golden hour.
- **Replies** to comments on the user's own posts (`automate_reply_commenting`); **seed a first comment** on own posts (`auto_seed_comment_on_post`).
- **Reciprocity tracking** via the `post_engagers` table — boosts commenting back on people who engaged with us (`get_recent_engagers`).
- **DMs**: appreciation (connection / recommendation / collaboration), profile-viewer outreach, and **multi-touch follow-up sequences** — all templated and voice-aligned (`build_dm_from_template`, `dm_templates`, `dm_followups`, `process_user_followups`).
- **DM conversation auto-nurture** (`_nurture_after_reply`, `utilities/ai/dm_nurture.py`): a reply used to END a sequence — now it's classified (interested / objection / not-now / disinterest / neutral) and becomes an **approval-gated** context-aware next message queued as a `pending` row in `scheduled_dms` (`source='nurture'`), one open draft per thread, per-day draft cap, explicit disinterest stops the thread for good.
- Monthly **company-page invitations** (`utilities/linkedin/company_page_inviter.py`).

### Engagement configuration (`engagement_preferences` table, API in `api/main.py`, SPA in `ui/.../Account.tsx`)
- Targeting: include/exclude topics/keywords/authors, `min_reactions`, `max_post_age_hours`, plus LLM topic-relevance scoring.
- Voice: tone, `comment_length` (short/medium/long; default short), style, emoji/hashtag toggles.
- Caps: `max_comments_per_day`, `max_dms_per_day`; DM template editor with follow-up steps; Login Location (city/state geocoding via `utilities/geocoding.py`, with admin override).

### Marketing video tutorials (`utilities/marketing/video_tutorials.py`, beat `produce-feature-tutorial`)
- One declarative `TutorialFlow` per feature (routes + the CSS anchors that prove the screen rendered) → headless SPA capture via `get_docker_driver()` → grounded script (`lem-medium`) → TTS voice-over (OpenAI `lem-tts` by default, ElevenLabs behind `TUTORIAL_TTS_PROVIDER`) → ffmpeg MP4 with branded intro/outro + `.srt` → 9:16 clip → YouTube Data API v3 upload.
- **Fail-closed**: a missing UI anchor, an unparseable script, profanity, an over-cap narration or a fabricated number aborts BEFORE any TTS/publish spend. Cost is attributed per part (script tokens, TTS characters, render minutes) and totalled on the manifest record.
- State lives in `assets/videos/tutorials/manifest.json` (no schema change); the SPA embeds it via `TutorialVideos.tsx`. Weekly cadence, and a flow is re-filmed only when its captured UI fingerprint changes. OFF unless `TUTORIAL_VIDEOS_ENABLED`.

### Anti-bot / session infra
- Per-user static residential proxy (`utilities/proxy.py`) + an in-memory **MV3 proxy-auth extension** (`selenium_util.py`, MV2 background pages are disabled in current Chrome).
- Cookie persistence and an email-PIN LinkedIn verification flow (`utilities/linkedin/verification_pin.py`).
- 429 / auth-wall backoff and resilience (`utilities/linkedin/rate_limit.py`).

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

Track events via `utilities/observability.py`:

```python
from cqc_lem.utilities.observability import track_llm_call, track_task, track_api_call
```

PostHog receives LLM usage (model, tokens, latency, cost) and Celery task metrics for fine-tuning decisions.

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
- Image ref is `${DOCKER_IMAGE_NAME}:${IMAGE_TAG:-latest}`, both set in `/opt/lem/.env` (git-ignored, lives on the box); `scripts/deploy.sh` exports `IMAGE_TAG` per-deploy. App services sharing the image: `web_api_blue`/`web_api_green` (the FastAPI app — blue/green pair), `celery_worker`, `celery_worker_selenium`, `celery_worker_selenium_prepost`, `celery_worker_selenium_outreach`, `celery_worker_selenium_content`, `celery_beat`, `flower`. In PROD, `web_app` is a tiny nginx **edge** (the stable name the Cloudflare tunnel targets) that routes to the active color; deploys are zero-downtime blue/green flips and releases batch 4x daily (05/11/17/23 UTC) — see `docs/zero-downtime-deploys.md`. In DEV, `web_app` is still the FastAPI container directly. Infra services (`mysql`, `redis`, `selenium-chrome`, `litellm`, `cloudflared`, `flyway`) use their own upstream images.
- **Runtime state (429 breaker, manual automation pause, reply-sweep cadence keys) lives in Redis**, not the DB or containers — it survives deploys. Inspect/repair it by calling the real functions in the container, e.g. `docker exec celery_worker_selenium python -c "from cqc_lem.utilities.linkedin.rate_limit import clear_rate_limit, pause_automation; ..."`, so the correct Redis URL is used.
- **Local hotfix deploy (fallback when CI/release is too slow or blocked):** build a thin overlay image `FROM` the running release tag that only `COPY`s the changed `src` files (guarantees identical deps, seconds not minutes), then on the box set `/opt/lem/.env` `IMAGE_TAG=<hotfix-tag>` and `cd /opt/lem && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --pull never <app-services>`. Keep the prior release image locally for instant rollback (`IMAGE_TAG=vX.Y.Z`). **This diverges prod from `main`** — the fix MUST still land via PR→release, or the next release will REVERT it. Requires `sudo` for the Docker socket on this box.

## Known Gotchas

- `get_docker_driver()` previously connected to Selenium Grid hub+node. It now connects to `selenium/standalone-chrome:latest` at port 4444.
- `ai_helper.py` had all functions hardcoded to `model="gpt-4o-mini"` — they now use tier aliases.
- `run_scheduler.py:22` previously had a `raise ValueError("This is a test error")` — this was removed in M3.
- PostHog replaces Prometheus + Jaeger (both removed from docker-compose).
- `linkedin-preview` service (external) was removed — preview is now the native `LinkedInPostPreview.tsx` component.
- **LinkedIn SDUI:** the old `urn:`, `feed-shared-*`, and `comments-comment-*` DOM anchors are gone. Prefer `data-testid` / `aria-label` selectors via `find_first`/`click_first`. The comment composer has NO `<form>` — "submit" means clicking the Comment/Post button next to the composer (`_composer_submitted`), and the comment overflow "…" menu is hover-hidden.
- **Emoji in Selenium:** ChromeDriver `send_keys` throws on non-BMP emoji — strip them before typing with `_strip_non_bmp()`.
- **ENUM columns:** `logs.action_type` (and other status columns) are MySQL ENUMs. Adding a new value requires a migration — e.g. V37 added `'followup'` to `logs.action_type`. Migrations live in `compose/local/database/migrations/` and currently run through **V52** (V48 added `profiles.synthesis`/`synthesis_generated_at` — the cached durable voice brief, V49 added `newsletter_editions.subject` for topic dedup, V50 added `newsletter_editions.format`/`hook_style`/`opening_line`/`blueprint` — the edition SHAPE history for format/hook/opener rotation, V51 added `posts.archetype`/`hook_style` — the post-side shape history for the same rotation, V52 widened `engagement_preferences.tone` VARCHAR(64)→VARCHAR(255): the whole engagement upsert is one row, so an over-long tone raised MySQL 1406 and silently rolled back ALL sections — DM templates persisted only because they're a separate table, V53 added the `scheduled_dms` table for the DM scheduler — issue #306).
- **Unified content core:** newsletters, posts, AND comments all draw framework (blueprints/variety), research, and alignment from `utilities/ai/content_framework.py` / `content_research.py` / `content_alignment.py`. Do NOT add parallel per-content-type prompt helpers — extend the shared menus/engine instead. Comment research is OFF by default (`COMMENT_RESEARCH_ENABLED`) because comments run at high volume; the target post is their grounding. Comments also carry their own **quality contract + similarity gate** (issue #617, same module): a draft must reference a specific claim from the target post, add one of {own experience, data point, respectful disagreement, genuine question}, run ≥2 sentences, and never open with validation filler — and must not near-duplicate the user's last 50 posted comments (`COMMENT_SIMILARITY_MAX`, embedding cosine via `lem-embedding`, token-overlap fallback). A failing draft is regenerated up to `COMMENT_GATE_MAX_ATTEMPTS` times and then the post is SKIPPED — `generate_ai_response` returns `None`, never a failing comment. The post-history uniqueness engine (opener/subject avoidance steering + the deterministic `POST_SIMILARITY_MAX` review gate in `create_text_post`, mirroring the newsletter's V49/V50 dedup) also lives in `content_framework.py` — and trend-based post subjects are ANCHORED to the user's `focus_topics` (rotated per post_id via `select_focus_topic` in `content_alignment.py`), not just their profile industry. The **story bank** (issue #620, `story_bank.py` + the `story_bank` table) is the FACT half of that core: `create_text_post` selects ONE of the user's own entries per post (relevance, then least-used/longest-unused rotation) and its facts are the only personal specifics the writer may state — an empty or irrelevant bank ships an explicit no-fabrication fallback (industry observation) instead of an invented anecdote, and a first-person specific that traces to no supplied source regenerates once (`POST_FABRICATION_REGEN_ENABLED`). `profiles.synthesis` still feeds VOICE; the bank feeds FACTS. Two **save-targeted** post archetypes live in the same `POST_FORMATS` menu (issue #619): `build_receipt` and `resource_compendium`. They are marked `save_targeted` (so scheduling can prefer them via `select_blueprint(prefer_save_targeted=True)`) and `fact_anchored`, which narrows their hook menu to `NUMBER_LED_HOOK_STYLES` (lead with a real number, ~140-char mobile budget) and turns on the **no-fabrication guard**: the writer may only state a specific that a VERIFIED fact backs, otherwise it must ship as a `[[LABEL: …]]` placeholder. Those verified facts are the story bank's, at two different widths on purpose — the WRITER's allow-list is only the ONE entry this post was anchored to (carried on the blueprint as `fact_anchors`, since a number from some other entry was never in its prompt), while the CHECKERS (`_review_generated_post` and the `fact_grounding` gate, via `run_content_plan._fact_anchors`) count EVERY active entry, because a number out of the user's own material is by definition not one the model invented. `fact_grounding_report` grades the draft deterministically; an invented number costs one regeneration and then holds the post PENDING behind the `fact_grounding` quality gate, and unfilled placeholders hold it too until the author fills them in (a re-score of human-EDITED text treats the author's own numbers as verified, or the hold could never clear). An empty bank means every such draft is placeholder-only and approval-gated. Carousels draw from the same menu via `carousel_blueprint_directive` and persist their shape into the same V51 rotation history — but with an EMPTY bank the fact-anchored archetypes are taken off the carousel menu entirely (`select_blueprint(exclude_formats=fact_anchored_formats("post"))`), because a carousel bakes its text into rendered slide IMAGES and a `[[…]]` placeholder there can never be edited away. Tool/model version numbers ("GPT-4o", "Postgres 16") are NOT graded as claims — the receipt's structure asks for the exact stack by name. The **deterministic slop lint** (issue #625, `slop_lint.py`) is the cheap explainable layer under the two LLM passes (`humanize_text` #416, `score_authenticity` #382): pure regex/statistics, ~0.5ms, run on posts AND comments AND DMs AND newsletter editions AND group posts after humanization. Five HARD checks (banned lexicon pileup, the "it's not X, it's Y" contrastive frame, "here's the kicker" ta-da transitions, bait/reflex closers, emoji-bullet listicles) are regenerated up to `SLOP_LINT_MAX_ATTEMPTS` and then BLOCK — a post is held at PENDING behind the `ai_slop` quality gate with the exact constructions named, a feed comment is SKIPPED (it shares the comment gate's retry budget), and a DM/newsletter/group post ships with a logged reason because those have no review queue and dropping them breaks the sequence. Four WARN checks (em-dash density, rule-of-three, burstiness, rhetorical hook) are advisory and never hold anything — they have real false positives (a genuine list of three tools reads like a rule-of-three). The wordbank is `content_alignment.AI_TELL_WORDS`, NOT a second copy, and the bait check honours the same lead-magnet `exempt_keyword` `strip_engagement_bait` does, or every "Comment YES" CTA would hold its own post.
- **Content mix (70/20/10) governor:** every planned post carries a mix class in `posts.content_mix` — `value` (70%, audience value, sells nothing) / `authority` (20%, expertise education, sells nothing) / `promo` (10%). The classes are assigned deterministically in `content_alignment.assign_content_mix` (promo cadence `PROMO_EVERY_N_POSTS`, clamped to 10–30 so promo can never exceed 10%), the promo slot claims a TEXT post and is forced into the `case_snapshot` blueprint, and the class rides into the prompt via `alignment_directive(..., content_mix=)`. **Promo CTAs are always an ARTIFACT** (lead magnet / newsletter) — a meeting ask is banned in the prompts (`ARTIFACT_CTA_POLICY`, injected by `cta_policy_directive`), repaired deterministically by `replace_meeting_ask_cta`, and any that survives HOLDS the post at PENDING via the `meeting_cta` quality gate. Compliance is reported on `/user/engagement-analytics` (`content_mix`) and rendered on the Dashboard.
- **Proxy auth:** proxies are authenticated by the runtime MV3 extension (`_build_proxy_auth_extension_b64`), not by URL-embedded credentials — MV2 background pages that used to do this are disabled in Chrome 149+.

## Git Safety & Multi-Agent Concurrency Rules
- **Fresh State Enforcement**: Before executing or generating any code edit, you MUST explicitly run `git status` and a file read command (e.g., `cat <filename>`) to verify no hidden or uncommitted upstream modifications exist. Never rely on your internal conversation memory for file contents.
- **Micro-Branching Workflow**: Do not make edits directly on shared branches while working asynchronously. When starting a distinct task, automatically spin up a task-specific feature branch (`git checkout -b feature/claude-<task-name>`).
- **Atomic Commits**: For every completed sub-task or successful implementation block, automatically stage and commit your files with a clean, concise descriptive message (e.g., `git add . && git commit -m "feat(api): implement active sub-agent locking mechanism"`). 
- **Conflict Avoidance**: If you detect changes in the working directory that clash with your active target files, immediately halt, stash your progress (`git stash`), pull down the current state, and safely resolve the differences before re-applying your changes.
