"""When and how much the nightly comment-candidate crawl may read (issue #2647, Phase 2).

The crawl walks the user's engagement roster and saves what it finds into `comment_candidates`
with source `crawl`, through the Phase 1 recorder, so the dedupe keys and safety gates are the
ones the feed scan already uses. It is read-only on LinkedIn.

Timing: the #2628 session-window gate (`session_window_decision`) is not on main yet, so the
window here is a fixed one: weekdays, 09:00-16:59 in the user's own timezone, which keeps every
session clear of 19:00-07:00 and of weekends. When the gate lands, `crawl_window_open` is the one
function to replace with a call to it.
"""

import os
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from cqc_lem.utilities.comment_candidates import SOURCE_CRAWL, pool_enabled
from cqc_lem.utilities.db import count_comment_candidates, get_user_timezone
from cqc_lem.utilities.flags import COMMENT_CANDIDATE_CRAWL, flag_enabled

WINDOW_FIRST_HOUR = 9
WINDOW_LAST_HOUR = 16
DEFAULT_DAILY_CAP = 200


def crawl_enabled(user_id: int) -> bool:
    """Whether the crawl may run for this user: its own flag AND the pool flag. False on a fault."""
    try:
        return bool(flag_enabled(COMMENT_CANDIDATE_CRAWL, user_id)) and pool_enabled(user_id)
    except Exception:
        return False


def crawl_window_open(tz_name: str, now: Optional[datetime] = None) -> bool:
    """True on a weekday between 09:00 and 16:59 in `tz_name`; an unknown timezone is closed."""
    try:
        local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz_name))
    except Exception:
        return False
    return local.weekday() < 5 and WINDOW_FIRST_HOUR <= local.hour <= WINDOW_LAST_HOUR


def crawl_daily_cap() -> int:
    """The per-user, per-day ceiling on crawl-sourced candidates (`COMMENT_CRAWL_DAILY_CAP`)."""
    try:
        return max(0, int(os.getenv("COMMENT_CRAWL_DAILY_CAP", DEFAULT_DAILY_CAP)))
    except ValueError:
        return DEFAULT_DAILY_CAP


def crawl_budget(user_id: int) -> int:
    """How many more crawl candidates today's cap allows; 0 when the count cannot be read."""
    used = count_comment_candidates(user_id, source=SOURCE_CRAWL)
    if used is None:
        return 0
    return max(0, crawl_daily_cap() - used)


def crawl_due(user_id: int, now: Optional[datetime] = None) -> bool:
    """Flags on, inside the user's local window — the dispatcher's whole question."""
    return crawl_enabled(user_id) and crawl_window_open(get_user_timezone(user_id), now)
