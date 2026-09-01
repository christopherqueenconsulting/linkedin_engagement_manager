# Documentation index

Every tracked `docs/**/*.md`, one line each. This file exists so that an author — human or
agent — can find the doc that **already owns** a topic instead of appending a new row to
`CLAUDE.md`. An unindexed doc is invisible, and an invisible doc is why the same posture
gets written twice.

**★ marks a doc that `CLAUDE.md` (or a directory-scoped `CLAUDE.md`) points at directly.**
Those are the load-bearing ones: a root table row names the symbol and the invariant's
failure direction, and the ★ doc holds the full posture behind it.

Adding a doc? Add its line here in the same commit. Renaming or deleting one? Fix this
index and the `CLAUDE.md` row that points at it, or the pointer becomes a dead end.

---

## Start here

- ★ [Spec, Verifier, Environment](spec-verifier-environment.md) — grounding an issue before any code is written; the `spec-first` skill's authority
- ★ [Agent Workflow Playbook](AGENT_WORKFLOW_PLAYBOOK.md) — issues, PRs, labels, Decision Comments; the human contract for the pipeline
- ★ [Gauntlet Loop](gauntlet-loop.md) — the optional pre-PR quality bar: builder/critic pairs blind-compared against a reference exemplar
- ★ [Docstrings & lint — the house standard](docstring-standard.md) — Google convention, the `.ruff-baseline` ratchet, `agent:docfix`
- [Testing LinkedIn engagement via the API](TESTING_ENGAGEMENT_API.md) — hitting comment / reply / DM by hand
- [Postman collection](postman/README.md) — the LEM engagement request collection and its environments
- [Owner action tracker](OWNER_ACTION_TRACKER.md) — everything waiting on a human, not on code
- [VPS go-live checklist](SETUP_CHECKLIST.md) — the manual actions a fresh box still needs

## Content generation, scheduling & quality

- ★ [Unified content core](content-core.md) — framework / research / alignment shared by posts, comments and newsletters; the similarity + slop gates
- ★ [Content plan cadence & posting days](content-scheduling.md) — `POST_DAY_TYPES`, `posts_per_week`, `posting_days`; the plan is not one post a day
- ★ [Newsletter cover images](newsletter-covers.md) — upload lands `approved`, generated lands `pending_review`; notify-and-publish at the slot
- ★ [The image stack](image-stack.md) — one engine, two modules; `image_brief.py` authors, `image_gen.py` renders, presets not per-type helpers
- [Weekly group post](group-posts.md) — statuses, media, and what actually works inside a LinkedIn group
- [Timezone contract](timezone-contract.md) — what is stored in UTC, what is rendered local, and where the boundary is
- [Authenticity rubric](AUTHENTICITY_RUBRIC.md) — the A1 anti-slop gate and the 360Brew defense it is built against
- [Avatar fidelity, preview, guardrails & video language](AVATAR_FIDELITY_AND_VIDEO_LANGUAGE.md) — LoRA likeness, the guardrail resolver, spoken-language rules
- [Format API feasibility](FORMAT_API_FEASIBILITY.md) — why document posts and article/newsletter publishing have no usable API

### Content-quality audits (per surface)

- ★ [Native video posts](content-quality-audits/video.md) — the video surface audit; burned-in captions, C2PA order, avatar sidecar rule
- [Text posts](content-quality-audits/text.md) — the writing surface audit and its banned scaffolds
- [Newsletters](content-quality-audits/newsletter.md) — the newsletter audit; `canned_scaffold` is HARD here, not WARN
- [Carousels](content-quality-audits/carousel.md) — the deck audit and the reference-value gate
- [Generated images](content-quality-audits/image.md) — the render audit: no text, no logos, focal concept
- [Video telemetry dimensions](content-quality-audits/video-telemetry.md) — what `content_quality_scores` stores for a video
- [Deck reference gate — worked example](examples/deck-reference-728/README.md) — before/after slides for the save-worthiness gate

## Engagement automation

- ★ [Engagement automation internals](engagement-automation.md) — the full posture for every lane row in `CLAUDE.md`'s engagement table
- [Automation cooldown & pause](AUTOMATION_COOLDOWN.md) — the 429 circuit breaker's escalation and the operator kill-switch
- [Event-driven reply notifications](REPLY_NOTIFICATIONS.md) — the forwarded-email reply path and its modes
- [LinkedIn re-index playbook](linkedin-reindex-playbook.md) — what to do when LinkedIn stops surfacing the account's posts
- [Engagement growth analysis — July 2026](engagement-growth-analysis-2026-07.md) — the low-engagement audit that produced issues #616-630
- [Settings & configuration — research + IA proposal](SETTINGS_IA_RESEARCH.md) — how the engagement-preferences surface should be organised

## LinkedIn session, anti-bot & Selenium

- ★ [SDUI Selenium gotchas](sdui-selenium-notes.md) — the live-grounded anchor map; `data-testid` / `aria-label` over dead `urn:` selectors
- ★ [SDUI surface inventory + probe coverage](sdui-probe-coverage.md) — which surfaces have a read-only probe and a weekly drift sweep
- ★ [LinkedIn session health](linkedin-session-health.md) — sign-in visibility and OAuth token renewal; `unknown` means nothing recorded
- ★ [Selenium Grid](SELENIUM_GRID.md) — the horizontal browser path and the session-slot capacity invariant
- [Connect LinkedIn by session cookie](LINKEDIN_COOKIE.md) — `li_at` is the default engagement login; the extension and manual paths
- [Email-reply verification PIN](EMAIL_PIN_VERIFICATION.md) — answering LinkedIn's login challenge without a human at the keyboard
- [Per-user egress proxy](PER_USER_PROXY.md) — static residential egress per user and the MV3 auth-extension that carries its credentials
- [Egress & LinkedIn access at scale](EGRESS_AT_SCALE.md) — the build-vs-buy decision behind the proxy posture
- [Debugging the live browser](SELENIUM_DEBUGGING.md) — Selenium MCP + lemvnc against a running session
- [Self-healing Selenium Grid](Self%20Healing%20Grid.md) — grid recovery with Celery integration
- [Live validation — document posts & saves/impressions](LIVE_VALIDATION_FORMAT_AND_STATS.md) — what the read-only live probe grades

## Identity, auth & secrets

- ★ [Identity & sessions](identity-and-sessions.md) — `public_uid` as identity, session-token hashing, per-route authorisation, CSRF, session scopes
- ★ [Strong authentication](strong-authentication.md) — passkeys, TOTP, recovery codes, and the step-up contract
- ★ [Secrets at rest](secrets-at-rest.md) — AES-256-GCM per user+column; the field-name constants are AAD
- [Auth, identity & session-secret protection](AUTH_SECURITY_DESIGN.md) — the research and design doc the above three implement
- [Admin user management](admin-user-management.md) — the admin surface for users and its authorisation rules
- [Email deliverability](EMAIL_DELIVERABILITY.md) — keeping the login PIN email out of spam

## Observability & analytics

- ★ [Observability map](observability-map.md) — the per-surface invariants; the paragraph behind every row in `CLAUDE.md`'s observability table
- ★ [Error tracking](error-tracking.md) — `$exception` → PostHog issues → GitHub issues, and the warning-escalation contract
- ★ [LLM analytics](llm-analytics.md) — `llm_call` vs `$ai_generation`; never summed
- ★ [KPI dashboards & alerts](kpi-dashboards.md) — alert tiles must be single-series `TrendsQuery` on string properties
- ★ [Session replay](session-replay.md) — error-triggered plus sampled SPA recording; never set project sampling
- ★ [PostHog advanced surface](posthog-advanced-surface.md) — CDP destinations, Workflows, Logs, Scouts/Inbox
- ★ [Experiments](experiments.md) — unresolvable experiment means control; the registered experiment list
- ★ [Feature flags](feature-flags.md) — fails open to the env var; safety controls are never flags
- ★ [Surveys](surveys.md) — NPS/CSAT as `api`-type PostHog surveys rendered headless
- ★ [Content-quality telemetry](content-quality-telemetry.md) — the nightly trend line; unscored is never zero, and it gates nothing
- ★ [Marketing attribution](marketing-attribution.md) — only owned destinations get UTMs; existing ones are never overwritten
- ★ [Model-tier benchmarks](model-benchmarks/README.md) — the scoring suite, its contract floor, and how to read a scorecard
- [Benchmark run — `bm-20260802-20ae40`](model-benchmarks/2026-08-02-bm-20260802-20ae40.md) — archived scorecard
- [Benchmark run — `bm-20260802-5fff18`](model-benchmarks/2026-08-02-bm-20260802-5fff18.md) — archived scorecard
- [Benchmark run — `bm-20260802-b84f19`](model-benchmarks/2026-08-02-bm-20260802-b84f19.md) — archived scorecard
- [Benchmark run — `bm-20260830-3048e1`](model-benchmarks/2026-08-30-bm-20260830-3048e1.md) — archived scorecard
- [Benchmark run — `bm-20260830-1e6b4e`](model-benchmarks/2026-08-30-bm-20260830-1e6b4e.md) — archived scorecard
- ★ [Stack watchdog & deep health](stack-watchdog.md) — the host watchdog and the `/health/deep` monitor contract
- ★ [Production log files](production-logs.md) — `/opt/lem/logs/`, one dated file per UTC day; INFO not DEBUG; grep beats `docker logs`

## Infrastructure, deploy & scaling

- ★ [VPS deployment runbook](DEPLOYMENT.md) — compose layering, image refs, and the local-hotfix fallback
- ★ [Zero-downtime deploys & batched releases](zero-downtime-deploys.md) — the blue/green colour flip and the 4×-daily release windows
- ★ [The `release:now` fast lane](release-fast-lane.md) — shipping a PR at merge instead of the next window
- ★ [VPS scaling & concurrency plan](scaling-plan.md) — the browser-slot budget and what scales before it
- ★ [Stale lazy chunks after a deploy](spa-deploy-freshness.md) — asset retention, the loop-guarded reload, `/api/app-info` polling
- ★ [Cost, performance & unit-economics plan](cost-performance-margin-plan.md) — the margin model and the cost-aware down-routing design
- [Selenium hosting — cost & feasibility](scaling-cost-options.md) — hosted/cloud vs self-managed comparison
- [Celery & Flower operations guide](celery-flower-guide.md) — queues, workers, beats, and reading Flower
- [Cost tracking](CostTracking.md) — the running third-party subscription tally

## Agent pipeline & contribution

- ★ [Agent pipeline v2](agent-pipeline-v2.md) — the `lem-agentd` daemon, its state machine, and the full `decide()` table
- ★ [Contribution security](contribution-security.md) — a label is not an access control; provenance, not presence
- ★ [Branch cleanup](branch-cleanup.md) — the two layers that keep merged branches from reaccumulating
- ★ [Git safety & multi-agent concurrency](git-safety-multi-agent.md) — worktree isolation, the stash race, the safe stash form, one-venv-many-worktrees
- [CodeQL PR gate](codeql-pr-gate.md) — how the diff-informed gate calibrates against the base ref
- [The Optional-typing guard](typing-guard.md) — advisory mypy over 13 modules
- [Branch cleanup audit — 2026-07-28](branch-cleanup-audit-2026-07-28.md) — the one-shot sweep that deleted 372 branches

## Marketing, product & business

- ★ [Marketing video tutorials](marketing-video-tutorials.md) — declarative `TutorialFlow` capture, fail-closed and cheapest-first
- ★ [YouTube publishing](youtube-publishing.md) — keeping the OAuth refresh token alive; `unknown` is not `needs_reauth`
- [Launch & marketing plan](launch-and-marketing-plan.md) — the automated launch sequence
- [Marketing front page — UX target spec](marketing-page-ux-spec.md) — what the public page should be
- [Landing page update](LANDING_PAGE_UPDATE.md) — the landing-page copy revision
- [Affiliate / ambassador program](affiliate-program.md) — the referral mechanic and its payout model
- [Stripe billing setup guide](stripe-setup-guide.md) — provisioning products, prices and webhooks

## Architecture graphs

- [Graph directory](graphs/README.md) — what each graph covers and how to read them
- [Content generation](graphs/content-generation.md) — the generation call tree
- [Content scheduling & quality loop](graphs/content-scheduling-quality.md) — plan → schedule → gate → publish
- [Engagement — feed & reply](graphs/engagement-feed-reply.md) — the feed walk and reply sweep call tree
- [Engagement — outreach & DM](graphs/engagement-outreach-dm.md) — the DM and follow-up call tree
- [Deploy / release](graphs/deploy-release.md) — the release train end to end
- [Agent / issue shipping](graphs/agent-issue-shipping.md) — the pipeline's own path from issue to merge

## Historical roadmaps

- [Roadmap v1](RoadMap_v1.md) — superseded; kept for the decisions it records
- [Roadmap v2](RoadMap_v2.md) — superseded; kept for the decisions it records
