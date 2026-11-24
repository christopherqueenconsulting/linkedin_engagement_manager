"""Calendly polling booking hook (#2658): flag, token handling, apply_booking transitions."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.platform.db.repositories import booking
from cqc_lem.utilities import booking_hook

TOKEN = "not-a-real-token"


class _Cur:
    """Scripted cursor: each execute() pops the next fetchone/fetchall result by SQL prefix."""

    def __init__(self, one=None, many=None):
        self.one = list(one or [])
        self.many = list(many or [])
        self.sql = []
        self.lastrowid = 99

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.one.pop(0) if self.one else None

    def fetchall(self):
        return self.many.pop(0) if self.many else []


def _run(cur, **kw):
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    args = dict(user_id=1, invitee_uri="inv/1", event_uri="ev/1", email="A@x.com", name="Ann", canceled=False)
    args.update(kw)
    with patch.object(booking, "db_cursor", return_value=ctx):
        return booking.apply_booking(**args)


def _events(cur):
    return [p for s, p in cur.sql if s.startswith("INSERT INTO lead_stage_events")]


def test_booking_matches_lead_by_email_and_writes_event():
    cur = _Cur(one=[None, (7,), (None, "warm")])
    assert _run(cur) == booking.BOOKING_APPLIED
    assert _events(cur) == [(7, 1, "warm", "meeting_booked", "booking", "inv/1")]
    assert any(s.startswith("INSERT INTO calendly_bookings") for s, _ in cur.sql)


def test_unmatched_booking_creates_lead():
    cur = _Cur(one=[None, None, (None, "cold")], many=[[]])
    assert _run(cur) == booking.BOOKING_CREATED
    assert any(s.startswith("INSERT INTO leads") for s, _ in cur.sql)
    assert _events(cur)[0][2] is None  # from_stage NULL for a new lead


def test_ambiguous_name_match_creates_lead_instead():
    cur = _Cur(one=[None, None, (None, "cold")], many=[[(1,), (2,)]])
    assert _run(cur) == booking.BOOKING_CREATED


def test_replay_changes_nothing():
    cur = _Cur(one=[(7, "active", None)])
    assert _run(cur) == booking.BOOKING_NOOP
    assert _events(cur) == []


def test_cancel_restores_prior_stage_with_history():
    cur = _Cur(one=[(7, "active", "hot"), ("meeting_booked", "warm")])
    assert _run(cur, canceled=True) == booking.BOOKING_CANCELED
    assert _events(cur) == [(7, 1, "meeting_booked", "hot", "booking", "canceled:inv/1")]


def test_cancel_of_unseen_or_already_canceled_is_noop():
    assert _run(_Cur(one=[None]), canceled=True) == booking.BOOKING_NOOP
    assert _run(_Cur(one=[(7, "canceled", None)]), canceled=True) == booking.BOOKING_NOOP


def test_cancel_leaves_operator_moved_stage_alone():
    cur = _Cur(one=[(7, "active", None), ("won", "warm")])
    assert _run(cur, canceled=True) == booking.BOOKING_CANCELED
    assert _events(cur) == []


def test_token_is_encrypted_under_its_own_aad(monkeypatch):
    monkeypatch.setenv("LEM_SECRET_KEY", "")
    seen = {}

    def fake_encrypt(value, user_id, field):
        seen["field"] = field
        return "sealed"

    cur = _Cur()
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    with patch.object(booking, "encryption_enabled", return_value=True), \
            patch.object(booking, "encrypt_secret", fake_encrypt), \
            patch.object(booking, "db_cursor", return_value=ctx):
        assert booking.upsert_calendly_token(1, TOKEN)
    assert seen["field"] == booking.CALENDLY_TOKEN_FIELD == "calendly_credentials.access_token"
    assert TOKEN not in str(cur.sql)


def test_token_aad_is_distinct():
    from cqc_lem.platform.db.repositories import linkedin_connections, users
    others = {users.SECRET_FIELD_COOKIE_VALUE, users.SECRET_FIELD_ACCESS_TOKEN,
              users.SECRET_FIELD_REFRESH_TOKEN, users.SECRET_FIELD_PASSWORD,
              linkedin_connections._ACCESS_FIELD}
    assert booking.CALENDLY_TOKEN_FIELD not in others


def test_token_refused_without_key():
    with patch.object(booking, "encryption_enabled", return_value=False):
        assert booking.upsert_calendly_token(1, TOKEN) is False


def test_flag_off_does_nothing(monkeypatch):
    monkeypatch.delenv("BOOKING_HOOK_ENABLED", raising=False)
    with patch.object(booking_hook.db, "get_calendly_user_ids") as users:
        assert booking_hook.poll_all_bookings() == "Booking hook disabled"
    users.assert_not_called()


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


def test_poll_applies_invitees_and_never_logs_token(monkeypatch, caplog):
    monkeypatch.setenv("BOOKING_HOOK_ENABLED", "true")
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        if url.endswith("/users/me"):
            return _resp({"resource": {"uri": "u/1"}})
        if url.endswith("/scheduled_events"):
            return _resp({"collection": [{"uri": "https://api.calendly.com/scheduled_events/E"}],
                          "pagination": {}})
        return _resp({"collection": [{"uri": "inv/1", "email": "a@x.com", "name": "Ann", "status": "active"},
                                     {"uri": "inv/2", "email": "b@x.com", "name": "Bo", "status": "canceled"}]})

    caplog.set_level(logging.DEBUG)
    with patch.object(booking_hook.requests, "get", fake_get), \
            patch.object(booking_hook.db, "get_calendly_user_ids", return_value=[1]), \
            patch.object(booking_hook.db, "get_calendly_token", return_value=TOKEN), \
            patch.object(booking_hook.db, "touch_calendly_polled"), \
            patch.object(booking_hook.db, "apply_booking", side_effect=["booked", "canceled"]) as apply:
        out = booking_hook.poll_all_bookings()
    assert apply.call_count == 2 and apply.call_args_list[1].args[-1] is True
    assert "booked" in out
    assert TOKEN not in caplog.text and TOKEN not in out


def test_failed_request_logs_no_token(caplog):
    import requests
    caplog.set_level(logging.DEBUG)
    with patch.object(booking_hook.requests, "get", side_effect=requests.ConnectionError(TOKEN)):
        assert booking_hook._get("/users/me", TOKEN) is None
    assert TOKEN not in caplog.text


def test_refused_status_returns_none():
    with patch.object(booking_hook.requests, "get", return_value=_resp({}, 401)):
        assert booking_hook._get("/users/me", TOKEN) is None


def test_poll_user_without_token_is_empty():
    with patch.object(booking_hook.db, "get_calendly_token", return_value=None):
        assert booking_hook.poll_user_bookings(1) == {}


@pytest.mark.parametrize("val,expected", [("1", True), ("true", True), ("", False), ("no", False)])
def test_flag_parsing(monkeypatch, val, expected):
    monkeypatch.setenv("BOOKING_HOOK_ENABLED", val)
    assert booking_hook.booking_hook_enabled() is expected
