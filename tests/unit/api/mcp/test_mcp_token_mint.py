"""Minting, rotating and listing MCP tokens (plan §3.5; owner rulings O2, O3, O11).

The routes live in `api/routers/mcp_tokens.py`. Every test names the account state it runs in:
a signed-in human (`_main.get_session_user_id`), the rollout flag, a strong factor and step-up.
"""

import logging
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.platform.db.repositories.auth import SessionStoreUnavailable

from .conftest import BASE_URL

pytestmark = pytest.mark.unit

_R = "cqc_lem.api.routers.mcp_tokens"
_USER = "cqc_lem.api.routers.user"
_UID = 42
_MINTED = "lem_mcp_" + "mintcanary" * 4


class _State:
    """The patched collaborators of one test, by name."""

    def __init__(self, stack: ExitStack) -> None:
        self.stack = stack

    def patch(self, target: str, **kwargs: Any) -> MagicMock:
        return self.stack.enter_context(patch(target, **kwargs))


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> Iterator[_State]:
    """A signed-in human with a strong factor and a satisfied step-up, flag on, nothing minted."""
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    with ExitStack() as stack:
        s = _State(stack)
        s.uid = s.patch("cqc_lem.api.main.get_session_user_id", return_value=_UID)
        s.factor = s.patch(f"{_R}.has_strong_factor", return_value=True)
        s.step_up = s.patch(f"{_USER}.step_up_satisfied", return_value=True)
        s.patch(f"{_USER}.available_methods", return_value=["passkey"])
        s.denied = s.patch(f"{_USER}.record_auth_event", return_value=True)
        s.create = s.patch(f"{_R}.create_session", return_value=_MINTED)
        s.sid = s.patch(f"{_R}.get_session_id", return_value=77)
        s.count = s.patch(f"{_R}.count_active_sessions", return_value=0)
        s.audit = s.patch(f"{_R}.record_auth_event", return_value=True)
        s.notify = s.patch(f"{_R}.notify_token_event", return_value=True)
        s.mint_rate = s.patch(f"{_R}.claim_mint", return_value=True)
        yield s


def _mint(client: Any, **body: Any) -> Any:
    return client.post("/api/user/mcp-token", json={"session_token": "tok", **body})


class TestMint:
    def test_a_mint_returns_the_token_once_with_the_url_and_defaults(self, api_client: Any,
                                                                     state: _State) -> None:
        resp = _mint(api_client)
        assert resp.status_code == 200, resp.text
        detail = resp.json()["detail"]
        assert detail["session_token"] == _MINTED and detail["session_id"] == 77
        assert detail["mcp_url"] == f"{BASE_URL}/api/mcp"
        assert detail["scope"] == "mcp" and detail["ttl_days"] == 30
        assert detail["label"] == "Coding agent"
        expires = datetime.fromisoformat(detail["expires_at"].replace("Z", "+00:00"))
        assert abs(expires - (datetime.now(timezone.utc) + timedelta(days=30))) < timedelta(
            minutes=1)
        kwargs = state.create.call_args.kwargs
        assert kwargs["scope"] == "mcp" and kwargs["ttl_hours"] == 30 * 24
        assert kwargs["token_prefix"] == "lem_mcp_"

    def test_outreach_is_an_opt_in_that_mints_mcp_outreach(self, api_client: Any,
                                                           state: _State) -> None:
        resp = _mint(api_client, outreach=True, ttl_days=90, label="Claude Code - laptop")
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"]["scope"] == "mcp_outreach"
        assert state.create.call_args.kwargs["scope"] == "mcp_outreach"
        assert state.create.call_args.kwargs["ttl_hours"] == 90 * 24

    def test_it_is_audited_and_emailed_without_the_token(self, api_client: Any,
                                                         state: _State) -> None:
        _mint(api_client, label="Codex desk")
        event = state.audit.call_args
        assert str(event.args[0]) == "mcp_token_minted"
        assert event.kwargs["user_id"] == _UID and event.kwargs["session_id"] == 77
        assert event.kwargs["details"] == {"scope": "mcp", "ttl_days": 30, "label": "Codex desk"}
        state.notify.assert_called_once_with(_UID, "minted", label="Codex desk", scope="mcp")
        assert _MINTED not in repr(state.audit.call_args_list) + repr(state.notify.call_args_list)

    def test_a_failed_email_does_not_fail_the_mint(self, api_client: Any, state: _State) -> None:
        state.notify.return_value = False
        assert _mint(api_client).status_code == 200

    @pytest.mark.parametrize("body", [{"ttl_days": 0}, {"ttl_days": 91}, {"ttl_days": 365},
                                      {"label": "a" * 121}, {"label": "me@example.com"},
                                      {"label": "two\nlines"}, {"label": "<b>"}])
    def test_label_and_ttl_bounds(self, api_client: Any, state: _State, body: dict) -> None:
        assert _mint(api_client, **body).status_code == 422
        state.create.assert_not_called()

    def test_ten_live_tokens_is_the_cap(self, api_client: Any, state: _State) -> None:
        state.count.return_value = 10
        resp = _mint(api_client)
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "mcp_token_limit"
        state.create.assert_not_called()
        assert state.count.call_args.args == (_UID, frozenset({"mcp", "mcp_outreach"}))

    def test_an_unreadable_count_is_503_never_a_mint(self, api_client: Any,
                                                     state: _State) -> None:
        state.count.return_value = None
        assert _mint(api_client).status_code == 503
        state.create.assert_not_called()

    def test_the_hourly_mint_rate(self, api_client: Any, state: _State) -> None:
        state.mint_rate.return_value = False
        resp = _mint(api_client)
        assert resp.status_code == 429
        assert resp.json()["detail"]["code"] == "mcp_mint_rate_limited"
        state.create.assert_not_called()

    def test_a_failed_insert_is_500(self, api_client: Any, state: _State) -> None:
        state.create.return_value = None
        assert _mint(api_client).status_code == 500
        state.audit.assert_not_called()

    def test_the_token_is_never_logged(self, api_client: Any, state: _State,
                                       caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="cqc-lem"):
            assert _mint(api_client).status_code == 200
        for record in caplog.records:
            assert _MINTED not in record.getMessage() and _MINTED not in str(record.__dict__)


class TestTheCeremony:
    def test_the_flag_off_is_404(self, api_client: Any, state: _State,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVER_ENABLED", "false")
        assert _mint(api_client).status_code == 404
        assert api_client.post("/api/user/mcp-token/rotate",
                               json={"session_token": "tok", "session_id": 3}).status_code == 404
        state.step_up.assert_not_called()

    def test_no_session_is_401(self, api_client: Any, state: _State) -> None:
        state.uid.return_value = None
        assert _mint(api_client).status_code == 401

    def test_no_strong_factor_is_403_before_any_ceremony(self, api_client: Any,
                                                         state: _State) -> None:
        state.factor.return_value = False
        resp = _mint(api_client)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "mcp_needs_strong_factor"
        state.step_up.assert_not_called()
        state.create.assert_not_called()

    def test_a_missing_step_up_is_403_and_audited(self, api_client: Any, state: _State) -> None:
        state.step_up.return_value = False
        resp = _mint(api_client)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "step_up_required"
        assert str(state.denied.call_args.args[0]) == "step_up_denied"
        assert state.denied.call_args.kwargs["details"] == {"action": "mint_mcp_token"}
        state.create.assert_not_called()

    def test_the_extension_scope_never_satisfies_the_step_up(self, api_client: Any,
                                                             state: _State) -> None:
        _mint(api_client)
        assert state.step_up.call_args.kwargs.get("extension_scope_ok") is False


class TestRotate:
    ROW = {"id": 5, "label": "Cursor - desk", "scope": "mcp_outreach",
           "created_at": datetime(2026, 11, 1), "expires_at": datetime(2026, 11, 8)}

    def _rotate(self, client: Any, session_id: int = 5) -> Any:
        return client.post("/api/user/mcp-token/rotate",
                           json={"session_token": "tok", "session_id": session_id})

    def test_it_revokes_first_then_mints_the_same_label_scope_and_ttl(self, api_client: Any,
                                                                      state: _State) -> None:
        order = MagicMock()
        state.patch(f"{_R}.get_owned_session", return_value=dict(self.ROW))
        revoke = state.patch(f"{_R}.revoke_session", return_value=True)
        order.attach_mock(revoke, "revoke")
        order.attach_mock(state.create, "create")
        resp = self._rotate(api_client)
        assert resp.status_code == 200, resp.text
        assert [c[0] for c in order.mock_calls] == ["revoke", "create"]
        revoke.assert_called_once_with(_UID, 5)
        kwargs = state.create.call_args.kwargs
        assert (kwargs["label"], kwargs["scope"], kwargs["ttl_hours"]) == (
            "Cursor - desk", "mcp_outreach", 7 * 24)
        assert resp.json()["detail"]["session_token"] == _MINTED
        event = state.audit.call_args
        assert str(event.args[0]) == "mcp_token_rotated"
        assert event.kwargs["details"]["old_session_id"] == 5
        state.notify.assert_called_once_with(_UID, "rotated", label="Cursor - desk",
                                             scope="mcp_outreach")

    def test_ownership_and_scope_are_checked_in_the_lookup(self, api_client: Any,
                                                           state: _State) -> None:
        owned = state.patch(f"{_R}.get_owned_session", return_value=None)
        revoke = state.patch(f"{_R}.revoke_session", return_value=True)
        assert self._rotate(api_client, 999).status_code == 404
        assert owned.call_args.args == (_UID, 999, frozenset({"mcp", "mcp_outreach"}))
        revoke.assert_not_called()
        state.create.assert_not_called()

    def test_a_lost_revoke_race_mints_nothing(self, api_client: Any, state: _State) -> None:
        state.patch(f"{_R}.get_owned_session", return_value=dict(self.ROW))
        state.patch(f"{_R}.revoke_session", return_value=False)
        assert self._rotate(api_client).status_code == 404
        state.create.assert_not_called()

    def test_an_unreadable_lookup_is_503(self, api_client: Any, state: _State) -> None:
        state.patch(f"{_R}.get_owned_session", side_effect=SessionStoreUnavailable())
        assert self._rotate(api_client).status_code == 503

    def test_rotate_needs_the_same_ceremony(self, api_client: Any, state: _State) -> None:
        state.factor.return_value = False
        assert self._rotate(api_client).status_code == 403
        state.factor.return_value = True
        state.step_up.return_value = False
        assert self._rotate(api_client).status_code == 403

    @pytest.mark.parametrize("created,expires,days", [
        (datetime(2026, 1, 1), datetime(2026, 1, 1, 12), 1),
        (datetime(2026, 1, 1), datetime(2026, 9, 1), 90),
        (None, None, 30),
    ])
    def test_the_granted_ttl_is_clamped(self, created: Any, expires: Any, days: int) -> None:
        from cqc_lem.api.routers.mcp_tokens import _granted_ttl_days

        assert _granted_ttl_days({"created_at": created, "expires_at": expires}) == days


class TestInfo:
    def _info(self, client: Any) -> Any:
        return client.get("/api/user/mcp-token/info", params={"session_token": "tok"})

    def test_the_listing_is_token_free_and_greys_out_dead_rows(self, api_client: Any,
                                                               state: _State) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [
            {"id": 3, "label": "Codex", "scope": "mcp", "created_at": now - timedelta(days=2),
             "last_seen_at": now, "expires_at": now + timedelta(days=28), "revoked_at": None},
            {"id": 2, "label": "Old", "scope": "mcp_outreach", "created_at": now,
             "last_seen_at": None, "expires_at": now - timedelta(days=1), "revoked_at": now},
        ]
        state.patch(f"{_R}.list_sessions_by_scope", return_value=rows)
        state.patch(f"{_R}.read_activity",
                    return_value={3: {"calls_7d": 12, "last_client_name": "codex"}})
        resp = self._info(api_client)
        assert resp.status_code == 200, resp.text
        detail = resp.json()["detail"]
        assert detail["enabled"] is True and detail["mcp_url"] == f"{BASE_URL}/api/mcp"
        first, second = detail["tokens"]
        assert first["calls_7d"] == 12 and first["last_client_name"] == "codex"
        assert (first["revoked"], first["expired"]) == (False, False)
        assert (second["revoked"], second["expired"]) == (True, True)
        assert second["calls_7d"] is None
        assert "session_token" not in resp.text and "lem_mcp_" not in resp.text

    def test_unreadable_activity_is_null_never_zero(self, api_client: Any,
                                                    state: _State) -> None:
        state.patch(f"{_R}.list_sessions_by_scope", return_value=[
            {"id": 3, "label": "x", "scope": "mcp", "created_at": None, "last_seen_at": None,
             "expires_at": None, "revoked_at": None}])
        state.patch(f"{_R}.read_activity", return_value=None)
        token = self._info(api_client).json()["detail"]["tokens"][0]
        assert token["calls_7d"] is None and token["last_client_name"] is None

    def test_an_unreadable_list_is_503(self, api_client: Any, state: _State) -> None:
        state.patch(f"{_R}.list_sessions_by_scope", return_value=None)
        assert self._info(api_client).status_code == 503

    def test_the_flag_off_still_lists_so_tokens_stay_revocable(
            self, api_client: Any, state: _State, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVER_ENABLED", "false")
        state.patch(f"{_R}.list_sessions_by_scope", return_value=[])
        detail = self._info(api_client).json()["detail"]
        assert detail == {"enabled": False, "mcp_url": f"{BASE_URL}/api/mcp", "tokens": []}

    def test_no_base_url_is_a_null_url(self, api_client: Any, state: _State,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
        state.patch(f"{_R}.list_sessions_by_scope", return_value=[])
        monkeypatch.setenv("PUBLIC_BASE_URL", "")
        assert self._info(api_client).json()["detail"]["mcp_url"] is None

    def test_no_session_is_401(self, api_client: Any, state: _State) -> None:
        state.uid.return_value = None
        assert self._info(api_client).status_code == 401


class TestNotify:
    def test_no_address_warns_and_sends_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        from cqc_lem.api.mcp.notify import notify_token_event

        with patch("cqc_lem.utilities.db.get_user_email", return_value=None), \
             patch("cqc_lem.utilities.email.send_mcp_token_email") as send, \
             caplog.at_level(logging.WARNING, logger="cqc-lem"):
            assert notify_token_event(1, "minted") is False
        send.assert_not_called()
        assert any("not sent" in r.getMessage() for r in caplog.records)

    def test_a_raising_sender_never_escapes(self, caplog: pytest.LogCaptureFixture) -> None:
        from cqc_lem.api.mcp.notify import notify_token_event

        with patch("cqc_lem.utilities.db.get_user_email", return_value="a@example.com"), \
             patch("cqc_lem.utilities.email.send_mcp_token_email",
                   side_effect=RuntimeError("smtp")), \
             caplog.at_level(logging.WARNING, logger="cqc-lem"):
            assert notify_token_event(1, "rotated") is False
        assert any("email failed" in r.getMessage() for r in caplog.records)

    def test_a_sent_notice(self) -> None:
        from cqc_lem.api.mcp.notify import notify_token_event

        with patch("cqc_lem.utilities.db.get_user_email", return_value="a@example.com"), \
             patch("cqc_lem.utilities.email.send_mcp_token_email", return_value=True) as send:
            assert notify_token_event(1, "exposed", label="L", scope="full") is True
        send.assert_called_once_with("a@example.com", "exposed", label="L", scope="full")

    @pytest.mark.parametrize("kind,needle,priority", [
        ("minted", "A new agent connection was created", False),
        ("rotated", "was rotated", False),
        ("exposed", "revoked an exposed", True),
    ])
    def test_the_email_names_the_label_escaped_and_never_a_token(self, kind: str, needle: str,
                                                                 priority: bool) -> None:
        from cqc_lem.utilities import email

        with patch.object(email, "_dispatch_email", return_value=True) as dispatch:
            assert email.send_mcp_token_email("a@example.com", kind, label="<i>Codex</i>",
                                              scope="mcp_outreach") is True
        _to, _subject, html = dispatch.call_args.args
        assert needle in html
        assert "&lt;i&gt;Codex&lt;/i&gt;" in html and "<i>Codex</i>" not in html
        assert "lem_mcp_" not in html
        assert dispatch.call_args.kwargs["high_priority"] is priority
