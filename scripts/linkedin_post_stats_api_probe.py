#!/usr/bin/env python3
"""Member post-stats API probe — issue #645.

Impressions and saves reach LEM through a **Selenium scrape** of
``/analytics/post-summary/<activity-urn>/`` (``_post_analytics_counts`` in
``app/engagement/posting.py``). That works, but it breaks on any SDUI relayout and rides the
same 429 breaker as the rest of the Selenium lanes.

LinkedIn's versioned ``GET /rest/memberCreatorPostAnalytics`` returns the same numbers for the
**authenticated member's own** posts over HTTP, with no browser and no DOM. This script settles
whether LEM's stored member token can actually read it — and, when it can, whether the numbers
agree with what the scrape already stored.

**Read-only.** Every call is a GET. It publishes nothing and writes nothing back to the DB.
It is a token probe, not a browser session, so it holds no Selenium lane and is 429-immune.

``scripts/`` is not baked into the image, so pipe it in on stdin like the weekly version probe::

    sudo docker exec -i celery_worker python - --user-id 1 --post-id 123 \
        < scripts/linkedin_post_stats_api_probe.py

One JSON report on stdout — paste it into the issue. No token or response header is ever echoed.

Three facts the report is built to separate, because they demand different fixes:

* **403** — the token was never granted ``r_member_postAnalytics`` (a Community Management API
  permission). LEM's authorize call asks for ``openid profile email w_member_social``
  (``api/main.py``), so this is the expected answer today and it is a *scope + app-provisioning*
  problem, not a code one.
* **426** — ``LI_API_VERSION`` is retired. Unrelated to this surface; fix the pin.
* **400 / absent metric** — the pinned version predates the metric. ``q=entity`` (single-post
  stats) landed in ``202506``; ``POST_SAVE`` only in ``202604``. The report states the floor for
  every metric it asks for so a 400 is never misread as "LinkedIn does not expose saves".
* **400 under ``--aggregation DAILY``** — LinkedIn serves some metrics under ``TOTAL`` only
  (``IMPRESSION`` among them, for a post entity) and answers the rest with the SAME 400 shape as a
  version gap. That one is named separately, because the fix is the operator's own flag.

The parsing/verdict logic is pure and unit-tested; the HTTP call is injected so tests never
touch the network.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

API_URL = "https://api.linkedin.com/rest/memberCreatorPostAnalytics"
REQUIRED_PERMISSION = "r_member_postAnalytics"

# The finder that takes a single post. Aggregated ("q=me") stats have been available longer but
# cannot be attributed to a post_stats row, so they are not what this probe is about.
ENTITY_FINDER_MIN_VERSION = "202506"
# Before this version metricType came back as {"com.linkedin...MetricTypeV1": "REACTION"};
# from it, as a bare string. Both shapes are parsed — a probe that only understood one would
# read a perfectly good response as empty.
STRING_METRIC_TYPE_MIN_VERSION = "202605"

# queryType -> the post_stats column the scrape stores it in.
METRIC_COLUMNS = {
    "IMPRESSION": "impressions",
    "REACTION": "reactions",
    "COMMENT": "comments",
    "RESHARE": "reposts",
    "POST_SAVE": "saves",
}
# The oldest LinkedIn-Version that serves each metric through the entity finder.
METRIC_MIN_VERSION = {
    "IMPRESSION": ENTITY_FINDER_MIN_VERSION,
    "REACTION": ENTITY_FINDER_MIN_VERSION,
    "COMMENT": ENTITY_FINDER_MIN_VERSION,
    "RESHARE": ENTITY_FINDER_MIN_VERSION,
    "POST_SAVE": "202604",
}
DEFAULT_METRICS = tuple(METRIC_COLUMNS)

# LinkedIn documents DAILY as unsupported for these queryTypes ("Daily impression metrics are not
# supported if given entity is post", plus the MEMBERS_REACHED/LINK_CLICKS/FOLLOWER_GAINED_FROM_
# CONTENT/PROFILE_VIEW_FROM_CONTENT note). Those 400s carry the SAME "query type ... metric type"
# error shape as a version gap, so without this the probe would send whoever reads the report off
# to bump LI_API_VERSION over a flag they set themselves. TOTAL — the default — clears all of it.
DAILY_UNSUPPORTED_METRICS = frozenset({
    "IMPRESSION", "MEMBERS_REACHED", "LINK_CLICKS",
    "FOLLOWER_GAINED_FROM_CONTENT", "PROFILE_VIEW_FROM_CONTENT",
})

# The entity finder takes a share or ugcPost URN. An ACTIVITY urn — which is what the analytics
# PAGE keys off, and what _post_analytics_counts resolves by following the redirect — is not a
# valid entity here, and the numeric ids are not interchangeable. Say so rather than guess.
_URN_RE = re.compile(r"urn:li:(share|ugcPost|activity):[0-9]+")
_ENTITY_KEYS = {"share": "share", "ugcPost": "ugc"}

_BODY_EXCERPT = 300


class ProbeError(RuntimeError):
    """The probe could not be run at all (no token, no usable URN)."""


def urn_from_post_url(post_url: Optional[str]) -> Optional[str]:
    """Pull the share/ugcPost/activity URN out of a feed permalink."""
    match = _URN_RE.search(post_url or "")
    return match.group(0) if match else None


def urn_type(urn: Optional[str]) -> Optional[str]:
    match = _URN_RE.fullmatch((urn or "").strip())
    return match.group(1) if match else None


def entity_param(urn: Optional[str]) -> Optional[str]:
    """Build the finder's ``entity`` value, e.g. ``(share:urn%3Ali%3Ashare%3A123)``.

    None when the URN is missing, malformed, or an activity URN (unsupported entity type)."""
    key = _ENTITY_KEYS.get(urn_type(urn) or "")
    if not key:
        return None
    return f"({key}:{urllib.parse.quote(urn.strip(), safe='')})"


def version_supports(configured: Optional[str], minimum: str) -> Optional[bool]:
    """Is ``configured`` at or past ``minimum``? None when the pin isn't a YYYYMM string."""
    if not configured or not str(configured).isdigit() or len(str(configured)) != 6:
        return None
    return int(configured) >= int(minimum)


def aggregation_supports(metric: str, aggregation: str) -> bool:
    """Is this metric served under this aggregation? Only DAILY is ever restricted."""
    return not (str(aggregation).upper() == "DAILY" and metric in DAILY_UNSUPPORTED_METRICS)


def metric_type_name(raw) -> Optional[str]:
    """Read metricType in either the pre-202605 object shape or the bare-string shape."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, str):
                return value
    return None


def metric_total(payload, metric: str) -> Optional[int]:
    """Total count for ``metric`` across the response's elements.

    None — never 0 — when the response carries no matching element: "LinkedIn returned nothing
    for this metric" and "this post has zero" are different facts, and merging them would let an
    unprovisioned surface look like a post nobody saw."""
    if not isinstance(payload, dict):
        return None
    elements = payload.get("elements")
    if not isinstance(elements, list):
        return None
    total, matched = 0, False
    for element in elements:
        if not isinstance(element, dict):
            continue
        name = metric_type_name(element.get("metricType"))
        if name is not None and name != metric:
            continue
        count = element.get("count")
        if isinstance(count, bool) or not isinstance(count, int):
            continue
        total += count
        matched = True
    return total if matched else None


def classify_status(status: int, body: str) -> str:
    """Name the failure mode so the report says what to FIX, not just that it broke."""
    if status == 200:
        return "ok"
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 426:
        return "version_retired"
    if status == 429:
        return "throttled"
    if status == 400:
        # LinkedIn's own error table answers an unknown queryType/aggregation pairing with a 400
        # naming both. That is "the pinned version does not serve this metric", not a malformed
        # request, and the fix is LI_API_VERSION — so it must not read as a coding bug.
        lowered = (body or "").lower()
        if "query type" in lowered or "querytype" in lowered or "metric type" in lowered:
            return "unsupported_metric"
        return "bad_request"
    if 500 <= status < 600:
        return "server_error"
    return f"http_{status}"


def http_get_json(url: str, token: str, version: str, timeout: int = 20) -> tuple[int, str]:
    """GET the versioned API. Returns (status, body). Never raises for an HTTP status."""
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": version,
        "X-Restli-Protocol-Version": "2.0.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise ProbeError(f"could not reach LinkedIn: {e}") from e


def build_url(entity: str, metric: str, aggregation: str = "TOTAL") -> str:
    return (f"{API_URL}?q=entity&entity={entity}&queryType={metric}"
            f"&aggregation={aggregation}")


def probe_metric(entity: str, metric: str, token: str, version: str,
                 aggregation: str = "TOTAL", fetch: Optional[Callable] = None) -> dict:
    """Probe ONE metric. Records status, classification, count and the response's shape."""
    fetch = fetch or http_get_json
    result = {
        "metric": metric,
        "column": METRIC_COLUMNS.get(metric),
        "min_version": METRIC_MIN_VERSION.get(metric),
        "version_ok": version_supports(version, METRIC_MIN_VERSION.get(metric, "000000")),
        "aggregation": aggregation,
        "aggregation_ok": aggregation_supports(metric, aggregation),
    }
    status, body = fetch(build_url(entity, metric, aggregation), token, version)
    result["http_status"] = status
    result["status"] = classify_status(status, body)
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        payload = None
    if result["status"] == "ok" and isinstance(payload, dict):
        result["count"] = metric_total(payload, metric)
        result["element_count"] = len(payload.get("elements") or [])
        result["metric_type_shape"] = _metric_type_shape(payload)
    else:
        result["count"] = None
        result["error"] = _error_excerpt(payload, body)
    return result


def _metric_type_shape(payload: dict) -> Optional[str]:
    """'string' or 'object' — evidence of which side of the 202605 change served us."""
    for element in payload.get("elements") or []:
        if isinstance(element, dict) and "metricType" in element:
            return "string" if isinstance(element["metricType"], str) else "object"
    return None


def _error_excerpt(payload, body: str) -> str:
    """LinkedIn's own error message, truncated. Response bodies here carry no user content."""
    if isinstance(payload, dict):
        for key in ("message", "code", "status"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:_BODY_EXCERPT]
    return (body or "")[:_BODY_EXCERPT]


def compare_counts(results: list, stored: Optional[dict]) -> dict:
    """API count vs the number the scrape stored, per signal.

    ``match`` is None whenever either side is unknown — an unread signal is never graded as a
    disagreement, and never as agreement either."""
    comparison = {}
    for result in results:
        column = result.get("column")
        if not column:
            continue
        api_value = result.get("count")
        stored_value = (stored or {}).get(column)
        comparison[column] = {
            "api": api_value,
            "scrape": stored_value,
            "match": None if api_value is None or stored_value is None
            else api_value == stored_value,
            "delta": None if api_value is None or stored_value is None
            else api_value - stored_value,
        }
    return comparison


def verdict(results: list, comparison: dict) -> dict:
    """One overall answer plus the action it implies."""
    statuses = {r.get("status") for r in results}
    if not results:
        return {"outcome": "not_probed", "reason": "no metrics probed"}
    if "forbidden" in statuses:
        return {"outcome": "blocked",
                "reason": f"403 — the token lacks {REQUIRED_PERMISSION} (or the app is not "
                          f"provisioned for the Community Management API, or the probed post is "
                          f"not this member's own); keep the scrape"}
    if "unauthorized" in statuses:
        return {"outcome": "error",
                "reason": "401 — the stored member token is expired or invalid; re-probe after "
                          "reconnecting LinkedIn"}
    if "version_retired" in statuses:
        return {"outcome": "error",
                "reason": "426 — LI_API_VERSION is retired; bump it and re-probe"}
    if statuses == {"ok"}:
        graded = [c for c in comparison.values() if c["match"] is not None]
        mismatched = [k for k, c in comparison.items() if c["match"] is False]
        if not graded:
            return {"outcome": "available",
                    "reason": "every metric returned, but no stored scrape row to compare against"}
        if mismatched:
            return {"outcome": "available_mismatch",
                    "reason": f"every metric returned; disagrees with the scrape on "
                              f"{', '.join(sorted(mismatched))}"}
        return {"outcome": "available_match",
                "reason": f"every metric returned and agrees with the scrape on "
                          f"{len(graded)} signal(s)"}
    if "unsupported_metric" in statuses:
        # The aggregation is checked FIRST: it is the one cause the operator introduced with a
        # flag, and LinkedIn answers it with the same 400 shape as a version gap. Blaming
        # LI_API_VERSION for a --aggregation DAILY run would send them to fix the wrong thing.
        by_aggregation = sorted(r["metric"] for r in results
                                if r.get("status") == "unsupported_metric"
                                and r.get("aggregation_ok") is False)
        if by_aggregation:
            return {"outcome": "partial",
                    "reason": f"LinkedIn does not serve {', '.join(by_aggregation)} under DAILY "
                              f"aggregation — re-probe with --aggregation TOTAL"}
        stale = sorted(r["metric"] for r in results
                       if r.get("status") == "unsupported_metric" and r.get("version_ok") is False)
        if stale:
            return {"outcome": "partial",
                    "reason": f"LinkedIn-Version predates {', '.join(stale)} "
                              f"(floor: {', '.join(sorted({METRIC_MIN_VERSION[m] for m in stale}))})"
                              f" — bump LI_API_VERSION and re-probe"}
    failed = sorted(s for s in statuses if s != "ok")
    return {"outcome": "partial", "reason": f"some metrics failed: {', '.join(failed)}"}


def build_report(entity_urn: str, entity: str, version: str, results: list,
                 stored: Optional[dict], aggregation: str = "TOTAL") -> dict:
    comparison = compare_counts(results, stored)
    return {
        "probe": "memberCreatorPostAnalytics",
        "entity_urn": entity_urn,
        "entity_param": entity,
        "linkedin_version": version,
        # Recorded because it changes which metrics LinkedIn will serve at all — a report pasted
        # into an issue without it cannot be read.
        "aggregation": aggregation,
        "required_permission": REQUIRED_PERMISSION,
        "entity_finder_min_version": ENTITY_FINDER_MIN_VERSION,
        "metric_type_string_since": STRING_METRIC_TYPE_MIN_VERSION,
        "metrics": results,
        "comparison": comparison,
        "verdict": verdict(results, comparison),
    }


def _resolve_from_db(args, need_url: bool) -> tuple[Optional[str], Optional[str], Optional[dict]]:
    """(token, post_url, stored_stats) — the DB is only touched for what was NOT passed in, so a
    fully-specified probe runs anywhere the DB does not (and tests never import it)."""
    token, post_url, stored = args.token, args.post_url, None
    if token and (post_url or not need_url) and args.post_id is None:
        return token, post_url, None
    from cqc_lem.utilities.db import get_latest_post_stats, get_post_url_from_log_for_user, \
        get_user_access_token
    if not token:
        token = get_user_access_token(args.user_id)
    if args.post_id is not None:
        post_url = post_url or get_post_url_from_log_for_user(args.user_id, args.post_id)
        stored = get_latest_post_stats(args.user_id, args.post_id)
    return token, post_url, stored


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Probe LinkedIn member post stats (read-only)")
    parser.add_argument("--user-id", type=int, default=1, help="whose stored token to probe with")
    parser.add_argument("--post-id", type=int, help="LEM post id — resolves the URL and the "
                                                    "stored scrape row to compare against")
    parser.add_argument("--post-url", help="post permalink (skips the log lookup)")
    parser.add_argument("--entity-urn", help="share/ugcPost URN (skips URL parsing entirely)")
    parser.add_argument("--token", help="access token (default: the user's, from the DB)")
    parser.add_argument("--version", help="LinkedIn-Version (default: LI_API_VERSION)")
    parser.add_argument("--metrics", nargs="+", default=list(DEFAULT_METRICS),
                        choices=list(METRIC_COLUMNS))
    parser.add_argument("--aggregation", default="TOTAL", choices=["TOTAL", "DAILY"])
    args = parser.parse_args(argv)

    version = args.version
    if not version:
        from cqc_lem.utilities.env_constants import LI_API_VERSION
        version = LI_API_VERSION

    try:
        token, post_url, stored = _resolve_from_db(args, need_url=not args.entity_urn)
        if not token:
            raise ProbeError(f"no LinkedIn access token for user {args.user_id}")
        entity_urn = args.entity_urn or urn_from_post_url(post_url)
        if not entity_urn:
            raise ProbeError("no share/ugcPost URN — pass --entity-urn, --post-url or --post-id")
        entity = entity_param(entity_urn)
        if not entity:
            raise ProbeError(
                f"{entity_urn} is not a valid entity for this finder — it takes share/ugcPost "
                f"URNs, and an activity URN's id is not interchangeable with them")
        results = [probe_metric(entity, metric, token, version, args.aggregation)
                   for metric in args.metrics]
    except ProbeError as e:
        print(json.dumps({"probe": "memberCreatorPostAnalytics",
                          "verdict": {"outcome": "error", "reason": str(e)}}))
        return 1

    print(json.dumps(build_report(entity_urn, entity, version, results, stored,
                                  args.aggregation), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
