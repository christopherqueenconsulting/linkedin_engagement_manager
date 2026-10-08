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
| 2 | Datasets (≥20 rows) + code graders + rubrics + calibration sets for 8 wave-1 prompts | **shipped** |
| 3 | `scripts/benchmark_prompts.py`: change-driven runner, model graders, spend plan | **shipped** |
| 4 | `scripts/prompt_eval_publish.py` + the workflow (owner-landed) + the first baseline run | **shipped** (workflow awaits the owner) |
| 5 | Remediation PR for every wave-1 prompt that fails its floor | next, after the baseline |
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
- **Family** decides the graders and rubric: `post_longform`, `comment`, `own_comment`, `dm`,
  `classifier`, `json_planner`, `rewrite`, `summary`, `media_prompt` or `judge`. The defaults for
  each family live in the `families:` block at the top of `registry.yaml` (§4).

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
      patches: {dotted.name: value}                              # upstream steps replaced (optional)
```

A builder that makes several calls is scored on its **first draft** (`select: 0`), with the extra calls
switched off in `env`: `HUMANIZE_ENABLED=0` drops the humanize rewrite, `CONTENT_RESEARCH_ENABLED=0`
drops live research, and `COMMENT_GATE_MAX_ATTEMPTS=1` makes one gate attempt. This is the harness's
#910 rule: a suite scores a first draft, while production ships an n-th. An upstream LLM step that only
*feeds* the prompt, such as the trend analysis behind a thought-leadership post, is replaced through
`patches`, so the row supplies its output instead of the step running.

A builder that needs the database gets a pure `build_<x>_messages(inputs)` extracted, and production
calls it too. Do this rather than patching the database, because a patch list breaks silently on
every refactor.

## 3. Datasets

`tests/benchmarks/prompts/datasets/<id>.jsonl` holds one JSON row per case:

```json
{"id": "cf-scrap", "source": "synthetic",
 "inputs": {"post_content": "...", "profile": {"$persona": "quality_lead"},
            "profile_synthesis": {"$synthesis": "quality_lead"}, "prefs": {"comment_length": "medium"}},
 "patches": {},
 "context": {"post_content": "..."}, "labels": {}, "tags": ["persona:non_tech"],
 "canned": {"output": "..."}}
```

- **`inputs`** are the builder's keyword arguments. Two placeholders resolve against
  `personas.json`, which holds 5 synthetic personas (hospital operations, fractional CFO, SaaS
  founder, plant quality lead, people leader):
  - `{"$persona": name}` becomes a `LinkedInProfile`;
  - `{"$synthesis": name}` becomes that persona's voice synthesis.
- **`patches`** replace upstream steps for this row (see §2).
- **`context`** is what graders read, for example `post_content` for `comment_contract` and
  `ungrounded_metrics`.
- **`labels.expected`** is the ground truth for `label_match`.
- **`tags`** mark edge cases: `edge:empty`, `edge:long`, `edge:non_english`,
  `adversarial:injection`, `persona:non_tech`, `mix:promo`, `near_miss`, `ambiguous`, `golden`.
- **`canned.output`** is a hand-written answer. Capture feeds it to the builder as the model's
  reply, and the dry run grades it.
- **`expect_no_request: true`** marks a row the builder must short-circuit before any model call,
  such as an empty or URL-only post. It is asserted, then left out of the suite.

`python scripts/prompt_capture.py --render <id>` renders a dataset through the real builder into a
suite. The output is a list of harness cases in the tier-suite format, with sampling parameters
dropped. It is rendered at run time and **not committed**: the comment system prompt alone is several
KB per row. Rendering is deterministic, because `random` is seeded per row from its id. A builder's
random picks, such as a comment blueprint, therefore vary across rows but never between runs.

**Rules,** enforced by `tests/unit/scripts/test_prompt_datasets.py`:

- At least 20 rendered rows per evaluated prompt.
- Unique ids, and personas that resolve.
- Classifier and judge labels balanced to within one.
- Coverage of `edge:long`, `edge:non_english` and `adversarial:injection`, plus `mix:promo` for
  the comment and post families.
- Every row renders through the real builder into a suite `bm.load_prompt_suite` accepts.
- **Every canned answer clears all of its graders,** so the dry run is green for a reason.

Further rules:

- `authenticity_rubric.GOLDEN_SET` (6 items) seeds `judge.authenticity`.
- **The repo is public.** Every row is synthetic: no real people, companies or harvested text. The
  datasets are a *regression set*, not a held-out set.
- **Datasets are versioned.** Editing a row moves `dataset_hash`, and `--write` bumps
  `dataset_version` (with `dataset_history`) without bumping the prompt's version. `--check` reports
  it as "dataset changed". Phase 3's runner re-runs the eval on that bump.

### Wave 1

| Prompt | Builder | Family | Rows |
|---|---|---|---|
| `comment.feed` | `generate_ai_response` | comment | 22 (2 `expect_no_request`) |
| `comment.seed` | `generate_seed_comment` | own_comment | 20 |
| `post.thought_leadership` | `get_thought_leadership_post_from_ai` (trend step patched) | post_longform | 20 |
| `dm.nurture` | `generate_nurture_dm` | dm | 20 |
| `classify.post_relevance` | `post_is_relevant` | classifier | 20, yes/no 10/10 |
| `classify.lead_intent` | `lead_intent._llm_says_lead` | classifier | 20, yes/no 10/10 |
| `classify.dm_reply_intent` | `dm_nurture._llm_intent` | classifier | 20, 4 per intent |
| `judge.authenticity` | `content_alignment.score_authenticity` | judge | 20, keep/demote 10/10 |

### Adding a row

1. Append a line to the dataset.
2. Run `python scripts/prompt_capture.py --write`, which bumps `dataset_version`.
3. Run `pytest tests/unit/scripts/test_prompt_datasets.py`.

If the row's canned answer fails a grader, fix the answer, not the grader. A grader that disagrees
with production is a bug in the grader, and it gets its own PR.

## 4. Grading: code graders and model graders

Every row is scored by its family's default graders, then the entry's own `assertions`, then the
row's. The family defaults live in the `families:` block of `registry.yaml`:

| Family | Contract | Repairable | Rubric |
|---|---|---|---|
| `comment` | `no_preamble`, `max_chars 700` | `comment_contract`, `slop_lint(comment)`, `meeting_ask`, `ungrounded_metrics` | `comment` |
| `own_comment` | `no_preamble`, `max_chars 700` | `slop_lint(own_post_comment)`, `meeting_ask` | `comment` |
| `dm` | `no_preamble`, `max_chars 300` | `slop_lint(dm)`, `meeting_ask` | `dm` |
| `post_longform` | `no_preamble`, `max_chars 3000` | `slop_lint(post)`, `min_burstiness 0.3`, `meeting_ask`, `ungrounded_metrics`, `dwell ≥ 40` | `post_longform` |
| `classifier` | entry's `label_match` (`yes_no` or `enum`) | none | none (labels are truth) |
| `judge` | `production_json[score]`, `label_match(authenticity_action, 60)` | none | none |

`own_comment` is the user's own first comment on their post. Production never runs it through the
third-party comment contract or the grounding gate, so neither grades it here. The phase-2 dataset
test caught that mismatch.

*(phase 3)* A grader is versioned by a hash of its code or rubric. A grader is versioned by a hash of its code or rubric. Editing one
grader re-grades only the stale pairs, and it reuses stored outputs instead of regenerating them.

### Code graders

Code graders are deterministic and free, and they run on every case. They are the
`scripts/benchmark_models.py` `ASSERTIONS` plus wrappers around production gates, so an eval fails for
the same reason production would.

| Grader | Wraps | Class |
|---|---|---|
| `no_preamble`, `max_chars`, `min_chars`, `max_lines`, `forbidden_phrases`, `required_phrases` | existing harness | contract |
| `production_json` | `ai_helper._loads_json_object` + `required_keys` (tolerates the fence production tolerates; `json_object` stays the strict check) | contract |
| `label_match` | reads the answer the way the call site does: `yes_no` = `startswith("y")`; `enum` = `dm_nurture`'s normaliser + allowed `values`; `authenticity_action` = `_coerce_authenticity_result` score ≥ threshold. Compared with `labels.expected`; `bm.aggregate_labels` turns the predictions into accuracy + macro-F1 | contract |
| `slop_lint` | `slop_lint.lint_report`, HARD vs WARN per surface | repairable |
| `comment_contract` | `content_framework.comment_contract_report` | repairable |
| `max_similarity`, `min_burstiness` | existing harness | repairable |
| `meeting_ask` | `content_alignment.contains_meeting_ask` | repairable |
| `ungrounded_metrics` | `ai_helper._ungrounded_first_person_metrics` | repairable |
| `dwell` | `content_framework.dwell_report` | repairable |

A **contract** failure counts against the floor. A **repairable** failure is reported and compared
with the champion, but it is advisory, because production retries it (#910).

### Model graders (LLM-as-judge)

- **Rubrics.** `tests/benchmarks/prompts/rubrics/{comment,dm,post_longform}.md` each have 4–5
  binary criteria with pass/fail anchors. A rubric judges only what code cannot (engages *this*
  post, fits the intent, grounded in the inputs, voice); length, slop, meeting asks and invented
  numbers are already code-graded. The judge returns per-criterion JSON, and a case passes when every criterion passes.
  A timeout or unparseable answer is recorded as `judge:timeout` or `judge:unparseable` and is
  **never** turned into a score.
- **Judge model.** Claude Sonnet via OpenRouter (`models.yaml` → `judge`).
  - It is reached through `benchmark_routed.build_routed_client`, not the proxy. Anthropic is not on
    the proxy (#2059), and `fallback_judge` hard-codes `lem-medium`.
  - A `GET /models` preflight verifies the id.
  - The judge is priced from its own entry.
  - Its family must differ from every candidate's. Sonnet is never a candidate, because
    `anthropic/*` ids are refused for adoption.
- **Calibration** *(sets shipped in phase 2; scoring in phase 3)*. Each run first re-scores the hand-labelled outputs in
  `rubrics/<rubric>.calibration.jsonl`. There are 20 per rubric, 50–60% of them fails, with per-criterion
  labels and an `overall` that must equal "all criteria pass". If agreement is below 0.85, model grades are reported as
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

Every new or changed `prompt@version` gets two samples, and the contract rate and its Wilson bound
are taken over all of them, so one bad sample counts. A prompt **fails** when its serving champion
misses a floor. A candidate that misses is a finding about the *model*, not the prompt. A
deployed fallback that misses is reported as `fails-on-fallback`.

## 5. Models: `tests/benchmarks/prompts/models.yaml`

- **Champions and fallbacks** come from `.litellm/config.yaml`, never repeated here.
  `benchmark_prompts.tier_deployments` reads each tier's deployments in config order. The first one
  is the **champion**; the rest are deployed **fallbacks**, because a prompt is really served by them
  during an outage.
- **Candidates** carry `status: considering | adopted | rejected`, and only `considering` ones are
  graded.
  - To evaluate a new model, add it as `considering`. The next run grades every evaluated prompt in
    its tiers against that model only.
  - For a one-off trial, use `--models a,b`.
- **The CI route (`wire_id`).**
  - A `proxy_host` mapping wins. The free Ollama champions run as their OpenRouter-hosted
    equivalents, because the production Ollama key never goes into GitHub: eval traffic would spend
    the quota live traffic needs.
  - Otherwise a provider-qualified id is used as-is.
  - A bare Ollama tag with no mapping is **skipped and named** in the run output. It never blocks
    the reachable models.
- **Prices.**
  - The pinned `.litellm/model_prices_snapshot.json` comes first. It deliberately prices the Ollama
    tags at $0, but their OpenRouter twins are **not** free, so they are priced by id and never by
    the tag.
  - Ids the snapshot lacks are priced from OpenRouter's public model list. `--plan` and `--run` read
    it; `--offline` skips it.
  - The judge's `price_id` names its pinned entry, for the case where the OpenRouter id differs in
    punctuation.
  - An id that still has no price **refuses the run**.

## 6. The change-driven runner: `scripts/benchmark_prompts.py`

```bash
python scripts/benchmark_prompts.py --plan                     # work list + priced plan, no calls
python scripts/benchmark_prompts.py --dry-run --out-dir /tmp/x # grade canned outputs, no network, no state
PROMPT_EVALS_ENABLED=1 OPENROUTER_API_KEY=… python scripts/benchmark_prompts.py --run \
    --max-spend-usd 15 --outputs-out outputs.json --artifact-ref <run-id> [--prompt-ids a,b] [--force-full]
```

`build_work_list` compares the tree with `eval_state.json`, so each prompt@version × model is measured
once:

| Trigger | Work |
|---|---|
| New prompt, or new `id@version` | **generate**: 2 samples per row (`--samples`) on the champion, fallbacks and considering candidates |
| `dataset_version` moved | generate |
| New candidate, or a deployment change in `config.yaml` | generate, for that model only |
| A grader's version moved (`grader_versions`: a hash of each code grader's source and each rubric file) | **regrade**: the outputs stored by a prior run (`--outputs-in`) are re-graded, with no generation spend. Without them it regenerates |
| A model's role moved (the config reordered its tier: a fallback promoted to champion) | regrade: same outputs, but the verdict now carries the new role and a candidate's pairwise baseline is the new champion |
| Nothing | "No work", exit 0 |

**What a run does,** in this order:

1. **Prices** every call as a worst-case ceiling (`plan_spend`). This covers generation at the wire
   model's price, and judge, pairwise and calibration calls at the judge's own price. A plan over
   `--max-spend-usd` (default `PROMPT_EVAL_MAX_SPEND_USD` or $15) or with an unpriced id is refused.
   `routed.SpendMeter` caps the real spend again, call by call.
2. **Renders** each suite through the real builders (`prompt_capture.render_suite`).
3. **Generates** with the champion first, so candidates can be compared against it. It uses
   `benchmark_routed.build_routed_client` on OpenRouter.
4. **Grades** every sample with the code graders.
5. **Calibrates** each rubric once per run against its hand-labelled set. Below 0.85 agreement, the
   judge is reported but never decides the verdict.
6. **Judges** a tag-stratified sample (`--judge-sample`, default 5) per prompt × model.
7. **Pairwise-compares** each candidate with the champion.
8. **Refuses** an all-errored run (#923). Otherwise it writes the report, the leaderboard
   (`docs/prompt-evals/README.md`), the eval state and the inventory.

`--outputs-out` writes the generated text for the workflow's artifact. It is **never committed**, and
the report carries scores only.

**Exit codes:** `0` means ok or no work. `1` means refused or an error. `2` means a **champion** or
fallback missed a floor (`failing_prompts`), which is what the workflow files issues from. A candidate
missing a floor is a finding about the model, not the prompt.

## 7. Automation

- **On every PR (free, already required).** The registry, capture, version and dataset guards run
  inside `Unit Tests (Python 3.12)`.
- **Weekly: `.github/workflows/prompt-evals.yml`.** The reviewed source is
  [`docs/prompt-evals/prompt-evals.workflow.yml`](prompt-evals/prompt-evals.workflow.yml).
  - **Who lands it:** the owner copies the file into `.github/workflows/` and commits it, because the
    pipeline credential has no `workflows` scope.
  - **Triggers:** cron, Monday 04:00 UTC, and `workflow_dispatch`. The dispatch inputs are
    `prompt_ids`, `models`, `max_spend` (default 15) and `force_full`.
  - **Concurrency:** `concurrency: prompt-evals`. A run that has already spent is never cancelled.
  - **Steps:**
    1. Download the previous successful run's `prompt-eval-outputs` artifact (`gh run download`), so a
       grader-only change re-grades for free.
    2. Run `benchmark_prompts.py --run` with exit codes:
       - `0`: ok, or no work.
       - `2`: a floor was missed. This is reported as issues, not as a red run.
       - Anything else: refused or an error, which fails the job.
    3. Upload `outputs.json` for 90 days. It is never committed.
    4. Run `scripts/prompt_eval_publish.py`. It force-pushes the state, report, leaderboard and
       inventory to ONE branch, `bot/prompt-evals`, and opens its PR with `RELEASE_DISPATCH_TOKEN`, so
       the required checks run. It also files or updates one `prompt-eval:failing` issue per failing
       `prompt@version`, using a hidden marker as the dedup key. A re-run comments on the existing
       issue instead of filing another.
  - **Secrets:**
    - `PROMPT_EVAL_OPENROUTER_KEY`: a dedicated OpenRouter key with a hard monthly limit (about $40 is
      suggested).
    - `RELEASE_DISPATCH_TOKEN`: the existing secret.
  - **The baseline:** after landing the workflow, dispatch it once with `prompt_ids=classify.lead_intent`
    and `max_spend=1` as a smoke test of the key, the spend preflight, the artifact and the results PR.
    Then dispatch `force_full=true` for the baseline.

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
| `tests/benchmarks/prompts/datasets/<id>.jsonl` | hand (synthetic only) |
| `tests/benchmarks/prompts/personas.json` | hand (synthetic only) |
| `tests/benchmarks/prompts/rubrics/<rubric>.md`, `.calibration.jsonl` | hand |
| `tests/benchmarks/prompts/prompts.lock.json` | `prompt_capture.py --write` |
| `tests/benchmarks/prompts/captured/<id>.json` | `prompt_capture.py --write` |
| `tests/benchmarks/prompts/models.yaml` | hand |
| `tests/benchmarks/prompts/eval_state.json` | the eval workflow's results PR only |
| `docs/prompt-evals/inventory.md` | `prompt_registry.py --inventory` (also run by `--write`) |
| `tests/hermetic.py` | shared guards: unit lane + capture |
| `scripts/benchmark_prompts.py` | the runner (phase 3): plan, generate, grade, judge, record |
| `scripts/prompt_eval_publish.py` | the workflow's last step: results PR + deduplicated failure issues |
| `docs/prompt-evals/prompt-evals.workflow.yml` | hand; the owner copies it to `.github/workflows/prompt-evals.yml` |
| `docs/prompt-evals/<date>-<run>.md`, `docs/prompt-evals/README.md` (leaderboard) | `benchmark_prompts.py` (dry runs write wherever `--out-dir` says) |
