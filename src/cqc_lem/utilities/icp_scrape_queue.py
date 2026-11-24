"""Hold-for-scrape queue for the ICP rubric (issue #2655, Phase 2 of #2630).

A connection candidate with NO scraped facts is never scored blind and never silently excluded: the
scan parks its profile URL here, and a paced task scrapes a few per day through the existing profile
scrape path. The next scan finds the facts in `profiles` and scores the person.

State lives in Redis (runtime state, like the 429 breaker), so it survives deploys. The queue fails
CLOSED: no Redis means nothing is queued and nothing is scraped — a scrape budget that cannot be
counted is never spent.

The window is deliberately fixed: weekday daytime in the user's local time, never 19:00-07:00 and
never a weekend. It stands in for #2628's session windows until `session_window_decision` lands.
"""

import os
from datetime import datetime, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cqc_lem.utilities.lead_scoring import profile_slug
from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_warning

WINDOW_START_HOUR = 9
WINDOW_END_HOUR = 17  # exclusive, so the last scrape starts before 17:00
DEFAULT_DAILY_CAP = 10
_QUEUE_TTL_SECONDS = 14 * 24 * 60 * 60
_ATTEMPT_TTL_SECONDS = 7 * 24 * 60 * 60
_CAP_TTL_SECONDS = 2 * 24 * 60 * 60

_QUEUE_PREFIX = "icp:scrape_queue:"
_ATTEMPT_PREFIX = "icp:scrape_attempt:"
_CAP_PREFIX = "icp:scrape_budget:"


def daily_cap() -> int:
    """Profiles scraped per user per day, from `ICP_SCRAPE_DAILY_CAP` (0 turns the queue off)."""
    try:
        return max(0, min(50, int(os.getenv("ICP_SCRAPE_DAILY_CAP", DEFAULT_DAILY_CAP))))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


def _local(now: datetime, tz_name: Optional[str]) -> datetime:
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    try:
        return now.astimezone(ZoneInfo(tz_name)) if tz_name else now.astimezone(timezone.utc)
    except (ZoneInfoNotFoundError, ValueError):
        return now.astimezone(timezone.utc)


def in_scrape_window(now: datetime, tz_name: Optional[str] = None) -> bool:
    """True on a weekday between 09:00 and 17:00 local time."""
    local = _local(now, tz_name)
    return local.weekday() < 5 and WINDOW_START_HOUR <= local.hour < WINDOW_END_HOUR


def _queue_key(user_id: int) -> str:
    return f"{_QUEUE_PREFIX}{int(user_id)}"


def _attempt_key(user_id: int, slug: str) -> str:
    return f"{_ATTEMPT_PREFIX}{int(user_id)}:{slug}"


def enqueue_held(user_id: int, profile_urls: Iterable[str]) -> int:
    """Park URLs for a scrape, skipping anyone already attempted this week. Returns how many queued."""
    client = shared_redis_client()
    if client is None:
        return 0
    queued = 0
    try:
        for url in dict.fromkeys(u for u in profile_urls if u):
            slug = profile_slug(url)
            if not slug or client.exists(_attempt_key(user_id, slug)):
                continue
            queued += int(client.sadd(_queue_key(user_id), url) or 0)
        if queued:
            client.expire(_queue_key(user_id), _QUEUE_TTL_SECONDS)
    except Exception as e:
        log_warning("Could not queue held ICP candidates", exc=e, user_id=user_id)
    return queued


def claim_batch(user_id: int, now: datetime, cap: Optional[int] = None) -> list:
    """Pop up to the day's remaining budget of queued URLs. Empty when Redis is down or the cap is spent.

    The budget is claimed BEFORE the pop and counts the URLs actually handed out, so a crashed task
    cannot scrape more than the cap on a retry.
    """
    limit = daily_cap() if cap is None else max(0, int(cap))
    client = shared_redis_client()
    if client is None or limit <= 0:
        return []
    day = now.strftime("%Y%m%d")
    budget_key = f"{_CAP_PREFIX}{int(user_id)}:{day}"
    try:
        remaining = limit - int(client.get(budget_key) or 0)
        if remaining <= 0:
            return []
        batch = []
        for _ in range(remaining):
            raw = client.spop(_queue_key(user_id))
            if raw is None:
                break
            batch.append(raw.decode() if isinstance(raw, bytes) else str(raw))
        if batch:
            client.incrby(budget_key, len(batch))
            client.expire(budget_key, _CAP_TTL_SECONDS)
            for url in batch:
                client.set(_attempt_key(user_id, profile_slug(url)), "1", ex=_ATTEMPT_TTL_SECONDS)
        return batch
    except Exception as e:
        log_warning("Could not claim an ICP scrape batch", exc=e, user_id=user_id)
        return []


def requeue(user_id: int, profile_urls: Iterable[str]) -> None:
    """Give back URLs a run did not reach (a rate-limited page stops the batch), refunding the budget."""
    client = shared_redis_client()
    urls = [u for u in profile_urls if u]
    if client is None or not urls:
        return
    try:
        for url in urls:
            client.delete(_attempt_key(user_id, profile_slug(url)))
            client.sadd(_queue_key(user_id), url)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        client.decrby(f"{_CAP_PREFIX}{int(user_id)}:{day}", len(urls))
    except Exception as e:
        log_warning("Could not requeue ICP scrape URLs", exc=e, user_id=user_id)
