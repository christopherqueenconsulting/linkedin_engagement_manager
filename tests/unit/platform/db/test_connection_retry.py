"""Issue #1660: a MySQL that is momentarily unreachable is ridden out, not reported.

Docker's embedded DNS failed to resolve `mysql_db` once (errno 2005). Every caller answers a
connection failure with its own fallback, so the blip surfaced as `get_active_user_ids()` returning
`[]` — a scheduler run that engaged for nobody — plus a grouped `$exception`. These tests pin which
failures are retried (only those where nothing was ever sent) and which still fail immediately.
"""

from unittest.mock import MagicMock, patch

import pytest
from mysql.connector.errors import DatabaseError, PoolError

from cqc_lem.platform.db import connection as db

pytestmark = pytest.mark.unit

_CONNECT = "cqc_lem.platform.db.connection.mysql.connector.connect"
_SLEEP = "cqc_lem.platform.db.connection.time.sleep"
_POOL_CLASS = "cqc_lem.platform.db.connection.MySQLConnectionPool"


def _connect_error(errno: int) -> DatabaseError:
    """Build the connector's own error object so `.errno` is populated exactly as in production."""
    return DatabaseError(msg=f"({errno}) connect failed", errno=errno)


@pytest.fixture(autouse=True)
def _direct_connect_config(monkeypatch):
    """Pin the DB config so no test reads the developer's `.env` (pooling is off via conftest)."""
    db.reset_connection_pool()
    monkeypatch.setattr(db, "AWS_MYSQL_SECRET_NAME", None)
    monkeypatch.setattr(db, "AWS_REGION", None)
    monkeypatch.setattr(db, "MYSQL_HOST", "mysql_db")
    monkeypatch.setattr(db, "MYSQL_USER", "lem")
    monkeypatch.setattr(db, "MYSQL_PASSWORD", "unused-by-assertions")
    monkeypatch.setattr(db, "MYSQL_DATABASE", "lem_db")
    monkeypatch.setattr(db, "MYSQL_PORT", "3306")
    monkeypatch.setattr(db, "MYSQL_CONNECT_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(db, "MYSQL_CONNECT_RETRY_BACKOFF_SECONDS", 2.0)
    yield
    db.reset_connection_pool()


class TestUnreachableIsRetried:
    @pytest.mark.parametrize("errno", [2002, 2003, 2005])
    def test_a_never_established_connection_is_retried_and_succeeds(self, errno):
        """2002/2003/2005 all mean the handshake never happened, so a retry duplicates nothing."""
        established = MagicMock(name="connection")
        with patch(_SLEEP) as mock_sleep, \
                patch(_CONNECT, side_effect=[_connect_error(errno), established]) as mock_connect:
            assert db.get_db_connection() is established

        assert mock_connect.call_count == 2
        mock_sleep.assert_called_once_with(2.0)

    def test_the_wait_doubles_between_attempts(self):
        established = MagicMock(name="connection")
        with patch(_SLEEP) as mock_sleep, \
                patch(_CONNECT, side_effect=[_connect_error(2005),
                                             _connect_error(2005),
                                             established]):
            assert db.get_db_connection() is established

        assert [call.args[0] for call in mock_sleep.call_args_list] == [2.0, 4.0]

    def test_one_wait_is_capped_so_a_mistyped_backoff_cannot_park_a_worker(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_CONNECT_RETRY_BACKOFF_SECONDS", 3600.0)
        established = MagicMock(name="connection")
        with patch(_SLEEP) as mock_sleep, \
                patch(_CONNECT, side_effect=[_connect_error(2005), established]):
            db.get_db_connection()

        mock_sleep.assert_called_once_with(db._MAX_CONNECT_RETRY_DELAY)

    def test_an_exhausted_budget_still_raises_for_the_caller_to_log(self):
        """The caller's own `except mysql.connector.Error` is what reports a real outage."""
        with patch(_SLEEP), patch(_CONNECT, side_effect=_connect_error(2005)) as mock_connect:
            with pytest.raises(DatabaseError) as excinfo:
                db.get_db_connection()

        assert excinfo.value.errno == 2005
        assert mock_connect.call_count == 3

    def test_attempts_of_one_turns_the_retry_off(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_CONNECT_RETRY_ATTEMPTS", 1)
        with patch(_SLEEP) as mock_sleep, \
                patch(_CONNECT, side_effect=_connect_error(2005)) as mock_connect:
            with pytest.raises(DatabaseError):
                db.get_db_connection()

        assert mock_connect.call_count == 1
        mock_sleep.assert_not_called()


class TestEstablishedFailuresAreNotRetried:
    @pytest.mark.parametrize("errno", [
        1045,  # ER_ACCESS_DENIED_ERROR — the server answered and refused us
        2006,  # CR_SERVER_GONE_ERROR — an established connection, possibly mid-statement
        2013,  # CR_SERVER_LOST — same, and a retry there could duplicate a write
    ])
    def test_it_fails_on_the_first_attempt(self, errno):
        with patch(_SLEEP) as mock_sleep, \
                patch(_CONNECT, side_effect=_connect_error(errno)) as mock_connect:
            with pytest.raises(DatabaseError):
                db.get_db_connection()

        assert mock_connect.call_count == 1
        mock_sleep.assert_not_called()


class TestPooledPath:
    """With pooling ON the pool opens the socket, so the same failure arrives through the pool."""

    @pytest.fixture(autouse=True)
    def _pooling_enabled(self, monkeypatch):
        monkeypatch.setattr(db, "MYSQL_POOL_ENABLED", True)
        monkeypatch.setattr(db, "MYSQL_POOL_SIZE", 4)

    def test_an_unreachable_server_is_not_reported_as_a_pool_problem(self):
        """A DNS blip is not the pool's fault.

        It must not warn, and must not fall through to a direct connect that would fail
        identically — the retry is what answers it.
        """
        pool = MagicMock(name="pool")
        pool.pool_name = "cqc-lem-test"
        pool.pool_size = 4
        pool.get_connection.side_effect = PoolError("Failed getting connection; pool exhausted")
        pool.add_connection.side_effect = _connect_error(2005)

        with patch(_SLEEP), patch(_POOL_CLASS, return_value=pool), \
                patch(_CONNECT) as mock_connect, \
                patch("cqc_lem.platform.db.connection.log_warning") as mock_warning:
            with pytest.raises(DatabaseError):
                db.get_db_connection()

        assert pool.add_connection.call_count == 3
        mock_connect.assert_not_called()
        mock_warning.assert_not_called()

    def test_a_pool_problem_still_falls_back_to_a_direct_connection(self):
        """The pre-existing fallback is untouched: an exhausted pool is not an unreachable server."""
        pool = MagicMock(name="pool")
        pool.pool_name = "cqc-lem-test"
        pool.pool_size = 4
        pool.get_connection.side_effect = PoolError("Failed getting connection; pool exhausted")
        pool.add_connection.side_effect = PoolError("Failed adding connection; pool is full")
        direct = MagicMock(name="direct-connection")

        with patch(_SLEEP) as mock_sleep, patch(_POOL_CLASS, return_value=pool), \
                patch(_CONNECT, return_value=direct), \
                patch("cqc_lem.platform.db.connection.log_warning") as mock_warning:
            assert db.get_db_connection() is direct

        mock_sleep.assert_not_called()
        mock_warning.assert_called_once()


class TestServerUnreachablePredicate:
    def test_a_non_connector_exception_is_never_retryable(self):
        assert db._server_unreachable(TimeoutError("boom")) is False

    def test_an_error_without_an_errno_is_never_retryable(self):
        assert db._server_unreachable(DatabaseError("no errno here")) is False
