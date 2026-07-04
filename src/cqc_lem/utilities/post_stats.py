"""Personalized post-time recommendations from captured engagement stats (pure functions)."""

from collections import defaultdict

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def engagement_score(reactions, comments, reposts=0) -> int:
    """Weight deeper signals higher (2026: comments/reposts >> likes)."""
    return int(reactions or 0) + 2 * int(comments or 0) + 2 * int(reposts or 0)


def recommend_post_times(rows, top_n: int = 3, min_posts: int = 3) -> list:
    """rows: iterable of (scheduled_time[datetime], reactions, comments, reposts). Returns the top
    (weekday, hour) buckets by average engagement per post. Empty until >= min_posts of data so we
    don't recommend off noise."""
    buckets = defaultdict(list)
    n = 0
    for row in rows:
        st = row[0]
        if st is None:
            continue
        n += 1
        reposts = row[3] if len(row) > 3 else 0
        buckets[(st.weekday(), st.hour)].append(engagement_score(row[1], row[2], reposts))
    if n < min_posts:
        return []
    ranked = sorted(
        ({"weekday": _WEEKDAYS[wd], "weekday_num": wd, "hour": hr,
          "avg_engagement": round(sum(v) / len(v), 1), "sample": len(v)}
         for (wd, hr), v in buckets.items()),
        key=lambda b: b["avg_engagement"], reverse=True)
    return ranked[:top_n]
