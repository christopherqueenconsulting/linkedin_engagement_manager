"""Golden-hour presence (issue #622 / G7) — the ONE timing + reporting core for the first-hour
reply amplifier and the 6–8h second-wave self-comment.

#401 shipped the amplifier (several reply sweeps spread across the hour after publishing) and then
nothing measured it: 14 days of logs held two replies, and there was no way to tell whether the
sweeps fired late, were rate-limited, or simply found no comments. Every one of those has a
different fix, so this module makes the sweep ANSWERABLE:

- `sweep_countdowns()` decides when the sweeps fire (moved here from run_automation so the timing
  decision is testable without importing 6k lines of Celery tasks).
- `latency_minutes()` / `within_golden_window()` turn a post-age reading into the verdict the
  audit actually asked for, and `sweep_retry_countdown()` decides whether a sweep that could NOT
  run (429, no session) is worth one more shot before the window closes.
- `golden_hour_report()` builds the per-post record — comments found, replies sent, latency — that
  `track_golden_hour_report` ships to PostHog.
- `second_wave_due_minutes()` / `second_wave_hop_seconds()` / `self_comment_cap()` own the second
  wave's schedule — including the hop that keeps its 6–8h wait under the broker's visibility
  timeout — and the hard ceiling that keeps it from colliding with the #344 seed comment.

Every TIMING decision here is a PURE function of its arguments and the environment — no DB, no LLM,
no Celery — so it is unit-testable in isolation (the `story_bank.py` / `suppression.py` pattern).

The one exception is the RECORDING pair at the bottom (`_reply_outcome`, `_record_golden_hour_report`,
moved down from `app.run_automation` in #1154): building the report needs the post's real publish
time from the DB and ships it to PostHog. It is kept here rather than in a task module because two
clusters call it and neither owns it — and because the rule it enforces, that latency is measured off
the REAL publish time and an unmeasurable one is never on-time, belongs beside `latency_minutes`.
"""

import os
import random
from typing import Optional

from cqc_lem.utilities.db import get_post_age_minutes
from cqc_lem.utilities.human_pacing import PACE_RESPONSIVE, dispatch_jitter_seconds
from cqc_lem.utilities.logger import log_info, log_warning
from cqc_lem.utilities.observability import track_golden_hour_report

# The distribution window itself: LinkedIn decides a post's fate on what happens in roughly the
# first hour, so that is the span the sweeps cover.
GOLDEN_HOUR_MINUTES = 60
# The audit's tolerance. A sweep that lands by minute 90 still hit the window the issue named
# ("within 60–90 min"); past that the reply was too late to move distribution, and a report that
# says so is the whole point of measuring.
GOLDEN_HOUR_LATE_MINUTES = 90
GOLDEN_HOUR_REPLY_SWEEPS = 3
# How old a post can be and still produce a golden-hour report. The sweep walks the last couple of
# days of posts on purpose; only the fresh ones say anything about the amplifier's timing.
GOLDEN_HOUR_REPORT_MAX_MINUTES = 24 * 60
# Hard cap on scheduled sweeps — a misconfigured GOLDEN_HOUR_REPLY_SWEEPS can't flood the broker
# with ETA tasks or fragment the golden hour into meaninglessly tight intervals.
GOLDEN_HOUR_MAX_SWEEPS = 12

# When a golden-hour sweep can't run at all (rate-limited session, login failure), the window used
# to be lost silently — the sweep returned a clean skip and nothing tried again. One short retry
# recovers the common transient case; it is bounded TWICE, by attempts and by the window itself, so
# a hard 429 can never turn into a retry storm against LinkedIn.
GOLDEN_HOUR_RETRY_MINUTES = 7
GOLDEN_HOUR_MAX_RETRIES = 2

# Total self-comments allowed on one of our own posts: the #344 seed comment plus the second wave.
# A third would read as thread-stuffing, and it is the seed/second-wave collision the issue calls
# out — so the cap is enforced on the COUNT of our own comments, not on either task knowing about
# the other.
SELF_COMMENT_MAX_PER_POST = 2

# Alić's second wave: a self-comment that ADDS an insight, timed for the evening re-surface rather
# than the publish hour. Randomized per post so it never lands on a machine-exact offset.
SECOND_WAVE_MIN_HOURS = 6.0
SECOND_WAVE_MAX_HOURS = 8.0
# How far past the scheduled second wave still counts as on-time. A queue backlog of an hour is
# noise; half a day means the second wave didn't happen when it was meant to, and the report says so.
SECOND_WAVE_GRACE_MINUTES = 60

PHASE_REPLY_SWEEP = "reply_sweep"
PHASE_SECOND_WAVE = "second_wave"


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_float(name, float(default)))
    except (TypeError, ValueError, OverflowError):
        return default


def reply_sweeps() -> int:
    """How many reply sweeps one publish schedules, read at call time so ops can tune
    GOLDEN_HOUR_REPLY_SWEEPS without a restart.
    """
    return _env_int("GOLDEN_HOUR_REPLY_SWEEPS", GOLDEN_HOUR_REPLY_SWEEPS)


def sweep_countdowns(sweeps: int = GOLDEN_HOUR_REPLY_SWEEPS,
                     window_minutes: int = GOLDEN_HOUR_MINUTES) -> list:
    """Countdown seconds (from publish) for the golden-hour reply sweeps, spread evenly across the
    window so a comment left at any point in the golden hour is answered within window/sweeps
    minutes. e.g. 3 sweeps over 60 min → [1200, 2400, 3600] (20, 40, 60 min in). Sweep count is
    floored to 1 and capped at GOLDEN_HOUR_MAX_SWEEPS so misconfiguration can't schedule an
    unbounded burst.

    Each slot carries a small RESPONSIVE jitter (issue #626): replying quickly to comments on your
    OWN post is human, so these are exempt from the tens-of-minutes engagement jitter — but landing
    on 20:00/40:00/60:00 after every single publish is a machine signature, so the exact minute
    still moves. Jitter is additive (never earlier), so the last sweep still covers the window.
    """
    n = max(1, min(GOLDEN_HOUR_MAX_SWEEPS, int(sweeps)))
    step = (window_minutes * 60) / n
    # Half a step is the hard jitter ceiling: even a misconfigured PACING_RESPONSIVE_JITTER_MAX_SECONDS
    # can then never reorder two sweeps or collapse them onto the same minute.
    jitter_cap = int(step // 2)
    return [int(round(step * i)) + min(jitter_cap, dispatch_jitter_seconds(PACE_RESPONSIVE))
            for i in range(1, n + 1)]


def latency_minutes(age_minutes) -> Optional[float]:
    """Normalize a post-age reading into the latency the report carries: a non-negative float, or
    None when it is unknown.

    None is NOT zero: an unknown publish time means the reading is unavailable, and a report that
    quietly called it 0.0 would show a perfect golden hour that was never measured. The reading
    itself is computed by the database (`get_post_age_minutes`) against its OWN clock — the app and
    MySQL do not share a timezone, so subtracting `logs.created_at` from a Python UTC `now()` would
    skew every latency by the `TZ` offset.
    """
    if isinstance(age_minutes, bool) or not isinstance(age_minutes, (int, float)):
        return None
    return max(0.0, float(age_minutes))


def report_horizon_minutes(phase: str = PHASE_REPLY_SWEEP) -> float:
    """How old a post can be and still be GRADED by this phase. Twice the phase's own window: a
    reply sweep that reaches a post 3 hours after publish was genuinely late, but the same sweep
    walking yesterday's post at hour 14 was never that post's golden-hour sweep at all — it is a
    routine revisit, and grading it would put a permanent stream of out-of-window readings into the
    denominator of the only question this module exists to answer.
    """
    return min(float(GOLDEN_HOUR_REPORT_MAX_MINUTES), phase_window_minutes(phase) * 2.0)


def should_report(minutes: Optional[float], phase: str = PHASE_REPLY_SWEEP) -> bool:
    """Whether a swept post is one the amplifier is answerable for. The sweep deliberately also
    walks older posts (a late comment still deserves an answer), but a report on one of those says
    nothing about the amplifier's timing — see `report_horizon_minutes`. An UNKNOWN latency still
    reports: not knowing when a post published is itself a finding.
    """
    return minutes is None or minutes <= report_horizon_minutes(phase)


def phase_window_minutes(phase: str = PHASE_REPLY_SWEEP) -> float:
    """The deadline each phase is graded against. The reply sweeps own the 60–90 min distribution
    window; the second wave is SUPPOSED to land 6–8h out, so grading it against 90 minutes would
    mark every healthy second wave as late.
    """
    if phase == PHASE_SECOND_WAVE:
        low = _env_float("SECOND_WAVE_MIN_HOURS", SECOND_WAVE_MIN_HOURS)
        high = _env_float("SECOND_WAVE_MAX_HOURS", SECOND_WAVE_MAX_HOURS)
        return max(low, high) * 60.0 + SECOND_WAVE_GRACE_MINUTES
    return float(GOLDEN_HOUR_LATE_MINUTES)


def within_golden_window(minutes: Optional[float], phase: str = PHASE_REPLY_SWEEP) -> bool:
    """True when an action landed inside the window its phase promises (60–90 min for a reply
    sweep, 6–8h + grace for the second wave). An unknown latency is False — unmeasured is never
    counted as on-time.
    """
    return minutes is not None and minutes <= phase_window_minutes(phase)


def sweep_retry_countdown(minutes_since_publish: Optional[float], attempt: int = 0,
                          rng: Optional[random.Random] = None) -> Optional[int]:
    """Seconds to wait before retrying a golden-hour sweep that could not run, or None when a retry
    is not worth it. `attempt` is how many retries have already been spent (0 on the first run).

    A retry is only scheduled while it would still land INSIDE the window — answering at minute 110
    buys nothing and costs a browser slot — and never more than GOLDEN_HOUR_MAX_RETRIES times, so a
    sustained 429 decays instead of hammering LinkedIn.
    """
    if minutes_since_publish is None or int(attempt) >= GOLDEN_HOUR_MAX_RETRIES:
        return None
    delay_minutes = max(1.0, _env_float("GOLDEN_HOUR_RETRY_MINUTES", GOLDEN_HOUR_RETRY_MINUTES))
    if minutes_since_publish + delay_minutes > GOLDEN_HOUR_LATE_MINUTES:
        return None
    delay = int(delay_minutes * 60)
    return delay + min(delay // 2, dispatch_jitter_seconds(PACE_RESPONSIVE, rng=rng))


def golden_hour_report(post_id: Optional[int], minutes_since_publish: Optional[float],
                       comments_found: int = 0, replies_sent: int = 0,
                       status: str = "ok", sweep_slot: int = 0,
                       phase: str = PHASE_REPLY_SWEEP) -> dict:
    """One post's golden-hour record: how late we got there, what we found, what we sent. This is
    the shape both the log line and the PostHog event carry, so an analysis never has to reconcile
    two versions of the same reading.
    """
    latency = None if minutes_since_publish is None else round(float(minutes_since_publish), 1)
    return {
        "phase": phase,
        "post_id": post_id,
        "sweep_slot": int(sweep_slot),
        "status": status,
        "latency_minutes": latency,
        "within_window": within_golden_window(latency, phase),
        "window_minutes": round(phase_window_minutes(phase), 1),
        "comments_found": int(comments_found or 0),
        "replies_sent": int(replies_sent or 0),
    }


def report_summary(report: dict) -> str:
    """The human one-liner for the log — the same numbers the event carries."""
    report = dict(report or {})
    latency = report.get("latency_minutes")
    when = "unknown latency" if latency is None else f"{latency:.0f} min after publish"
    window = report.get("window_minutes") or phase_window_minutes(str(report.get("phase") or ""))
    return (f"Golden-hour {report.get('phase')} on post {report.get('post_id')}: "
            f"{report.get('comments_found', 0)} comment(s) found, "
            f"{report.get('replies_sent', 0)} reply/replies sent, {when} "
            f"({'in' if report.get('within_window') else 'OUTSIDE'} the "
            f"{window:.0f}-min window, status={report.get('status')})")


def second_wave_enabled() -> bool:
    """The second-wave self-comment is ON by default; SECOND_WAVE_COMMENT_ENABLED=false is the
    kill switch (it publishes to the user's own post, so ops needs one).
    """
    return (os.environ.get("SECOND_WAVE_COMMENT_ENABLED") or "true").strip().lower() \
        not in ("0", "false", "no")


def second_wave_bounds() -> tuple:
    """The (low, high) hour bounds of the second-wave draw, env-tunable and clamped into (0, 24h];
    an inverted pair is swapped rather than ignored.
    """
    low = _env_float("SECOND_WAVE_MIN_HOURS", SECOND_WAVE_MIN_HOURS)
    high = _env_float("SECOND_WAVE_MAX_HOURS", SECOND_WAVE_MAX_HOURS)
    if low > high:
        low, high = high, low
    low = min(max(low, 0.1), 24.0)
    high = min(max(high, low), 24.0)
    return low, high


def second_wave_due_minutes(user_id: Optional[int] = None, post_id: Optional[int] = None,
                            rng: Optional[random.Random] = None) -> float:
    """Minutes after publish that THIS post's second wave is due.

    Seeded on (user, post) rather than drawn fresh, because the task no longer waits in a single
    long countdown (see `second_wave_hop_seconds`): every re-arm — and every broker redelivery —
    has to recompute the SAME target, or the second wave would walk forward every hop and never
    land. Different posts still get different offsets, which is all the anti-pattern draw needs.
    """
    if rng is None and user_id is not None and post_id is not None:
        rng = random.Random(f"second-wave:{user_id}:{post_id}")
    low, high = second_wave_bounds()
    return (rng or random).uniform(low, high) * 60.0


def second_wave_hop_cap_seconds() -> int:
    """The longest countdown the second wave may sit on in ONE hop.

    Celery/Redis redelivers any message that stays unacked past the broker's `visibility_timeout`
    (`celeryconfig.py`, ~75 min by default) — and with `task_acks_late` a countdown task is unacked
    for its whole wait. A single 6–8h countdown would therefore be handed to another worker every
    75 minutes and end up posting the self-comment several times over. So the wait is split into
    hops that finish comfortably inside that timeout, and the task re-arms itself until the post is
    actually due.
    """
    visibility = _env_int("CELERY_VISIBILITY_TIMEOUT",
                          _env_int("CELERY_LONGEST_TASK_SECONDS", 3600) + 15 * 60)
    return max(60, int(max(120, visibility) * 0.6))


def second_wave_hop_seconds(due_minutes: Optional[float],
                            age_minutes: Optional[float]) -> Optional[int]:
    """Seconds to wait before re-checking this post's second wave, or None when it is due NOW.

    An unknown post age is treated as due — the task's own guards (URL, cap, quality) are the real
    gate, and hopping forever on a reading we can't take would be worse than acting once.
    """
    if due_minutes is None or age_minutes is None:
        return None
    remaining = float(due_minutes) - float(age_minutes)
    if remaining <= 0:
        return None
    return max(60, min(second_wave_hop_cap_seconds(), int(remaining * 60)))


def second_wave_first_countdown(user_id: Optional[int] = None,
                                post_id: Optional[int] = None) -> int:
    """The countdown `post_to_linkedin` hands the first second-wave dispatch — the post's due offset,
    clipped to one hop so the broker never has to hold an 8-hour message.
    """
    return max(60, min(second_wave_hop_cap_seconds(),
                       int(second_wave_due_minutes(user_id, post_id) * 60)))


def self_comment_cap() -> int:
    """Total comments WE may leave on one of our own posts (seed + second wave). Clamped to 1–5:
    zero would silently disable the seed comment, and anything above a handful is thread-stuffing.
    """
    return max(1, min(5, _env_int("SELF_COMMENT_MAX_PER_POST", SELF_COMMENT_MAX_PER_POST)))


# ── recording a sweep (moved verbatim from `app.run_automation`, #1154) ──────────────────────────
# The only impure code in this module: reading a post's real publish time and shipping the report.
# `_golden.<name>` was how run_automation reached this module's own helpers, so those prefixes are
# the one edit the move made.
def _reply_outcome(status: str, summary: str, comments_found: int = 0,
                   replies_sent: int = 0) -> dict:
    """One post's reply-sweep outcome. A dict, not a string, because the golden-hour report
    (issue #622) needs the counts the old summary line only ever rendered.
    """
    return {"status": status, "summary": summary, "comments_found": int(comments_found),
            "replies_sent": int(replies_sent)}


def _record_golden_hour_report(user_id: int, post_id: int, sweep_slot: int, outcome: dict,
                               phase: str = PHASE_REPLY_SWEEP) -> "dict | None":
    """Build, log and ship ONE post's golden-hour report (issue #622), or None for a post too old to
    be the amplifier's business. Latency is measured from the post's real publish time, so the
    report answers the audit's question — did this sweep land inside the window, and did it find
    anything? A sweep that arrived late is logged as a WARNING: that is the queue-backlog signal the
    #401 audit had no way to see.
    """
    outcome = dict(outcome or {})
    try:
        minutes = latency_minutes(get_post_age_minutes(user_id, post_id))
    except Exception as e:
        # Measurement must never break the thing it measures — an unreadable age reports as unknown
        # (and therefore out-of-window), it does not abort the sweep mid-post.
        log_warning("Golden-hour latency unreadable", exc=e, user_id=user_id, post_id=post_id)
        minutes = None
    # Scoped to the phase's own horizon: the sweep also walks older posts by design, and grading
    # those revisits would bury the real reading under permanent out-of-window noise.
    if not should_report(minutes, phase):
        return None
    report = golden_hour_report(
        post_id, minutes, comments_found=outcome.get("comments_found"),
        replies_sent=outcome.get("replies_sent"), status=outcome.get("status") or "ok",
        sweep_slot=sweep_slot, phase=phase)
    summary = report_summary(report)
    second_wave = phase == PHASE_SECOND_WAVE
    task_name = "auto_second_wave_comment" if second_wave else "sweep_reply_comments"
    action_type = "comment" if second_wave else "reply"
    if report["within_window"]:
        log_info(summary, user_id=user_id, post_id=post_id, action_type=action_type,
                 task_name=task_name)
    else:
        # Out of window on a post young enough to still matter (should_report already dropped the
        # older ones) — that is the queue-backlog / rate-limit signal, so it goes out as a WARNING.
        log_warning(summary, user_id=user_id, post_id=post_id, action_type=action_type,
                    task_name=task_name)
    try:
        track_golden_hour_report(user_id, report)
    except Exception as e:
        log_warning("Golden-hour report not tracked", exc=e, user_id=user_id, post_id=post_id)
    return report
