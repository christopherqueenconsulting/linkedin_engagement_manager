"""Comment-volume ramp state machine, 20 -> 40 -> 70 -> 100 a day (issue #2647, Phase 5).

The step is the ONLY thing that lifts the brand account's comment ceiling above
`BRAND_CAP_CEILINGS` (`brand_account.brand_cap_ceiling`); a raw preference never does. State lives in
Redis next to the other runtime limiters and the whole machine is OFF unless `COMMENT_RAMP_ENABLED`.

Asymmetric on purpose: a breach steps DOWN within the run that sees it, while stepping UP needs
every gate to be positively known and the account to have dwelt a full week at its step. Anything
unreadable — Redis, the outcome rows, the 429 memory — leaves the step where it is.
"""

import time
from typing import Any, Optional

from cqc_lem.utilities.logger import log_debug, log_info, log_warning

STEPS = (20, 40, 70, 100)
# The hard server-side clamp every user's `max_comments_per_day` is held under, ramp or not.
HARD_MAX_COMMENTS_PER_DAY = STEPS[-1]
DWELL_SECONDS = 7 * 24 * 3600
RATE_LIMIT_LOOKBACK_SECONDS = 7 * 24 * 3600
MIN_REPLY_SAMPLE = 10

_STATE_KEY = "comment_ramp:{user_id}"

MOVE_HOLD = "hold"
MOVE_UP = "up"
MOVE_DOWN = "down"
MOVE_UNKNOWN = "unknown"
MOVE_DISABLED = "disabled"


def clamp_comments_per_day(value: Any) -> int:
    """`value` as a whole number held within 0..`HARD_MAX_COMMENTS_PER_DAY`.

    An unparseable value falls to the first step rather than to the ceiling.
    """
    try:
        return max(0, min(HARD_MAX_COMMENTS_PER_DAY, int(value)))
    except (TypeError, ValueError):
        return STEPS[0]


def ramp_enabled(user_id: Optional[int] = None) -> bool:
    """Whether the ramp flag is on (read at the call site, never at import)."""
    from cqc_lem.utilities.flags import COMMENT_RAMP, flag_enabled
    return flag_enabled(COMMENT_RAMP, user_id)


def _client():
    from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
    return shared_redis_client()


def read_state(user_id: int) -> Optional[dict]:
    """The stored state `{step, since, baseline}`, or None when Redis is unreadable.

    A user with no stored state reads as the first step entered now — never a higher one.
    """
    client = _client()
    if client is None:
        return None
    try:
        raw = client.hgetall(_STATE_KEY.format(user_id=int(user_id)))
    except Exception:
        return None
    fields = {(k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
              for k, v in (raw or {}).items()}
    try:
        step = int(fields["step"])
        since = float(fields["since"])
    except (KeyError, ValueError):
        return {"step": STEPS[0], "since": None, "baseline": None}
    if step not in STEPS:
        return {"step": STEPS[0], "since": None, "baseline": None}
    try:
        baseline = float(fields["baseline"]) if fields.get("baseline") not in (None, "") else None
    except ValueError:
        baseline = None
    return {"step": step, "since": since, "baseline": baseline}


def _write_state(user_id: int, step: int, baseline: Optional[float], now: float) -> bool:
    client = _client()
    if client is None:
        return False
    try:
        client.hset(_STATE_KEY.format(user_id=int(user_id)),
                    mapping={"step": step, "since": now,
                             "baseline": "" if baseline is None else repr(baseline)})
    except Exception:
        return False
    return True


def current_step(user_id: int) -> int:
    """The user's ramp step; the first step whenever the state cannot be read."""
    state = read_state(user_id)
    return state["step"] if state else STEPS[0]


def ramp_comment_ceiling(user_id: int) -> int:
    """The comment ceiling the ramp grants this user.

    The first step when the flag is off or the state is unreadable.
    """
    if not ramp_enabled(user_id):
        return STEPS[0]
    return current_step(user_id)


def read_signals(user_id: int, now: float) -> dict:
    """Gather the gate readings. Each is None when it cannot be read — never a default.

    Returns:
        `reply_rate` (author-reply rate over the v2-reader rows), `demotion` (the quality verdict
        status), `rate_limited` (a 429 inside the lookback), `tripped` (suppression tripwire).
    """
    from cqc_lem.utilities.comment_outcomes import quality_verdict, summarize_outcomes
    from cqc_lem.utilities.db import get_comment_outcomes
    from cqc_lem.utilities.linkedin.rate_limit import (
        is_suppression_tripped,
        seconds_since_last_rate_limit,
        shared_redis_client,
    )

    signals: dict = {"reply_rate": None, "reply_sample": 0, "demotion": None,
                     "rate_limited": None, "tripped": None}
    try:
        rows = get_comment_outcomes(user_id, days=7)
    except Exception as exc:
        log_debug(f"Comment ramp could not read outcomes: {exc}", user_id=user_id)
        rows = None
    if rows:
        summary = summarize_outcomes(rows)
        signals["reply_sample"] = int(summary.get("reply_scored") or 0)
        if signals["reply_sample"] >= MIN_REPLY_SAMPLE:
            signals["reply_rate"] = summary.get("author_reply_rate")
        signals["demotion"] = quality_verdict(summary).get("status")
    if shared_redis_client() is not None:
        since = seconds_since_last_rate_limit()
        signals["rate_limited"] = since is not None and since < RATE_LIMIT_LOOKBACK_SECONDS
        signals["tripped"] = bool(is_suppression_tripped(user_id))
    return signals


def decide_move(state: dict, signals: dict, now: float) -> tuple[str, str]:
    """Pure transition: `(move, reason)` for a state and the gate readings.

    A breach (demotion HOLD, a 429 inside the lookback, a tripped tripwire) steps down, and wins
    over everything else. A step up needs a week at the step AND every gate positively known and
    clear AND an author-reply rate that did not fall below the one recorded when the step began.
    """
    step = state["step"]
    if signals.get("demotion") == "hold":
        breach = "demotion HOLD"
    elif signals.get("rate_limited"):
        breach = "429 in the last 7 days"
    elif signals.get("tripped"):
        breach = "suppression tripwire"
    else:
        breach = ""
    if breach:
        if step == STEPS[0]:
            return MOVE_HOLD, f"breach at the first step ({breach})"
        return MOVE_DOWN, breach
    if step == STEPS[-1]:
        return MOVE_HOLD, "at the top step"
    since = state.get("since")
    if since is None or now - since < DWELL_SECONDS:
        return MOVE_HOLD, "less than 7 days at this step"
    unknown = [name for name, value in (("reply rate", signals.get("reply_rate")),
                                        ("demotion verdict", signals.get("demotion")),
                                        ("429 history", signals.get("rate_limited")),
                                        ("tripwire", signals.get("tripped"))) if value is None]
    if unknown:
        return MOVE_UNKNOWN, "unreadable: " + ", ".join(unknown)
    if signals["demotion"] != "ok":
        return MOVE_HOLD, f"demotion verdict is {signals['demotion']}"
    baseline = state.get("baseline")
    if baseline is not None and signals["reply_rate"] < baseline:
        return MOVE_HOLD, "author-reply rate fell"
    return MOVE_UP, "all gates clear"


def evaluate_ramp(user_id: int, now: Optional[float] = None) -> dict:
    """Run one ramp evaluation for `user_id` and persist the resulting step.

    Returns:
        `{move, step, reason}`. `move` is `disabled` with the flag off, `unknown` when the state or
        a gate could not be read (the step is left exactly as it was).
    """
    now = time.time() if now is None else now
    if not ramp_enabled(user_id):
        return {"move": MOVE_DISABLED, "step": STEPS[0], "reason": "flag off"}
    state = read_state(user_id)
    if state is None:
        log_debug("Comment ramp state unreadable — holding the current step", user_id=user_id)
        return {"move": MOVE_UNKNOWN, "step": STEPS[0], "reason": "ramp state unreadable"}
    signals = read_signals(user_id, now)
    move, reason = decide_move(state, signals, now)
    step = state["step"]
    if move in (MOVE_UP, MOVE_DOWN):
        index = STEPS.index(step) + (1 if move == MOVE_UP else -1)
        step = STEPS[max(0, min(len(STEPS) - 1, index))]
        if not _write_state(user_id, step, signals.get("reply_rate"), now):
            log_warning("Comment ramp could not store its step", user_id=user_id)
            return {"move": MOVE_UNKNOWN, "step": state["step"], "reason": "state write failed"}
        log_info(f"Comment ramp stepped {move} to {step}/day — {reason}", user_id=user_id,
                 task_name="auto_comment_ramp")
    elif state["since"] is None and _write_state(user_id, step, signals.get("reply_rate"), now):
        log_debug(f"Comment ramp initialised at {step}/day", user_id=user_id)
    return {"move": move, "step": step, "reason": reason}
