"""Comment outcome scoring (issue #628) — pure functions over `comment_outcomes` rows.

Comment→reply rate is the truth metric separating a valuable comment from spam, and LinkedIn's
May 2026 enforcement demotes automated-looking comments out of the default 'Most relevant' view —
a silent kill. This module turns the rows the T+24h sweep records into the weekly quality score
that ships to PostHog, renders on the analytics dashboard, and gates feed commenting.

No DB, no Selenium: the browser step stays thin and the arithmetic is unit-testable (the #403/#404
validation pattern).

Three-valued visibility is load-bearing. `visible_most_relevant` is 1 (present under the default
sort, or present on a thread LinkedIn rendered no sort control for at all — nothing is ordered
there, so nothing is demoted within it), 0 (absent under the default sort but present under
'Most recent' — demoted) or NULL (ambiguous). NULL rows are excluded from the demotion denominator
entirely rather than counted as healthy, because treating "we couldn't tell" as "fine" is exactly
how a silent kill stays silent. What earns the 1 on an unsorted thread is an evidence scan that
DESCRIBED the page and found nothing NAMING a sort (`posting._page_still_names_a_sort`) — a page that
still names one we cannot resolve is drift and stays NULL, and so does a scan that came back blind,
since an empty capture is equally a failed read (#1117).
"""

import os
from typing import Any, Iterable, Mapping, Optional

# Share of visibility-readable comments demoted out of 'Most relevant' that trips the hold. Half of
# a real sample reading as demoted is not drift — it is the enforcement doing what it does.
DEFAULT_DEMOTION_HOLD_RATE = 0.5
# Demotion is a best-effort DOM signal, so a hold never fires off a handful of reads.
DEFAULT_MIN_VISIBILITY_SAMPLE = 10
# How long feed commenting stays held once tripped: long enough that the next weekly report is what
# lifts it (a human decides sooner), short enough that a stale hold can't outlive the account.
DEFAULT_HOLD_SECONDS = 7 * 24 * 60 * 60

STATUS_CHECKED = "checked"
STATUS_SKIPPED = "skipped"

VERDICT_OK = "ok"
VERDICT_WATCH = "watch"
VERDICT_HOLD = "hold"
VERDICT_UNKNOWN = "unknown"


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


def demotion_hold_rate() -> float:
    """Demoted share (0–1) at or above which feed commenting is held, from `COMMENT_DEMOTION_HOLD_RATE`.

    Read per call rather than at import so an operator can retune the threshold on a live account
    without a deploy. An unparseable value falls back to the default instead of raising — a bad env
    string must not take the whole nightly sweep down.
    """
    return _env_float("COMMENT_DEMOTION_HOLD_RATE", DEFAULT_DEMOTION_HOLD_RATE)


def min_visibility_sample() -> int:
    """How many visibility-readable comments must exist before a demotion rate may trip the hold.

    Floored at 1 so a misconfigured 0 can never make an empty sample look like a conclusive one —
    the hold pauses a user's feed commenting, and it must never fire off noise.
    """
    return max(1, _env_int("COMMENT_QUALITY_MIN_SAMPLE", DEFAULT_MIN_VISIBILITY_SAMPLE))


def hold_seconds() -> int:
    """How long a tripped hold lasts, in seconds (default 7 days), from `COMMENT_QUALITY_HOLD_SECONDS`.

    Floored at 60s: a hold shorter than that is indistinguishable from no hold at all, which would
    silently disable the protection rather than loosen it.
    """
    return max(60, _env_int("COMMENT_QUALITY_HOLD_SECONDS", DEFAULT_HOLD_SECONDS))


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """Share as a 0–1 float, or None when there is nothing to divide by. None is not 0.0: an empty
    window has no reply rate, and charting it as zero reads as a collapse that never happened.
    """
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _truthy(value: Any) -> bool:
    return bool(value) and str(value).lower() not in ("0", "false", "none")


def summarize_outcomes(rows: Optional[Iterable[Mapping[str, Any]]]) -> dict:
    """Aggregate `comment_outcomes` rows into the weekly comment-quality picture.

    Denominators differ on purpose: reply/like rates are over the comments we actually READ
    (status='checked'), while the demotion rate is over the narrower set whose visibility we could
    actually determine. A skipped check (deleted post, comment not findable) contributes to neither
    — it is a coverage fact, reported separately as `skipped`.
    """
    checked = skipped = unreadable = 0
    author_replies = replied = liked = our_replies = 0
    demoted = visibility_sample = 0
    total_replies = total_likes = 0

    for row in rows or []:
        if str(row.get("status") or STATUS_CHECKED) == STATUS_SKIPPED:
            skipped += 1
            continue
        checked += 1
        reply_count = int(row.get("reply_count") or 0)
        like_count = int(row.get("like_count") or 0)
        total_replies += reply_count
        total_likes += like_count
        if _truthy(row.get("author_replied")):
            author_replies += 1
        if reply_count > 0:
            replied += 1
        if like_count > 0:
            liked += 1
        if _truthy(row.get("our_reply_sent")):
            our_replies += 1
        visible = row.get("visible_most_relevant")
        if visible is None:
            unreadable += 1  # sort control unreadable — reported so a starved denominator is visible
            continue  # ambiguous read — never counted as healthy
        visibility_sample += 1
        if not _truthy(visible):
            demoted += 1

    return {
        "sample_size": checked + skipped,
        "checked": checked,
        "skipped": skipped,
        "author_replies": author_replies,
        "replied_comments": replied,
        "liked_comments": liked,
        "our_replies_sent": our_replies,
        "total_replies": total_replies,
        "total_likes": total_likes,
        "author_reply_rate": _rate(author_replies, checked),
        "reply_rate": _rate(replied, checked),
        "like_rate": _rate(liked, checked),
        "visibility_sample": visibility_sample,
        "unreadable_readings": unreadable,
        "demoted": demoted,
        "demotion_rate": _rate(demoted, visibility_sample),
    }


def quality_verdict(summary: Optional[Mapping[str, Any]]) -> dict:
    """Turn a summary into the actionable verdict that gates commenting (the G2 feedback loop).

    'watch' exists so a scary-looking rate off three reads never pauses a user's engagement: the
    rate is over threshold but the visibility sample is too thin to act on, so it is surfaced and
    nothing is stopped.
    """
    summary = dict(summary or {})
    sample = int(summary.get("visibility_sample") or 0)
    rate = summary.get("demotion_rate")
    threshold = demotion_hold_rate()
    minimum = min_visibility_sample()
    verdict = {"status": VERDICT_UNKNOWN, "reason": "No comment visibility readings in the window",
               "demotion_rate": rate, "visibility_sample": sample,
               "threshold": threshold, "min_sample": minimum}
    if not sample or rate is None:
        return verdict
    if rate >= threshold and sample >= minimum:
        verdict.update(status=VERDICT_HOLD,
                       reason=(f"{rate:.0%} of {sample} readable comments were demoted out of "
                               f"'Most relevant' (threshold {threshold:.0%})"))
    elif rate >= threshold:
        verdict.update(status=VERDICT_WATCH,
                       reason=(f"{rate:.0%} demoted, but only {sample} readable reading(s) — "
                               f"{minimum} needed before holding commenting"))
    else:
        verdict.update(status=VERDICT_OK,
                       reason=f"{rate:.0%} of {sample} readable comments demoted")
    return verdict


def comment_quality_report(rows: Optional[Iterable[Mapping[str, Any]]], days: int = 7) -> dict:
    """The one shape shared by the weekly PostHog event, the analytics endpoint and the dashboard —
    so the number the user reads and the number the guard acts on can never diverge.
    """
    summary = summarize_outcomes(rows)
    return {"days": int(days), **summary, "verdict": quality_verdict(summary)}
