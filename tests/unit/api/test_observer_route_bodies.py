"""What an observer's list responses actually contain, route by route, key for key.

`test_observer_exposure.py` proves the readers SELECT explicit columns. This file is the other lock,
at the response: each list route is called with an `observer` session over a getter that returns rows
seeded with everything an observer must never see — a credential, a session handle, a third party's
email address and phone number — and the body is held to three claims:

(a) no credential key appears;
(b) the third party's email and phone appear nowhere in the JSON (a `has_recipient_email` boolean is
    the only way a screen learns an address exists);
(c) every item's key set EQUALS an explicit whitelist written out in this file.

The whitelist is deliberately a second, literal copy of `_OBSERVER_ROW_FIELDS`: adding a phone or an
email field to a handler's response, or to the table, makes the two disagree and fails here with the
route's name in the test id. A full session is also covered, to show the projection is observer-only.
"""

import re
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_OUTREACH = "cqc_lem.api.routers.outreach"

_UID = 61
_TOKEN = "observer-body-token"

_EMAIL = "lead@example.com"
_PHONE = "+1-555-0100"
_POISON_FIELDS: dict[str, Any] = {
    "li_at": "POISON-li-at-cookie-value",
    "cookie": "POISON-cookie-value",
    "password": "POISON-password-value",
    "token": "POISON-token-value",
    "session_id": "POISON-session-id-value",
    "email": _EMAIL,
    "recipient_email": _EMAIL,
    "contact_email": _EMAIL,
    "phone": _PHONE,
    "mobile": _PHONE,
    "phone_number": _PHONE,
}
_POISON_VALUES = [v for v in _POISON_FIELDS.values()]
_CREDENTIAL_KEY = re.compile(r"li_at|cookie|passw|token|secret|session|credential|api_key|bearer",
                             re.IGNORECASE)
# Any key that could hold a way to reach a person. `has_recipient_email` is the one exception: a
# boolean that says an address exists, never the address.
_CONTACT_KEY = re.compile(r"phone|mobile|mail|\btel", re.IGNORECASE)
_ALLOWED_CONTACT_KEYS = {"has_recipient_email"}


def _contact_keys(keys: list[str]) -> list[str]:
    return [k for k in keys if _CONTACT_KEY.search(k) and k not in _ALLOWED_CONTACT_KEYS]

_WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

# --- the explicit whitelists, one per route -----------------------------------------------------
_DMS = {"id", "user_id", "recipient_profile_url", "recipient_name", "message", "source",
        "scheduled_time", "status", "created_at", "updated_at"}
_CONNECTION_REQUESTS = {"id", "user_id", "recipient_profile_url", "recipient_name", "message", "source",
                        "icp_score", "reasons", "failure_reason", "status", "attempts", "created_at",
                        "updated_at", "has_recipient_email"}
_OUTREACH_TARGETS = {"id", "user_id", "target_profile_url", "target_name", "stage", "status",
                     "context_url", "draft_text", "notes", "created_at", "updated_at"}
_CATCHUP_TOUCHES = {"id", "user_id", "profile_url", "person_name", "event_type", "event_detail",
                    "event_period", "score", "message", "status", "created_at", "updated_at"}
_LEAD_SIGNALS = {"id", "user_id", "source", "channel", "person_name", "person_profile_url", "thread_key",
                 "snippet", "score", "matched_signals", "post_id", "context_url", "draft_response",
                 "status", "delivery_attempts", "created_at", "updated_at"}
_LEADS = {"id", "user_id", "person_key", "person_name", "person_profile_url", "score", "icp_score",
          "engagement_score", "stage", "signals", "signal_count", "reasons", "next_action",
          "first_signal_at", "last_signal_at", "manual_stage", "notes", "dismissed", "created_at",
          "updated_at"}


def _row(fields: set[str]) -> dict[str, Any]:
    """A plausible row carrying exactly `fields`, with a datetime where one is expected."""
    out: dict[str, Any] = {}
    for name in sorted(fields):
        if name.endswith("_at") or name == "scheduled_time":
            out[name] = _WHEN
        elif name == "has_recipient_email":
            out[name] = 1
        elif name in ("id", "user_id", "post_id", "score", "icp_score", "engagement_score",
                      "attempts", "delivery_attempts", "signal_count", "dismissed"):
            out[name] = 7
        else:
            out[name] = f"value-of-{name}"
    return out


def _poisoned(fields: set[str]) -> dict[str, Any]:
    return {**_row(fields), **_POISON_FIELDS}


# route id -> (url, whitelist, list key, patches factory)
def _dms(rows: list[dict[str, Any]]) -> list[Any]:
    return [patch(f"{_M}.get_scheduled_dms",
                  return_value={"dms": rows, "total": len(rows), "page": 1, "page_size": 25})]


def _connection_requests(rows: list[dict[str, Any]]) -> list[Any]:
    return [patch(f"{_M}.get_connection_requests",
                  return_value={"requests": rows, "total": len(rows), "page": 1, "page_size": 25})]


def _leads(rows: list[dict[str, Any]]) -> list[Any]:
    return [patch(f"{_M}.get_leads", return_value={"leads": rows, "total": len(rows), "page": 1,
                                                  "page_size": 100}),
            patch(f"{_M}.count_hot_leads", return_value=0)]


def _lead_signals(rows: list[dict[str, Any]]) -> list[Any]:
    return [patch(f"{_M}.get_lead_signals", return_value={"signals": rows, "total": len(rows), "page": 1,
                                                         "page_size": 25}),
            patch(f"{_M}.count_new_lead_signals", return_value=0)]


def _outreach_targets(rows: list[dict[str, Any]]) -> list[Any]:
    return [patch(f"{_OUTREACH}.get_outreach_targets",
                  return_value={"targets": rows, "total": len(rows), "page": 1, "page_size": 25})]


def _catchup_touches(rows: list[dict[str, Any]]) -> list[Any]:
    return [patch(f"{_M}.get_catchup_touches",
                  return_value={"touches": rows, "total": len(rows), "page": 1, "page_size": 25})]


_ROUTES = [
    pytest.param("/api/dms", _DMS, "dms", _dms, id="dms"),
    pytest.param("/api/leads", _LEADS, "leads", _leads, id="leads"),
    pytest.param("/api/lead_signals", _LEAD_SIGNALS, "signals", _lead_signals, id="lead_signals"),
    pytest.param("/api/outreach/targets", _OUTREACH_TARGETS, "targets", _outreach_targets,
                 id="outreach_targets"),
    pytest.param("/api/catchup/touches", _CATCHUP_TOUCHES, "touches", _catchup_touches,
                 id="catchup_touches"),
    pytest.param("/api/connection_requests", _CONNECTION_REQUESTS, "requests", _connection_requests,
                 id="connection_requests"),
]


@pytest.fixture(autouse=True)
def _account_without_a_strong_factor() -> Any:
    """Pin the factor count so the real resolver runs without a database (see test_observer_scope)."""
    with patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=0):
        yield


@pytest.fixture(autouse=True)
def _quiet() -> Any:
    from cqc_lem.api import main

    with patch.object(main, "record_auth_event", return_value=True), \
         patch(f"{_M}.claim_observer_access", return_value=False), \
         patch.object(main, "get_session_id", return_value=1):
        yield


def _walk(obj: Any) -> tuple[list[str], list[str]]:
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


def _call(api_client: Any, scope: str, url: str, factory: Any, rows: list[dict[str, Any]]) -> Any:
    patches = factory(rows)
    with patch(f"{_M}._db_resolve_session",
               side_effect=lambda t: {"user_id": _UID, "scope": scope} if t == _TOKEN else None):
        for p in patches:
            p.start()
        try:
            return api_client.get(url, params={"session_token": _TOKEN})
        finally:
            for p in patches:
                p.stop()


class TestObserverListBodies:
    @pytest.mark.parametrize("url,whitelist,list_key,factory", _ROUTES)
    def test_each_item_has_exactly_the_whitelisted_keys(self, api_client: Any, url: str,
                                                        whitelist: set[str], list_key: str,
                                                        factory: Any) -> None:
        resp = _call(api_client, "observer", url, factory, [_poisoned(whitelist), _poisoned(whitelist)])
        assert resp.status_code == 200, resp.text
        items = resp.json()["detail"][list_key]
        assert len(items) == 2
        for item in items:
            assert set(item) == whitelist, (
                f"{url}: extra={sorted(set(item) - whitelist)} missing={sorted(whitelist - set(item))}")

    @pytest.mark.parametrize("url,whitelist,list_key,factory", _ROUTES)
    def test_no_credential_key_appears_anywhere_in_the_body(self, api_client: Any, url: str,
                                                            whitelist: set[str], list_key: str,
                                                            factory: Any) -> None:
        resp = _call(api_client, "observer", url, factory, [_poisoned(whitelist)])
        keys, _ = _walk(resp.json())
        assert not [k for k in keys if _CREDENTIAL_KEY.search(k)], keys
        for poison in ("POISON-li-at-cookie-value", "POISON-cookie-value", "POISON-password-value",
                       "POISON-token-value", "POISON-session-id-value"):
            assert poison not in resp.text, poison

    @pytest.mark.parametrize("url,whitelist,list_key,factory", _ROUTES)
    def test_a_third_partys_email_and_phone_appear_nowhere(self, api_client: Any, url: str,
                                                           whitelist: set[str], list_key: str,
                                                           factory: Any) -> None:
        resp = _call(api_client, "observer", url, factory, [_poisoned(whitelist)])
        assert _EMAIL not in resp.text
        assert _PHONE not in resp.text
        assert "555-0100" not in resp.text
        keys, values = _walk(resp.json())
        assert not _contact_keys(keys), keys
        assert not [v for v in values if v in _POISON_VALUES]

    @pytest.mark.parametrize("url,whitelist,list_key,factory", _ROUTES)
    def test_an_empty_list_is_still_an_empty_list(self, api_client: Any, url: str,
                                                  whitelist: set[str], list_key: str,
                                                  factory: Any) -> None:
        resp = _call(api_client, "observer", url, factory, [])
        assert resp.status_code == 200
        assert resp.json()["detail"][list_key] == []

    @pytest.mark.parametrize("url,whitelist,list_key,factory", _ROUTES)
    def test_whitelisted_values_survive_the_projection(self, api_client: Any, url: str,
                                                       whitelist: set[str], list_key: str,
                                                       factory: Any) -> None:
        """Stripping must not eat the data an observer is meant to see."""
        resp = _call(api_client, "observer", url, factory, [_row(whitelist)])
        item = resp.json()["detail"][list_key][0]
        assert item["id"] == 7
        assert item["user_id"] == 7


class TestTheProjectionIsObserverOnly:
    @pytest.mark.parametrize("url,whitelist,list_key,factory", _ROUTES)
    def test_a_full_session_gets_the_getters_rows_untouched(self, api_client: Any, url: str,
                                                            whitelist: set[str], list_key: str,
                                                            factory: Any) -> None:
        """The SPA's own session is unchanged: the getters' column tuples are its contract."""
        resp = _call(api_client, "full", url, factory, [_row(whitelist)])
        assert resp.status_code == 200
        assert set(resp.json()["detail"][list_key][0]) == whitelist


class TestTheWhitelistTableMatchesThisFile:
    """The handler-side table and the literals above must agree route for route."""

    @pytest.mark.parametrize("kind,expected", [
        ("dms", _DMS), ("connection_requests", _CONNECTION_REQUESTS),
        ("outreach_targets", _OUTREACH_TARGETS), ("catchup_touches", _CATCHUP_TOUCHES),
        ("lead_signals", _LEAD_SIGNALS), ("leads", _LEADS),
    ])
    def test_the_table_equals_the_literal_whitelist(self, kind: str, expected: set[str]) -> None:
        from cqc_lem.api import main

        assert set(main._OBSERVER_ROW_FIELDS[kind]) == expected

    def test_no_whitelist_names_a_contact_or_credential_field(self) -> None:
        from cqc_lem.api import main

        for kind, fields in main._OBSERVER_ROW_FIELDS.items():
            for name in fields:
                assert not _CREDENTIAL_KEY.search(name), (kind, name)
                assert name in _ALLOWED_CONTACT_KEYS or not _CONTACT_KEY.search(name), (kind, name)

    @pytest.mark.parametrize("kind,getter,cols", [
        ("dms", "get_scheduled_dms", "_SCHED_DM_COLS"),
        ("connection_requests", "get_connection_requests", "_CONN_REQ_COLS"),
        ("outreach_targets", "get_outreach_targets", "_OUTREACH_COLS"),
        ("catchup_touches", "get_catchup_touches", "_CATCHUP_COLS"),
        ("lead_signals", "get_lead_signals", "_LEAD_SIGNAL_COLS"),
        ("leads", "get_leads", "_LEAD_COLS"),
    ])
    def test_the_table_is_exactly_what_the_reader_selects(self, kind: str, getter: str,
                                                          cols: str) -> None:
        """Drift in either direction — a column added to the SELECT, or a field added here — fails."""
        from cqc_lem.api import main
        from cqc_lem.platform.db.repositories import outreach

        selected = set()
        for column in getattr(outreach, cols):
            selected.add(column.rsplit(" AS ", 1)[-1].strip())
        assert set(main._OBSERVER_ROW_FIELDS[kind]) == selected, getter

    def test_the_observer_surface_has_no_path_parameters(self) -> None:
        """So the path stored on an `observer_access` row can only ever be one of these literals."""
        from cqc_lem.api import main

        for path in main._OBSERVER_SESSION_SURFACE:
            assert not re.search(r"[{}:*?]", path), path
        assert main.SESSION_SCOPE_OBSERVER not in main.__dict__.get("_SCOPE_PATTERNS", {})
