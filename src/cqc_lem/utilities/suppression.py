"""Suppression tripwire (issue #629) — pure detection over the daily engagement trend.

2026 LinkedIn penalties are SILENT. A flagged account does not get a notification; its reach
step-collapses (the documented 8,500 -> 340 impressions overnight pattern) and stays collapsed for
60-90 days of clean behaviour. A single day of automation is worth far less than a month of
suppressed reach, so the system has to notice the collapse itself and stop.

This module is the arithmetic half: it takes the SAME daily series the analytics dashboard renders
(`post_stats.build_engagement_trend`) plus the comment-quality report (`comment_outcomes`, issue
#628) and returns a verdict. No DB, no Redis, no Selenium — the beat task owns the side effects.

Two guards keep it from crying wolf, because a false trip costs the user a real day of engagement:

* Everything is relative to the account's OWN trailing median, never an absolute impression count —
  the whole platform's organic reach is declining, and a baseline that ignores that would trip on
  everyone eventually.
* Days with no posts are dropped BEFORE anything is measured, so a weekend off or a sparse poster is
  not a collapse. `consecutive_days` therefore means consecutive POSTING days, not calendar days.
"""

import os
from datetime import date, timedelta
from math import ceil
from statistics import median
from typing import Any, Iterable, Mapping, Optional, Sequence

from cqc_lem.utilities.comment_outcomes import (
    VERDICT_HOLD,
    VERDICT_UNKNOWN,
    VERDICT_WATCH,
    min_visibility_sample,
)

# A collapse is a step function, not a slide: the documented pattern is an order-of-magnitude drop.
# 70% off the trailing median is well outside normal day-to-day variance but comfortably inside the
# real penalty's magnitude.
DEFAULT_DROP_RATIO = 0.7
# Sustained, not a single bad day. One post landing flat is content; three posting days in a row at
# a fraction of the median is the account.
DEFAULT_CONSECUTIVE_DAYS = 3
DEFAULT_BASELINE_DAYS = 14
# The trailing window has to contain a real sample before it can be called a baseline — a first-week
# account comparing post #4 against post #1 would trip on nothing but variance.
DEFAULT_MIN_BASELINE_POSTS = 3
# How long the automation pause lasts once tripped. Recovery from a real penalty takes 60-90 days of
# clean behaviour, and the daily check RE-ARMS this while the tripwire is still set, so in practice
# it never lapses on its own — a human clears it.
DEFAULT_PAUSE_SECONDS = 90 * 24 * 60 * 60
# The comment-demotion signal reads the LAST FEW DAYS, not #628's rolling week — see
# comment_history_days().
DEFAULT_COMMENT_DAYS = 3
# The window `min_visibility_sample()` (10) was calibrated for: `auto_weekly_comment_quality` scores
# a rolling week. It is the denominator comment_min_sample() scales that floor by, so a narrower
# comment window keeps the same reads-per-day expectation instead of an unreachable floor.
COMMENT_SAMPLE_REFERENCE_DAYS = 7

STATUS_OK = "ok"
STATUS_WATCH = "watch"
STATUS_TRIPPED = "tripped"
STATUS_UNKNOWN = "unknown"

METRIC_IMPRESSIONS = "impressions_per_post"
METRIC_ENGAGEMENT = "engagement_per_post"

SIGNAL_REACH = "reach_collapse"
SIGNAL_COMMENT_DEMOTION = "comment_demotion"


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(float(raw)) if raw else default
    except ValueError:
        return default


def tripwire_enabled() -> bool:
    """Is the daily check armed? Defaults ON, and only an explicitly falsy env value turns it off.

    On by default because the asymmetry runs the other way from most opt-in features here: a missed
    real penalty costs 60-90 days of suppressed reach, a false trip costs a day of engagement that a
    human can hand back. Every other knob in this module exists to make the false trip unlikely.
    """
    return (os.environ.get("SUPPRESSION_TRIPWIRE_ENABLED") or "true").strip().lower() not in (
        "0", "false", "no", "off")


def drop_ratio() -> float:
    """Share below the trailing median that counts as collapsed, clamped to a real 0-1 share so a
    misconfigured 70 (meaning percent) can never make the tripwire unreachable.
    """
    return min(0.99, max(0.01, _env_float("SUPPRESSION_DROP_RATIO", DEFAULT_DROP_RATIO)))


def consecutive_days() -> int:
    """How many POSTING days in a row must all be collapsed before this counts as suppression.

    Floored at 1: `_reach_signal` slices the recent run as `series[-run_days:]`, and 0 there selects
    the WHOLE history rather than nothing, which would compare the account against itself.
    """
    return max(1, _env_int("SUPPRESSION_CONSECUTIVE_DAYS", DEFAULT_CONSECUTIVE_DAYS))


def baseline_days() -> int:
    """Width of the trailing window the recent run is measured against, in CALENDAR days (floor 1).

    Calendar rather than posting days on purpose: the window is cut back from the first day of the
    recent run, so a sparse poster's baseline stays recent instead of reaching back months and
    comparing today against a different era of the account.
    """
    return max(1, _env_int("SUPPRESSION_BASELINE_DAYS", DEFAULT_BASELINE_DAYS))


def min_baseline_posts() -> int:
    """Posts the trailing window must contain before its median may be called a baseline (floor 1).

    Below this the reach signal stays `unknown` — not `ok` — so a thin-history account is reported
    as unmeasured rather than quietly graded healthy on a sample of one.
    """
    return max(1, _env_int("SUPPRESSION_MIN_BASELINE_POSTS", DEFAULT_MIN_BASELINE_POSTS))


def pause_seconds() -> int:
    """How long a trip pauses engagement, floored at 60s so a bad override cannot make it a no-op.

    The TTL is a backstop against a dead scheduler, not an expiry: the daily beat re-arms the pause
    while the trip still stands, and recovery is a human clearing it (`POST /user/automation-resume`)
    — never the clock running out.
    """
    return max(60, _env_int("SUPPRESSION_PAUSE_SECONDS", DEFAULT_PAUSE_SECONDS))


def history_days() -> int:
    """How far back the caller must read so a full baseline still exists behind the recent run.
    The recent run is counted in POSTING days, so allow generous calendar room for a sparse poster.
    """
    return baseline_days() + consecutive_days() * 7


def comment_history_days() -> int:
    """How far back the COMMENT-demotion signal reads — days, not `history_days()` or #628's week.

    Two things set it. A demotion episode #628 has since remediated must not trip a 90-day
    engagement pause weeks later, so the reach baseline's window is far too wide here. And this beat
    runs DAILY: at a week wide, a sudden demotion spike is averaged against up to six healthy days,
    so the rate takes days to cross the threshold even though the check itself ran the morning it
    started. Three days is short enough that a spike moves the rate while it is still a spike.

    The floor scales with it (`comment_min_sample()`) rather than staying at the weekly 10, which a
    3-day window would rarely reach. That is a deliberate sensitivity trade: a narrower window on a
    smaller sample trips sooner and is likelier to trip on noise. It is the right side to err on
    here because the reach signal is independent evidence, `watch` and `unknown` action nothing, and
    a human clears the pause — a false trip costs a day of engagement, a missed penalty costs 60-90.
    """
    return max(1, _env_int("SUPPRESSION_COMMENT_DAYS", DEFAULT_COMMENT_DAYS))


def comment_min_sample() -> int:
    """Readable comments the demotion rate needs before it may trip the pause, in THIS window.

    Derived from `comment_history_days()` instead of being its own env knob, so tuning
    `SUPPRESSION_COMMENT_DAYS` moves the floor with it — two independent knobs drift, and a window
    narrowed without its floor is a signal that silently never fires (a 3-day window almost never
    collects the weekly 10). At the defaults: `ceil(10 * 3 / 7)` = 5. Floored at 1 so no
    combination can make an empty sample look conclusive.
    """
    return max(1, ceil(min_visibility_sample() * comment_history_days()
                       / COMMENT_SAMPLE_REFERENCE_DAYS))


def _posts(day: Mapping[str, Any]) -> int:
    try:
        return int(day.get("posts") or 0)
    except (TypeError, ValueError):
        return 0


def _impressions(day: Mapping[str, Any]) -> Optional[int]:
    """Positive impression total for the day, or None when the day's total is unknown.
    `build_engagement_trend` already returns None whenever any post that day lacked impressions, so
    a partial day never masquerades as a low one.
    """
    value = day.get("impressions")
    if value is None:
        return None
    try:
        views = int(value)
    except (TypeError, ValueError):
        return None
    return views if views > 0 else None


def _day_value(day: Mapping[str, Any], metric: str) -> Optional[float]:
    posts = _posts(day)
    if posts <= 0:
        return None
    if metric == METRIC_IMPRESSIONS:
        views = _impressions(day)
        return None if views is None else views / posts
    try:
        return float(day.get("engagement") or 0) / posts
    except (TypeError, ValueError):
        return None


def _pick_metric(days: Sequence[Mapping[str, Any]]) -> str:
    """Impressions are the real reach signal, but only the author's own analytics view exposes them,
    so they are often absent. Use them only when EVERY day being compared has them — mixing a
    complete baseline with an impression-less recent day would read as a total collapse.
    """
    if days and all(_impressions(day) is not None for day in days):
        return METRIC_IMPRESSIONS
    return METRIC_ENGAGEMENT


def _date(day: Mapping[str, Any]) -> str:
    return str(day.get("date") or "")


def _posting_days(trend: Optional[Iterable[Mapping[str, Any]]]) -> list:
    """Days that actually carried a post, oldest first. A day with no post has no reach to measure —
    counting it as a zero is how a weekend off becomes a false penalty.
    """
    days = [dict(day) for day in (trend or []) if day and _posts(day) > 0 and _date(day)]
    days.sort(key=_date)
    return days


def _comment_signal(comment_quality: Optional[Mapping[str, Any]]) -> dict:
    """The D4 comment-demotion verdict (issue #628) read as a suppression signal. Its own hold only
    stops commenting; sustained demotion of a user's comments is also evidence the ACCOUNT is being
    suppressed, which is a bigger stop.
    """
    verdict = dict((comment_quality or {}).get("verdict") or {})
    status = verdict.get("status")
    signal = {"name": SIGNAL_COMMENT_DEMOTION, "status": STATUS_UNKNOWN,
              "reason": "No comment visibility readings",
              "demotion_rate": verdict.get("demotion_rate"),
              "visibility_sample": verdict.get("visibility_sample")}
    if not status or status == VERDICT_UNKNOWN:
        return signal
    if status == VERDICT_HOLD:
        signal.update(status=STATUS_TRIPPED,
                      reason=f"Comment demotion: {verdict.get('reason')}")
    elif status == VERDICT_WATCH:
        signal.update(status=STATUS_WATCH, reason=f"Comment demotion: {verdict.get('reason')}")
    else:
        signal.update(status=STATUS_OK, reason=f"Comment visibility healthy — {verdict.get('reason')}")
    return signal


def _reach_signal(trend: Optional[Iterable[Mapping[str, Any]]], *, ratio: float, run_days: int,
                  window_days: int, min_posts: int) -> dict:
    series = _posting_days(trend)
    signal: dict[str, Any] = {"name": SIGNAL_REACH, "status": STATUS_UNKNOWN, "reason": "", "metric": None,
                              "baseline": None, "baseline_posts": 0, "baseline_days_sampled": 0,
                              "posting_days": len(series), "recent": [], "max_drop": None}
    if len(series) <= run_days:
        signal["reason"] = (f"Only {len(series)} posting day(s) of history — "
                            f"{run_days + 1} needed before reach can be compared")
        return signal

    recent = series[-run_days:]
    # Calendar-window the baseline off the first day of the recent run: "the trailing 14 days before
    # the drop", not the last 14 posting days, which on a sparse account could reach back months.
    cutoff = _shift_date(_date(recent[0]), -window_days)
    baseline = [day for day in series[:-run_days] if _date(day) >= cutoff]
    baseline_posts = sum(_posts(day) for day in baseline)
    signal.update(baseline_posts=baseline_posts, baseline_days_sampled=len(baseline))
    if baseline_posts < min_posts:
        signal["reason"] = (f"Only {baseline_posts} post(s) in the trailing {window_days} days — "
                            f"{min_posts} needed for a baseline")
        return signal

    metric = _pick_metric(baseline + recent)
    signal["metric"] = metric
    baseline_values = [v for v in (_day_value(day, metric) for day in baseline) if v is not None]
    recent_values = [(day, _day_value(day, metric)) for day in recent]
    scored = [(day, value) for day, value in recent_values if value is not None]
    if not baseline_values or len(scored) != len(recent_values):
        signal["reason"] = "Not enough comparable readings to score reach"
        return signal
    median_value = median(baseline_values)
    signal["baseline"] = round(median_value, 4)
    if median_value <= 0:
        # A zero-engagement baseline has nothing to collapse FROM; every day would read as a 0% drop
        # and the tripwire would be permanently silent OR permanently loud depending on rounding.
        signal["reason"] = "Trailing baseline is zero — nothing to compare against"
        return signal

    drops = []
    for day, value in scored:
        drop = 1.0 - (value / median_value)
        drops.append(drop)
        signal["recent"].append({"date": _date(day), "value": round(value, 4),
                                 "drop": round(drop, 4), "posts": _posts(day)})
    signal["max_drop"] = round(max(drops), 4)
    label = "impressions" if metric == METRIC_IMPRESSIONS else "engagement"
    if all(drop >= ratio for drop in drops):
        signal.update(status=STATUS_TRIPPED,
                      reason=(f"{min(drops):.0%}+ drop in {label} per post across {run_days} "
                              f"consecutive posting days vs the trailing {window_days}-day median "
                              f"({median_value:,.0f})"))
    elif any(drop >= ratio for drop in drops):
        signal.update(status=STATUS_WATCH,
                      reason=(f"{max(drops):.0%} drop in {label} per post on some of the last "
                              f"{run_days} posting days — not yet sustained"))
    else:
        signal.update(status=STATUS_OK,
                      reason=(f"{label.capitalize()} per post within {max(drops):.0%} of the "
                              f"trailing {window_days}-day median"))
    return signal


def _shift_date(iso_date: str, days: int) -> str:
    try:
        return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()
    except (TypeError, ValueError):
        return ""


def evaluate_suppression(trend: Optional[Iterable[Mapping[str, Any]]],
                         comment_quality: Optional[Mapping[str, Any]] = None) -> dict:
    """Score one user for silent suppression.

    `trend` is `post_stats.build_engagement_trend` output (daily buckets, ascending); `comment_quality`
    is `comment_outcomes.comment_quality_report` output. Returns the verdict the beat task acts on and
    the UI renders — one shape, so the banner the user reads and the condition that paused their
    automation can never disagree.

    'watch' never stops anything: it is the signal present but not yet sustained. 'unknown' means we
    could not measure (cold start, sparse posting, no impressions) and is likewise never actioned —
    a tripwire that fires on absent data is worse than no tripwire.
    """
    ratio, run_days = drop_ratio(), consecutive_days()
    window_days, min_posts = baseline_days(), min_baseline_posts()
    reach = _reach_signal(trend, ratio=ratio, run_days=run_days, window_days=window_days,
                          min_posts=min_posts)
    comments = _comment_signal(comment_quality)
    signals = [reach, comments]
    statuses = {signal["status"] for signal in signals}
    if STATUS_TRIPPED in statuses:
        status = STATUS_TRIPPED
    elif STATUS_WATCH in statuses:
        status = STATUS_WATCH
    elif STATUS_OK in statuses:
        status = STATUS_OK
    else:
        status = STATUS_UNKNOWN
    triggers = [signal for signal in signals if signal["status"] == status]
    return {
        "status": status,
        "tripped": status == STATUS_TRIPPED,
        "reason": "; ".join(signal["reason"] for signal in triggers if signal["reason"]),
        "signals": signals,
        "config": {"drop_ratio": ratio, "consecutive_days": run_days,
                   "baseline_days": window_days, "min_baseline_posts": min_posts},
    }
