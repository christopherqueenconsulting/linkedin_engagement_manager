"""Soft daily AI spend cap for `free_trial` users (issue #2378).

A trial account's AI spend is accrued per UTC day by `observability.track_llm_call` and
`observability.track_media_cost` (the same attribution that feeds `cost_ledger`; local `local-*`
compute does not count). Once a trial user's spend for the current UTC day reaches
`FREE_TRIAL_DAILY_AI_CAP_USD`, NEW generation for that user pauses until the next UTC day.

What pauses and what does not (full posture: `docs/llm-analytics.md`, "Free-trial daily AI cap"):

* **Paused (non-essential):** starting any new generation — the background content plan, variants,
  comment drafting, newsletter editions, and any other chat or image call attributed to the user.
  In a Celery task the run ends as `no_op` (never FAILURE) and the next scheduled run after
  00:00 UTC generates again; in an API request the caller gets a 429 "daily AI limit reached".
* **Never paused (essential):**
    - a pipeline ADMITTED under the cap runs to completion, so a post never lands half-generated
      (the cap is soft, and the spend already sunk into its first steps would be wasted);
    - `lem-embedding` and `lem-vision` calls — dedup vectors and the quality check on a render that
      is already paid for. Refusing them saves nothing and degrades a gate;
    - calls with no attributed user (system work) and every user not on the `free_trial` tier.

Soft by construction: an unreadable spend (no Redis, a Redis error) or an unreadable tier never
blocks — the spend case is warned ONCE per UTC day per worker process, not per call.
"""

import contextvars
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from cqc_lem.utilities.logger import log_debug, log_info, log_warning

CAP_ENV = "FREE_TRIAL_DAILY_AI_CAP_USD"
DEFAULT_CAP_USD = 0.50
CAPPED_TIER = "free_trial"

# Tiers whose calls are essential (see module docstring) and are never refused.
EXEMPT_MODEL_TIERS = frozenset({"lem-embedding", "lem-vision"})

STATUS_EXEMPT = "exempt"
STATUS_UNDER = "under"
STATUS_REACHED = "reached"
STATUS_UNKNOWN = "unknown"

SURFACE_BACKGROUND = "background"
SURFACE_USER_REQUEST = "user_request"

_SPEND_KEY_PREFIX = "lem:ai_spend:user:"
_REACHED_LATCH_PREFIX = "lem:ai_spend:reached:"
# Two days, so a key written just before midnight is still readable by a late reconciliation and
# never outlives the window it measures by much.
_KEY_TTL_SECONDS = 2 * 24 * 60 * 60
# The tier changes on a Stripe webhook, not per call; a short cache keeps a DB read off the path of
# every LLM request without holding a stale tier past an upgrade for long.
_TIER_CACHE_TTL_SECONDS = 300.0
# A FAILED read is cached too, but briefly: otherwise a DB fault adds a DB read to every LLM call
# while it lasts, on the path it is already slowing down.
_TIER_FAILURE_TTL_SECONDS = 30.0

#: user_id -> (monotonic expiry, tier or None for an unreadable read).
_TIER_CACHE: dict[int, tuple[float, Optional[str]]] = {}
#: UTC days an unreadable spend has already been warned about in this process.
_UNKNOWN_WARNED: set[str] = set()
#: Fallback (user, day) latch for the reached event when Redis cannot take the SET NX.
_REACHED_LOCAL: set[tuple[int, str]] = set()
#: Unparseable cap values already warned about, so a typo in `.env` says so once.
_BAD_CAP_WARNED: set[str] = set()

_pipeline_admitted: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ai_spend_cap_pipeline_admitted", default=False)


class DailyAICapReached(Exception):
    """A free-trial user's AI spend for today has reached the cap; new generation is paused.

    `expected_refusal` tells error tracking this is a designed answer, not a defect — the same
    reason an `HTTPException` is never captured (`observability.capture_exception`).
    """

    expected_refusal = True

    def __init__(self, user_id: int, spend_usd: float, cap_usd: float, resets_at: datetime) -> None:
        """Carry the reading that refused the call and when the cap resets."""
        self.user_id = user_id
        self.spend_usd = spend_usd
        self.cap_usd = cap_usd
        self.resets_at = resets_at
        super().__init__(user_message(resets_at))


def user_message(resets_at: datetime) -> str:
    """The text a user-initiated request sees when the cap refuses it."""
    return ("Daily AI limit reached for your free trial. AI generation resumes at "
            f"{resets_at.strftime('%H:%M')} UTC.")


def _now(now: Optional[datetime] = None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def utc_day(now: Optional[datetime] = None) -> str:
    """The UTC calendar day (`YYYY-MM-DD`) a spend reading belongs to."""
    return _now(now).astimezone(timezone.utc).date().isoformat()


def next_reset(now: Optional[datetime] = None) -> datetime:
    """The next UTC midnight — when a paused user's generation resumes."""
    today = _now(now).astimezone(timezone.utc).date()
    return datetime(today.year, today.month, today.day, tzinfo=timezone.utc) + timedelta(days=1)


def daily_cap_usd() -> Optional[float]:
    """The cap read at CALL time, or None when the cap is turned off (a value of 0 or below).

    An unparseable value falls back to the default rather than turning the cap off: a typo in `.env`
    must not silently remove a cost control.
    """
    raw = os.getenv(CAP_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_CAP_USD
    try:
        value = float(raw)
    except (TypeError, ValueError):
        if raw not in _BAD_CAP_WARNED:
            _BAD_CAP_WARNED.add(raw)
            log_warning(f"{CAP_ENV} is not a number; using the default cap", api_provider="litellm")
        return DEFAULT_CAP_USD
    return value if value > 0 else None


def _spend_key(user_id: int, day: str) -> str:
    return f"{_SPEND_KEY_PREFIX}{day}:{user_id}"


def _redis():
    from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
    return shared_redis_client()


def _as_user_id(user_id: object) -> Optional[int]:
    """An attributed user id as an int, or None for no user / the system sentinel / junk."""
    if user_id is None or isinstance(user_id, bool):
        return None
    try:
        return int(str(user_id).strip())
    except (TypeError, ValueError):
        return None


def record_user_spend(user_id: object, usd: float, client: object = None,
                      now: Optional[datetime] = None) -> None:
    """Add one tracked call's spend to the user's per-UTC-day counter.

    Best-effort and silent: a missed increment costs the cap precision, and the per-call
    `llm_call` event still has it.
    """
    uid = _as_user_id(user_id)
    if uid is None or not usd or usd <= 0:
        return
    try:
        client = client if client is not None else _redis()
        if client is None:
            return
        key = _spend_key(uid, utc_day(now))
        client.incrbyfloat(key, float(usd))
        client.expire(key, _KEY_TTL_SECONDS)
    except Exception as exc:
        log_debug(f"Could not record per-user AI spend: {exc}", user_id=uid)


def _read_spend(user_id: int, day: str) -> Optional[float]:
    """Today's recorded spend, 0.0 when nothing is recorded yet, None when it cannot be read."""
    try:
        client = _redis()
        if client is None:
            return None
        raw = client.get(_spend_key(user_id, day))
    except Exception:
        return None
    if raw is None:
        return 0.0
    try:
        return float(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None


def _user_tier(user_id: int) -> Optional[str]:
    """The user's `subscription_tier`, cached for 5 minutes; None when unreadable (cached 30s)."""
    now = time.monotonic()
    cached = _TIER_CACHE.get(user_id)
    if cached and now < cached[0]:
        return cached[1]
    try:
        from cqc_lem.utilities.db import get_user_subscription_info
        info = get_user_subscription_info(user_id)
    except Exception:
        info = None
    if info is None:
        _TIER_CACHE[user_id] = (now + _TIER_FAILURE_TTL_SECONDS, None)
        return None
    tier = str(info.get("subscription_tier") or "")
    _TIER_CACHE[user_id] = (now + _TIER_CACHE_TTL_SECONDS, tier)
    return tier


def cap_status(user_id: object, now: Optional[datetime] = None) -> tuple[str, Optional[float], Optional[float]]:
    """`(status, spend_usd, cap_usd)` for the user right now.

    Status is one of `exempt`, `under`, `reached` or `unknown`; only `reached` ever pauses anything.
    """
    uid = _as_user_id(user_id)
    if uid is None:
        return STATUS_EXEMPT, None, None
    cap = daily_cap_usd()
    if cap is None:
        return STATUS_EXEMPT, None, None
    # An unreadable tier is not evidence of a trial, so it reads as exempt — the soft direction.
    if _user_tier(uid) != CAPPED_TIER:
        return STATUS_EXEMPT, None, cap
    day = utc_day(now)
    spend = _read_spend(uid, day)
    if spend is None:
        if day not in _UNKNOWN_WARNED:
            _UNKNOWN_WARNED.add(day)
            log_warning("Free-trial AI spend is unreadable; the daily cap is not enforced until it "
                        "can be read", user_id=uid, api_provider="litellm")
        return STATUS_UNKNOWN, None, cap
    return (STATUS_REACHED if spend >= cap else STATUS_UNDER), spend, cap


def _first_reach_today(user_id: int, day: str) -> bool:
    """True exactly once per (user, UTC day) across processes.

    So the INFO line and the event record the transition, not every refused call after it.
    """
    try:
        client = _redis()
        if client is not None:
            return bool(client.set(f"{_REACHED_LATCH_PREFIX}{day}:{user_id}", "1",
                                   nx=True, ex=_KEY_TTL_SECONDS))
    except Exception as exc:
        log_debug(f"Could not set the AI cap latch in Redis: {exc}", user_id=user_id)
    if (user_id, day) in _REACHED_LOCAL:
        return False
    _REACHED_LOCAL.add((user_id, day))
    return True


def _current_surface() -> str:
    """`background` inside a Celery task, `user_request` otherwise (an API request or a CLI)."""
    try:
        from celery import current_task
        if getattr(current_task, "name", None):
            return SURFACE_BACKGROUND
    except Exception:
        # Celery not importable or no task context: treat the call as a user request.
        pass
    return SURFACE_USER_REQUEST


def _paused_reading(user_id: object, surface: Optional[str],
                    now: Optional[datetime]) -> Optional[tuple[float, float]]:
    """`(spend_usd, cap_usd)` when the user is paused, else None. Records the transition once."""
    status, spend, cap = cap_status(user_id, now)
    if status != STATUS_REACHED:
        return None
    uid = _as_user_id(user_id)
    day = utc_day(now)
    if _first_reach_today(uid, day):
        log_info(f"Free-trial daily AI cap reached; non-essential generation paused until "
                 f"{next_reset(now).isoformat()}", user_id=uid)
        try:
            from cqc_lem.utilities.observability import track_ai_daily_cap_reached
            track_ai_daily_cap_reached(user_id=uid, spend_usd=spend, cap_usd=cap, day=day,
                                       surface=surface or _current_surface())
        except Exception as exc:
            log_debug(f"Could not emit ai_daily_cap_reached: {exc}", user_id=uid)
    else:
        log_debug("Free-trial daily AI cap: generation still paused", user_id=uid)
    return float(spend or 0.0), float(cap or 0.0)


def generation_paused(user_id: object, surface: Optional[str] = None,
                      now: Optional[datetime] = None) -> bool:
    """True when this user's non-essential generation is paused for the rest of the UTC day.

    The first refusal of the day logs INFO and emits `ai_daily_cap_reached`; later ones are DEBUG,
    because a paused user being refused again is the expected no-op, not news.
    """
    return _paused_reading(user_id, surface, now) is not None


def enforce(user_id: object, surface: Optional[str] = None, now: Optional[datetime] = None) -> None:
    """Raise `DailyAICapReached` when this user's generation is paused; otherwise return."""
    reading = _paused_reading(user_id, surface, now)
    if reading is not None:
        raise DailyAICapReached(_as_user_id(user_id), reading[0], reading[1], next_reset(now))


def pipeline_admitted() -> bool:
    """True inside a pipeline that was admitted under the cap — its remaining calls are essential."""
    return _pipeline_admitted.get()


@contextmanager
def admit_pipeline(user_id: object) -> Iterator[None]:
    """Gate a pipeline at its ENTRY.

    Refuse it when the user is paused; otherwise let every call inside it finish even if the cap is
    crossed part-way.
    """
    if not _pipeline_admitted.get():
        enforce(user_id)
    token = _pipeline_admitted.set(True)
    try:
        yield
    finally:
        _pipeline_admitted.reset(token)


@contextmanager
def operator_action() -> Iterator[None]:
    """An operator action (an admin-secret route) is never refused, though its spend still counts.

    The admin routes act ON a user's post with the owner's credential, not the user's. Refusing them
    would stop the operator repairing a capped trial user's content, and the classifier an admin
    approval runs is attributed to the feedback's SUBMITTER — so a submitter at their cap would turn
    the admin's approve into `filed: false`.
    """
    token = _pipeline_admitted.set(True)
    try:
        yield
    finally:
        _pipeline_admitted.reset(token)


def check_request(user_id: object, model: Optional[str]) -> None:
    """The per-request gate `AttributedOpenAI` runs before anything is sent."""
    if pipeline_admitted():
        return
    if model and str(model) in EXEMPT_MODEL_TIERS:
        return
    enforce(user_id)
