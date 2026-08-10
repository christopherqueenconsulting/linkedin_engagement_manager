"""SQLite state for the v2 agent-pipeline daemon.

Why SQLite and not the app's MySQL: the pipeline is what *ships* the app, so coupling its liveness
to the app stack would mean a `docker compose down` during a deploy stalls the thing performing the
deploy. Why not flat files (what v1 uses): v1's counters cannot express an atomic claim, so it needs
per-branch `flock`s layered on top, and its state has no TTL — 216 lock files and 48 orphaned
counters had accumulated by the time v2 was designed.

Two writers touch this file: the webhook receiver (INSERTs into `events` only) and the daemon
(everything else). WAL mode plus a busy timeout is sufficient for that shape at this volume, and
the DB is disposable by construction — every row is re-derivable from GitHub, so a corrupt database
is deleted and reconciled rather than repaired.

Deliberately NOT stored here: run budgets. Those live in the TSV ledger (`lib/ledger.sh`) so that
v1 and v2 read and write the same counters byte-for-byte during migration — a rollback mid-flight
must not lose or double-count an item's attempts. Two sources of truth for a budget is how you get
a PR parked at 2 attempts and another retried forever.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1

# States. An item in a `wait_*` state costs the scheduler nothing until an event marks it dirty or
# its TTL fires — that is the whole point of v2, because v1 spent 75-84% of its ticks re-asking
# GitHub questions whose answers had not changed.
STATE_READY = "ready"
STATE_CLAIMED = "claimed"
STATE_RUNNING = "running"
STATE_WAIT_CI = "awaiting_ci"
STATE_WAIT_REVIEW = "awaiting_review"
STATE_WAIT_QUEUE = "awaiting_queue"
STATE_PARKED = "parked"
STATE_MERGED = "merged"
STATE_CLOSED = "closed"

TERMINAL_STATES = frozenset({STATE_MERGED, STATE_CLOSED})
#: States the scheduler may pick work from. `claimed`/`running` are excluded because something
#: already owns them; the wait states are excluded because only an event or TTL may revive them.
DISPATCHABLE_STATES = frozenset({STATE_READY})
#: Every non-terminal state must be leaveable by BOTH an event and a TTL, or an item can wedge
#: forever in a state nothing is watching. `startup_recover()` enforces the `claimed` half.
WAIT_STATES = frozenset({STATE_WAIT_CI, STATE_WAIT_REVIEW, STATE_WAIT_QUEUE, STATE_PARKED})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id              INTEGER PRIMARY KEY,
  kind            TEXT    NOT NULL CHECK (kind IN ('issue','pr')),
  number          INTEGER NOT NULL,
  issue_number    INTEGER,
  branch          TEXT,
  head_sha        TEXT,
  state           TEXT    NOT NULL,
  wait_reason     TEXT,
  parked_reason   TEXT,
  priority        INTEGER NOT NULL DEFAULT 2,
  risk            TEXT    NOT NULL DEFAULT 'none',
  model_hint      TEXT,
  wake_at         INTEGER,
  ready_since     INTEGER,
  last_comment_id INTEGER,
  labels_json     TEXT,
  dirty           INTEGER NOT NULL DEFAULT 0,
  updated_at      INTEGER NOT NULL,
  UNIQUE (kind, number)
);

-- One active run per branch. This replaces v1's `locks/br-*.lock` flocks for v2's own dispatches:
-- a partial unique index makes "two workers on one branch" unrepresentable rather than merely
-- guarded, so a claim cannot race even if two scheduler passes overlap.
CREATE UNIQUE INDEX IF NOT EXISTS items_active_branch
  ON items(branch) WHERE state IN ('claimed','running') AND branch IS NOT NULL;

CREATE INDEX IF NOT EXISTS items_state ON items(state);
CREATE INDEX IF NOT EXISTS items_wake  ON items(wake_at) WHERE wake_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY,
  delivery_id  TEXT UNIQUE,
  event        TEXT NOT NULL,
  action       TEXT,
  number       INTEGER,
  head_sha     TEXT,
  payload      TEXT,
  received_at  INTEGER NOT NULL,
  processed_at INTEGER
);
CREATE INDEX IF NOT EXISTS events_unprocessed ON events(processed_at) WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS runs (
  id           INTEGER PRIMARY KEY,
  item_id      INTEGER REFERENCES items(id),
  mode         TEXT NOT NULL,
  lane         TEXT,
  model        TEXT,
  route_reason TEXT,
  pid          INTEGER,
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER,
  rc           INTEGER,
  log_path     TEXT
);
CREATE INDEX IF NOT EXISTS runs_open ON runs(ended_at) WHERE ended_at IS NULL;

-- Four scalars, not a config store: heartbeat, last_webhook_at, missed_event_count, schema_version.
-- The adversarial review flagged that a general kv table inside a queue DB grows into a second,
-- undocumented config surface; the daemon reads its knobs from config.env like v1 does.
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the queue database, creating and migrating it if needed.

    Args:
        path: Filesystem path to the SQLite file; parent directories are created.

    Returns:
        A connection with WAL enabled and a busy timeout, rows as `sqlite3.Row`.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: explicit transactions via `transaction()` below, so a claim is one
    # statement in one transaction rather than whatever the driver decides to batch.
    conn = sqlite3.connect(str(p), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO kv(k, v) VALUES('schema_version', ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (str(SCHEMA_VERSION),),
    )
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one IMMEDIATE transaction, rolling back on error.

    IMMEDIATE (not deferred) so the write lock is taken up front: the claim path must not discover
    a conflict halfway through and leave a half-applied state transition behind.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Read one daemon scalar."""
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Write one daemon scalar."""
    conn.execute(
        "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(value)),
    )


def upsert_item(
    conn: sqlite3.Connection,
    *,
    kind: str,
    number: int,
    state: str,
    now: int | None = None,
    **fields: Any,
) -> int:
    """Create or update an item, returning its row id.

    Only the columns passed in `fields` are touched on update, so a webhook carrying just a head SHA
    cannot blank the branch an earlier reconcile established.
    """
    now = int(now if now is not None else time.time())
    cols = {k: v for k, v in fields.items() if k in _ITEM_COLUMNS}
    row = conn.execute("SELECT id FROM items WHERE kind=? AND number=?", (kind, number)).fetchone()
    if row is None:
        cols.setdefault("ready_since", now)
        names = ["kind", "number", "state", "updated_at", *cols]
        conn.execute(
            f"INSERT INTO items({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            [kind, number, state, now, *cols.values()],
        )
        return int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    sets = ["state=?", "updated_at=?"] + [f"{c}=?" for c in cols]
    conn.execute(
        f"UPDATE items SET {', '.join(sets)} WHERE id=?",
        [state, now, *cols.values(), row["id"]],
    )
    return int(row["id"])


_ITEM_COLUMNS = frozenset(
    {
        "issue_number", "branch", "head_sha", "wait_reason", "parked_reason", "priority",
        "risk", "model_hint", "wake_at", "ready_since", "last_comment_id", "labels_json", "dirty",
    }
)


def get_item(conn: sqlite3.Connection, kind: str, number: int) -> sqlite3.Row | None:
    """Fetch one item by its GitHub identity."""
    return conn.execute("SELECT * FROM items WHERE kind=? AND number=?", (kind, number)).fetchone()


def claim_item(conn: sqlite3.Connection, item_id: int, *, now: int | None = None) -> bool:
    """Atomically move a `ready`, non-dirty item to `claimed`.

    Returns:
        True when this caller won the claim; False when another pass got there first, the item went
        dirty (an event arrived and it must be re-observed before acting), or its branch already has
        an active run.

    The `dirty=0` predicate is the important half: acting on an item whose state GitHub has just
    changed is how v1 re-armed auto-merge on a PR another slot was busy parking.
    """
    now = int(now if now is not None else time.time())
    try:
        with transaction(conn):
            cur = conn.execute(
                "UPDATE items SET state=?, updated_at=? WHERE id=? AND state=? AND dirty=0",
                (STATE_CLAIMED, now, item_id, STATE_READY),
            )
            return cur.rowcount == 1
    except sqlite3.IntegrityError:
        # items_active_branch rejected it: another item on the same branch is already running.
        return False


def mark_dirty(conn: sqlite3.Connection, kind: str, number: int, *, now: int | None = None) -> None:
    """Flag an item for re-observation (a webhook arrived, or the reconciler saw drift)."""
    now = int(now if now is not None else time.time())
    conn.execute(
        "UPDATE items SET dirty=1, updated_at=? WHERE kind=? AND number=?", (now, kind, number)
    )


def dispatchable(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Items the scheduler may act on now, cheapest-to-unblock first.

    Wait-state and parked items are absent by construction, which is what removes v1's head-of-line
    blocking: a PR waiting on the merge queue is not a candidate, so it cannot starve the ones
    behind it.
    """
    return list(
        conn.execute(
            "SELECT * FROM items WHERE state IN (%s) AND dirty=0 ORDER BY priority ASC, ready_since ASC"
            % ",".join("?" * len(DISPATCHABLE_STATES)),
            tuple(sorted(DISPATCHABLE_STATES)),
        ).fetchall()
    )


def due_items(conn: sqlite3.Connection, *, now: int | None = None) -> list[sqlite3.Row]:
    """Wait-state items whose TTL has expired and that must be re-polled once.

    Scoped to `WAIT_STATES` on purpose. Nothing clears `wake_at` when an item leaves a wait state,
    so an unscoped query keeps returning every PR that ever waited — including merged ones — and the
    TTL sweep grows into exactly the unconditional re-polling v2 exists to remove.
    """
    now = int(now if now is not None else time.time())
    placeholders = ",".join("?" * len(WAIT_STATES))
    return list(
        conn.execute(
            "SELECT * FROM items WHERE wake_at IS NOT NULL AND wake_at <= ? "
            f"AND state IN ({placeholders}) ORDER BY wake_at ASC",
            (now, *sorted(WAIT_STATES)),
        ).fetchall()
    )


def record_event(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    event: str,
    action: str | None,
    number: int | None,
    head_sha: str | None,
    payload: str,
    now: int | None = None,
) -> bool:
    """Append a webhook delivery, ignoring duplicates.

    Returns:
        True when the row was new; False when this delivery GUID has already been recorded (GitHub
        retries, and a tunnel can replay — reprocessing must be a no-op, not a second dispatch).
    """
    now = int(now if now is not None else time.time())
    cur = conn.execute(
        "INSERT OR IGNORE INTO events(delivery_id, event, action, number, head_sha, payload, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (delivery_id, event, action, number, head_sha, payload, now),
    )
    return cur.rowcount == 1


def unprocessed_events(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Deliveries the daemon has not yet folded into item state."""
    return list(
        conn.execute(
            "SELECT * FROM events WHERE processed_at IS NULL ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
    )


def mark_events_processed(
    conn: sqlite3.Connection, ids: Sequence[int], *, now: int | None = None
) -> None:
    """Mark deliveries as folded in."""
    if not ids:
        return
    now = int(now if now is not None else time.time())
    conn.executemany("UPDATE events SET processed_at=? WHERE id=?", [(now, i) for i in ids])


def start_run(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    mode: str,
    pid: int | None = None,
    lane: str | None = None,
    model: str | None = None,
    route_reason: str | None = None,
    log_path: str | None = None,
    now: int | None = None,
) -> int:
    """Open a run row and move its item to `running`."""
    now = int(now if now is not None else time.time())
    conn.execute(
        "INSERT INTO runs(item_id, mode, lane, model, route_reason, pid, started_at, log_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, mode, lane, model, route_reason, pid, now, log_path),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute("UPDATE items SET state=?, updated_at=? WHERE id=?", (STATE_RUNNING, now, item_id))
    return run_id


def finish_run(
    conn: sqlite3.Connection, run_id: int, rc: int, *, now: int | None = None
) -> None:
    """Close a run row. The item's next state is the observer's decision, not this function's."""
    now = int(now if now is not None else time.time())
    conn.execute("UPDATE runs SET ended_at=?, rc=? WHERE id=?", (now, rc, run_id))


def startup_recover(conn: sqlite3.Connection, *, now: int | None = None) -> dict[str, int]:
    """Repair state left behind by a crash, so restart is always safe.

    Two repairs, both from the "every state needs an exit" invariant:

    * A run whose process is gone is closed with rc=-9 and its item marked dirty, so the next
      observation decides its fate from GitHub rather than from a half-finished local guess.
    * An item stuck in `claimed` (the daemon died between claiming and spawning) returns to `ready`.
      Without this the partial unique index would keep its branch locked forever.

    Returns:
        Counts keyed `runs_closed` and `claims_released`, for the startup log line.
    """
    now = int(now if now is not None else time.time())
    closed = 0
    for row in conn.execute("SELECT id, pid FROM runs WHERE ended_at IS NULL").fetchall():
        if row["pid"] and _pid_alive(int(row["pid"])):
            continue
        conn.execute("UPDATE runs SET ended_at=?, rc=-9 WHERE id=?", (now, row["id"]))
        closed += 1
    conn.execute(
        "UPDATE items SET dirty=1, state=?, updated_at=? WHERE state=? "
        "AND id NOT IN (SELECT item_id FROM runs WHERE ended_at IS NULL AND item_id IS NOT NULL)",
        (STATE_READY, now, STATE_RUNNING),
    )
    cur = conn.execute(
        "UPDATE items SET state=?, dirty=1, updated_at=? WHERE state=?",
        (STATE_READY, now, STATE_CLAIMED),
    )
    return {"runs_closed": closed, "claims_released": cur.rowcount}


def _pid_alive(pid: int) -> bool:
    """True when a process with this pid exists (any owner)."""
    return Path(f"/proc/{pid}").exists()


def counts_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    """Item counts per state — the status endpoint's headline numbers."""
    return {
        r["state"]: r["n"]
        for r in conn.execute("SELECT state, COUNT(*) AS n FROM items GROUP BY state").fetchall()
    }
