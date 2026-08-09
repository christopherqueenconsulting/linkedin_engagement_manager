"""Issue #555: the real MySQL connection pool behind get_db_connection(), against a live server.

Unit tests fake the pool; this one proves the parts that only a real server can answer — that a
reused connection still speaks UTC after mysql-connector resets its session, and that a fan-out
burst of checkouts leaves no connection behind on the server.
"""
import os
import threading
import time

import mysql.connector
import pytest

from cqc_lem.platform.db import connection as db

pytestmark = pytest.mark.integration

_POOL_SIZE = 4


def _server_available() -> bool:
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    connection.close()
    return True


@pytest.fixture
def pooled(monkeypatch):
    if not _server_available():
        pytest.skip("no MySQL server available for the pooling integration test")

    db.reset_connection_pool()
    monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", True)
    monkeypatch.setattr(db, "MYSQL_POOL_SIZE", _POOL_SIZE)
    yield
    db.reset_connection_pool()


def _threads_connected() -> int:
    """Connections this test's own database is holding open on the server.

    Counted off `information_schema.processlist` scoped to our schema rather than the server-wide
    `Threads_connected`, because that counter answers for the whole server: under pytest-xdist the
    other workers open and close connections continuously, so a global count would move underneath
    the before/after deltas below and fail a pool that leaked nothing. Each worker gets its own
    database (tests/integration/conftest.py), which makes the scoped count exactly this test's
    connections — the question it was always asking.
    """
    config = db._get_mysql_config()
    connection = mysql.connector.connect(connect_timeout=3, **config)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM information_schema.processlist WHERE db = %s",
                       (config["database"],))
        value = int(cursor.fetchone()[0])
        cursor.close()
        return value
    finally:
        connection.close()


def _settled_threads_connected(ceiling: int) -> int:
    """Threads_connected once the server has reaped the sockets the test closed (it lags briefly)."""
    for _ in range(20):
        count = _threads_connected()
        if count <= ceiling:
            return count
        time.sleep(0.1)
    return _threads_connected()


def _scalar(connection, sql: str):
    cursor = connection.cursor()
    cursor.execute(sql)
    value = cursor.fetchone()[0]
    cursor.close()
    return value


class TestPooledConnectionBehaviour:
    def test_checkout_returns_a_working_pooled_connection(self, pooled):
        connection = db.get_db_connection()
        try:
            assert _scalar(connection, "SELECT 1") == 1
            assert connection.pool_name == f"cqc-lem-{os.getpid()}"
        finally:
            connection.close()

    def test_reused_connection_still_speaks_utc(self, pooled):
        first = db.get_db_connection()
        assert _scalar(first, "SELECT @@session.time_zone") == "+00:00"
        first.close()

        second = db.get_db_connection()
        try:
            # same socket, session reset by the pool — time_zone must be re-applied, or every
            # TIMESTAMP read would silently drift off the naive-UTC storage contract
            assert _scalar(second, "SELECT @@session.time_zone") == "+00:00"
        finally:
            second.close()

    def test_sequential_fanout_reuses_one_server_connection(self, pooled):
        before = _threads_connected()

        for _ in range(100):
            connection = db.get_db_connection()
            assert _scalar(connection, "SELECT 1") == 1
            connection.close()

        after = _settled_threads_connected(before + 1)
        assert after - before <= 1, f"connections leaked: {before} -> {after}"

    def test_concurrent_fanout_stays_within_the_pool(self, pooled):
        before = _threads_connected()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(10):
                    connection = db.get_db_connection()
                    try:
                        assert _scalar(connection, "SELECT 1") == 1
                    finally:
                        connection.close()
            except Exception as e:  # noqa: BLE001 - surfaced as a test failure below
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(_POOL_SIZE * 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        after = _settled_threads_connected(before + _POOL_SIZE)
        assert after - before <= _POOL_SIZE, f"connections leaked: {before} -> {after}"
