"""Drafts for the comment-candidate pool, and the review lane that shows them (issue #2647, Phase 3).

One draft per `comment_candidates` row, written from the post text saved at scan time through
`generate_ai_response` — the same claims-gated generator every feed comment uses, so the quality
contract, similarity gate, slop lint and the `COMMENT_CLAIMS_GATE` mode (off / shadow / enforce)
apply unchanged. A generator that returns None (no readable body, or it never cleared the gates)
retires the row as `skipped`: a template comment costs reach and a skipped post costs one comment.

This module posts NOTHING. A drafted row waits in Daily review; approving it moves it to
`approved`, and only the existing automated lane (within its caps) or the Assist extension posts it.
Behind `COMMENT_CANDIDATE_DRAFTS_ENABLED` (default OFF) and the pool flag.
"""

import os
from datetime import datetime
from typing import Optional

from cqc_lem.utilities.comment_candidates import pool_enabled
from cqc_lem.utilities.db import (
    get_candidates_to_draft,
    get_drafted_comment_candidates,
    get_engagement_preferences,
    get_recent_comment_texts,
    save_candidate_draft,
    skip_candidate,
)
from cqc_lem.utilities.flags import COMMENT_CANDIDATE_DRAFTS, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

DEFAULT_BATCH = 40
DEFAULT_BACKLOG_MAX = 200
LOW_SCORE = 0.3
LOW_SCORE_REASON = "Low candidate score — a weak match for your topics and recency"


def drafts_enabled(user_id: int) -> bool:
    """Whether drafting may run for this user: its own flag AND the pool flag. False on a fault."""
    try:
        return bool(flag_enabled(COMMENT_CANDIDATE_DRAFTS, user_id)) and pool_enabled(user_id)
    except Exception:
        return False


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, default)))
    except ValueError:
        return default


def draft_batch_size() -> int:
    """Most drafts one run writes (`COMMENT_DRAFT_BATCH`), so a run stays short."""
    return _env_int("COMMENT_DRAFT_BATCH", DEFAULT_BATCH)


def draft_backlog_max() -> int:
    """Drafts allowed to wait for review at once (`COMMENT_DRAFT_BACKLOG_MAX`).

    Past it nothing new is drafted: a review queue that only grows is a queue nobody reads.
    """
    return _env_int("COMMENT_DRAFT_BACKLOG_MAX", DEFAULT_BACKLOG_MAX)


def draft_candidates(user_id: int, now: Optional[datetime] = None) -> dict:
    """Draft the best undrafted candidates; returns `{drafted, skipped, reason}`. Never raises.

    `reason` is set only when the run did nothing for a stated cause (flag off, backlog full,
    unreadable state, no profile). An unreadable backlog or candidate list drafts nothing: acting
    on a guess could flood the review queue.
    """
    result = {"drafted": 0, "skipped": 0, "reason": None}
    try:
        if not drafts_enabled(user_id):
            result["reason"] = "off"
            return result
        waiting = get_drafted_comment_candidates(user_id)
        if waiting is None:
            result["reason"] = "backlog unreadable"
            return result
        room = draft_backlog_max() - len(waiting)
        if room <= 0:
            result["reason"] = "backlog full"
            return result
        rows = get_candidates_to_draft(user_id, limit=min(room, draft_batch_size()))
        if rows is None:
            result["reason"] = "candidates unreadable"
            return result
        if not rows:
            result["reason"] = "nothing to draft"
            return result
        from cqc_lem.utilities.ai.ai_helper import generate_ai_response
        from cqc_lem.utilities.linkedin.helper import load_profile_for_user
        profile = load_profile_for_user(user_id)
        if profile is None:
            result["reason"] = "no profile"
            return result
        prefs = get_engagement_preferences(user_id)
        recent = list(get_recent_comment_texts(user_id) or []) + [w["draft_text"] for w in waiting]
        for row in rows:
            text = generate_ai_response(row["post_text"], profile, None, prefs=prefs,
                                        recent_comments=recent, user_id=user_id,
                                        post_id=row["post_key"])
            if text and save_candidate_draft(user_id, row["id"], text):
                recent.insert(0, text)
                result["drafted"] += 1
            elif not text and skip_candidate(user_id, row["id"]):
                result["skipped"] += 1
        log_info("Drafted comment candidates", user_id=user_id, action_type="comment",
                 task_name="auto_draft_comment_candidates")
        return result
    except Exception as exc:
        log_warning("Could not draft comment candidates", exc=exc, user_id=user_id,
                    action_type="comment", task_name="auto_draft_comment_candidates")
        result["reason"] = "error"
        return result


def review_items(user_id: int, now: datetime) -> list[dict]:
    """The `comment_candidates` review lane: one queue item per drafted candidate.

    A low candidate score is carried as the item's `hold_reason`, which `review_flags.annotate`
    turns into the `gate_near_miss` flag, so a weak match is never swept in by "approve all clean".
    The number, first-person-claim and named-entity flags come from the draft text as in every lane.
    """
    from cqc_lem.utilities.review_queue import _age_hours, _item
    rows = get_drafted_comment_candidates(user_id)
    if rows is None:
        raise RuntimeError("drafted comment candidates unreadable")
    items = []
    for r in rows:
        try:
            low = r.get("score") is not None and float(r["score"]) < LOW_SCORE
        except (TypeError, ValueError):
            low = False
        items.append(_item("comment_candidates", r["id"], _age_hours(r.get("scanned_at"), now),
                           f"Comment on {r.get('author') or r.get('post_url') or 'a post'}",
                           r.get("draft_text"), ("approve", "cancel"), "/content?tab=review",
                           hold_reason=LOW_SCORE_REASON if low else None))
    if not items:
        log_debug("No drafted comment candidates waiting", user_id=user_id,
                  task_name="review_items")
    return items
