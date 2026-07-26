# LLM analytics — LiteLLM → PostHog (`$ai_generation`)

Issue #647. Every LEM model call goes through the LiteLLM proxy, so the proxy is the one place that
sees the *real* model, the *real* token counts and the provider's *own* cost number. LiteLLM's
native PostHog logger publishes that as PostHog's first-class LLM-analytics event — no app code runs
per call, so nothing can be forgotten at a new call site.

## What ships

| Where | What |
|---|---|
| `.litellm/config.yaml` | `litellm_settings.success_callback` / `failure_callback` include `posthog`; `turn_off_message_logging: true` |
| `docker-compose.yml` (`litellm`) | `POSTHOG_API_KEY`, `POSTHOG_API_URL` (from the app's `POSTHOG_HOST`) |
| `utilities/ai/client.py` | stamps `metadata: {user_id, feature}` on every `lem-*` request |

Requires LiteLLM **≥ 1.77.3** for the logger, and the `posthog serilization fix` (BerriAI/litellm
[#20668](https://github.com/BerriAI/litellm/pull/20668), 2026-02-07) for the batch-send crash
reported as [#18332](https://github.com/BerriAI/litellm/issues/18332) — the batch is now written
with `safe_dumps`, so a non-JSON-serializable value in the proxy's own metadata (`UserAPIKeyAuth`)
degrades to a string instead of dropping every event. The stack runs
`ghcr.io/berriai/litellm:main-latest`, which is well past both.

## Failure mode

The callback is best-effort by construction: LiteLLM initializes it lazily and treats a failure as
non-blocking (`_init_custom_logger_compatible_class` logs and returns `None`). A missing
`POSTHOG_API_KEY` therefore costs no generations — it just silently produces no events. If
`$ai_generation` is missing in PostHog, check that env var on the box before anything else.

## Event shape

`$ai_generation` (`$ai_embedding` for `client.embeddings.create`), one per call:

- `$ai_model`, `$ai_provider` — the model that actually served it, *after* fallbacks and cost-aware
  down-routing. This is the honest answer to "what did `lem-complex` really run on?"
- `$ai_input_tokens`, `$ai_output_tokens`, `$ai_total_cost_usd`, `$ai_latency`
- `$ai_is_error` / `$ai_error` on the failure callback
- `$ai_trace_id`, `$ai_span_id`
- `feature` — LEM's own bucket (`content` / `comment` / `dm` / `newsletter` / `marketing` /
  `system`), from the request metadata
- **distinct_id** = the request's `metadata.user_id`

### Attribution

`utilities/ai/client.py` attaches `metadata` to every request whose model is a `lem-*` tier alias,
reading the ambient `llm_attribution()` scope. It lives in the client, not at the call sites,
because a call that skips it is invisible in cost routing *and* lands on an anonymous PostHog
person — a silent failure nobody would notice. `_call_llm` still sets its own metadata first so an
explicit `_track_user_id` / `_track_feature` beats the ambient scope.

Unattributed traffic sends `user_id: "system"` rather than omitting the field: LiteLLM would
otherwise fall back to the trace id and mint a throwaway person per call. `"system"` is the same
sentinel `observability.py` uses server-side and the SPA's `String(user_id)` convention rounds out —
one PostHog person per user across browser, app and proxy. `routing_policy.assign_arm` reads that
sentinel exactly like a missing id, so system traffic still lands in the experiment's control arm.

### Privacy

`turn_off_message_logging: true` redacts `$ai_input` / `$ai_output_choices` before any callback sees
them. LEM's prompts are the user's own LinkedIn material — profile synthesis, story-bank anecdotes,
draft DMs — and the SPA already masks exactly that content (`maskProps`). Metrics are unaffected.
Turning it off gives PostHog's generation view the full conversation text; that is a deliberate
product decision, not a default.

## De-dupe: which stream answers which question

Both streams fire on every call. **Never sum spend across them.**

| | `llm_call` (app, `observability.track_llm_call`) | `$ai_generation` (proxy) |
|---|---|---|
| Cost | LEM's *estimate* from `estimate_llm_cost_usd`, zeroed on a cache hit | the provider's own `response_cost` |
| Model | the tier alias the caller asked for (`model`, plus `model_tier`) | the provider model that served it |
| Feeds | `cost_ledger` rollups, the margin report, budget alerts (§C/§E of the margin plan) | PostHog's LLM-analytics product: generations, traces, per-model/user breakdowns, evals |
| Use for | anything a dollar figure is reported from | latency, error rate, model mix, per-user/feature volume |

Rule of thumb: **money questions use `llm_call`** (it is the ledger's source and joins to Stripe);
**everything else uses `$ai_generation`** (it is the truth about what ran). An insight must pick one
— a "total LLM spend" chart that unions both double-counts every call.

Caveat worth knowing when the two are compared: a LiteLLM **cache hit** still emits
`$ai_generation`, at `$ai_total_cost_usd` 0 like `llm_call`'s `cached` path, but its
`$ai_input_tokens` are the served request's, so token volume reads higher than billable tokens.

## Not in scope here

`$ai_trace` / `$ai_span` hierarchies — wrapping a post's full generation chain
(research → draft → review → humanize) into one readable trace. Every event already carries a
`$ai_trace_id`, but it is per-call; grouping them is app-side work via the `posthog-python` AI
helpers. Tracked as the stretch half of #647.
