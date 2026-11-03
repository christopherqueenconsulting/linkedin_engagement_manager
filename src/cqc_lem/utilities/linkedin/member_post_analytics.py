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
from cqc_lem.utilities.logger import log_warning

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
    from cqc_lem.utilities.env_constants import LI_MEMBER_POST_ANALYTICS_MAX_POSTS_PER_SWEEP
    return LI_MEMBER_POST_ANALYTICS_MAX_POSTS_PER_SWEEP


def member_post_analytics_separate_app_enabled() -> bool:
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


def log_member_post_analytics_failure(reason: str, user_id: int, post_id: int) -> None:
    """Record fallback reason without exposing token or response content."""
    if reason == "demo_mode":
        return
    log_warning("Member post analytics API unavailable; keeping scrape values",
                user_id=user_id, post_id=post_id, api_failure=reason,
                task_name="auto_scrape_post_stats")
