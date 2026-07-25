# Cost, Performance & Unit-Economics (Margin) Observability Plan

**Status:** Proposed · **Owner:** Platform / Growth · **Created:** 2026-07-24
**Scope:** Track spend AND performance over time, connect COST → REVENUE into per-user and
system-wide contribution margin, and close the loop with automated optimization that improves
margin as the user base scales — without a human babysitting it.

This plan is **automation-first**: dashboards, scheduled reports, threshold alerts, anomaly
detection, and auto-generated optimization recommendations (agent-filed PRs for the safe ones).
It **extends** the existing observability foundation rather than replacing it, and it
**complements** the daily engagement snapshot at `/home/lem/perf-tracking/snapshot.sh` by adding a
COST + MARGIN layer on top of that engagement layer.

Related docs: `docs/CostTracking.md` (superseded by this plan — was a static cost stub),
`docs/EGRESS_AT_SCALE.md`, `docs/PER_USER_PROXY.md`, `CLAUDE.md` (Observability section).

---

## 0. What already exists (the foundation we build on)

| Piece | File | What it already gives us | Gap this plan closes |
|---|---|---|---|
| LLM cost tracking | `src/cqc_lem/utilities/observability.py` → `track_llm_call` + `estimate_llm_cost_usd` | Emits a PostHog `llm_call` event per call with `model`, tokens, `cost_usd` (coarse per-1K table, env-overridable via `LLM_COST_PER_1K`), latency, success | **No `user_id` and no `feature`/`task` dimension** at the call site → cost cannot be sliced by user or feature today |
| LLM call wrapper | `src/cqc_lem/utilities/ai/ai_helper.py` → `_call_llm` | Central choke point every LLM call flows through; already extracts tokens + latency | Calls `track_llm_call(...)` **without** `user_id` / feature — the single highest-leverage fix |
| Post outcome tracking | `observability.py` → `track_post_outcome` | Emits `post_outcome` (impressions, engagement, engagement_rate) — perf data alongside cost | Not yet joined to cost for a per-post "cost to produce vs. engagement earned" ROI view |
| Task metrics | `observability.py` → `track_task` (wired in `app/my_celery.py`) | `celery_task` events with duration/success | No infra-cost amortization attached |
| API metrics | `observability.py` → `track_api_call` (wired in `api/main.py`) | `api_call` events | — |
| Model routing + cache | `.litellm/config.yaml`, `.litellm/complexity_router.py` | Tier aliases `lem-simple/medium/complex/router/image/research`; latency-based routing; **Redis LLM cache already ON** (`cache: true`); complexity router hook | Router optimizes for latency, **not cost/quality**; no cost-aware down-routing loop |
| Media cost model | `src/cqc_lem/utilities/ai/video_models.py` → `estimate_video_cost`, `VideoModelSpec.cost_per_second` | Per-render USD is **computable** (gen4_turbo $0.05/s, gen4.5 $0.12/s, premium tiers via credits) | Computed but **never emitted** to PostHog / ledger |
| Per-user proxy | `src/cqc_lem/utilities/proxy.py` | Regional/per-user egress resolution; per `EGRESS_AT_SCALE.md` cost scales with *regions*, not users (today) — but paid per-user residential is an override path | No recurring proxy cost captured or amortized per active user |
| Revenue | `src/cqc_lem/utilities/stripe_util.py`, `db.py` → `get_user_subscription_info`, `get_users_with_stripe_subscriptions` | Tiers `starter`/`professional`/`enterprise` → Stripe price IDs; per-user subscription status/tier | MRR not joined to cost → **no margin anywhere today** |
| Credit ledgers | `avatar_credit_ledger` (V27), `video_credit_ledger` (V30) | Proven `delta`-sum ledger pattern with `user_id`, `reason`, `stripe_session_id`, `post_id` | The template for a generalized `cost_ledger` |
| Engagement snapshot | `scripts/perf_snapshot.sh` (host cron; supersedes the on-box `/home/lem/perf-tracking/snapshot.sh`) → `metrics.jsonl` (baseline 2026-07-24) | Daily engagement KPIs for user 1 (comments/replies/dms/reactions/posts + cumulative post_stats/engagers/followups) | ~~Engagement-only~~ — now carries the `margin` cost/unit-economics block too (§D.2, issue #491) |

**Design principle:** one instrumentation choke point (`_call_llm` / `track_llm_call`), one durable
store (a `cost_ledger` table mirroring the credit-ledger pattern), one analytics plane (PostHog),
one durable margin report (extends `metrics.jsonl`). No parallel per-feature cost helpers — same rule
as the unified content core in `CLAUDE.md`.

---

## A. Cost attribution (per-user × per-feature × system-wide)

### A.1 Cost driver inventory

Every recurring cost, how it is incurred, and how we capture it:

| # | Cost driver | Unit | Variable/Fixed | Scales with | Capture mechanism |
|---|---|---|---|---|---|
| 1 | **LLM inference** (OpenAI / Claude / Ollama Cloud / OpenRouter via LiteLLM) | per call (tokens) | Variable | usage volume × tier | `track_llm_call` `cost_usd` (already) + **add `user_id`, `feature`, `model_tier`** |
| 2 | **Research** (Perplexity Sonar `lem-research`) | per edition/call | Variable | newsletters; comments OFF by default (`COMMENT_RESEARCH_ENABLED`) | Same `llm_call` path, `feature="research"` |
| 3 | **Media — video** (RunwayML gen4/gen4.5/premium) | per render (sec × rate) | Variable, spiky | video posts | **New** `track_media_cost` using existing `estimate_video_cost` |
| 4 | **Media — image** (DALL-E 3 `lem-image`) | per image | Variable | carousels/posts w/ imagery | `track_media_cost`, priced per image (add to cost table) |
| 5 | **Media — stock** (Pexels) | per fetch (free tier / quota) | ~Fixed | quota | Counter event; $0 unless quota exceeded |
| 6 | **Per-user residential proxy** | per user per month | Variable (linear in users on paid path) | active users w/ paid proxy | **New** monthly `cost_ledger` accrual per user with a proxy assigned |
| 7 | **Regional egress proxy** | per region per month | Semi-fixed | regions, not users (`EGRESS_AT_SCALE.md`) | Monthly fixed accrual, amortized across active users in region |
| 8 | **Email** (SendGrid) | per send | Variable (often free tier) | DMs-as-email / notifications / PIN | Counter × unit rate |
| 9 | **Geocoding** | per lookup | Variable, tiny | new users / location changes | Counter × unit rate |
| 10 | **PostHog** | per event / MTU | Variable | total event volume | Track our own event count; watch ingestion cost (see Risks) |
| 11 | **Infra** (Hostinger VPS + containers, MySQL, Redis, selenium-chrome, LiteLLM, cloudflared) | per month | Fixed | box size | Monthly fixed accrual, amortized per active user |

### A.2 The attribution model

Allocate every variable cost to a `(user_id, feature, model/provider, day)` tuple; amortize fixed
cost per active user per period.

```
variable_cost(user, feature, day) = Σ llm_cost + Σ media_cost + Σ api_cost   (attributed events)
proxy_cost(user, month)           = monthly proxy accrual for that user (paid path) OR
                                    region_fixed / active_users_in_region    (regional path)
fixed_infra_per_user(month)       = total_monthly_infra / active_users_in_month
fully_loaded_cost(user, month)    = Σ_days variable_cost + proxy_cost + fixed_infra_per_user
```

**Feature dimension** — a small closed enum tagged on every cost event, aligned to the code paths:
`content` (posts/carousels/video via `run_content_plan.py`), `comment`, `reply`, `dm`,
`newsletter`, `research`, `image`, `video`, `refine`, `system` (housekeeping). Derive it from the
Celery `task_name` where possible (we already pass `task_name` in structured logs), else pass
explicitly from the caller.

### A.3 Instrumentation spec (exact events & dimensions to add)

This is the concrete "what to add" for the buildable issues.

**(1) `track_llm_call` — add attribution dimensions (highest leverage, do first).**
Extend the signature and the `llm_call` event properties:

```python
def track_llm_call(model, prompt_tokens, completion_tokens, latency_ms,
                   success=True, user_id=None,
                   feature: Optional[str] = None,     # NEW: content|comment|dm|newsletter|research|...
                   model_tier: Optional[str] = None,  # NEW: lem-simple|medium|complex|... (the alias)
                   cached: bool = False):             # NEW: LiteLLM cache hit → cost ~0
    ...
    properties = {..., "feature": feature, "model_tier": model_tier, "cached": cached, "cost_usd": ...}
```

Thread `user_id` + `feature` through `ai_helper._call_llm(**kwargs)` — accept optional
`_track_user_id` / `_track_feature` kwargs (popped before the LiteLLM call) so callers in
`run_content_plan.py`, `run_automation.py`, and the newsletter path can attribute cost. Where a
caller can't supply it, derive `feature` from the active Celery `task_name`.

**(2) `track_media_cost` — new event `media_cost`.**

```python
def track_media_cost(kind: str, provider: str, usd: float, user_id=None,
                     post_id=None, feature="content", meta: dict | None = None): ...
```

Call it from the video render path using the existing `estimate_video_cost(model, duration)` and
from the DALL-E path with a per-image rate. Emit `media_cost` to PostHog **and** write a
`cost_ledger` row (durable — media is spiky and needs exact per-post accounting).

**(3) `cost_ledger` table (new migration, TIMESTAMP-versioned per the deploy-break guard).**
Mirror `video_credit_ledger`:

```sql
CREATE TABLE IF NOT EXISTS cost_ledger (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id      INT NULL,                          -- NULL = system/shared cost
  feature      VARCHAR(32) NOT NULL,              -- content|comment|dm|newsletter|research|image|video|system
  category     VARCHAR(32) NOT NULL,              -- llm|media|proxy|email|geocoding|infra|posthog
  provider     VARCHAR(64) NULL,                  -- openai|anthropic|ollama|runway|perplexity|sendgrid|...
  model_tier   VARCHAR(64) NULL,                  -- lem-* alias where applicable
  usd          DECIMAL(12,6) NOT NULL,            -- cost in USD (fine precision; cheap tiers round to 0 otherwise)
  qty          DECIMAL(12,4) NULL,                -- tokens / seconds / sends / lookups
  post_id      INT NULL,
  task_name    VARCHAR(128) NULL,
  incurred_on  DATE NOT NULL,                     -- for daily rollups
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_cost_user_day (user_id, incurred_on),
  KEY idx_cost_feature_day (feature, incurred_on),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
```

PostHog is the fast analytics plane; `cost_ledger` is the durable, joinable-to-Stripe source of
truth for the margin report (PostHog data can be sampled/retention-limited; margin math must be
exact). High-volume LLM cost can be **rolled up daily** into `cost_ledger` (one row per
`user×feature×tier×day`) rather than one row per call, keeping the table small.

DB helpers (all DB access stays in `db.py` per `CLAUDE.md`):
`insert_cost_ledger_entry(...)`, `get_user_cost(user_id, start, end)`,
`get_cost_rollup(group_by=("user"|"feature"|"provider"), start, end)`,
`accrue_monthly_fixed_costs(period)` (proxy + infra amortization).

**(4) Proxy & infra accrual (new scheduled Celery task).**
Monthly beat task walks users with an assigned proxy (`users.proxy_url` or regional match from
`proxy.py`) and writes a `proxy` `cost_ledger` accrual; writes one `infra` accrual = total monthly
fixed / active-user count. Rates come from env (`PROXY_COST_PER_USER_MONTH`,
`INFRA_FIXED_MONTHLY`, `REGION_PROXY_COST`) so no secrets/prices are hardcoded.

### A.4 Gaps summary (what is NOT captured today)

1. LLM cost has no `user_id` / `feature` → cannot slice per user or per feature. **(fix (1))**
2. Media (video/image) cost computed but never recorded. **(fix (2))**
3. Proxy cost never recorded/amortized. **(fix (4))**
4. Infra fixed cost never amortized per user. **(fix (4))**
5. Email/geocoding/PostHog costs uncounted. **(counters into `cost_ledger`)**
6. No join between cost and Stripe MRR → no margin. **(Section C)**

---

## B. Performance/engagement analytics that improve with scale

### B.1 Per-user engagement KPI set

Sourced from `post_stats`, `logs`, `post_engagers`, `commented_posts`, `comment_followups`
(already in `metrics.jsonl`) plus PostHog `post_outcome`:

- **Reach:** impressions, unique viewers (where available).
- **Engagement earned (own posts):** reactions, comments received, reposts, saves, `engagement_rate`
  (via `post_stats` weighting, already emitted by `track_post_outcome`).
- **Engagement produced (outbound):** comments made, replies made, DMs sent, connection requests,
  follow-ups — from `logs.action_type` (already snapshotted).
- **Reciprocity:** engagers gained (`post_engagers`), comment-back rate.
- **Funnel/outcomes:** profile views → connections → leads/DMs → replies (outreach funnel).
- **Lifecycle:** activation (first successful post + first comment), weekly active automation,
  retention (subscription active + automation not paused).

### B.2 System aggregate & the metrics that only get better with scale

- **Cohort benchmarks:** group users by signup month × tier; track median engagement_rate,
  comments-made/day, leads/user. New users compared against cohort benchmark on day 7/30/90.
- **Engagement lift:** engagement_rate at day N vs. that user's day-0 baseline, and vs. cohort —
  the headline "does LEM work, and is it working *better* over time" number.
- **Quality signals:** `posts.authenticity_score` (V57) and comment quality → feed the fine-tuning
  loop (below); rising quality at flat/falling cost is the goal.

### B.3 The data flywheel (what data feeds which improvement)

```
More users → more (prompt, output, engagement_rate, authenticity_score, cost) tuples
   │
   ├─► ROUTING: learn which feature×context is safe to down-route to a cheaper tier
   │            (engagement/authenticity held) → cost-aware complexity router (Section D)
   ├─► PROMPTS: mine top-engagement outputs → tighten/compress prompts (fewer tokens, same/better
   │            engagement) → lower cost per post
   ├─► CACHE/DEDUP: high-frequency near-duplicate calls (comments) → cache/batch → lower cost
   └─► BENCHMARKS: cohort engagement benchmarks sharpen as N grows → better activation targeting
```

Each arrow is a measurable before/after: a change ships behind a flag, we compare
engagement_rate + authenticity_score (quality gate) against cost delta on the affected cohort.
Ship only if quality holds and cost drops. This is exactly what makes margin improve **with** scale
instead of degrade.

---

## C. Unit economics & margins (cost ↔ income)

### C.1 Core formulas

```
MRR(user)                  = monthly price of the user's active Stripe tier (starter/professional/enterprise)
variable_cost(user, month) = Σ cost_ledger[user, category ∈ {llm, media, email, geocoding}]   (attributed)
semi_var_cost(user, month) = proxy_cost(user, month)                                            (linear-ish)
allocated_fixed(user)      = INFRA_FIXED_MONTHLY / active_users(month)                          (amortized)

contribution_margin(user)  = MRR(user) − variable_cost(user) − semi_var_cost(user)
CM%(user)                   = contribution_margin(user) / MRR(user)
fully_loaded_margin(user)  = contribution_margin(user) − allocated_fixed(user)

# System
gross_margin$              = Σ MRR − Σ variable_cost − Σ semi_var_cost − INFRA_FIXED_MONTHLY
gross_margin%              = gross_margin$ / Σ MRR

# Lifetime
LTV(user)                  = avg_monthly_contribution_margin × expected_lifetime_months
payback_months             = CAC / avg_monthly_contribution_margin      (CAC from the marketing plan)
LTV:CAC                     = LTV / CAC                                   (target ≥ 3:1)
```

CAC and lifetime assumptions are **owned by the marketing plan** (`docs/launch-and-marketing-plan.md`);
this plan imports them and provides the cost/CM side. We do not duplicate CAC methodology here.

### C.2 How margin moves with scale

| Force | Direction | Mechanism |
|---|---|---|
| Fixed-infra amortization | ▲ margin | `INFRA_FIXED_MONTHLY / active_users` shrinks per user as N grows |
| Model-cost optimization | ▲ margin | cost-aware down-routing + prompt compression + Ollama-Cloud-first tiers |
| LLM cache/dedup | ▲ margin | Redis cache already ON; extend to comment/dedup paths |
| Media reuse | ▲ margin | reuse generated assets across variants instead of re-rendering |
| **Per-user residential proxy** | ▼ margin (risk) | **linear in users** — the main margin headwind at scale (mitigate via regional egress, `EGRESS_AT_SCALE.md`) |
| Media spikes | ▼ margin (variance) | video renders are spiky; premium video is credit-gated (already) — keep it credit-funded so it's revenue-neutral |

### C.3 Targets & levers

- **Target blended gross margin ≥ 70%** at 100+ active users; **per-user CM% ≥ 60%** on the
  `professional` tier.
- **Per-user variable-cost ceiling** per tier (alert if breached) — e.g. LLM+media should not
  exceed ~20–25% of that tier's MRR.
- **Levers, in priority order:** (1) attribute cost (can't optimize what you can't see),
  (2) cost-aware routing, (3) prompt/token compression, (4) cache/dedup, (5) proxy strategy
  (regional over per-user), (6) media reuse. Each is a buildable issue below.

---

## D. The automated fine-tuning / optimization loop

Goal: cost+performance data drives decisions with **no human babysitting**, and any change that
could degrade output quality is gated by the quality signals (engagement_rate,
`authenticity_score`) and the `risk:product-decision` label.

### D.1 Optimization levers (each measured, each reversible)

1. **Cost-aware down-routing** — extend `complexity_router.py` to consider a cost/quality signal,
   not just latency. For feature×context buckets where the cheaper tier's historical
   engagement/authenticity is statistically indistinguishable, route down. Ship behind a flag; auto
   A/B on a cohort; auto-rollback if quality drops. **`risk:product-decision`.**
2. **Feature cost toggles** — the proven pattern (`COMMENT_RESEARCH_ENABLED` OFF by default because
   comments are high-volume). Generalize: any feature whose cost/engagement ratio degrades gets an
   auto-recommended toggle.
3. **Cache / batch / dedup** — LiteLLM Redis cache is on; extend semantic dedup on high-volume
   comment generation and batch newsletter research.
4. **Prompt compression** — mine high-engagement outputs; shorten prompts to cut input tokens with
   held quality.
5. **Proxy optimization** — prefer regional egress pools over per-user residential where LinkedIn
   trust allows (biggest linear-cost lever).
6. **Media reuse** — reuse/branch existing assets before paying for a new render.

### D.2 Automated cadence

| Cadence | Job | Output |
|---|---|---|
| **Daily** | Extend `snapshot.sh` with a cost+margin block (`cost_by_feature`, `spend_usd`, est. `mrr`, `contribution_margin`) appended to `metrics.jsonl` | trend line for cost & margin next to engagement |
| **Daily** | Spend anomaly detection (spend vs. trailing-7-day mean/σ) | alert if today's spend > μ+3σ or a per-user ceiling is breached |
| **Weekly** | Margin report (per-user CM, system gross margin, cohort engagement lift, LTV:CAC) | posted to owner (email/PostHog dashboard) |
| **Weekly** | Auto-optimization recommender: scans cost×quality by feature×tier, emits ranked recommendations; for **safe** changes (config toggle, cache extension) the agent pipeline can open an `agent:ready` PR; anything touching routing/quality is filed **`risk:product-decision`** for a human call | recommendations + optional auto-PRs |

**Shipped (issue #491) — the daily block + weekly report.** `src/cqc_lem/utilities/margin.py` holds
the §C.1 formulas (pure, unit-tested) plus the DB-fed collectors and delivery:

| Piece | How it runs |
|---|---|
| Daily cost/margin block | `scripts/perf_snapshot.sh` (host cron; the repo-versioned successor to the on-box `snapshot.sh`) calls `python -m cqc_lem.utilities.margin --daily-json` inside `web_app` and appends it to `metrics.jsonl` as `margin` |
| Weekly owner report | Celery beat `weekly-margin-report` → `run_scheduler.auto_weekly_margin_report` (Mon 12:00 UTC) → email + PostHog `margin_report` event; also `python -m cqc_lem.utilities.margin --weekly-report [--email]` |
| Spend source | `cost_ledger` via read-only `db.get_cost_rollup` / `get_user_cost`; `db.cost_ledger_available()` drives the `ledger_available` flag, so a $0 spend reads as "not capturing yet" rather than "nothing spent" |
| Inputs | `TIER_MRR_*`, `INFRA_FIXED_MONTHLY`, `CAC_USD`, `EXPECTED_LIFETIME_MONTHS`, `MARGIN_REPORT_EMAIL` (env, see `.env.example`) |

Weekly figures are put on a **monthly run-rate** basis (`window spend × 30.4375 / window days`) so
they are comparable with monthly MRR and the §C.1 formulas apply unchanged; the raw window spend
rides along as `period_cost_usd`. Trials are included at $0 MRR so their cost still lands in system
margin, and system-minus-per-user spend is reported as `unattributed_cost_usd` — the §E.2 signal.

### D.3 Guardrails

- **Quality gate:** no auto-change ships if it moves cohort engagement_rate or median
  `authenticity_score` below a threshold. Cost cuts that fail the gate are auto-reverted.
- **Human-in-loop for risk:** routing, billing, and prompt-content changes are `risk:product-decision`
  and never auto-merged. Config/cache/dedup changes with a passing quality gate can auto-PR.
- **Blast radius:** every optimization ships flag-gated and cohort-scoped first.

---

## E. Dashboards, alerts, cadence, KPIs, risks

### E.1 PostHog dashboards (project `CQC LEM`, id 475262)

**Shipped (issue #492) — all four are live and pinned.** Every tile is a HogQL query over the events
LEM emits, so a tile resolves even before the event feeding it starts flowing; it just renders empty
until then (`media_cost` and `margin_report` arrive with rollout steps 2/3 below).

| # | Dashboard | Tiles | Link |
|---|---|---|---|
| 1 | **Cost Explorer** — `sum(llm_call.cost_usd) + sum(media_cost.usd)` by `user_id`, `feature`, `model_tier`, provider and day; LLM cache-hit rate; unattributed-spend share | 7 | [dashboard/1903770](https://us.posthog.com/project/475262/dashboard/1903770) |
| 2 | **Margin by Cohort** — CM and CM% by signup-month cohort, gross margin $/% vs the 70% target, the variable/semi-variable/fixed cost stack, CM per paying user, unattributed cost | 6 | [dashboard/1903771](https://us.posthog.com/project/475262/dashboard/1903771) |
| 3 | **Engagement Lift** — cohort `engagement_rate` over time vs each user's day-0 baseline, weekly median rate, engagement earned, per-user lift | 4 | [dashboard/1903773](https://us.posthog.com/project/475262/dashboard/1903773) |
| 4 | **Unit-Economics Scorecard** — north-star System Gross Margin $ plus the §E.3 KPI-tree tiles (MRR, variable cost/user, LTV:CAC, payback, 30d spend, cache-hit %, quality guardrail) | 9 | [dashboard/1903774](https://us.posthog.com/project/475262/dashboard/1903774) |

The dashboards are **defined as code** in `scripts/posthog_dashboards.py` so they are reviewable and
re-creatable if a tile is edited or deleted in the UI. It diffs the spec against the live project and
is idempotent:

```bash
python scripts/posthog_dashboards.py --print-sql   # every tile's HogQL, no network
python scripts/posthog_dashboards.py --dry-run     # what would change (exit 2 = drift)
python scripts/posthog_dashboards.py --apply       # create missing / update drifted
```

It needs `POSTHOG_PERSONAL_API_KEY` (insight + dashboard write scope); `POSTHOG_PROJECT_ID` and
`POSTHOG_APP_HOST` default to the CQC LEM project on US cloud.

Two dimensions read differently than the plan first assumed: `llm_call` carries no `provider`
property, so the provider tile falls back to the model/tier the call routed through (media rows will
supply a real `provider`); and cohort margin is read from the weekly `margin_report` event rather
than joining `cost_ledger` ↔ Stripe in PostHog, since `utilities/margin.py` already computes the
exact §C.1 figures from the durable ledger.

### E.2 Alerts (thresholds)

- **Per-user cost ceiling breached** (variable cost > X% of tier MRR).
- **Gross-margin floor breached** (system gross_margin% < target).
- **Spend anomaly** (daily spend > μ+3σ of trailing 7 days, or > absolute daily budget).
- **Cache-hit-rate collapse** (LLM cache-hit% drops → cost spike warning).
- **Unattributed spend** (share of `llm_call` events with `user_id="system"` or null `feature`
  rises above a threshold → instrumentation regression).

### E.3 KPI tree (north-star: **System Gross Margin $ at target margin %**)

```
System Gross Margin $
├── Revenue (Σ MRR)         ── active paying users × ARPU (tier mix)   [marketing plan owns growth]
└── Cost
    ├── Variable/user       ── LLM cost/user · media cost/user · email/geocoding
    │     └── driven by: tokens/action · tier mix · cache-hit% · actions/day
    ├── Semi-var/user       ── proxy cost/user (regional vs residential)
    └── Fixed (amortized)   ── infra / active users
Quality guardrail (must hold): cohort engagement_rate · median authenticity_score
```

### E.4 Cadence summary

- **Daily:** engagement snapshot (exists) **+ new cost/margin block**; spend-anomaly check.
- **Weekly:** margin report + auto-optimization recommendations.
- **Monthly:** proxy/infra accrual; cohort LTV:CAC review; target-margin recalibration.

### E.5 Risks & mitigations

| Risk | Mitigation |
|---|---|
| **Unattributed / runaway LLM spend** | Fix (1) makes all cost attributable; unattributed-spend alert; per-user ceiling |
| **Proxy cost linear in users** | Prefer regional egress pools (`EGRESS_AT_SCALE.md`); reserve per-user residential for users whose trust needs it; accrue + monitor |
| **Media cost spikes** | Keep premium video credit-gated (already, `video_credit_ledger`); media reuse; spike alert |
| **PostHog ingestion cost** | Roll high-volume LLM events into daily `cost_ledger` rollups; sample verbose events; watch our own event count |
| **Over-aggressive cost cutting degrades engagement** | Quality gate on every auto-change; `risk:product-decision` gate; cohort-scoped + auto-rollback |
| **Privacy of per-user cost/revenue data** | Cost/margin dashboards are internal-only; per-user financials access-controlled; never surfaced in user-facing UI |
| **Estimated vs. actual LLM cost drift** | `estimate_llm_cost_usd` is coarse; periodically reconcile against LiteLLM/provider actuals and refresh `LLM_COST_PER_1K` |

---

## F. Rollout (maps to the filed `agent:ready` issues)

1. **Attribution first** — add `user_id`/`feature`/`model_tier`/`cached` to `track_llm_call` and
   thread through `_call_llm`. *(No margin is possible without this.)*
2. **`cost_ledger` table + DB helpers** (TIMESTAMP-versioned migration) + media/proxy/infra capture.
3. **Cost+margin block** appended to the daily snapshot → `metrics.jsonl`; **weekly margin report**.
   *(Shipped, issue #491 — see the table at the end of §D.2. Exact spend needs step 2's ledger.)*
4. **PostHog dashboards** (Cost Explorer, Margin by Cohort, Engagement Lift, Scorecard).
   *(Shipped, issue #492 — links + the `scripts/posthog_dashboards.py` provisioner in §E.1.)*
5. **Alerts** — per-user ceiling, gross-margin floor, spend anomaly, unattributed-spend.
6. **Cost-aware optimization loop** extending `complexity_router.py` (`risk:product-decision`).

Each is filed as an `agent:ready` GitHub issue referencing this doc. Anything that can auto-degrade
output quality or auto-modify routing/billing carries `risk:product-decision`.
