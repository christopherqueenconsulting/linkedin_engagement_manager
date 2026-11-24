"""Calendly polling booking hook (#2658, Phase 2 of #2633).

User 1 is on Calendly Free, where webhooks are unavailable, so an hourly beat reads
`GET /scheduled_events` with the user's personal access token. The hook only LABELS and COUNTS: it
moves a lead's outcome stage and writes a `booking` stage event. It sends nothing, and there is no
public route. Off unless `BOOKING_HOOK_ENABLED` is set.

The token never reaches a log line: only status codes and exception TYPES are logged.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from cqc_lem.utilities import db
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

CALENDLY_API = "https://api.calendly.com"
_TIMEOUT_SECONDS = 20
_LOOKBACK_DAYS = 1
_MAX_PAGES = 3
_PAGE_SIZE = 100


def booking_hook_enabled() -> bool:
    """Read at the call site, never at import. Default OFF."""
    return (os.environ.get("BOOKING_HOOK_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


def _get(path_or_url: str, token: str, params: Optional[dict] = None) -> Optional[dict]:
    url = path_or_url if path_or_url.startswith("http") else f"{CALENDLY_API}{path_or_url}"
    if not url.startswith(f"{CALENDLY_API}/"):
        # event and pagination URLs come from the response; never send the PAT anywhere but Calendly
        log_warning("Refusing non-Calendly URL for token-bearing request")
        return None
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params,
                            timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as err:
        log_warning("Calendly request failed", error_type=type(err).__name__)
        return None
    if resp.status_code != 200:
        log_warning("Calendly request refused", status_code=resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def poll_user_bookings(user_id: int) -> dict:
    """Poll one user's recent Calendly events and apply each invitee. Returns outcome counts."""
    counts: dict = {}
    token = db.get_calendly_token(user_id)
    if not token:
        return counts
    me = _get("/users/me", token)
    user_uri = ((me or {}).get("resource") or {}).get("uri")
    if not user_uri:
        return counts
    since = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    page = _get("/scheduled_events", token, {"user": user_uri, "min_start_time": since,
                                              "count": _PAGE_SIZE, "sort": "start_time:asc"})
    for _ in range(_MAX_PAGES):
        if not page:
            break
        for event in page.get("collection") or []:
            event_uri = event.get("uri") or ""
            invitees = _get(f"{event_uri}/invitees", token) if event_uri else None
            for inv in (invitees or {}).get("collection") or []:
                canceled = (inv.get("status") == "canceled") or event.get("status") == "canceled"
                outcome = db.apply_booking(user_id, inv.get("uri") or "", event_uri,
                                           inv.get("email"), inv.get("name"), canceled)
                counts[outcome] = counts.get(outcome, 0) + 1
        next_page = (page.get("pagination") or {}).get("next_page")
        page = _get(next_page, token) if next_page else None
    db.touch_calendly_polled(user_id)
    return counts


def poll_all_bookings() -> str:
    """The beat body: a no-op unless the flag is on. Returns a one-line summary."""
    if not booking_hook_enabled():
        log_debug("Booking hook off, skipping poll", task_name="auto_poll_calendly_bookings")
        return "Booking hook disabled"
    total: dict = {}
    users = db.get_calendly_user_ids()
    for user_id in users:
        for key, n in poll_user_bookings(user_id).items():
            total[key] = total.get(key, 0) + n
    log_info("Calendly poll finished", task_name="auto_poll_calendly_bookings", users=len(users), **total)
    return f"Polled {len(users)} user(s): {total or 'nothing new'}"
