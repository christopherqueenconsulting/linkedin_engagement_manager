"""Every MySQL connection and cursor in LEM comes from here.

`db_cursor` is the one place a connection is checked out and given back. `utilities/db.py` re-exports
this module's names, so both the historical `from cqc_lem.utilities.db import get_db_connection` and
the direct import resolve to the same objects.
"""

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Union

import mysql.connector
from dotenv import load_dotenv
from mysql.connector.abstracts import MySQLConnectionAbstract, MySQLCursorAbstract
from mysql.connector.pooling import CNX_POOL_MAXSIZE, MySQLConnectionPool, PooledMySQLConnection

from cqc_lem.utilities.env_constants import (
    AWS_MYSQL_SECRET_NAME,
    AWS_REGION,
    DB_CONNECT_RETRY_ATTEMPTS,
    DB_CONNECT_RETRY_BACKOFF_SECONDS,
    MYSQL_POOL_ENABLED,
    MYSQL_POOL_SIZE,
)
from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.utils import get_aws_ssm_secret

MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE')
MYSQL_PORT = os.getenv('MYSQL_PORT')

# Loaded AFTER the block above, deliberately. This call moved here from `utilities/db.py` (issue
# #1614), where it ran at the END of that module -- i.e. after this one had already been imported
# and had already read the five names above off the real environment. Hoisting it any earlier would
# let a stale `.env` start deciding which MySQL a dev box connects to, which is a different program.
# It stays because everything that imports the facade relies on it to populate `os.environ` for the
# LATER readers (`env_constants` never calls it).
load_dotenv()

DbConnection = Union[PooledMySQLConnection, MySQLConnectionAbstract]

#: MySQL's own default, and what `.env.example` / docker-compose ship. Used when MYSQL_PORT is
#: unset so an environment that never exported it still connects.
DEFAULT_MYSQL_PORT = 3306

# The connector's client-side errnos for "the server was never reached": the compose DNS name did
# not resolve (2005), the TCP connect failed (2003) or the socket could not be opened at all (2002).
# All three are raised BEFORE the handshake, so no credentials were checked and no statement ran —
# which is what makes retrying them safe. Every server-side errno is deliberately absent: 1045
# (access denied) and 1049 (unknown database) are misconfiguration that a retry only delays.
_UNREACHABLE_ERRNOS = frozenset({2002, 2003, 2005})

# Attempts is operator-tunable and every wait doubles, so cap ONE wait: a mistyped value must not
# park an API request (or a worker slot) for hours. The default schedule (1s, 2s) is untouched.
_MAX_CONNECT_RETRY_DELAY = 30.0


def _db_unreachable(exc: BaseException) -> bool:
    """True only when MySQL was NEVER REACHED (issue #1675).

    It is down, restarting, or its compose DNS name has not come back yet. Nothing was sent on such
    a failure, so retrying cannot run a statement twice. Anything the server itself answered — bad
    credentials, a missing database, a query error — is NOT this and is raised on the first attempt.
    """
    return isinstance(exc, mysql.connector.Error) and getattr(exc, "errno", None) in _UNREACHABLE_ERRNOS

@dataclass
class _PoolState:
    """Per-process pool bookkeeping, held on one mutable object rather than several module globals.

    Rebinding module globals across calls reads as a dead store to static analysis (CodeQL
    py/unused-global-variable) because each assignment is only consumed by a LATER invocation.
    """

    pool: Optional[MySQLConnectionPool] = None
    pid: Optional[int] = None
    config: Optional[dict[str, Any]] = None
    opened: int = 0


_POOL_LOCK = threading.Lock()
_POOL_STATE = _PoolState()


def _resolve_mysql_port(raw: Any) -> int:
    """Turns whatever MYSQL_PORT holds into a port number the connector can use.

    The connector does `int(port)` deep inside `connect()`, so an unset MYSQL_PORT used to raise
    `TypeError: int() argument ... not 'NoneType'` — which is NOT a `mysql.connector.Error`, so it
    escaped every caller's `except mysql.connector.Error` and surfaced as a grouped error-tracking
    issue instead of a connection failure (issue #1319).

    Returns:
        The parsed port, or DEFAULT_MYSQL_PORT when the value is missing or unparseable.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_MYSQL_PORT
    try:
        return int(raw)
    except (TypeError, ValueError):
        # A garbage value IS a misconfiguration worth escalating, unlike an unset one.
        log_warning("MYSQL_PORT is not a number - falling back to the default MySQL port",
                    mysql_port=str(raw), default_port=DEFAULT_MYSQL_PORT)
        return DEFAULT_MYSQL_PORT


def _get_mysql_config() -> dict[str, Any]:
    """Resolves the MySQL connection arguments, preferring the AWS secret when one is configured."""
    global MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE, MYSQL_PORT

    # if MYSQL_USER and MYSQL_PASSWORD are empty try to get it from AWS using get_secret function
    if AWS_MYSQL_SECRET_NAME is not None and AWS_REGION is not None:
        secret_dict = get_aws_ssm_secret(AWS_MYSQL_SECRET_NAME, AWS_REGION)
        MYSQL_HOST = secret_dict['host']
        MYSQL_USER = secret_dict['username']
        MYSQL_PASSWORD = secret_dict['password']
        MYSQL_DATABASE = secret_dict['dbname']
        MYSQL_PORT = secret_dict['port']

    return {
        'host': MYSQL_HOST,
        'user': MYSQL_USER,
        'password': MYSQL_PASSWORD,
        'database': MYSQL_DATABASE,
        'port': _resolve_mysql_port(MYSQL_PORT),
        'time_zone': '+00:00',
    }


def _get_connection_pool(config: dict[str, Any]) -> MySQLConnectionPool:
    """Returns this PROCESS's pool, (re)building it when the pid or the DB config changed.

    Keyed on pid because Celery prefork forks its workers: a pool built before the fork would hand
    the same live socket to parent and child. The pool is created WITHOUT connection kwargs so it
    opens nothing up front — connections are added on demand in _get_pooled_connection() — because
    every app process would otherwise pre-open MYSQL_POOL_SIZE sockets at first use and the sum
    across ~20 processes would blow past MySQL's max_connections.
    """
    pid = os.getpid()
    if _POOL_STATE.pool is None or _POOL_STATE.pid != pid:
        pool_size = max(1, min(MYSQL_POOL_SIZE, CNX_POOL_MAXSIZE))
        _POOL_STATE.pool = MySQLConnectionPool(pool_name=f"cqc-lem-{pid}", pool_size=pool_size)
        _POOL_STATE.pool.set_config(**config)
        _POOL_STATE.pid = pid
        _POOL_STATE.config = config
        _POOL_STATE.opened = 0
    elif config != _POOL_STATE.config:
        # e.g. a rotated AWS secret — pooled connections reconnect with the new config on checkout
        _POOL_STATE.pool.set_config(**config)
        _POOL_STATE.config = config

    return _POOL_STATE.pool


def _get_pooled_connection(config: dict[str, Any]) -> Optional[DbConnection]:
    """Checks a connection out of the process pool, growing it lazily up to its size.

    Returns None when the pool is at capacity so the caller can fall back to an unpooled
    connection instead of failing a task during a fan-out burst.
    """
    with _POOL_LOCK:
        pool = _get_connection_pool(config)
        try:
            return pool.get_connection()
        except mysql.connector.Error:
            # pool exhausted: every connection it has opened is checked out
            if _POOL_STATE.opened >= pool.pool_size:
                log_debug(f"MySQL pool {pool.pool_name} at capacity ({pool.pool_size}) - "
                          f"opening a direct connection for this call")
                return None
            pool.add_connection()
            _POOL_STATE.opened += 1
            return pool.get_connection()


def reset_connection_pool() -> None:
    """Drops this process's pool reference so the next call builds a fresh one (tests/diagnostics).

    Deliberately does NOT close the pooled connections: after a fork those sockets belong to the
    parent process, and closing them there would break the parent's in-flight queries.
    """
    with _POOL_LOCK:
        _POOL_STATE.pool = None
        _POOL_STATE.pid = None
        _POOL_STATE.config = None
        _POOL_STATE.opened = 0


def _open_db_connection(config: dict[str, Any]) -> DbConnection:
    """One attempt at getting a connection: the process pool first, a direct socket as the fallback."""
    if MYSQL_POOL_ENABLED:
        try:
            connection = _get_pooled_connection(config)
            if connection is not None:
                return connection
        except mysql.connector.Error as e:
            if _db_unreachable(e):
                # The pool is fine; the SERVER is not there. A direct connection would fail
                # identically, so raise for the retry above rather than warning about the pool.
                raise
            log_warning("MySQL connection pool unavailable - using a direct connection", exc=e)

    return mysql.connector.connect(**config)


def get_db_connection() -> DbConnection:
    """Establishes a connection to the MySQL database and returns the connection object.

    Connections come from a per-process pool when MYSQL_POOL_ENABLED (the default); calling
    .close() on the returned object returns it to the pool rather than dropping the socket.

    A MySQL that was never reached is retried (see `_db_unreachable`, DB_CONNECT_RETRY_ATTEMPTS):
    the database is a container LEM restarts on its own schedule, and a deploy that briefly took its
    compose DNS name away failed one task outright (issue #1675).

    Raises:
        mysql.connector.Error: If there is an error connecting to the database.
    """
    config = _get_mysql_config()
    attempts = max(1, DB_CONNECT_RETRY_ATTEMPTS)
    backoff = max(0.0, DB_CONNECT_RETRY_BACKOFF_SECONDS)

    # The LAST attempt is the one outside the loop, so its failure raises unchanged — there is no
    # branch here that can fall through without either a connection or the connector's own error.
    for attempt in range(attempts - 1):
        try:
            return _open_db_connection(config)
        except Exception as exc:
            if not _db_unreachable(exc):
                raise
            delay = min(backoff * (2 ** attempt), _MAX_CONNECT_RETRY_DELAY)
            # DEBUG, not a warning: a database restart is expected on every deploy, and a blip we
            # rode out is not a degraded outcome. Exhausting the budget still raises, and whoever
            # owns that call logs it.
            log_debug(f"MySQL is not reachable; retrying the connection in {delay:.0f}s",
                      mysql_host=str(config.get('host')), attempt=attempt + 1, attempts=attempts)
            time.sleep(delay)

    return _open_db_connection(config)


@contextmanager
def db_cursor(*, dictionary: bool = False, commit: bool = False) -> Iterator[MySQLCursorAbstract]:
    """Check out a connection, hand back a cursor, and always give both back.

    This owns the RESOURCE half of a database call and nothing else. It deliberately does NOT catch
    `mysql.connector.Error`: every caller in this module answers a read failure with its own
    fallback — False, None, `[]`, 0 — and a context manager that swallowed the error would have to
    invent one. Callers keep their own `except`, and what they lose is the four lines of ceremony
    this replaces, repeated 417 times.

    It also closes a hole that shape had. Those 417 blocks build the cursor BETWEEN
    `get_db_connection()` and their `try:`, so a failure in `.cursor()` itself skipped the `finally`
    — and `PooledMySQLConnection` has no `__del__`, so that connection never returned to the pool.
    One statement wide, but it drained a pool slot permanently every time it happened. Building the
    cursor inside this function fixes it for every migrated caller at once, which is the point of
    having one place.

    `commit=True` commits only when the body completed. An uncommitted transaction left by a raising
    body is not leaked to the next user of the connection: the pool is built with the connector's
    default `pool_reset_session=True`, so `close()` resets the session (mysql/connector/pooling.py
    :409), and the unpooled fallback connection drops the transaction when its socket closes.

    Args:
        dictionary: Rows come back as dicts keyed by column instead of positional tuples.
        commit: Commit after the body succeeds. Required for writes; a no-op cost for reads.

    Yields:
        The cursor — including its `rowcount` and `lastrowid`, which callers read after `execute`.
    """
    connection = get_db_connection()
    try:
        cursor = connection.cursor(dictionary=dictionary)
    except BaseException:
        # The one path the old shape leaked: no cursor was created, so no `finally` below can run.
        connection.close()
        raise
    try:
        yield cursor
        if commit:
            connection.commit()
    finally:
        cursor.close()
        connection.close()


def to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """The one storage-side timezone conversion (see docs/timezone-contract.md).

    Every scheduling column in this schema (posts.scheduled_time, scheduled_dms.scheduled_time,
    newsletter_editions.scheduled_for, …) holds NAIVE UTC. An aware datetime is converted to UTC;
    a naive one is assumed to already be UTC. Normalizing here rather than at each call site matters
    because mysql-connector serializes a datetime from its wall-clock fields and silently DROPS
    tzinfo — an aware non-UTC value would otherwise be stored as its local wall clock, i.e. off by
    the sender's UTC offset, and the post/DM would fire hours away from what the user scheduled.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
