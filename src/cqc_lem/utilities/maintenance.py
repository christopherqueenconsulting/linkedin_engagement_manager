"""Deploy maintenance mode — pause, drain, deploy, resume (issue #549).

`scripts/deploy.sh` recreates every app container, which SIGTERMs the Celery workers. Warm
shutdown (`compose/local/celery/run-as-celery` + `stop_grace_period`) lets a task that is ALREADY
running finish, but nothing stopped a worker from reserving NEW work in the seconds before the
recreate — a task started at T-1s is still mid-flight when the grace period expires.

Maintenance mode closes that window around the recreate:

1. ``begin_maintenance`` — pause dispatch via the existing Redis kill-switch
   (``pause_automation``), then ``cancel_consumer`` each worker's own queues so no further
   message is reserved. The (worker, queue) pairs are snapshotted into Redis first so they can be
   restored even from a different process later.
2. ``drain_workers`` — poll ``inspect active`` until nothing is executing, or a bounded timeout.
3. ``end_maintenance`` — restore the snapshotted consumers and lift the pause, but ONLY if the
   pause is still ours (a 429 breaker pause must outlive the deploy).

Everything fails OPEN: if Redis or the broker is unreachable these are no-ops and the deploy
proceeds exactly as it did before. The pause carries a TTL, so even an aborted deploy cannot
leave automation switched off for good.

Also usable as a CLI from inside a container::

    python -m cqc_lem.utilities.maintenance begin --pause-seconds 1800
    python -m cqc_lem.utilities.maintenance drain --timeout 480
    python -m cqc_lem.utilities.maintenance end
    python -m cqc_lem.utilities.maintenance status
"""

import argparse
import json
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Reuse the breaker's Redis handle so the maintenance flag lands in the same place as the pause
# it extends (same URL resolution, same fail-open semantics) rather than re-deriving the URL.
from cqc_lem.utilities.linkedin.rate_limit import (
    _redis_client,
    automation_pause_reason,
    automation_pause_remaining,
    pause_automation,
    resume_automation,
)
from cqc_lem.utilities.logger import log_info, log_warning

_MAINTENANCE_KEY = "lem:maintenance"
_CONSUMERS_KEY = "lem:maintenance:consumers"

MAINTENANCE_REASON = "deploy"
DEFAULT_PAUSE_SECONDS = 30 * 60
DEFAULT_DRAIN_TIMEOUT_SECONDS = 8 * 60  # matches the workers' stop_grace_period
DEFAULT_POLL_SECONDS = 5
INSPECT_TIMEOUT_SECONDS = 10

# Used only when the Celery config can't be imported (e.g. a stripped CLI context).
FALLBACK_QUEUES: Tuple[str, ...] = ("celery", "se_engage", "se_prepost", "se_outreach", "se_content")


def queue_names() -> Tuple[str, ...]:
    """Every queue the stack consumes, straight from celeryconfig so lanes can't drift apart."""
    try:
        from cqc_lem.app.celeryconfig import task_queues
        names = tuple(q.name for q in task_queues if getattr(q, "name", None))
        return names or FALLBACK_QUEUES
    except Exception:
        return FALLBACK_QUEUES


def _control() -> Any:
    from cqc_lem.app.my_celery import app
    return app.control


def _inspect() -> Any:
    return _control().inspect(timeout=INSPECT_TIMEOUT_SECONDS)


def maintenance_remaining() -> int:
    """Seconds left on the maintenance flag, or 0 when not in maintenance / Redis is down."""
    client = _redis_client()
    if client is None:
        return 0
    try:
        ttl = client.ttl(_MAINTENANCE_KEY)
    except Exception:
        return 0
    return ttl if ttl and ttl > 0 else 0


def is_maintenance_mode() -> bool:
    return maintenance_remaining() > 0


def worker_queue_map() -> Dict[str, List[str]]:
    """{worker hostname: [queue names it consumes]} — empty when no worker replies."""
    try:
        replies = _inspect().active_queues() or {}
    except Exception as e:
        log_warning("Could not inspect worker queues for maintenance mode", exc=e)
        return {}
    mapping: Dict[str, List[str]] = {}
    for worker, queues in replies.items():
        mapping[worker] = [q.get("name") for q in (queues or []) if q.get("name")]
    return mapping


def active_task_count() -> int:
    """Tasks currently EXECUTING across all workers. 0 when nobody replies (nothing to drain)."""
    try:
        replies = _inspect().active() or {}
    except Exception as e:
        log_warning("Could not inspect active tasks — treating the stack as drained", exc=e)
        return 0
    return sum(len(tasks or []) for tasks in replies.values())


def _snapshot_ttl_seconds(pause_seconds: int) -> int:
    """The snapshot must outlive the window it describes, or end_maintenance() would have nothing
    to restore consumers from. Scale with the caller's pause (a long manual window included) and
    never go below the default deploy window."""
    return max(int(pause_seconds), DEFAULT_PAUSE_SECONDS) * 2


def _store_consumer_snapshot(mapping: Dict[str, List[str]],
                             pause_seconds: int = DEFAULT_PAUSE_SECONDS) -> None:
    client = _redis_client()
    if client is None or not mapping:
        return
    try:
        client.set(_CONSUMERS_KEY, json.dumps(mapping), ex=_snapshot_ttl_seconds(pause_seconds))
    except Exception as e:
        log_warning("Could not persist the maintenance consumer snapshot", exc=e)


def _load_consumer_snapshot() -> Dict[str, List[str]]:
    client = _redis_client()
    if client is None:
        return {}
    try:
        raw = client.get(_CONSUMERS_KEY)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def begin_maintenance(pause_seconds: int = DEFAULT_PAUSE_SECONDS,
                      queues: Optional[Iterable[str]] = None) -> bool:
    """Pause dispatch and stop every worker from reserving new messages.

    Returns True when the maintenance flag was stored (Redis reachable); the consumer
    cancellation itself is best-effort — a broker that won't answer must not block a deploy.
    """
    seconds = max(1, int(pause_seconds))
    stored = False
    client = _redis_client()
    if client is not None:
        try:
            client.set(_MAINTENANCE_KEY, MAINTENANCE_REASON, ex=seconds)
            stored = True
        except Exception as e:
            log_warning("Could not set the maintenance flag", exc=e)

    pause_automation(seconds, MAINTENANCE_REASON)

    # Cancel each worker's OWN queues only, and remember the pairs: broadcasting add_consumer on
    # resume would otherwise make e.g. the main worker consume a Selenium lane and break lane
    # isolation. A worker recreated by the deploy simply comes back consuming its configured
    # --queues, so a restore aimed at its old (now gone) hostname is a harmless no-op.
    mapping = worker_queue_map()
    if queues is not None:
        wanted = set(queues)
        mapping = {w: [q for q in qs if q in wanted] for w, qs in mapping.items()}
    _store_consumer_snapshot(mapping, seconds)

    cancelled = 0
    for worker, worker_queues in mapping.items():
        for queue in worker_queues:
            try:
                _control().cancel_consumer(queue, destination=[worker])
                cancelled += 1
            except Exception as e:
                log_warning(f"Could not cancel consumer {queue} on {worker}", exc=e)

    log_info(f"Maintenance mode ON for {seconds}s — paused dispatch, cancelled {cancelled} "
             f"queue consumer(s) across {len(mapping)} worker(s)")
    return stored


def drain_workers(timeout_seconds: int = DEFAULT_DRAIN_TIMEOUT_SECONDS,
                  poll_seconds: int = DEFAULT_POLL_SECONDS,
                  sleep: Any = time.sleep) -> int:
    """Wait for running tasks to finish. Returns how many are STILL running (0 = fully drained).

    A non-zero return is not fatal: with task_acks_late those tasks are re-queued when the
    worker dies and re-run on the new one.
    """
    deadline = time.monotonic() + max(0, int(timeout_seconds))
    remaining = active_task_count()
    while remaining > 0 and time.monotonic() < deadline:
        log_info(f"Draining Celery workers — {remaining} task(s) still running")
        sleep(max(1, int(poll_seconds)))
        remaining = active_task_count()
    if remaining:
        log_warning(f"Drain timed out with {remaining} task(s) still running — they will be "
                    "re-queued on shutdown (task_acks_late)")
    else:
        log_info("Celery workers drained — no tasks running")
    return remaining


def end_maintenance(queues: Optional[Iterable[str]] = None) -> bool:
    """Restore consumers and lift the deploy pause. Returns True if the pause was ours to lift."""
    snapshot = _load_consumer_snapshot()
    wanted = set(queues) if queues is not None else None
    for worker, worker_queues in snapshot.items():
        for queue in worker_queues:
            if wanted is not None and queue not in wanted:
                continue
            try:
                _control().add_consumer(queue, destination=[worker])
            except Exception as e:
                log_warning(f"Could not restore consumer {queue} on {worker}", exc=e)

    client = _redis_client()
    if client is not None:
        try:
            client.delete(_MAINTENANCE_KEY, _CONSUMERS_KEY)
        except Exception as e:
            log_warning("Could not clear the maintenance flag", exc=e)

    # Only lift a pause WE set — a 429 breaker pause or an operator's manual pause has to
    # survive the deploy, otherwise a release would silently un-throttle a rate-limited account.
    reason = automation_pause_reason()
    if reason is not None and reason != MAINTENANCE_REASON:
        log_info(f"Maintenance mode OFF — leaving the existing '{reason}' pause in place "
                 f"({automation_pause_remaining()}s left)")
        return False
    resume_automation()
    log_info("Maintenance mode OFF — dispatch resumed")
    return True


def status() -> Dict[str, Any]:
    return {
        "maintenance": is_maintenance_mode(),
        "maintenance_remaining": maintenance_remaining(),
        "pause_reason": automation_pause_reason(),
        "pause_remaining": automation_pause_remaining(),
        "active_tasks": active_task_count(),
        "worker_queues": worker_queue_map(),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="cqc_lem.utilities.maintenance",
                                     description="Celery deploy maintenance mode (issue #549)")
    sub = parser.add_subparsers(dest="command", required=True)

    begin = sub.add_parser("begin", help="pause dispatch and stop consuming new tasks")
    begin.add_argument("--pause-seconds", type=int, default=DEFAULT_PAUSE_SECONDS)

    drain = sub.add_parser("drain", help="wait for in-flight tasks to finish")
    drain.add_argument("--timeout", type=int, default=DEFAULT_DRAIN_TIMEOUT_SECONDS)
    drain.add_argument("--poll", type=int, default=DEFAULT_POLL_SECONDS)

    sub.add_parser("end", help="restore consumers and lift the deploy pause")
    sub.add_parser("status", help="print the current maintenance/pause/active-task state")

    args = parser.parse_args(argv)

    if args.command == "begin":
        begin_maintenance(pause_seconds=args.pause_seconds)
        return 0
    if args.command == "drain":
        # Non-zero tells deploy.sh the drain timed out so it can log it; the deploy continues
        # either way because acks_late re-queues whatever is still running.
        return 1 if drain_workers(timeout_seconds=args.timeout, poll_seconds=args.poll) else 0
    if args.command == "end":
        end_maintenance()
        return 0
    print(json.dumps(status(), default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
