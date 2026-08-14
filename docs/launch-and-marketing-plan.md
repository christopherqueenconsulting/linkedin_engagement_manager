# LEM — Automated Launch & Marketing Plan

> **Status:** Proposal for owner review. This document is the master plan for taking LinkedIn Engagement
> Manager (LEM) from private beta to General Availability (GA) using a **fully-automated** feedback loop and a
> **100% agent-executed** marketing engine. Every buildable component named here is (or will be) filed as an
> `agent:ready` GitHub issue so the existing autonomous pipeline can implement, review, merge, and deploy it.
>
> **Prime directive:** _No human in the loop for marketing execution, and no human triage for feedback._ Humans
> only make the decisions that are explicitly gated by `risk:product-decision` / `needs-human` (billing changes,
> outbound-at-scale policy, ToS judgment) via the pipeline's Decision Comment mechanism.

---

## 0. Grounding — what already exists (so this plan reuses, not reinvents)

| Capability | Where it lives | How this plan uses it |
|---|---|---|
| **Autonomous issue→deploy pipeline** | `scripts/agent-pipeline/RUNBOOK.md`; labels `agent:ready`, `agent:working`, `agent:blocked`, `needs-human`, `risk:*` | The feedback loop files `agent:ready` issues; the pipeline builds/ships them. This is the backbone of "iterate until users are satisfied." |
| **Stripe billing + 14-day trial** | `src/cqc_lem/utilities/stripe_util.py` (`create_checkout_session`, `upgrade_subscription`, `fetch_subscription`), `FREE_TRIAL_DAYS` (default 14) in `src/cqc_lem/utilities/env_constants.py`, trial row created in `src/cqc_lem/utilities/db.py` (`subscription_status='trial'`, `subscription_tier='free_trial'`, `trial_ends_at`) | Extended-trial cohort program grants a longer `trial_ends_at` + Stripe coupon. Tiers: `starter` / `professional` / `enterprise` (`STRIPE_PRICE_ID_*` in `env_constants.py`). |
| **PostHog analytics** | `src/cqc_lem/utilities/observability.py` (`track_llm_call`, `track_task`, `track_api_call`) | Extended with signup/activation/funnel events → CAC, activation rate, retention, channel ROI. |
| **Content generation engine** | `src/cqc_lem/app/run_content_plan.py` (`auto_generate_content`, `plan_content_for_user`, `create_content`), unified core `src/cqc_lem/utilities/ai/content_framework.py` / `content_research.py` / `content_alignment.py` | Dogfooding: the LEM company account runs the same 30-day content plan to market LEM. |
| **Feed engagement + outreach** | `src/cqc_lem/app/engagement/feed.py` — `automate_commenting`, `comment_on_feed_inline`; `src/cqc_lem/app/engagement/outreach.py` — `build_dm_from_template`, `automate_appreciation_dms_for_user`, `process_user_followups`; `src/cqc_lem/app/engagement/invites.py` — `invite_to_connect`, `automate_invites_to_company_page_for_user` | Dogfooding: comment→connect→DM funnel run **as** the LEM brand account to acquire users. |
| **Newsletter engine** | `src/cqc_lem/app/run_scheduler.py` (`auto_generate_newsletter_drafts`, `auto_publish_scheduled_editions`), `newsletter_editions` table | LEM publishes a LinkedIn newsletter about LinkedIn growth → top-of-funnel awareness. |
| **Lead-gen (in-flight)** | Issues #482–#486 (inbound-intent detection, lead scoring/CRM-lite, catch-up/trigger outreach, smart connection targeting) | These same features power LEM's **self**-lead-gen once shipped — the marketing engine is a first customer of the lead-gen roadmap. |
| **Celery beat scheduler** | `src/cqc_lem/app/my_celery.py` `beat_schedule` | All marketing/feedback agents are new beat entries (content 01:00, engagement 13:00, newsletter 10:00, etc. already exist as the pattern). |

**Build status.** This plan was written when Sections A and B were entirely greenfield. Much of that has since
shipped through the pipeline (issues #496–#507); the table below is the current state, so follow-on issues don't
re-file work that already exists. Sections A–D remain the plan of record for everything still open.

| Plan item | Status | Where it landed |
|---|---|---|
| In-app feedback widget + bug reporter (B.1) | **Shipped** | `POST /feedback`, `ui/src/components/FeedbackWidget.tsx`, `feedback` table (`V20260725063146__add_feedback.sql`) |
| NPS / CSAT survey capture (B.1) | **Shipped** | `POST /survey/nps`, `survey-prompts` beat |
| Feedback→issue classifier, dedup/recluster, FAQ auto-reply (B.2, B.3) | **Shipped** | `utilities/feedback/classifier.py`, `issue_service.py`, `faq_service.py`; beats `file-feedback-issues`, `recluster-feedback`, `update-faq` |
| Auto-changelog + notify reporter (B.4) | **Shipped** | `utilities/feedback/shipped.py`, `changelog-notify` beat |
| Onboarding/activation checklist + stalled-user nudges (A.3) | **Shipped** | `OnboardingChecklist.tsx`, `onboarding_state` (`V20260725090900__add_onboarding_state.sql`), `onboarding-nudges` beat |
| Extended-trial endpoint + cohort slots (A.2) | **Shipped** | `POST /trial/extend`, `extend_trial_for_user`, `early_adopter_slots` / `early_adopter_grants`, `EARLY_ADOPTER_*` + `LAUNCH_PHASE` env |
| Funnel instrumentation (C.5) | **Shipped** | `track_funnel_event` in `observability.py` |
| Brand-account dogfooding (C.2) | **Partial** | `sync-brand-account` beat (`auto_sync_brand_account`); the brand user is **user 1 by convention** (issue #736, no env var required) and runs the existing content/engagement/newsletter tasks |
| Passive-signal mining (B.1) | **Planned** | — |
| SEO, email-nurture, referral, lead-magnet, retargeting and partner/affiliate agents (C.3, C.4) | **Planned** | — |
| Analytics agent: CAC / channel-ROI rollups + optimization loop (C.5) | **Planned** | — |

---

## A. Release & Extended-Trial Program

### A.1 Phased rollout

Three phases with hard entry/exit gates. The pipeline and marketing agents behave differently per phase (e.g.
outbound volume ramps only as ToS-safety and satisfaction signals hold).

| Phase | Cohort & size | Entry criteria | Exit criteria (→ next phase) |
|---|---|---|---|
| **P0 — Private early-adopter** | Hand-picked + waitlist, **cap 25 users** | Core loops green in prod (content plan, feed commenting, DMs, newsletter); auto-onboarding live; feedback loop live; billing + trial live | ≥15 activated users; **0 open `priority:critical` bugs**; week-1 retention ≥ 50%; ≥ 8 pieces of actionable feedback processed end-to-end through the pipeline |
| **P1 — Open beta** | Public signup, self-serve, **soft cap 250** | P0 exit met; extended-trial automation live; self-marketing dogfood loop running at throttled volume; deliverability warmed | Activation rate ≥ 40%; **week-2 retention ≥ 30%**; NPS ≥ 30 (min 20 responses); trial→paid conversion ≥ 8%; 0 open `priority:critical`, ≤ 3 open `priority:high` bugs |
| **P2 — GA / official release** | Unlimited public | P1 exit met + GA gate (A.4) | — (continuous improvement continues via the same loop) |

Phase state is a single config key (`LAUNCH_PHASE` env / Redis) read by marketing agents and the signup path so
guardrails (caps, outbound volume, price display) switch atomically. Advancing a phase is a
`risk:product-decision` gate — the owner confirms the exit metrics via a Decision Comment.

### A.2 Extended trial for first adopters

- **Standard trial today:** 14 days, all Professional features, no credit card (`FREE_TRIAL_DAYS`, default 14;
  trial row in `src/cqc_lem/utilities/db.py`). Marketing copy (previously `src/cqc_lem/streamlit/Home.py`,
  deleted #972 — legacy Streamlit UI, replaced by the React SPA): *"Start with a free 14-day trial on any
  plan. No credit card required."* / FAQ: *"The 14-day free trial includes access to all Professional plan
  features with no limitations. You can generate content, schedule posts, and use automation features to
  fully evaluate the platform."* Keep this as the default.
- **Early-adopter offer:** a **materially longer 60-day trial** for the first **capped cohort (P0: 25, P1 first
  100)**, granted automatically — no human touch:
  1. New endpoint `POST /trial/extend` (feature-flagged, cohort-gated) sets `trial_ends_at = trial_started_at +
     EARLY_ADOPTER_TRIAL_DAYS` (default 60) in the users/subscription row via a new `db.py` helper
     (`extend_trial_for_user`). All DB access stays in `db.py` per `CLAUDE.md`.
  2. Mirror it in Stripe: create a reusable **early-adopter coupon / `trial_period_days` override** and attach it
     when the user later converts (extend `create_checkout_session` to accept `trial_period_days` /
     `discounts=[{coupon}]`). *Shipped:* `create_checkout_session` now takes both parameters.
  3. Cohort membership is claimed automatically at signup while an **atomic counter** (`early_adopter_slots`
     in Redis/DB) is > 0; when slots run out, new signups fall back to the 14-day default. No codes to type.
  4. Because it **auto-modifies billing terms**, the issue that builds this carries `risk:product-decision` — the
     owner signs off on the length (60d), the cap, and the coupon once via Decision Comment.
- **What early adopters give in exchange** (all captured automatically, see Section B): an NPS/CSAT response, at
  least one testimonial prompt answered, and a referral link. Completing these unlocks a **loyalty perk**
  (e.g. 30% lifetime discount coupon auto-applied at conversion) — again a `risk:product-decision` on the discount.

### A.3 Fully-automated onboarding & activation (no human)

**"Aha moment" (activation) definition:** a user reaches activation when, within their first 7 days, LEM has
**published their first AI post AND landed their first automated feed comment/DM under the user's own voice
settings** — i.e. they have seen LEM *act on LinkedIn for them*. This is the single event the whole funnel
optimizes toward.

**Activation checklist** (rendered in the SPA, state persisted server-side, emitted to PostHog as each step
completes):

1. Connect LinkedIn session (cookie/extension) — reuses the LinkedIn Connect extension + verification-PIN flow.
2. Set voice/tone + targeting in `Account.tsx` (`engagement_preferences`).
3. Approve the first AI-generated post from the 30-day plan (`run_content_plan.py`).
4. Enable feed commenting + DM caps.
5. **Activated:** first post published + first comment/DM sent.

**Automated nudges for stalled users** — a new beat task `auto_onboarding_nudges` (daily, mirrors
`auto_notify_missing_linkedin_session` throttling) inspects each trial user's checklist state and sends the next
best nudge via the existing notification-email path (`utilities/linkedin/notification_email.py`) and in-app
banner:
- No LinkedIn session after 24h → "Connect in 2 minutes" email + extension link.
- Session but no voice set after 48h → guided voice-setup nudge.
- Voice set but no post approved after 72h → "Approve your first post" with a pre-generated draft.
- Approaching trial end (T-3d) with activation → conversion nudge; without activation → "need help?" + concierge
  fallback. All copy is LLM-generated via `lem-simple` and brand-voice-aligned.

### A.4 GA (official release) criteria — quantitative gate

Leaving beta requires **all** of the following, measured in PostHog over a trailing 14-day window (advancing is a
`risk:product-decision` Decision Comment):

| Metric | Threshold |
|---|---|
| Activation rate (signup → aha within 7d) | **≥ 45%** |
| Week-2 retention | **≥ 35%** |
| NPS (rolling, ≥ 40 responses) | **≥ 40** |
| Trial → paid conversion | **≥ 10%** |
| Open `priority:critical` bugs | **0** |
| Open `priority:high` bugs | **≤ 2** |
| Median feedback→deploy cycle time (loop health) | **≤ 7 days** |
| LinkedIn automation health (self + user accounts: 429/auth-wall rate) | **< 2% of sessions** |

---

## B. Feedback → Auto-Work Loop ("iterate until users are satisfied")

The loop's promise: **a user reports something → it becomes a GitHub issue with the right labels → the
autonomous pipeline builds & deploys it → the reporter is told it shipped → satisfaction is re-measured.** No
human triages. Humans only touch items the classifier routes to `needs-human` / `risk:*`.

```
 ┌─ in-app widget ─┐
 │  NPS / CSAT     │        ┌──────────────┐      ┌──────────────┐      ┌─────────────────┐
 │  bug reporter   ├──────► │  Classifier   ├────► │ Dedup/cluster ├────► │ GitHub Issue svc │
 │  passive signals│  event │ (LLM + rules) │      │ (embeddings)  │      │  labels+body     │
 └─────────────────┘        └──────────────┘      └──────────────┘      └────────┬────────┘
        ▲                                                                          │ agent:ready
        │ re-measure satisfaction        ┌───────────────┐    ┌──────────────┐    ▼
        └────────────────────────────────┤ auto-changelog │◄───┤  Pipeline     │◄─ RUNBOOK.md
             notify reporter + PostHog    │  + notify user │    │ build→CI→ship │
                                          └───────────────┘    └──────────────┘
```

### B.1 Zero-human feedback capture

| Channel | Mechanism | Build |
|---|---|---|
| **In-app feedback widget** | Persistent "Feedback / Report a bug" button in the SPA; free-text + type hint + optional screenshot + auto-attached context (route, user_id, app version from `/api/app-info`, last PostHog session id) | `POST /feedback` endpoint + `feedback` table + React widget |
| **NPS / CSAT surveys** | Triggered post-activation (day 3) and at trial T-3d via in-app modal + email; 0–10 + one free-text "why" | `POST /survey/nps`; stored in `feedback` with `source='nps'` |
| **Bug reporter** | Same widget with `type=bug`; client auto-captures console errors + failing request id | reuses `/feedback` |
| **Passive signals (no user action)** | PostHog funnels (drop-off between checklist steps), error-tracking spikes, churn signals (trial ending w/o activation, cancels), repeated task failures (`track_task success=false`), 429/auth-wall rate | A daily `auto_mine_passive_signals` beat task queries PostHog + `logs` and emits synthetic feedback items for the classifier |

All captured items land in one `feedback` table (migration, **TIMESTAMP-versioned** per RUNBOOK) with:
`id, user_id, source, type_hint, body, context_json, embedding, cluster_id, github_issue_number, status,
sentiment, created_at`.

### B.2 Auto-classification (bug / feature / fix / update) → auto-filed issue

A new module `utilities/feedback/classifier.py` runs each item through an LLM call (`lem-medium` via the standard
`client`) with a strict JSON schema output:

```json
{
  "category": "bug | feature | fix | update | question | noise",
  "severity": "critical | high | medium | low",
  "component": "content | engagement | billing | onboarding | ui | infra | ...",
  "title": "<conventional-commit style>",
  "summary": "<why + scope + acceptance>",
  "risk": "none | product-decision | live-linkedin | migration | security",
  "duplicate_of": "<cluster_id | null>",
  "confidence": 0.0
}
```

Label mapping (matches the **real** repo labels):
- `category` → `bug` / `feature` / `enhancement` / `cleanup`.
- `severity` → `priority:critical|high|medium|low`.
- **Always add `agent:ready`** when `risk == none` and `confidence ≥ 0.7` → the pipeline builds it unattended.
- `risk != none` → add the matching `risk:product-decision|live-linkedin|migration|security` **and** `needs-human`,
  assign `gitchrisqueen`, and post a **Decision Comment** (lettered options) per the RUNBOOK — the pipeline will
  hold it at merge. This is the safety valve: anything touching billing, outbound-at-scale, DB destructiveness,
  or ToS judgment cannot auto-ship.
- Low confidence or `category == question/noise` → route to a `needs-human` triage queue (no issue spam);
  `question` items get an auto-reply from an FAQ agent instead.

A new service `utilities/feedback/issue_service.py` creates the issue via `gh` / GitHub API, writing a
structured body (Why / Scope / Files-hint / Acceptance) that the RUNBOOK's `MODE=start` expects, and stamps the
issue number back onto the `feedback` row. It references this plan doc in every issue it files.

**Admin gate (issue #793).** Auto-filing is not open to everyone: `process_new_feedback` and
`auto_recluster_feedback` only act on reports from **admin** users (`users.is_admin`, or the
`ADMIN_USER_EMAILS` allowlist). Everything else stays `new` until an admin approves or dismisses it in the
in-app triage panel (`/admin/feedback`, `GET|POST /api/admin/feedback…`), where approve runs the normal
classifier/filer path and stamps `feedback.reviewed_by/reviewed_at`. Two consequences worth knowing:
- The gate is applied **in the query** (`get_unprocessed_feedback(admin_only=True)`), not in the caller's
  loop. Parked rows keep their `new` + NULL-cluster shape forever, so a caller-side skip would refill the
  `limit` window with the same rows every pass and admin feedback would never be reached again.
- At least one admin has to exist or the loop parks everything with nobody able to release it. The migration
  seeds the founding account; `ADMIN_USER_EMAILS` is the no-SQL bootstrap for any other deploy.
- A row that already reached GitHub (`clustered` / `issue_created` / `resolved`, or any row carrying an issue
  number) is **not re-reviewable** — the review endpoint answers 409. A filed row IS its own open cluster, so
  re-running the filer on it would match it to itself at similarity 1.0 and post a false "+1 another report"
  on the very issue it created. The panel's buttons alone are not the guard: its list is cached, and the
  filing beat or a second admin can settle a row between render and click.
- **Approve is a human decision, so the unattended holds do not re-apply to it (issue #1036).**
  `file_feedback_issue(..., admin_approved=True)` skips the per-user daily issue cap and files a `NEEDS_HUMAN`
  (low-confidence) row instead of parking it back in `triaged`. Both holds exist because the batch pass runs
  with nobody watching; re-applying them to an explicit approve returned 200 and left the row in `new`, so the
  panel re-rendered it unchanged — which is what "the approve button does nothing" was. Confidence still
  shapes the LABELS, so a shaky classification lands `needs-human` + assigned + Decision Comment and never
  `agent:ready`. The NOISE and FAQ verdicts are unchanged: those settle the row, so the panel already shows
  the admin what happened. What can still file nothing is GitHub refusing — the response carries `filed:
  false` and the panel says so **at the button**, because that outcome changes nothing else on screen.
  A low-confidence verdict and NO verdict are NOT the same thing: the fail-safe classification (`errors`
  set — the LLM never answered) carries the RAW report as its `summary`, so approving it would publish the
  feedback text §B.2 promises never reaches GitHub, under `issue_created` — which is terminal. That case
  files nothing, leaves the row in `new`, and returns `reason: classification unavailable` so the panel
  tells the admin to retry rather than blaming GitHub.
`count_pending_admin_review()` reports the backlog on every filing pass so it can't grow silently.

### B.3 Deduplication / clustering (one recurring problem = one issue)

- Each item is embedded (cheap embedding model via LiteLLM). Before filing, `issue_service` cosine-compares
  against open feedback clusters. If similarity ≥ threshold → attach to the existing `cluster_id` and **comment
  a +1 with the new detail on the existing issue** instead of opening a duplicate; bump a `weight`/reaction count
  so the pipeline can prioritize by demand.
- Nightly `auto_recluster_feedback` re-groups the backlog and can auto-close issues whose cluster is resolved.
- Guard against **feedback-loop abuse**: rate-limit per user, spam/sentiment filter on the classifier
  (`category=noise`), and require ≥ N distinct users OR an owner nod before a *feature* cluster gets `agent:ready`
  (features are higher-risk than bugs; bugs auto-file immediately).

### B.4 Close the loop — changelog + notify + re-measure

- When the pipeline merges/deploys a PR that `Closes #<issue>`, a `auto_changelog_notify` task (triggered by the
  existing release/deploy flow or a beat poll of merged PRs) appends a human-readable line to a public changelog
  and **notifies every user attached to that feedback cluster** ("You asked, we shipped: …") via email + in-app.
- After notify, schedule a **micro-CSAT** ("did this fix it?") to that user; the response re-enters the loop and
  updates the satisfaction metrics feeding the GA gate.
- **Loop runs continuously** — it does not stop at GA; GA is just the point where the metric thresholds are first
  met. The KPI dashboard (Section D) shows loop cycle-time and satisfaction trend as first-class health metrics.

---

## C. Fully-Automated Marketing (AGENTS ONLY)

Hard requirement: **every marketing action is executed by an agent/automated task, never a person.** The plan is
built so that a human's only role is approving `risk:product-decision` gates (outbound volume policy, discount
economics, brand-voice guardrails) — never pressing "post."

### C.1 The automated funnel

| Stage | Automated mechanism | Agent / task |
|---|---|---|
| **Awareness** | LEM's own LinkedIn posts (30-day plan), newsletter editions, SEO blog posts, syndicated content | Content agent, SEO agent |
| **Interest** | Value-add feed commenting on ICP posts, newsletter subscribes, lead-magnet downloads | Outreach agent |
| **Trial signup** | Comment→connect→DM funnel driving to signup URL; retargeting; email nurture from lead magnet | Outreach agent, Email agent |
| **Activation** | Automated onboarding + nudges (Section A.3) | Onboarding task |
| **Retention** | Value delivery + churn-signal nudges + feature-ship notifications (Section B.4) | Feedback/notify loop |
| **Referral** | Post-activation referral prompt + double-sided incentive; testimonial capture auto-posted (with consent) | Referral agent |

### C.2 Dogfooding as the flagship channel — **LEM markets LEM**

The single most credible proof that LEM works is LEM growing its own audience with LEM. The **LEM brand
LinkedIn account** is a first-class user of the product and runs the exact same automation users pay for.

**Who the brand account is: user 1, by convention (issue #736).** The first account on the box is the owner's
own, it doubles as the brand account permanently, and `brand_account.brand_user_id()` resolves it with **zero
configuration** — the earlier `BRAND_ACCOUNT_ENABLED` / `BRAND_ACCOUNT_EMAIL` pair was wiring that only ever
failed closed, and kept this whole engine dormant in prod because two env vars were never set. An optional
`BRAND_USER_ID` seats the brand elsewhere; blank or invalid falls back to user 1 rather than switching
self-marketing off. What the brand does:

1. **Content about the problem LEM solves.** The brand account's `focus_topics` are set to the ICP's pains
   ("consistent LinkedIn presence without the grind", "solo-founder pipeline", "AI content that sounds like
   you"). `plan_content_for_user` + `auto_generate_content` produce the 30-day mix (thought-leadership,
   industry-news commentary, personal-story, engagement prompts, carousels, video). Every post is a live demo of
   LEM's output quality — the product *is* the ad.
2. **Feed commenting → connect → DM funnel, run as the brand.** `automate_commenting` /
   `comment_on_feed_inline` add value on ICP posts (founders, consultants, coaches); the smart-connection-
   targeting feature (#486/#398) turns engagers into connection requests via `invite_to_connect`; appreciation +
   outreach DMs (`build_dm_from_template`, `automate_appreciation_dms_for_user`, `process_user_followups`) run a
   multi-touch sequence that offers the extended trial. All gated by per-day caps + voice/tone in
   `engagement_preferences`.
3. **Newsletter.** The brand runs `auto_generate_newsletter_drafts` / `auto_publish_scheduled_editions` to publish
   a LinkedIn newsletter on LinkedIn-growth tactics → recurring top-of-funnel with a subscribe CTA to the trial.
4. **Company-page invitations.** Monthly `automate_invites_to_company_page_for_user` grows the LEM company page
   audience.
5. **Self-lead-gen.** Once #482–#486 ship, the brand account's inbound-intent detection + lead scoring surface
   warm prospects (people who commented "how does this work?") into a hot-lead list that the DM agent nurtures to
   signup — LEM eats its own lead-gen dog food.

**Concreteness / guardrails:** the brand account obeys the same rate-limit + 429 backoff infra
(`utilities/linkedin/rate_limit.py`), per-user proxy (`utilities/proxy.py`), and per-day caps as any user — so
self-marketing can never exceed ToS-safe volume. Outbound cadence for the brand is a `risk:product-decision`
config (daily connect/DM caps) the owner signs off once.

**The collision guard (issues #736, #952).** Because the brand user is *also* the owner's ordinary LEM account,
the nightly `sync_brand_preferences` **seeds, it does not re-assert**. Which half applies is decided by
`db.engagement_preferences_are_configured` — three-valued, because "could not read the row" is not "never
configured" and an unreadable row skips the sync outright:

- **No saved engagement row** → the phase sets `max_comments_per_day` / `max_dms_per_day` /
  `max_invites_per_day` and `connection_request_mode` / `connection_targeting_mode` outright.
- **A saved row** → those are the owner's own Settings choices and the phase is not applied to them at all.
  Only `BRAND_CAP_CEILINGS` still binds: a cap above the shipped per-user default (20 / 20 / 10) is pulled back,
  so the brand can still never run hotter than a paying user out of the box.

The re-assertion this replaces was the reported bug: the Settings hub recommends the **Balanced** preset
(15 / 10 / 8 — the P1 numbers) while prod runs `LAUNCH_PHASE=P0`, so the caps the owner picked in the UI were
back at 8 / 5 / 5 by morning with nothing on screen to say why. Saved settings are the sign-off; an env var
nobody has touched is not. The seeded content fields (`focus_topics`, `business_goals`) stay fill-only-when-empty
as before, everything else on the row is untouched (only the policy fields are sent — issue #639), and any field
the sync does change is named before → after in one INFO line, so an edit to the owner's own settings is never
silent.

### C.3 Other automated channels (each agent-run)

- **SEO / content publishing:** SEO agent generates + publishes blog/landing content targeting LinkedIn-growth
  keywords (reuses the unified content core `content_framework.py`/`content_research.py`), cross-links the
  newsletter and lead magnets, and refreshes stale pages. Ahrefs/GSC data (available via MCP) informs keyword
  targeting; analytics agent tracks organic entries in PostHog.
- **Email nurture:** Email agent runs sequenced, event-triggered campaigns (welcome, lead-magnet delivery,
  activation nudges, trial-ending, win-back) off the `notification_email` path. **CAN-SPAM/GDPR compliance is a
  hard guardrail** — every send includes an unsubscribe link + physical address, honors suppression list, and only
  emails opted-in trial/lead records.
- **Lead magnets:** auto-generated assets (e.g. "30-Day LinkedIn Content Calendar", "The Solo-Founder LinkedIn
  Growth Playbook") gated behind an email capture; delivery + nurture handled by the email agent. The lead-magnet
  table already exists (`V43__add_lead_magnet.sql`).
- **Retargeting:** PostHog cohorts (visited-pricing-no-signup, signup-no-activation) export to ad/retargeting
  audiences; the analytics agent maintains the cohorts.
- **Partner / affiliate outreach:** Outreach agent runs a separate, low-volume, ICP-specific sequence to potential
  affiliates/communities (double-opt, personalized) — **`risk:product-decision`** because it is outbound at scale
  to non-users.

### C.4 Agent roles, cadence & guardrails

| Agent | Owns | Cadence (Celery beat) | Guardrails |
|---|---|---|---|
| **Content agent** | Brand 30-day plan, posts, carousels/video, newsletter drafts | Daily 01:00 (`auto_generate_content`), newsletter 10:00 | Brand-voice via `content_alignment.py`; AI-disclosure (`_apply_ai_disclosure`); anti-slop authenticity guardrails |
| **Outreach agent** | Feed commenting, connect, appreciation/outreach DMs, follow-ups, company invites — **as the brand** | Pre-post + daily 13:00 (`auto_daily_engagement`), DMs 08:00, follow-ups every 30m, invites monthly | **LinkedIn ToS**: per-day caps, `rate_limit.py` 429 backoff, human-like pacing, per-user proxy; volume caps are `risk:product-decision` |
| **SEO agent** | Blog/landing generation + publish + refresh | Weekly | Keyword targeting from Ahrefs/GSC; no thin/duplicate content; canonical/no-index hygiene |
| **Email agent** | Nurture + lifecycle sequences | Event-triggered + daily sweep | **CAN-SPAM/GDPR**: unsubscribe, suppression list, opt-in only, throttled sends for deliverability |
| **Analytics agent** | Funnel/CAC/activation dashboards, cohort maintenance, channel ROI, optimization signals | Daily 23:xx (near `auto_scrape_stats`) | Read-mostly; proposes changes as feedback items, never ships directly |
| **Feedback-triage agent** | Classify → dedup → file `agent:ready` issues → changelog/notify | Continuous / per-event + daily recluster | `risk:*`/`needs-human` gating; per-user rate limit; feature clusters need demand threshold |

**Cross-cutting guardrails:**
- **LinkedIn ToS / rate limits:** all self-marketing runs through the same caps + backoff as paying users; the
  brand account is not special-cased to go faster. A tripped 429 breaker pauses brand outbound just like a user.
- **Brand voice / quality:** anti-AI-slop authenticity policy (READER-mode humanization, never fabricate facts —
  see `docs/AUTHENTICITY_RUBRIC.md`) applies to all brand content; a similarity/quality review gate
  (`POST_SIMILARITY_MAX`) blocks repetitive posts.
- **Email compliance:** CAN-SPAM + GDPR as above.
- **Over-automation quality risk:** the `risk:product-decision` gate + the review/CI/Copilot gates in the
  pipeline mean no auto-generated *code* and no scale-outbound *policy* ships without the relevant gate.

### C.5 Attribution, analytics & the optimization loop

- Instrument the funnel end-to-end in PostHog: `signup_started`, `signup_completed`, `trial_started`,
  `onboarding_step_completed`, `activated`, `subscription_started`, `churned`, plus source/UTM + `channel`
  properties (extend `observability.py` with `track_funnel_event`). Attribute every trial to a channel (brand
  LinkedIn / newsletter / SEO / email / referral / affiliate).
- **CAC & channel ROI:** since marketing is agent-run, cost is dominated by LLM + infra spend already tracked via
  `track_llm_call` (tokens/cost) — CAC ≈ (LLM + infra cost attributable to a channel) / conversions. The analytics
  agent computes CAC, funnel conversion, activation, and retention **per channel** and writes a daily rollup.
- **Automated optimization loop:** the analytics agent detects underperforming stages (e.g. connect→DM reply rate
  falling, a landing page with high bounce) and emits **feedback items** into the Section B loop → those become
  `agent:ready` issues (copy tweaks, cadence changes, new content angles) → the pipeline ships them → re-measure.
  Marketing thus self-tunes through the same iterate-until-satisfied machinery, with `risk:product-decision`
  guarding any change that would increase outbound volume.

---

## D. Metrics, Risks & Timeline

### D.1 North-star metric + KPI tree

**North-star:** **Weekly Activated Retained Users (WARU)** — users who reached the aha moment *and* were active
in the trailing 7 days. It captures acquisition, activation, and retention in one number and is the thing GA and
marketing both serve.

```
WARU (north star)
├── Acquisition
│   ├── Awareness: brand LinkedIn impressions/reach, newsletter subscribers, SEO organic sessions
│   ├── Interest: profile visits, lead-magnet downloads, connect-accept rate
│   └── Signups: trial_started (by channel) → CAC per channel
├── Activation
│   ├── Activation rate (signup → aha ≤7d)
│   └── Onboarding step conversion (checklist funnel drop-off)
├── Retention
│   ├── Week-1 / Week-2 / Week-4 retention
│   └── Feature adoption depth (posts published, comments/DMs sent per user)
├── Monetization
│   ├── Trial → paid conversion
│   └── MRR, churn, LTV
└── Advocacy
    ├── NPS / CSAT
    └── Referral rate + testimonials captured
```

**Loop-health KPIs (unique to this plan):** feedback→deploy median cycle time, % feedback auto-filed vs.
human-triaged, dedup precision, auto-shipped-change rollback rate.

### D.2 Phased timeline (weeks)

| Weeks | Phase | Build & marketing milestones |
|---|---|---|
| **1–2** | Pre-P0 build | Ship feedback widget + classifier + issue service (B); onboarding checklist + nudges (A.3); funnel instrumentation (C.5); trial-extension endpoint (A.2). Brand LinkedIn account onboarded (dogfood, throttled). |
| **3–5** | **P0 private beta** | 25 early adopters on 60-day trial; feedback loop live; iterate on top clusters; brand content + low-volume outreach warming. Exit-gate check at W5. |
| **6–9** | **P1 open beta** | Public self-serve signup; extended-trial auto-grant for first 100; ramp brand outreach + newsletter + SEO + email nurture; referral live. Weekly GA-gate metric review. |
| **10–12** | GA readiness | Hit GA thresholds (A.4); auto-changelog cadence steady; optimization loop tuning channels. |
| **13** | **GA / official release** | Owner approves GA `risk:product-decision`; remove beta caps; full-volume (still ToS-safe) marketing engine; loop continues perpetually. |

### D.3 Risks & mitigations

| Risk | Mitigation |
|---|---|
| **LinkedIn ToS / automation limits (self-marketing)** | Brand account uses the *same* per-day caps, human-like pacing, 429/auth-wall backoff (`rate_limit.py`), and per-user residential proxy (`proxy.py`) as paying users; a tripped breaker pauses brand outbound. Outbound volume is a `risk:product-decision` gate. No special "faster" path. |
| **Email deliverability** | Warm-up ramp, opt-in only, suppression list, SPF/DKIM/DMARC, throttled sends, engagement-based list hygiene; monitor bounce/complaint rate as a KPI with auto-throttle. |
| **Feedback-loop abuse / spam** | Per-user rate limits, `category=noise` classifier filter, feature clusters need a demand threshold before `agent:ready`, dedup prevents issue spam. |
| **Over-automation quality risk (bad auto-shipped change)** | Every code change goes through CI + Copilot review + (for risky ones) `risk:product-decision`/`needs-human` Decision Comment before merge (RUNBOOK). Auto-shipped changes are scoped, tested (≥80% patch coverage), and reversible; rollback rate is a tracked KPI. Nothing touching billing, outbound-at-scale, migrations, or security can auto-ship. |
| **Off-brand / hallucinated marketing content** | Anti-AI-slop authenticity guardrails + brand-voice alignment + similarity review gate; AI-disclosure applied; never fabricate facts (READER-mode humanization policy). |
| **Trial-extension / discount economics** | Capped cohorts (25 / 100), atomic slot counter, and both length + discount are `risk:product-decision` — owner sets the economics once; can't runaway. |
| **The `risk:product-decision` gate itself** | It is the primary protection against bad auto-shipped changes and runaway outbound: the pipeline **holds** any such issue/PR at merge, posts lettered options + a recommendation, and waits for the owner's one-line letter reply — turning safety into a 10-second decision, not a bottleneck. |

---

## Appendix — Buildable components filed as `agent:ready` issues

The concrete automation this plan requires is filed as separate scoped issues (see the repo issue list). Items
that do outbound-at-scale or auto-modify billing additionally carry `risk:product-decision` so the pipeline holds
them for owner sign-off. Each issue references this document.
