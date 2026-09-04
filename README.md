# LinkedIn Engagement Manager (LEM)

## Overview

LinkedIn Engagement Manager (LEM) is an automated solution for managing engagement and post interactions on LinkedIn. It builds a 30-day content plan, publishes AI-generated posts (text, carousels, native video, native PDF documents) around peak hours, and runs the day-to-day engagement — feed commenting, replies, direct messages, and follow-ups — on your behalf. All content is AI-generated via a LiteLLM proxy, passes through quality gates (a slop lint and an authenticity score, with content-quality telemetry underneath), and can flow through a preview/approval workflow before publishing. Engagement is shaped by per-user targeting, voice/tone, and daily-cap preferences.

## Key Features

### Content generation & scheduling
- **Buyer-journey content plan**: A balanced 30-day plan across awareness / consideration / decision stages — thought-leadership, industry-news commentary, personal-story, and engagement-prompt posts, carousels (educational / case-study / product-demo / insights), native video, native PDF documents, and blog summaries. The plan balances four post types (`PLANNED_POST_TYPES` in `src/cqc_lem/app/run_content_plan.py`) and never schedules two posts inside 24 hours.
- **AI-Generated Content**: LiteLLM-proxied models generate text, carousel, and video content with tiered routing (`lem-simple`, `lem-medium`, `lem-complex`, `lem-image`).
- **Peak-hour scheduling**: Auto-schedules posts around golden/peak hours, with self-healing carousels and media asset backfill.
- **Quality gates & approval workflow**: Drafts go through a slop lint and an authenticity score, and `src/cqc_lem/utilities/content_quality.py` tracks the trend; preview and approve manually or let it auto-approve. Native `LinkedInPostPreview` component and a date-time picker for editing scheduled posts.

### Engagement automation
- **Feed commenting**: SDUI-resilient feed interaction with a recency-dominant scoring matrix (recency + relevance + reciprocity + activity), inline compose/submit, and best-effort "Recent" feed sort. Runs shortly before each scheduled post and daily at a golden hour.
- **Replies, seed & pin**: Replies to comments on your own posts and auto-seeds + pins a first comment on your posts.
- **Reciprocity**: Tracks who engaged with you and prioritizes commenting back.
- **Direct messages**: Appreciation DMs (connections / recommendations / collaborations), profile-viewer outreach, and multi-touch follow-up sequences — all templated and voice-aligned.
- **Company-page invitations**: A daily, per-day-capped drip at each user's staggered slot (`invite_to_company_pages` in `src/cqc_lem/app/my_celery.py`); it replaced an earlier once-a-month blast.

### Configuration & controls
- **Targeting**: Include/exclude topics, keywords, and authors; minimum reactions and maximum post age; LLM topic-relevance scoring.
- **Voice**: Tone, comment length (short/medium/long), style, and emoji/hashtag toggles.
- **Caps**: Per-day comment and DM limits; DM template editor with follow-up steps; Login Location (city/state geocoding).

### Platform
- **Anti-bot infra**: Per-user static residential proxy with an MV3 proxy-auth extension, cookie persistence, and an email-PIN LinkedIn verification flow; 429/auth-wall backoff.
- **Dockerized Environment**: One Docker Compose stack, run locally or on a VPS.
- **Modular Design**: Content generation providers are swappable via LiteLLM aliases.
- **React SPA Dashboard**: Mobile- and web-friendly dashboard for monitoring and controlling engagement.
- **Observability**: PostHog-based LLM usage tracking, Celery task metrics, and API latency monitoring.

## Tech Stack
- **Python 3.12+** with **FastAPI** for the backend API.
- **React 19 + Vite 8 + Tailwind CSS 4** for the single-page frontend (`src/cqc_lem/ui/package.json`).
- **Selenium 4** (`selenium/standalone-chrome`) for LinkedIn browser automation.
- **Celery + Redis** for distributed task scheduling and execution.
- **MySQL 8** as the relational database.
- **LiteLLM** (port 4000) as an AI proxy routing to Ollama Cloud, OpenAI, Anthropic, and Perplexity. An `OPENROUTER_API_KEY` is passed through to the proxy container, but no alias in `.litellm/config.yaml` uses it.
- **Docker Compose** for local orchestration and for the VPS deploy — the only supported deploy path.
- **PostHog** for observability (LLM cost, task metrics, API latency).
- **Poetry** for Python dependency management.

## Getting Started

### Prerequisites
1. **Docker** installed on your system.
2. **Python 3.12+** for local development (managed via Poetry).
3. **Poetry** for Python package management (`pip install poetry`).
4. **Node.js** for frontend development (only needed if editing the React UI; CI builds the UI with Node 24, see `.github/workflows/ui-build.yml`).
5. API keys for the LLM providers wired up in `.litellm/config.yaml`: Ollama Cloud (`OLLAMA_CLOUD_API_KEY`) serves the text tiers, and OpenAI (`OPENAI_API_KEY`) is the in-group fallback and the only provider for the image, vision, embedding, and TTS aliases. Anthropic and Perplexity keys are optional.
6. (Optional) **ngrok** for exposing local services publicly:
   ```bash
   brew install ngrok/ngrok/ngrok
   ```

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/christopherqueenconsulting/linkedin_engagement_manager.git
   cd linkedin_engagement_manager
   ```

2. Set up the environment:
   ```bash
   cp .env.example .env
   # Fill in required values: MySQL credentials, LinkedIn credentials,
   # AI API keys, LiteLLM master key, Docker image name, etc.
   ```

3. (Optional) Configure ngrok by setting `NGROK_AUTH_TOKEN`, `NGROK_CUSTOM_DOMAIN`,
   `NGROK_EDGE_TOKEN`, and the `NGROK_*_PREFIX` variables in your `.env` file.

4. Set up Stripe for subscription billing — see **[docs/stripe-setup-guide.md](docs/stripe-setup-guide.md)** for
   the full walkthrough. At minimum you need:
   ```env
   STRIPE_API_KEY=sk_test_...
   STRIPE_PRICE_ID_STARTER=price_...
   STRIPE_PRICE_ID_PROFESSIONAL=price_...
   STRIPE_PRICE_ID_ENTERPRISE=price_...
   STRIPE_WEBHOOK_SECRET=whsec_...
   ```
   The webhook secret comes from registering your ngrok URL as a Stripe webhook endpoint
   (same pattern as the LinkedIn OAuth redirect URL). Do **not** use `stripe listen` inside
   Docker — it generates an ephemeral secret that breaks on every restart.

5. Build and run the Docker containers:
   ```bash
   ./run.sh
   ```
   The script will prompt whether to build and push the Docker image, then start all
   services and print a table of local (or ngrok) URLs.

6. Access the services at the URLs printed in the console.

### Docker Image

The main application image (`${DOCKER_IMAGE_NAME}:latest`) is shared by the `web_app`
(FastAPI), `celery_worker`, the four `celery_worker_selenium*` workers, `celery_beat`, and
`flower` services in `docker-compose.yml`. The `run.sh` script handles building and optionally
pushing this image to Docker Hub; the release train (`.github/workflows/build-and-push.yml`)
pushes to GHCR instead — see `docs/DEPLOYMENT.md`.

### AI Proxy (LiteLLM)

All LLM calls are routed through the LiteLLM proxy at `http://litellm:4000`. Model tier
aliases are defined in `.litellm/config.yaml`:

| Alias | Use case |
|---|---|
| `lem-simple` | Fast, lightweight — refine / summarize / list tasks |
| `lem-medium` | Balanced: comments, post refinement |
| `lem-complex` | Long-form: thought leadership, personal story |
| `lem-image` | Image generation (gpt-image-2, gpt-image-1 in-group fallback) |
| `lem-vision` | Render quality gate — looks at a generated image (gpt-4o-mini) |
| `lem-router` | Auto-routes to a tier by prompt shape (`LEMComplexityRouter` in `.litellm/complexity_router.py`) |

The same file also defines `lem-research` (Perplexity Sonar), `lem-tts`, `lem-embedding`, and the
`lem-agent-*` tiers used by the agent pipeline. Deployments inside a group compete under
`latency-based-routing`; order is not a priority list.

## Testing

This project uses pytest, in two lanes: unit (all I/O mocked) and integration (real MySQL + Redis).
There is no browser lane — a change that needs a real browser is graded by the read-only live probe,
`scripts/linkedin_live_validation.py`. See `tests/README.md` for why (#1215).

### Running Tests

1. **Install test dependencies:**
   ```bash
   poetry install --with test
   ```

2. **Run all tests:**
   ```bash
   poetry run pytest
   ```

3. **Run tests with coverage:**
   ```bash
   poetry run pytest --cov=src/cqc_lem --cov-report=html --cov-report=term
   ```

4. **Run specific test categories:**
   ```bash
   # Run only unit tests
   poetry run pytest tests/unit -v --tb=short

   # Run only integration tests (requires MySQL + Redis)
   poetry run pytest tests/integration -v
   ```

5. **Run specific test file:**
   ```bash
   poetry run pytest tests/unit/utilities/test_db.py -v
   ```

6. **Run tests matching a pattern:**
   ```bash
   poetry run pytest -k "test_database" -v
   ```

### Test Markers

Tests are organized with the following markers:
- `unit` — Fast tests with all external I/O mocked
- `integration` — Require real MySQL + Redis service containers
- `slow` — Tests that take longer to execute
- `requires_openai` — Tests requiring OpenAI API access
- `requires_database` — Tests requiring a database connection
- `requires_selenium` — Tests requiring browser automation

`pyproject.toml` also registers `requires`, `asyncio`, and `compile`; `--strict-markers` is on, so
an unregistered marker fails collection.

### Test Coverage Requirements

Targets are set in `codecov.yml`:

- **Patch**: 80% of changed lines, 5% threshold
- **Project**: 93%, 1% threshold
- Per-component statuses (API, Celery tasks, and so on) target 90% and are informational only

Codecov posts these statuses on every PR, but they are not among the required branch-protection
checks listed below.

### Continuous Integration

Branch protection on `main` requires these status checks before a PR can merge:
- `Unit Tests (Python 3.12)` (`unit-tests.yml`)
- `Integration Tests` (`integration-coverage.yml`)
- `GitGuardian Scan` (`gitguardian-scan.yml`)
- `UI Build` (`ui-build.yml`)
- `Migration Versions` (`migration-check.yml`)
- `CodeQL PR Quality Gate` (`codeql-pr-gate.yml`)
- `Docstring & Lint Gate` (`docstring-lint.yml`)

`CodeQL Security Analysis` (`codeql-analysis.yml`) also runs on push, PR, and a schedule, but is
not a required check.

The test workflows run on:
- Every push to `main`
- All pull requests targeting `main`, and merge-queue runs

There is no `develop` branch.

View test results in the GitHub Actions tab of the repository.

## Contributing
We welcome contributions to the project. Please submit a pull request with clear documentation of any changes. All required checks above must pass before review. See `CONTRIBUTING.md`.

## License
MIT License.
