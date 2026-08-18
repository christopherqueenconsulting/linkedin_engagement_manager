"""Issue #1675: a MySQL that was never reached is retried; anything it answered is not.

The live failure was `2005 (HY000): Unknown MySQL server host 'mysql_db'` — one task lost to a
compose DNS name that had not come back yet after the database container restarted.
"""

from unittest.mock import MagicMock, patch

import pytest
from mysql.connector.errors import DatabaseError, ProgrammingError

from cqc_lem.platform.db import connection as db

pytestmark = pytest.mark.unit

_CONNECT = "cqc_lem.platform.db.connection.mysql.connector.connect"
_SLEEP = "cqc_lem.platform.db.connection.time.sleep"


def _unknown_host() -> DatabaseError:
    """The exact error the issue was filed for."""
    return DatabaseError(msg="Unknown MySQL server host 'mysql_db' (-3)", errno=2005)


@pytest.fixture(autouse=True)
def _direct_connections(monkeypatch):
    """Pooling off by default here.

    The retry wraps BOTH paths and the direct one is the shorter read; the pooled path gets its own
    test below.
    """
    db.reset_connection_pool()
    monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", False)
    monkeypatch.setattr(db, "AWS_MYSQL_SECRET_NAME", None)
    monkeypatch.setattr(db, "AWS_REGION", None)
    monkeypatch.setattr(db, "MYSQL_HOST", "mysql_db")
    monkeypatch.setattr(db, "MYSQL_USER", "lem")
    monkeypatch.setattr(db, "MYSQL_PASSWORD", "secret")
    monkeypatch.setattr(db, "MYSQL_DATABASE", "lem_db")
    monkeypatch.setattr(db, "MYSQL_PORT", "3306")
    monkeypatch.setattr(db, "DB_CONNECT_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(db, "DB_CONNECT_RETRY_BACKOFF_SECONDS", 1.0)
    yield
    db.reset_connection_pool()


class TestUnreachableClassification:
    @pytest.mark.parametrize("errno", [2002, 2003, 2005])
    def test_client_side_connect_errnos_are_unreachable(self, errno):
        assert db._db_unreachable(DatabaseError(msg="no server", errno=errno)) is True

    @pytest.mark.parametrize("errno", [1045, 1049, 2006, 1146])
    def test_anything_the_server_answered_is_not_unreachable(self, errno):
        assert db._db_unreachable(DatabaseError(msg="answered", errno=errno)) is False

    def test_a_non_connector_error_is_not_unreachable(self):
        assert db._db_unreachable(TypeError("int() argument must not be NoneType")) is False


class TestRetryOnUnreachable:
    def test_a_dns_blip_is_ridden_out(self):
        connection = MagicMock(name="cnx")
        with patch(_CONNECT, side_effect=[_unknown_host(), connection]) as mock_connect, \
                patch(_SLEEP) as mock_sleep:
            assert db.get_db_connection() is connection
        assert mock_connect.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    def test_waits_double_between_attempts(self, monkeypatch):
        monkeypatch.setattr(db, "DB_CONNECT_RETRY_ATTEMPTS", 4)
        connection = MagicMock(name="cnx")
        side_effects = [_unknown_host(), _unknown_host(), _unknown_host(), connection]
        with patch(_CONNECT, side_effect=side_effects), patch(_SLEEP) as mock_sleep:
            assert db.get_db_connection() is connection
        assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0, 2.0, 4.0]

    def test_one_wait_is_capped_so_a_mistyped_backoff_cannot_park_a_worker(self, monkeypatch):
        monkeypatch.setattr(db, "DB_CONNECT_RETRY_BACKOFF_SECONDS", 600.0)
        connection = MagicMock(name="cnx")
        with patch(_CONNECT, side_effect=[_unknown_host(), connection]), patch(_SLEEP) as mock_sleep:
            db.get_db_connection()
        mock_sleep.assert_called_once_with(db._MAX_CONNECT_RETRY_DELAY)

    def test_the_budget_is_bounded_and_the_last_failure_is_raised(self):
        errors = [_unknown_host(), _unknown_host(), _unknown_host()]
        with patch(_CONNECT, side_effect=errors) as mock_connect, patch(_SLEEP) as mock_sleep:
            with pytest.raises(DatabaseError) as excinfo:
                db.get_db_connection()
        assert excinfo.value is errors[-1]
        assert mock_connect.call_count == 3
        assert mock_sleep.call_count == 2

    def test_attempts_of_one_turns_the_wait_off(self, monkeypatch):
        monkeypatch.setattr(db, "DB_CONNECT_RETRY_ATTEMPTS", 1)
        with patch(_CONNECT, side_effect=_unknown_host()) as mock_connect, patch(_SLEEP) as mock_sleep:
            with pytest.raises(DatabaseError):
                db.get_db_connection()
        assert mock_connect.call_count == 1
        mock_sleep.assert_not_called()

    def test_a_nonsense_attempts_value_still_makes_one_attempt(self, monkeypatch):
        monkeypatch.setattr(db, "DB_CONNECT_RETRY_ATTEMPTS", 0)
        connection = MagicMock(name="cnx")
        with patch(_CONNECT, return_value=connection) as mock_connect:
            assert db.get_db_connection() is connection
        assert mock_connect.call_count == 1


class TestNoRetryOnAnAnsweringServer:
    def test_bad_credentials_fail_on_the_first_attempt(self):
        denied = ProgrammingError(msg="Access denied for user 'lem'", errno=1045)
        with patch(_CONNECT, side_effect=denied) as mock_connect, patch(_SLEEP) as mock_sleep:
            with pytest.raises(ProgrammingError):
                db.get_db_connection()
        assert mock_connect.call_count == 1
        mock_sleep.assert_not_called()

    def test_a_typeerror_is_not_swallowed_into_the_retry_budget(self):
        with patch(_CONNECT, side_effect=TypeError("boom")) as mock_connect, patch(_SLEEP) as mock_sleep:
            with pytest.raises(TypeError):
                db.get_db_connection()
        assert mock_connect.call_count == 1
        mock_sleep.assert_not_called()


class TestPooledPath:
    """With pooling on, an unreachable server must not read as a pool problem."""

    def test_unreachable_server_is_retried_without_blaming_the_pool(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", True)
        connection = MagicMock(name="pooled-cnx")
        pooled = patch("cqc_lem.platform.db.connection._get_pooled_connection",
                       side_effect=[_unknown_host(), connection])
        with pooled as mock_pooled, patch(_CONNECT) as mock_connect, \
                patch("cqc_lem.platform.db.connection.log_warning") as mock_warning, \
                patch(_SLEEP) as mock_sleep:
            assert db.get_db_connection() is connection
        assert mock_pooled.call_count == 2
        # No direct connection was opened: the server was down, not the pool.
        mock_connect.assert_not_called()
        mock_warning.assert_not_called()
        mock_sleep.assert_called_once_with(1.0)

    def test_a_real_pool_failure_still_falls_back_to_a_direct_connection(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", True)
        exhausted = DatabaseError(msg="Failed getting connection; pool exhausted", errno=None)
        connection = MagicMock(name="direct-cnx")
        with patch("cqc_lem.platform.db.connection._get_pooled_connection", side_effect=exhausted), \
                patch(_CONNECT, return_value=connection) as mock_connect, \
                patch("cqc_lem.platform.db.connection.log_warning") as mock_warning, \
                patch(_SLEEP) as mock_sleep:
            assert db.get_db_connection() is connection
        mock_connect.assert_called_once()
        mock_warning.assert_called_once()
        mock_sleep.assert_not_called()
