"""ONE place a PostHog personal API key is resolved, by PURPOSE (issue #1453).

`POSTHOG_PERSONAL_API_KEY` was a single account-scoped key doing four unrelated jobs — a CI
release annotation (write), the app's runtime reads (feature-flag local evaluation, the provisioned
HogQL endpoints behind the SPA stats panel), the daily error->issue cron's HogQL query, and the
weekly model benchmark's LLM-evaluation scoring. One key means one blast radius, and four of those
five consumers fail SILENTLY without it: flags fall back to their env vars, the endpoints panel goes
empty, the cron files nothing, the benchmark drops to its in-runner judge. None of that raises — the
CI annotation is the only one that says anything, and its step is `continue-on-error`.

So each purpose reads its OWN env var first and falls back to `POSTHOG_PERSONAL_API_KEY`:

| Purpose | Env var | Consumers | Scope it needs |
|---|---|---|---|
| `annotation` | `POSTHOG_ANNOTATION_API_KEY` | `scripts/posthog_annotate.py` (CI deploy job) | `annotation:write` |
| `runtime` | `POSTHOG_RUNTIME_API_KEY` | `flags.py`, `posthog_endpoints.py` | `feature_flag:read`, `query:read` |
| `query` | `POSTHOG_QUERY_API_KEY` | `scripts/posthog_error_issues.py` (host cron) | `query:read` |
| `benchmark` | `POSTHOG_BENCHMARK_API_KEY` | `benchmark_models.py` (cron) | `evaluation:read+write`, `query:read` |
| `operator` | `POSTHOG_OPERATOR_API_KEY` | 7 hand-run scripts (below) | broad — every write scope + `query:read` |

`observability.posthog_hogql_query` is a runtime read too, so it rides the `runtime` key — the
`query` one is the CRON's, and the two live in different environments.

The fallback is what made the rollout ADDITIVE: until a scoped key existed in an environment,
nothing changed there, so the keys were created and populated one consumer at a time and the old key
revoked last (`docs/kpi-dashboards.md`). An unset scoped var is the normal state, never an error.

**The shared key was revoked on 2026-08-31, so the fallback is now a dead branch everywhere LEM
runs** — kept because it costs nothing and a future environment may legitimately export one. Do not
read a `via POSTHOG_PERSONAL_API_KEY` line out of `scripts/posthog_key_check.py` as working: after
the revoke it means an unpopulated consumer holding a revoked credential, which answers 401.

`benchmark` is a purpose rather than an operator key for one reason: `scripts/benchmark_models.py`
is NOT hand-run — `scripts/weekly_model_check.sh` (host cron) sources its key out of `/opt/lem/.env`
— so the shared key is a *stored* credential for that lane, and revoking it would silently drop the
weekly run onto the in-runner judge. It reads PostHog's LLM-evaluation API, a scope none of the
other three cover, so widening `runtime` to carry it would widen the one key that lives in the app
containers (issue #1453, owner decision `1A`).

The provisioning scripts (`posthog_provision`, `posthog_flags`, `posthog_surveys`,
`posthog_experiments`, `posthog_dashboards`, `posthog_ops_destination`, `slop_retry_clear_rate`) are
run by hand, never stored in an environment the app or a cron owns — `operator` is their purpose:
`POSTHOG_OPERATOR_API_KEY`, exported into a shell for the run and stored nowhere. It is deliberately
the broadest scope of the five, one key standing in for six write scopes an operator already holds
account access to, rather than five separate keys nobody would provision. Revoking the shared key
still leaves these seven scripts a NAMED var to export instead of a silent break (issue #1453,
2026-08-22 follow-up).

`scripts/posthog_key_check.py` is the read-only preflight over this table — one live read per
surface, PASS/FAIL per purpose, naming the env var that actually supplied each key.

**stdlib-only, and it stays that way** — `scripts/posthog_annotate.py` runs on a bare CI runner with
only `requests` installed, and `scripts/posthog_error_issues.py` runs from a cron clone, so both put
`src/` on `sys.path` and import this module directly. Anything imported here has to survive that.
"""

from __future__ import annotations

import os

FALLBACK_ENV_VAR = "POSTHOG_PERSONAL_API_KEY"

ANNOTATION_ENV_VAR = "POSTHOG_ANNOTATION_API_KEY"
RUNTIME_ENV_VAR = "POSTHOG_RUNTIME_API_KEY"
QUERY_ENV_VAR = "POSTHOG_QUERY_API_KEY"
BENCHMARK_ENV_VAR = "POSTHOG_BENCHMARK_API_KEY"
OPERATOR_ENV_VAR = "POSTHOG_OPERATOR_API_KEY"

#: Purpose -> the scoped env var read BEFORE the shared fallback. Adding a purpose means adding a
#: row here, never a second resolution rule at a call site.
PURPOSE_ENV_VARS = {
    "annotation": ANNOTATION_ENV_VAR,
    "runtime": RUNTIME_ENV_VAR,
    "query": QUERY_ENV_VAR,
    "benchmark": BENCHMARK_ENV_VAR,
    "operator": OPERATOR_ENV_VAR,
}


def key_env_vars(purpose: str) -> tuple:
    """Return the env vars read for `purpose`, in precedence order.

    Args:
        purpose: One of the keys of `PURPOSE_ENV_VARS`.

    Returns:
        `(scoped_env_var, FALLBACK_ENV_VAR)`.

    Raises:
        ValueError: If `purpose` is not a known purpose — a typo here would otherwise resolve to
            the fallback key forever and look like it worked.
    """
    scoped = PURPOSE_ENV_VARS.get(purpose)
    if scoped is None:
        known = ", ".join(sorted(PURPOSE_ENV_VARS))
        raise ValueError(f"Unknown PostHog key purpose {purpose!r} — expected one of: {known}")
    return (scoped, FALLBACK_ENV_VAR)


def resolve_posthog_key_source(purpose: str) -> tuple:
    """Resolve one purpose's key AND name the env var that supplied it.

    The name is what makes a rollout auditable: mid-rollout the same key value can arrive from
    either var, and "which one answered" is the only way to tell a populated scoped key from a
    fallback that is still doing the work (`scripts/posthog_key_check.py` prints it).

    Args:
        purpose: One of the keys of `PURPOSE_ENV_VARS`.

    Returns:
        `(key, env_var_name)`, or `("", "")` when neither var is set.
    """
    for name in key_env_vars(purpose):
        value = (os.getenv(name) or "").strip()
        if value:
            return (value, name)
    return ("", "")


def resolve_posthog_key(purpose: str) -> str:
    """Resolve the personal API key for one purpose, scoped var first, shared key second.

    Args:
        purpose: One of the keys of `PURPOSE_ENV_VARS`.

    Returns:
        The first non-empty key value, or `""` when neither var is set — every caller already
        treats an empty key as "this surface is not configured", so this never raises for it.
    """
    return resolve_posthog_key_source(purpose)[0]


def missing_key_message(purpose: str) -> str:
    """One-line "no key" message naming BOTH vars, so a reader knows which one to set.

    Args:
        purpose: One of the keys of `PURPOSE_ENV_VARS`.

    Returns:
        A message of the form `"Neither <scoped> nor <fallback> is set"`.
    """
    scoped, fallback = key_env_vars(purpose)
    return f"Neither {scoped} nor {fallback} is set"


def annotation_api_key() -> str:
    """The CI release-annotation key (`annotation:write`)."""
    return resolve_posthog_key("annotation")


def runtime_api_key() -> str:
    """The app-runtime read key — feature-flag local evaluation and HogQL reads."""
    return resolve_posthog_key("runtime")


def query_api_key() -> str:
    """The host-cron HogQL query key."""
    return resolve_posthog_key("query")


def benchmark_api_key() -> str:
    """The weekly model-benchmark key — PostHog's LLM-evaluation API."""
    return resolve_posthog_key("benchmark")


def operator_api_key() -> str:
    """The hand-run provisioning-script key — broad, exported into a shell, stored nowhere."""
    return resolve_posthog_key("operator")
