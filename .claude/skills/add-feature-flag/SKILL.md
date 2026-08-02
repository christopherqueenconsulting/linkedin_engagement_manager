---
name: add-feature-flag
description: Use when adding a feature flag, experiment, or PostHog-gated behavior toggle — FlagSpec registry, env fallback, call-site read, PostHog provisioning, and what must NOT be a flag.
---

# Adding a feature flag

1. Add a `FlagSpec` to `FLAGS` in `src/cqc_lem/utilities/flags.py` (key, env var, default, owner, description) and export a constant.
2. Add the env var to `.env.example` next to the feature it governs, noted as a flag fallback. Flags **fail open to the env var** (no key, disabled, undefined, inconclusive, SDK raises → env value).
3. Read the flag at the **call site, never at import** — an import-time read can't flip without a deploy and is the #1 way to make a flag do nothing.
4. Create the PostHog flag with the same key using a **rollout-percentage** condition only (never person properties — local evaluation can't resolve them and silently falls back to env). Provision via `scripts/posthog_flags.py`, which **defaults to `--dry-run`** — it prints `[dry-run] create flag …` and exits 2 with changes pending, writing nothing. The write is `--apply`, and it needs `POSTHOG_PERSONAL_API_KEY` (scope `feature_flag:read+write`). Add the row to the registry table in `docs/feature-flags.md`.
5. Verify **against prod — owner/interactive only; a pipeline agent skips this step** (the RUNBOOK forbids running `docker`, and a headless lane has no permission prompt to stop it; provisioning in step 4 is the same call). `docker exec celery_worker python -c "from cqc_lem.utilities.flags import bootstrap_payload; print(bootstrap_payload(1))"` and `curl -s "$LEM_URL/api/flags" | jq .detail` — `local_evaluation: false` with a key set means the definition fetch failed.
6. SPA reads flags ONLY through `GET /api/flags` bootstrap (`useFeatureFlag(FLAGS.…)`) — never a second posthog-js reader.

**Not flags:** safety controls (429 breaker, suppression holds, automation pauses, per-day caps) stay in Redis/env. Experiments are the sibling procedure — `utilities/experiments.py` adapter, unresolvable = CONTROL arm: `docs/experiments.md`.

Authoritative: `docs/feature-flags.md`.
