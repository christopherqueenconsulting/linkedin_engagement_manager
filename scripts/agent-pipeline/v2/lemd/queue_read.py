"""Read-only queue-state helpers for callers outside the daemon process.

`scripts/triage_issues.py` runs from a host cron clone with no app env, and it must never WRITE to
`v2/state/queue.db` — the daemon is the only writer, and this database is disposable by
construction (see `db.py`'s own module docstring: every row is re-derivable from GitHub). This
module opens the same file in SQLite's read-only URI mode and delegates straight to
`db.wip_count()` — the daemon's OWN definition of "work in flight" (PR-only; an `agent:ready`
ISSUE is a queue entry, not work in flight, per `db.py`'s own note on `wip_count()`) — so the
hourly triage admission cap (`docs/agent-pipeline-v2.md`, Part A of the hourly-triage plan) can
never disagree with the concurrency gate it is trying to stay under.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from . import db


def read_inflight_count(queue_db_path: str | Path) -> Optional[int]:
    """Read the daemon's own in-flight PR count from its queue database, read-only.

    Args:
        queue_db_path: Filesystem path to the daemon's `queue.db`.

    Returns:
        The count, or `None` if the database is missing or unreadable. Callers must fail CLOSED
        on `None` — never assume zero in-flight — the same "unreadable is untrusted" rule
        `triage_issues.py` already applies to author standing.
    """
    path = Path(queue_db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            return db.wip_count(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None
