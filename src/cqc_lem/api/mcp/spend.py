"""The MCP spend sub-cap and post provenance (W2b, issue #2689, owner ruling O4).

Two Redis records back the AI/media spend tools, both read and written ONLY by the narrow
`/api/user/mcp-spend/*` routes (`api/routers/mcp_spend.py`), never by the tools themselves, so the
rules hold server-side whatever a client sends:

- **A per-user, per-UTC-day sub-cap** on each kind of spend: `MCP_SPEND_TEXT` (default 10),
  `MCP_SPEND_IMAGE` (3) and `MCP_SPEND_VIDEO` (1). A value of 0 refuses every call of that kind
  without touching Redis. The counter's TTL is set ONCE, when the day's counter is created
  (`limits._incr_with_ttl`). It sits on top of the free-trial daily AI cap, never instead of it.
  A unit is claimed BEFORE the spend is dispatched, and a render that later fails does not give it
  back: the cap bounds attempts, which is what bounds cost.
- **Which MCP token created which post.** Media is generated only for a post THIS token created,
  so the post is marked with `(user_id, session_id)` when an MCP seat creates it (here, and on
  `POST /schedule_post/`). The mark lives as long as the longest-lived MCP token can.

Both fail CLOSED on spend: Redis unreadable raises `LimitsUnavailable`, which the route answers
503 `mcp_unavailable`, and a mark that could not be read is never treated as a match.
"""

import os
from typing import Optional

from cqc_lem.api.mcp.limits import DAY, LimitsUnavailable, _incr_with_ttl, _require_redis
from cqc_lem.utilities.ai_spend_cap import utc_day
from cqc_lem.utilities.db import AGENT_POST_PLACEHOLDER
from cqc_lem.utilities.logger import log_debug

# The body `lem_generate_post`'s row carries until its text is written (defined in
# `platform/db/shared.py` so the publish lane can refuse it). It names itself as a placeholder; the
# media routes, the approve routes, the ready-to-post query and `post_to_linkedin` all refuse it.
PLACEHOLDER_CONTENT = AGENT_POST_PLACEHOLDER

SPEND_TEXT = "text"
SPEND_IMAGE = "image"
SPEND_VIDEO = "video"

ENV_SPEND = {SPEND_TEXT: "MCP_SPEND_TEXT", SPEND_IMAGE: "MCP_SPEND_IMAGE",
             SPEND_VIDEO: "MCP_SPEND_VIDEO"}
DEFAULT_SPEND = {SPEND_TEXT: 10, SPEND_IMAGE: 3, SPEND_VIDEO: 1}

# A day's counter outlives its UTC day by an hour, so a claim made just before midnight is still
# counted against the day it was made in; the key carries the day, so the next day starts at 0.
_SPEND_TTL_SECONDS = DAY + 60 * 60
# The longest an MCP token can live (`mcp_tokens.MCP_TTL_MAX_DAYS`), so the mark outlives every token
# that could ask about it.
_CREATED_TTL_SECONDS = 90 * DAY


def spend_limit(kind: str) -> int:
    """Today's sub-cap for one kind of spend, read at call time.

    Args:
        kind: `SPEND_TEXT`, `SPEND_IMAGE` or `SPEND_VIDEO`.

    Returns:
        A non-negative integer; 0 refuses every call. A malformed value falls back to the default
        rather than lifting the cap.
    """
    raw = (os.getenv(ENV_SPEND[kind]) or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if value >= 0:
            return value
    return DEFAULT_SPEND[kind]


def _spend_key(kind: str, user_id: int) -> str:
    return f"mcp:spend:{kind}:{int(user_id)}:{utc_day()}"


def claim_spend(user_id: int, kind: str) -> bool:
    """Claim one unit of today's sub-cap for `kind`. Blocking.

    Args:
        user_id: The account spending.
        kind: `SPEND_TEXT`, `SPEND_IMAGE` or `SPEND_VIDEO`.

    Returns:
        True when the unit was within the cap; False when the cap is 0 or already spent.

    Raises:
        LimitsUnavailable: Redis could not be read or written (fails closed).
    """
    cap = spend_limit(kind)
    if cap == 0:
        return False
    client = _require_redis()
    try:
        used = _incr_with_ttl(client, _spend_key(kind, user_id), _SPEND_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — any client fault is an unreadable cap.
        raise LimitsUnavailable() from exc
    return used <= cap


def _created_key(post_id: int) -> str:
    return f"mcp:spend:post:{int(post_id)}"


def _owner_value(user_id: int, session_id: int) -> str:
    return f"{int(user_id)}:{int(session_id)}"


def mark_created_post(post_id: int, user_id: int, session_id: Optional[int]) -> bool:
    """Record that this MCP token created this post. Never raises.

    A mark that cannot be written only means media is later REFUSED for the post, the safe
    direction, so a failure here is DEBUG and never fails the create.

    Args:
        post_id: The post just created.
        user_id: Its owner.
        session_id: The MCP token's `sessions` row id.

    Returns:
        True when the mark was stored.
    """
    if not post_id or not user_id or not session_id:
        return False
    try:
        client = _require_redis()
        client.set(_created_key(post_id), _owner_value(user_id, session_id),
                   ex=_CREATED_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 — best effort; the read side fails closed.
        log_debug(f"Could not mark an MCP-created post ({type(exc).__name__})", post_id=post_id,
                  user_id=user_id)
        return False


def created_by_session(post_id: int, user_id: int, session_id: Optional[int]) -> bool:
    """Did THIS MCP token create this post?

    Args:
        post_id: The post a media tool names.
        user_id: The caller.
        session_id: The caller's MCP token row id.

    Returns:
        True only when the stored mark names exactly this user and this token.

    Raises:
        LimitsUnavailable: Redis could not be read (fails closed).
    """
    if not post_id or not user_id or not session_id:
        return False
    client = _require_redis()
    try:
        stored = client.get(_created_key(post_id))
    except Exception as exc:  # noqa: BLE001
        raise LimitsUnavailable() from exc
    if isinstance(stored, bytes):
        stored = stored.decode("utf-8", "replace")
    return stored == _owner_value(user_id, session_id)


# --- per-post claims: one render per (post, kind) at a time, and one paid call per dispatch ---------
#
# Two keys per (post, kind), both `SET NX` with a TTL longer than the worst-case render:
#
# - `inflight`, taken by the ROUTE before it queues anything. A second request for the same post
#   and kind while one is queued or running is a 409 `generation_in_progress`, never a second
#   charge. The task releases it when it finishes, whatever the outcome.
# - `run`, taken by the TASK immediately before the paid call. Celery redelivers a task whose worker
#   died (`acks_late` / `reject_on_worker_lost`); the redelivered copy finds `run` held and skips,
#   so the renderer is never called (and video credits never deducted) twice for one request. It is
#   released on a terminal FAILURE, so the user can ask again; after a success it is left to expire,
#   by which time the post already carries its result and the route refuses a repeat anyway.

_CLAIM_TTL_SECONDS = {SPEND_TEXT: 2 * 60 * 60, SPEND_IMAGE: 2 * 60 * 60, SPEND_VIDEO: 4 * 60 * 60}


def _claim_key(stage: str, kind: str, post_id: int) -> str:
    return f"mcp:spend:{stage}:{kind}:{int(post_id)}"


def _claim(stage: str, kind: str, post_id: int) -> bool:
    client = _require_redis()
    try:
        return bool(client.set(_claim_key(stage, kind, post_id), "1",
                               ex=_CLAIM_TTL_SECONDS[kind], nx=True))
    except Exception as exc:  # noqa: BLE001 — an unreadable claim fails closed.
        raise LimitsUnavailable() from exc


def _release(stage: str, kind: str, post_id: int) -> None:
    try:
        client = _require_redis()
        client.delete(_claim_key(stage, kind, post_id))
    except Exception as exc:  # noqa: BLE001 — the TTL releases it anyway.
        log_debug(f"Could not release an MCP spend claim ({type(exc).__name__})", post_id=post_id,
                  action_type=kind)


def claim_inflight(post_id: int, kind: str) -> bool:
    """Take the route's in-flight marker for (post, kind). False when one is already held.

    Raises:
        LimitsUnavailable: Redis could not be read or written (fails closed).
    """
    return _claim("inflight", kind, post_id)


def release_inflight(post_id: int, kind: str) -> None:
    """Drop the in-flight marker for (post, kind). Never raises.

    During a Redis outage the delete fails too (logged at DEBUG), so the marker then clears on its
    own TTL rather than at once.
    """
    _release("inflight", kind, post_id)


def claim_run(post_id: int, kind: str) -> bool:
    """Take the task's once-per-dispatch claim for (post, kind). False when already held.

    Raises:
        LimitsUnavailable: Redis could not be read or written (fails closed).
    """
    return _claim("run", kind, post_id)


def release_run(post_id: int, kind: str) -> None:
    """Drop the task's run claim after a terminal failure. Never raises.

    During a Redis outage the delete fails too, so the claim then clears on its own TTL.
    """
    _release("run", kind, post_id)


__all__ = ["PLACEHOLDER_CONTENT", "SPEND_TEXT", "claim_inflight", "release_inflight", "claim_run",
           "release_run", "SPEND_IMAGE", "SPEND_VIDEO", "ENV_SPEND",
           "DEFAULT_SPEND", "spend_limit", "claim_spend", "mark_created_post", "created_by_session",
           "LimitsUnavailable"]
