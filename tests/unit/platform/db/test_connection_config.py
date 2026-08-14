"""Unit tests for MySQL connection-argument resolution (issue #1319).

The connector calls `int(port)` inside its own `connect()`, so a missing MYSQL_PORT raised
`TypeError: int() argument ... not 'NoneType'` — not a `mysql.connector.Error`, therefore invisible
to every caller's `except mysql.connector.Error`. These pin the port down before it gets there.
"""

from unittest.mock import patch

import pytest

from cqc_lem.platform.db import connection as db

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _direct_connection_env(monkeypatch):
    """Keeps config resolution on the env-var path, and off the pool.

    Pooling is left ON by default (`MYSQL_POOL_ENABLED` unset resolves to True, which is what CI's
    empty `.env` gives), and `_get_pooled_connection()` then builds a real `MySQLConnectionPool`
    whose `add_connection()` opens a genuine socket to MYSQL_HOST — I/O this lane is not allowed to
    do — and leaves that pool bound to the process for every later test in the worker. The port is
    resolved in `_get_mysql_config()` before either branch, so the direct path proves the same
    thing without any of that. `test_connection_pooling.py` owns the pooled path.
    """
    db.reset_connection_pool()
    monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", False)
    monkeypatch.setattr(db, "AWS_MYSQL_SECRET_NAME", None)
    monkeypatch.setattr(db, "AWS_REGION", None)
    monkeypatch.setattr(db, "MYSQL_HOST", "mysql-host")
    monkeypatch.setattr(db, "MYSQL_USER", "lem")
    monkeypatch.setattr(db, "MYSQL_PASSWORD", "secret")
    monkeypatch.setattr(db, "MYSQL_DATABASE", "lem_db")
    yield
    db.reset_connection_pool()


class TestResolveMysqlPort:
    def test_unset_falls_back_to_the_default_port(self):
        assert db._resolve_mysql_port(None) == db.DEFAULT_MYSQL_PORT

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_falls_back_to_the_default_port(self, raw):
        assert db._resolve_mysql_port(raw) == db.DEFAULT_MYSQL_PORT

    def test_string_is_parsed(self):
        assert db._resolve_mysql_port(" 3307 ") == 3307

    def test_int_passes_through(self):
        # The AWS secret path hands back whatever the secret stored, which may already be a number.
        assert db._resolve_mysql_port(3308) == 3308

    def test_unparseable_value_warns_and_falls_back(self):
        with patch.object(db, "log_warning") as warn:
            assert db._resolve_mysql_port("mysql_db:3306") == db.DEFAULT_MYSQL_PORT
        assert warn.called, "a garbage MYSQL_PORT is a misconfiguration, not a silent default"

    def test_unset_does_not_warn(self):
        # Nothing is wrong with an environment that never exported MYSQL_PORT.
        with patch.object(db, "log_warning") as warn:
            db._resolve_mysql_port(None)
        warn.assert_not_called()


class TestGetMysqlConfig:
    def test_port_is_always_an_int(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_PORT", "3306")
        assert db._get_mysql_config()["port"] == 3306

    def test_missing_port_does_not_reach_the_connector_as_none(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_PORT", None)
        assert db._get_mysql_config()["port"] == db.DEFAULT_MYSQL_PORT


class TestGetDbConnection:
    def test_missing_port_connects_instead_of_raising_typeerror(self, monkeypatch,
                                                               mock_database_connection):
        """The regression itself: `int(None)` escaped `except mysql.connector.Error` everywhere."""
        monkeypatch.setattr(db, "MYSQL_PORT", None)
        with patch("mysql.connector.connect") as connect:
            connect.return_value = mock_database_connection["connection"]
            assert db.get_db_connection() is mock_database_connection["connection"]
        assert connect.call_args.kwargs["port"] == db.DEFAULT_MYSQL_PORT
