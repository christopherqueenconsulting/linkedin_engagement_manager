# Model-tier benchmarks

Rolling standing of every model LEM has measured against a LiteLLM tier contract (issue #721).

Each row is one model × one tier from one run of `scripts/benchmark_models.py`. Per-run detail —
which case failed, what the judge said, latency and token counts — lives in the dated report beside
this file (`<date>-<run_id>.md`).

## Why this exists

`scripts/model_health_check.py` (#716) detects that a model is *about to change*: a published
retirement, a new Ollama Cloud tag, a family we trail. It cannot say whether a candidate is any
**good** — before this, a swap was decided on a spec sheet. The benchmark is the measurement half,
and it always runs the current **champion** alongside the candidate, so a verdict is a comparison
rather than an absolute claim about one model.

## What is measured

| Tier | Contract |
|---|---|
| `lem-simple` | Short outputs ≤300 chars: refine, brief summarize, comma lists. Obeys the shape, no preamble. |
| `lem-medium` | Feed comments (must satisfy the #617 comment quality contract), post refinement, blog summaries, DMs. |
| `lem-complex` | Long-form thought leadership / personal story / industry news / carousels / newsletter editions — blueprint-shaped and slop-lint clean. |
| `lem-router` | The `LEMComplexityRouter` classification contract, plus a clean answer at the router alias. |

Two scoring layers, in this order:

1. **Deterministic** (free, in-repo, the source of truth) — length caps, comma-list shape, JSON
   validity, `slop_lint.lint_report`, `content_framework.comment_contract_report`, burstiness,
   lexical self-similarity, and `routing_policy.complexity_tier`. A case that fails here never
   spends a judge call.
2. **LLM judge** (paid, capped) — PostHog Evaluations scoring the harness's own tagged
   `$ai_generation` events, filtered to a `benchmark_run_id` so it can never bill against
   production traffic. With no judge provider configured, the runner degrades to an in-runner
   `lem-medium` judge and emits `$ai_metric` — that degradation is decided by the RESULT, not by
   config the runner cannot see: PostHog's judge needs a provider key of its own (it cannot judge
   via Ollama Cloud), so a run whose evaluations exist but score nothing spends what is left of the
   cap on the in-runner judge and reports the mode as `posthog-evals+in-runner-judge`. A verdict
   that never arrives either way is `judge:timeout` — never a fabricated score, and never dropped
   from the scorecard, so a partial read cannot render as a full-marks judge pass.

Suite inputs are **synthetic** prompt templates in `tests/benchmarks/model_tiers/`. No customer
content, credentials or production logs appear in a report; the renderer refuses any run that is
not tagged as benchmark output.

## The gate

A candidate is emitted as a **swap recommendation** only when it clears the tier's absolute
thresholds *and* meets-or-beats the champion on every graded expectation. Weaker outcomes are
reported but go no further:

| Verdict | Meaning |
|---|---|
| `recommend` | Cleared every floor and matched or beat the champion. The only verdict that becomes a recommendation. |
| `recommend-deterministic-only` | Cleared the deterministic floors, but judge evidence was missing on one side. Advisory. |
| `no-baseline` | No champion measured for that tier — nothing to compare against. |
| `reject` | Failed at least one expectation. |

A recommendation is **not** a change. `.litellm/model_upgrades.yaml` is the RETIREMENT map and the
reactive half of the model-health check auto-swaps whatever lands in it, so adopting a benchmark
winner is a deliberate edit to `.litellm/config.yaml` (or a #717-style PR). The report renders the
exact mapping lines for a human to take.

## Running it

```bash
# Offline proof — scores the suites against each case's committed canned output. No network at all.
poetry run python scripts/benchmark_models.py --dry-run --models qwen3.6:400b --out-dir /tmp/bm

# Validate the suites only.
poetry run python scripts/benchmark_models.py --print-suites

# A real run (needs BENCHMARK_ENABLED, OLLAMA_CLOUD_URL / OLLAMA_CLOUD_API_KEY).
poetry run python scripts/benchmark_models.py --run --models qwen3.6:400b --results-out /tmp/bm.json

# Re-render a report from a saved results file (what the cron's PR worktree does).
poetry run python scripts/benchmark_models.py --render /tmp/bm.json --out-dir docs/model-benchmarks
```

`scripts/weekly_model_check.sh` invokes it automatically for the candidates the #716 catalog scan
found, and opens the report as a PR. A benchmark failure alerts but never blocks the retirement-swap
safety path.

## Leaderboard

<!-- LEADERBOARD:BEGIN -->
| Date | Run | Tier | Model | Role | Deterministic | Judge | p50 | Verdict |
|---|---|---|---|---|---|---|---|---|
<!-- LEADERBOARD:END -->
