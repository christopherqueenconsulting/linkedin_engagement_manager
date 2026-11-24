"""What two account reads hand an MCP seat: `/user/settings` billing, `/user/security` events.

`/user/settings` is on the `mcp` read surface (it backs `lem_whoami`), so it is driven here through
the REAL resolver with the dispatch binding; billing must not leave for any machine seat. The
Security card is a human page, and its recent-events window must not fill with `mcp_*` rows.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api.mcp.dispatch import DISPATCH_HEADER, dispatch_secret

pytestmark = pytest.mark.unit

_USER = "cqc_lem.api.routers.user"
_UID = 91
_TOKEN = "lem_mcp_" + "settings" * 4
_SUBSCRIPTION = {"subscription_status": "active", "subscription_tier": "pro",
                 "trial_started_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                 "trial_ends_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
                 "stripe_customer_id": "cus_canary"}


@pytest.fixture
def settings_reads() -> Iterator[MagicMock]:
    """Every collaborator of `/user/settings`, the billing read returned for inspection."""
    with ExitStack() as stack:
        billing = stack.enter_context(patch(f"{_USER}.get_user_subscription_info",
                                            return_value=dict(_SUBSCRIPTION)))
        stack.enter_context(patch(f"{_USER}.get_user_preferences", return_value=None))
        stack.enter_context(patch(f"{_USER}.get_user_blog_url", return_value=None))
        stack.enter_context(patch(f"{_USER}.get_user_sitemap_url", return_value=None))
        stack.enter_context(patch(f"{_USER}.get_company_linked_in_url_for_user",
                                  return_value=None))
        mode = MagicMock()
        mode.value = "automate"
        stack.enter_context(patch(f"{_USER}.resolve_engagement_mode", return_value=mode))
        yield billing


class TestSettingsBilling:
    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
    def test_an_mcp_seat_gets_no_billing_and_none_is_read(self, api_client: Any,
                                                          settings_reads: MagicMock,
                                                          scope: str) -> None:
        with patch("cqc_lem.api.main._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": scope} if t == _TOKEN
                   else None):
            resp = api_client.get("/api/user/settings", params={"session_token": _TOKEN},
                                  headers={DISPATCH_HEADER: dispatch_secret()})
        assert resp.status_code == 200, resp.text
        detail = resp.json()["detail"]
        assert detail["subscription"] is None
        assert "cus_canary" not in resp.text
        settings_reads.assert_not_called()

    def test_the_owner_still_gets_billing(self, api_client: Any,
                                          settings_reads: MagicMock) -> None:
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_UID):
            resp = api_client.get("/api/user/settings", params={"session_token": "tok"})
        assert resp.status_code == 200, resp.text
        subscription = resp.json()["detail"]["subscription"]
        assert subscription["stripe_customer_id"] == "cus_canary"
        assert subscription["tier"] == "pro"
        settings_reads.assert_called_once_with(_UID)


class TestSecurityCardEvents:
    def _security(self, api_client: Any, events: Any) -> tuple[Any, MagicMock]:
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.list_user_sessions", return_value=[]), \
             patch(f"{_USER}.list_auth_audit_events", return_value=events) as audit, \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value="me@example.com"):
            resp = api_client.get("/api/user/security", params={"session_token": "tok"})
        return resp, audit

    def test_mcp_rows_are_excluded_in_the_query(self, api_client: Any) -> None:
        resp, audit = self._security(api_client, [
            {"event": "login_success", "success": 1, "user_agent": "UA", "created_at": None}])
        assert resp.status_code == 200
        assert audit.call_args.args == (_UID,)
        assert audit.call_args.kwargs == {"exclude_prefix": "mcp_", "limit": 50}
        assert resp.json()["detail"]["recent_events"][0]["user_agent"] == "UA"

    def test_an_unreadable_history_is_an_empty_list_as_before(self, api_client: Any) -> None:
        resp, _ = self._security(api_client, None)
        assert resp.status_code == 200
        assert resp.json()["detail"]["recent_events"] == []

    def test_every_mcp_event_carries_the_excluded_prefix(self) -> None:
        from cqc_lem.api.routers.user import _MCP_EVENT_PREFIX
        from cqc_lem.platform.db.enums import AuthAuditEvent

        mcp_events = [e for e in AuthAuditEvent if e.name.startswith("MCP_")]
        assert mcp_events and all(str(e).startswith(_MCP_EVENT_PREFIX) for e in mcp_events)
        assert not [e for e in AuthAuditEvent
                    if str(e).startswith(_MCP_EVENT_PREFIX) and not e.name.startswith("MCP_")]
