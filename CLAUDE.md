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
│   ├── human_pacing.py  ONE cadence engine (read delays, schedule jitter, variable daily volume, account governor)
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
imports). Off unless BOTH `COST_ROUTING_ENABLED` and `COST_AWARE_ROUTING_ENABLED` are set. Since
issue #652 the treatment cohort comes from a PostHog experiment flag resolved app-side and handed to
the router in the policy document's `arms` map (the hash stays as the fallback) — see
`docs/experiments.md`. Also `docs/cost-performance-margin-plan.md` §D.1.1.

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
- **Golden-hour presence** (`utilities/golden_hour.py`, issue #622): the ONE place the first-hour amplifier's timing is decided, and the place it finally became MEASURABLE. #401 spread several reply sweeps across the hour after publish and nothing recorded whether they fired — 14 days of logs held two replies with no way to tell late from rate-limited from nothing-to-reply-to. Every swept post now emits ONE `golden_hour_report` (comments found, replies sent, minutes since the REAL publish time from the POST log — not `scheduled_time`, or a late publish would read as a late sweep), logged at INFO in-window and WARNING out of it, and shipped to PostHog by `track_golden_hour_report`. Posts older than a day emit nothing (the sweep walks them by design; they'd only add permanent out-of-window noise), and an unknown publish time is `latency_minutes=None` + `within_window=False` — unmeasured is never counted as on-time. A sweep that could NOT run (429, session failure) emits its OWN report (`status=rate_limited` / `session_failed`, so a silent hour has a cause instead of an absence) and retries (`sweep_retry_countdown`), bounded twice — by attempts AND by the window, so a retry that would land past minute 90 is never scheduled. Reports are scoped to twice the phase's window (`report_horizon_minutes`): every sweep also walks yesterday's posts, and grading those routine revisits would bury the on-time rate under permanent out-of-window readings. The **second wave** is the other half: ONE self-comment 6–8h after publish (`auto_second_wave_comment`) that must ADD substance — it runs the same #617 quality contract, similarity gate and slop lint as a feed comment (`_gated_comment`, shared with `generate_ai_response`) and ships NOTHING when no draft passes, draws its specifics from the story bank (#620) so nothing is invented, and posts through the socialActions API like the seed comment (no browser, so it holds no Selenium lane and is 429-immune). Its 6–8h wait is served in HOPS (`second_wave_due_minutes` seeded on (user, post) so every re-arm recomputes the same target, `second_wave_hop_seconds` sized off `CELERY_VISIBILITY_TIMEOUT`) — with `task_acks_late` the broker redelivers anything unacked past that timeout, so one 8h countdown would become several self-comments — but unlike the seed it is discretionary amplification, so it stands down under `is_automation_paused()`. Seed + second wave can never stack: the cap is enforced on the COUNT of our own comments on that post URL (`count_user_comments_on_post_url`, `SELF_COMMENT_MAX_PER_POST=2`), so neither task has to know the other ran.
- **Reciprocity tracking** via the `post_engagers` table — boosts commenting back on people who engaged with us (`get_recent_engagers`).
- **DMs**: appreciation (connection / recommendation / collaboration), profile-viewer outreach, and **multi-touch follow-up sequences** — all templated and voice-aligned (`build_dm_from_template`, `dm_templates`, `dm_followups`, `process_user_followups`).
- **DM conversation auto-nurture** (`_nurture_after_reply`, `utilities/ai/dm_nurture.py`): a reply used to END a sequence — now it's classified (interested / objection / not-now / disinterest / neutral) and becomes an **approval-gated** context-aware next message queued as a `pending` row in `scheduled_dms` (`source='nurture'`), one open draft per thread, per-day draft cap, explicit disinterest stops the thread for good.
- **Owned-asset CTA loop** (`resolve_artifact_delivery` in `content_alignment.py` + `_queue_artifact_delivery`, issue #624): #618 made every promo CTA an artifact ask; this is what makes the ask DELIVER. `resolve_artifact_delivery` is the ONE map from a CTA to the asset behind it, and it names the CHANNEL because the two assets arrive completely differently — the **lead magnet** is the comment-keyword mechanic whose payload is a DM, the **newsletter** is a subscribe LINK. So the newsletter's `newsletter_url` now rides in `artifact_cta_line`, and #392's `split_link_for_first_comment` (the single choke point in `post_to_linkedin`) decides where it lands: an OFF-platform newsletter is carried into the first comment rather than the body, where a link costs 19–60% reach, while a linkedin.com newsletter (what `mark_newsletter_published` records) is deliberately left in the body — the penalty is off-platform only. Attribution has to match on BOTH halves for that reason (`content` OR `first_comment_link`); a first-comment-only count would read 0 forever for the mainline LinkedIn newsletter. `deliverable` is the honest half: an enabled newsletter with no URL saved can be NAMED but delivers nothing, and a generated resource (a build-receipt checklist as PDF) is deliberately NOT a kind — LEM has no public asset host to link one from. The keyword delivery is **approval-gated** and no longer a direct `send_private_dm`: it lands as a `pending` `scheduled_dms` row (`source='artifact'`) beside the #485 nurture drafts, blocked by an open draft from EITHER mechanic in BOTH directions (two queued messages on one thread is spam, so `_nurture_after_reply` checks for an open artifact draft too), capped per day on `max_dms_per_day` at drafting AND re-checked by `send_scheduled_dm` at send. `record_lead_magnet_sent` fires on QUEUE, not on send — its job is to stop the next sweep re-drafting the same resource. Attribution rides on `GET /user/newsletter-subscribers` (`count_artifact_cta_deliveries`): lead-magnet DMs queued and posts that carried the subscribe link, so subscriber growth can be read against the CTAs that actually delivered. `newsletter_links` is None (not 0) with no URL configured — nothing to carry is not the same fact as carried nothing.
- **Human pacing** (`utilities/human_pacing.py`, issue #626): the ONE place cadence is decided, consumed by commenting, replies, DMs and invites. Read-time delay before any comment (`pace_read` — length-scaled, floored at `PACING_READ_MIN_SECONDS`, ceilinged below `MAX_INLINE_SLEEP_SECONDS` so no worker ever parks >5 min); `dispatch_jitter_seconds` countdowns on every beat-dispatched engagement task (own-post replies use `PACE_RESPONSIVE` — jittered by seconds, not delayed by an hour); and `daily_budget`/`remaining_actions`, which turn each per-day cap into a stable random draw (weekend asymmetry + occasional account-wide rest days) under one account-level envelope, so the lanes can't each spend a full cap on the same day. Seeded on (user, action, date) and persisted in Redis, so a retry never re-rolls the day's budget. Fails open — no Redis, or `HUMAN_PACING_ENABLED=false`, restores the pre-#626 behaviour. Pacing only ever slows us down; the 429 breaker in `rate_limit.py` is the separate, harder gate.
- **Comment outcome tracking** (`sweep_comment_outcomes` + `utilities/comment_outcomes.py`, issue #628): commenting used to be write-only — LEM posted and never looked back. A read-only sweep revisits each posted comment at T+24h (work list = un-checked `logs` comment rows with a navigable `feedurn://` key), locates it via the same #478 thread map, and writes ONE `comment_outcomes` row: author replies, thread replies, likes, whether we replied, and `visible_most_relevant`. That last one is **three-valued on purpose** — 1 present under the default 'Most relevant' sort, 0 absent there but present under 'Most recent' (the May-2026 demotion signal), NULL when the sort control couldn't be read or flipped. NULL rows are excluded from the demotion denominator, never counted as healthy. A comment that can't be found in either sort is a SKIPPED row with a reason, so an unfindable comment is never re-walked. The weekly report (`auto_weekly_comment_quality`) ships the rates to PostHog + `/user/engagement-analytics`, and a demotion rate over `COMMENT_DEMOTION_HOLD_RATE` on ≥`COMMENT_QUALITY_MIN_SAMPLE` readable readings **holds that user's feed commenting** (`hold_commenting` in `rate_limit.py` — narrower than the global `pause_automation`; posting/replies/DMs are untouched) and escalates as CRITICAL. Live selector grounding: `scripts/linkedin_live_validation.py --comment-outcome-url`.
- **Suppression tripwire** (`auto_suppression_tripwire` + `utilities/suppression.py`, issue #629): 2026 LinkedIn penalties are SILENT — a flagged account just sees its reach step-collapse (the documented 8,500→340 pattern) and stays collapsed for 60–90 days, with no notification. A daily beat reads each user's own `build_engagement_trend` series and compares **impressions per post** (or engagement per post, when impressions weren't captured — a single impression-less day switches the whole comparison, it never mixes scales) against their OWN trailing 14-day median. Days with no posts are dropped BEFORE anything is measured, so `SUPPRESSION_CONSECUTIVE_DAYS` means consecutive **posting** days and a weekend off is never a collapse. A ≥`SUPPRESSION_DROP_RATIO` drop sustained across that run — or #628's comment-demotion verdict — `pause_automation()`s **engagement only** — posting is API-driven and never gated, and the read-only stat-capture lanes (`auto_scrape_stats`, `auto_capture_follower_stats`) are exempted from THIS pause specifically via `is_measurement_paused`, because the daily scrape is what produces the readings the tripwire re-evaluates: freeze it and a recovered account could never be seen to recover. It records the WHY in Redis (`record_suppression_trip`, no TTL), emails the user in plain language and escalates as CRITICAL. Cold start, a thin baseline (<`SUPPRESSION_MIN_BASELINE_POSTS`) or a zero baseline are `unknown` and never actioned; one bad day is `watch` and stops nothing. The pause is **re-armed daily while the trip stands** and only ever refreshed when the standing pause is the tripwire's own, so it never self-resumes and never extends a maintenance/429 pause. The only way back is the human one: `POST /user/automation-resume` behind the Account banner (`SuppressionBanner.tsx` off `GET /user/automation-status`), which reports a recovered reading beside the standing trip but leaves the decision to the user.
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

### LLM analytics (LiteLLM → PostHog, issue #647)

There are TWO LLM streams in PostHog and they must never be summed together — see
`docs/llm-analytics.md` for the full split.

- **`llm_call`** (app, `track_llm_call`) — LEM's cost ESTIMATE, keyed by the tier alias the caller
  asked for. It is what `cost_ledger`, the margin report and the budget alerts are built on, so
  **every money question uses this one**.
- **`$ai_generation`** / **`$ai_embedding`** (proxy) — LiteLLM's native `posthog` callback emits one
  per call with the model that ACTUALLY served it (post-fallback, post-down-route), the provider's
  own `response_cost`, tokens and latency. This is PostHog's LLM-analytics product; **use it for
  latency, error rate, model mix and volume**.

`utilities/ai/client.py` stamps `metadata: {user_id, feature}` on every `lem-*` request — attribution
lives in the ONE client, not at the ~10 call sites, because a call that skips it is invisible to both
cost routing and analytics. `metadata.user_id` becomes the event's distinct_id and falls back to the
`"system"` sentinel (same one `observability.py` uses, same person the SPA's `String(user_id)`
resolves to), so a user's browser, Celery and proxy events land on ONE PostHog person. Don't remove
`_attach_routing_metadata` from `_call_llm`: it is what lets an explicit `_track_user_id` beat the
ambient `llm_attribution()` scope.

Prompts and completions are redacted (`litellm_settings.turn_off_message_logging`) — they are the
user's own LinkedIn material, and the SPA masks the same content.

### Error tracking (`$exception` → issues, issue #648)

Errors reach PostHog TWICE on purpose, and the two are not redundant — see `docs/error-tracking.md`.

- **Logs** (`logger.py` → PostHog Logs) keep the message and its structured context. Unchanged.
- **`$exception`** is the grouped, fingerprinted ISSUE — what alerting and the error→GitHub-issue
  cron are built on. Emitted by `posthog.enable_exception_autocapture` (uncaught), by
  `log_error`/`log_critical` **when `exc=` is passed**, by the `task_failure`/`task_retry` handlers in
  `my_celery.py`, by the unhandled branch of `api/main.py`'s `observability_middleware`, and by
  `posthog-js` in the SPA.

Use `observability.capture_exception(exc, user_id=..., **context)` for anything you catch and do NOT
re-raise; everything else is already covered. It is safe to call twice with the same exception
object — `posthog.capture_exception` is idempotent per instance, so the log call and the Celery
signal do not double-count. A route's own `HTTPException` is NEVER captured: a 4xx is a response,
not an issue.

`scripts/posthog_error_issues.py` (cron: `scripts/error_to_issues.sh`) files ONE `agent:ready` GitHub
issue per ACTIVE PostHog issue, deduped on `posthog-issue-<issue_id>` across open AND closed issues.
It replaced the old log-grep scan, whose sha1-of-the-message dedup refiled any error with an id in
its text every single day. Don't add a second dedup layer — the fingerprint IS the dedup.

### Browser-side analytics (SPA, issue #646)

The SPA has ONE PostHog surface — `ui/src/utils/analytics.ts`. Never call `posthog` directly from a
component; import from there so the key, the privacy defaults and the distinct_id convention stay
in one place.

```ts
import { EVENTS, capture, maskProps } from '../utils/analytics'

capture(EVENTS.postApproved, { post_id, post_type, archetype })

// Any editor holding the user's own content (DM, story, draft post):
<textarea {...maskProps('w-full border rounded-lg px-3 py-2 text-sm')} />
```

- **distinct_id is `String(user_id)`** — identical to `observability.py`'s server-side convention,
  so a user's browser and Celery/API events land on ONE PostHog person. `$identify` fires from
  `AuthContext` off `GET /auth/session` (plan, plan_status, timezone; `created_at` as `$set_once`)
  and `posthog.reset()` fires on logout. Never put credentials or LinkedIn data on the person.
- **Env-gated at BUILD time.** `VITE_POSTHOG_KEY` / `VITE_POSTHOG_HOST` are baked into the bundle by
  Vite (docker build-arg / CI secret `UI_POSTHOG_KEY`), not read from the running container. With no
  key, `posthog-js` is never imported — it is a lazy chunk the browser never fetches — and every
  `capture`/`identify` is a no-op.
- **Autocapture is on**, with `mask_all_element_attributes` and pageviews owned by the router
  (`capture_pageview: false` + a `usePageviews()` hook, so in-app navigation is counted). Web vitals
  ride on `capture_performance`.
- `maskProps()` adds BOTH the `ph-no-capture` class (autocapture skips the element) and
  `data-ph-mask` (replay's `maskTextSelector`). Use it on every new content editor.
- New product events go in `EVENTS` — that vocabulary is what PostHog insights key off, so add to it
  rather than passing a bare string.

### Session replay (issue #649)

The recording RULES live in the SDK, not in PostHog's project settings, so they are one testable
place: a `VITE_POSTHOG_REPLAY_SAMPLE` slice (default 10%) of ordinary sessions, **plus every session
that produces an `$exception` or opens the feedback widget**. Both go through one
`ensureSessionRecorded()` in `analytics.ts`; the error one is wired to a single
`posthog.on('eventCaptured')` hook, which catches both posthog's own unhandled-error autocapture and
`captureException()`. Never gate that override on `posthog.sessionRecordingStarted()` — posthog
attaches rrweb for EVERY session and sampling only decides whether the buffer is sent, so it reads
`true` in exactly the sampled-out case the override is for. Leave the project's own sampling at 100%
(and Record user sessions ON, or nothing records at all): the local `sampleRate` takes
precedence, and configuring both multiplies them. Only **minimum duration** (the bounce filter) is
remote config; `strictMinimumDuration: true` in code makes it measure the buffer, not session age.

Inputs are masked wholesale, `data-ph-mask` masks non-input text, and network capture is timings
only — never headers (session token) or bodies (the user's LinkedIn content). Canvas is off.
`VITE_POSTHOG_REPLAY=false` ships a bundle that never records.

Every report links its recording: `session_replay_url()` in `observability.py` turns the widget's
`posthog_session_id` into the link on auto-filed feedback issues and their `+1` comments, and
`scripts/posthog_error_issues.py` does the same from the exception's `$session_id`. An id that isn't
uuid-ish, or a missing `POSTHOG_PROJECT_ID`, omits the line rather than guessing a URL. Full posture
(who is recorded, what is masked, how to verify): `docs/session-replay.md`.

### KPI funnels, dashboards, alerts + weekly report (issue #650)

`scripts/posthog_provision.py` is the ONE place the business-KPI surface is defined — two
consolidated dashboards (**LEM Health**: task failures, 429 trips, LLM spend/day, posts/day,
follower delta, API errors · **LEM Growth**: the content-loop and signup→subscription funnels,
onboarding drop-off, the comment→reply loop, ER/audience trends), four threshold alerts, and one
weekly email subscription of Growth that replaces the hand-run perf-report cron. `--dry-run`
(default) diffs, `--apply` converges, `--simulate 'NAME=VALUE'` proves an alert's threshold without
waiting for a real breach. Details: `docs/kpi-dashboards.md`.

It sits ON TOP of the cost/margin set (`scripts/posthog_dashboards.py`), which it does not replace.
Both plan by insight NAME against the same project, so **insight names must stay unique across the
two scripts** — a unit test fails the build if they collide. Alert-bearing tiles must stay native
single-series `TrendsQuery` insights (a threshold is evaluated against one `series_index`), and they filter on STRING
properties (`celery_task.state = 'FAILURE'`) rather than booleans: a boolean filter that matches
nothing yields an alert that never fires. Money tiles read `$ai_generation`, never `llm_call`.
`mark_rate_limited()` emits `rate_limit_trip` (issue #650) because the breaker's WARNING log never
reaches PostHog at the default `POSTHOG_LOG_LEVEL`.

### Experiments (`utilities/experiments.py`, issue #652)

LEM hand-rolled experimentation twice — the cost/quality down-routing cohort and the #396 media A/B
harness — and neither could say whether a difference was real, or render anywhere a human looks. This
module is the **adapter onto PostHog Experiments**, NOT a third implementation: the homegrown loops
keep running, PostHog gets the arms, the exposures and the outcome labels. Full posture:
`docs/experiments.md`.

**An unresolvable experiment is the CONTROL arm** — no key, no flag, inconclusive evaluation,
`EXPERIMENTS_ENABLED=false`, SDK raises. There is deliberately NO env fallback per experiment (a
toggle has a default worth honouring, an arm does not), and `_raw_variant()` keeps "PostHog said
control" apart from "PostHog said nothing" so `experiment_properties()` never stamps
`$feature/x=control` on a metric event from someone who was never enrolled — a fabricated control arm
makes a readout look populated. Assignment reuses `flags.py`'s ONE local-evaluation bootstrap (no
second poller, zero network calls per lookup), so every experiment flag must use rollout-percentage /
distinct-ID conditions only. Exposure is `$feature_flag_called` — PostHog's own event name and
properties, not ours — emitted explicitly (flags.py suppresses it for hot toggles) and deduped per
(experiment, person, arm) per process.

Three registered experiments. **`cost-routing-arm`**: the arm is resolved app-side and written into
each routing bucket as `arms: {"<user_id>": "treatment"}` INSIDE the policy document Redis already
carries to the LiteLLM router — `routing_policy.py` is mounted into that container and must stay
stdlib-only, so the decision is handed to it, never imported by it. `flag_arm()` reads the map,
`assign_arm()` falls back to the original hash for anyone PostHog has no answer for (a PostHog outage
moves no traffic), and `resolve_tier()` reports `assignment` so a live-experiment down-route is
distinguishable from a fallback one. The flag decides WHO, `cohort_pct` decides WHETHER: a parked
bucket can never be started by a flag, and the arms map is applied AFTER the weekly evaluation because
the window being judged was routed under the PREVIOUS document's arms. **`comment-contract-prompt`**:
the pilot LLM prompt experiment — the #617 contract's closing ask, measured on author-reply rate from
#628's sweep, scoped to FRESH FEED comments only (the seed/second-wave/reply surfaces are never
measured by that sweep) and with the six deterministically-graded rules IDENTICAL in both arms, or
"passes the gate" would mean two different things. **`post-media-variant`**: the #396 adapter, whose
arms are data (the combo that shipped) rather than a flag — `select_variant_winners` is untouched.

`scripts/posthog_experiments.py` provisions the multivariate flags + experiment records
(`--print-specs` / dry-run / `--apply` / `--rollout KEY=PCT`). It NEVER resets an existing flag's
rollout: PostHog owns the ramp once an experiment runs, and an `--apply` that reverted it would
re-cohort a live experiment. Read a readout honestly — at current volume everything here is
underpowered by design (the small-sample caveat is in the doc), and never re-roll a running
experiment's variants: that re-cohorts people and invalidates the attribution already collected.

### Content-quality telemetry (`utilities/content_quality.py`, issue #630)

Every other quality gate is a one-time verdict — the slop lint (#625) blocks a draft, the comment
contract (#617) throws one away, `score_authenticity` (#382) grades a post once. None of them can
answer *is the writing getting worse*, and it silently can: the weekly model-retirement cron swaps
models under unchanged prompts. This is the TREND LINE. `auto_nightly_content_quality` scores
everything SHIPPED in `CONTENT_QUALITY_WINDOW_DAYS` across all three writing surfaces (posts, feed
comments, newsletter editions) into `content_quality_scores` + one `content_quality` PostHog event
each: the weighted slop score (HARD violations count triple), self-similarity, #382's **stored**
`posts.authenticity_score` (never a fresh judge call — the gate already paid for it), hook length vs
`MOBILE_HOOK_MAX_CHARS`, and engagement per impression. `auto_weekly_content_quality` reads TWO
periods back out and emits `content_quality_rollup` with the deltas plus any of three alerts —
slop regression, ER under `CONTENT_QUALITY_ENGAGEMENT_FLOOR`, similarity creep — each raised through
`log_error` (the existing PostHog pipeline) and rendered on `/user/engagement-analytics` +
the Dashboard. Nothing is ever paused: quality drift is "go look at the prompts", account safety is
#629's job.

Load-bearing details. **Unscored is never zero** — no impressions yet, lint disabled, no history to
compare against are each None and excluded from their own denominator, so every dimension carries its
OWN sample size. ER is impression-WEIGHTED, so a post seen by 50 people can't outvote one seen by
5,000. A regression is measured against the account's own prior period (the ER floor is the one
absolute, set deliberately below the ~3.6% B2B benchmark so it catches a collapse rather than firing
forever on a below-average account) and needs `CONTENT_QUALITY_MIN_SAMPLE` on BOTH sides — except the
ER floor, which counts against the smaller `CONTENT_QUALITY_MIN_ER_SAMPLE` (default 3, the weekly
posting cadence) because impressions come from POSTS alone and a piece-count threshold would gate it
on comment volume it can never reach. The nightly
window is 2 days, not 1: a missed night self-heals AND a post scored the night it shipped gets its ER
once the 23:00 scrape lands — the write is an upsert. Self-similarity is batched into ONE
`lem-embedding` call per surface (under an `llm_attribution(user_id=…)` scope — the task loops over
users rather than taking a `user_id` kwarg, so without it the spend bills to `system`) and graded
WITHIN that surface (a post compared against the user's comments looks unique no matter how templated
it is), with the item's own text excluded from its history and a token-overlap fallback when
embeddings are unavailable. Because each surface embeds in its own batch, one failed call can leave a
lexical minority inside a cosine period, so `similarity_avg` is taken over the DOMINANT measure only —
the two scales are never averaged together, within a period or across two. The optional external
AI-detector (`AI_DETECTOR_*`) is OFF by default, sampled on a stable per-item draw, capped per run,
and a REGRESSION SIGNAL ONLY per the #416 policy — it never rewrites or holds anything, and a missing
key is a silent no-op.

### Feature flags — runtime toggles (issue #651)

`utilities/flags.py` is the ONE place a runtime toggle is read, and its contract is **fail open to
the env var**: no personal API key, `POSTHOG_FLAGS_ENABLED=false`, definitions unloaded, flag
undefined, evaluation inconclusive, SDK raises — every one of those returns the flag's own env var,
so a deployment with no PostHog flags behaves exactly as it did before. Each registered flag keeps
its env var as BOTH default and fallback; the registry (`FLAGS`, a `FlagSpec` per toggle with key /
env var / default / owner) is the whole vocabulary, and call sites use the exported constant so a
typo raises instead of silently reading `False` inside a Celery task.

Lookups are `only_evaluate_locally=True` + `send_feature_flag_events=False`: definitions are polled
into the process (`POSTHOG_FLAG_POLL_SECONDS`, default 30) and evaluated in memory, so a feed loop
checking a flag per post makes ZERO network requests and a flip lands on a long-running worker
without a restart. The one fetch is at a process's first check, and a failed fetch retries no more
than once per poll interval. The price is a hard registry constraint: **flags must use rollout-%
/ distinct-ID conditions only** — a person-property condition can't be decided locally and would
silently fall back to env everywhere. distinct_id is `str(user_id)` / `"system"`, same as
`observability.py`.

Read the toggle at the CALL SITE, never into a module constant at import — an import-time read is
exactly how a flag ends up doing nothing. Migrated so far: `comment-research-enabled`,
`tutorial-videos-enabled`, `feed-fallback-when-empty-default` (fleet default only — a user's saved
row always wins), `cost-routing-enabled`. **Safety controls are NOT flags**: the 429 breaker,
`hold_commenting`, `pause_automation`/the suppression tripwire and every per-day cap stay in
Redis/env, and `COST_AWARE_ROUTING_ENABLED` stays env-only because `routing_policy.py` must remain
stdlib-only. The SPA bootstraps from `GET /api/flags` (server-resolved, so no flag flicker and no
disagreement between browser, API and workers) via `hooks/useFeatureFlags.ts` — NOT through
posthog-js, which stays the analytics surface. Full posture: `docs/feature-flags.md`.

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
- **ENUM columns:** `logs.action_type` (and other status columns) are MySQL ENUMs. Adding a new value requires a migration — e.g. V37 added `'followup'` to `logs.action_type`. Migrations live in `compose/local/database/migrations/` and currently run through **V52** (V48 added `profiles.synthesis`/`synthesis_generated_at` — the cached durable voice brief, V49 added `newsletter_editions.subject` for topic dedup, V50 added `newsletter_editions.format`/`hook_style`/`opening_line`/`blueprint` — the edition SHAPE history for format/hook/opener rotation, V51 added `posts.archetype`/`hook_style` — the post-side shape history for the same rotation, V52 widened `engagement_preferences.tone` VARCHAR(64)→VARCHAR(255): the whole engagement upsert is one row, so an over-long tone raised MySQL 1406 and silently rolled back ALL sections — DM templates persisted only because they're a separate table, V53 added the `scheduled_dms` table for the DM scheduler — issue #306). New migrations use **TIMESTAMP** versions (`V<YYYYMMDDHHMMSS>__name.sql`, `date -u +%Y%m%d%H%M%S`) so two branches can never collide — e.g. `V20260726170427__add_comment_outcomes.sql` (issue #628) and
`V20260726230423__add_content_quality_scores.sql` (issue #630).
- **Unified content core:** newsletters, posts, AND comments all draw framework (blueprints/variety), research, and alignment from `utilities/ai/content_framework.py` / `content_research.py` / `content_alignment.py`. Do NOT add parallel per-content-type prompt helpers — extend the shared menus/engine instead. Comment research is OFF by default (`COMMENT_RESEARCH_ENABLED`) because comments run at high volume; the target post is their grounding. Comments also carry their own **quality contract + similarity gate** (issue #617, same module): a draft must reference a specific claim from the target post, add one of {own experience, data point, respectful disagreement, genuine question}, run ≥2 sentences, and never open with validation filler — and must not near-duplicate the user's last 50 posted comments (`COMMENT_SIMILARITY_MAX`, embedding cosine via `lem-embedding`, token-overlap fallback). A failing draft is regenerated up to `COMMENT_GATE_MAX_ATTEMPTS` times and then the post is SKIPPED — `generate_ai_response` returns `None`, never a failing comment. The post-history uniqueness engine (opener/subject avoidance steering + the deterministic `POST_SIMILARITY_MAX` review gate in `create_text_post`, mirroring the newsletter's V49/V50 dedup) also lives in `content_framework.py` — and trend-based post subjects are ANCHORED to the user's `focus_topics` (rotated per post_id via `select_focus_topic` in `content_alignment.py`), not just their profile industry. The **story bank** (issue #620, `story_bank.py` + the `story_bank` table) is the FACT half of that core: `create_text_post` selects ONE of the user's own entries per post (relevance, then least-used/longest-unused rotation) and its facts are the only personal specifics the writer may state — an empty or irrelevant bank ships an explicit no-fabrication fallback (industry observation) instead of an invented anecdote, and a first-person specific that traces to no supplied source regenerates once (`POST_FABRICATION_REGEN_ENABLED`). `profiles.synthesis` still feeds VOICE; the bank feeds FACTS. Two **save-targeted** post archetypes live in the same `POST_FORMATS` menu (issue #619): `build_receipt` and `resource_compendium`. They are marked `save_targeted` (so scheduling can prefer them via `select_blueprint(prefer_save_targeted=True)`) and `fact_anchored`, which narrows their hook menu to `NUMBER_LED_HOOK_STYLES` (lead with a real number, ~140-char mobile budget) and turns on the **no-fabrication guard**: the writer may only state a specific that a VERIFIED fact backs, otherwise it must ship as a `[[LABEL: …]]` placeholder. Those verified facts are the story bank's, at two different widths on purpose — the WRITER's allow-list is only the ONE entry this post was anchored to (carried on the blueprint as `fact_anchors`, since a number from some other entry was never in its prompt), while the CHECKERS (`_review_generated_post` and the `fact_grounding` gate, via `run_content_plan._fact_anchors`) count EVERY active entry, because a number out of the user's own material is by definition not one the model invented. `fact_grounding_report` grades the draft deterministically; an invented number costs one regeneration and then holds the post PENDING behind the `fact_grounding` quality gate, and unfilled placeholders hold it too until the author fills them in (a re-score of human-EDITED text treats the author's own numbers as verified, or the hold could never clear). An empty bank means every such draft is placeholder-only and approval-gated. Carousels draw from the same menu via `carousel_blueprint_directive` and persist their shape into the same V51 rotation history — but with an EMPTY bank the fact-anchored archetypes are taken off the carousel menu entirely (`select_blueprint(exclude_formats=fact_anchored_formats("post"))`), because a carousel bakes its text into rendered slide IMAGES and a `[[…]]` placeholder there can never be edited away. Tool/model version numbers ("GPT-4o", "Postgres 16") are NOT graded as claims — the receipt's structure asks for the exact stack by name. The **deterministic slop lint** (issue #625, `slop_lint.py`) is the cheap explainable layer under the two LLM passes (`humanize_text` #416, `score_authenticity` #382): pure regex/statistics, ~0.5ms, run on posts AND comments AND DMs AND newsletter editions AND group posts after humanization. Five HARD checks (banned lexicon pileup, the "it's not X, it's Y" contrastive frame, "here's the kicker" ta-da transitions, bait/reflex closers, emoji-bullet listicles) are regenerated up to `SLOP_LINT_MAX_ATTEMPTS` and then BLOCK — a post is held at PENDING behind the `ai_slop` quality gate with the exact constructions named, a feed comment is SKIPPED (it shares the comment gate's retry budget), and a DM/newsletter/group post ships with a logged reason because those have no review queue and dropping them breaks the sequence. Four WARN checks (em-dash density, rule-of-three, burstiness, rhetorical hook) are advisory and never hold anything — they have real false positives (a genuine list of three tools reads like a rule-of-three). The wordbank is `content_alignment.AI_TELL_WORDS`, NOT a second copy, and the bait check honours the same lead-magnet `exempt_keyword` `strip_engagement_bait` does, or every "Comment YES" CTA would hold its own post.
- **Content mix (70/20/10) governor:** every planned post carries a mix class in `posts.content_mix` — `value` (70%, audience value, sells nothing) / `authority` (20%, expertise education, sells nothing) / `promo` (10%). The classes are assigned deterministically in `content_alignment.assign_content_mix` (promo cadence `PROMO_EVERY_N_POSTS`, clamped to 10–30 so promo can never exceed 10%), the promo slot claims a TEXT post and is forced into the `case_snapshot` blueprint, and the class rides into the prompt via `alignment_directive(..., content_mix=)`. **Promo CTAs are always an ARTIFACT** (lead magnet / newsletter) — a meeting ask is banned in the prompts (`ARTIFACT_CTA_POLICY`, injected by `cta_policy_directive`), repaired deterministically by `replace_meeting_ask_cta`, and any that survives HOLDS the post at PENDING via the `meeting_cta` quality gate. Compliance is reported on `/user/engagement-analytics` (`content_mix`) and rendered on the Dashboard.
- **Proxy auth:** proxies are authenticated by the runtime MV3 extension (`_build_proxy_auth_extension_b64`), not by URL-embedded credentials — MV2 background pages that used to do this are disabled in Chrome 149+.

## Git Safety & Multi-Agent Concurrency Rules
- **Fresh State Enforcement**: Before executing or generating any code edit, you MUST explicitly run `git status` and a file read command (e.g., `cat <filename>`) to verify no hidden or uncommitted upstream modifications exist. Never rely on your internal conversation memory for file contents.
- **Micro-Branching Workflow**: Do not make edits directly on shared branches while working asynchronously. When starting a distinct task, automatically spin up a task-specific feature branch (`git checkout -b feature/claude-<task-name>`).
- **Atomic Commits**: For every completed sub-task or successful implementation block, automatically stage and commit your files with a clean, concise descriptive message (e.g., `git add . && git commit -m "feat(api): implement active sub-agent locking mechanism"`). 
- **Conflict Avoidance**: If you detect changes in the working directory that clash with your active target files, immediately halt, stash your progress (`git stash`), pull down the current state, and safely resolve the differences before re-applying your changes.
