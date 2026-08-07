"""Audience telemetry — parsing LinkedIn's follower/connection/profile-view labels and deriving
the growth series the analytics dashboard renders (issue #627). Pure functions: no DB, no Selenium,
so the label parsing and the delta math are unit-testable without a browser (the #403/#404
validation pattern — browser steps stay thin, the parsing is tested).

Follower growth is the primary outcome of the whole system; post engagement is the leading
indicator. A count that could not be read is None everywhere here — never 0 — because a zero would
show up in a delta as "lost the entire audience".
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

# LinkedIn renders audience counts as "1,234 followers", "3.2K followers", "500+ connections", and
# on the analytics surface as "48 profile views" / "12 search appearances".
_SUFFIX_MULTIPLIER = {"": 1, "K": 1_000, "M": 1_000_000}

FOLLOWER_LABEL = r"followers?"
CONNECTION_LABEL = r"connections?"
PROFILE_VIEW_LABEL = r"profile views?"
SEARCH_APPEARANCE_LABEL = r"search appearances?"

# Growth windows reported on the dashboard panel.
GROWTH_WINDOWS = (7, 30)

_ALL_LABELS = (FOLLOWER_LABEL, CONNECTION_LABEL, PROFILE_VIEW_LABEL, SEARCH_APPEARANCE_LABEL)
_NUMBER = r"(\d[\d.,]*)[ \t]*([KkMm]?)\+?"


def parse_labeled_count(text: Optional[str], label: str) -> Optional[int]:
    """Pull the count belonging to `label` out of LinkedIn label text.
    "1,234 followers" -> 1234, "3.2K followers" -> 3200, "500+ connections" -> 500,
    and the analytics card layout "48\\nProfile views" -> 48.

    Precedence is deliberate: LinkedIn writes the number BEFORE the label everywhere it renders one
    (inline on the profile, stacked above the caption on the analytics cards), so a number
    immediately preceding the label wins. The gap it may span is at most one line break, so a count
    belonging to some OTHER card further up the page can't bind to this label — and a number that
    is already the VALUE of a different label in front of it (`_claimed_by_another_label`) is
    skipped, so a stacked label-first page can't hand every metric the first card's number. A label
    with no number in front of it falls back to the first number just after it. Returns None when no
    count is present — callers persist that as NULL, which is distinct from a real zero.
    """
    if not text:
        return None
    for match in re.finditer(rf"{_NUMBER}[ \t]*\n?[ \t]*{label}", text, flags=re.IGNORECASE):
        if _claimed_by_another_label(text, match.start(), label):
            continue
        value = _to_number(match.group(1), match.group(2))
        if value is not None:
            return value
    match = re.search(rf"{label}[^\d\n]{{0,20}}\n?[ \t]*{_NUMBER}", text, flags=re.IGNORECASE)
    return _to_number(match.group(1), match.group(2)) if match else None


def _claimed_by_another_label(text: str, start: int, label: str) -> bool:
    """True when the number starting at `start` is really the value of a DIFFERENT audience label
    sitting immediately in front of it — LinkedIn's label-first card stack ("Profile views\\n288\\n
    Search appearances\\n88") would otherwise hand 288 to search appearances too, silently recording
    one metric's number under another. The other label only owns the number if it doesn't already
    have one of its own in front of it (the value-first layout, where "4,312 followers\\n500+
    connections" leaves 500 legitimately ours).
    """
    head = text[:start]
    for other in _ALL_LABELS:
        if other == label:
            continue
        owner = re.search(rf"{other}[^\d]{{0,3}}$", head, flags=re.IGNORECASE)
        if owner and not re.search(rf"{_NUMBER}[ \t]*\n?[ \t]*$", head[:owner.start()]):
            return True
    return False


def _to_number(raw: str, suffix: str) -> Optional[int]:
    """"3.2" + "K" -> 3200. A K/M suffix means the digits are a rounded magnitude, so the comma is a
    decimal separator in some locales — but LinkedIn renders en-US here, so commas are thousands.
    """
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    return int(value * _SUFFIX_MULTIPLIER.get(suffix.upper(), 1))


def parse_follower_count(text: Optional[str]) -> Optional[int]:
    """Followers out of the profile page's own text. None when the label is absent or unreadable —
    the caller persists that as NULL ("not measured"), which a growth delta skips instead of
    charting as a total audience loss.
    """
    return parse_labeled_count(text, FOLLOWER_LABEL)


def parse_connection_count(text: Optional[str]) -> Optional[int]:
    """Connections out of the profile page's own text. "500+" reads as 500, LinkedIn's own ceiling
    for the display — not a failure. None when unreadable, never 0.
    """
    return parse_labeled_count(text, CONNECTION_LABEL)


def parse_profile_views(text: Optional[str]) -> Optional[int]:
    """Profile views off the analytics surface, where the number sits stacked ABOVE its caption and
    next to the search-appearances card — `parse_labeled_count` is what keeps one card's number from
    binding to the other's label. None when unreadable, never 0.
    """
    return parse_labeled_count(text, PROFILE_VIEW_LABEL)


def parse_search_appearances(text: Optional[str]) -> Optional[int]:
    """Search appearances off the analytics surface. None when unreadable, never 0 — and the caller
    treats that None as a cue to retry on the dedicated search-appearances page before giving up.
    """
    return parse_labeled_count(text, SEARCH_APPEARANCE_LABEL)


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_follower_series(rows: Iterable[Mapping]) -> list:
    """Daily audience series, oldest first, for the growth panel. `rows` are the snapshot dicts from
    `db.get_follower_stats` (any order). Multiple captures on the same calendar day collapse to the
    LAST one — a re-run is a correction, not another data point, so summing would inflate exactly
    the way the post-stats snapshot bug did. Missing counts stay None.
    """
    buckets: dict = {}
    for row in rows:
        if not row:
            continue
        day = _as_date(row.get("captured_at"))
        if day is None:
            continue
        order = _sort_key(row)
        prior = buckets.get(day)
        # Keep the latest capture of the day, tie-broken by row id so a re-run on the same second
        # still resolves deterministically.
        if prior is not None and prior["_order"] > order:
            continue
        buckets[day] = {
            "date": day.isoformat(),
            "follower_count": _optional_int(row.get("follower_count")),
            "connection_count": _optional_int(row.get("connection_count")),
            "profile_views": _optional_int(row.get("profile_views")),
            "search_appearances": _optional_int(row.get("search_appearances")),
            "_order": order,
        }
    series = []
    for day in sorted(buckets):
        point = dict(buckets[day])
        point.pop("_order", None)
        series.append(point)
    return series


def _sort_key(row: Mapping) -> tuple:
    captured = row.get("captured_at")
    stamp = captured.isoformat() if isinstance(captured, (datetime, date)) else str(captured or "")
    return stamp, _optional_int(row.get("id")) or 0


def _latest_with(series: Sequence[Mapping], field: str) -> Optional[Mapping]:
    for point in reversed(series):
        if point.get(field) is not None:
            return point
    return None


def _on_or_before(series: Sequence[Mapping], field: str, cutoff: date) -> Optional[Mapping]:
    """The newest point on/before `cutoff` that actually carries `field` — the baseline a delta is
    measured from. None when the history doesn't reach back that far.
    """
    best = None
    for point in series:
        day = _as_date(point.get("date"))
        if day is None or day > cutoff or point.get(field) is None:
            continue
        best = point
    return best


def follower_growth(rows: Iterable[Mapping], windows: Sequence[int] = GROWTH_WINDOWS,
                    now: Optional[datetime] = None) -> dict:
    """Growth summary for the dashboard panel: the current follower/connection counts, the latest
    profile-view + search-appearance readings, and a follower delta per window in `windows`
    (7/30-day by default).

    A delta is None unless there is a baseline snapshot on or before `today - window` that carried a
    follower count — with a shorter history the honest answer is "not enough data", not a delta
    measured against the oldest row we happen to have (which would read as explosive growth on day
    two). Pure — no DB.
    """
    series = build_follower_series(rows)
    reference = (now or datetime.now(timezone.utc)).date()
    latest = _latest_with(series, "follower_count")
    deltas: dict = {}
    for window in windows:
        baseline = _on_or_before(series, "follower_count", reference - timedelta(days=int(window)))
        if latest is None or baseline is None or baseline is latest:
            deltas[str(window)] = None
            continue
        delta = latest["follower_count"] - baseline["follower_count"]
        start = baseline["follower_count"]
        deltas[str(window)] = {
            "delta": delta,
            "from": start,
            "to": latest["follower_count"],
            "from_date": baseline["date"],
            "pct": round(delta / start, 5) if start else None,
        }
    views = _latest_with(series, "profile_views")
    appearances = _latest_with(series, "search_appearances")
    connections = _latest_with(series, "connection_count")
    return {
        "series": series,
        "follower_count": latest["follower_count"] if latest else None,
        "captured_at": latest["date"] if latest else None,
        "connection_count": connections["connection_count"] if connections else None,
        "profile_views": views["profile_views"] if views else None,
        "search_appearances": appearances["search_appearances"] if appearances else None,
        "deltas": deltas,
        "samples": len(series),
    }


def build_activity_series(rows: Iterable[Mapping]) -> list:
    """Daily posting/commenting activity, oldest first, for the overlay on the growth chart —
    follower growth is only readable next to what we actually DID that day. `rows` are the
    `{date, action_type, count}` dicts from `db.get_daily_action_counts`.
    """
    buckets: dict = {}
    for row in rows:
        if not row:
            continue
        day = _as_date(row.get("date"))
        if day is None:
            continue
        bucket = buckets.setdefault(day, {"date": day.isoformat(), "posts": 0, "comments": 0,
                                          "replies": 0, "dms": 0})
        key = {"post": "posts", "comment": "comments", "reply": "replies", "dm": "dms"}.get(
            str(row.get("action_type") or "").lower())
        if key:
            bucket[key] += int(row.get("count") or 0)
    return [buckets[day] for day in sorted(buckets)]
