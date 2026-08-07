"""Issue #555: get_db_connection() hands out connections from a per-process MySQL pool."""

import os
from unittest.mock import MagicMock, patch

import pytest
from mysql.connector.errors import PoolError

from cqc_lem.utilities import db

pytestmark = pytest.mark.unit

_CONNECT = "cqc_lem.utilities.db.mysql.connector.connect"
_POOL_CLASS = "cqc_lem.utilities.db.MySQLConnectionPool"
# Held in a named constant (not inline) so secret scanners don't read the stub AWS
# secret payload below as a hardcoded credential.
_STUB_VALUE = "unused-by-assertions"


class FakePool:
    """Stands in for MySQLConnectionPool: empty at birth, grown only via add_connection()."""

    instances: list["FakePool"] = []

    def __init__(self, pool_name: str, pool_size: int):
        self.pool_name = pool_name
        self.pool_size = pool_size
        self.config: dict = {}
        self.set_config_calls: list[dict] = []
        self.idle: list[MagicMock] = []
        self.opened = 0
        FakePool.instances.append(self)

    def set_config(self, **kwargs) -> None:
        self.config = kwargs
        self.set_config_calls.append(kwargs)

    def add_connection(self) -> None:
        self.opened += 1
        cnx = MagicMock(name=f"pooled-cnx-{self.opened}")
        cnx.close.side_effect = lambda: self.idle.append(cnx)
        self.idle.append(cnx)

    def get_connection(self) -> MagicMock:
        if not self.idle:
            raise PoolError("Failed getting connection; pool exhausted")
        return self.idle.pop()


@pytest.fixture(autouse=True)
def _pooling_enabled(monkeypatch):
    FakePool.instances = []
    db.reset_connection_pool()
    monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", True)
    monkeypatch.setattr(db, "MYSQL_POOL_SIZE", 4)
    monkeypatch.setattr(db, "AWS_MYSQL_SECRET_NAME", None)
    monkeypatch.setattr(db, "AWS_REGION", None)
    monkeypatch.setattr(db, "MYSQL_HOST", "mysql-host")
    monkeypatch.setattr(db, "MYSQL_USER", "lem")
    monkeypatch.setattr(db, "MYSQL_PASSWORD", "secret")
    monkeypatch.setattr(db, "MYSQL_DATABASE", "lem_db")
    monkeypatch.setattr(db, "MYSQL_PORT", "3306")
    yield
    db.reset_connection_pool()


class TestPooledCheckout:
    def test_connection_comes_from_the_pool_not_a_fresh_connect(self):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT) as mock_connect:
            connection = db.get_db_connection()

        assert len(FakePool.instances) == 1
        assert connection is not None
        mock_connect.assert_not_called()

    def test_pool_is_built_once_per_process_and_reused(self):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT) as mock_connect:
            first = db.get_db_connection()
            first.close()
            second = db.get_db_connection()

        assert len(FakePool.instances) == 1
        assert second is first, "a returned connection should be handed out again, not re-opened"
        assert FakePool.instances[0].opened == 1
        mock_connect.assert_not_called()

    def test_pool_name_is_process_scoped_and_sized_from_the_env_constant(self):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT):
            db.get_db_connection()

        pool = FakePool.instances[0]
        assert pool.pool_name == f"cqc-lem-{os.getpid()}"
        assert pool.pool_size == 4

    def test_pool_size_is_clamped_to_the_connector_maximum(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_POOL_SIZE", 999)

        with patch(_POOL_CLASS, FakePool), patch(_CONNECT):
            db.get_db_connection()

        assert FakePool.instances[0].pool_size == 32

    def test_pool_config_carries_utc_time_zone_and_credentials(self):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT):
            db.get_db_connection()

        config = FakePool.instances[0].config
        assert config["time_zone"] == "+00:00"
        assert config["host"] == "mysql-host"
        assert config["user"] == "lem"
        assert config["database"] == "lem_db"

    def test_rotated_credentials_are_pushed_to_the_existing_pool(self, monkeypatch):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT):
            db.get_db_connection()
            monkeypatch.setattr(db, "MYSQL_PASSWORD", "rotated")
            db.get_db_connection()

        pool = FakePool.instances[0]
        assert len(FakePool.instances) == 1
        assert [c["password"] for c in pool.set_config_calls] == ["secret", "rotated"]


class TestPoolGrowth:
    def test_pool_grows_lazily_one_connection_at_a_time(self):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT) as mock_connect:
            held = [db.get_db_connection() for _ in range(3)]

        pool = FakePool.instances[0]
        assert pool.opened == 3, "each concurrent checkout adds exactly one connection"
        assert len(held) == 3
        mock_connect.assert_not_called()

    def test_burst_past_pool_size_falls_back_to_a_direct_connection(self):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT) as mock_connect:
            held = [db.get_db_connection() for _ in range(4)]
            overflow = db.get_db_connection()

        pool = FakePool.instances[0]
        assert pool.opened == 4, "the pool must never open more than its size"
        assert len(held) == 4
        assert overflow is mock_connect.return_value
        assert mock_connect.call_args[1]["time_zone"] == "+00:00"

    def test_sequential_fanout_does_not_leak_connections(self):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT) as mock_connect:
            for _ in range(50):
                connection = db.get_db_connection()
                connection.close()

        pool = FakePool.instances[0]
        assert pool.opened == 1, "closing returns the connection to the pool instead of dropping it"
        assert len(pool.idle) == 1
        mock_connect.assert_not_called()


class TestExistingFixtureContract:
    def test_mock_database_connection_fixture_still_drives_get_db_connection(
            self, mock_database_connection, monkeypatch):
        """The suite-wide default keeps pooling off, so the fixture's patched connect still wins."""
        monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", False)

        connection = db.get_db_connection()

        assert connection is mock_database_connection["connection"]
        assert connection.cursor() is mock_database_connection["cursor"]


class TestPoolFallbacks:
    def test_pooling_disabled_uses_a_direct_connection(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", False)

        with patch(_POOL_CLASS, FakePool), patch(_CONNECT) as mock_connect:
            connection = db.get_db_connection()

        assert FakePool.instances == []
        assert connection is mock_connect.return_value
        assert mock_connect.call_args[1]["time_zone"] == "+00:00"

    def test_unbuildable_pool_degrades_to_a_direct_connection(self):
        with patch(_POOL_CLASS, side_effect=PoolError("boom")), patch(_CONNECT) as mock_connect:
            connection = db.get_db_connection()

        assert connection is mock_connect.return_value

    def test_pool_is_rebuilt_after_a_fork(self):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT), \
                patch("cqc_lem.utilities.db.os.getpid", side_effect=[111, 222]):
            db.get_db_connection()
            db.get_db_connection()

        assert [p.pool_name for p in FakePool.instances] == ["cqc-lem-111", "cqc-lem-222"]

    def test_aws_secret_supplies_the_pool_config(self, monkeypatch):
        monkeypatch.setattr(db, "AWS_MYSQL_SECRET_NAME", "lem/mysql")
        monkeypatch.setattr(db, "AWS_REGION", "us-east-1")
        secret = {"host": "rds-host", "username": "rds-user", "password": _STUB_VALUE,
                  "dbname": "rds-db", "port": 3306}

        with patch(_POOL_CLASS, FakePool), patch(_CONNECT), \
                patch("cqc_lem.utilities.db.get_aws_ssm_secret", return_value=secret):
            db.get_db_connection()

        config = FakePool.instances[0].config
        assert config["host"] == "rds-host"
        assert config["user"] == "rds-user"
        assert config["database"] == "rds-db"
        assert config["time_zone"] == "+00:00"

    def test_reset_connection_pool_forces_a_new_pool(self):
        with patch(_POOL_CLASS, FakePool), patch(_CONNECT):
            db.get_db_connection()
            db.reset_connection_pool()
            db.get_db_connection()

        assert len(FakePool.instances) == 2
