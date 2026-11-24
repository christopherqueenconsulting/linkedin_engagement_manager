"""The daily comment-candidate pool: which scored posts a scan saves, and how they rank (issue #2635).

Phase 1 of "reach 100 reviewed comments a day". The feed scan and the roster pass already read and
score posts; this module saves the ones that clear the cheap gates so a later phase can draft from
a pool instead of from whatever one scan happened to surface. Behind
``COMMENT_CANDIDATE_POOL_ENABLED`` (default OFF) and fail-soft: a pool fault never touches the scan.

A saved post must pass `comment_safety` and the user's `min_reactions`, and must not already be in
the comment ledger — checked through the SAME two readers the scans use (`has_commented_post`,
`has_user_commented_on_post_url`), keyed on the SAME post key, so there is no second key scheme.

Ranking for now is roster membership + topic relevance + recency; buyer fit follows the ICP
target-list builder.
"""

import math
from typing import Optional

from cqc_lem.utilities.ai.comment_safety import classify_comment
from cqc_lem.utilities.db import (
    count_comment_candidates,
    has_commented_post,
    has_user_commented_on_post_url,
    insert_comment_candidate,
)
from cqc_lem.utilities.flags import COMMENT_CANDIDATE_POOL, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.observability import track_comment_candidates

SOURCE_FEED = "feed"
SOURCE_ROSTER = "roster"
SOURCE_CRAWL = "crawl"

# Weights sum to 1.0 so the score reads as 0..1.
_W_ROSTER = 0.35
_W_TOPIC = 0.35
_W_RECENCY = 0.30
_RECENCY_HALFLIFE_MIN = 360.0


def candidate_score(source: str, relevant: bool, focus_affinity: float,
                    age_minutes: Optional[float]) -> "tuple[float, dict]":
    """(score, components) for one candidate: roster membership, topic relevance, recency.

    An unreadable age scores 0.4 on the recency term — mid-priority, never top, as the feed scorer
    treats it.
    """
    roster = 1.0 if source == SOURCE_ROSTER else 0.0
    topic = max(1.0 if relevant else 0.0, min(1.0, max(0.0, float(focus_affinity or 0.0))))
    recency = (0.4 if age_minutes is None
               else math.exp(-max(0.0, float(age_minutes)) / _RECENCY_HALFLIFE_MIN))
    components = {"roster": roster, "topic": round(topic, 4), "recency": round(recency, 4)}
    score = _W_ROSTER * roster + _W_TOPIC * topic + _W_RECENCY * recency
    return round(score, 4), components


def pool_enabled(user_id: int) -> bool:
    """Whether the pool is on for this user; False on any fault, so a scan never depends on it."""
    try:
        return bool(flag_enabled(COMMENT_CANDIDATE_POOL, user_id))
    except Exception:
        return False


def record_comment_candidate(user_id: int, key: str, content: str, author: str, source: str,
                             prefs: Optional[dict] = None, reactions: int = 0,
                             age_minutes: Optional[float] = None, relevant: bool = False,
                             focus_affinity: float = 0.0, post_url: Optional[str] = None) -> bool:
    """Save one scored post into the pool. True only when a new row was written; never raises.

    A flag-off call returns False before any read — the no-op the default state must be.
    """
    try:
        if not pool_enabled(user_id):
            return False
        if not key or not classify_comment(content).is_safe:
            return False
        min_reactions = int((prefs or {}).get("min_reactions") or 0)
        if min_reactions and int(reactions or 0) < min_reactions:
            return False
        if has_commented_post(user_id, key) or has_user_commented_on_post_url(user_id, key):
            return False
        score, components = candidate_score(source, relevant, focus_affinity, age_minutes)
        return bool(insert_comment_candidate(user_id, key, source, score, post_url=post_url,
                                             author=author, components=components,
                                             post_text=content if isinstance(content, str) else None))
    except Exception as exc:
        log_warning("Could not record a comment candidate", exc=exc, user_id=user_id,
                    action_type="comment", task_name="record_comment_candidate")
        return False


def report_candidate_pool(user_id: int, source: str, written: int) -> None:
    """Emit the daily pool-size event for a scan that wrote candidates; never raises.

    A scan that wrote nothing emits nothing (flag off, or every post already pooled) — an expected
    no-op, logged at DEBUG.
    """
    if written <= 0:
        log_debug("No new comment candidates from this scan", user_id=user_id,
                  action_type="comment", task_name="report_candidate_pool")
        return
    try:
        track_comment_candidates(user_id, source, written, count_comment_candidates(user_id) or 0)
    except Exception as exc:
        log_warning("Could not report the comment-candidate pool", exc=exc, user_id=user_id,
                    action_type="comment", task_name="report_candidate_pool")
