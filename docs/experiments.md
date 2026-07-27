# Experiments (PostHog Experiments) — issue #652

LEM hand-rolled experimentation twice before this.

* **Cost/quality down-routing** (`utilities/cost_routing.py`, `utilities/routing_policy.py`,
  [cost-performance-margin-plan.md](cost-performance-margin-plan.md) §D.1.1) grew its own cohort
  hash, its own non-inferiority test and its own weekly email.
* **The media A/B harness** (#396: `app/generate_variants.py`, `post_variants`,
  `post_stats.select_variant_winners`) grew its own shipped-variant table and its own recency-weighted
  winner ranking.

Both work. Neither renders anywhere a human looks, and neither can say whether a difference is real
with anything a statistician would sign. PostHog Experiments already has the multivariate flag, the
exposure model, both stats engines (Bayesian + frequentist), CUPED and the readout — and it is billed
with feature flags, which at LEM's volume is free.

So `utilities/experiments.py` is an **adapter, not a third implementation**. The homegrown loops keep
running exactly as they did; PostHog gets the arms, the exposures and the outcome labels.

## The contract

**An unresolvable experiment is the CONTROL arm.** No PostHog key, definitions not loaded, flag not
defined, evaluation inconclusive, `EXPERIMENTS_ENABLED=false`, SDK raises — every one of those paths
returns the spec's control variant, which is by construction the behaviour that shipped before the
experiment existed.

There is deliberately **no env-var fallback** here, unlike [feature flags](feature-flags.md): a toggle
has a configured default worth honouring, an experiment arm does not.

`resolve_variant()` never returns None, but internally `_raw_variant()` keeps *"PostHog said control"*
and *"PostHog said nothing"* apart. That distinction is load-bearing:
`experiment_properties()` will not stamp `$feature/<key>=control` on a metric event from someone who
was never enrolled. A fabricated control arm is worse than a missing one — it makes the readout look
populated.

**Local evaluation only.** Assignment reuses the bootstrap in `utilities/flags.py` (personal API key,
the SDK's background definition poller, the failed-load cooldown) — one poller per process, zero
network requests per lookup, because these run inside feed loops. The consequence is a real constraint
on every experiment flag: its release condition must use **rollout percentage / distinct-ID only**. A
condition needing server-held person properties cannot be decided locally, and every Celery worker
would silently fall back to control.

**distinct_id is `str(user_id)`** (or the `"system"` sentinel) — the same convention
`observability.py`, `flags.py` and the SPA use. That is the whole mechanism by which PostHog
attributes an outcome to an arm: exposure and metric events must land on ONE person.

## Exposure

`utilities/flags.py` suppresses `$feature_flag_called` for boolean toggles (a per-feed-post event
nobody reads). Experiments are the opposite — without exposures there is no readout at all — so
`experiments.track_exposure()` emits it explicitly, **deduped per (experiment, person, arm) per
process**, via `observability.track_experiment_exposure()`.

The event name and the `$feature_flag` / `$feature_flag_response` properties are PostHog's, not ours.
Renaming them silently empties every experiment readout.

## The registry

`EXPERIMENTS` in `utilities/experiments.py`. `key` is the PostHog flag key AND the experiment key AND
the registry name — one identifier, so a rename cannot leave the code, the flag and the readout
disagreeing. `variants[0]` is always the control.

| Key | Arms | Assignment | Metrics | Owner |
|---|---|---|---|---|
| `cost-routing-arm` | `control`, `treatment` | flag (10% start) | `post_outcome`, `$ai_generation` | cost |
| `comment-contract-prompt` | `control`, `author-question` | flag (50/50) | `comment_outcome` | content |
| `post-media-variant` | `control` + one per shipped combo | shipped | `post_outcome` | content |

### 1. `cost-routing-arm` — cohorting moved to the flag

The hard constraint: `routing_policy.py` is **mounted into the LiteLLM container**, which has no LEM
package and no LEM dependencies. It must stay stdlib-only, so it can never import `experiments.py`.

So the flag DECISION is handed to it rather than looked up by it:

1. The weekly `auto_weekly_cost_routing` run resolves the arm for every active user
   (`cost_routing.resolve_cohort` → `experiments.assignments`), emitting one exposure each.
2. The answers are written into each routing bucket as `arms: {"7": "treatment", ...}` — inside the
   same policy document Redis already carries to the router.
3. `routing_policy.flag_arm()` reads that map; `assign_arm()` prefers it and falls back to the
   original SHA-256 hash for anyone it has no answer for. `resolve_tier()` reports which one decided
   in its `assignment` field, so a down-route from a live experiment is distinguishable in the proxy
   logs from one that came from the hash because PostHog was unreachable.

Two properties fall out of that ordering, both intentional:

* **The flag decides WHO, `cohort_pct` decides WHETHER.** A bucket the optimizer parked
  (`cohort_pct <= 0`, `rolled_back`, `hold`) routes nothing no matter what the flag says. Inside a
  running bucket the flag wins in both directions — including keeping a user in control at a cohort
  the ramp would have swept up, which is exactly the permanent holdout the §D.3 quality gate needs to
  stay measurable after adoption.
* **The arms map is applied AFTER the weekly evaluation.** The window being judged was routed under
  the PREVIOUS document's arms; grading it against a freshly-resolved cohort would score posts in the
  arm they are about to be in.

The map is capped at `ARMS_MAX_USERS` (500) and a truncation is logged — the users left out are
silently hash-assigned, which is not something to discover from an empty readout.

**Ramping.** PostHog owns the rollout once the experiment is running. `scripts/posthog_experiments.py`
never resets an existing flag's percentage (an `--apply` that reverted a 50% ramp to the spec's 10%
start would re-cohort a live experiment); move it deliberately:

```bash
python scripts/posthog_experiments.py --rollout cost-routing-arm=50 --apply
```

The weekly digest and the `routing_policy` PostHog event both report `cohort_assignment`,
`cohort_enrolled` and `cohort_treatment_share`, so a divergence between the loop's ramp and the flag's
is visible rather than inferred.

### 2. `comment-contract-prompt` — the pilot LLM prompt experiment

The variable is the **closing ask** of the #617 feed-comment quality contract
(`content_framework.comment_contract_directive`):

* `control` — four value-adds, any genuinely open question.
* `author-question` — adds one rule: end on a question **only this author could answer** (their
  numbers, their decision, what they saw). A question anyone in the thread could answer doesn't count.

Metric: **author-reply rate** (D4) from the #628 T+24h outcome sweep, carried on `comment_outcome`.

Three scoping decisions:

* The six deterministically-graded rules are **identical in both arms**. An arm that loosened one
  would change what "passes the gate" means and the arms would stop being comparable.
* Only **fresh feed comments** are enrolled. Replies to a specific comment have their own
  acknowledge-and-answer contract, and the seed / second-wave comments are never measured by the
  outcome sweep — enrolling them would add exposures the metric can never cover.
* The arm is resolved **at read time** on the outcome event, not stored per comment. PostHog's
  assignment is deterministic per person for the life of the flag, so this is stable — but see the
  caveat below.

### 3. `post-media-variant` — the #396 harness adapter

Its arms are DATA, not registry text: whichever media combo actually shipped. There is no flag to
read, so `assignment` is `shipped` and `experiments.track_shipped_variant()` reports the arm that
already happened. `record_shipped_variant()` is the exposure moment; `get_shipped_variant_keys()` is
read once per stats sweep so each `post_outcome` event carries its variant as
`$feature/post-media-variant`.

Combo keys (`black-forest-labs/flux-dev|gen4_turbo|1:1`) are not valid PostHog variant keys, so
`variant_slug()` slugifies them — with a stable digest suffix when a slug had to be truncated, so two
long combos can never collapse into one arm. `scripts/posthog_experiments.py` derives the flag's arm
list from `generate_variants.DEFAULT_COMBOS`, so it cannot drift from what the code can actually ship.

`post_stats.select_variant_winners` is untouched. PostHog gets the stats engine; the homegrown ranking
keeps its recency weighting, which is what the scheduler reads.

## Provisioning

An experiment in PostHog is two objects: a multivariate **flag** (arms + rollout) and an **experiment
record** (which flag, which metrics). The code resolves variants without either existing — it just
always answers control — so this script is what makes an experiment real, and it is code rather than
UI clicks for the same reason `scripts/posthog_provision.py` is: a flag someone edited by hand is not
reviewable.

```bash
python scripts/posthog_experiments.py --print-specs      # payloads, no network
python scripts/posthog_experiments.py                    # dry run (default); exit 2 = changes pending
python scripts/posthog_experiments.py --apply            # create missing flags + experiments
python scripts/posthog_experiments.py --rollout comment-contract-prompt=50 --apply
```

Needs `POSTHOG_PERSONAL_API_KEY` with feature-flag and experiment read+write, plus
`POSTHOG_PROJECT_ID`. It creates the flag first and reports the experiment as `blocked` in the same
dry run — PostHog needs the flag id, and promising both in one pass would be a lie about what a
follow-up `--apply` can do.

Flag release conditions are created **property-free at 100%**, with the split living in the variant
list, so local evaluation can always decide them (see the constraint above).

## Reading the results honestly

**The small-sample caveat.** At LEM's current volume — one active user, ~3 posts a week, a few dozen
feed comments — none of these experiments will reach significance on anything subtler than a collapse.
`MINIMUM_DETECTABLE_EFFECT` is set to a deliberately coarse 30% for that reason. The harness exists so
the multi-user future has a measurement plane already wired and already emitting; treat a readout
today as a smoke test that exposures and metrics are flowing, **not** as evidence a prompt is better.
The #630 content-quality trend line and the #628 weekly comment report remain the signals to act on.

**The attribution caveat.** Metric events are attributed to an arm by PERSON via the exposure, and the
`$feature/<key>` labels are resolved when the outcome is read (up to 24h after the comment shipped,
weekly for routing). PostHog's assignment is deterministic per distinct_id for the life of a flag, and
raising a rollout only ADDS treatment members, so this is stable in practice. It is not stable across
a **re-roll**: changing a flag's variant list, or lowering a rollout, re-cohorts people and invalidates
already-collected attribution. Don't re-roll a running experiment — end it in PostHog and start a new
one. (The cost-routing loop's own `generation` counter does exactly this for the hash path.)

**Never sum the two LLM streams** when reading cost per arm — `$ai_generation` is the proxy's own
`response_cost`, `llm_call` is LEM's estimate. See [llm-analytics.md](llm-analytics.md).

## Safety

Experiments are **not** a control plane. The 429 breaker, the automation pause, the suppression
tripwire and the per-day caps stay in Redis/env, exactly as [feature flags](feature-flags.md) says —
an arm that could pause an account would put account safety behind a percentage rollout.

The one thing an experiment CAN do is change model tier (cost-routing) or prompt wording
(comment-contract), and both are already fenced: down-routing can only ever route DOWN and is
auto-rolled-back by the §D.3 quality gate, and every comment arm faces the same #617 contract,
similarity gate and #625 slop lint before anything ships.
