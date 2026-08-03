"""Human-pacing engine — the one place engagement CADENCE is decided (issue #626).

LinkedIn's comment-bot detection keys on cadence, not content: engaging seconds after a post
appears, machine-exact daily start times, identical intervals, and a flat daily volume are all
signals no human produces. Three knobs live here, plus the governor that keeps them honest:

1. **Read-time delay** (`read_delay_seconds`) — before commenting we wait as long as it would take
   to actually READ the target post (scaled by its length) plus a little browse time, floored at
   `PACING_READ_MIN_SECONDS` and ceilinged at `PACING_READ_MAX_SECONDS`. We never interact faster
   than a human could have read the thing.
2. **Schedule jitter** (`dispatch_jitter_seconds`) — beat-dispatched engagement tasks get a random
   forward countdown instead of firing on the crontab minute. The delay is forward-only because a
   beat tick cannot dispatch into the past; the staggered per-user slot (issue #554) already
   supplies the "±" half by spreading users across a window. Replies to comments on the user's OWN
   post use the `PACE_RESPONSIVE` profile — answering fast is human, so those are jittered by
   seconds, not by an hour.
3. **Variable daily volume** (`daily_budget`) — each day's allowance is a random draw from the
   user's configured cap (40–100% by default) with weekend asymmetry and occasional rest days,
   rather than the same number every day. The draw is seeded from (user, action, day) so it is
   STABLE: a retry, a second worker, or a Redis outage all re-derive the same number instead of
   re-rolling a fresh (possibly larger) budget mid-day.

`record_action` / `remaining_actions` are the account-level governor: every engagement lane
(commenting, replies, DMs, invites) reports what it spent into one per-day counter, so the combined
traffic can't exceed the account envelope even though each lane checks its own cap separately.

Everything here fails OPEN. No Redis, or `HUMAN_PACING_ENABLED=false`, degrades to the pre-#626
behaviour (full cap, no jitter, no added delay) rather than blocking engagement. The breaker in
`utilities/linkedin/rate_limit.py` is a separate, harder gate and is unaffected — pacing only ever
makes us slower, never more aggressive.

Nothing here sleeps longer than `MAX_INLINE_SLEEP_SECONDS`: a Celery worker must never be parked on
a long sleep, so anything beyond a read delay is expressed as an `apply_async(countdown=...)`.
"""

import hashlib
import os
import random
import time
from datetime import date, datetime, timezone
from typing import Dict, Optional

from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_debug, log_warning

# Engagement lanes the governor accounts for. Each maps to one per-day cap in engagement_preferences.
ACTION_COMMENT = "comment"
ACTION_REPLY = "reply"
ACTION_DM = "dm"
ACTION_INVITE = "invite"
# Company-page invites (issue #732) draw their OWN daily budget: they run on a different, smaller cap
# (`max_company_page_invites_per_day`) than connection invites, and `daily_budget` keys its stored
# draw on the action alone — so sharing ACTION_INVITE would let whichever lane ran first that day
# write the other's budget, silently halving the connection lane whenever page invites drew first.
# It is deliberately NOT in ENVELOPE_ACTIONS / ACTION_CAP_PREF: page invites still SPEND the account
# envelope (they `record_action` under ACTION_INVITE, which is also their harder ceiling), they just
# must not enlarge it with a second cap of their own.
ACTION_COMPANY_INVITE = "company_invite"
# Affiliate promotional CONTENT (issue #770). Deliberately outside ENGAGEMENT_ACTIONS/ENVELOPE_ACTIONS:
# it is a generated POST, and posting is API-driven — it was never gated by the engagement envelope
# (the same reason #629's suppression pause covers engagement only). It draws its own daily budget
# purely so the writer inherits the rest-day and variable-volume behaviour every other lane has.
ACTION_AFFILIATE_PROMO = "affiliate_promo"
# Following a roster target (issue #962). LinkedIn rate-limits follows and bulk-following is one of
# the oldest bot signatures there is, so this lane draws its own small daily budget rather than
# riding the comment lane's. It is deliberately NOT in ENVELOPE_ACTIONS: a follow is one click on a
# page the roster pass already opened, and letting it into the envelope would ENLARGE the account's
# daily allowance by its cap (`account_envelope` sums every envelope lane's budget) — the opposite
# of what a conservative opt-in should do. The caller still passes `caps`, so an account that has
# spent its comment/DM/invite envelope stops following too: bounded by the envelope, never adding
# to it.
ACTION_FOLLOW = "follow"
ENGAGEMENT_ACTIONS = (ACTION_COMMENT, ACTION_REPLY, ACTION_DM, ACTION_INVITE)

# Which preference key caps each lane.
ACTION_CAP_PREF = {
    ACTION_COMMENT: "max_comments_per_day",
    ACTION_DM: "max_dms_per_day",
    ACTION_INVITE: "max_invites_per_day",
    ACTION_FOLLOW: "max_follows_per_day",
}

# Lanes that spend the account envelope. Replies are deliberately NOT one of them: a reply is an
# answer to someone who spoke to us on our OWN post, and going quiet on those to protect a
# commenting budget is worse for the account than the cadence risk it would avoid (the G7 golden-hour
# exemption). They are still recorded per-lane for visibility, and still jittered (PACE_RESPONSIVE) —
# they just never consume what discretionary outbound spends.
ENVELOPE_ACTIONS = (ACTION_COMMENT, ACTION_DM, ACTION_INVITE)

# Jitter profiles.
PACE_STANDARD = "standard"      # discretionary outbound — jitter by tens of minutes
PACE_RESPONSIVE = "responsive"  # answering someone on our own post — jitter by seconds

# A Celery worker parked on a sleep is a worker not doing work; long delays must be scheduled with
# countdown/eta instead. Every inline pacing sleep this module hands out is clamped below this.
MAX_INLINE_SLEEP_SECONDS = 300

_DEFAULTS = {
    "PACING_READ_MIN_SECONDS": 45.0,
    "PACING_READ_MAX_SECONDS": 240.0,
    "PACING_READ_WPM": 240.0,
    "PACING_BROWSE_MIN_SECONDS": 3.0,
    "PACING_BROWSE_MAX_SECONDS": 25.0,
    "PACING_JITTER_MIN_MINUTES": 30.0,
    "PACING_JITTER_MAX_MINUTES": 90.0,
    "PACING_RESPONSIVE_JITTER_MAX_SECONDS": 180.0,
    "PACING_BUDGET_MIN_RATIO": 0.4,
    "PACING_BUDGET_MAX_RATIO": 1.0,
    "PACING_REST_DAY_CHANCE": 0.08,
    "PACING_WEEKEND_RATIO": 0.55,
}

_BUDGET_KEY = "pacing:budget:{action}:{user_id}:{day}"
_USED_KEY = "pacing:used:{user_id}:{day}"
_USED_TOTAL_FIELD = "total"
# Long enough to outlast the day it was written on (any timezone), short enough to garbage-collect.
_DAY_TTL_SECONDS = 36 * 60 * 60


def pacing_enabled() -> bool:
    """False turns the whole engine into a no-op — full cap, no jitter, no added read delay."""
    return (os.environ.get("HUMAN_PACING_ENABLED") or "true").strip().lower() not in ("0", "false", "no")


def _env_float(name: str) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return _DEFAULTS[name]
    try:
        return float(raw)
    except ValueError:
        log_warning(f"{name} ignored (not a number: {raw!r})")
        return _DEFAULTS[name]


def _today(day: Optional[date] = None) -> date:
    return day or datetime.now(timezone.utc).date()


def _seeded_rng(*parts) -> random.Random:
    """A Random seeded from the given parts — the same (user, action, day) always draws the same
    numbers, which is what makes a day's budget survive retries and Redis outages alike."""
    salt = os.environ.get("PACING_SEED") or ""
    digest = hashlib.sha256(":".join([salt] + [str(p) for p in parts]).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# --- 1. read-time delay ------------------------------------------------------------------------

def read_delay_seconds(content: str, rng: Optional[random.Random] = None) -> float:
    """How long to wait before engaging with a post of this length, in seconds.

    Reading time at `PACING_READ_WPM` plus a random browse/scroll allowance, clamped into
    [`PACING_READ_MIN_SECONDS`, `PACING_READ_MAX_SECONDS`]. The floor is the point: a 12-word post
    still costs 45s, because commenting three seconds after a post rendered is the single loudest
    bot tell. Returns 0.0 when pacing is disabled.
    """
    if not pacing_enabled():
        return 0.0
    rng = rng or random
    words = len((content or "").split())
    wpm = max(1.0, _env_float("PACING_READ_WPM"))
    reading = words / wpm * 60.0
    browse = rng.uniform(_env_float("PACING_BROWSE_MIN_SECONDS"), _env_float("PACING_BROWSE_MAX_SECONDS"))
    low = max(0.0, _env_float("PACING_READ_MIN_SECONDS"))
    # The ceiling also keeps this sleep inline-safe: a worker never parks past MAX_INLINE_SLEEP_SECONDS.
    high = min(float(MAX_INLINE_SLEEP_SECONDS), max(low, _env_float("PACING_READ_MAX_SECONDS")))
    return min(high, max(low, reading + browse))


def pace_read(content: str, user_id: Optional[int] = None) -> float:
    """Sleep for `read_delay_seconds(content)` and return the delay actually used."""
    delay = read_delay_seconds(content)
    if delay <= 0:
        return 0.0
    log_debug(f"Human-pacing read delay {delay:.0f}s before engaging", user_id=user_id,
              action_type="comment", duration_ms=int(delay * 1000))
    time.sleep(delay)
    return delay


# --- 2. schedule jitter ------------------------------------------------------------------------

def dispatch_jitter_seconds(profile: str = PACE_STANDARD,
                            rng: Optional[random.Random] = None) -> int:
    """A countdown (seconds) to hand `apply_async` so a beat-dispatched engagement task doesn't
    start on the crontab's exact minute. `PACE_RESPONSIVE` keeps replies on our own post fast
    (seconds of jitter) while still breaking the machine-exact timing. 0 when pacing is disabled."""
    if not pacing_enabled():
        return 0
    rng = rng or random
    if profile == PACE_RESPONSIVE:
        return int(rng.uniform(0.0, max(0.0, _env_float("PACING_RESPONSIVE_JITTER_MAX_SECONDS"))))
    low = max(0.0, _env_float("PACING_JITTER_MIN_MINUTES")) * 60.0
    high = max(low, _env_float("PACING_JITTER_MAX_MINUTES") * 60.0)
    return int(rng.uniform(low, high))


# --- 3. variable daily volume ------------------------------------------------------------------

def is_rest_day(user_id: int, day: Optional[date] = None) -> bool:
    """True on the occasional day this account does no discretionary engagement at all. Account-level
    (not per-lane) — a human who is offline is offline for everything."""
    if not pacing_enabled():
        return False
    chance = min(1.0, max(0.0, _env_float("PACING_REST_DAY_CHANCE")))
    return _seeded_rng("rest", user_id, _today(day).isoformat()).random() < chance


def _draw_budget(user_id: int, action: str, cap: int, day: date) -> int:
    rng = _seeded_rng("budget", user_id, action, day.isoformat())
    low = max(0.0, _env_float("PACING_BUDGET_MIN_RATIO"))
    high = max(low, _env_float("PACING_BUDGET_MAX_RATIO"))
    ratio = rng.uniform(low, high)
    if day.weekday() >= 5:  # Sat/Sun — real people post and comment less on weekends
        ratio *= max(0.0, _env_float("PACING_WEEKEND_RATIO"))
    if is_rest_day(user_id, day):
        return 0
    # A non-zero cap always keeps at least one action: the draw shapes volume, it must not silently
    # zero out a lane the user explicitly enabled (that is what rest days are for).
    return max(1, int(round(cap * ratio)))


def daily_budget(user_id: int, action: str, cap: int, day: Optional[date] = None) -> int:
    """Today's allowance for one engagement lane — a stable random draw from `cap`.

    Persisted in Redis for the day so a mid-day cap edit (or a re-run) doesn't change the number
    already being spent against; the draw is seeded anyway, so with no Redis the same value is
    re-derived rather than re-rolled. Returns `cap` unchanged when pacing is disabled.

    The stored value is still clamped to the CURRENT cap on the way out: stability must never make
    us more aggressive than the user's own setting, so lowering a cap mid-day (the one edit someone
    makes because they're worried about the account) takes effect immediately.
    """
    cap = max(0, int(cap or 0))
    if not pacing_enabled() or cap == 0:
        return cap
    day = _today(day)
    key = _BUDGET_KEY.format(action=action, user_id=int(user_id), day=day.isoformat())
    client = shared_redis_client()
    if client is not None:
        try:
            stored = client.get(key)
            if stored is not None:
                return max(0, min(cap, int(stored)))
        except (ValueError, TypeError):
            pass  # corrupt value — fall through and re-derive
        except Exception as e:
            log_warning("Could not read today's pacing budget", exc=e, user_id=user_id)
            client = None
    budget = _draw_budget(user_id, action, cap, day)
    if client is not None:
        try:
            client.set(key, budget, ex=_DAY_TTL_SECONDS)
        except Exception as e:
            log_warning("Could not persist today's pacing budget", exc=e, user_id=user_id)
    return budget


def engagement_caps_from_prefs(prefs: Optional[dict]) -> Dict[str, int]:
    """The per-lane caps the governor's envelope is built from, read off engagement_preferences.

    Only ENVELOPE_ACTIONS appear — replies are outside the envelope on purpose (see above)."""
    prefs = prefs or {}
    caps: Dict[str, int] = {}
    for action in ENVELOPE_ACTIONS:
        try:
            caps[action] = max(0, int(prefs.get(ACTION_CAP_PREF[action]) or 0))
        except (TypeError, ValueError):
            caps[action] = 0
    return caps


# --- the account-level governor ----------------------------------------------------------------

def record_action(user_id: int, action: str, count: int = 1) -> None:
    """Report one completed engagement action into the shared per-day counter. Best-effort.

    Only ENVELOPE_ACTIONS add to the account total; a reply is counted in its own field so the day
    can still be audited, without letting a busy thread on the user's own post eat the outbound
    budget."""
    if count <= 0:
        return
    client = shared_redis_client()
    if client is None:
        return
    key = _USED_KEY.format(user_id=int(user_id), day=_today().isoformat())
    try:
        client.hincrby(key, action, int(count))
        if action in ENVELOPE_ACTIONS:
            client.hincrby(key, _USED_TOTAL_FIELD, int(count))
        client.expire(key, _DAY_TTL_SECONDS)
    except Exception as e:
        log_warning("Could not record engagement action for pacing", exc=e, user_id=user_id)


def actions_used_today(user_id: int, action: Optional[str] = None,
                       day: Optional[date] = None) -> int:
    """Actions this account has spent today across every lane (or in one lane when `action` is
    given). 0 when Redis is unavailable — the governor then can't restrict anything, which is the
    intended fail-open."""
    client = shared_redis_client()
    if client is None:
        return 0
    key = _USED_KEY.format(user_id=int(user_id), day=_today(day).isoformat())
    try:
        raw = client.hget(key, action or _USED_TOTAL_FIELD)
    except Exception as e:
        log_warning("Could not read today's pacing usage", exc=e, user_id=user_id)
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def account_envelope(user_id: int, caps: Dict[str, int], day: Optional[date] = None) -> int:
    """The account's total engagement allowance today — the sum of every lane's paced budget. This
    is what stops commenting, DMs and invites from each spending a full cap on the same day."""
    return sum(daily_budget(user_id, action, cap, day) for action, cap in (caps or {}).items())


def remaining_actions(user_id: int, action: str, cap: int, used_today: int,
                      caps: Optional[Dict[str, int]] = None, day: Optional[date] = None) -> int:
    """How many more `action`s this account may take right now.

    The smaller of what the lane has left (its paced budget minus what the DB says it already spent)
    and what the account envelope has left across every lane. Pass `caps` (from
    `engagement_caps_from_prefs`) to engage the account-level governor; without it the envelope is
    just this lane and the call degrades to the per-lane budget.

    With pacing disabled the envelope is skipped entirely, not just softened: the whole point of the
    switch is that it restores the pre-#626 behaviour, and pre-#626 each lane spent its own cap with
    no shared pool at all.
    """
    lane_left = max(0, daily_budget(user_id, action, cap, day) - max(0, int(used_today or 0)))
    if not caps or not pacing_enabled():
        return lane_left
    envelope_left = max(0, account_envelope(user_id, caps, day) - actions_used_today(user_id, day=day))
    return min(lane_left, envelope_left)
