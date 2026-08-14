"""The weekly group post's publish slot, and the window a skip stays undoable in (issue #1415).

`auto_group_posts` publishes on Tuesdays at 15:00 UTC (`my_celery.beat_schedule`), two days after the
draft beat writes the post. "Skip this week" is the user's own call and stays reversible right up to
that slot — after it the week is spent, and restoring the draft would ship a post written for a week
that has passed. This module is the ONE place that boundary is computed, so the API refusal and the
control the SPA offers cannot disagree about it.

The SPA mirrors `next_group_publish_slot` in `ui/src/utils/groupPostSlot.ts` to show when a queued
draft ships; keep the two in step.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cqc_lem.utilities.logger import log_debug

# Tuesday, in `datetime.weekday()` terms (Monday is 0).
GROUP_PUBLISH_WEEKDAY = 1
GROUP_PUBLISH_HOUR_UTC = 15


def next_group_publish_slot(from_dt: Optional[datetime] = None) -> datetime:
    """The next weekly group-post publish slot strictly after ``from_dt`` (UTC).

    Args:
        from_dt: The instant to measure from. Naive values are read as UTC, which is what the
            group-post rows carry. Defaults to now.

    Returns:
        The next Tuesday 15:00 UTC after ``from_dt``, as a timezone-aware UTC datetime.
    """
    base = _as_utc(from_dt) if from_dt else datetime.now(timezone.utc)
    candidate = base.replace(hour=GROUP_PUBLISH_HOUR_UTC, minute=0, second=0, microsecond=0)
    candidate += timedelta(days=(GROUP_PUBLISH_WEEKDAY - candidate.weekday()) % 7)
    if candidate <= base:
        candidate += timedelta(days=7)
    return candidate


def skip_undo_deadline(draft: dict[str, Any]) -> Optional[datetime]:
    """The instant this draft's skip stops being undoable, or None when it cannot be worked out.

    The deadline is the publish slot the draft was WRITTEN for — the first one after it was created
    — not one measured from the skip. A user may edit a skipped draft, and anchoring on the row's
    `updated_at` would push the deadline out by a week every time they did.

    Args:
        draft: A group-post draft row as the API serves it (`created_at` is an ISO string).

    Returns:
        The publish slot as a timezone-aware UTC datetime, or None when `created_at` is missing or
        unparseable.
    """
    created = _parse(draft.get("created_at"))
    if created is None:
        return None
    return next_group_publish_slot(created)


def group_skip_undo_open(draft: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Whether this draft's skip can still be undone.

    Fails OPEN: a draft whose `created_at` cannot be read is treated as still undoable, because the
    bug this window exists to fix (#1415) is a user stuck with an accidental skip, and a restore is
    an explicit action that publishes at the NEXT slot rather than silently.

    Args:
        draft: A group-post draft row as the API serves it.
        now: The instant to judge against. Defaults to now.

    Returns:
        True while the draft's publish slot is still ahead.
    """
    deadline = skip_undo_deadline(draft)
    if deadline is None:
        log_debug("Group post skip window unreadable — treating the skip as undoable",
                  task_name="group_skip_undo_open", draft_id=draft.get("id"))
        return True
    return (_as_utc(now) if now else datetime.now(timezone.utc)) < deadline


def _as_utc(value: datetime) -> datetime:
    """A naive datetime read as UTC; an aware one converted to it."""
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse(value: Any) -> Optional[datetime]:
    """A row timestamp (ISO string or datetime) as an aware UTC datetime, or None."""
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        try:
            return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None
