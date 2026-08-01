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

Three streams fire around every call. **Never sum spend or token counts across them.**

| | `llm_call` (app, `observability.track_llm_call`) | `$ai_generation` (proxy) |
|---|---|---|
| Cost | LEM's *estimate* from `estimate_llm_cost_usd`, zeroed on a cache hit | the provider's own `response_cost` |
| Model | the **serving model** LiteLLM actually ran (`model`), with the requested tier alias preserved as `model_tier` | the provider model that served it |
| Feeds | `cost_ledger` rollups, the margin report, budget alerts (§C/§E of the margin plan) | PostHog's LLM-analytics product: generations, traces, per-model/user breakdowns, evals |
| Use for | anything a dollar figure is reported from | latency, error rate, model mix, per-user/feature volume |

Rule of thumb: **money questions use `llm_call`** (it is the ledger's source and joins to Stripe);
**everything else uses `$ai_generation`** (it is the truth about what ran). An insight must pick one
— a "total LLM spend" chart that unions both double-counts every call.

### The third stream: `shadow_cost_usd`

For subscription-priced models (Ollama Cloud), the real marginal cost is $0, so both `cost_usd`
and `$ai_total_cost_usd` read $0. That is correct for today's bill, but it hides what the same
usage would cost if LEM left the subscription. `llm_call` therefore carries a separate
`shadow_cost_usd` property for those models — a hand-picked metered reference price documented in
`.litellm/model_prices_snapshot.json`.

| Stream | What it answers | Do NOT use it for |
|---|---|---|
| `cost_usd` (`llm_call`) | What LEM's ledger/margin report books today | summing with `shadow_cost_usd` — they're different decisions |
| `$ai_total_cost_usd` (`$ai_generation`) | The provider's actual metered charge (often $0 for Ollama Cloud) | the "what if we left?" question |
| `shadow_cost_usd` (`llm_call`) | What the same tokens would cost at a metered reference | billing or the margin report |

Caveat worth knowing when the two are compared: a LiteLLM **cache hit** still emits
`$ai_generation`, at `$ai_total_cost_usd` 0 like `llm_call`'s `cached` path, but its
`$ai_input_tokens` are the served request's, so token volume reads higher than billable tokens.

## Pipeline traces — `$ai_trace` / `$ai_span` (issue #746)

A post is not one model call. Research → draft → refine → humanize → authenticity → review is six,
and until they shared a trace the question the analytics exist to answer — *what did THIS post cost,
end to end?* — had no answer at all: every call was its own isolated `$ai_generation` with its own
proxy-minted `$ai_trace_id`.

Grouping is **app-side work**, because only the app knows where a pipeline starts. The
`$ai_generation` stream is untouched: no extra event, no changed property, no LiteLLM config change.

| Piece | Where |
|---|---|
| `llm_trace()` / `llm_span()` + the `@llm_pipeline` / `@llm_step` decorator forms | `utilities/observability.py` |
| Putting the ids on the wire | `utilities/ai/client.py` (`_attach_trace`) |
| Kill switch | `LLM_TRACING_ENABLED` (default on), read at call time |

### The two wires

LiteLLM's PostHog logger sources the two ids from two different places, so LEM has to send them two
different ways. Getting this wrong is silent — the events still ship, they just don't nest.

- **`x-litellm-trace-id` header** → becomes the proxy's own `litellm_trace_id`, which it publishes as
  `$ai_trace_id`. It **cannot** be sent as request metadata: the logger reads `$ai_trace_id` from its
  own standard logging payload and would overwrite anything we put in metadata.
- **`metadata.parent_run_id`** → the logger maps it to `$ai_parent_id` and keeps the key out of the
  copied-through properties, so the generation nests under the span that made it.

Both live in the shared client, for the same reason attribution does: a step that forgot them would
drop out of its post's trace and nothing would say so. Tracing rides in its own hook rather than
inside `_attach_attribution`, which bails out whenever the caller supplied its own metadata —
`_call_llm` always does, so folding them together would have excluded nearly every generation LEM
makes.

### The shape

`@llm_pipeline` marks a function that IS one pipeline and supersedes `attribute_llm_cost` on it — it
opens the trace *and* the same `llm_attribution()` scope, reading the user id off the call's own
`user_id` argument. Three are marked: `create_text_post` (`post_generation`),
`generate_newsletter_edition` (`newsletter_edition`), `generate_ai_response` (`comment_generation`).

`@llm_step` goes on the STEP FUNCTION, never at a call site. Newsletters and comments draw draft,
research, humanize and authenticity from the same shared content core as posts, so decorating the
core is what gives every pipeline a legible trace without touching a single caller. Current spans:
`research`, `draft`, `refine`, `hook`, `humanize`, `authenticity`, `review`.

Two invariants worth knowing before you add either:

- **A trace opened inside an open trace becomes a span**, not a second trace. `create_text_post`
  recurses into itself for post-type fallbacks, and two half-traces of one post answer nobody.
- **A span outside a pipeline is a no-op.** Most calls into the shared core are not part of a
  pipeline; an orphan span is something PostHog cannot render, and the work must run identically
  either way. Same posture as the rest of this file: telemetry is never a reason to lose the
  generation, so a capture failure is swallowed at DEBUG.

### Reading it

`$ai_trace` / `$ai_span` carry `$ai_trace_id`, `$ai_span_id`, `$ai_span_name`, `$ai_latency`
(**seconds** — `llm_call.latency_ms` is the other convention and the two must not share a chart),
plus LEM's `feature` / `user_id` and `$ai_is_error` / `$ai_error` when the step raised. distinct_id
follows the same rule as every other event here: the user id, or the `"system"` sentinel.

The de-dupe rule above still holds inside a trace — the money number is `llm_call`, and summing the
generations under one trace gives you the provider's charge, not LEM's ledger.
