"""Per-user progress for a weekly content-generation run (issue #545).

`/create_weekly_content/` fires an async Celery chain and returns immediately, but the run
takes minutes (each video post is a ~6-minute Runway/Veo call). Without a progress signal the
SPA looks broken, so the run publishes a lightweight status the UI can poll: queued →
in_progress (X of N) → done/failed.

State lives in Redis, not the DB: it is short-lived runtime progress (like the 429 breaker),
so it survives deploys without a migration and expires on its own. Fails open — when Redis is
unavailable every write no-ops and `get_generation_status` returns None, so generation itself
never breaks because progress reporting is down.

One task owns a user's key at a time (the chain is serial per user), so the read-modify-write
counters below don't need locking.
"""

import json
import os
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Optional, Protocol

from cqc_lem.utilities.logger import log_debug

_KEY_PREFIX = "content_generation:status:"
_DEFAULT_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_RESULT_TTL_SECONDS = 60 * 60


class RedisLike(Protocol):
    """The slice of the Redis API this module uses — structural, so `redis.Redis` and the test
    doubles both satisfy it without importing `redis` at module scope (it is imported lazily so
    the module keeps working, no-opping, when the client is unavailable)."""

    def get(self, name: str) -> Any: ...

    def set(self, name: str, value: str, ex: Optional[int] = None) -> Any: ...

    def delete(self, *names: str) -> Any: ...


class ContentGenerationState(StrEnum):
    """Lifecycle of one weekly content-generation run for a user."""
    QUEUED = 'queued'            # chain dispatched, generation hasn't started yet
    IN_PROGRESS = 'in_progress'  # generating, `completed`/`failed` of `total` done
    DONE = 'done'                # finished; `failed` may still be > 0 (partial success)
    FAILED = 'failed'            # finished with nothing generated


def _ttl_seconds() -> int:
    try:
        return int(os.getenv("CONTENT_GENERATION_STATUS_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS)))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _result_ttl_seconds() -> int:
    """A FINISHED run expires sooner than a running one: "5 posts are ready to review" is useful
    right after the run and just noise a day later, and expiring it server-side keeps the SPA from
    having to reason about how stale a result is."""
    try:
        return int(os.getenv("CONTENT_GENERATION_RESULT_TTL_SECONDS",
                             str(_DEFAULT_RESULT_TTL_SECONDS)))
    except ValueError:
        return _DEFAULT_RESULT_TTL_SECONDS


def _redis_client() -> Optional[RedisLike]:
    """Redis handle for the progress key, or None if unavailable (progress then no-ops).

    Mirrors the 429 breaker's resolution order: the Celery broker URL when it points at Redis,
    else the result backend (on AWS the broker is SQS), else the local default.
    """
    try:
        import redis
    except Exception:
        return None
    url = os.getenv("CELERY_BROKER_URL", "")
    if not url.startswith("redis"):
        url = os.getenv("CELERY_RESULT_BACKEND", "")
    if not url.startswith("redis"):
        url = f"redis://redis:{os.getenv('REDIS_PORT', '6379')}/0"
    try:
        return redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    except Exception:
        return None


def _key(user_id: int) -> str:
    return f"{_KEY_PREFIX}{int(user_id)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(client: RedisLike, user_id: int) -> Optional[dict]:
    try:
        raw = client.get(_key(user_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        status = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def _write(client: RedisLike, user_id: int, status: dict, ttl: Optional[int] = None) -> None:
    status["updated_at"] = _now()
    try:
        client.set(_key(user_id), json.dumps(status), ex=ttl if ttl is not None else _ttl_seconds())
    except Exception as e:
        log_debug(f"Could not persist content generation status: {e}", user_id=user_id)


def mark_queued(user_id: int) -> None:
    """Record that a generation run was dispatched, before any post is processed. Called by the
    API so the SPA gets an immediate 'queued' the moment the user clicks Generate."""
    client = _redis_client()
    if client is None:
        return
    _write(client, user_id, {
        "state": str(ContentGenerationState.QUEUED),
        "total": 0,
        "completed": 0,
        "failed": 0,
        "post_ids": [],
        "ready_post_ids": [],
        "failed_post_ids": [],
        "started_at": _now(),
        "finished_at": None,
    })


def mark_in_progress(user_id: int, post_ids: list[int]) -> None:
    """Start the run with the posts it will generate — this is where `total` becomes known."""
    client = _redis_client()
    if client is None:
        return
    existing = _read(client, user_id) or {}
    _write(client, user_id, {
        "state": str(ContentGenerationState.IN_PROGRESS),
        "total": len(post_ids),
        "completed": 0,
        "failed": 0,
        "post_ids": [int(p) for p in post_ids],
        "ready_post_ids": [],
        "failed_post_ids": [],
        "started_at": existing.get("started_at") or _now(),
        "finished_at": None,
    })


def _record(user_id: int, post_id: int, failed: bool) -> None:
    client = _redis_client()
    if client is None:
        return
    status = _read(client, user_id)
    if status is None:
        return
    bucket = "failed_post_ids" if failed else "ready_post_ids"
    ids = status.get(bucket) or []
    if int(post_id) not in ids:
        ids.append(int(post_id))
    status[bucket] = ids
    status["failed" if failed else "completed"] = len(ids)
    status["state"] = str(ContentGenerationState.IN_PROGRESS)
    _write(client, user_id, status)


def record_post_generated(user_id: int, post_id: int) -> None:
    """One more post finished generating."""
    _record(user_id, post_id, failed=False)


def record_post_failed(user_id: int, post_id: int) -> None:
    """One post could not be generated — surfaced per-post in the SPA so a media failure is
    visible instead of silently missing from the batch."""
    _record(user_id, post_id, failed=True)


def mark_finished(user_id: int) -> Optional[dict]:
    """Close out the run and return the final status (None when nothing was tracked).

    FAILED only when the run produced nothing; a partial run stays DONE and reports its
    `failed` count so the user still sees what is ready to review.
    """
    client = _redis_client()
    if client is None:
        return None
    status = _read(client, user_id)
    if status is None:
        return None
    completed = int(status.get("completed", 0))
    failed = int(status.get("failed", 0))
    status["state"] = str(ContentGenerationState.FAILED if failed and not completed
                          else ContentGenerationState.DONE)
    status["finished_at"] = _now()
    _write(client, user_id, status, ttl=_result_ttl_seconds())
    return status


def get_generation_status(user_id: int) -> Optional[dict]:
    """Current progress for the user, or None when no run is being tracked."""
    client = _redis_client()
    if client is None:
        return None
    return _read(client, user_id)


def clear_generation_status(user_id: int) -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        client.delete(_key(user_id))
    except Exception as e:
        log_debug(f"Could not clear content generation status: {e}", user_id=user_id)
