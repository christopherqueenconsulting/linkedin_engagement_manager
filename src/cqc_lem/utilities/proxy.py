"""Egress proxy resolution for the automation browser.

Zero user-side setup: a user's session is routed automatically through an egress
proxy chosen from their *already-captured* location (country), so their LinkedIn
login appears to come from near where they normally sign in — without the user
installing or configuring anything.

Resolution order (first match wins):
  1. An explicit per-user override (``users.proxy_url``) — for power users / paid proxies.
  2. A regional proxy matched by the user's country via ``REGION_PROXIES``.
  3. A global default (``PROXY_URL``).
  4. None — egress straight from the host (today's behavior).

``REGION_PROXIES`` is a JSON object mapping ISO-3166 alpha-2 country codes (and an
optional ``"DEFAULT"``) to proxy URLs, e.g.::

    REGION_PROXIES={"US":"http://us-east.proxy.internal:3128",
                    "GB":"http://eu-west.proxy.internal:3128",
                    "DEFAULT":"http://us-east.proxy.internal:3128"}

See docs/PER_USER_PROXY.md for how those regional proxies are provisioned (a few
cheap AWS/VPS egress nodes cover the whole user base — cost scales with regions,
not users).
"""

import json
import os
from typing import Optional

from cqc_lem.utilities.logger import log_info


def _region_proxies() -> dict:
    raw = (os.getenv("REGION_PROXIES") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError) as e:
        log_info(f"Invalid REGION_PROXIES JSON — ignoring: {e}")
        return {}


def resolve_proxy(explicit_proxy: Optional[str], country: Optional[str]) -> Optional[str]:
    """Return the egress proxy URL for a session, or None for direct egress."""
    if explicit_proxy:
        return explicit_proxy

    regions = _region_proxies()
    if regions:
        if country:
            match = regions.get(country.upper())
            if match:
                return match
        default = regions.get("DEFAULT")
        if default:
            return default

    return os.getenv("PROXY_URL") or None
