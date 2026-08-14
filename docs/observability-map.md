# Observability map — the per-surface invariants

[CLAUDE.md](../CLAUDE.md) § **Observability** is the index: one row per surface, naming the ONE
module and the invariant that bites. This file holds the paragraph behind each row; the doc named in
each heading holds the rationale, contracts, sample code and edge cases.

Track events via `utilities/observability.py` (`track_llm_call` / `track_task` / `track_api_call`).
Plan with the CLAUDE.md table, drill in here, then read the linked doc. Doc paths in the headings
below are repo-root relative (`docs/llm-analytics.md` is this file's sibling).

## The event registry (issue #1218) — `utilities/observability.py`
Every server-side capture goes through ONE `_emit()`; what an event CARRIES is declared in `EVENTS`,
so each `track_*` is a one-liner and its docstring is free to be only about WHY. **Adding an event
means adding an `EventSpec`, never a new `posthog.capture`** — a capture written outside `_emit`
skips the coercions and is invisible to the contract below (`test_observability_events.py` fails the
build on a second call site).

Pick the field constructor deliberately: `count()` is 0 when absent ("none happened"),
`count_or_none()` stays None because the ABSENCE is the reading, `flag()` is a real boolean, and
**`label()` is a string a dashboard tile or ALERT filters on**. That last one is the load-bearing
choice — PostHog matches a property filter against the INGESTED type, so one boolean row makes
`status = "paused"` match nothing and the alert silently never fires. A numeric property an alert
COMPARES (`status_code >= 500`) is the opposite case and stays `prop()`. The pairs a provisioned
alert actually filters on are pinned in `ALERT_FILTERED` in the test, so demoting one fails CI.
A filtered property must also be a DECLARED field: `**extra` is applied after the coercions, so a
value a call site passes that way reaches PostHog untouched and the contract cannot see it. That is
why `track_task` names `state` — the Celery-failure alert's filter — as an argument of its own.

## LLM analytics (issue #647, traces #746) — `docs/llm-analytics.md`
Two streams, never summed: `llm_call` (app, cost ESTIMATE — every money question reads this) vs
`$ai_generation`/`$ai_embedding` (proxy-native, post-fallback, provider-priced — latency/error/
volume). `utilities/ai/client.py` is the ONE client and both its hooks (`_attach_attribution`,
`_attach_trace`) must stay. **Pipeline traces** group a post/edition/comment's 5–6 calls into ONE
`$ai_trace` with per-step `$ai_span`s: `@llm_pipeline` on the three roots (`create_text_post`,
`generate_newsletter_edition`, `generate_ai_response` — supersedes `attribute_llm_cost`), `@llm_step`
on the SHARED-core step functions, never at a call site. Nested trace = span; span with no trace =
no-op; `LLM_TRACING_ENABLED=false` mints nothing.

## Error tracking (issue #648) — `docs/error-tracking.md`
Logs (`logger.py` → PostHog Logs) carry message+context; `$exception` is the grouped/fingerprinted
ISSUE alerting + the error→GitHub-issue cron — NOT redundant. Use
`observability.capture_exception(...)` for caught-and-not-reraised. Never capture `HTTPException` —
4xx is a response, not an issue.

## Browser-side analytics (issue #646) — `docs/posthog-advanced-surface.md`
ONE SPA surface: `ui/src/utils/analytics.ts` — never call `posthog` directly. `distinct_id =
String(user_id)` matches `observability.py` so browser+Celery+proxy share ONE PostHog person.
Env-gated at BUILD time (`VITE_POSTHOG_KEY`); no key → lazy chunk never fetched → no-op.
`maskProps()` (adds `ph-no-capture` + `data-ph-mask`) goes on every content editor.

## Session replay (issue #649) — `docs/session-replay.md`
Rules live in the SDK: `VITE_POSTHOG_REPLAY_SAMPLE` slice + EVERY `$exception`/feedback session, both
via one `ensureSessionRecorded()`. Local `sampleRate` takes precedence — never set project sampling
(multiplies). Canvas off; inputs masked; network capture timings only.

## KPI funnels, dashboards, alerts + weekly report (issue #650) — `docs/kpi-dashboards.md`
`scripts/posthog_provision.py` owns Health + Growth dashboards, four threshold alerts, the weekly
Growth email. Alert tiles must be native single-series `TrendsQuery` filtering on STRING props
(boolean filters match nothing → silent alerts). Money tiles read `$ai_generation`, never `llm_call`.
`mark_rate_limited()` emits `rate_limit_trip` (a WARNING log never reaches PostHog).

## Experiments (issue #652) — `docs/experiments.md`
`utilities/experiments.py` is the adapter onto PostHog Experiments, NOT a third implementation.
**Unresolvable experiment = CONTROL arm** (no key, `EXPERIMENTS_ENABLED=false`, inconclusive, SDK
raises — no env fallback per experiment). Flags must use rollout-% / distinct-ID only. Three
registered: `cost-routing-arm`, `comment-contract-prompt`, `post-media-variant`.

## Marketing attribution — UTMs (issue #658) — `docs/marketing-attribution.md`
`utilities/marketing/attribution.py` is the ONE place. Two rules: only OWNED destinations tagged
(`is_owned_link`); existing UTMs never overwritten (`build_utm_url` fills missing only,
`mark_placement` replaces `utm_content` only). `signup_completed_web` (browser) ≠ `signup_completed`
(API) — never summed.

## Model-tier evaluation harness (issue #721) — `docs/model-benchmarks/README.md`
`scripts/benchmark_models.py` measures candidates vs each tier's contract suite (NO user data),
beside the current champion. Deterministic checks are source of truth (the in-repo linters, not a
copy); the LLM judge is PostHog Evaluations filtered on `benchmark_run_id` (production never carries
it → the customer is never billed). **The suite scores a FIRST draft, production ships an n-th**
(#910): every check is `contract` (the call site consumes it — the ABSOLUTE floor) or `repairable`
(a regeneration gate retries it — advisory). A run where every case of every model errored is a
harness outage, not a scorecard of zeros, and is REFUSED (#923). Only `recommend` becomes a swap,
and recommendations are RENDERED, never written.

## Content-quality telemetry (issue #630) — `docs/content-quality-telemetry.md`
`auto_nightly_content_quality` is the TREND LINE (other gates are one-time verdicts). Scores
posts/comments/editions into `content_quality_scores`: weighted slop (HARD ×3), self-similarity,
**stored** authenticity (no fresh judge call), hook length, impression-weighted ER. **Unscored is
never zero** — each dimension has its own sample size. Never pauses (safety is #629).

## Image generation telemetry (issue #1291)
Every AI still image carries its surface on the `media_cost` event: `meta.surface` is threaded
from the caller (`post_image` / `carousel` / `newsletter` / `video` / `thumbnail`), so per-surface
spend and volume are queryable in PostHog. The bounded vision gate emits `image_gate_verdict` for
**every** gated render: `verdict` is `accepted` / `rejected` / `unchecked` (the fail-open outage
case, distinguishable from a pass), alongside the surface, issue categories, attempt count, and
whether the gate actually ran (`checked`). This is the trend line for image quality; it does not
change the gate's decision or thresholds.

## Motion-prompt lint (issue #1277) — `docs/content-core.md`
One `motion_prompt_check` event per graded motion prompt, emitted BEFORE a Runway credit is spent:
`verdict` (`pass` / `warn` / `regenerate` / `hold` / `unchecked`) next to `enforced`, plus the model,
the attempt number, the names of the checks that fired and the banned phrases they matched. While
`video-motion-lint-hold` is off, `enforced=false` with hard findings is the measurement the decision
to promote the lint to a spend gate is made on — how often it WOULD have held a render. The prompt
body is never sent: `evidence` carries only phrases from LEM's own fixed `MOTION_BANNED_*` lists,
which is why the opening check (whose only evidence is prompt text) reports none.

## Slop-lint regeneration (issue #1434) — `docs/content-core.md`
One `slop_retry` event per steered regeneration. **The newsletter loop is the only emitter today** —
the short-form surfaces (`lint_repaired`: seed comments, replies, DMs) and the affiliate promo draft
run the same bounded retry and emit nothing yet, so a breakdown by `surface` reads `newsletter` or
empty. Widening it is #1536; do not read a missing surface as a surface that never retries.
`outcome` is the reading: `cleared` (no HARD check remains), `traded` (it fixed what it was steered
on and tripped a DIFFERENT check — the failure mode a whole-draft rewrite has and a targeted edit
does not), `worsened`, `persisted`, or `lost` (the regeneration returned nothing; counted, because
it still spent a call and dropping it flatters the clear rate). This event exists because the
finished draft is not evidence: it records only what was still firing when the budget ran out, so
`cleared` and `traded` are indistinguishable afterwards — which is why #1434 could not measure the
clear-rate from the 10-edition newsletter corpus. `kept` says whether that regeneration's draft
actually survived (`slop_lint.keep_retry` discards one that came back worse) and is NOT derivable
from `outcome` — a `persisted` row can be either the draft that shipped or one that was thrown
away, and a call that bought nothing is the whole argument about the budget. `attempt` next to
`max_attempts` is what a per-surface budget change (`SLOP_LINT_MAX_ATTEMPTS_<SURFACE>`) should be
decided on. Check NAMES only: a violation's `evidence` is draft text.

## Feature flags (issue #651) — `docs/feature-flags.md`
`utilities/flags.py` is the ONE place; **fail open to env var** (no key, disabled, undefined,
inconclusive, SDK raises → all return the flag's env var). `only_evaluate_locally=True` → ZERO
network per check, flip lands without restart. Read at CALL SITE, never at import. **Safety controls
are NOT flags** (429 breaker, holds, pauses, per-day caps stay in Redis/env). SPA bootstraps from
`GET /api/flags` — not through posthog-js.

## Surveys — NPS/CSAT (issue #653) — `docs/surveys.md`
TWO owners. PostHog Surveys: NPS (30d past ACTIVATION, not signup) + post-quality CSAT (on
`post_approved` once `posts_approved >= 5`). `utilities/surveys.py` keeps the bespoke ones —
trial-T-3d review (#499) + fix CSAT (#502). Type **`api`**, rendered headless in
`PostHogSurveyModal.tsx`. ONE answer = TWO paths (native `$survey_response` + POST
`/api/survey/posthog` → `feedback` row), counted ONCE — `track_survey_response` deliberately NOT
emitted. Detractor (NPS ≤6 / CSAT ≤2) or any free text stays `new`; happy+blank → `resolved`.
`markSurveySeen()` advances the 30d wait — drop it and the throttle stops silently.

## Endpoints panel + release annotations (issue #654) — `docs/kpi-dashboards.md`
**Endpoints** (PostHog beta): HogQL as a versioned cached HTTP route — the Dashboard's "Live stats"
with no MySQL reporting layer. Every query scoped with `distinct_id = {variables.distinct_id}` (ONE
shared project → un-scoped leaks across customers); resolves against ONE `InsightVariable`, and the
endpoint is `blocked_endpoint` until it exists. `GET /user/posthog-stats` is server-side only — the
personal API key never reaches the browser; any failure → `available: false` for that panel.
**Release annotations**: `scripts/posthog_annotate.py` posts `"vX.Y.Z deployed"` per deploy (needs
the `POSTHOG_PERSONAL_API_KEY` GH secret); absent/outage → no-op, never a failed release.
