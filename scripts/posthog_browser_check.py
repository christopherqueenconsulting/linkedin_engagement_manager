#!/usr/bin/env python3
"""Read-only preflight for the SPA's browser-side PostHog install (issue #1676).

`utilities/observability.py` has a preflight for the personal API keys
(`scripts/posthog_key_check.py`); the BROWSER half had none, and it fails in exactly the same
shape — silently.

`VITE_POSTHOG_KEY` is a Vite **build arg** (`compose/local/Dockerfile`, `build-and-push.yml`), so
the key is inlined into the bundle at image-build time. When that arg arrives empty,
`analytics.ts` short-circuits at `if (!KEY)`: the `posthog-js` chunk is never imported and the
browser makes NO request at all. From PostHog's side that is indistinguishable from a quiet day —
no error, no rejected key, just an absence — which is what issue #1676 was filed against.

So this proves the chain end to end, from the artifact the browser actually downloads to rows in
ClickHouse, and prints one PASS/FAIL line per link:

1. `shell`     — the live `index.html` is reachable and references JS assets.
2. `bundle`    — a `phc_` project token is INLINED in that entry graph (the empty-build-arg case),
                 and matches the expected token when one is supplied.
3. `api-host`  — the ingestion host is inlined, so captures are addressed somewhere real.
4. `ingest`    — browser-sourced (`$lib = 'web'`) `$pageview` rows exist for that host inside the
                 window. Zero rows is a FAIL here, unlike `posthog_key_check.py`: this check's whole
                 question is whether events ARRIVE, so "the query ran and found nothing" is the bug.

**It only ever reads.** Every site request is a GET and the PostHog request is a HogQL query — no
event is captured, nothing is provisioned, and the SPA is not touched. Safe to run against
production repeatedly.

Split like the other `posthog_*.py` scripts: PURE parsing/classification logic (unit-tested) over
thin I/O clients (mocked in tests).

CLI:
  --site URL          Site to inspect (default https://lem.christopherqueenconsulting.com).
  --hours N           Ingestion window in hours (default 24).
  --project-token T   Token the bundle is expected to carry. Default: $VITE_POSTHOG_KEY, and with
                      neither the bundle check asserts presence only.
  --max-assets N      Cap on entry-graph assets fetched (default 8).
  --skip-ingest       Artifact checks only; makes no PostHog request and needs no API key.
  --timeout S         Per-request timeout in seconds (default 30).
Env:
  POSTHOG_QUERY_API_KEY   HogQL key for the ingest check, falling back to
                          POSTHOG_PERSONAL_API_KEY (posthog_keys.py owns that precedence).
  POSTHOG_PROJECT_ID      PostHog project id (default 475262 — "CQC LEM").
  POSTHOG_APP_HOST        App host for the API (default https://us.posthog.com).
  VITE_POSTHOG_KEY        Default for --project-token.
Exit: 0 every check passed, 1 at least one failed, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

# Reached by path, not by installation — the same way posthog_key_check.py reaches the resolver, so
# this runs from a cron clone or a bare checkout where the app is not installed. posthog_keys.py is
# stdlib-only, so this costs nothing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cqc_lem.utilities.posthog_keys import (  # noqa: E402
    missing_key_message,
    resolve_posthog_key_source,
)

DEFAULT_SITE = "https://lem.christopherqueenconsulting.com"
DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"
DEFAULT_HOURS = 24
DEFAULT_MAX_ASSETS = 8
DEFAULT_TIMEOUT_SECONDS = 30

#: The purpose whose key this script reads with. `query` is the HOST-CRON read key, which is what a
#: hand- or cron-run script on the box resolves; the app containers' `runtime` key is a different
#: environment (see posthog_keys.py).
KEY_PURPOSE = "query"

#: A PostHog project API key as it appears inlined in a built bundle. Deliberately anchored on the
#: `phc_` prefix rather than on any surrounding JS: minifiers rename everything around it, so the
#: token is the only stable thing in there.
_TOKEN_RE = re.compile(r"phc_[A-Za-z0-9]{20,}")

#: `<script type="module" src="...">` and `<link rel="modulepreload" href="...">`. Those two are the
#: STATIC entry graph, which is where `analytics.ts` lands: `main.tsx` imports `initAnalytics`
#: eagerly, so the `posthog.init(KEY, …)` call site — and therefore the inlined token — cannot be in
#: the lazily-imported `posthog-js` chunk this deliberately does not fetch.
_ASSET_RE = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+\.js)["']""", re.IGNORECASE)

#: Any PostHog ingestion host, `us`/`eu` or a reverse proxy that keeps the suffix.
_INGEST_HOST_RE = re.compile(r"https://[A-Za-z0-9.\-]+\.posthog\.com")

#: A plausible DNS hostname. Used to reject a `--site` typo before any request is made.
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9\-.]*[A-Za-z0-9])?")


def site_host(site: str) -> str:
    """The bare hostname of `site`, which is what `$host` carries on a captured event.

    Doubles as the `--site` validator, so a typo is a usage error BEFORE any request goes out:
    `urlparse` happily returns a hostname for nonsense like `"not a url"`, and letting that through
    turns one bad argument into four confusing transport failures.

    Args:
        site: Site URL, with or without a scheme.

    Returns:
        The hostname, lowercased and without a port, or "" when `site` is not a usable http(s) URL.
    """
    parsed = urlparse(site if "://" in site else f"https://{site}")
    if parsed.scheme not in ("http", "https"):
        return ""
    host = (parsed.hostname or "").lower()
    return host if _HOSTNAME_RE.fullmatch(host) else ""


def parse_asset_urls(html: str, base_url: str, limit: int = DEFAULT_MAX_ASSETS) -> list:
    """Absolute URLs of the JS assets `index.html` pulls in, in document order.

    Args:
        html: The fetched `index.html`.
        base_url: URL it was fetched from, used to resolve relative hrefs.
        limit: Most assets to return. The entry graph is small; a cap keeps a hostile or
            mis-served page from turning a preflight into a crawl.

    Returns:
        Deduplicated absolute URLs, at most `limit` of them.
    """
    urls = []
    for match in _ASSET_RE.finditer(html or ""):
        absolute = urljoin(base_url, match.group(1))
        if absolute not in urls:
            urls.append(absolute)
    return urls[:limit]


def find_project_tokens(text: str) -> list:
    """Every distinct PostHog project token inlined in `text`, in first-seen order.

    Args:
        text: Bundle source.

    Returns:
        The matching `phc_…` tokens.
    """
    found = []
    for match in _TOKEN_RE.finditer(text or ""):
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


def find_ingest_hosts(text: str) -> list:
    """Every distinct PostHog ingestion host inlined in `text`, in first-seen order.

    Args:
        text: Bundle source.

    Returns:
        The matching `https://….posthog.com` origins.
    """
    found = []
    for match in _INGEST_HOST_RE.finditer(text or ""):
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


def mask_token(token: str) -> str:
    """A token rendered so two tokens can be COMPARED in a log without publishing either.

    The project key is public by design (it ships in the bundle), but this script also prints the
    EXPECTED token, which is read out of the environment — and an env var is not something a
    preflight should echo in full.

    Args:
        token: A `phc_…` token, or "".

    Returns:
        e.g. `"phc_examp1…6789"`, or `"-"` for an empty token.
    """
    if not token:
        return "-"
    return token if len(token) <= 14 else f"{token[:9]}…{token[-4:]}"


def classify_shell(status: int, assets: list) -> dict:
    """Read the `index.html` fetch.

    Args:
        status: HTTP status of the fetch.
        assets: URLs `parse_asset_urls` found in it.

    Returns:
        `{"ok": bool, "detail": str}`.
    """
    if not 200 <= status < 300:
        return {"ok": False, "detail": f"HTTP {status} — the site shell did not load"}
    if not assets:
        return {"ok": False,
                "detail": "HTTP 200 but no JS asset referenced — the SPA shell is not being served"}
    return {"ok": True, "detail": f"HTTP {status}, {len(assets)} JS asset(s) referenced"}


def classify_bundle(tokens: list, expected: str = "") -> dict:
    """Read the inlined project token — the empty-build-arg failure this script exists for.

    Args:
        tokens: Every token found across the entry graph.
        expected: The token the build should have carried, or "" to check presence only.

    Returns:
        `{"ok": bool, "detail": str}`.
    """
    if not tokens:
        return {"ok": False,
                "detail": ("no phc_ token inlined — the build shipped an empty VITE_POSTHOG_KEY, "
                           "so posthog-js is never imported and the browser sends NOTHING")}
    found = ", ".join(mask_token(token) for token in tokens)
    if not expected:
        return {"ok": True, "detail": f"token inlined: {found} (no expected token supplied)"}
    if expected in tokens:
        return {"ok": True, "detail": f"token inlined and matches expected: {mask_token(expected)}"}
    return {"ok": False,
            "detail": (f"inlined token {found} does not match expected {mask_token(expected)} — "
                       "the bundle is reporting to a different project")}


def classify_api_host(hosts: list) -> dict:
    """Read the inlined ingestion host.

    Args:
        hosts: Every ingestion host found across the entry graph.

    Returns:
        `{"ok": bool, "detail": str}`.
    """
    if not hosts:
        return {"ok": False,
                "detail": "no PostHog ingestion host inlined — captures have nowhere to go"}
    return {"ok": True, "detail": f"api_host: {', '.join(hosts)}"}


def pageview_hogql(host: str, hours: int = DEFAULT_HOURS) -> str:
    """The one query the ingest check runs.

    Filtered to `$lib = 'web'` and to the site's own `$host` on purpose: the point is that the
    BROWSER on THAT deployment is reaching PostHog, and this project also receives server-side
    captures from Celery and the API, which would otherwise mask a dead SPA.

    Args:
        host: Hostname to match against `$host`.
        hours: Lookback window in hours. 0/None fall back to the default, anything else to a
            minimum of 1 hour.

    Returns:
        A HogQL string returning one row: `(views, people, last_seen)`.
    """
    window = max(1, int(hours or DEFAULT_HOURS))
    safe_host = (host or "").replace("'", "")
    return ("SELECT count() AS views, uniq(distinct_id) AS people, max(timestamp) AS last_seen "
            "FROM events WHERE event = '$pageview' AND properties.$lib = 'web' "
            f"AND properties.$host = '{safe_host}' "
            f"AND timestamp > now() - INTERVAL {window} HOUR")


def parse_query_rows(body: str) -> list:
    """The `results` rows out of a PostHog query response.

    Args:
        body: Raw response body.

    Returns:
        The rows, or `[]` when the body is not JSON or carries no results.
    """
    try:
        payload = json.loads(body or "")
    except (TypeError, ValueError):
        return []
    results = payload.get("results") if isinstance(payload, dict) else None
    return results if isinstance(results, list) else []


def classify_ingest(status: int, body: str, host: str, hours: int) -> dict:
    """Read the ingest query — the only check where ZERO rows is a failure.

    `posthog_key_check.py` passes on an empty result because it asks "may this key query?". This
    asks "did the browser report?", so an empty answer IS the reported defect.

    Args:
        status: HTTP status of the query.
        body: Raw response body.
        host: Hostname the query filtered on.
        hours: The window queried, for the message.

    Returns:
        `{"ok": bool, "detail": str}`.
    """
    if not 200 <= status < 300:
        hint = {
            401: "the key is rejected — wrong value, or revoked",
            403: "authenticated but the key lacks query:read",
            404: "wrong project id, or the query API is unavailable",
        }.get(status, "unexpected status")
        snippet = (body or "").strip().replace("\n", " ")[:160]
        detail = f"HTTP {status} — {hint}"
        return {"ok": False, "detail": f"{detail}: {snippet}" if snippet else detail}
    rows = parse_query_rows(body)
    if not rows or not rows[0]:
        return {"ok": False,
                "detail": f"no $pageview rows for {host} in the last {hours}h — "
                          "the browser is not reaching PostHog"}
    row = rows[0]
    views = row[0] if len(row) > 0 else 0
    people = row[1] if len(row) > 1 else 0
    last_seen = row[2] if len(row) > 2 else "?"
    if not views:
        return {"ok": False,
                "detail": f"0 $pageview in the last {hours}h for {host} — "
                          "the browser is not reaching PostHog"}
    return {"ok": True,
            "detail": f"{views} $pageview / {people} person(s) in {hours}h, last {last_seen}"}


def format_result(result: dict) -> str:
    """One aligned line per check.

    Args:
        result: A dict carrying `name`, `ok` and `detail`.

    Returns:
        The line to print.
    """
    status = "PASS" if result["ok"] else "FAIL"
    return f"{status}  {result['name']:<10} {result['detail']}"


def summarize(results: list) -> str:
    """A one-line tally.

    Args:
        results: Every result produced by the run.

    Returns:
        e.g. `"3 passed, 1 failed"`.
    """
    passed = sum(1 for result in results if result["ok"])
    return f"{passed} passed, {len(results) - passed} failed"


def exit_code(results: list) -> int:
    """0 only when every check passed.

    Args:
        results: Every result produced by the run.

    Returns:
        0 or 1. An empty list is 1: checking nothing must never read as success.
    """
    if not results:
        return 1
    return 0 if all(result["ok"] for result in results) else 1


class SiteReader:
    """GETs the deployed artifacts. The only requests made to the SPA."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def get(self, url: str) -> tuple:
        """Fetch one URL.

        Args:
            url: Absolute URL.

        Returns:
            `(status_code, body_text)`.
        """
        import requests
        response = requests.get(url, timeout=self.timeout,
                                headers={"Cache-Control": "no-cache"})
        return response.status_code, (response.text or "")


class PostHogReader:
    """The HogQL read half of the PostHog REST API."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def query(self, url: str, hogql: str, api_key: str) -> tuple:
        """Run one HogQL query.

        Args:
            url: The project's `/query/` URL.
            hogql: The query string.
            api_key: The resolved personal API key.

        Returns:
            `(status_code, body_text)`.
        """
        import requests
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": {"kind": "HogQLQuery", "query": hogql}},
            timeout=self.timeout,
        )
        return response.status_code, (response.text or "")[:600]


def check_artifacts(site: str, reader: "SiteReader", expected_token: str = "",
                    max_assets: int = DEFAULT_MAX_ASSETS) -> list:
    """Run the three artifact checks against the deployed SPA.

    A transport error is a FAIL like any other: from a visitor's side an unreachable site and a
    keyless bundle both produce no events.

    Args:
        site: Site URL.
        reader: The site I/O client.
        expected_token: Token the bundle should carry, or "".
        max_assets: Cap on entry-graph assets fetched.

    Returns:
        Three result dicts: `shell`, `bundle`, `api-host`.
    """
    base = site if site.endswith("/") else f"{site}/"
    try:
        status, html = reader.get(base)
    except Exception as exc:  # noqa: BLE001 - every transport failure is one FAIL line
        failed = {"ok": False, "detail": f"request failed: {str(exc)[:160]}"}
        return [{"name": name, **failed} for name in ("shell", "bundle", "api-host")]

    assets = parse_asset_urls(html, base, limit=max_assets)
    shell = {"name": "shell", **classify_shell(status, assets)}
    if not shell["ok"]:
        skipped = {"ok": False, "detail": "not checked — the shell did not load"}
        return [shell, {"name": "bundle", **skipped}, {"name": "api-host", **skipped}]

    tokens, hosts = [], []
    for url in assets:
        try:
            asset_status, source = reader.get(url)
        except Exception:  # noqa: BLE001 - an unreadable asset is simply no evidence
            continue
        if not 200 <= asset_status < 300:
            continue
        for token in find_project_tokens(source):
            if token not in tokens:
                tokens.append(token)
        for host in find_ingest_hosts(source):
            if host not in hosts:
                hosts.append(host)
    return [
        shell,
        {"name": "bundle", **classify_bundle(tokens, expected_token)},
        {"name": "api-host", **classify_api_host(hosts)},
    ]


def check_ingest(host: str, reader: "PostHogReader", project_id: str, app_host: str,
                 hours: int = DEFAULT_HOURS) -> dict:
    """Run the ingestion check — one HogQL read.

    Args:
        host: Hostname to filter `$host` on.
        reader: The PostHog I/O client.
        project_id: PostHog project id.
        app_host: PostHog app host (trailing slash tolerated).
        hours: Lookback window in hours.

    Returns:
        A result dict named `ingest`.
    """
    key, source = resolve_posthog_key_source(KEY_PURPOSE)
    if not key:
        return {"name": "ingest", "ok": False, "detail": missing_key_message(KEY_PURPOSE)}
    url = f"{app_host.rstrip('/')}/api/projects/{project_id}/query/"
    try:
        status, body = reader.query(url, pageview_hogql(host, hours), key)
    except Exception as exc:  # noqa: BLE001 - every transport failure is one FAIL line
        return {"name": "ingest", "ok": False, "detail": f"request failed: {str(exc)[:160]}"}
    result = classify_ingest(status, body, host, max(1, int(hours or DEFAULT_HOURS)))
    return {"name": "ingest", **result, "detail": f"{result['detail']} (via {source})"}


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only check of the SPA's browser-side PostHog install.")
    parser.add_argument("--site", default=DEFAULT_SITE,
                        help=f"Site to inspect (default {DEFAULT_SITE}).")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                        help="Ingestion window in hours (default 24).")
    parser.add_argument("--project-token", default=os.getenv("VITE_POSTHOG_KEY", ""),
                        help="Token the bundle is expected to carry (default $VITE_POSTHOG_KEY).")
    parser.add_argument("--max-assets", type=int, default=DEFAULT_MAX_ASSETS,
                        help="Cap on entry-graph assets fetched (default 8).")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="Artifact checks only; makes no PostHog request.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help="Per-request timeout in seconds (default 30).")
    args = parser.parse_args(argv)

    host = site_host(args.site)
    if not host:
        print(f"--site {args.site!r} has no hostname", file=sys.stderr)
        return 2
    if args.max_assets < 1:
        print("--max-assets must be at least 1", file=sys.stderr)
        return 2

    results = check_artifacts(args.site, SiteReader(timeout=args.timeout),
                              expected_token=(args.project_token or "").strip(),
                              max_assets=args.max_assets)
    if not args.skip_ingest:
        project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
        app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)
        results.append(check_ingest(host, PostHogReader(timeout=args.timeout),
                                    project_id, app_host, hours=args.hours))

    for result in results:
        print(format_result(result))
    print(summarize(results))
    return exit_code(results)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
