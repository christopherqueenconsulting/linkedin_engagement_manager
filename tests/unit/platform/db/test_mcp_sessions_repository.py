"""The MCP additions to `platform/db/repositories/auth.py` (epic #2682, plan §3.1, §3.4, §3.5)."""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_ERR = mysql.connector.Error("boom")


def _conn(conn: Any) -> Any:
    return patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sql(cursor: Any) -> list[str]:
    return [c.args[0] for c in cursor.execute.call_args_list]


class TestPrefixedTokens:
    def test_the_prefix_is_kept_and_the_hash_covers_the_whole_token(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import MCP_TOKEN_PREFIX, create_session

        conn, cursor = fake_cursor()
        with _conn(conn):
            token = create_session(7, scope="mcp", ttl_hours=24, token_prefix=MCP_TOKEN_PREFIX)
        assert token.startswith("lem_mcp_") and len(token) == len("lem_mcp_") + 64
        stored = cursor.execute.call_args_list[0].args[1][0]
        assert stored == _sha(token)

    def test_no_prefix_is_unchanged(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import create_session

        conn, _cursor = fake_cursor()
        with _conn(conn):
            token = create_session(7)
        assert len(token) == 64 and not token.startswith("lem_")

    def test_a_prefixed_token_resolves(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import resolve_session_checked

        token = "lem_mcp_" + "ab" * 32
        conn, cursor = fake_cursor(fetch_one={"id": 9, "user_id": 7, "scope": "mcp",
                                              "created_at": None, "expires_at": None,
                                              "last_seen_at": None})
        with _conn(conn):
            assert resolve_session_checked(token)["session_id"] == 9
        assert cursor.execute.call_args_list[0].args[1][0] == _sha(token)


def _now() -> datetime:
    # Read at call time, never at import: under xdist a module can load minutes before it runs.
    return datetime.now(timezone.utc)


class TestLastSeenThrottle:
    def _resolve(self, fake_cursor: Any, scope: str, last_seen: Any) -> list[str]:
        from cqc_lem.utilities.db import resolve_session

        conn, cursor = fake_cursor(fetch_one={"id": 1, "user_id": 7, "scope": scope,
                                              "created_at": None, "expires_at": None,
                                              "last_seen_at": last_seen})
        with _conn(conn):
            assert resolve_session("tok") == {"user_id": 7, "scope": scope}
        return _sql(cursor)

    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
    def test_an_mcp_session_seen_within_a_minute_writes_nothing(self, fake_cursor: Any,
                                                                scope: str) -> None:
        sql = self._resolve(fake_cursor, scope, _now() - timedelta(seconds=10))
        assert not any(s.startswith("UPDATE") for s in sql)

    def test_a_naive_timestamp_is_read_as_utc(self, fake_cursor: Any) -> None:
        naive = (_now() - timedelta(seconds=10)).replace(tzinfo=None)
        assert not any(s.startswith("UPDATE") for s in self._resolve(fake_cursor, "mcp", naive))

    @pytest.mark.parametrize("seconds_ago", [None, 120])
    def test_an_mcp_session_seen_longer_ago_is_written(self, fake_cursor: Any,
                                                       seconds_ago: Any) -> None:
        last_seen = None if seconds_ago is None else _now() - timedelta(seconds=seconds_ago)
        sql = self._resolve(fake_cursor, "mcp", last_seen)
        assert sql[-1].startswith("UPDATE sessions SET last_seen_at")

    @pytest.mark.parametrize("scope", ["agent", "observer", "operator", "assist"])
    def test_every_other_fixed_ttl_seat_writes_every_time(self, fake_cursor: Any,
                                                          scope: str) -> None:
        sql = self._resolve(fake_cursor, scope, _now())
        assert sql[-1].startswith("UPDATE sessions SET last_seen_at")

    def test_a_full_session_still_slides(self, fake_cursor: Any) -> None:
        sql = self._resolve(fake_cursor, "full", _now())
        assert sql[-1].startswith("UPDATE sessions SET expires_at")


class TestCheckedResolve:
    def test_an_execute_fault_raises(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import SessionStoreUnavailable, resolve_session_checked

        conn, _cursor = fake_cursor(execute_error=_ERR)
        with _conn(conn), pytest.raises(SessionStoreUnavailable):
            resolve_session_checked("tok")

    def test_a_connect_fault_raises(self) -> None:
        from cqc_lem.utilities.db import SessionStoreUnavailable, resolve_session_checked

        with patch("cqc_lem.platform.db.connection.get_db_connection", side_effect=_ERR), \
             pytest.raises(SessionStoreUnavailable):
            resolve_session_checked("tok")

    def test_the_unchecked_resolver_still_answers_none_on_an_execute_fault(
            self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import resolve_session

        conn, _cursor = fake_cursor(execute_error=_ERR)
        with _conn(conn):
            assert resolve_session("tok") is None

    def test_an_unknown_token_and_an_empty_one(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import resolve_session_checked

        conn, _cursor = fake_cursor(fetch_one=None)
        with _conn(conn):
            assert resolve_session_checked("tok") is None
        assert resolve_session_checked("") is None

    def test_a_full_session_reports_its_slid_expiry(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import resolve_session_checked

        conn, _cursor = fake_cursor(fetch_one={"id": 1, "user_id": 7, "scope": None,
                                               "created_at": datetime(2020, 1, 1),
                                               "expires_at": None, "last_seen_at": None})
        with _conn(conn):
            found = resolve_session_checked("tok")
        assert found["scope"] == "full" and isinstance(found["expires_at"], datetime)


class TestReaders:
    def test_count_active_sessions(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import count_active_sessions

        conn, cursor = fake_cursor(fetch_one=(3,))
        with _conn(conn):
            assert count_active_sessions(7, frozenset({"mcp_outreach", "mcp"})) == 3
        sql, params = cursor.execute.call_args.args
        assert "scope IN (%s, %s)" in sql and "revoked_at IS NULL" in sql
        assert params[:3] == (7, "mcp", "mcp_outreach")
        conn, _ = fake_cursor(execute_error=_ERR)
        with _conn(conn):
            assert count_active_sessions(7, ("mcp",)) is None

    def test_an_empty_scope_set_is_a_programming_error(self) -> None:
        from cqc_lem.utilities.db import count_active_sessions

        with pytest.raises(ValueError):
            count_active_sessions(7, ())

    def test_get_owned_session(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import SessionStoreUnavailable, get_owned_session

        conn, cursor = fake_cursor(fetch_one={"id": 5})
        with _conn(conn):
            assert get_owned_session(7, 5, ("mcp",)) == {"id": 5}
        sql, params = cursor.execute.call_args.args
        assert "user_id = %s" in sql and params[:2] == (5, 7)
        conn, _ = fake_cursor(execute_error=_ERR)
        with _conn(conn), pytest.raises(SessionStoreUnavailable):
            get_owned_session(7, 5, ("mcp",))

    def test_list_sessions_by_scope(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import list_sessions_by_scope

        conn, cursor = fake_cursor(fetch_all=[{"id": 1}])
        with _conn(conn):
            assert list_sessions_by_scope(7, ("mcp",)) == [{"id": 1}]
        assert "session_token" not in cursor.execute.call_args.args[0]
        conn, _ = fake_cursor(execute_error=_ERR)
        with _conn(conn):
            assert list_sessions_by_scope(7, ("mcp",)) is None

    def test_list_auth_audit_events_matches_the_prefix_literally(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import list_auth_audit_events

        conn, cursor = fake_cursor(fetch_all=[])
        with _conn(conn):
            assert list_auth_audit_events(7, prefix="mcp_", exclude_prefix="a%b", limit=5) == []
        sql, params = cursor.execute.call_args.args
        assert "event LIKE %s" in sql and "event NOT LIKE %s" in sql and "ip_hash" not in sql
        assert params == (7, "mcp\\_%", "a\\%b%", 5)
        conn, _ = fake_cursor(execute_error=_ERR)
        with _conn(conn):
            assert list_auth_audit_events(7) is None


class TestRevokes:
    def test_revoke_session_by_token(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import revoke_session_by_token

        conn, cursor = fake_cursor(fetch_one={"id": 4, "user_id": 7, "scope": None,
                                              "label": "Chrome"})
        with _conn(conn):
            assert revoke_session_by_token("tok") == {"user_id": 7, "session_id": 4,
                                                      "scope": "full", "label": "Chrome"}
        assert cursor.execute.call_args_list[0].args[1][0] == _sha("tok")
        assert _sql(cursor)[1].startswith("UPDATE sessions SET revoked_at")
        assert not any("last_seen_at" in s or "expires_at =" in s for s in _sql(cursor))

    @pytest.mark.parametrize("fetch,rowcount", [(None, 1), ({"id": 4, "user_id": 7}, 0)])
    def test_nothing_to_revoke(self, fake_cursor: Any, fetch: Any, rowcount: int) -> None:
        from cqc_lem.utilities.db import revoke_session_by_token

        conn, _ = fake_cursor(fetch_one=fetch, rowcount=rowcount)
        with _conn(conn):
            assert revoke_session_by_token("tok") is None
        assert revoke_session_by_token("") is None

    def test_a_revoke_fault_is_none(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import revoke_session_by_token

        conn, _ = fake_cursor(execute_error=_ERR)
        with _conn(conn):
            assert revoke_session_by_token("tok") is None

    def test_revoke_by_scope_audits_each_user_in_the_same_transaction(self,
                                                                      fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import MCP_SESSION_SCOPES, revoke_sessions_by_scope

        conn, cursor = fake_cursor(fetch_all=[{"user_id": 7, "n": 2}, {"user_id": 9, "n": 1}],
                                   rowcount=3)
        with _conn(conn):
            assert revoke_sessions_by_scope(MCP_SESSION_SCOPES, reason="incident " * 20) == 3
        inserts = [c for c in cursor.execute.call_args_list
                   if c.args[0].startswith("INSERT INTO auth_audit_log")]
        assert [c.args[1][0] for c in inserts] == [7, 9]
        assert {c.args[1][2] for c in inserts} == {"mcp_sessions_revoked"}
        assert '"revoked": 2' in inserts[0].args[1][7]
        assert len(conn.cursor.call_args_list) == 1, "one cursor, one transaction"
        conn.commit.assert_called_once()

    def test_nothing_live_revokes_nothing(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import revoke_sessions_by_scope

        conn, cursor = fake_cursor(fetch_all=[])
        with _conn(conn):
            assert revoke_sessions_by_scope(("mcp",), reason="x") == 0
        assert len(cursor.execute.call_args_list) == 1

    def test_a_failed_transaction_is_none(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import revoke_sessions_by_scope

        conn, _ = fake_cursor(execute_error=_ERR)
        with _conn(conn):
            assert revoke_sessions_by_scope(("mcp",), reason="x") is None


class TestTheGateLookupIsReadOnlyForOtherScopes:
    def _resolve(self, fake_cursor: Any, scope: str) -> list[str]:
        from cqc_lem.utilities.db import MCP_SESSION_SCOPES, resolve_session_checked

        conn, cursor = fake_cursor(fetch_one={"id": 1, "user_id": 7, "scope": scope,
                                              "created_at": None, "expires_at": None,
                                              "last_seen_at": None})
        with _conn(conn):
            found = resolve_session_checked("tok", mutate_scopes=MCP_SESSION_SCOPES)
        assert found["scope"] == scope
        return _sql(cursor)

    @pytest.mark.parametrize("scope", ["full", "recovery", "agent", "observer", "operator",
                                       "assist", "extension", "enroll"])
    def test_a_non_mcp_session_is_read_and_nothing_is_written(self, fake_cursor: Any,
                                                              scope: str) -> None:
        sql = self._resolve(fake_cursor, scope)
        assert len(sql) == 1 and sql[0].startswith("SELECT")

    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
    def test_an_mcp_session_still_records_last_seen(self, fake_cursor: Any, scope: str) -> None:
        assert self._resolve(fake_cursor, scope)[-1].startswith("UPDATE sessions SET last_seen_at")


class TestTheCappedMint:
    def _mint(self, fake_cursor: Any, fetch_one_side_effect: Any, **kwargs: Any) -> tuple:
        from cqc_lem.utilities.db import MCP_SESSION_SCOPES, create_session_within_cap

        conn, cursor = fake_cursor(fetch_one_side_effect=fetch_one_side_effect, lastrowid=88,
                                   **kwargs)
        with _conn(conn):
            result = create_session_within_cap(7, MCP_SESSION_SCOPES, 10, "mcp", label="L",
                                               ttl_hours=24, token_prefix="lem_mcp_")
        return result, conn, cursor

    def test_under_the_cap_it_locks_counts_and_inserts_in_one_transaction(
            self, fake_cursor: Any) -> None:
        (token, session_id), conn, cursor = self._mint(fake_cursor, [{"id": 7}, (9,)])
        assert token.startswith("lem_mcp_") and session_id == 88
        sql = _sql(cursor)
        assert sql[0] == "SELECT id FROM users WHERE id = %s FOR UPDATE"
        assert sql[1].startswith("SELECT COUNT(*) FROM sessions") and "revoked_at IS NULL" in sql[1]
        assert sql[2].startswith("INSERT INTO sessions")
        assert cursor.execute.call_args_list[2].args[1][0] == _sha(token)
        assert len(conn.cursor.call_args_list) == 1, "one cursor, one transaction"
        conn.commit.assert_called_once()

    def test_at_the_cap_nothing_is_inserted(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import SessionCapReached

        with pytest.raises(SessionCapReached):
            self._mint(fake_cursor, [{"id": 7}, (10,)])

    def test_a_dict_row_count_is_read_too(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import SessionCapReached

        with pytest.raises(SessionCapReached):
            self._mint(fake_cursor, [{"id": 7}, {"COUNT(*)": 12}])

    def test_no_such_user_mints_nothing(self, fake_cursor: Any) -> None:
        result, _conn_, cursor = self._mint(fake_cursor, [None])
        assert result == (None, None)
        assert len(_sql(cursor)) == 1

    def test_a_fault_mints_nothing(self, fake_cursor: Any) -> None:
        result, _conn_, _cursor = self._mint(fake_cursor, [{"id": 7}], execute_error=_ERR)
        assert result == (None, None)


class TestListingAndAuditColumns:
    def test_live_tokens_come_first_in_sql(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import list_sessions_by_scope

        conn, cursor = fake_cursor(fetch_all=[])
        with _conn(conn):
            list_sessions_by_scope(7, ("mcp",), limit=3)
        sql, params = cursor.execute.call_args.args
        assert "ORDER BY (revoked_at IS NULL AND expires_at > %s) DESC, id DESC LIMIT %s" in sql
        assert isinstance(params[-2], datetime) and params[-1] == 3

    def test_the_audit_list_carries_user_agent_for_the_security_card(
            self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import list_auth_audit_events

        conn, cursor = fake_cursor(fetch_all=[])
        with _conn(conn):
            list_auth_audit_events(7, exclude_prefix="mcp_")
        assert "user_agent" in cursor.execute.call_args.args[0]
