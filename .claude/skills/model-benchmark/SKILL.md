---
name: model-benchmark
description: Manually invoked only — run scripts/benchmark_models.py against the model-tier contract suites and interpret the champion/challenger gate. Real runs need creds and cost money.
disable-model-invocation: true
---

# Model benchmark: run and decide

```bash
poetry run python scripts/benchmark_models.py --print-suites                                  # validate suites, no network
poetry run python scripts/benchmark_models.py --dry-run --models <m> --out-dir /tmp/bm        # offline proof vs canned outputs
poetry run python scripts/benchmark_models.py --run --models <m> --results-out /tmp/bm.json   # real run
poetry run python scripts/benchmark_models.py --render /tmp/bm.json --out-dir docs/model-benchmarks
```

Real runs require: `BENCHMARK_ENABLED=true` (else `--run` silently no-ops), `OLLAMA_CLOUD_URL=https://ollama.com/v1` + `OLLAMA_CLOUD_API_KEY`, and `BENCHMARK_USAGE_LEVELS` — unsupplied levels grade `unknown`, and **unknown holds a swap**. Optional PostHog keys switch the judge to PostHog Evaluations.

Gate semantics: always benchmark candidates **beside the current champion**; `contract` checks are the absolute floor (production consumes the failure), `repairable` are advisory (a regeneration gate retries them); one run is not a verdict. A run where every case of every model errored — champion included — is a **harness outage**: refuse to publish (exit 1), never render a scorecard of zeros. Partial failures publish with an `Unmeasured cases` count.

Only `recommend` verdicts become swaps, and recommendations are **RENDERED into the report, never written** to config — `.litellm/model_upgrades.yaml` is the retirement map, owned by the weekly cron (`scripts/weekly_model_check.sh`). Spend-policy `hold`s belong to the owner, not a config PR.

Authoritative: `docs/model-benchmarks/README.md` (methodology, leaderboard, unattended-run env table).
