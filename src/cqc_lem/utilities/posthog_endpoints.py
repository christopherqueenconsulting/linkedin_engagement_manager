"""Runtime client for the PostHog HogQL Endpoints provisioned by scripts/posthog_provision.py
(issue #654) — the server-side half of the in-SPA "your stats" panel. PostHog is one project
shared by every LEM account, so every call is scoped to ONE user via the `distinct_id` variable
each endpoint's query was provisioned with (`{variables.distinct_id}`); nothing here ever reads or
returns another user's rows.

Never raises: a missing key, an endpoint that hasn't been provisioned yet (404) or a PostHog outage
all degrade to `available: False` for that panel, never a broken Dashboard page. The FastAPI route
(`GET /user/posthog-stats`) is the only caller — the personal API key lives here, server-side, and
never reaches the browser.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

from cqc_lem.utilities.logger import log_warning

DEFAULT_APP_HOST = "https://us.posthog.com"
DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.

# Must match scripts/posthog_provision.py's DISTINCT_ID_VARIABLE_NAME — PostHog derives the
# variable's code_name from that (already-lowercase) name unchanged.
DISTINCT_ID_VARIABLE_CODE_NAME = "distinct_id"

ENDPOINT_POSTS_ENGAGEMENT = "lem-posts-engagement-weekly"
ENDPOINT_COMMENT_ACTIVITY = "lem-comment-activity-weekly"
ENDPOINT_LLM_COST_BY_FEATURE = "lem-llm-cost-by-feature"

# Response key (stable SPA contract) -> provisioned endpoint name (PostHog implementation detail).
STATS_PANELS = {
    "posts_engagement": ENDPOINT_POSTS_ENGAGEMENT,
    "comment_activity": ENDPOINT_COMMENT_ACTIVITY,
    "llm_cost_by_feature": ENDPOINT_LLM_COST_BY_FEATURE,
}


def _client_config() -> Optional[tuple]:
    api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "")
    if not api_key:
        return None
    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST).rstrip("/")
    return api_key, project_id, app_host


def run_endpoint(name: str, user_id: int) -> Optional[dict]:
    """Execute one provisioned endpoint scoped to `user_id`'s own distinct_id (issue #654's
    `str(user_id)` convention, same as observability.py). Returns None on any failure — no key
    configured, the endpoint/variable isn't provisioned yet (404), or PostHog is unreachable — so a
    caller can treat every failure mode identically: this panel isn't available right now.
    """
    config = _client_config()
    if config is None:
        return None
    api_key, project_id, app_host = config
    url = f"{app_host}/api/projects/{project_id}/endpoints/{name}/run/"
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"variables": {DISTINCT_ID_VARIABLE_CODE_NAME: str(user_id)}},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        log_warning("PostHog endpoint call failed", exc=exc, user_id=user_id,
                    api_provider="posthog", action_type=name)
        return None


def _rows_as_dicts(payload: Optional[dict]) -> list:
    """Endpoint `/run` responses are columnar (a `columns` list + row-arrays) — reshape to the list
    of dicts the SPA and any other JSON consumer actually wants.
    """
    if not payload:
        return []
    columns = payload.get("columns") or []
    rows = payload.get("results") or []
    return [dict(zip(columns, row)) for row in rows]


def get_user_stats_panel(user_id: int) -> dict:
    """The combined panel payload for `/user/posthog-stats`: one key per panel, each with
    `available` + `rows`, so a partial PostHog outage still renders whichever panels did answer
    rather than failing the whole response.
    """
    panel = {}
    for key, endpoint_name in STATS_PANELS.items():
        payload = run_endpoint(endpoint_name, user_id)
        panel[key] = {"available": payload is not None, "rows": _rows_as_dicts(payload)}
    return panel
