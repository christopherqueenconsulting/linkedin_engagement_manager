# LinkedIn Engagement Manager — GitHub Copilot Instructions

## Project Overview

LinkedIn Engagement Manager (LEM) automates LinkedIn engagement: Selenium-based scraping, AI-generated content (via LiteLLM proxy routing to OpenAI / Claude / Ollama / OpenRouter), Celery task queue, React SPA frontend, MySQL persistence, and FastAPI backend.

Two pillars: (1) **content generation & scheduling** — a 30-day buyer-journey content plan (thought leadership, industry-news commentary, personal story, engagement prompts, carousels, native video, blog summaries) auto-scheduled around peak hours with sentiment checks and a preview/approval workflow; (2) **engagement automation** — SDUI-resilient feed commenting with a recency-dominant scoring matrix, replies + seed/pin on own posts, reciprocity tracking, appreciation/outreach DMs with multi-touch follow-ups, and monthly company-page invites. All shaped by per-user targeting, voice/tone, and per-day caps (`engagement_preferences`). Code paths: `app/run_content_plan.py`, `app/run_scheduler.py`, `app/engagement/*.py`, `utilities/ai/ai_helper.py`.

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
| Infra | Docker Compose — the ONLY supported deploy (VPS) |
| Observability | PostHog |

## Directory Map

```
src/cqc_lem/
├── api/           FastAPI app (main.py, routers)
├── app/           Celery tasks (run_scheduler.py, run_content_plan.py, my_celery.py)
│   └── engagement/  the engagement lanes: feed, posting, outreach, invites, newsletter
├── utilities/
│   ├── ai/        LiteLLM-backed AI helpers (ai_helper.py, client.py)
│   ├── linkedin/  Selenium automation (scrapper.py, poster.py, commenter.py)
│   ├── db.py      All database access (no raw SQL outside this file)
│   ├── logger.py  Structured logger — log_info/log_error/etc. preferred over myprint()
│   └── selenium_util.py  get_docker_driver() — always use this for WebDriver
└── ui/            React SPA (src/, dist/ is built output served by FastAPI)
tests/
├── unit/          Fast tests — mock all I/O
└── integration/   Require MySQL + Redis service containers  (TWO lanes — #1215 deleted tests/e2e/)
.litellm/
├── config.yaml    LiteLLM model aliases and routing config
└── complexity_router.py  Pre-call hook: routes lem-router by prompt complexity
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
- **Enums:** Use `PostStatus`, `PostType`, `LogActionType` from `db.py` — never raw strings.
- **Imports:** Absolute from `cqc_lem.*` throughout.
- **Database:** All DB access through functions in `utilities/db.py`. No raw SQL in other modules.
- **Secrets:** Never hardcode. Use `.env` with `load_dotenv()`. See `.env.example` for required vars.
- **Comments & docstrings:** Only add when WHY is non-obvious — which is what a docstring is for.
  Google-convention docstrings are required and machine-checked (`docs/docstring-standard.md`).
  Never restate the signature, never invent behaviour to satisfy the linter.

## Files Copilot Must Never Modify

- `.env` — live credentials
- `poetry.lock` — lockfile managed by Poetry
- `src/cqc_lem/ui/dist/` — built frontend artifacts (auto-generated)

## AI Call Pattern

All LLM calls go through LiteLLM proxy via `utilities/ai/client.py`:

```python
from cqc_lem.utilities.ai.client import client
response = client.chat.completions.create(model="lem-simple", messages=[...])
```

**Model tier aliases:**

| Alias | Use case |
|---|---|
| `lem-simple` | Short outputs ≤300 chars, refine/summarize/list |
| `lem-medium` | Balanced: comments, refinements, summaries |
| `lem-complex` | Long-form: thought leadership, personal story, news |
| `lem-image` | Image generation (DALL-E 3) |
| `lem-router` | Auto-routes by prompt complexity |

## Selenium Pattern

Always use `get_docker_driver()` from `selenium_util.py`. Connect to `selenium-chrome:4444`. Never instantiate `webdriver.Chrome()` directly. Use `click_element_wait_retry()` for all clicks.

**LinkedIn SDUI gotchas:**
- Old `urn:` / `feed-shared-*` / `comments-comment-*` DOM anchors are gone — prefer `data-testid` / `aria-label` via `find_first`/`click_first`. The comment composer has NO `<form>`; submit = the Comment/Post button next to it.
- ChromeDriver `send_keys` throws on non-BMP emoji — strip with `_strip_non_bmp()` before typing.
- Proxies authenticate via a runtime MV3 extension (`_build_proxy_auth_extension_b64`), not URL credentials.
- `logs.action_type` and other status columns are MySQL ENUMs — add values via a Flyway migration (`compose/local/database/migrations/`, currently through V39; V37 added `'followup'`).

## Testing Standards

- ≥80% patch coverage enforced via Codecov.
- Unit tests in `tests/unit/` — mock all external I/O.
  - `mock_openai_client` fixture — patches `cqc_lem.utilities.ai.client.OpenAI`
  - `mock_database_connection` fixture — patches `mysql.connector.connect`
  - `mock_selenium_driver` fixture — patches `selenium.webdriver.Chrome`
- Integration tests in `tests/integration/` — use real MySQL + Redis.
- **There is no e2e lane** (#1215 deleted it). Nothing in CI drives a browser; Selenium work is
  graded by the read-only live probe, `scripts/linkedin_live_validation.py`.
- Run: `poetry run pytest tests/unit -v --tb=short`
- Run with coverage: `poetry run pytest --cov=src/cqc_lem --cov-report=xml`

## CI/CD

Required status checks before merge:
- `CI / Unit Tests`
- `CI / Integration Test w/ Coverage`
- `CodeQL Security Analysis`
- `GitGuardian Security Scan`

Dependabot creates PRs for pip, github-actions, docker, and npm weekly. Dependabot PRs with failing CI are auto-tagged — high priority to fix.

## Observability

```python
from cqc_lem.utilities.observability import track_llm_call, track_task, track_api_call
```

PostHog receives LLM usage (model, tokens, latency, estimated cost) and Celery task metrics. No Prometheus or Jaeger — those services were removed.
