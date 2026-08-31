# Model-tier benchmarks

Rolling standing of every model LEM has measured against a LiteLLM tier contract (issue #721).

Each row is one model × one tier from one run of `scripts/benchmark_models.py`. Per-run detail —
which case failed, what the judge said, latency and token counts — lives in the dated report beside
this file (`<date>-<run_id>.md`).

## Why this exists

`scripts/model_health_check.py` (#716) detects that a model is *about to change*: a published
retirement, a new Ollama Cloud tag, a family we trail, or — since #925 — a tag we already run whose
*build* moved underneath an unchanged name. It cannot say whether a candidate is any
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
   spends a judge call. Reported as **two** rates — see *What the deterministic rates mean* below.
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

### What the deterministic rates mean — the floor, recalibrated (#910)

The first real run measured 40–80% deterministic pass rates for every model in the roster **including
both reigning champions**, against a 90% absolute floor. A floor the incumbent itself fails is not a
floor — it is a gate that can never open, so a genuinely better model would have landed as `reject`
and nobody would have noticed.

The failures were real model behaviour, not harness artifacts. What was wrong was treating them all
as the same KIND of failure. **The suite scores a first draft; production ships an n-th**, because
every surface with a deterministic quality check has a bounded regeneration behind it:

| Production path | What it retries against | What happens if it still fails |
|---|---|---|
| `ai_helper.lint_repaired` (seed comments, thread replies, DMs) | `slop_lint` hard checks | ships with a structured warning |
| `ai_helper._gated_comment` (#617 feed + second-wave comments) | comment quality contract, similarity, slop lint | the post is **skipped** — no comment is posted |
| `run_content_plan._review_generated_post` + `evaluate_post_gates` | slop lint, similarity, fabricated specifics | the post is **held** at pending for review |

So every assertion is now classified by what production does with its failure:

- **contract** — what the call site actually consumes. Broken JSON, a preamble pasted into LinkedIn
  copy, a classification returned as prose, a post over LinkedIn's own 3000-character limit, no
  output at all. Nothing repairs these.
- **repairable** — what a regeneration gate catches: `slop_lint`, `comment_contract`,
  `max_similarity`, `min_burstiness` by default, plus anything a fixture marks
  `"production": "repairable"`. Repairable-ness belongs to the CALL SITE, not to the check: on
  long-form, the tier's craft length target is repairable and the 3000-char hard limit beside it is
  not, so both live on the same case.

| Rate | Column | Role in the gate |
|---|---|---|
| `contract_pass_rate` | **Contract** | The ABSOLUTE floor (`contract_pass_rate`, default 0.9) and a meets-or-beats against the champion. |
| `deterministic_pass_rate` | **First draft** | **Advisory** — rendered with its target and the redraft count, but it never changes a verdict. Still a meets-or-beats against the champion, because needing more drafts than the incumbent is a real cost (tokens, latency, skipped comments) even when nothing broken ships. |

Read the gap between the two columns as **redraft cost, not shipped defects**. A model that clears
the contract floor only by burning three drafts a case is visible rather than flattered.

The floor can still be missed — that is the point. Replaying the #842 roster's own measured failures
through this calibration (`TestBm842Replay` in `tests/unit/test_benchmark_models.py`, one entry per
❌ line of the committed report) puts `lem-medium`'s champion at 100% contract and `lem-complex`'s at
70%: three of `qwen3.5:397b`'s long-form drafts blew LinkedIn's hard character limit, which is a real
defect the old aggregate hid. When the incumbent misses its tier's floor, every verdict on that tier
says so out loud rather than leaving a reader to infer it from two tables.

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

**Where the retry does NOT measure production (#910).** Long-form generation sets no `max_tokens` at
all (`ai_helper.py`), so on `lem-complex` a doubled budget is closer to the real call than the
fixture's own cap was. The short tiers are the opposite: `lem-simple`'s `simple-relevance-yes-no` is
`max_tokens: 3` precisely because `ai_helper.py`'s relevance check is, and `simple-reaction-choice`
(5), `simple-single-value-extract` (8) and `router-classify-short` (5) are the same shape. A model
that cannot answer inside those budgets is **disqualified at that call site**, not merely truncated —
scoring it at 6 or 12 tokens would credit it with something LEM could never run there.

Those cases carry `"budget_mirrors_production": true` in the fixture and are **never** escalated. The
report names any that ended empty under 🔒 *production budget*, so the exemption is visible rather
than a silent zero. The flag is for call-site-sized budgets only — a test fails the build if it is
ever set on a case whose `max_tokens` is above 10, since that would quietly re-disable the
reasoning-headroom retry the #842 run needed.

### Measurement variance — one run of one case is not a verdict (#910)

`minimax-m3` returned an EMPTY completion on two `bm-20260802-20ae40` cases after exhausting the
truncation retry; re-run at the same effective budget it answered and passed both. A reasoning
model's single run is therefore not a stable measurement, and an empty-then-fine case must never read
as a quality verdict.

So a completion that is still empty after the budget escalations is **re-measured at that same
budget** (`BENCHMARK_EMPTY_REPEATS`, default 1, `0` disables). The repeat re-asks; it never buys
headroom, so it applies to production-budget cases too. Two things follow:

- every case that needed a repeat is named in the report under ⚖️ *measurement variance*, with the
  reminder that this model's rates are single-measurement;
- a completion that is *still* empty is recorded as **no output**, not as a zero-length answer.
  Grading `""` rendered as `min_chars: 0 chars < 700` — a quality verdict on a model that was never
  measured. No output fails the gate's "provider answered every case" expectation instead, which is
  what it actually was.

### A harness outage is not a run of zeros (#923)

The rule above at run scope. A provider error is a legitimate case result — a 500, a rate limit, a
retired tag — and `ProviderClient.complete` turns any exception into one. That is also what a broken
venv, a revoked `OLLAMA_CLOUD_API_KEY` or a DNS failure looks like, so a harness that could not call
anything still *completed*: a full report, `reject` on every verdict, champions at `0% (0/10)`, and
one leaderboard row per model that is indistinguishable from a real bad run forever after. It
happened for real while working #921 — a worktree whose venv had no `openai` produced exactly that,
off a run that never made an HTTP request, and it was caught by eye rather than by the harness. The
harness runs unattended (`scripts/weekly_model_check.sh` opens the report as a PR), so an expired key
would ship a PR asserting that every model LEM runs scores zero.

The tell is **every case of every model, the tier's own champion included**. A roster of candidates
can genuinely be bad; the incumbent that serves production scoring zero on every single case means
nothing reached a provider at all. The champion is the tell, not a precondition — a run that
measured no champion at all (a tier with no configured incumbent) still measured nothing, so it is
refused on the same condition, with the champion clause simply absent from the message. On that
condition the run is **refused**, not published:

- no per-run report is written and **no leaderboard rows** are appended — nothing measured, nothing
  to record;
- the script exits **1**, which the weekly cron already reads as a real failure (only `0` and `2`
  open the PR), so the outage alerts instead of shipping;
- the underlying error is named **once** (`refusing to render: harness outage: …`), taken from the
  commonest case error, with a count of any others rather than a wall of repeats.

The honest half is untouched: one model failing every case beside a champion that answered, or some
cases timing out everywhere, is a real measurement and renders exactly as before. Those runs now
carry the split in the report **header** — `**Unmeasured cases:** 3 of 60`, naming any model that
answered nothing at all — rather than only under the per-case ❌ details, because how much of a run
is a measurement at all decides whether the rest of it means anything.

## The gate

A candidate is emitted as a **swap recommendation** only when it clears the tier's absolute
thresholds — the **contract** floor and the judge floor, never the advisory first-draft one — *and*
meets-or-beats the champion on every graded expectation (contract, first draft, judge). Weaker
outcomes are reported but go no further:

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

Two caveats the run itself surfaced are what #910 then fixed — both decisions above stand under the
new calibration, but the numbers in that run's report were measured under the old one:

- **The absolute deterministic floor (90%) was met by nobody, champions included.** Recalibrated:
  the absolute floor is now the **contract** rate and the first-draft rate is advisory — see *What
  the deterministic rates mean* above. Replaying this roster's own measured failures through the new
  calibration, `deepseek-v4-flash` on `lem-medium` clears every expectation and lands as a
  `recommend` (it tied the champion deterministically and beat it 83% to 50% on judge, at the same
  Medium usage level, which is exactly what the table above called "the model to re-measure first").
  The gate is demonstrably openable; the replay is a committed test, not a claim.
- **A reasoning model's single-run score is not stable.** Fixed: an empty completion is now
  re-measured at the same budget and reported under ⚖️ *measurement variance*, and one that stays
  empty is recorded as no output rather than as a zero-length answer.

**These verdicts are not re-derived, they are replayed.** A live re-run spends metered Ollama Cloud
quota, so the demonstration above re-scores the failures this report already recorded rather than
inventing new measurements. The next real run is the one whose scorecard carries both columns.

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

### What the 2026-08-02 tag scan settled (#921) — both declined

The catalog scan found two new Ollama Cloud tags. Both are **declined**; neither reaches
`.litellm/config.yaml`. Recorded here because the issue's own rule is that a decline has to be
readable by the next scan's reader — otherwise the same tag gets re-evaluated from its spec sheet
every month.

| Tag | What was measured | Decision |
|---|---|---|
| `deepseek-v4-flash:0731` | Medium (2), same level as the build already deployed. Run against all three content tiers beside both the tier champion **and** the incumbent `deepseek-v4-flash` build (`bm-20260802-b84f19`): contract **80% vs 90%** on `lem-complex`, **80% vs 90%** on `lem-medium`, **40% vs 50%** on `lem-simple` | **Decline.** No `recommend` on any tier: it did not beat the build LEM already ships on the contract rate anywhere, and there is no quota argument to offset that — both builds are Medium. Note the margins are one case wide, i.e. inside this suite's run-to-run spread (third bullet below), so this reads as *"did not carry the burden"*, not *"is the worse build"*. Either way it is a decline — adoption requires beating the incumbent, and a tie inside noise is not that. |
| `kimi-k3` | Never benchmarked: the Ollama Cloud API answers **HTTP 402** — *"this model uses extra usage only (not included plan usage) and your extra usage balance is empty"* | **Decline.** Not a quality question. It is outside plan usage entirely, so its page publishes a per-token price ($3.00 / $15.00 per 1M, $0.30 cached) instead of a usage-level pip — the harness reads that as `unknown`, which the standing spend policy already holds. |

Four things this run is worth reading for beyond the two verdicts:

- **`:0731` is a different build, not a re-tag.** The catalog carries it at 167GB against the
  unversioned tag's 140GB, and ollama.com dates them 2026-07-31 and three months apart, so the
  `bm-20260802-20ae40` measurement of `deepseek-v4-flash` was not a measurement of this one. That is
  why the incumbent build was re-run here rather than quoted: a build-vs-build comparison is the
  actual decision, and the two builds have to share a calibration and a run to be comparable at all.
- **`lem-simple` is the wrong shape for this model, not merely a weak fit.** `deepseek-v4-flash` is a
  reasoning model and bills its chain-of-thought against `max_tokens`, so on the three
  `budget_mirrors_production` cases (`max_tokens` 3 / 5 / 8, mirroring `ai_helper.py`'s own call
  sites) it returns nothing at all. Both builds are also **+1 usage level** against `gpt-oss:20b`
  there. Neither build belongs on that tier at any quality.
- **The suites' run-to-run spread is wider than this decision's margins.** Two runs of the same
  fixtures ninety minutes apart moved `deepseek-v4-flash:0731` on `lem-medium` from 100% to 80%
  contract, `qwen3.5:397b` on `lem-complex` from 90% to 70%, and `gpt-oss:20b` on `lem-simple` from
  40% to 50% (`bm-20260802-5fff18` and `bm-20260802-b84f19`, both committed beside this file). Ten
  cases per tier means one case is ten points, so a one- or two-case gap between two models is
  noise. This is what the ⚖️ *measurement variance* note says per case, stated at the level of a
  whole scorecard: **a one-run margin under ~20 points is not a reason to swap anything.** It is
  also not a reason to swap the other way: every margin in the `:0731` decision above is exactly
  one case, which is why that decline rests on "no `recommend`" rather than on the gap's size.
- **Declining `:0731` is not the same as being safe from it.** `.litellm/config.yaml` ran the
  *unversioned* `deepseek-v4-flash` on two tiers, so those tiers followed whatever the catalog's
  moving tag pointed at — and `scripts/model_health_check.py` diffed tag NAMES only, so a re-point of
  that name onto the 0731 build filed no evaluation issue and swapped a live tier's model
  unbenchmarked. The id was left unversioned anyway (it is the build measured at 90%, and a pin has
  upkeep of its own on every path that keys the exact id string); **#925** closed the detection gap
  instead — the weekly scan now also compares each CONFIGURED tag's `size`/`modified_at` against the
  committed snapshot and files a re-point evaluation issue naming the tiers that moved. The dedup
  marker carries the new build's fingerprint, so a *second* re-point is not deduped away as
  already-filed. It stays a trigger, never an auto-pin. **That exposure fired a week later** — the
  bare tag now carries the `:0731` build. See the next section.

Both runs are `in-runner-judge` mode with **no judge evidence** — the runner had no LiteLLM proxy to
reach, so the judge answered nothing and every judge expectation renders as unscored. Read the
scorecards' `Timeouts` column with that in mind: it counts only the cases that were *eligible* for
the judge (a case that already failed deterministically is never judged), so it tracks the
first-draft pass count, not the case count. None of that changes a verdict here — all nine gate
verdicts across the two runs already fail a *deterministic* graded expectation, and the judge can
only ever add a reason to reject. A run that intends to *promote* something still needs one.

### What the 2026-08-09 tag scan settled (#1201) — one tag, adopted, unbenchmarked on purpose

| Tag | What it is | Decision |
|---|---|---|
| `deepseek-v4-flash:preview` | Medium usage level, 1M context, text-only (ollama.com/library/deepseek-v4-flash). 140GB, published 2026-04-24 — the same catalog entry (`size` and `modified_at`) the committed snapshot carried under the *bare* `deepseek-v4-flash` until this scan, i.e. the build measured at **90% contract on `lem-medium` and on `lem-complex`** in `bm-20260802-b84f19` | **Adopt** — and it already is, on both tiers, since #1200. No new benchmark: this is not a new model, it is a versioned name for the one LEM has served since #717. Same usage level, so the quota burn is flat. |

The scan filed this as a NEW tag because a new tag is what `/api/tags` showed. What the tag actually
records is a **re-point of the bare name**, and the two halves are worth separating because #1200
landed the right config change with the wrong reason attached:

- `/api/tags` on 2026-08-09 lists 18 tags. The bare `deepseek-v4-flash` is not among them;
  `:0731` (167GB, 2026-07-31) and `:preview` (140GB, 2026-04-24) are.
- ollama.com's tags page for the model still lists three rows — the `-cloud` spellings, with no
  bare/`:latest` row — and publishes a per-tag digest. `deepseek-v4-flash:cloud` and
  `deepseek-v4-flash:0731-cloud` share one digest (`031ce2a95446`, "1 week ago");
  `deepseek-v4-flash:preview-cloud` carries a different one (`dd3d9b94bae4`, "3 months ago").
  `:cloud` is the alias of the bare name (probed on #844), so that shared digest is what says where
  the bare name now points.
- So the bare name was not deleted and it was not republished as `:preview`. **It was re-pointed
  onto the `:0731` build** — the one #921 declined — and the 140GB build it used to serve was
  published under `:preview`. #1200's config comment reads the other way round; corrected there in
  the same PR as this section.

That makes the repoint in #1200 load-bearing rather than cosmetic. Had the bare id been left in
place — or "corrected" back to it, which is exactly what a reader following the *use the bare
catalog id* rule would try — `lem-medium` and `lem-complex` would both be pointing at a name this
file no longer controls. Which of two failures that buys was NOT probed, and both are bad: if the
endpoint still resolves the untagged name the way it resolves `:cloud` (#844), the two tiers serve
the `:0731` build this file rejected, silently, with no run and no issue; if it does not, they 404 —
and under latency routing a 404 is the fastest answer in the group, so the dead deployment takes
the traffic (the ministral-3:8b failure). Don't write it back to find out. **A versioned id is the
only thing that pins a build**; the bare-id rule in `.litellm/config.yaml` is about matching the
catalog verbatim, and `:preview` matches it verbatim.

The gap this left open is now closed (**#1237**). The guard that exists for precisely this — the
#925 re-point scan — skipped it: `plan_repoints` compares a CONFIGURED tag's `size`/`modified_at`
against the snapshot and ignores any tag missing from either side, so a re-point that *also* drops
the name from `/api/tags` was invisible to it. What caught this one was the catalog diff plus the
roster test failing CI, which says "your configured id is gone" and not "your configured id now
serves a build you rejected". Since #1237 the scan treats that as its own re-point-class finding:

- `plan_vanished` reports a CONFIGURED tag the snapshot had and the live catalog no longer lists,
  and files ONE `agent:ready` issue naming the tiers it served **and the sibling tags the catalog
  still offers in that family** — that sibling list is where the build went, and it is what stops a
  reader from restoring the bare id per the *use the bare catalog id* rule and landing on `:0731`.
  The issue says so in words.
- **Sharing a base name is not enough to be called the likeliest home.** Two siblings are listed as
  evidence but never recommended: one whose size tag parses as a different parameter count
  (`gpt-oss:20b` vs `gpt-oss:120b` — both live tiers here, and not the same build under a new name),
  and one carrying a published retirement date, which `plan_family_upgrades` already refuses to
  recommend for the same reason. A non-numeric tag (`:preview`, `:0731`) proves nothing about the
  size and stays a candidate — that is the deepseek case itself.
- The dedup marker carries the LAST BUILD recorded for the tag (`<tag> gone @ <fingerprint>`), so a
  tag that leaves, returns under a fresh build and leaves again is not swallowed by the first
  issue — the same rule as the #925 marker.
- The snapshot now records the per-tag **`digest`** when `/api/tags` publishes one, and a digest
  wins the fingerprint: `size`+`modified_at` are a fingerprint by coincidence, the digest is build
  identity, and it is what settled the question above. The digest on `/api/tags` and the one on the
  library tags page are **different values for the same tag** — only the `/api/tags` one is stored,
  and the two must never be compared. A tag with no published digest keeps the old pair, so a
  snapshot written before digests were captured is a missing baseline rather than a build change.
- An **empty catalog reports nothing**. `fetch_catalog` returns `None` only when the request raised,
  so a 200 carrying no models parses to `{}` — and this is the one scan finding with an autonomous
  consumer (it files `agent:ready`), so a degraded fetch must not put an agent to work re-pointing
  every tier off a phantom. A vanish is a TRANSITION; an empty catalog is no evidence of one.
- Still a trigger, never an auto-pin — same posture as #925.

### What the 2026-08-16 tag scan settled (#1583) — both declined, unbenchmarked on purpose

| Tag | What it is | Decision |
|---|---|---|
| `deepseek-v4-pro:0813` | **Extra high (4)** usage level, 1M context, text-only, 1.6T total / 49B active MoE with three thinking modes (ollama.com/library/deepseek-v4-pro). 893GB, published 2026-08-13 — a genuinely new build: it is the one the bare `deepseek-v4-pro` name now points at | **Decline.** +2 usage levels against every serving-tier incumbent (+1 against the `lem-agent-*` lane's High models), and the standing spend policy buys an increase only on `lem-complex` and only on a **strict** judge-rate win over the champion. See below for why no run can produce one today. |
| `deepseek-v4-pro:preview` | Same model page, so the same **Extra high (4)** level. 1600GB, published 2026-04-24 — the same `size`/`modified_at` the committed snapshot carried under the *bare* `deepseek-v4-pro`, i.e. a versioned name for a build LEM has never deployed | **Decline**, on the same policy, plus there is nothing to trade: it is the *older* of the two builds at the identical usage level. |

Three things worth reading beyond the two verdicts:

- **The decline is a spend call, not a quality claim, and it is decidable without a run.** Every
  Ollama deployment in the **serving** tiers is Low (`gemma4:31b`) or Medium (`gpt-oss:20b`,
  `gpt-oss:120b`, `deepseek-v4-flash:preview`, `qwen3.5:397b`), so Extra high is **+2** levels on
  every one of them. (The `lem-agent-*` lane is the exception to the "+2" arithmetic, not to the
  verdict: `glm-5.3` and `minimax-m3` are **High (3)**, so adopting there would be +1 — and that
  lane pins one model per tier with no benchmark gate at all, which is a reason for MORE caution,
  not less.) The standing spend policy above allows an increase on `lem-complex` alone, on a
  strict judge-rate win — and the champion `qwen3.5:397b`'s last measured judge rate on that tier is
  **100%** (`bm-20260802-20ae40`). A strict win over 100% does not exist, so a benchmark run could
  at best tie, at Extra-high metering for every one of its calls. If the champion is ever
  re-measured *below* 100% on `lem-complex`, that is the event that makes this model worth the
  quota — re-open then, not on the next spec sheet.
- **This is not the `kimi-k3` case.** kimi-k3 was declined because it is outside plan usage
  entirely (HTTP 402, per-token pricing, `unknown` level). `deepseek-v4-pro` publishes a usage pip
  like every model LEM serves, so it *is* inside plan usage — it just sits in the most expensive
  class of it. Different reason, same outcome.
- **The bare name moved again — the flash story, in the `-pro` family.** `/api/tags` on 2026-08-16
  no longer lists a bare `deepseek-v4-pro`; `:0813` (893GB, 2026-08-13) and `:preview` (1600GB,
  2026-04-24) are what it lists. ollama.com's tags page gives `deepseek-v4-pro:cloud` and
  `deepseek-v4-pro:0813-cloud` the **same digest** (`6ed7420bc3a4`) while
  `:preview-cloud` carries its own (`22bfd5026abd`) — so, exactly as on `deepseek-v4-flash`
  (#1201), the bare name was **re-pointed onto the new build** and the April build was republished
  under `:preview`. It cost nothing this time only because no tier ever served this model: nothing
  in `.litellm/config.yaml` had to move, and `plan_vanished` filed no tier-naming issue for the same
  reason. Read it as confirmation that the family does this routinely — a bare `deepseek-*` id in
  this config is a build LEM does not control. The snapshot refresh for this scan is the cron's own
  PR (#1582, +2 new / -1 gone), not this record.

### What the 2026-08-30 tag scan settled (#1757) — one declined unbenchmarked, one measured and declined

| Tag | What it is | Decision |
|---|---|---|
| `glm-5.3` | **High (3)** usage level, 1M context, tool calling, no vision (ollama.com/library/glm-5.3) — the same family/level as `glm-5.2` | **Decline, unbenchmarked**, on the same #842 standing spend policy as `deepseek-v4-pro` above: it is +1 usage level against every **serving**-tier incumbent (all Low/Medium), the policy only buys an increase on `lem-complex`, and only on a strict judge-rate win — the champion `qwen3.5:397b` is already measured at 100% judge there, so no run can produce one. The `lem-agent-*` lane's own `glm-5.2` → `glm-5.3` question is tracked separately on #1756 (flat usage level there, not this issue's scope). |
| `glm-5.3-flash` | **Medium (2)** usage level, 1M context, vision-capable, tool use (ollama.com/library/glm-5.3-flash) — flat quota against `lem-medium`'s incumbents, so the spend policy above does not apply and the only way to answer was to run it | **Decline, measured** (`bm-20260830-3048e1`, report beside this file). Beat champion `gpt-oss:120b` on contract (100% vs 90%) and tied on first draft (60% vs 60%), but missed the judge floor (33% vs 80%) and lost to the champion on judge (33% vs 67%) — comment-contract failures reading as generic validation rather than engaging a specific claim. No `recommend`; `.litellm/config.yaml` unchanged. |

Both are within #1757's scope (`lem-simple`/`lem-medium`/`lem-complex` only) — neither tag touches `lem-agent-*`.

### What the 2026-08-30 tag scan settled (#1758) — `:preview` vanished, no fit left

| Tag | What happened | Decision |
|---|---|---|
| `deepseek-v4-flash:preview` | Left `ollama.com/api/tags` entirely on 2026-08-30 — no republish this time, just gone. It was the 140GB build measured at 90% contract on `lem-medium` and `lem-complex` (`bm-20260802-b84f19`) and the id both tiers served since #1200 | **Removed from both tiers.** A vanished tag is next week's 410, found early — leaving it configured only means the same outage, later. |
| `deepseek-v4-flash:0731` (the only sibling left in the family) | 167GB, published 2026-07-31, digest `4de9bf66c0eb` on `ollama.com/api/tags` — a DIFFERENT digest from `:preview`'s `ac252c581d64`, so this is not the same build under a new name | **Not a fit — already declined.** #921 benchmarked it directly beside `:preview` on all three tiers and rejected it: 80% vs 90% contract on `lem-complex` and `lem-medium`, 40% vs 50% on `lem-simple`. Restoring the bare `deepseek-v4-flash` id would just follow the vendor's moving tag onto this same build (#1201's finding, still true). |

No candidate replaces the family on either tier in this change — `lem-medium` keeps `gpt-oss:120b`
+ `gemma4:31b`, `lem-complex` drops to its lone Ollama deployment (`qwen3.5:397b`) plus the paid
fallbacks. A fresh `lem-complex` candidate, benchmarked rather than assumed, is the #1758 follow-up
issue; nothing in `gemma4:31b` or the HIGH usage-level `minimax-m3` / `glm-5.2` closes that gap for
free — all three are already declined on this tier (`bm-20260802-20ae40`, `#842`).

This is the same finding shape as #1200/#1201, minus the republish: `plan_repoints` (#925) is blind
to a tag missing from both sides, so a re-point that also drops the name from the catalog is only
ever caught by `plan_vanished` (#1237) and the roster test asserting every configured id is in the
committed catalog — which is exactly what fired here.

### What #1762 settled — `lem-complex`'s second Ollama deployment, declined, measured

`deepseek-v4-flash:preview` left `ollama.com/api/tags` on 2026-08-30 (#1758) and its only sibling,
`:0731`, was already declined on this tier (#921). Per the #842 standing spend policy, every
High/Extra-high usage-level candidate already on the roster (`minimax-m3`, `glm-5.2`,
`deepseek-v4-pro`, `glm-5.3`) is a decline-unbenchmarked here: a usage-level increase on
`lem-complex` needs a strict judge-rate win over the champion, and no run can beat a champion
already measured at 100% judge. That left one genuinely new, not-yet-evaluated candidate at a
usage level the policy doesn't block outright: `mistral-large-3:675b` (**Medium**, 682GB, 1M
context, on the catalog since 2025-12-02 — never benchmarked for any LEM tier before).

| Tag | What it is | Decision |
|---|---|---|
| `mistral-large-3:675b` | **Medium (2)** usage level, flat quota against the tier's departed `deepseek-v4-flash:preview` deployment (ollama.com/library/mistral-large-3) | **Decline, measured** (`bm-20260830-1e6b4e`, report beside this file). Tied champion `qwen3.5:397b` on contract (90% vs 90%) but missed the judge floor (40% vs 80%) and lost to the champion on both first draft (50% vs 90%) and judge (40% vs 44%) — a markdown-fenced JSON case, an invented statistic, a short prompt-shaped answer instead of long-form copy, and repeated contrastive/"ta-da" slop-lint hits repairable only on the first-draft measure. No `recommend`; `.litellm/config.yaml` unchanged. |

Two things worth reading beyond the verdict:

- **The champion's own judge rate moved.** `qwen3.5:397b` measured 100% judge on `lem-complex` in
  `bm-20260802-20ae40` (PostHog-evals mode) and the config's own decline notes for
  `deepseek-v4-pro`/`glm-5.3` lean on that figure. This run (`in-runner-judge` mode, no PostHog
  benchmark key configured) measured it at 44% instead — one case with no output at all
  (`complex-mobile-hook`, empty after budget escalation and a re-measurement) plus five judge
  misses. Per the *measurement variance* posture above, a single run's judge score is not a stable
  read, especially across scoring modes, so this is **not** treated as reopening the High/Extra-high
  declines above — but it means "no run can beat 100%" is no longer literally true of the last
  measurement, and the next `lem-complex` evaluation should re-measure the champion under the same
  mode as whatever it compares against rather than citing the 2026-08-02 figure.
- **The roster gap itself is unresolved, on purpose.** `lem-complex` still runs
  `deepseek-v4-flash:preview` today (#1758's removal is #1763, not yet merged) — once that lands the
  tier is down to one Ollama deployment (`qwen3.5:397b`) plus the paid fallbacks, and this decline
  does not close that. Declining is the correct outcome for `mistral-large-3:675b` specifically, not
  a statement that the tier doesn't need a win; the next monthly catalog scan is where a fresh
  candidate gets evaluated.

### What the 2026-08-30 family-version scan settled (#1756) — one tag, adopted, unbenchmarked on purpose

| Family | What it is | Decision |
|---|---|---|
| `glm-5.2` -> `glm-5.3` (`lem-agent-tier1`) | Same **High (3)** usage level as `glm-5.2` — flat quota burn. ollama.com/library/glm-5.3 states the swap is post-training on the SAME base model, with the gains aimed at exactly this tier's job: "Terminal-Bench 3.0 improves from 4.6 to 28.3", "SWE-Marathon more than doubles (19.4 -> 42.5)", and the page names coding-agent use explicitly, including Claude Code | **Adopt.** No harness measures the `lem-agent-*` lane's own workload (`scripts/benchmark_models.py` targets `lem-simple`/`lem-medium`/`lem-complex` only), so this reads the vendor's own published benchmark deltas rather than running one. Not the #842 spend-policy case — that gates a usage-LEVEL INCREASE, and this one is flat. |

This is a different question from `glm-5.3`'s candidacy for `lem-complex` (#1757/#1765, **declined**
unbenchmarked): that decline turns on `glm-5.3` being **+1 usage level** against every serving-tier
incumbent there (all Low/Medium) with the #842 spend policy requiring a strict judge-rate win to
justify it. `lem-agent-tier1` starts from `glm-5.2`, already **High (3)** — so `glm-5.3` there is
flat quota, and the swap is a same-cost quality question, not a spend one. `.litellm/config.yaml`
and `.litellm/model_prices_snapshot.json` / `.litellm/ollama_catalog_snapshot.json` carry the usual
roster bookkeeping for the new tag.

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
| `OLLAMA_CLOUD_URL` | `https://ollama.com/v1`, the direct API that serves exactly the ids `/api/tags` lists (`glm-5.2`, not `glm-5.2:cloud`; `deepseek-v4-flash:preview`, whose bare form the catalog no longer carries at all). |
| `OLLAMA_CLOUD_API_KEY` | The metered credential. Never in the repo, never in a report. |
| `BENCHMARK_USAGE_LEVELS` | The incumbents' levels, which ollama.com does not publish. Unsupplied is `unknown`, and `unknown` holds a swap — so a run without it can recommend but can never conclude. |

`POSTHOG_BENCHMARK_API_KEY` + `POSTHOG_API_KEY` are optional: both present uses PostHog Evaluations
as the judge, otherwise the run falls back to the in-runner judge and says so in the report *and* on
stderr. The personal key is resolved by PURPOSE (issue #1453,
`src/cqc_lem/utilities/posthog_keys.py`): `POSTHOG_BENCHMARK_API_KEY` first, then the shared
`POSTHOG_PERSONAL_API_KEY` — so an environment that has not been split yet is unaffected.
`python scripts/posthog_key_check.py --purpose benchmark` answers "does this personal key still
reach both halves PostHog scoring needs — the evaluation API and HogQL?" without running a
benchmark. It does not look at `POSTHOG_API_KEY`; the run itself says so on stderr when that one is
the missing half.

**The evaluation-API half of that check is a documented ceiling, not a fixable failure** (confirmed
2026-08-22, `docs/kpi-dashboards.md` § Purpose-scoped personal keys): PostHog's current LLM Analytics
API does not expose the `/llm_analytics/evaluations/` collection endpoint `PostHogEvals` is built on
— `create_evaluation` / `run_evaluation` per `$ai_generation` has no equivalent in PostHog's public
surface (`evaluation_reports`, `evaluation_config`, `evaluation_summary`). It is not a key-scope
problem — a fully-scoped key 404s the same as the shared one — so it will read as PostHog-evals
unavailable indefinitely, and the run falls open to the in-runner judge exactly as designed. Do not
spend more scope trying to make this one pass.

The report is the deliverable — `--results-out` for the JSON a later `--render` replays, and
`--recommendations-out` for the swap list a follow-up config PR reads. Anything the standing spend
policy marks `hold` is named in the report and belongs to the owner, not to the config PR.

## Leaderboard

Rows measured before #910 carry `n/a` under **Contract**: those runs computed one deterministic rate,
and printing it in both columns would invent a measurement nobody took. Their **First draft** number
is the rate their report published.

<!-- LEADERBOARD:BEGIN -->
| Date | Run | Tier | Model | Role | Contract | First draft | Judge | p50 | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-30 | `bm-20260830-1e6b4e` | lem-complex | `qwen3.5:397b` | champion | 90% | 90% | 44% | 29310 ms | baseline |
| 2026-08-30 | `bm-20260830-1e6b4e` | lem-complex | `mistral-large-3:675b` | candidate | 90% | 50% | 40% | 5461 ms | reject |
| 2026-08-30 | `bm-20260830-3048e1` | lem-medium | `gpt-oss:120b` | champion | 90% | 60% | 67% | 1423 ms | baseline |
| 2026-08-30 | `bm-20260830-3048e1` | lem-medium | `glm-5.3-flash` | candidate | 100% | 60% | 33% | 2351 ms | reject |
| 2026-08-02 | `bm-20260802-b84f19` | lem-complex | `qwen3.5:397b` | champion | 70% | 50% | n/a | 30346 ms | baseline |
| 2026-08-02 | `bm-20260802-b84f19` | lem-complex | `deepseek-v4-flash:0731` | candidate | 80% | 80% | n/a | 5634 ms | reject |
| 2026-08-02 | `bm-20260802-b84f19` | lem-complex | `deepseek-v4-flash` | candidate | 90% | 60% | n/a | 3016 ms | reject |
| 2026-08-02 | `bm-20260802-b84f19` | lem-medium | `gpt-oss:120b` | champion | 90% | 60% | n/a | 2429 ms | baseline |
| 2026-08-02 | `bm-20260802-b84f19` | lem-medium | `deepseek-v4-flash:0731` | candidate | 80% | 40% | n/a | 1820 ms | reject |
| 2026-08-02 | `bm-20260802-b84f19` | lem-medium | `deepseek-v4-flash` | candidate | 90% | 40% | n/a | 1438 ms | reject |
| 2026-08-02 | `bm-20260802-b84f19` | lem-simple | `gpt-oss:20b` | champion | 50% | 50% | n/a | 1432 ms | baseline |
| 2026-08-02 | `bm-20260802-b84f19` | lem-simple | `deepseek-v4-flash:0731` | candidate | 40% | 40% | n/a | 996 ms | reject |
| 2026-08-02 | `bm-20260802-b84f19` | lem-simple | `deepseek-v4-flash` | candidate | 50% | 50% | n/a | 1036 ms | reject |
| 2026-08-02 | `bm-20260802-5fff18` | lem-complex | `qwen3.5:397b` | champion | 90% | 60% | n/a | 28535 ms | baseline |
| 2026-08-02 | `bm-20260802-5fff18` | lem-complex | `deepseek-v4-flash:0731` | candidate | 60% | 30% | n/a | 5212 ms | reject |
| 2026-08-02 | `bm-20260802-5fff18` | lem-medium | `gpt-oss:120b` | champion | 90% | 70% | n/a | 1952 ms | baseline |
| 2026-08-02 | `bm-20260802-5fff18` | lem-medium | `deepseek-v4-flash:0731` | candidate | 100% | 60% | n/a | 1636 ms | reject |
| 2026-08-02 | `bm-20260802-5fff18` | lem-simple | `gpt-oss:20b` | champion | 40% | 40% | n/a | 1403 ms | baseline |
| 2026-08-02 | `bm-20260802-5fff18` | lem-simple | `deepseek-v4-flash:0731` | candidate | 40% | 40% | n/a | 1214 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `qwen3.5:397b` | champion | n/a | 50% | 100% | 26955 ms | baseline |
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `deepseek-v4-flash` | candidate | n/a | 70% | 57% | 3275 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `gemma4:31b` | candidate | n/a | 80% | 57% | 2428 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `minimax-m3` | candidate | n/a | 40% | 75% | 30564 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-complex | `glm-5.2` | candidate | n/a | 70% | 86% | 14477 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `gpt-oss:120b` | champion | n/a | 60% | 50% | 1635 ms | baseline |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `deepseek-v4-flash` | candidate | n/a | 60% | 83% | 1310 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `gemma4:31b` | candidate | n/a | 40% | 100% | 807 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `minimax-m3` | candidate | n/a | 50% | 80% | 5405 ms | reject |
| 2026-08-02 | `bm-20260802-20ae40` | lem-medium | `glm-5.2` | candidate | n/a | 40% | 75% | 6315 ms | reject |
<!-- LEADERBOARD:END -->
