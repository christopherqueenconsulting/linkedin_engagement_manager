# Prompt evals: tracking, versioning, datasets, grading

LEM sends about 75 LLM requests from code. Most prompts are inline f-strings in `utilities/ai/ai_helper.py`;
the rest are module constants in a dozen other files. This doc covers how every prompt is
**found**, **versioned**, **graded** against a dataset, and **re-checked** when the prompt, a grader or the
model list changes. The model-tier benchmark (`docs/model-benchmarks/README.md`) answers a different
question: whether a tier should swap models, scored on synthetic cases. Prompt evals run the real
production prompts.

Live inventory, generated: [`docs/prompt-evals/inventory.md`](prompt-evals/inventory.md).

## Status: what ships in which phase

| Phase | What | State |
|---|---|---|
| 1 | Inventory, drift guard, versioning lock, capture, hermetic guards, this doc | **shipped** |
| 2 | Datasets (≥20 rows) + code graders + rubrics for 8 wave-1 prompts | next |
| 3 | `scripts/benchmark_prompts.py`: change-driven runner, model graders, spend plan | |
| 4 | `.github/workflows/prompt-evals.yml` (owner-landed) + the first baseline run | |
| 5 | Remediation PR for every wave-1 prompt that fails its floor | |
| 6 | Waves 2–3: datasets for the remaining planned prompts, about 10 per PR | |

Sections below marked *(phase N)* describe the design the later phases implement.

## 1. Tracking: every call site is in the registry

`tests/benchmarks/prompts/registry.yaml` has one entry per call site. Entries are keyed
`module:function#index` exactly as `python scripts/prompt_registry.py --scan` prints them.

- **Found by AST, never a hand-kept list.** The scan covers `src/cqc_lem` and `scripts/` and looks for
  `chat.completions.create`, `responses.create`, `images.generate`, `images.edit`, and calls to the
  in-repo wrappers in `WRAPPERS`: `_call_llm` anywhere, and `_complete` inside `curated_commentary`.
  - A prompt sent through a wrapper is registered at its **caller**. The wrapper's own transport
    call is `exempt:wrapper`.
  - Two calls in one function are two prompts (`#0`, `#1`).
  - Embeddings and TTS are not scanned, because their input is content to encode or speak, not an
    instruction for a model.
- **The guard.** `tests/unit/scripts/test_prompt_registry.py` fails the Unit Tests check when the
  code has a call site with no entry, or the registry has an entry whose call site is gone. Fix it by
  adding or re-keying the entry, then running `python scripts/prompt_capture.py --write`.
- **Status:**
  - `evaluated`: captured, with a dataset and graders.
  - `planned`: tracked and versioned, but no dataset yet.
  - `exempt:<reason>`: not graded here, and the reason why. Examples: vision and image calls, which
    `benchmark_media.py` grades; live web research, which is not reproducible; untiered legacy
    tools; eval-harness transports.
- **Family** decides the graders and rubric: `post_longform`, `comment`, `dm`, `classifier`,
  `json_planner`, `rewrite`, `summary`, `media_prompt` or `judge`.

## 2. Versioning: `prompts.lock.json`

Every tracked prompt has a version from day one. The version follows one of two hashes:

| Prompt | Version follows | Moves when |
|---|---|---|
| **Captured** (has a `capture:` block) | `content_hash`: the rendered messages plus the contract parameters (`model`, `response_format`, `max_tokens`, `tools`, `tool_choice`) | the text production sends changes. A refactor does not move it, and neither does a resampled temperature. |
| **Not captured yet** | `source_hash`: the enclosing function (docstrings stripped) plus every module constant it names | the function body or a constant it reads changes. This is coarser, because a refactor moves it too. |

- `scripts/prompt_capture.py --write` re-renders every prompt and bumps the version of each one whose
  hash moved. The old hash goes into `history` along with the date it was retired, and
  `--changed-in "PR #N"` labels the new version. The same command regenerates the captures and the
  inventory.
- **The guard** (`tests/unit/scripts/test_prompt_capture.py`) fails the build in three cases. Any
  prompt edit therefore shows up in a PR as a message diff plus a version bump.
  1. A committed capture no longer matches what the builder renders.
  2. The lock is stale.
  3. Against `origin/main`, when that ref is in the checkout, a hash changed without a version bump.
     This is the hand-edit case.
- **Hashing is interpreter-stable.** `_canon` serialises the AST itself, because `ast.dump` changed
  in 3.13 and CI runs 3.12.

### Capture is hermetic

Capture calls the **real** builder, so it runs under every guard in `tests/hermetic.py`. These are the
same guards `tests/unit/conftest.py` installs in the unit lane:

- no LLM endpoint, and no second `openai.OpenAI`
- no MySQL, Redis, Celery broker or Selenium Grid

It also runs in a pinned environment:

- `CAPTURE_ENV` replaces the whole environment, so no host secret is visible, and flags take the env fallback
- the date is frozen at `CAPTURE_DATE`
- `random` is seeded

The recorder replaces `chat.completions.create` on the shared `AttributedOpenAI` client and answers
with the entry's canned `reply`. A builder that sends nothing on its canonical input is an error, and
nothing is written for it.

To capture a new prompt, add to its registry entry:

```yaml
    capture:
      target: cqc_lem.utilities.ai.lead_intent._llm_says_lead   # dotted production builder
      canonical: {text: "..."}                                   # kwargs it is called with
      reply: "yes"                                               # canned model answer
      select: -1                                                 # which recorded call (default last)
      env: {}                                                    # extra pinned env (optional)
```

A builder that needs the database gets a pure `build_<x>_messages(inputs)` extracted, and production
calls it too. Do this rather than patching the database, because a patch list breaks silently on
every refactor.

## 3. Datasets *(phase 2)*

`tests/benchmarks/prompts/datasets/<id>.jsonl` holds one JSON row per case, with these fields:

- `id`
- `source`: `synthetic` or `golden`
- `persona`: from `personas.json`
- `inputs`: builder kwargs
- `context`
- `labels`
- `tags`, for edge cases: `edge:empty`, `edge:long`, `edge:non_english`, `adversarial:injection`,
  `persona:non_tech`, `mix:promo`
- `canned`: an output used by `--dry-run`

Rules:

- Each evaluated prompt has at least 20 rows, and classifiers are label-balanced.
- `authenticity_rubric.GOLDEN_SET` is the dataset for `judge.authenticity`.
- **The repo is public.** No harvested production content is committed. The datasets are a
  *regression set*, not a held-out set, because anything committed here can be read.
- Changing a dataset bumps `dataset_version` in the lock, and that triggers a re-run.

## 4. Grading: code graders and model graders *(phase 2–3)*

Each prompt declares its graders. A grader is versioned by a hash of its code or rubric. Editing one
grader re-grades only the stale pairs, and it reuses stored outputs instead of regenerating them.

### Code graders

Code graders are deterministic and free, and they run on every case. They are the
`scripts/benchmark_models.py` `ASSERTIONS` plus wrappers around production gates, so an eval fails for
the same reason production would.

| Grader | Wraps | Class |
|---|---|---|
| `no_preamble`, `max_chars`, `min_chars`, `max_lines`, `forbidden_phrases`, `required_phrases` | existing harness | contract |
| `production_json` | `ai_helper._loads_json_object` + required keys/types | contract |
| `enum_label` / `label_match` | allowed set; accuracy + macro-F1 vs `labels.expected` | contract |
| `slop_lint` | `slop_lint.lint_report`, HARD vs WARN per surface | repairable |
| `comment_contract` | `content_framework.comment_contract_report` | repairable |
| `max_similarity`, `min_burstiness` | existing harness | repairable |
| `meeting_ask` | `content_alignment.contains_meeting_ask` | repairable |
| `ungrounded_metrics` | `ai_helper._ungrounded_first_person_metrics` | repairable |
| `dwell` | `content_framework.dwell_report` | repairable |

A **contract** failure counts against the floor. A **repairable** failure is reported and compared
with the champion, but it is advisory, because production retries it (#910).

### Model graders (LLM-as-judge)

- **Rubrics.** `tests/benchmarks/prompts/rubrics/<family>.md` has 3–5 binary criteria with pass/fail
  anchors. The judge returns per-criterion JSON, and a case passes when every criterion passes.
  A timeout or unparseable answer is recorded as `judge:timeout` or `judge:unparseable` and is
  **never** turned into a score.
- **Judge model.** Claude Sonnet via OpenRouter (`models.yaml` → `judge`).
  - It is reached through `benchmark_routed.build_routed_client`, not the proxy. Anthropic is not on
    the proxy (#2059), and `fallback_judge` hard-codes `lem-medium`.
  - A `GET /models` preflight verifies the id.
  - The judge is priced from its own entry.
  - Its family must differ from every candidate's. Sonnet is never a candidate, because
    `anthropic/*` ids are refused for adoption.
- **Calibration.** Each run first re-scores about 20 hand-labelled outputs per family from
  `rubrics/<family>.calibration.jsonl`. If agreement is below 0.85, model grades are reported as
  `judge:uncalibrated` and the verdict uses code graders only.
- **Sampling.** The judge sees 4–6 cases per prompt per model, stratified by tag.
- **Pairwise mode.** For a candidate against the champion, the judge picks between the two outputs,
  shown in random order. Cases are sampled independently of the contract results. A contract fail
  is an automatic loss, and a fail on both sides is a tie.

### Verdict per prompt@version × model

Floors are fixed before the baseline run, from product contracts, not from what models score today:

| Component | Floor |
|---|---|
| Contract pass rate | Wilson lower bound ≥ 0.90 (classifier and JSON: raw ≥ 0.95) |
| Classifier accuracy | ≥ 0.90 |
| Judge pass rate (calibrated) | ≥ 0.80 |
| Pairwise win-or-tie vs champion | ≥ 0.5 (candidates only) |

Every new or changed `prompt@version` gets two samples. A prompt **fails** when its serving champion
misses a floor on both. A candidate that misses is a finding about the *model*, not the prompt. A
deployed fallback that misses is reported as `fails-on-fallback`.

## 5. Models: `tests/benchmarks/prompts/models.yaml`

- **Champions and fallbacks** are read from `.litellm/config.yaml`, not repeated here.
- **Candidates** carry `status: considering | adopted | rejected`. To evaluate a new model, add it as
  `considering`. The next run grades every tracked prompt in its tiers against it. A candidate that
  passes every prompt in its tier triggers a promotion-proposal issue; one that fails more than 20% of
  them is proposed `rejected`.
- **`proxy_host`.** CI runs the free Ollama champions as their OpenRouter-hosted equivalents
  (labelled `proxy-host`). The production Ollama key never goes into GitHub, because eval traffic would
  spend the quota that live traffic needs.

## 6. The change-driven runner *(phase 3–4)*

`scripts/benchmark_prompts.py --plan` compares the tree with `eval_state.json` and builds a work list.
When nothing changed, it does nothing.

| Trigger | Work |
|---|---|
| New prompt, or new `id@version` | Generate and grade on its tier's champion, fallbacks and considering candidates |
| `dataset_version` changed | Same |
| New candidate, or a champion changed in `config.yaml` | Every evaluated prompt in that tier, for that model only |
| A grader's hash changed | **Re-grade only**, from the outputs stored in the prior run's artifact (no generation spend). If the artifact has expired, regeneration is a priced line |
| Nothing | Exit 0, no PR |

The work list is priced (`plan_text_spend`, judge included) before any call, and it is refused over
`max_spend`. A mass bump from a shared directive builder needs `force_full` or a raised cap. An
all-errored run is refused (#923), never written up as zeros. Reports
(`docs/prompt-evals/<date>-<run>.md`) carry scores, versions, hashes and grader counts, but
**no generated text**.

## 7. Automation

- **On every PR (free, already required).** The registry, capture and version guards run inside
  `Unit Tests (Python 3.12)`.
- **Weekly *(phase 4)*.** `.github/workflows/prompt-evals.yml` runs on cron Monday 04:00 UTC and on
  `workflow_dispatch`, with inputs `prompt_ids`, `models`, `max_spend` and `force_full`. It:
  - uses `concurrency: prompt-evals`;
  - runs the plan, then the evals;
  - uploads outputs as an artifact (`download-artifact` with `run-id` for re-grades);
  - force-updates one `bot/prompt-evals` branch and its PR, opened with `RELEASE_DISPATCH_TOKEN` so
    the required checks run;
  - files one deduplicated `prompt-eval:failing` issue per failing `prompt@version`.

  The secret is a dedicated, capped `PROMPT_EVAL_OPENROUTER_KEY`. **The owner lands this workflow
  file**, because the pipeline credential has no `workflows` scope.

## 8. Fixing a failing prompt

1. Edit the prompt.
2. Run `python scripts/prompt_capture.py --write --changed-in "PR #N"`. This re-captures it and bumps
   its version.
3. Show before and after for both samples, with no regression on any other row.
4. The next weekly run grades the new version automatically.

Wording that changes voice or tone is `risk:product-decision`, and nothing here is auto-merged.

## Files

| File | Written by |
|---|---|
| `tests/benchmarks/prompts/registry.yaml` | hand |
| `tests/benchmarks/prompts/prompts.lock.json` | `prompt_capture.py --write` |
| `tests/benchmarks/prompts/captured/<id>.json` | `prompt_capture.py --write` |
| `tests/benchmarks/prompts/models.yaml` | hand |
| `tests/benchmarks/prompts/eval_state.json` | the eval workflow's results PR only |
| `docs/prompt-evals/inventory.md` | `prompt_registry.py --inventory` (also run by `--write`) |
| `tests/hermetic.py` | shared guards: unit lane + capture |
