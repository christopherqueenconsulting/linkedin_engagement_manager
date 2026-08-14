"""Unit tests for MySQL connection-argument resolution (issue #1319).

The connector calls `int(port)` inside its own `connect()`, so a missing MYSQL_PORT raised
`TypeError: int() argument ... not 'NoneType'` — not a `mysql.connector.Error`, therefore invisible
to every caller's `except mysql.connector.Error`. These pin the port down before it gets there.
"""

from unittest.mock import patch

import mysql.connector
import pytest

from cqc_lem.platform.db import connection as db

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _direct_connection_env(monkeypatch):
    """Keeps config resolution on the env-var path, and off the pool.

    `AWS_MYSQL_SECRET_NAME` is the one that matters: a dev `.env` that sets it sends
    `_get_mysql_config()` into `get_aws_ssm_secret()` — real network I/O this lane is not allowed to
    do — and the secret's own port would then decide what these assert on. Pinning the four
    env-var globals keeps the config under test fully declared here.

    Pooling is already OFF suite-wide (`_db_pool_disabled_by_default` in tests/conftest.py, #555);
    it is re-pinned locally so this file states the branch it exercises rather than inheriting it.
    The port is resolved in `_get_mysql_config()` before either branch, so the direct path proves
    the same thing; `test_connection_pooling.py` owns the pooled path.
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

    # Both arms of the `except (TypeError, ValueError)`: a non-numeric string raises ValueError,
    # a value of a type int() refuses outright raises TypeError. The TypeError arm is the one
    # #1319 was about, so it is covered here rather than assumed. (Bytes are NOT such a value —
    # `int(b"3306")` is 3306 — so a secret store handing back bytes parses, it does not fall back.)
    @pytest.mark.parametrize("raw", ["mysql_db:3306", ["3306"]])
    def test_unparseable_value_warns_and_falls_back(self, raw):
        with patch.object(db, "log_warning") as warn:
            assert db._resolve_mysql_port(raw) == db.DEFAULT_MYSQL_PORT
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


class TestUnitLaneNeverOpensASocket:
    """The lane-wide guard (`_no_real_mysql`, tests/unit/conftest.py) that #1496 added.

    A unit test that forgets to stub a database collaborator used to reach a real connector call:
    in CI that raised the `TypeError` this file exists for, and on a dev box running the compose
    stack it reached the LIVE database. Neither is a unit test.
    """

    def test_an_unmocked_connection_fails_as_a_connector_error(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_PORT", None)
        # mysql.connector.Error is what every repository reader already answers with its own
        # fallback, so the guard leaves the code under test on the branch CI always took.
        with pytest.raises(mysql.connector.Error):
            db.get_db_connection()

    def test_the_guard_is_not_a_typeerror(self, monkeypatch):
        """The distinction #1319/#1496 turned on.

        A TypeError escapes `except mysql.connector.Error` and surfaces as a captured production
        exception instead of a handled connection failure.
        """
        monkeypatch.setattr(db, "MYSQL_PORT", None)
        try:
            db.get_db_connection()
        except mysql.connector.Error:
            pass
        except TypeError as err:  # pragma: no cover - the failure this asserts against
            pytest.fail(f"the unit lane produced an unhandleable {err!r}")
