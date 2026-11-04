"""What an `observer` session can READ, and the record that it read.

An observer is a machine that sees the owner's queues and history. Three things are pinned here:

1. `GET /user/security` is bounded for every scope, drops `user_agent` for an observer, and never
   carries a token, token hash, address or address hash.
2. Every route on the observer surface returns only whitelisted columns, none of which is a
   credential. The list getters select an explicit column tuple; this proves it from the SQL they
   actually run, so a `SELECT *` or a new credential column fails the build.
3. The first successful observer request of each UTC day, per session, writes ONE `observer_access`
   audit row, and a Redis outage never turns a read into an error.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_AUTH = "cqc_lem.api.routers.auth"

_UID = 51
_TOKEN = "observer-exposure-token"

# A column or key that names a credential, a session handle or an address. `recipient_email` is a
# third party's address; it is only ever exposed as the `has_recipient_email` boolean.
_CREDENTIAL = re.compile(
    r"li_at|cookie|passw|token|secret|session|hash|credential|api_key|bearer|recipient_email",
    re.IGNORECASE)
# The one place an address column is allowed to appear: as a boolean, never the value.
_ADDRESS_AS_BOOLEAN = "recipient_email IS NOT NULL AS has_recipient_email"
# For a response BODY, `sessions` is a legitimate key (the device list); a session HANDLE is not.
_CREDENTIAL_KEY = re.compile(r"li_at|cookie|passw|token|secret|hash|credential|api_key|bearer|^ip$",
                             re.IGNORECASE)


def _session(scope: str) -> Any:
    return patch(f"{_M}._db_resolve_session",
                 side_effect=lambda t: {"user_id": _UID, "scope": scope} if t == _TOKEN else None)


def _kernel() -> Any:
    from cqc_lem.api import main

    return main


@pytest.fixture(autouse=True)
def _account_without_a_strong_factor() -> Any:
    """See `test_observer_scope.py`: pins the factor count so the real resolver runs without a DB."""
    with patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=0):
        yield


@pytest.fixture(autouse=True)
def _quiet() -> Any:
    with patch.object(_kernel(), "record_auth_event", return_value=True), \
         patch(f"{_USER}.record_auth_event", return_value=True), \
         patch(f"{_M}.claim_observer_access", return_value=False), \
         patch.object(_kernel(), "get_session_id", return_value=1):
        yield


def _walk(obj: Any) -> tuple[list[str], list[str]]:
    """Every key and every string value anywhere in a JSON-shaped structure."""
    keys: list[str] = []
    values: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(str(k))
            sub_keys, sub_values = _walk(v)
            keys += sub_keys
            values += sub_values
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            sub_keys, sub_values = _walk(item)
            keys += sub_keys
            values += sub_values
    elif isinstance(obj, str):
        values.append(obj)
    return keys, values


# ---------------------------------------------------------------------------
# GET /user/security
# ---------------------------------------------------------------------------

_POISON = {
    "session_token": "POISON-session-token-value",
    "token": "POISON-bare-token-value",
    "token_hash": "POISON-token-hash-value",
    "ip": "203.0.113.77",
    "ip_hash": "POISON-ip-hash-value",
    "li_at": "POISON-li-at-value",
}


def _audit_rows(n: int) -> list[dict[str, Any]]:
    return [{"event": "login_success", "success": 1, "user_agent": f"UA-{i}",
             "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), **_POISON} for i in range(n)]


def _session_rows() -> list[dict[str, Any]]:
    return [{"id": 9, "label": "ops watcher", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
             "last_seen_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
             "expires_at": datetime(2026, 4, 1, tzinfo=timezone.utc), "is_current": True,
             "scope": "observer", **_POISON}]


def _get_security(api_client: Any, scope: str, events: list[dict[str, Any]]) -> tuple[Any, Any]:
    def bounded(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        return events[:limit]

    with _session(scope), \
         patch(f"{_USER}.list_user_sessions", return_value=_session_rows()), \
         patch(f"{_USER}.get_auth_audit_events", side_effect=bounded) as audit, \
         patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
         patch(f"{_USER}.get_user_email", return_value="owner@example.com"):
        resp = api_client.get("/api/user/security", params={"session_token": _TOKEN})
    return resp, audit


class TestSecurityEndpoint:
    @pytest.mark.parametrize("scope", ["observer", "full"])
    def test_a_hundred_events_come_back_as_at_most_fifty(self, api_client: Any, scope: str) -> None:
        """The bound holds for every scope that can read the page (an agent is not on this surface)."""
        resp, audit = _get_security(api_client, scope, _audit_rows(100))
        assert resp.status_code == 200
        events = resp.json()["detail"]["recent_events"]
        assert 0 < len(events) <= 50
        assert audit.call_args.kwargs["limit"] == 50, "the bound is asked of the query, not only trimmed"

    def test_an_endpoint_that_ignores_the_limit_is_still_trimmed(self, api_client: Any) -> None:
        with _session("observer"), \
             patch(f"{_USER}.list_user_sessions", return_value=[]), \
             patch(f"{_USER}.get_auth_audit_events", return_value=_audit_rows(100)), \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value="owner@example.com"):
            resp = api_client.get("/api/user/security", params={"session_token": _TOKEN})
        assert len(resp.json()["detail"]["recent_events"]) == 50

    def test_an_observer_response_has_no_user_agent_key(self, api_client: Any) -> None:
        resp, _ = _get_security(api_client, "observer", _audit_rows(3))
        events = resp.json()["detail"]["recent_events"]
        assert events and all("user_agent" not in e for e in events)
        assert "UA-" not in resp.text

    def test_a_human_session_still_sees_the_user_agent(self, api_client: Any) -> None:
        resp, _ = _get_security(api_client, "full", _audit_rows(3))
        events = resp.json()["detail"]["recent_events"]
        assert [e["user_agent"] for e in events] == ["UA-0", "UA-1", "UA-2"]

    @pytest.mark.parametrize("scope", ["observer", "full"])
    def test_the_body_carries_no_token_hash_or_address_key_or_value(self, api_client: Any,
                                                                    scope: str) -> None:
        resp, _ = _get_security(api_client, scope, _audit_rows(5))
        assert resp.status_code == 200
        body = resp.json()
        keys, values = _walk(body)
        forbidden_keys = {"token", "token_hash", "session_token", "ip", "ip_hash", "li_at"}
        assert not forbidden_keys & {k.lower() for k in keys}, keys
        for poison in _POISON.values():
            assert poison not in resp.text, poison
            assert poison not in values
        assert not [k for k in keys if _CREDENTIAL_KEY.search(k)], keys


# ---------------------------------------------------------------------------
# What each observer-readable route can return
# ---------------------------------------------------------------------------

def _select_list(sql: str) -> str:
    match = re.search(r"SELECT\s+(.*?)\s+FROM\s", sql, re.IGNORECASE | re.DOTALL)
    assert match, sql
    return match.group(1)


class TestListRoutesSelectOnlyWhitelistedColumns:
    """The six queue readers each SELECT an explicit column tuple. Proven from the SQL they run."""

    @pytest.mark.parametrize("getter,rows_key", [
        ("get_scheduled_dms", "dms"), ("get_connection_requests", "requests"),
        ("get_outreach_targets", "targets"), ("get_catchup_touches", "touches"),
        ("get_lead_signals", "signals"), ("get_leads", "leads"),
    ])
    def test_the_row_select_has_no_wildcard_and_no_credential_column(
            self, fake_cursor: Any, getter: str, rows_key: str) -> None:
        from cqc_lem.utilities import db

        conn, cursor = fake_cursor(fetch_one={"c": 0}, fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            result = getattr(db, getter)(_UID)
        assert rows_key in result
        row_selects = [c.args[0] for c in cursor.execute.call_args_list
                       if "COUNT(*)" not in c.args[0].upper()]
        assert row_selects, f"{getter} ran no row query"
        for sql in row_selects:
            columns = _select_list(sql)
            assert "*" not in columns, f"{getter} selects a wildcard: {columns}"
            checked = columns.replace(_ADDRESS_AS_BOOLEAN, "")
            assert not _CREDENTIAL.search(checked), f"{getter} selects a credential-like column: {columns}"

    def test_a_connection_request_exposes_whether_an_address_exists_never_the_address(
            self, fake_cursor: Any) -> None:
        from cqc_lem.platform.db.repositories.outreach import _CONN_REQ_COLS

        assert "recipient_email IS NOT NULL AS has_recipient_email" in _CONN_REQ_COLS
        assert "recipient_email" not in [c.strip() for c in _CONN_REQ_COLS]

    def test_the_curated_source_select_is_an_explicit_whitelist(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities import db

        conn, cursor = fake_cursor(fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            db.get_curated_sources(_UID)
        columns = _select_list(cursor.execute.call_args_list[0].args[0])
        assert "*" not in columns
        assert not _CREDENTIAL.search(columns), columns


class TestHandlersThatReshapeTheirRows:
    def test_the_curated_summary_drops_every_field_it_does_not_name(self) -> None:
        from cqc_lem.api.routers.curated import _summary

        row = {"id": 4, "platform": "web", "url": "https://example.com/a", "author": "A", "title": "T",
               "status": "candidate", "licence": "cc", "link_only": 0, "canonical_id": None,
               **_POISON, "cookie": "POISON-cookie", "password": "POISON-password"}
        keys, values = _walk(_summary(row))
        assert not [k for k in keys if _CREDENTIAL_KEY.search(k)], keys
        assert not [v for v in values if "POISON" in v]

    def test_auth_session_returns_only_the_account_facts_it_names(self, api_client: Any) -> None:
        profile = {"subscription_tier": "pro", "subscription_status": "active", "timezone": "UTC",
                   "created_at": None, "onboarding_completed_at": None, "posts_approved": 3,
                   **_POISON, "password": "POISON-password", "access_token": "POISON-access"}
        with _session("observer"), \
             patch(f"{_AUTH}.get_user_email", return_value="owner@example.com"), \
             patch(f"{_AUTH}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_AUTH}.get_user_analytics_profile", return_value=profile), \
             patch(f"{_AUTH}.is_user_admin", return_value=False), \
             patch(f"{_AUTH}.strong_factor_deadline", return_value=None):
            resp = api_client.get("/api/auth/session", params={"session_token": _TOKEN})
        assert resp.status_code == 200
        keys, values = _walk(resp.json()["detail"])
        assert not [k for k in keys if _CREDENTIAL_KEY.search(k)], keys
        assert not [v for v in values if "POISON" in v]

    def test_profile_skills_returns_only_skills_and_topics(self, api_client: Any) -> None:
        profile = {"skills": ["python"], "li_at": "POISON-li-at", "cookies": [{"v": "POISON-cookie"}]}
        with _session("observer"), \
             patch(f"{_USER}.load_profile_for_user", return_value=profile), \
             patch(f"{_USER}.profile_niche_anchors", return_value=["python"]), \
             patch(f"{_USER}.get_engagement_preferences",
                   return_value={"focus_topics": ["python"], "li_at": "POISON-li-at"}):
            resp = api_client.get("/api/user/linkedin-profile-skills", params={"session_token": _TOKEN})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert set(detail) == {"skills", "adopted", "focus_topics"}
        assert "POISON" not in resp.text


# ---------------------------------------------------------------------------
# The once-a-day access record
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Just enough Redis for `SET key value NX EX seconds`."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self.calls.append({"key": key, "nx": nx, "ex": ex})
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


class TestObserverAccessRecord:
    @pytest.fixture
    def redis(self) -> Any:
        from cqc_lem.utilities import observer_access

        fake = _FakeRedis()
        with patch.object(observer_access, "shared_redis_client", return_value=fake), \
             patch(f"{_M}.claim_observer_access", observer_access.claim_observer_access):
            yield fake

    @staticmethod
    def _read_dms(api_client: Any, scope: str = "observer", token: str = _TOKEN) -> Any:
        with _session(scope), \
             patch(f"{_M}.get_scheduled_dms",
                   return_value={"dms": [], "total": 0, "page": 1, "page_size": 25}):
            return api_client.get("/api/dms", params={"session_token": token})

    def _access_rows(self, recorded: Any) -> list[Any]:
        from cqc_lem.utilities.db import AuthAuditEvent

        return [c for c in recorded.call_args_list if c.args[0] == AuthAuditEvent.OBSERVER_ACCESS]

    def test_the_first_read_of_the_day_writes_exactly_one_row_and_the_second_none(
            self, api_client: Any, redis: _FakeRedis) -> None:
        with patch.object(_kernel(), "record_auth_event", return_value=True) as recorded, \
             patch.object(_kernel(), "get_session_id", return_value=55):
            first = self._read_dms(api_client)
            second = self._read_dms(api_client)
            third = self._read_dms(api_client)
        assert first.status_code == second.status_code == third.status_code == 200
        rows = self._access_rows(recorded)
        assert len(rows) == 1
        kwargs = rows[0].kwargs
        assert kwargs["user_id"] == _UID
        assert kwargs["session_id"] == 55
        assert kwargs["details"] == {"scope": "observer", "path": "/dms"}

    def test_the_row_carries_no_body_address_or_user_agent(self, api_client: Any,
                                                          redis: _FakeRedis) -> None:
        with patch.object(_kernel(), "record_auth_event", return_value=True) as recorded, \
             patch.object(_kernel(), "get_session_id", return_value=55):
            self._read_dms(api_client)
        kwargs = self._access_rows(recorded)[0].kwargs
        assert set(kwargs) == {"user_id", "session_id", "details"}

    def test_the_claim_is_per_session_and_per_day(self, redis: _FakeRedis) -> None:
        from cqc_lem.utilities import observer_access

        assert observer_access.claim_observer_access("session-a") is True
        assert observer_access.claim_observer_access("session-a") is False
        assert observer_access.claim_observer_access("session-b") is True
        key = redis.calls[0]["key"]
        assert re.fullmatch(r"lem:observer_access:\d{8}:session-a", key)
        assert redis.calls[0]["nx"] is True and redis.calls[0]["ex"] == 25 * 3600

    def test_tomorrow_is_a_new_claim(self, redis: _FakeRedis) -> None:
        from cqc_lem.utilities import observer_access

        class _Day1(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return datetime(2026, 5, 1, 12, tzinfo=timezone.utc)

        class _Day2(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return datetime(2026, 5, 2, 0, 1, tzinfo=timezone.utc)

        with patch.object(observer_access, "datetime", _Day1):
            assert observer_access.claim_observer_access("s") is True
            assert observer_access.claim_observer_access("s") is False
        with patch.object(observer_access, "datetime", _Day2):
            assert observer_access.claim_observer_access("s") is True

    def test_two_different_sessions_each_get_a_row(self, api_client: Any, redis: _FakeRedis) -> None:
        with patch.object(_kernel(), "record_auth_event", return_value=True) as recorded, \
             patch.object(_kernel(), "get_session_id", return_value=55):
            self._read_dms(api_client)
            # A second session is a second token resolving to the same account.
            with patch(f"{_M}._db_resolve_session",
                       side_effect=lambda t: {"user_id": _UID, "scope": "observer"}), \
                 patch(f"{_M}.get_scheduled_dms",
                       return_value={"dms": [], "total": 0, "page": 1, "page_size": 25}):
                api_client.get("/api/dms", params={"session_token": "another-observer-token"})
        assert len(self._access_rows(recorded)) == 2

    def test_a_redis_outage_still_serves_the_read(self, api_client: Any) -> None:
        from cqc_lem.utilities import observer_access

        class _Down:
            def set(self, *args: Any, **kwargs: Any) -> None:
                raise ConnectionError("redis is down")

        with patch.object(observer_access, "shared_redis_client", return_value=_Down()), \
             patch(f"{_M}.claim_observer_access", observer_access.claim_observer_access), \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = self._read_dms(api_client)
        assert resp.status_code == 200
        assert len(self._access_rows(recorded)) == 1, "fail OPEN: the access is recorded, not lost"

    def test_no_redis_at_all_still_serves_the_read(self, api_client: Any) -> None:
        from cqc_lem.utilities import observer_access

        with patch.object(observer_access, "shared_redis_client", return_value=None), \
             patch(f"{_M}.claim_observer_access", observer_access.claim_observer_access), \
             patch.object(_kernel(), "record_auth_event", return_value=True):
            resp = self._read_dms(api_client)
        assert resp.status_code == 200

    @pytest.mark.parametrize("failure", ["record", "session_id"])
    def test_a_failed_audit_write_never_turns_a_read_into_an_error(
            self, api_client: Any, redis: _FakeRedis, failure: str) -> None:
        boom = RuntimeError("db down")
        with patch.object(_kernel(), "record_auth_event",
                          side_effect=boom if failure == "record" else None), \
             patch.object(_kernel(), "get_session_id",
                          side_effect=boom if failure == "session_id" else None, return_value=1):
            resp = self._read_dms(api_client)
        assert resp.status_code == 200

    @pytest.mark.parametrize("scope", ["agent", "full"])
    def test_other_scopes_write_no_access_row(self, api_client: Any, redis: _FakeRedis,
                                              scope: str) -> None:
        with patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = self._read_dms(api_client, scope=scope)
        assert resp.status_code == 200
        assert self._access_rows(recorded) == []

    def test_a_refused_observer_request_writes_no_access_row(self, api_client: Any,
                                                             redis: _FakeRedis) -> None:
        with _session("observer"), \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = api_client.put("/api/user/engagement-preferences",
                                  json={"session_token": _TOKEN, "tone": "casual"})
        assert resp.status_code == 403
        assert self._access_rows(recorded) == []

    def test_the_event_is_a_varchar_value_so_no_migration_is_needed(self) -> None:
        from cqc_lem.utilities.db import AuthAuditEvent

        assert AuthAuditEvent.OBSERVER_ACCESS == "observer_access"
        assert len("observer_access") <= 50  # auth_audit_log.event is VARCHAR(50)


def test_the_json_of_a_response_is_walkable() -> None:
    """The helpers above would pass vacuously on a structure they cannot see into."""
    keys, values = _walk(json.loads('{"a": {"b": [{"li_at": "x"}], "c": "y"}}'))
    assert keys == ["a", "b", "li_at", "c"]
    assert values == ["x", "y"]
