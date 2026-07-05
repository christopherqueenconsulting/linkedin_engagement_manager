"""Newsletter scheduling math (pure, testable) — computes when the next edition should publish."""

from datetime import datetime, timedelta

import pytz

_CADENCE_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30}

# How many days before the scheduled slot a draft is generated for review.
GENERATE_LEAD_DAYS = 3


def _as_utc(dt: datetime):
    if dt is None:
        return None
    return pytz.utc.localize(dt) if dt.tzinfo is None else dt.astimezone(pytz.utc)


def next_publish_datetime(publish_day: int, publish_hour: int, cadence: str,
                          last_published_at: datetime, tz, now_utc: datetime) -> datetime:
    """Next UTC datetime (naive) the newsletter should publish.

    - publish_day: 0=Mon .. 6=Sun; publish_hour: 0-23, interpreted in the user's local `tz`.
    - Respects cadence: the slot must be >= last_published_at + interval (7/14/30 days), and >= now.
    - `tz` is a pytz timezone; DST-safe via localize() on a naive local wall-clock time.
    """
    utc = pytz.utc
    now_utc = _as_utc(now_utc)
    interval = _CADENCE_DAYS.get(cadence, 7)
    if last_published_at is not None:
        earliest = max(now_utc, _as_utc(last_published_at) + timedelta(days=interval))
    else:
        earliest = now_utc

    local_earliest = earliest.astimezone(tz)
    d = local_earliest.date()
    days_ahead = (int(publish_day) - d.weekday()) % 7
    cand_date = d + timedelta(days=days_ahead)
    local_dt = tz.localize(datetime(cand_date.year, cand_date.month, cand_date.day, int(publish_hour), 0, 0))
    if local_dt < local_earliest:
        # the slot already passed today/this week → next weekly occurrence
        local_dt = tz.localize(datetime(cand_date.year, cand_date.month, cand_date.day,
                                        int(publish_hour), 0, 0) + timedelta(days=7))
    return local_dt.astimezone(utc).replace(tzinfo=None)


def should_generate_now(next_publish: datetime, now_utc: datetime,
                        lead_days: int = GENERATE_LEAD_DAYS) -> bool:
    """True when we're within the lead window of the next slot (time to generate the draft)."""
    if next_publish is None:
        return False
    return _as_utc(now_utc) >= _as_utc(next_publish) - timedelta(days=lead_days)
