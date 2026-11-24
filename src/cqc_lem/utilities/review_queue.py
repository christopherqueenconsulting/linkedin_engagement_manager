"""The cross-lane Daily review: ONE read model over every approval waiting on the author (#2625).

Each lane is a `Lane` in `LANES` — an id, a read adapter that maps that lane's existing repository
reader onto the common item shape, and the actions it allows. Adding a lane is adding a row, never
a new endpoint. This module is READ-ONLY: it approves and cancels nothing, so no send guard
(`_refuse_agent_approval`, the stale-DM slot reset, `approved_by`) is bypassed by it. The bulk write
path is a later phase and must delegate to the per-lane helpers.

Item shape: `lane, id, age_hours, summary, draft_text, hold_reason, risk_flags, confidence,
allowed_actions, edit_href, agent_created`. `risk_flags` and `confidence` are filled by `review_flags.annotate`
(Phase 2); `age_hours` is None when the lane's row carries no readable timestamp — an
unmeasured wait is never zero, and is never counted stale. `agent_created` is True for a DM or
connection request an MCP seat queued (`source='mcp'`, MCP W2): the SPA badges it and never
bulk-approves it, and `POST /review/bulk` refuses it in a multi-item approve.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from cqc_lem.utilities.logger import log_warning
from cqc_lem.utilities.review_flags import annotate

REVIEW_STALE_HOURS = 48
_PAGE = 500
_SUMMARY_CHARS = 140

_APPROVE_CANCEL = ("approve", "cancel")
# The `source` an MCP seat's row carries (`api.main.MCP_ROW_SOURCE`; not imported, to keep this
# module free of the API package).
MCP_SOURCE = "mcp"
_EDIT_ONLY = ("edit",)


def _age_hours(value: Any, now: datetime) -> Optional[float]:
    """Hours since `value` (datetime or ISO string, naive = UTC), or None when unreadable."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return round(max(0.0, (now - value).total_seconds() / 3600), 1)


def _clip(text: Optional[str]) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _SUMMARY_CHARS else text[:_SUMMARY_CHARS - 1] + "…"


def _item(lane: str, row_id: int, age: Optional[float], summary: str, draft: Optional[str],
          actions: tuple[str, ...], edit_href: str, hold_reason: Optional[str] = None,
          agent_created: bool = False) -> dict:
    return {"lane": lane, "id": row_id, "age_hours": age, "summary": _clip(summary),
            "draft_text": draft or "", "hold_reason": hold_reason, "risk_flags": [],
            "confidence": None, "allowed_actions": list(actions), "edit_href": edit_href,
            "agent_created": agent_created}


def _read_scheduled_dms(user_id: int, now: datetime) -> list[dict]:
    from cqc_lem.utilities.db import get_scheduled_dms
    rows = get_scheduled_dms(user_id, status_filter="pending", page_size=_PAGE).get("dms", [])
    return [_item("scheduled_dms", r["id"], _age_hours(r.get("created_at"), now),
                  f"DM to {r.get('recipient_name') or r.get('recipient_profile_url')}",
                  r.get("message"), _APPROVE_CANCEL, "/content?tab=dms",
                  agent_created=r.get("source") == MCP_SOURCE) for r in rows]


def _read_connection_requests(user_id: int, now: datetime) -> list[dict]:
    from cqc_lem.utilities.db import get_connection_requests
    rows = get_connection_requests(user_id, status_filter="pending", page_size=_PAGE,
                                   sort_order="asc").get("requests", [])
    return [_item("connection_requests", r["id"], _age_hours(r.get("created_at"), now),
                  f"Connect with {r.get('recipient_name') or r.get('recipient_profile_url')}",
                  r.get("message"), _APPROVE_CANCEL, "/content?tab=connections",
                  agent_created=r.get("source") == MCP_SOURCE) for r in rows]


def _read_funnel_targets(user_id: int, now: datetime) -> list[dict]:
    from cqc_lem.utilities.db import get_outreach_targets
    rows = get_outreach_targets(user_id, status_filter="pending", page_size=_PAGE,
                                sort_order="asc").get("targets", [])
    return [_item("outreach_funnel_targets", r["id"], _age_hours(r.get("created_at"), now),
                  f"Funnel {r.get('stage')}: {r.get('target_name') or r.get('target_profile_url')}",
                  r.get("draft_text"), _APPROVE_CANCEL, "/content?tab=outreach") for r in rows]


def _read_catchup_touches(user_id: int, now: datetime) -> list[dict]:
    from cqc_lem.utilities.db import get_catchup_touches
    rows = get_catchup_touches(user_id, status_filter="pending", page_size=_PAGE,
                               sort_by="created_at", sort_order="asc").get("touches", [])
    return [_item("catchup_touches", r["id"], _age_hours(r.get("created_at"), now),
                  f"{r.get('event_type')}: {r.get('person_name') or r.get('profile_url')}",
                  r.get("message"), _APPROVE_CANCEL, "/content?tab=catchup") for r in rows]


def _read_held_posts(user_id: int, now: datetime) -> list[dict]:
    from cqc_lem.utilities.db import get_held_pending_posts
    from cqc_lem.utilities.quality_gates import parse_gate_findings
    items = []
    for r in get_held_pending_posts(user_id):
        findings = parse_gate_findings(r.get("gate_reason"))
        reason = "; ".join(str(f.get("label") or f.get("gate")) for f in findings
                           if f.get("demoted"))
        if not reason and r.get("ever_gate_demoted"):
            reason = "Repaired by the review gate — read it before it ships"
        items.append(_item("posts", r["id"], _age_hours(r.get("updated_at"), now),
                           f"{r.get('post_type') or 'text'} post", r.get("content"),
                           _EDIT_ONLY, "/content?tab=review", hold_reason=reason or None))
    return items


def _read_newsletter_draft(user_id: int, now: datetime) -> list[dict]:
    from cqc_lem.utilities.db import get_pending_newsletter_editions
    items = []
    for r in get_pending_newsletter_editions(user_id):
        if r.get("status") != "draft":
            continue
        items.append(_item("newsletter_editions", r["id"], None,
                           r.get("title") or "Newsletter draft", r.get("body"),
                           _EDIT_ONLY, "/content?tab=newsletters",
                           hold_reason=r.get("fact_hold")))
    return items


def _read_group_post_draft(user_id: int, now: datetime) -> list[dict]:
    from cqc_lem.utilities.db import get_open_group_post_draft
    r = get_open_group_post_draft(user_id)
    if not r:
        return []
    return [_item("group_posts", r["id"], _age_hours(r.get("created_at"), now),
                  f"Group post to {r.get('group_name') or r.get('group_id')}", r.get("content"),
                  _EDIT_ONLY, "/content?tab=groups")]


@dataclass(frozen=True)
class Lane:
    """One approval lane: its id and the adapter onto the common item shape."""

    id: str
    read: Callable[[int, datetime], list[dict]]


LANES: tuple[Lane, ...] = (
    Lane("scheduled_dms", _read_scheduled_dms),
    Lane("connection_requests", _read_connection_requests),
    Lane("outreach_funnel_targets", _read_funnel_targets),
    Lane("catchup_touches", _read_catchup_touches),
    Lane("posts", _read_held_posts),
    Lane("newsletter_editions", _read_newsletter_draft),
    Lane("group_posts", _read_group_post_draft),
)
LANE_IDS = tuple(lane.id for lane in LANES)
LANE_LABELS = {
    "scheduled_dms": "Scheduled DMs", "connection_requests": "Connection requests",
    "outreach_funnel_targets": "Funnel targets", "catchup_touches": "Catch-up touches",
    "posts": "Held posts", "newsletter_editions": "Newsletter draft",
    "group_posts": "Group post",
}


def _oldest_first(item: dict) -> tuple:
    age = item["age_hours"]
    return (age is None, -(age or 0.0))


def get_review_queue(user_id: int, lane: Optional[str] = None,
                     now: Optional[datetime] = None) -> list[dict]:
    """Every waiting item for `user_id`, oldest first, optionally one lane.

    A lane whose adapter raises is skipped with a warning rather than failing the whole queue: the
    other lanes' approvals are still the author's to act on. Unmeasured ages sort last.
    """
    now = now or datetime.now(timezone.utc)
    items: list[dict] = []
    for ln in LANES:
        if lane and ln.id != lane:
            continue
        try:
            items.extend(ln.read(user_id, now))
        except Exception as e:
            log_warning("Could not read a review lane", exc=e, user_id=user_id,
                        action_type="review_queue")
    items.sort(key=_oldest_first)
    return annotate(items)


def summarize(items: list[dict], stale_hours: int = REVIEW_STALE_HOURS) -> dict:
    """`{total, stale_48h, oldest_age_hours, by_lane}` over an already-read queue."""
    by_lane = {lane_id: 0 for lane_id in LANE_IDS}
    ages = [i["age_hours"] for i in items if i["age_hours"] is not None]
    for i in items:
        by_lane[i["lane"]] += 1
    return {"total": len(items),
            "stale_48h": sum(1 for a in ages if a > stale_hours),
            "oldest_age_hours": max(ages) if ages else None,
            "by_lane": by_lane}
