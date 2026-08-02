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

### Reasoning headroom — measuring a model, not the harness's budget (#842)

Every case fixture carries a `max_tokens`, and a **reasoning** model bills its chain-of-thought
against it: the answer is whatever is left. At the fixtures' budgets `minimax-m3` and `glm-5.2`
returned an EMPTY string with `finish_reason='length'`, which the deterministic layer scores as *the
model produced nothing* — a `reject` earned by the harness, not by the model. The in-runner judge had
the same failure at its old 200-token budget: every verdict came back empty and read as
`judge:timeout`, so a run could never produce the judge RATE the standing spend policy needs.

So a completion that spent its whole budget **before emitting anything** is retried at double
(`BENCHMARK_TRUNCATION_RETRIES`, default 2 — 1400 → 2800 → 5600), champion and candidate alike. A
truncated-but-**non-empty** answer is never re-rolled: that is the model's real output at that
budget, and re-rolling it would hand the verbose models a second attempt the concise ones never got.
Cases that needed the headroom are named in the report under 🧠 *reasoning headroom* rather than
silently absorbed. Only the FINAL attempt is measured — a discarded one is harness waste production
never pays, so its wall-clock is not charged to the model's p50/p90 — and a retried verdict counts
its real completions against `BENCHMARK_MAX_JUDGE_CALLS`, so the run's cap still bounds spend.

**Where the retry does NOT measure production.** Long-form generation sets no `max_tokens` at all
(`ai_helper.py`), so on `lem-complex` a doubled budget is closer to the real call than the fixture's
own cap was. The short tiers are the opposite: `lem-simple`'s `simple-relevance-yes-no` is
`max_tokens: 3` precisely because `ai_helper.py`'s relevance check is, and there a model that cannot
answer inside the budget is disqualified in production, not merely truncated. The retry currently
applies to every case, so on those cases it measures a model LEM could not actually run at that call
site. Calibrating that (per-case opt-out, or scoring the first attempt for production-mirror
budgets) is tracked on #910 with the rest of the harness-calibration work.

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

### Usage level — what a swap COSTS (issue #842)

The gate scores quality. It says nothing about price, and on Ollama Cloud those are separate
questions: metering is by the model's **usage level** (Low / Medium / High / Extra high), so
promoting a High model over a Medium one raises quota burn on every call that tier serves. That is a
spend decision, not a free upgrade.

So the harness carries the level **beside** the scores and never gates on it:

- every scorecard has a `Usage` column, champion and candidate alike;
- every gate verdict carries a `usage_delta` (`up` / `flat` / `down` / `unknown`), rendered under
  the expectations, and it rides on the JSON that `--recommendations-out` writes;
- a recommendation that raises the level renders **⚠️ quota increase** with the step count, and the
  Swap-recommendations section adds a "decide the extra quota burn deliberately" note.

`unknown` is **not** `flat`. A level that could not be read renders with the same warning an
increase gets — the failure this prevents is a High model being adopted as if it were free because
nobody could see its level. Unknown is common and expected: ollama.com publishes the Usage stat only
on **cloud-only** model pages, so models that are also pullable locally (`gpt-oss:120b`,
`qwen3.5:397b`, `gemma4:31b`) have no level to scrape. Supply theirs from the cloud listing:

```bash
--usage-levels gpt-oss:120b=medium,qwen3.5:397b=medium
```

`--no-usage-levels` skips the fetch entirely (one page request per measured model); a fetch that
fails leaves that model `unknown` rather than failing the run. On an **unattended** run there is
nobody to type that flag, so the same string is read from `BENCHMARK_USAGE_LEVELS` (an explicit
flag wins) — see *Unattended runs* below.

### Standing spend policy — when a quota increase may be taken (#842)

The delta says what a swap costs. The policy says who gets to accept that cost, and the owner
settled it once (#842 decision `2A`) so a run does not park for the same question every time:

> A usage-level **increase** is adoptable only on `lem-complex` — long-form is the one tier where
> quality *is* the product — and only on a **strict** judge-rate win. A tie is not worth +1 usage
> level on every call that tier serves.

`QUOTA_INCREASE_TIERS` in `scripts/benchmark_models.py` is that rule, and every `recommend` verdict
carries a `quota_policy` of `adopt` / `hold` with the reason, rendered under the verdict and again
per recommendation. `hold` is **not** a gate — the quality verdict is unchanged and the swap is
still recommended; it means the swap goes to the owner rather than into a config PR. `unknown`
holds too, for the same reason it never renders as `flat`: a level nobody read cannot be checked
against a rule written about increases.

### What the 2026-08-02 run settled (#842)

The first real run of this harness — `bm-20260802-20ae40`, four candidates against both live
champions, report beside this file — answered the two questions #717 left open. Both answers are
**keep**, and both are recorded here so the next roster refresh starts from a measurement rather
than from the spec sheets again:

| Question | Measurement | Decision |
|---|---|---|
| `minimax-m3` / `glm-5.2` as `lem-complex`'s quality option | Both **High (3)** vs champion `qwen3.5:397b` **Medium (2)** — `+1` usage level. `glm-5.2` 70% deterministic / 86% judge, `minimax-m3` 40% / 75%, champion 50% / **100%** | **Keep `qwen3.5:397b`.** Neither beat the champion on judge rate, and the standing spend policy buys a usage-level increase only on a *strict* judge-rate win. |
| Demote `gpt-oss:120b` on `lem-medium` now that `deepseek-v4-flash` + `gemma4:31b` cover the tier | `deepseek-v4-flash` **ties** it deterministically (60% vs 60%) and beats it on judge (83% vs 50%) at the same Medium (2) level; `gemma4:31b` is worse deterministically (40%) though cheaper (Low (1)) | **Keep `gpt-oss:120b` as champion.** Nothing scored a `recommend`, and #717's own rule is that no `recommend`-less candidate gets promoted. `deepseek-v4-flash` is the model to re-measure first next time. |

No swap was recommended, so `.litellm/config.yaml` is unchanged and no restart was owed. All four
tiers were smoke-tested green against the live proxy anyway (`lem-simple`, `lem-medium`,
`lem-complex`, `lem-router` — 1-token completions, HTTP 200 on each).

Two caveats the run itself surfaced, both tracked on #910 rather than papered over here:

- **The absolute deterministic floor (90%) was met by nobody, champions included.** The relative
  half of every verdict is sound, but a floor the incumbent fails cannot open for a challenger
  either. The per-case failures are real model behaviour (long-form overruns `max_chars`,
  contrastive frames, the #617 comment contract), not harness artifacts — the suites score a FIRST
  draft where production ships an n-th.
- **A reasoning model's single-run score is not stable.** `minimax-m3` returned an empty completion
  on two cases after exhausting the shipped truncation retry; re-run at the same effective budget it
  answered and passed both. That would have moved it at most to a tie on each tier, so the decision
  above stands either way — but one run of one case is a data point, not a verdict.

**Read this run's p50/p90 with one correction.** `bm-20260802-20ae40` was measured before the
per-attempt timing rule above, so a retried case charged every discarded attempt to the model.
`minimax-m3` (9 of 10 `lem-complex` cases retried) and `glm-5.2` (5 of 10) are inflated in that
run's leaderboard rows by roughly the retry factor; the deterministic and judge columns, which is
what the verdicts turn on, are unaffected. Later runs report the answering attempt only, so do not
compare them against these two rows as if they were the same measurement.

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

### Unattended runs (#842)

A real run needs a key, so it used to need a person. Four env vars are the whole difference between
"paste me the report" and a run that happens on its own:

| Variable | Why it has to be in the environment |
|---|---|
| `BENCHMARK_ENABLED=true` | Without it `--run` prints "nothing to do" and exits 0 — a silent no-op, not an error. |
| `OLLAMA_CLOUD_URL` | `https://ollama.com/v1`, the direct API that serves the bare model ids (`deepseek-v4-flash`, not `deepseek-v4-flash:cloud`). |
| `OLLAMA_CLOUD_API_KEY` | The metered credential. Never in the repo, never in a report. |
| `BENCHMARK_USAGE_LEVELS` | The incumbents' levels, which ollama.com does not publish. Unsupplied is `unknown`, and `unknown` holds a swap — so a run without it can recommend but can never conclude. |

`POSTHOG_PERSONAL_API_KEY` + `POSTHOG_API_KEY` are optional: both present uses PostHog Evaluations
as the judge, otherwise the run falls back to the in-runner judge and says so in the report.

The report is the deliverable — `--results-out` for the JSON a later `--render` replays, and
`--recommendations-out` for the swap list a follow-up config PR reads. Anything the standing spend
policy marks `hold` is named in the report and belongs to the owner, not to the config PR.

## Leaderboard

<!-- LEADERBOARD:BEGIN -->
| Date | Run | Tier | Model | Role | Deterministic | Judge | p50 | Verdict |
|---|---|---|---|---|---|---|---|---|
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `qwen3.5:397b` | champion | 50% | 100% | 26955 ms | baseline |
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `deepseek-v4-flash` | candidate | 70% | 57% | 3275 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `gemma4:31b` | candidate | 80% | 57% | 2428 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `minimax-m3` | candidate | 40% | 75% | 30564 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `glm-5.2` | candidate | 70% | 86% | 14477 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `gpt-oss:120b` | champion | 60% | 50% | 1635 ms | baseline |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `deepseek-v4-flash` | candidate | 60% | 83% | 1310 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `gemma4:31b` | candidate | 40% | 100% | 807 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `minimax-m3` | candidate | 50% | 80% | 5405 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `glm-5.2` | candidate | 40% | 75% | 6315 ms | reject |
<!-- LEADERBOARD:END -->
