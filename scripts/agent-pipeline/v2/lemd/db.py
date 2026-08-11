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

SCHEMA_VERSION = 3

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

#: "Not the pipeline's business." A fork PR, a release-please PR, an item with no `agent:*` label,
#: an issue with neither `agent:ready` nor `agent:working` — none of these was escalated to anyone,
#: and none of them should be.
#:
#: They used to be written as `parked`, which made the queue claim something false. `parked` means
#: "the pipeline stopped and ASKED A HUMAN": it carries a Decision Comment, the hold labels, an
#: assignee and a disarmed auto-merge. These four carry none of that, because `decide()` returned
#: ACT_NONE and no action ever ran — so an operator reading `parked` saw a question that had never
#: been posed, and the daemon's own `ACT_PARK` branch was unreachable dead code.
STATE_IGNORED = "ignored"

#: The pipeline has stopped asking. Reached when an item has been parked for the SAME reason
#: `LEMD_MAX_PARK_LAPS` times: the question has been posed and answered and posed again, and a
#: further identical Decision Comment has never once been the thing that unblocked anything.
#:
#: Terminal for the pipeline, NOT for the work. Nothing here closes an issue or a PR — that stays
#: the owner's call. Removing the `agent:abandoned` label revives the item and clears its history.
STATE_ABANDONED = "abandoned"

TERMINAL_STATES = frozenset({STATE_MERGED, STATE_CLOSED, STATE_ABANDONED})
#: States where something already owns the item, so observation must not move it (see upsert_item).
ACTIVE_STATES = frozenset({STATE_CLAIMED, STATE_RUNNING})
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
  -- The action `observe()` decided on but the scheduler has not yet had a free slot for. It must be
  -- DURABLE: an item left `ready` with the decision held only in memory is a dead end, because
  -- nothing marks it dirty again and its `wake_at` is deliberately cleared. Storing the verdict is
  -- what lets a pass that ran out of slots be resumed by the next one.
  pending_mode    TEXT,
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
  -- NOT NULL because SQLite treats NULLs as DISTINCT: a nullable unique column silently allows
  -- unlimited rows with no delivery id, bypassing dedupe entirely.
  delivery_id  TEXT NOT NULL UNIQUE,
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
  -- /proc/<pid>/stat field 22. Paired with pid so a recycled pid cannot make a dead run look alive.
  pid_start    TEXT,
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER,
  rc           INTEGER,
  log_path     TEXT
);
CREATE INDEX IF NOT EXISTS runs_open ON runs(ended_at) WHERE ended_at IS NULL;

-- Four scalars, not a config store: heartbeat, last_webhook_at, missed_event_count, schema_version.
-- The adversarial review flagged that a general kv table inside a queue DB grows into a second,
-- undocumented config surface; the daemon reads its knobs from config.env like v1 does.
CREATE TABLE IF NOT EXISTS park_history (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  number     INTEGER NOT NULL,
  reason     TEXT NOT NULL,
  head_sha   TEXT NOT NULL DEFAULT '',
  event      TEXT NOT NULL CHECK (event IN ('park','unpark')),
  at         INTEGER NOT NULL
);
-- One lap per (item, reason, head). The uniqueness is the counting rule, not an optimisation: an
-- item re-parked at the SAME head for the SAME reason has not gone round again, it is the same
-- park being re-observed, and `park.sh` already dedupes its comment on exactly that key. Without
-- this the 6-hourly ttl_parked re-decision would inflate every counter on its own.
CREATE UNIQUE INDEX IF NOT EXISTS park_history_lap
  ON park_history(kind, number, reason, head_sha, event);

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
    _migrate(conn)
    conn.execute(
        "INSERT INTO kv(k, v) VALUES('schema_version', ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (str(SCHEMA_VERSION),),
    )
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to a database created by an older schema.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a new column is
    invisible to every box that installed an earlier version — and the daemon would then fail on
    every write instead of on the one upgrade. Additive only: the queue is disposable, but taking it
    down on restart is not, and a daemon that cannot start is a pipeline that cannot roll back.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "pending_mode" not in have:
        conn.execute("ALTER TABLE items ADD COLUMN pending_mode TEXT")


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

    **State is NOT overwritten while an item is `claimed` or `running`.** The branch index prevents
    two ROWS sharing a branch; it cannot stop one row being walked back to `ready` and re-claimed.
    Without this guard the sequence is: item is `running` → a webhook arrives → `observe()` upserts
    it as `ready` → the next pass claims it and spawns a SECOND agent on the same branch and
    worktree, which is precisely the double-dispatch the claim model exists to make impossible.
    Observation may still update `head_sha`, `labels_json`, `dirty` and the rest — only the state
    transition is deferred until the run finishes and `finish_run()` hands the decision back.
    """
    now = int(now if now is not None else time.time())
    cols = {k: v for k, v in fields.items() if k in _ITEM_COLUMNS}
    row = conn.execute(
        "SELECT id, state FROM items WHERE kind=? AND number=?", (kind, number)
    ).fetchone()
    if row is None:
        cols.setdefault("ready_since", now)
        names = ["kind", "number", "state", "updated_at", *cols]
        conn.execute(
            f"INSERT INTO items({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            [kind, number, state, now, *cols.values()],
        )
        return int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])

    if row["state"] in ACTIVE_STATES and state != row["state"]:
        # Something owns this item. Record the observation, refuse the transition.
        sets = ["updated_at=?"] + [f"{c}=?" for c in cols]
        conn.execute(
            f"UPDATE items SET {', '.join(sets)} WHERE id=?", [now, *cols.values(), row["id"]]
        )
        return int(row["id"])

    sets = ["state=?", "updated_at=?"] + [f"{c}=?" for c in cols]
    conn.execute(
        f"UPDATE items SET {', '.join(sets)} WHERE id=?",
        [state, now, *cols.values(), row["id"]],
    )
    return int(row["id"])


def force_state(
    conn: sqlite3.Connection, item_id: int, state: str, *, now: int | None = None, **fields: Any
) -> None:
    """Move an item's state regardless of the `upsert_item` active-state guard.

    The guard exists to stop OBSERVATION walking a running item backwards. The run lifecycle itself
    (finish, timeout, crash recovery) legitimately needs to move it, so those paths call this
    explicitly — making every state change out of `claimed`/`running` easy to find and audit.
    """
    now = int(now if now is not None else time.time())
    cols = {k: v for k, v in fields.items() if k in _ITEM_COLUMNS}
    sets = ["state=?", "updated_at=?"] + [f"{c}=?" for c in cols]
    conn.execute(
        f"UPDATE items SET {', '.join(sets)} WHERE id=?", [state, now, *cols.values(), item_id]
    )


_ITEM_COLUMNS = frozenset(
    {
        "issue_number", "branch", "head_sha", "wait_reason", "parked_reason", "priority",
        "risk", "model_hint", "wake_at", "ready_since", "last_comment_id", "labels_json", "dirty",
        "pending_mode",
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

    **Finishing work outranks starting work.** `start` sorts last, ahead of nothing. `ready_since`
    alone put a 40-issue backlog — all stamped with the same older timestamp — in front of every
    PR that had just become dispatchable, so six PRs un-parked at 21:47 would have waited for that
    entire backlog to drain before getting a slot. The WIP gate does not catch this: it counts PRs
    in flight, and a PR sitting `ready` behind the queue is not in flight, so the gate reads zero
    and lets the starts through. This is v1's "new work never outruns merge throughput" rule
    applied where the ordering is actually decided.
    """
    return list(
        conn.execute(
            "SELECT * FROM items WHERE state IN (%s) AND dirty=0 AND pending_mode IS NOT NULL "
            "ORDER BY (pending_mode = 'start') ASC, priority ASC, ready_since ASC"
            % ",".join("?" * len(DISPATCHABLE_STATES)),
            tuple(sorted(DISPATCHABLE_STATES)),
        ).fetchall()
    )


#: States a TTL may fire for: the wait states, plus `running` — whose deadline is its run timeout.
#: Scoped deliberately. Nothing clears `wake_at` when an item leaves these, so an UNSCOPED query
#: keeps returning every PR that ever waited (merged ones included) and the TTL sweep grows back
#: into the unconditional re-polling v2 exists to remove.
TTL_STATES = frozenset({*WAIT_STATES, STATE_RUNNING})


def due_items(conn: sqlite3.Connection, *, now: int | None = None) -> list[sqlite3.Row]:
    """Items whose TTL has expired: a wait state to re-poll, or a run that overran its deadline.

    `running` is included because `start_run()` stamps the run's deadline as `wake_at`. That is the
    TTL edge which keeps `running` from being a dead end — an agent whose completion is never
    observed (daemon killed between the child exiting and the state write) would otherwise be
    invisible to both `dispatchable()` and this query, wedging the item and holding its branch.
    """
    now = int(now if now is not None else time.time())
    placeholders = ",".join("?" * len(TTL_STATES))
    return list(
        conn.execute(
            "SELECT * FROM items WHERE wake_at IS NOT NULL AND wake_at <= ? "
            f"AND state IN ({placeholders}) ORDER BY wake_at ASC",
            (now, *sorted(TTL_STATES)),
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
    timeout_s: int = 2700,
    now: int | None = None,
) -> int:
    """Open a run row and move its item to `running`.

    `wake_at` is set to the run's deadline so `running` has a TTL edge as well as an event edge.
    Without it, a run whose completion is never observed (daemon killed between the child exiting
    and the state write) leaves the item invisible to both `dispatchable()` and `due_items()` — a
    dead end that also holds its branch, violating the invariant that every non-terminal state must
    be leaveable two ways.
    """
    now = int(now if now is not None else time.time())
    conn.execute(
        "INSERT INTO runs(item_id, mode, lane, model, route_reason, pid, pid_start, started_at, log_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, mode, lane, model, route_reason, pid,
         pid_starttime(pid) if pid else None, now, log_path),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "UPDATE items SET state=?, updated_at=?, wake_at=? WHERE id=?",
        (STATE_RUNNING, now, now + max(60, timeout_s), item_id),
    )
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

    * A run whose process is gone is closed with `RC_VANISHED` and its item marked dirty, so the
      next observation decides its fate from GitHub rather than from a half-finished local guess.
      NOT `RC_KILLED`: nobody killed it, and a reader counting deadline kills must not find this.
    * An item stuck in `claimed` (the daemon died between claiming and spawning) returns to `ready`.
      Without this the partial unique index would keep its branch locked forever.

    Returns:
        Counts keyed `runs_closed` and `claims_released`, for the startup log line.
    """
    now = int(now if now is not None else time.time())
    closed = 0
    for row in conn.execute(
        "SELECT id, pid, pid_start FROM runs WHERE ended_at IS NULL"
    ).fetchall():
        # Identity, not just existence: a recycled pid must read as dead, or the run it replaced
        # stays "alive" forever and its item can never leave `running`.
        if row["pid"] and _pid_alive(int(row["pid"]), row["pid_start"]):
            continue
        conn.execute("UPDATE runs SET ended_at=?, rc=? WHERE id=?",
                     (now, RC_VANISHED, row["id"]))
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


def pid_starttime(pid: int) -> str | None:
    """The process's start time from `/proc/<pid>/stat` field 22, or None if it is gone.

    Recorded alongside the pid because a bare pid is a reuse oracle: pids wrap, and on a box that
    spawns an agent every few minutes a recycled pid makes a dead run look alive forever. Since
    `running` has no other exit edge, that would wedge the item AND its branch permanently.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    # comm (field 2) may contain spaces and parentheses; everything after the final ')' is safe.
    tail = stat.rpartition(")")[2].split()
    # After comm, fields are 1-indexed from `state`: starttime is field 22 overall => index 19 here.
    return tail[19] if len(tail) > 19 else None


def pid_alive(pid: int, starttime: str | None = None) -> bool:
    """Public name for the liveness check — the supervisor needs it to count adopted runs."""
    return _pid_alive(pid, starttime)


def _pid_alive(pid: int, starttime: str | None = None) -> bool:
    """True when this pid is alive AND is the same process we started.

    With `starttime` supplied, a recycled pid reads as dead — which is the correct answer, because
    the run we launched is gone regardless of what now occupies its number.
    """
    current = pid_starttime(pid)
    if current is None:
        return False
    return True if starttime is None else current == starttime


#: What counts as work already in flight for the WIP gate. The wait states are INCLUDED, and that
#: is the whole point: a PR sitting in the merge queue is unfinished work, so starts stay coupled to
#: merge THROUGHPUT rather than to how many agents happen to be idle. Excluding them would let the
#: scheduler open a PR for every ready issue the moment the agents finished writing them — 35 open
#: PRs against a queue that merges one at a time.
#: Run outcomes that are NOT the agent's own exit code. Distinct values because they are distinct
#: events, and collapsing them cost a real misdiagnosis: on 2026-08-10 nine runs closed `-9` with a
#: mean duration of ~675s, which reads exactly like a deadline kill. All nine were orphans adopted
#: across 16 daemon restarts while `config.env` was being edited — durations 149s to 1300s, no
#: fixed deadline anywhere. `rc=-9` made the start lane look like it had a timeout problem it did
#: not have, and that number feeds the judgement about lifting the start-lane throttle (#1311 §1).
RC_KILLED = -9        #: the supervisor stopped it on its deadline (SIGKILL)
RC_VANISHED = -99     #: its process was already gone — an adopted orphan, or a crash recovery

WIP_STATES = frozenset({STATE_CLAIMED, STATE_RUNNING, STATE_WAIT_CI, STATE_WAIT_REVIEW,
                        STATE_WAIT_QUEUE})


def record_park(conn: sqlite3.Connection, kind: str, number: int, reason: str,
                head_sha: str | None = None, *, now: int | None = None) -> None:
    """Record that this item was parked for `reason` at this head.

    `INSERT OR IGNORE`: the unique index is the counting rule, so a re-park at the same head for the
    same reason is silently not a new lap.
    """
    conn.execute(
        "INSERT OR IGNORE INTO park_history (kind, number, reason, head_sha, event, at) "
        "VALUES (?,?,?,?,'park',?)",
        (kind, number, reason or "unknown", head_sha or "",
         int(now if now is not None else time.time())),
    )


def record_unpark(conn: sqlite3.Connection, kind: str, number: int, reason: str,
                  head_sha: str | None = None, *, now: int | None = None) -> None:
    """Record that this item was released from a park."""
    conn.execute(
        "INSERT OR IGNORE INTO park_history (kind, number, reason, head_sha, event, at) "
        "VALUES (?,?,?,?,'unpark',?)",
        (kind, number, reason or "unknown", head_sha or "",
         int(now if now is not None else time.time())),
    )


def park_laps(conn: sqlite3.Connection, kind: str, number: int, reason: str) -> int:
    """How many times this item has been parked for this reason, at distinct heads.

    Counting per REASON rather than per item is deliberate: a PR that parks once for a lint failure
    and once for an exhausted review has not looped, it has had two different problems. A loop is
    the same question asked again.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM park_history "
        "WHERE kind=? AND number=? AND reason=? AND event='park'",
        (kind, number, reason or "unknown"),
    ).fetchone()
    return int(row["n"]) if row else 0


def clear_park_history(conn: sqlite3.Connection, kind: str, number: int) -> None:
    """Forget an item's laps. The owner's escape hatch from `abandoned`."""
    conn.execute("DELETE FROM park_history WHERE kind=? AND number=?", (kind, number))


def wip_count(conn: sqlite3.Connection) -> int:
    """PRs the pipeline is currently carrying.

    Counts PRs only. An `agent:ready` issue is a queue entry, not work in flight, and counting it
    would make the gate close against its own backlog and never open.
    """
    placeholders = ",".join("?" * len(WIP_STATES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM items WHERE kind='pr' AND state IN ({placeholders})",
        tuple(sorted(WIP_STATES)),
    ).fetchone()
    return int(row["n"]) if row else 0


def counts_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    """Item counts per state — the status endpoint's headline numbers."""
    return {
        r["state"]: r["n"]
        for r in conn.execute("SELECT state, COUNT(*) AS n FROM items GROUP BY state").fetchall()
    }


def set_run_lane(conn: sqlite3.Connection, run_id: int, *, lane: str | None,
                 model: str | None, route_reason: str | None) -> None:
    """Attach the lane a run actually used to its row.

    Separate from `start_run` because the answer does not exist yet when the row is opened: the
    daemon spawns the action, and the action's own routing decides the lane. Every column is
    written with COALESCE semantics inverted — a NULL report never overwrites a value already
    there, so a partial meta file cannot erase a good attribution.
    """
    conn.execute(
        "UPDATE runs SET lane=COALESCE(?, lane), model=COALESCE(?, model), "
        "route_reason=COALESCE(?, route_reason) WHERE id=?",
        (lane, model, route_reason, run_id),
    )
