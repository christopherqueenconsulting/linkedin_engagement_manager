"""ONE place a PostHog personal API key is resolved, by PURPOSE (issue #1453).

`POSTHOG_PERSONAL_API_KEY` was a single account-scoped key doing three unrelated jobs — a CI
release annotation (write), the app's runtime reads (feature-flag local evaluation, the provisioned
HogQL endpoints behind the SPA stats panel), and the daily error->issue cron's HogQL query. One key
means one blast radius, and three of those consumers fail SILENTLY without it: flags fall back to
their env vars, the endpoints panel goes empty, the cron files nothing. None of that raises.

So each purpose reads its OWN env var first and falls back to `POSTHOG_PERSONAL_API_KEY`:

| Purpose | Env var | Consumers | Scope it needs |
|---|---|---|---|
| `annotation` | `POSTHOG_ANNOTATION_API_KEY` | `scripts/posthog_annotate.py` (CI deploy job) | `annotation:write` |
| `runtime` | `POSTHOG_RUNTIME_API_KEY` | `flags.py`, `posthog_endpoints.py` | `feature_flag:read`, `query:read` |
| `query` | `POSTHOG_QUERY_API_KEY` | `scripts/posthog_error_issues.py` (host cron) | `query:read` |

`observability.posthog_hogql_query` is a runtime read too, so it rides the `runtime` key — the
`query` one is the CRON's, and the two live in different environments.

The fallback is what makes the rollout ADDITIVE: until a scoped key exists in an environment,
nothing changes there, so the keys can be created and populated one consumer at a time and the old
key revoked last (`docs/kpi-dashboards.md`). An unset scoped var is the normal state, never an error.

The provisioning scripts (`posthog_provision`, `posthog_flags`, `posthog_surveys`,
`posthog_experiments`, `posthog_dashboards`, `posthog_ops_destination`, `benchmark_models`) are run
by hand and deliberately do NOT appear here: they need a broad operator key that is exported into a
shell for the run and stored nowhere.

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

#: Purpose -> the scoped env var read BEFORE the shared fallback. Adding a purpose means adding a
#: row here, never a second resolution rule at a call site.
PURPOSE_ENV_VARS = {
    "annotation": ANNOTATION_ENV_VAR,
    "runtime": RUNTIME_ENV_VAR,
    "query": QUERY_ENV_VAR,
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


def resolve_posthog_key(purpose: str) -> str:
    """Resolve the personal API key for one purpose, scoped var first, shared key second.

    Args:
        purpose: One of the keys of `PURPOSE_ENV_VARS`.

    Returns:
        The first non-empty key value, or `""` when neither var is set — every caller already
        treats an empty key as "this surface is not configured", so this never raises for it.
    """
    for name in key_env_vars(purpose):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


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
