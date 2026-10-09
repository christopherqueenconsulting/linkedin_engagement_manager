# Prompt eval runs

One row per prompt@version × model per run; written by `scripts/benchmark_prompts.py`. Posture: [`docs/prompt-evals.md`](../prompt-evals.md).

<!-- LEADERBOARD:BEGIN -->
| Date | Run | Tier | Model | Role | Contract | First draft | Judge | p50 | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-08 | `pe-20261008-0250fe` | classify.dm_reply_intent@1 | `openai/gpt-oss:20b` | champion | 1.0 (+35 no output) | 1.0 | — | — | fail |
| 2026-10-08 | `pe-20261008-0250fe` | classify.dm_reply_intent@1 | `openai/gpt-4o-mini` | fallback | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | classify.dm_reply_intent@1 | `openai/gpt-5.4-mini` | candidate | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | classify.lead_intent@1 | `openai/gpt-oss:20b` | champion | 1.0 (+33 no output) | 1.0 | — | — | fail |
| 2026-10-08 | `pe-20261008-0250fe` | classify.lead_intent@1 | `openai/gpt-4o-mini` | fallback | 0.95 | 0.95 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | classify.lead_intent@1 | `openai/gpt-5.4-mini` | candidate | 0.95 | 0.95 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | classify.post_relevance@1 | `openai/gpt-oss:20b` | champion | 0.975 | 0.975 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | classify.post_relevance@1 | `openai/gpt-4o-mini` | fallback | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | classify.post_relevance@1 | `openai/gpt-5.4-mini` | candidate | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | comment.feed@1 | `openai/gpt-oss:120b` | champion | 1.0 | 0.475 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | comment.feed@1 | `openai/gemma4:31b` | fallback | 1.0 | 0.7 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | comment.feed@1 | `openai/gpt-4o-mini` | fallback | 1.0 | 0.675 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | comment.feed@1 | `openai/gpt-5.4-mini` | fallback | 1.0 | 0.725 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | comment.seed@1 | `openai/gpt-oss:120b` | champion | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | comment.seed@1 | `openai/gemma4:31b` | fallback | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | comment.seed@1 | `openai/gpt-4o-mini` | fallback | 0.975 | 0.95 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | comment.seed@1 | `openai/gpt-5.4-mini` | fallback | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | dm.nurture@1 | `openai/gpt-oss:120b` | champion | 0.875 | 0.85 | — | — | fail |
| 2026-10-08 | `pe-20261008-0250fe` | dm.nurture@1 | `openai/gemma4:31b` | fallback | 0.975 | 0.975 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | dm.nurture@1 | `openai/gpt-4o-mini` | fallback | 0.825 | 0.775 | — | — | fail |
| 2026-10-08 | `pe-20261008-0250fe` | dm.nurture@1 | `openai/gpt-5.4-mini` | fallback | 0.825 | 0.8 | — | — | fail |
| 2026-10-08 | `pe-20261008-0250fe` | judge.authenticity@1 | `openai/gpt-oss:120b` | champion | 0.925 | 0.925 | — | — | fail |
| 2026-10-08 | `pe-20261008-0250fe` | judge.authenticity@1 | `openai/gemma4:31b` | fallback | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | judge.authenticity@1 | `openai/gpt-4o-mini` | fallback | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | judge.authenticity@1 | `openai/gpt-5.4-mini` | fallback | 1.0 | 1.0 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | post.thought_leadership@1 | `openai/gpt-6.1-sol` | champion | 1.0 | 0.5 | — | — | pass |
| 2026-10-08 | `pe-20261008-0250fe` | post.thought_leadership@1 | `openai/gpt-4o` | fallback | 1.0 | 0.2 | — | — | pass |
<!-- LEADERBOARD:END -->
