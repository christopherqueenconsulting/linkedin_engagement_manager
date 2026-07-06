# LinkedIn Engagement Manager — Claude Code Context

## Project Overview

LinkedIn Engagement Manager (LEM) automates LinkedIn engagement end to end: Selenium-based scraping and feed interaction, AI-generated content (via LiteLLM proxy routing to OpenAI / Claude / Ollama / OpenRouter), Celery task queue, React SPA frontend, MySQL persistence, and FastAPI backend.

Two pillars:

- **Content generation & scheduling** — a 30-day content plan of buyer-journey-staged posts (thought leadership, industry-news commentary, personal story, engagement prompts, carousels, native video, blog summaries) auto-scheduled around peak/golden hours, with sentiment checks and a preview/approval workflow.
- **Engagement automation** — feed commenting, replies on the user's own posts, seed-and-pin first comments, appreciation/outreach DMs with multi-touch follow-ups, and monthly company-page invitations — all driven by per-user targeting, voice/tone, and per-day cap preferences.

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
│   ├── linkedin/  Selenium automation
│   │   ├── scrapper.py            profile/feed scraping
│   │   ├── poster.py              publishing posts/carousels/video
│   │   ├── company_page_inviter.py  monthly company-page invites
│   │   ├── verification_pin.py    email-PIN LinkedIn verification flow
│   │   ├── rate_limit.py          429/auth-wall backoff
│   │   └── helper.py, profile.py, token_refresh.py
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
compose/local/database/migrations/  Flyway migrations (through V48)
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
| `lem-router` | Auto-routes by prompt complexity via `LEMComplexityRouter` |

See `ai_helper.py` for the per-function model assignment.

## Selenium Pattern

Always use `get_docker_driver()` from `selenium_util.py`. It connects to `selenium-chrome:4444`, polls readiness, and sets 1920×1080. Never instantiate `webdriver.Chrome()` directly.

Use `click_element_wait_retry()` for all click interactions — it handles transient DOM timing issues.

## Feature Areas

### Content generation & scheduling (`app/run_content_plan.py`, `app/run_scheduler.py`, `utilities/ai/ai_helper.py`)
- AI content by buyer-journey stage (awareness / consideration / decision): thought-leadership, industry-news commentary, personal-story, engagement-prompt posts, carousels (educational / case-study / product-demo / insights), native video, and blog summaries.
- 30-day content plan with balanced post-type distribution; auto-scheduling around golden/peak hours.
- Self-healing carousels (stale/errored carousels re-generated into branded slides) and asset backfill.
- `PostType` is `text` / `carousel` / `video`; `PostStatus` includes `error` for generation/posting failures needing manual fix.

### Engagement automation (`app/run_automation.py`)
- **Feed commenting** rebuilt for LinkedIn's SDUI: resilient `find_first`/`click_first`/`find_all_first` selectors (`utilities/linkedin/helper.py`); inline compose + submit; **recency-dominant scoring matrix** (`_score_feed_post` = recency + relevance + reciprocity + activity) with post-age (`_post_age_minutes`) and social-count (`_post_social_counts`) extraction, best-effort "Recent" feed sort (`_switch_feed_to_recent`); targeting filters + per-day caps + voice/tone. Runs pre-post (≈15 min before each scheduled post) and daily at a golden hour.
- **Replies** to comments on the user's own posts (`automate_reply_commenting`); **seed + pin a first comment** on own posts (`auto_seed_comment_on_post` → `_pin_own_comment`).
- **Reciprocity tracking** via the `post_engagers` table — boosts commenting back on people who engaged with us (`get_recent_engagers`).
- **DMs**: appreciation (connection / recommendation / collaboration), profile-viewer outreach, and **multi-touch follow-up sequences** — all templated and voice-aligned (`build_dm_from_template`, `dm_templates`, `dm_followups`, `process_user_followups`).
- Monthly **company-page invitations** (`utilities/linkedin/company_page_inviter.py`).

### Engagement configuration (`engagement_preferences` table, API in `api/main.py`, SPA in `ui/.../Account.tsx`)
- Targeting: include/exclude topics/keywords/authors, `min_reactions`, `max_post_age_hours`, plus LLM topic-relevance scoring.
- Voice: tone, `comment_length` (short/medium/long; default short), style, emoji/hashtag toggles.
- Caps: `max_comments_per_day`, `max_dms_per_day`; DM template editor with follow-up steps; Login Location (city/state geocoding via `utilities/geocoding.py`, with admin override).

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

## Known Gotchas

- `get_docker_driver()` previously connected to Selenium Grid hub+node. It now connects to `selenium/standalone-chrome:latest` at port 4444.
- `ai_helper.py` had all functions hardcoded to `model="gpt-4o-mini"` — they now use tier aliases.
- `run_scheduler.py:22` previously had a `raise ValueError("This is a test error")` — this was removed in M3.
- PostHog replaces Prometheus + Jaeger (both removed from docker-compose).
- `linkedin-preview` service (external) was removed — preview is now the native `LinkedInPostPreview.tsx` component.
- **LinkedIn SDUI:** the old `urn:`, `feed-shared-*`, and `comments-comment-*` DOM anchors are gone. Prefer `data-testid` / `aria-label` selectors via `find_first`/`click_first`. The comment composer has NO `<form>` — "submit" means clicking the Comment/Post button next to the composer (`_composer_submitted`), and the comment overflow "…" menu is hover-hidden.
- **Emoji in Selenium:** ChromeDriver `send_keys` throws on non-BMP emoji — strip them before typing with `_strip_non_bmp()`.
- **ENUM columns:** `logs.action_type` (and other status columns) are MySQL ENUMs. Adding a new value requires a migration — e.g. V37 added `'followup'` to `logs.action_type`. Migrations live in `compose/local/database/migrations/` and currently run through **V48** (V46 added the `commented_posts` at-most-once claim ledger, V47 added engagement focus_topics/business_goals/personal_goals, V48 added `profiles.synthesis`/`synthesis_generated_at` — the cached durable voice brief).
- **Proxy auth:** proxies are authenticated by the runtime MV3 extension (`_build_proxy_auth_extension_b64`), not by URL-embedded credentials — MV2 background pages that used to do this are disabled in Chrome 149+.

## Git Safety & Multi-Agent Concurrency Rules
- **Fresh State Enforcement**: Before executing or generating any code edit, you MUST explicitly run `git status` and a file read command (e.g., `cat <filename>`) to verify no hidden or uncommitted upstream modifications exist. Never rely on your internal conversation memory for file contents.
- **Micro-Branching Workflow**: Do not make edits directly on shared branches while working asynchronously. When starting a distinct task, automatically spin up a task-specific feature branch (`git checkout -b feature/claude-<task-name>`).
- **Atomic Commits**: For every completed sub-task or successful implementation block, automatically stage and commit your files with a clean, concise descriptive message (e.g., `git add . && git commit -m "feat(api): implement active sub-agent locking mechanism"`). 
- **Conflict Avoidance**: If you detect changes in the working directory that clash with your active target files, immediately halt, stash your progress (`git stash`), pull down the current state, and safely resolve the differences before re-applying your changes.
