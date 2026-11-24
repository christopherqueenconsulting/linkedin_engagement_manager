"""Read Community Management member post analytics without replacing scrape data.

This is deliberately opt-in and user-allowlisted. In main-app mode, the LEM OAuth app needs
Community Management Standard access. A temporary Development-tier bridge uses a separately
encrypted, app-bound token and never replaces the token used for publishing.
"""

import re
from typing import Optional
from urllib.parse import quote

import requests

from cqc_lem.utilities.demo_mode import DemoModeError, guard_linkedin
from cqc_lem.utilities.logger import log_info, log_warning

API_URL = "https://api.linkedin.com/rest/memberCreatorPostAnalytics"
_POST_URN_RE = re.compile(r"urn:li:(share|ugcPost):[0-9]+")
_METRICS = (
    ("IMPRESSION", "impressions", "202506"),
    ("REACTION", "reactions", "202506"),
    ("COMMENT", "comments", "202506"),
    ("RESHARE", "reposts", "202506"),
    ("POST_SAVE", "saves", "202604"),
)


def member_post_analytics_enabled_for_user(user_id: Optional[int]) -> bool:
    """Fail closed unless global switch and explicit user allowlist both include this user."""
    from cqc_lem.utilities.env_constants import LI_MEMBER_POST_ANALYTICS_ENABLED
    return bool(LI_MEMBER_POST_ANALYTICS_ENABLED and member_post_analytics_user_allowed(user_id))


def member_post_analytics_user_allowed(user_id: Optional[int]) -> bool:
    """User allowlist independent of the global read switch, for connection cleanup/status."""
    if not user_id:
        return False
    from cqc_lem.utilities.env_constants import LI_MEMBER_POST_ANALYTICS_USER_IDS
    return user_id in LI_MEMBER_POST_ANALYTICS_USER_IDS


def member_post_analytics_max_posts_per_sweep() -> int:
    """Return configured per-sweep API post cap."""
    from cqc_lem.utilities.env_constants import LI_MEMBER_POST_ANALYTICS_MAX_POSTS_PER_SWEEP
    return LI_MEMBER_POST_ANALYTICS_MAX_POSTS_PER_SWEEP


def member_post_analytics_separate_app_enabled() -> bool:
    """Report whether CMA credentials use the separate-app bridge."""
    from cqc_lem.utilities.env_constants import LI_MEMBER_POST_ANALYTICS_SEPARATE_APP_ENABLED
    return LI_MEMBER_POST_ANALYTICS_SEPARATE_APP_ENABLED


def member_post_analytics_connection_available_for_user(user_id: Optional[int]) -> bool:
    """Separate-app OAuth is visible only when enabled, configured and user-allowlisted."""
    if not member_post_analytics_user_allowed(user_id):
        return False
    from cqc_lem.utilities.env_constants import (
        LI_MEMBER_POST_ANALYTICS_CLIENT_ID,
        LI_MEMBER_POST_ANALYTICS_CLIENT_SECRET,
        LI_MEMBER_POST_ANALYTICS_REDIRECT_URL,
        LI_MEMBER_POST_ANALYTICS_SEPARATE_APP_ENABLED,
    )
    return bool(LI_MEMBER_POST_ANALYTICS_SEPARATE_APP_ENABLED
                and LI_MEMBER_POST_ANALYTICS_CLIENT_ID
                and LI_MEMBER_POST_ANALYTICS_CLIENT_SECRET
                and LI_MEMBER_POST_ANALYTICS_REDIRECT_URL)


def get_member_post_analytics_access_token(user_id: int) -> Optional[str]:
    """Select token by app mode; never substitute the old app token for the Dev-app token."""
    if member_post_analytics_separate_app_enabled():
        from cqc_lem.utilities.db import get_member_analytics_access_token
        return get_member_analytics_access_token(user_id)
    from cqc_lem.utilities.db import get_user_access_token
    return get_user_access_token(user_id)


def _entity_from_post_url(post_url: str) -> Optional[str]:
    """Build entity finder value from a share/ugcPost permalink; reject activity URNs."""
    match = _POST_URN_RE.search(post_url or "")
    if not match:
        return None
    urn = match.group(0)
    finder_key = "ugc" if match.group(1) == "ugcPost" else "share"
    return f"({finder_key}:{quote(urn, safe='')})"


def _metric_count(payload: object, metric: str) -> Optional[int]:
    """Read a metric from either LinkedIn metricType response shape; missing is never zero."""
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        return None
    total = 0
    matched = False
    for element in payload["elements"]:
        if not isinstance(element, dict):
            continue
        metric_type = element.get("metricType")
        if isinstance(metric_type, dict):
            metric_type = next((value for value in metric_type.values()
                                if isinstance(value, str)), None)
        if metric_type is not None and metric_type != metric:
            continue
        count = element.get("count")
        if isinstance(count, bool) or not isinstance(count, int):
            continue
        total += count
        matched = True
    return total if matched else None


def fetch_member_post_analytics(post_url: str, access_token: str, version: str,
                                timeout: int = 20) -> tuple[dict, Optional[str]]:
    """Read existing post_stats metrics. Return parsed counts and a non-sensitive failure label.

    Only GET requests are sent. Token, headers, and LinkedIn response bodies are never logged.
    """
    entity = _entity_from_post_url(post_url)
    if not entity or not access_token:
        return {}, "missing_token_or_share_urn"
    try:
        guard_linkedin("member_post_analytics.fetch")
    except DemoModeError:
        return {}, "demo_mode"

    counts = {}
    for metric, column, minimum_version in _METRICS:
        if not version or not version.isdigit() or len(version) != 6 \
                or int(version) < int(minimum_version):
            return counts, "unsupported_version"
        try:
            response = requests.get(
                f"{API_URL}?q=entity&entity={entity}&queryType={metric}&aggregation=TOTAL",
                headers={"Authorization": f"Bearer {access_token}",
                         "LinkedIn-Version": version,
                         "X-Restli-Protocol-Version": "2.0.0"},
                timeout=timeout,
            )
        except requests.RequestException:
            return counts, "transport_error"
        if response.status_code != 200:
            # Do not spend more of a member's daily quota after auth, version, or rate-limit
            # failures. A metric-specific 400 can still leave earlier values useful.
            return counts, f"http_{response.status_code}"
        try:
            value = _metric_count(response.json(), metric)
        except (ValueError, TypeError):
            value = None
        if value is not None:
            counts[column] = value
    if len(counts) != len(_METRICS):
        return counts, "partial_response"
    return counts, None


FOLLOWERS_API_URL = "https://api.linkedin.com/rest/memberFollowersCount?q=me"
CONNECTIONS_API_URL = "https://api.linkedin.com/rest/connectionsSize"
FOLLOWERS_SCOPE = "r_member_profileAnalytics"
CONNECTIONS_SCOPE = "r_1st_connections_size"


def _api_get_json(url: str, access_token: str, version: str, timeout: int) -> Optional[dict]:
    """GET one LinkedIn REST resource; None on any non-200, transport or parse failure."""
    try:
        guard_linkedin("member_post_analytics.audience")
    except DemoModeError:
        return None
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}", "LinkedIn-Version": version,
                     "X-Restli-Protocol-Version": "2.0.0"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _int_or_none(value: object) -> Optional[int]:
    """A real non-negative int, else None (a bool or a missing field is never a count)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def fetch_member_follower_count(access_token: str, version: str, timeout: int = 20) -> Optional[int]:
    """The member's follower count (`memberFollowersCount?q=me`); None when unreadable."""
    payload = _api_get_json(FOLLOWERS_API_URL, access_token, version, timeout)
    elements = payload.get("elements") if payload else None
    if not isinstance(elements, list) or not elements or not isinstance(elements[0], dict):
        return None
    return _int_or_none(elements[0].get("memberFollowersCount"))


def fetch_member_connection_count(person_id: str, access_token: str, version: str,
                                  timeout: int = 20) -> Optional[int]:
    """The member's first-degree connection count; None when unreadable."""
    if not person_id:
        return None
    urn = quote(f"urn:li:person:{person_id}", safe="")
    payload = _api_get_json(f"{CONNECTIONS_API_URL}/{urn}?edgeType=FIRST_DEGREE_CONNECTION",
                            access_token, version, timeout)
    return _int_or_none(payload.get("firstDegreeSize")) if payload else None


def member_audience_counts(user_id: int) -> dict:
    """API follower/connection counts for the daily snapshot (#2702 Phase 2).

    Returns `{"follower_count": int|None, "connection_count": int|None}`. Every unmet condition —
    flag off, user not allowlisted, no token, or a count's scope absent from the stored granted
    scope — leaves that count None, so the caller silently keeps the scraped value.
    """
    from cqc_lem.utilities.env_constants import LI_API_VERSION, LI_MEMBER_AUDIENCE_API_ENABLED
    counts: dict = {"follower_count": None, "connection_count": None}
    if not (LI_MEMBER_AUDIENCE_API_ENABLED and member_post_analytics_enabled_for_user(user_id)
            and member_post_analytics_separate_app_enabled()):
        return counts
    from cqc_lem.utilities.db import get_member_analytics_token_info
    token = get_member_post_analytics_access_token(user_id)
    info = get_member_analytics_token_info(user_id) or {}
    granted = set((info.get("granted_scope") or "").replace(",", " ").split())
    if not token or not granted:
        return counts
    if FOLLOWERS_SCOPE in granted:
        counts["follower_count"] = fetch_member_follower_count(token, LI_API_VERSION)
    if CONNECTIONS_SCOPE in granted:
        counts["connection_count"] = fetch_member_connection_count(
            info.get("linked_sub_id") or "", token, LI_API_VERSION)
    return counts


def log_member_post_analytics_summary(user_id: int, posts_read: int, posts_captured: int,
                                      metrics_read: int, failures: int) -> None:
    """One INFO line per CMA post-stats sweep, so a working API read leaves evidence.

    `calls_max` is the ceiling of GETs spent (one per metric per post); a failure stops the sweep
    early, so the true count can be lower. No token or response content is ever passed in.
    """
    log_info("Member post analytics API sweep", user_id=user_id, posts_read=posts_read,
             posts_captured=posts_captured, metrics_read=metrics_read, failures=failures,
             calls_max=posts_read * len(_METRICS), task_name="auto_scrape_post_stats")


def log_member_post_analytics_failure(reason: str, user_id: int, post_id: int) -> None:
    """Record fallback reason without exposing token or response content."""
    if reason == "demo_mode":
        return
    log_warning("Member post analytics API unavailable; keeping scrape values",
                user_id=user_id, post_id=post_id, api_failure=reason,
                task_name="auto_scrape_post_stats")
