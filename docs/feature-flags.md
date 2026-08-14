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
| No `POSTHOG_API_KEY` or no `POSTHOG_PERSONAL_API_KEY` | env var |
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
| `POSTHOG_PERSONAL_API_KEY` | — | Required for local evaluation (scope `feature_flag:read`). Must be present in the **app** containers, not only in the cron scripts' environment. |
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
