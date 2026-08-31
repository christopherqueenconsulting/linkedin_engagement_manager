#!/usr/bin/env python3
"""Read-only preflight for the purpose-scoped PostHog personal keys (issue #1453).

The whole risk in splitting `POSTHOG_PERSONAL_API_KEY` is that most of its consumers fail
**silently**: a wrong or missing key makes every feature flag read its env default, empties the SPA
stats panel, files nothing from the error cron, and drops the weekly benchmark onto its in-runner
judge. Every one of those looks exactly like a quiet day, so "the deploy was green" is not evidence
that a rollout step worked.

This turns each of those absences into one loud line. For every SURFACE below — one per job a key
actually does, so a purpose whose consumer needs two scopes is checked twice — it resolves the key
the real consumer would resolve, performs ONE live read against that surface, and prints PASS/FAIL
naming the env var that actually supplied the key — which is what distinguishes a populated scoped key from
a shared fallback still doing the work mid-rollout.

**It only ever reads.** Every request is a GET, or a POST to a query/run endpoint that executes a
read — no annotation is created, no flag is written, no evaluation is triggered. Run it after
populating each consumer, and again immediately before revoking the shared key.

Split like the other `posthog_*.py` scripts: PURE planning/formatting logic (unit-tested) over a
thin I/O client (mocked in tests).

CLI:
  --purpose P     Check only this purpose (repeatable). Default: all of them.
  --list          Print the planned checks and exit; no network.
  --hours N       Window for the HogQL checks: error cron, benchmark (default 24).
  --distinct-id D distinct_id the SPA stats endpoint is run for (default 0 — any id proves the
                  key and the provisioning; rows are not the point).
  --timeout S     Per-request timeout in seconds (default 30).
Env:
  POSTHOG_ANNOTATION_API_KEY / POSTHOG_RUNTIME_API_KEY / POSTHOG_QUERY_API_KEY /
  POSTHOG_BENCHMARK_API_KEY / POSTHOG_OPERATOR_API_KEY
                              The purpose-scoped keys, each falling back to
                              POSTHOG_PERSONAL_API_KEY (posthog_keys.py owns that precedence).
  POSTHOG_PROJECT_ID          PostHog project id (default 475262 — "CQC LEM").
  POSTHOG_APP_HOST            App host for the API (default https://us.posthog.com).
Exit: 0 every checked surface passed, 1 at least one failed, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

# Reached by path, not by installation: this is run on the box and from cron clones, the same way
# posthog_annotate.py and posthog_error_issues.py reach the resolver. posthog_keys.py is
# stdlib-only, so this costs nothing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cqc_lem.utilities.posthog_keys import (  # noqa: E402
    PURPOSE_ENV_VARS,
    missing_key_message,
    resolve_posthog_key_source,
)

DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"
DEFAULT_HOURS = 24
DEFAULT_DISTINCT_ID = "0"
DEFAULT_TIMEOUT_SECONDS = 30

# Must match posthog_endpoints.ENDPOINT_POSTS_ENGAGEMENT — the SPA panel's own endpoint name. Kept
# here rather than imported so this script stays stdlib+requests (posthog_endpoints pulls the app's
# logger, and this runs where the app is not installed);
# tests/unit/scripts/test_posthog_key_check.py fails the build if the two ever drift.
STATS_ENDPOINT_NAME = "lem-posts-engagement-weekly"

# What a body carries, since a surface is otherwise pure data. `None` = GET.
BODY_NONE = None
BODY_DISTINCT_ID = "distinct_id"
BODY_HOGQL = "hogql"
BODY_EVAL_HOGQL = "eval_hogql"

#: One entry per SURFACE, not per purpose: `runtime` is one key doing two jobs, and a key that reads
#: flag definitions but cannot run an endpoint has to fail loudly on the half that is broken.
SURFACES = (
    {
        "purpose": "annotation",
        "name": "release annotations",
        "consumer": "scripts/posthog_annotate.py (build-and-push.yml deploy job)",
        "proves": "annotation scope (write implies read, so this GET is the safe probe)",
        "method": "GET",
        "path": "/api/projects/{project_id}/annotations/?limit=1",
        "body": BODY_NONE,
    },
    {
        "purpose": "runtime",
        "name": "feature-flag definitions",
        "consumer": "flags.py local evaluation",
        "proves": "feature_flag:read — without it every flag silently reads its env default",
        "method": "GET",
        "path": "/api/projects/{project_id}/feature_flags/?limit=1",
        "body": BODY_NONE,
    },
    {
        "purpose": "runtime",
        "name": "SPA stats endpoint",
        "consumer": "posthog_endpoints.py behind GET /user/posthog-stats",
        "proves": "query:read AND that the endpoint is provisioned — a 404 here is the empty panel",
        "method": "POST",
        "path": "/api/projects/{project_id}/endpoints/" + STATS_ENDPOINT_NAME + "/run/",
        "body": BODY_DISTINCT_ID,
    },
    {
        "purpose": "query",
        "name": "error-tracking HogQL",
        "consumer": "scripts/posthog_error_issues.py via error_to_issues.sh (daily cron)",
        "proves": "query:read against $exception events — no rows and no key look identical",
        "method": "POST",
        "path": "/api/projects/{project_id}/query/",
        "body": BODY_HOGQL,
    },
    {
        "purpose": "benchmark",
        "name": "LLM-evaluation API",
        "consumer": "scripts/benchmark_models.py via weekly_model_check.sh (weekly cron)",
        "proves": "evaluation:read — without it the run falls back to the in-runner judge. The "
                  "404 seen 2026-08-20/22 was a STALE PATH, not a missing scope: PostHog moved the "
                  "collection off the `llm_analytics/` prefix to `/evaluations/`, which its "
                  "published OpenAPI schema confirms (checked 2026-08-31). A 403 here IS a scope "
                  "gap — `evaluation:read` is a scope of its own, not covered by the "
                  "`llm_playground` / `llm_prompt` / `llm_skill` trio.",
        "method": "GET",
        "path": "/api/projects/{project_id}/evaluations/?limit=1",
        "body": BODY_NONE,
    },
    {
        "purpose": "benchmark",
        "name": "judge-verdict HogQL",
        "consumer": "benchmark_models.PostHogEvals.query (hogql_for_run)",
        "proves": "query:read against $ai_evaluation — the evaluation scope alone is not enough, "
                  "the run READS its verdicts back over HogQL and scores nothing without it",
        "method": "POST",
        "path": "/api/projects/{project_id}/query/",
        "body": BODY_EVAL_HOGQL,
    },
    {
        "purpose": "operator",
        "name": "dashboard listing",
        "consumer": "posthog_provision.py, posthog_dashboards.py, posthog_flags.py, "
                    "posthog_surveys.py, posthog_experiments.py, posthog_ops_destination.py, "
                    "slop_retry_clear_rate.py (all hand-run)",
        "proves": "dashboard:read as a proxy for the broad operator scope — a key missing this "
                  "makes every hand-run provisioning script unusable",
        "method": "GET",
        "path": "/api/projects/{project_id}/dashboards/?limit=1",
        "body": BODY_NONE,
    },
)


def known_purposes() -> tuple:
    """The purposes this script can check, in `SURFACES` order (not alphabetical).

    Returns:
        A tuple of purpose names, each appearing once.
    """
    seen = []
    for surface in SURFACES:
        if surface["purpose"] not in seen:
            seen.append(surface["purpose"])
    return tuple(seen)


def plan_checks(purposes: Optional[list] = None) -> list:
    """The surfaces to check, filtered to `purposes`.

    Args:
        purposes: Purpose names to keep, or None/empty for all of them.

    Returns:
        The matching `SURFACES` entries, in declaration order.

    Raises:
        ValueError: If a requested purpose is not one of `PURPOSE_ENV_VARS` — a typo would
            otherwise silently check nothing and exit 0, which is the failure this script exists to
            stop.
    """
    if not purposes:
        return list(SURFACES)
    unknown = [p for p in purposes if p not in PURPOSE_ENV_VARS]
    if unknown:
        known = ", ".join(sorted(PURPOSE_ENV_VARS))
        raise ValueError(f"Unknown purpose(s): {', '.join(unknown)} — expected one of: {known}")
    return [surface for surface in SURFACES if surface["purpose"] in purposes]


def error_hogql(hours: int = DEFAULT_HOURS) -> str:
    """The cron's own shape of query, trimmed to a count.

    The full query in `posthog_error_issues.build_query` reads a dozen `issue_*` columns; this one
    touches the same table, the same event and the same window, which is all that the KEY's scope
    depends on. Kept deliberately cheap — a preflight is run repeatedly during a rollout.

    Args:
        hours: Lookback window in hours. Floored the same way `build_query` floors it —
            0/None fall back to the default, anything else to a minimum of 1 hour.

    Returns:
        A HogQL string.
    """
    window = max(1, int(hours or DEFAULT_HOURS))
    return ("SELECT count() FROM events WHERE event = '$exception' "
            f"AND timestamp > now() - INTERVAL {window} HOUR")


def evaluation_hogql(hours: int = DEFAULT_HOURS) -> str:
    """The benchmark's verdict read-back, trimmed to a count.

    `benchmark_models.hogql_for_run` reads five `$ai_evaluation` properties for ONE run id; the key
    scope it depends on is the same for a count over the same event, and a count needs no run to
    exist. Zero rows is a PASS here on purpose — this asks "may this key query?", not "did last
    Sunday score?".

    Args:
        hours: Lookback window in hours, floored like `error_hogql`.

    Returns:
        A HogQL string.
    """
    window = max(1, int(hours or DEFAULT_HOURS))
    return ("SELECT count() FROM events WHERE event = '$ai_evaluation' "
            f"AND timestamp > now() - INTERVAL {window} HOUR")


def request_spec(surface: dict, project_id: str, app_host: str,
                 hours: int = DEFAULT_HOURS,
                 distinct_id: str = DEFAULT_DISTINCT_ID) -> dict:
    """Turn one surface into the exact request to make.

    Args:
        surface: An entry of `SURFACES`.
        project_id: PostHog project id.
        app_host: PostHog app host (trailing slash tolerated).
        hours: Window for the HogQL surfaces.
        distinct_id: distinct_id the stats endpoint is run for.

    Returns:
        `{"method": ..., "url": ..., "json": ... or None}`.
    """
    body = None
    if surface["body"] == BODY_DISTINCT_ID:
        body = {"variables": {"distinct_id": str(distinct_id)}}
    elif surface["body"] == BODY_HOGQL:
        body = {"query": {"kind": "HogQLQuery", "query": error_hogql(hours)}}
    elif surface["body"] == BODY_EVAL_HOGQL:
        body = {"query": {"kind": "HogQLQuery", "query": evaluation_hogql(hours)}}
    return {
        "method": surface["method"],
        "url": app_host.rstrip("/") + surface["path"].format(project_id=project_id),
        "json": body,
    }


def is_read_only(surface: dict) -> bool:
    """Whether a surface is structurally incapable of writing to PostHog.

    A GET is read-only by definition; the two POSTs are PostHog's read verbs (executing a HogQL
    query, running a provisioned `/endpoints/` query). Anything else would make this script able to
    change the project it is supposed to be inspecting.

    `/run/` is NOT enough on its own: a `/run/` suffix elsewhere in PostHog's API executes work
    rather than reading it, so the `/endpoints/` prefix is what makes this one safe. Triggering a
    judge evaluation — real spend and a new `$ai_evaluation` event — is a POST to
    `/evaluation_runs/` (`benchmark_models.PostHogEvals.run_evaluation`), which this predicate
    rejects, and that is the point: nothing here may spend.

    Args:
        surface: An entry of `SURFACES`.

    Returns:
        True when the surface only reads.
    """
    if surface["method"] == "GET":
        return True
    if surface["method"] != "POST":
        return False
    path = surface["path"].split("?")[0]
    if path.endswith("/query/"):
        return True
    return "/endpoints/" in path and path.endswith("/run/")


def describe_key(purpose: str) -> dict:
    """Resolve one purpose's key without revealing it.

    Args:
        purpose: One of `PURPOSE_ENV_VARS`.

    Returns:
        `{"key": <value>, "source": <env var name or "">, "message": <"no key" line or "">}`.
        The key itself is returned for the request and never printed.
    """
    key, source = resolve_posthog_key_source(purpose)
    return {
        "key": key,
        "source": source,
        "message": "" if key else missing_key_message(purpose),
    }


def classify_response(status: int, body: str = "") -> dict:
    """Read one response as PASS/FAIL, naming the likely cause of a failure.

    Args:
        status: HTTP status code.
        body: Response body (truncated by the caller); only used to make a detail readable.

    Returns:
        `{"ok": bool, "detail": str}`.
    """
    if 200 <= status < 300:
        return {"ok": True, "detail": f"HTTP {status}"}
    hint = {
        401: "the key is rejected — wrong value, or revoked",
        403: "authenticated but the key lacks this scope",
        404: "not provisioned in this project (or the project id is wrong)",
    }.get(status, "unexpected status")
    snippet = (body or "").strip().replace("\n", " ")[:160]
    detail = f"HTTP {status} — {hint}"
    return {"ok": False, "detail": f"{detail}: {snippet}" if snippet else detail}


def format_result(result: dict) -> str:
    """One aligned line per checked surface.

    Args:
        result: A dict from `run_check` (or the no-key short circuit).

    Returns:
        The line to print.
    """
    status = "PASS" if result["ok"] else "FAIL"
    source = result.get("source") or "-"
    return (f"{status}  {result['purpose']:<10} {result['name']:<26} "
            f"via {source:<28} {result['detail']}")


def summarize(results: list) -> str:
    """A one-line tally.

    Args:
        results: Every result produced by the run.

    Returns:
        e.g. `"4 passed, 1 failed"`.
    """
    passed = sum(1 for result in results if result["ok"])
    return f"{passed} passed, {len(results) - passed} failed"


def exit_code(results: list) -> int:
    """0 only when every checked surface passed.

    Args:
        results: Every result produced by the run.

    Returns:
        0 or 1. An empty list is 1: checking nothing must never read as success.
    """
    if not results:
        return 1
    return 0 if all(result["ok"] for result in results) else 1


class PostHogReader:
    """The read half of the PostHog REST API — the only I/O this script performs."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def read(self, spec: dict, api_key: str) -> tuple:
        """Perform one request.

        Args:
            spec: A `request_spec` dict.
            api_key: The resolved key for that surface's purpose.

        Returns:
            `(status_code, body_text)`.
        """
        import requests
        response = requests.request(
            spec["method"], spec["url"],
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=spec["json"], timeout=self.timeout,
        )
        return response.status_code, (response.text or "")[:400]


def run_check(surface: dict, reader: "PostHogReader", project_id: str, app_host: str,
              hours: int = DEFAULT_HOURS,
              distinct_id: str = DEFAULT_DISTINCT_ID) -> dict:
    """Resolve the key for one surface and read it.

    A transport error (DNS, timeout, TLS) is a FAIL like any other: from the consumer's side an
    unreachable PostHog and a bad key produce the same silence.

    Args:
        surface: An entry of `SURFACES`.
        reader: The I/O client.
        project_id: PostHog project id.
        app_host: PostHog app host.
        hours: Window for the HogQL surfaces.
        distinct_id: distinct_id the stats endpoint is run for.

    Returns:
        `{"purpose", "name", "source", "ok", "detail"}`.
    """
    resolved = describe_key(surface["purpose"])
    base = {"purpose": surface["purpose"], "name": surface["name"], "source": resolved["source"]}
    if not resolved["key"]:
        return {**base, "ok": False, "detail": resolved["message"]}
    spec = request_spec(surface, project_id, app_host, hours=hours, distinct_id=distinct_id)
    try:
        status, body = reader.read(spec, resolved["key"])
    except Exception as exc:  # noqa: BLE001 - every transport failure is one FAIL line
        return {**base, "ok": False, "detail": f"request failed: {str(exc)[:160]}"}
    return {**base, **classify_response(status, body)}


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only check of the purpose-scoped PostHog personal API keys.")
    parser.add_argument("--purpose", action="append", dest="purposes",
                        help="Check only this purpose (repeatable). Default: all.")
    parser.add_argument("--list", action="store_true",
                        help="Print the planned checks and exit; no network.")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                        help="Window for the HogQL checks: error cron, benchmark (default 24).")
    parser.add_argument("--distinct-id", default=DEFAULT_DISTINCT_ID,
                        help="distinct_id the SPA stats endpoint is run for (default 0).")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help="Per-request timeout in seconds (default 30).")
    args = parser.parse_args(argv)

    try:
        planned = plan_checks(args.purposes)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)

    if args.list:
        for surface in planned:
            spec = request_spec(surface, project_id, app_host,
                                hours=args.hours, distinct_id=args.distinct_id)
            resolved = describe_key(surface["purpose"])
            print(f"{surface['purpose']:<10} {surface['name']:<26} "
                  f"{spec['method']} {spec['url']}")
            print(f"{'':<10} key: {resolved['source'] or 'NONE'} | consumer: {surface['consumer']}")
            print(f"{'':<10} proves: {surface['proves']}")
        return 0

    reader = PostHogReader(timeout=args.timeout)
    results = [run_check(surface, reader, project_id, app_host,
                         hours=args.hours, distinct_id=args.distinct_id)
               for surface in planned]
    for result in results:
        print(format_result(result))
    print(summarize(results))
    return exit_code(results)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
