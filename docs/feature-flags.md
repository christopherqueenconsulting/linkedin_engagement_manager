# Feature flags (issue #651)

LEM's operational toggles were env vars: changing one meant editing `/opt/lem/.env` and restarting
the stack. PostHog feature flags make the same toggles changeable at runtime, with percentage
rollouts and per-user targeting. `src/cqc_lem/utilities/flags.py` is the ONE place that knows how a
flag is read; `GET /api/flags` is the ONE place the SPA reads them from.

## The contract: fail open to the env var

A flag service is a dependency. An engagement automation that changed behaviour because PostHog was
unreachable would be worse than one that can't be toggled at all — so **every** path that isn't a
clear PostHog answer returns the flag's env var:

| Situation | Value used |
|---|---|
| `POSTHOG_FLAGS_ENABLED=false` | env var |
| No `POSTHOG_API_KEY` or no runtime personal key | env var |
| Definitions not loaded yet / load failed | env var |
| Flag not defined in PostHog | env var |
| Flag defined but not decidable locally | env var |
| SDK raises | env var |
| Flag evaluates to `true` / `false` / a variant | **PostHog** |

A deployment that never creates a single PostHog flag behaves exactly as it did before this existed.
The env var is therefore not a legacy leftover — it is the documented answer to "what happens if
PostHog is down", and it stays the default for every registered flag.

## Local evaluation, and what it costs you

Lookups pass `only_evaluate_locally=True`. The SDK's background poller pulls flag *definitions* into
the process every `POSTHOG_FLAG_POLL_SECONDS` (default 30) and every lookup is evaluated in memory —
so a Celery loop checking a flag per feed post makes **zero** network requests, and a flip reaches a
long-running worker within one poll interval without a restart.

The one network call is the definition fetch at a process's FIRST flag check (the SDK caps it at
10s); a failure there falls back to env vars and is retried no more often than one poll interval, so
an outage can never turn a feed loop into a fetch loop.

The constraint local evaluation buys: a flag whose release condition needs person properties only the
server holds **cannot be decided locally**, and every worker would silently fall back to its env var.

> **Registered flags must use rollout percentage / distinct-ID conditions only.**

`send_feature_flag_events=False` on every lookup, too: a `$feature_flag_called` event per checked
feed post is volume nobody reads.

`distinct_id` follows `observability.py`'s convention — `str(user_id)`, or the `"system"` sentinel —
so a per-user rollout targets the SAME PostHog person that user's own events land on.

## What is NOT a flag, and never will be

Safety controls stay in Redis/env, where they are one authority a PostHog outage, a mis-targeted
rollout or an errant %-ramp cannot touch:

- the 429 breaker and `hold_commenting` (`utilities/linkedin/rate_limit.py`)
- `pause_automation()` / the suppression tripwire (`utilities/suppression.py`)
- every per-day cap (`max_comments_per_day`, `max_dms_per_day`, `max_invites_per_day`)
- `COST_AWARE_ROUTING_ENABLED`, the proxy half of cost routing — `utilities/routing_policy.py` is
  mounted into the LiteLLM container and must stay stdlib-only, so it can never import `flags.py`

## The registry

Flag key == registry name == the string in PostHog. Add a `FlagSpec` to `FLAGS` in
`utilities/flags.py`; call sites use the module constant, never a bare string, so a typo raises
instead of silently evaluating to `False` inside a Celery task.

| Flag key | Env fallback | Default | Owner | What it governs |
|---|---|---|---|---|
| `comment-research-enabled` | `COMMENT_RESEARCH_ENABLED` | `false` | content | Per-comment web research (`content_research.py`). Expensive at feed volume — the toggle most worth trialling on a cohort. Scoped per user. |
| `tutorial-videos-enabled` | `TUTORIAL_VIDEOS_ENABLED` | `false` | marketing | The weekly tutorial producer (`video_tutorials.py`) AND the SPA section that embeds its output — one toggle covers both. System-scoped. |
| `feed-fallback-when-empty-default` | `FEED_FALLBACK_WHEN_EMPTY_DEFAULT` | `true` | engagement | Fleet default for `feed_fallback_when_empty` for users with no SAVED engagement row. A saved per-user setting always wins. |
| `cost-routing-enabled` | `COST_ROUTING_ENABLED` | `false` | cost | App side of cost-aware down-routing (`cost_routing.py`) — written into the published routing policy. System-scoped. |
| `posthog-surveys-enabled` | `POSTHOG_SURVEYS_ENABLED` | `false` | growth | Gates the headless PostHog survey renderer (issue #653, `docs/surveys.md`). Keep OFF until the SPA bundle carries a `VITE_POSTHOG_KEY` and the surveys are launched. |
| `newsletter-editor-enabled` | `NEWSLETTER_EDITOR_ENABLED` | `false` | content | Final mechanical LLM edit pass on newsletter drafts (capitalization, grammar, punctuation, formatting) before slop-lint review. Adds one `lem-medium` call per draft. |
| `video-motion-lint-hold` | `VIDEO_MOTION_LINT_HOLD_ENABLED` | `false` | content | ENFORCEMENT for the motion-prompt lint (issue #1277, `docs/content-core.md`). The lint always grades and emits `motion_prompt_check`; this decides whether a HARD violation buys a steered rewrite and then HOLDS the render. OFF = warn-only, so the credit-spend profile is unchanged until it is flipped. Scoped per user. |
| `video-captions-enabled` | `VIDEO_CAPTIONS_ENABLED` | `false` | content | Burns the post's opening line into generated video posts for LinkedIn's muted autoplay (issue #1278, `video_captions.py`). One extra ffmpeg pass per video post; no LLM spend. Scoped per user. An avatar-led video needs the separate `users.avatar_caption_overlay` opt-in as well — the flag alone never paints over a likeness. |
| `occasion-native-publish-enabled` | `OCCASION_NATIVE_PUBLISH_ENABLED` | `true` | content | Automated Selenium publishing of approved occasion/milestone drafts through LinkedIn's native "Celebrate an occasion" composer (issue #1088, Phase 2 of #1074). Scoped per user, and read at BOTH ends — the scheduler never queues the message with it off, and the task refuses if it is flipped off mid-flight. Shipped OFF until `--occasion-composer` had been run live; #1621 and #1693 grounded the route (share box, occasion type, template chooser) and the owner authorised the flip on #1088, so it now defaults ON. Setting it off falls back to #1074's copy-and-paste handoff. |
| `animated-post-enabled` | `ANIMATED_POST_ENABLED` | `false` | content | Animated loop posts (`utilities/animated_loop.py`, `docs/animated-posts.md`). A text post's gated still is also animated into a subtle cinemagraph GIF (one standard-tier Runway render per post) and published through the versioned Images API instead of the still. Scoped per user and read at BOTH ends (production and publish), so a %-rollout is the A/B. Any loop failure ships the still. Needs a live grounding pass before it is enabled. |
| `comment-candidate-pool-enabled` | `COMMENT_CANDIDATE_POOL_ENABLED` | `false` | engagement | Daily comment-candidate pool, Phase 1 of #2635 (`utilities/comment_candidates.py`, table `comment_candidates`). The feed scan and roster pass save every scored post that passes `comment_safety` and `min_reactions`, deduped on the `commented_posts` key; ranked by roster membership + topic relevance + recency via `get_comment_candidates`. Nothing reads the pool yet; the flag-off path is a no-op. |
| `comment-ramp-enabled` | `COMMENT_RAMP_ENABLED` | `false` | engagement | Comment-volume ramp, Phase 5 of #2647 (`utilities/comment_ramp.py`, daily task `auto_comment_ramp`). State machine 20 -> 40 -> 70 -> 100 comments a day in Redis (`comment_ramp:<user_id>`), brand account only. Up needs a week at the step, a non-falling author-reply rate (v2 reader, 10+ scored), an `ok` demotion verdict, no 429 in 7 days (`linkedin:429_last_trip`) and no tripwire trip; a breach steps down in one run; any unreadable gate or Redis holds the step. It is the ONLY thing that lifts `BRAND_CAP_CEILINGS["max_comments_per_day"]` (`brand_cap_ceiling`). The 100/day clamp on every user's `max_comments_per_day` is NOT behind the flag. |
| `voice-exemplars-enabled` | `VOICE_EXEMPLARS` | `false` | content-quality | Voice from real writing, Phase 1a of #2627 (`utilities/ai/voice_exemplars.py`). `POST /user/voice-exemplars` takes a LinkedIn export `Comments.csv` / `Shares.csv`, dedupes, drops texts under 40 or over 1500 chars, caps 500 rows per user and 200 per upload, and embeds at ingest (`lem-embedding`). With the flag on and at least 4 embedded exemplars, the 5 nearest go into the feed-comment and nurture-DM prompts as few-shot and a comment draft within the comment cosine bar (0.82) of one is regenerated, so an exemplar is never reused verbatim. Below 4 the adjective brief is used and the prompt is byte-identical to flag-off. Phase 1b (same flag): the seed, second-wave, thread-reply, reply-followup, lead-response and group-post writers take the same block and near-copy check (`voice_context` / `redraft_if_copied`, one redraft). The first edit of a pending DM, catch-up touch or post keeps the drafted text in `voice_edit_labels`; approving stores original + final and ingests the final as an `edited` exemplar under the same caps — an untouched draft stores nothing. The SPA upload card is `VoiceExemplarsCard` (Voice section). |
| `comment-multi-candidate-enabled` | `COMMENT_MULTI_CANDIDATE` | `false` | content-quality | Multi-candidate feed comments, Phase 3a of #2627 (`utilities/ai/comment_candidates.py`). With the flag on the feed-comment writer draws 3 drafts per gate attempt, the deterministic gates grade each, and the `lem-judge` alias picks the one a busy practitioner would plausibly type from the clean ones (one clean draft skips the judge; none falls through to the normal gate path). `lem-judge` is a Google-family open-weight model (`gemma4:31b`), a different family from the OpenAI-family `lem-voice` writer (config test); its default is benchmark-gated against owner ratings (owner-run). Any judge failure fails open to the first clean draft. Off: one draft per attempt, byte-identical. The authenticity loop, block and override are Phase 3b. |
| `authenticity-loop-enabled` | `AUTHENTICITY_LOOP` | `false` | content-quality | Authenticity loop, Phase 3b of #2627 (`utilities/ai/authenticity_loop.py`). Comment and nurture-DM writers get authenticity pre-text; each draft is scored 0-100 by the judge and below the floor of 50 the specific issues go back for up to 3 regenerations. A DM that never reaches 50 is held PENDING with its score and issues stored and the user emailed the reasons; a comment that never reaches 50 is skipped (nothing is posted). Below 50, `PUT /dm` `approve` is refused 403 `authenticity_below_floor` and nurture auto-approve is skipped; the owner posts only with `authenticity_override_reason`, which writes an `authenticity_overrides` row (who, when, score, reason). A judge failure fails open. Off: byte-identical. |
| `comment-candidate-drafts-enabled` | `COMMENT_CANDIDATE_DRAFTS_ENABLED` | `false` | engagement | Candidate drafts, Phase 3 of #2647 (`utilities/comment_candidate_drafts.py`, hourly task `auto_draft_comment_candidates`). One draft per pooled candidate (status `new` -> `drafted`) from the `post_text` saved at scan time, through `generate_ai_response` and so the `COMMENT_CLAIMS_GATE` mode; a declined post is `skipped`. Needs the pool flag. Drafts appear as the `comment_candidates` lane of Daily review with the deterministic risk flags (a candidate score under 0.3 reads as a hold, so "approve all clean" never takes it); approve -> `approved`, which only the automated lane or Assist posts. Bounded by `COMMENT_DRAFT_BATCH` (40 a run) and `COMMENT_DRAFT_BACKLOG_MAX` (200 waiting). |
| `comment-candidate-crawl-enabled` | `COMMENT_CANDIDATE_CRAWL_ENABLED` | `false` | engagement | Nightly roster crawl, Phase 2 of #2647 (`utilities/comment_candidate_crawl.py`, task `crawl_comment_candidates`). Fills `comment_candidates` with source `crawl` through the Phase 1 recorder, read-only on LinkedIn. Needs the pool flag too. Runs only on weekdays 09:00-16:59 in the user's timezone (fixed window until the #2628 `session_window_decision` gate lands, then `crawl_window_open` is the one function to swap), once a day per user, capped by `COMMENT_CRAWL_DAILY_CAP` (default 200). |
| `curated-sources-enabled` | `CURATED_SOURCES_ENABLED` | `false` | content | Curated outside sources (`utilities/curated_sources.py`, `docs/curated-sources.md`). Collects candidates from the feed/roster, the RSS allowlist and a BLS watchlist, and lets the content plan draft at most one post in three (value/authority slots only) as a native reshare, a re-charted data card or a link post, each crediting its source. Read per user at the collector and the drafter; a post the owner already approved still publishes with the flag off, because the approval is the gate. The flag never approves: every curated draft is held PENDING until the owner approves it. |
| `weekly-report-enabled` | `WEEKLY_REPORT_ENABLED` | `false` | analytics | The weekly 5-minute report (`utilities/weekly_report.py`, #2632). `GET /api/user/weekly-report` answers 404 while it is off; read per user at the endpoint. Phase 1b (#2666): also gates the Friday `auto_weekly_report` beat (13:00 UTC), which emails each connected, active, non-opted-out user with activity once per ISO week (`weekly_report_sends` UNIQUE claim; `engagement_preferences.weekly_report_enabled` NULL = on, 0 = opt out; the email's opt-out link is the authenticated `/account` page). Rolled out to user 1 first. |
| `profile-audit-enabled` | `PROFILE_AUDIT_ENABLED` | `false` | analytics | The weekly (Mon 05:30 UTC) and monthly (1st, 06:00 UTC) profile audit beats (`utilities/profile_audit_job.py`, #2631 Phase 1). Browser-free: grades the cached own-profile JSON against the versioned rubric in `utilities/profile_audit.py` and stores `profile_audits` + `profile_audit_findings`, one audit per user per period (UNIQUE claim). Unreadable fields are `unknown`, never `fail`. Read-only: LEM never edits the LinkedIn profile. The monthly beat adds an LLM pass (`utilities/profile_audit_llm.py`, #2754, `lem-medium`, fail-open) comparing headline and About with stored ICP vocabulary (ICP definition phrases plus roster-scan post text); it stores a weight-0 `icp_language_gap` finding with the exact phrases, and the mined terms feed the `icp_keywords` check (`unknown` when none are stored). Phase 2 (#2753) also gates the reads behind it: `GET /user/profile-audit` (404 while off, 503 on a failed read), `POST /user/profile-audit/finding` (done / ignored / reopen, stored in `profile_audit_finding_actions`; an ignore is permanent, a done covers only the audit it was pressed on), the Account card, and one profile-audit line in the weekly report. The follower join is correlation only. || `impact-report-enabled` | `IMPACT_REPORT_ENABLED` | `false` | analytics | The impact-report beat (`utilities/impact_report_job.py`, #2643, `docs/impact-report.md`): emails the owner a 14-days-before/after report per release plus a monthly baseline, and posts one metric-free note on the tracker issue. Read per user at the beat; `GET /api/admin/impact-report` is not gated by it. Rollback: remove the beat entry. |
| `mcp-server-enabled` | `MCP_SERVER_ENABLED` | `false` | platform | The end-user MCP server at `POST /api/mcp` (`api/mcp/`, epic #2682, `docs/agent-mcp-plan.md`). Rollout only: it will gate the endpoint per user (gate step 9) and the Agents tab, mint, rotate and info routes. Registered in P1, where nothing reads it yet: the P1 endpoint is inert and 401s every bearer. NOT the safety control: that is the `mcp:kill` Redis key / `MCP_SERVER_KILL` env, which fails closed. Provision at 0% with owner-added overrides only, and never run `posthog_flags.py --apply` while those overrides exist (it replaces `groups` wholesale). |
| `mcp-server-writes` | `MCP_SERVER_WRITES_ENABLED` | `false` | platform | The MCP server's queue-only write tools (epic #2682, phase W2). Off: a write tool answers `isError` "writes not enabled". Registered in P1; nothing reads it until W2. Writes only ever create or edit PENDING items. |
| `mcp-settings-writes` | `MCP_SETTINGS_WRITES_ENABLED` | `false` | platform | The MCP settings tools `lem_update_settings`, `lem_undo_settings_change` and `lem_request_settings_change` (epic #2797, #2800, #2803). Off: none is listed and a call is refused. Needs `mcp-server-writes` too (they run through the same write path). Group-1 settings apply directly; a gate-loosening field is only ever a request the user approves in LEM. |
| `mcp-story-drafts` | `MCP_STORY_DRAFTS_ENABLED` | `false` | platform | `lem_add_story_draft` (#2804, epic #2797): an MCP agent may add a story-bank entry that lands UNVERIFIED and is never used by the content engine until the user confirms it in LEM. Checked in addition to `mcp-server-writes`. |

## Provisioning

`scripts/posthog_flags.py` creates these in PostHog from the registry itself — `utilities/flags.py`
is the source of truth, so a new flag is one registry entry, not two edits that drift.

```bash
python scripts/posthog_flags.py --print-spec   # no key needed; shows the planned rollouts
python scripts/posthog_flags.py --dry-run      # exit 2 when changes are pending
python scripts/posthog_flags.py --apply
```

**The safety property:** each flag is created at the rollout that reproduces what it resolves to
**today**. Every lookup currently falls back to `env_default()`, so a flag created at a different
value changes production the moment it appears — no deploy, no log line, no error.
`feed-fallback-when-empty-default` is the live example: it defaults to `true`, so it is provisioned
at **100%**. Creating it at the obvious-looking 0% would silently flip the fleet engagement default
for every user without a saved preferences row. The script derives the percentage rather than
accepting one, so that mistake is not reachable by hand.

Release conditions are rollout-percentage only, never person properties — `flags.py` evaluates
locally, and a condition needing server-held person properties falls back to env in every Celery
worker, which looks identical to the flag working.

## Adding a flag

1. Add a `FlagSpec` to `FLAGS` (key, env var, default, owner, description) and export a constant.
2. Add the env var to `.env.example` next to the feature it governs, noting that it is a flag
   fallback.
3. Replace the toggle read at the **call site**, not at import — a constant read at import time can't
   be flipped without a deploy. That is the single most common way to make a flag do nothing.
4. Create the flag in PostHog with the same key, using a rollout-percentage condition.
5. Add a row to the table above.

## The SPA

`GET /api/flags` returns `{distinct_id, flags: {key: bool}, local_evaluation: bool}` — every
registered flag, already resolved server-side. `local_evaluation: false` is the honest half: it means
every value in the payload is an env default, not a PostHog decision.

```tsx
import { FLAGS, useFeatureFlag } from '../hooks/useFeatureFlags'

const enabled = useFeatureFlag(FLAGS.tutorialVideos)   // fallback defaults to false
```

Three reasons the SPA reads flags from the API rather than from `posthog-js`:

- it is a **bootstrap** — one payload, so a gated section renders correctly on the FIRST paint
  instead of flickering from default to real value;
- it is the SAME evaluation the API and the workers just did, so the three can't disagree;
- it works with browser analytics fully off (no `VITE_POSTHOG_KEY`) — flags are a product control,
  not an analytics feature.

`utils/analytics.ts` stays the SPA's ONE PostHog surface for events/replay/identify. Don't add a
second client-side posthog reader for flags; that reintroduces exactly the split-brain this endpoint
removes.

An absent or invalid session resolves the same flags for the `"system"` identity rather than 401ing —
the landing page is logged out and still needs to know what to render. For the same reason
`/api/flags` sits in `_PUBLIC_API_PREFIXES`, outside the `API_ACCESS_TOKENS` credential gate: the
landing page is logged out, so it carries no credential of any kind, and the SPA's axios interceptor
treats any 401 as a dead session — a gated flags query would log a signed-in visitor out on `/`.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `POSTHOG_FLAGS_ENABLED` | `true` | Master kill switch. `false` = env vars only, no polling, no lookups. |
| `POSTHOG_RUNTIME_API_KEY` | — | Required for local evaluation (scope `feature_flag:read`), falling back to `POSTHOG_PERSONAL_API_KEY` when unset (issue #1453, `docs/kpi-dashboards.md`). Must be present in the **app** containers, not only in the cron scripts' environment. A key that can't evaluate flags fails SILENTLY — every flag just reads its env var. |
| `POSTHOG_FLAG_POLL_SECONDS` | `30` | Definition refresh interval — the worst-case delay before a flip is live. Doubles as the retry cooldown after a failed fetch. |

## Verifying

```bash
# What this process resolves, and whether PostHog is actually deciding any of it
docker exec celery_worker python -c \
  "from cqc_lem.utilities.flags import bootstrap_payload; print(bootstrap_payload(1))"

# The same payload the SPA bootstraps from
curl -s "$LEM_URL/api/flags" | jq .detail
```

`local_evaluation: false` with a personal key set means the definition fetch failed — check the
key's scopes and the worker's WARNING log (`PostHog feature-flag local evaluation unavailable`).
