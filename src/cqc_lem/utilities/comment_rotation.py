"""The comment archetype rotation, remembered ACROSS runs (issue #2034).

`select_blueprint("comment", recent_formats=…)` rotates the shape a comment is written to so a
day's comments do not all read from one template. It was handed a list built fresh at the top of
each feed walk — and a walk lands one or two comments, so the rotation reset before it could rotate.
Production, three days: the same three anecdotes and the same closing question across the sample.

Redis rather than the database for the same reason the 429 breaker lives there: this is runtime
state about the last few minutes of behaviour, not a fact about the account. Fails OPEN — no Redis
degrades to today's per-run rotation, never to no comment.
"""

from typing import Optional

from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_debug

# How many shapes back the rotation remembers. Long enough to span a day's comments at the observed
# 4-7/day, short enough that a shape becomes available again within the week.
RECENT_SHAPE_MEMORY = 8
# A week: a rotation that outlived the content plan it belongs to would keep steering long after the
# comments it is balancing have scrolled away.
_SHAPE_TTL_SECONDS = 7 * 24 * 60 * 60


def _key(user_id: int) -> str:
    return f"engagement:comment_shapes:{int(user_id)}"


def recent_comment_shapes(user_id: int) -> list:
    """The archetypes this user's last few comments used, most recent first.

    Empty when Redis is unavailable, which degrades the caller to a per-run rotation — what it did
    before this module existed.
    """
    client = shared_redis_client()
    if client is None:
        return []
    try:
        raw = client.lrange(_key(user_id), 0, RECENT_SHAPE_MEMORY - 1) or []
    except Exception as e:
        log_debug(f"Could not read the comment shape rotation: {e}", user_id=user_id)
        return []
    return [v.decode() if isinstance(v, bytes) else str(v) for v in raw]


def record_comment_shape(user_id: int, shape: Optional[str]) -> None:
    """Remember that a comment shipped in `shape`. Best-effort: never blocks a comment."""
    if not shape:
        return
    client = shared_redis_client()
    if client is None:
        return
    try:
        key = _key(user_id)
        client.lpush(key, str(shape))
        client.ltrim(key, 0, RECENT_SHAPE_MEMORY - 1)
        client.expire(key, _SHAPE_TTL_SECONDS)
    except Exception as e:
        log_debug(f"Could not record the comment shape: {e}", user_id=user_id)
