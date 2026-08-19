"""Issue #1450 — `/api/admin/users`: the admin's view of other accounts and the ONE write on it.

Patch targets follow the same rule as the feedback panel's suite (#1154): the auth kernel
(`get_session_user_id`) stays in `main` and is reached as a host-module attribute at request time,
so it is patched there; everything the ROUTER imported by name — `is_user_admin`, the user queries,
`record_auth_event` — is patched in the router's own globals.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_ADMIN = {"id": 1, "email": "admin@x.com", "is_admin": True}
_NON_ADMIN = {"id": 2, "email": "user@x.com", "is_admin": False}

_ROUTER = "cqc_lem.api.routers.admin"


def _auth(user):
    """Signed in as `user`, with the step-up gate satisfied (its own refusal has its own test)."""
    return (
        patch("cqc_lem.api.main.get_session_user_id", return_value=user["id"]),
        patch(f"{_ROUTER}.is_user_admin", return_value=user["is_admin"]),
        patch("cqc_lem.api.routers.user._require_step_up", return_value=None),
    )


def _row(**overrides):
    row = {
        "id": 5, "email": "member@x.com", "linkedin_email": None, "is_admin": 0,
        "subscription_status": "active", "subscription_tier": "professional",
        "trial_ends_at": None, "linkedin_connection_status": "connected",
        "last_login": None, "signed_up_at": None, "activated_at": None,
        "disabled_at": None,
    }
    row.update(overrides)
    return row


class TestUserList:
    def test_401_without_a_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = api_client.get("/api/admin/users", params={"session_token": "bad"})
        assert r.status_code == 401

    def test_403_for_a_signed_in_non_admin(self, api_client):
        session, admin, _ = _auth(_NON_ADMIN)
        with session, admin:
            r = api_client.get("/api/admin/users", params={"session_token": "tok"})
        assert r.status_code == 403

    def test_returns_the_page_and_its_total(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin, \
             patch(f"{_ROUTER}.list_users_for_admin", return_value=[_row()]) as lister, \
             patch(f"{_ROUTER}.count_users_for_admin", return_value=42), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value=set()):
            r = api_client.get("/api/admin/users",
                               params={"session_token": "tok", "limit": 10, "offset": 20})
        assert r.status_code == 200
        detail = r.json()["detail"]
        assert detail["total"] == 42
        assert detail["limit"] == 10 and detail["offset"] == 20
        assert detail["items"][0]["email"] == "member@x.com"
        assert lister.call_args.kwargs["limit"] == 10
        assert lister.call_args.kwargs["offset"] == 20

    def test_filters_reach_the_query(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin, \
             patch(f"{_ROUTER}.list_users_for_admin", return_value=[]) as lister, \
             patch(f"{_ROUTER}.count_users_for_admin", return_value=0):
            r = api_client.get("/api/admin/users", params={
                "session_token": "tok", "q": "acme", "subscription_status": "trial",
                "linkedin_connection_status": "expired", "is_admin": "true"})
        assert r.status_code == 200
        kwargs = lister.call_args.kwargs
        assert kwargs["search"] == "acme"
        assert kwargs["subscription_status"] == "trial"
        assert kwargs["linkedin_connection_status"] == "expired"
        assert kwargs["is_admin"] is True

    def test_an_unknown_status_is_422_not_a_query_parameter(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin:
            r = api_client.get("/api/admin/users",
                               params={"session_token": "tok", "subscription_status": "banana"})
        assert r.status_code == 422

    def test_the_page_size_is_capped(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin:
            r = api_client.get("/api/admin/users", params={"session_token": "tok", "limit": 5000})
        assert r.status_code == 422

    def test_an_unreadable_page_is_503_not_an_empty_list(self, api_client):
        # "No account matches that filter" is an answer an operator acts on; a DB fault must not
        # be able to give it.
        session, admin, _ = _auth(_ADMIN)
        with session, admin, \
             patch(f"{_ROUTER}.list_users_for_admin", return_value=None), \
             patch(f"{_ROUTER}.count_users_for_admin", return_value=0):
            r = api_client.get("/api/admin/users", params={"session_token": "tok"})
        assert r.status_code == 503

    def test_an_unreadable_total_is_503_too(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin, \
             patch(f"{_ROUTER}.list_users_for_admin", return_value=[]), \
             patch(f"{_ROUTER}.count_users_for_admin", return_value=None):
            r = api_client.get("/api/admin/users", params={"session_token": "tok"})
        assert r.status_code == 503

    def test_a_genuinely_empty_page_is_still_a_200(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin, \
             patch(f"{_ROUTER}.list_users_for_admin", return_value=[]), \
             patch(f"{_ROUTER}.count_users_for_admin", return_value=0):
            r = api_client.get("/api/admin/users", params={"session_token": "tok"})
        assert r.status_code == 200
        assert r.json()["detail"]["items"] == []
        assert r.json()["detail"]["total"] == 0

    def test_an_allowlist_admin_is_badged_as_one(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin, \
             patch(f"{_ROUTER}.list_users_for_admin",
                   return_value=[_row(email="Boss@X.com", is_admin=0)]), \
             patch(f"{_ROUTER}.count_users_for_admin", return_value=1), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value={"boss@x.com"}):
            r = api_client.get("/api/admin/users", params={"session_token": "tok"})
        item = r.json()["detail"]["items"][0]
        assert item["is_admin"] is True
        assert item["admin_via_allowlist"] is True
        assert item["admin_via_column"] is False


class TestUserDetail:
    def test_404_for_an_unknown_user(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin, patch(f"{_ROUTER}.get_user_for_admin", return_value=None):
            r = api_client.get("/api/admin/users/999", params={"session_token": "tok"})
        assert r.status_code == 404

    def test_403_for_a_non_admin(self, api_client):
        session, admin, _ = _auth(_NON_ADMIN)
        with session, admin:
            r = api_client.get("/api/admin/users/5", params={"session_token": "tok"})
        assert r.status_code == 403

    def test_no_credential_or_coordinate_ever_reaches_the_response(self, api_client):
        # The query does not select them; this is the second half of that guarantee — the
        # serializer names its fields, so a column added to the SELECT cannot ride out silently.
        row = _row(public_uid="pub-1", city="Atlanta", country="US",
                   password="hunter2", access_token="tok", refresh_token="rtok",
                   proxy_url="http://u:p@proxy", reply_inbound_token="secret",
                   latitude="33.7", longitude="-84.4",
                   stripe_customer_id="cus_1", stripe_subscription_id="sub_1")
        session, admin, _ = _auth(_ADMIN)
        with session, admin, patch(f"{_ROUTER}.get_user_for_admin", return_value=row), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value=set()):
            r = api_client.get("/api/admin/users/5", params={"session_token": "tok"})
        assert r.status_code == 200
        body = r.text
        for leaked in ("hunter2", "rtok", "proxy", "secret", "33.7", "-84.4", "cus_1", "sub_1"):
            assert leaked not in body
        detail = r.json()["detail"]
        assert detail["city"] == "Atlanta"
        assert "latitude" not in detail and "proxy_url" not in detail


class TestRoleChange:
    def _post(self, api_client, user_id, is_admin, token="tok"):
        return api_client.post(f"/api/admin/users/{user_id}/admin",
                               json={"session_token": token, "is_admin": is_admin})

    def test_401_without_a_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = self._post(api_client, 5, True, token="bad")
        assert r.status_code == 401

    def test_403_for_a_non_admin(self, api_client):
        session, admin, step_up = _auth(_NON_ADMIN)
        with session, admin, step_up:
            r = self._post(api_client, 5, True)
        assert r.status_code == 403

    def test_step_up_refusal_stops_the_write(self, api_client):
        from fastapi import HTTPException
        session, admin, _ = _auth(_ADMIN)
        refusal = HTTPException(status_code=403, detail={"code": "step_up_required"})
        with session, admin, \
             patch("cqc_lem.api.routers.user._require_step_up", side_effect=refusal), \
             patch(f"{_ROUTER}.set_user_admin") as writer:
            r = self._post(api_client, 5, True)
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "step_up_required"
        writer.assert_not_called()

    def test_404_for_an_unknown_target(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=None):
            r = self._post(api_client, 999, True)
        assert r.status_code == 404

    def test_grant_writes_and_audits_against_the_target(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, is_admin=0)), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value=set()), \
             patch(f"{_ROUTER}.set_user_admin", return_value=True) as writer, \
             patch(f"{_ROUTER}.record_auth_event", return_value=True) as audit:
            r = self._post(api_client, 5, True)
        assert r.status_code == 200
        assert r.json()["detail"] == {"user_id": 5, "is_admin": True, "changed": True}
        writer.assert_called_once_with(5, True)
        assert str(audit.call_args[0][0]) == "admin_granted"
        # Keyed on the TARGET, actor in details — so the change shows in the affected user's own
        # Security card and the actor is still recorded.
        assert audit.call_args.kwargs["user_id"] == 5
        assert audit.call_args.kwargs["details"] == {"actor_user_id": 1}

    def test_revoke_writes_when_another_admin_remains(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, is_admin=1)), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value=set()), \
             patch(f"{_ROUTER}.count_admin_users", return_value=2), \
             patch(f"{_ROUTER}.set_user_admin", return_value=True) as writer, \
             patch(f"{_ROUTER}.record_auth_event", return_value=True) as audit:
            r = self._post(api_client, 5, False)
        assert r.status_code == 200
        writer.assert_called_once_with(5, False)
        assert str(audit.call_args[0][0]) == "admin_revoked"

    def test_the_last_admin_cannot_be_demoted(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, is_admin=1)), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value=set()), \
             patch(f"{_ROUTER}.count_admin_users", return_value=1), \
             patch(f"{_ROUTER}.set_user_admin") as writer:
            r = self._post(api_client, 5, False)
        assert r.status_code == 409
        assert "last admin" in r.json()["detail"]
        writer.assert_not_called()

    def test_an_unreadable_admin_count_is_503_not_a_guess(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, is_admin=1)), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value=set()), \
             patch(f"{_ROUTER}.count_admin_users", return_value=None), \
             patch(f"{_ROUTER}.set_user_admin") as writer:
            r = self._post(api_client, 5, False)
        assert r.status_code == 503
        writer.assert_not_called()

    def test_an_admin_cannot_demote_themselves(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=1, is_admin=1)), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value=set()), \
             patch(f"{_ROUTER}.count_admin_users", return_value=5), \
             patch(f"{_ROUTER}.set_user_admin") as writer:
            r = self._post(api_client, 1, False)
        assert r.status_code == 409
        assert "your own admin access" in r.json()["detail"]
        writer.assert_not_called()

    def test_an_allowlist_admin_cannot_be_revoked_here(self, api_client):
        # The column is already 0, so the UPDATE would change nothing and report success while the
        # person stays an admin.
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin",
                   return_value=_row(id=5, email="Boss@X.com", is_admin=0)), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value={"boss@x.com"}), \
             patch(f"{_ROUTER}.set_user_admin") as writer:
            r = self._post(api_client, 5, False)
        assert r.status_code == 409
        assert "ADMIN_USER_EMAILS" in r.json()["detail"]
        writer.assert_not_called()

    def test_a_redundant_grant_is_an_explicit_no_op(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, is_admin=1)), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value=set()), \
             patch(f"{_ROUTER}.set_user_admin") as writer, \
             patch(f"{_ROUTER}.record_auth_event") as audit:
            r = self._post(api_client, 5, True)
        assert r.status_code == 200
        assert r.json()["detail"]["changed"] is False
        writer.assert_not_called()
        audit.assert_not_called()

    def test_a_write_that_did_not_land_is_not_reported_as_a_change(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, is_admin=0)), \
             patch(f"{_ROUTER}.admin_email_allowlist", return_value=set()), \
             patch(f"{_ROUTER}.set_user_admin", return_value=False), \
             patch(f"{_ROUTER}.record_auth_event") as audit:
            r = self._post(api_client, 5, True)
        assert r.status_code == 500
        audit.assert_not_called()


class TestUserDisable:
    """Issue #1603 — per-user disable, honoured by `get_active_user_ids()`."""

    def _post(self, api_client, user_id, disabled, token="tok"):
        return api_client.post(f"/api/admin/users/{user_id}/disable",
                               json={"session_token": token, "disabled": disabled})

    def test_401_without_a_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = self._post(api_client, 5, True, token="bad")
        assert r.status_code == 401

    def test_403_for_a_non_admin(self, api_client):
        session, admin, step_up = _auth(_NON_ADMIN)
        with session, admin, step_up:
            r = self._post(api_client, 5, True)
        assert r.status_code == 403

    def test_step_up_refusal_stops_the_write(self, api_client):
        from fastapi import HTTPException
        session, admin, _ = _auth(_ADMIN)
        refusal = HTTPException(status_code=403, detail={"code": "step_up_required"})
        with session, admin, \
             patch("cqc_lem.api.routers.user._require_step_up", side_effect=refusal), \
             patch(f"{_ROUTER}.set_user_disabled") as writer:
            r = self._post(api_client, 5, True)
        assert r.status_code == 403
        writer.assert_not_called()

    def test_an_admin_cannot_disable_themselves(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.set_user_disabled") as writer:
            r = self._post(api_client, 1, True)
        assert r.status_code == 409
        assert "own account" in r.json()["detail"]
        writer.assert_not_called()

    def test_404_for_an_unknown_target(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=None):
            r = self._post(api_client, 999, True)
        assert r.status_code == 404

    def test_disable_writes_and_audits_against_the_target(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, disabled_at=None)), \
             patch(f"{_ROUTER}.set_user_disabled", return_value=True) as writer, \
             patch(f"{_ROUTER}.record_auth_event", return_value=True) as audit:
            r = self._post(api_client, 5, True)
        assert r.status_code == 200
        assert r.json()["detail"] == {"user_id": 5, "disabled": True, "changed": True}
        writer.assert_called_once_with(5, True)
        assert str(audit.call_args[0][0]) == "admin_user_disabled"
        assert audit.call_args.kwargs["user_id"] == 5
        assert audit.call_args.kwargs["details"] == {"actor_user_id": 1}

    def test_enable_writes_and_audits_the_reverse_event(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, disabled_at="2026-01-01")), \
             patch(f"{_ROUTER}.set_user_disabled", return_value=True) as writer, \
             patch(f"{_ROUTER}.record_auth_event", return_value=True) as audit:
            r = self._post(api_client, 5, False)
        assert r.status_code == 200
        writer.assert_called_once_with(5, False)
        assert str(audit.call_args[0][0]) == "admin_user_enabled"

    def test_a_redundant_disable_is_an_explicit_no_op(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, disabled_at="2026-01-01")), \
             patch(f"{_ROUTER}.set_user_disabled") as writer, \
             patch(f"{_ROUTER}.record_auth_event") as audit:
            r = self._post(api_client, 5, True)
        assert r.status_code == 200
        assert r.json()["detail"]["changed"] is False
        writer.assert_not_called()
        audit.assert_not_called()

    def test_a_write_that_did_not_land_is_not_reported_as_a_change(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5, disabled_at=None)), \
             patch(f"{_ROUTER}.set_user_disabled", return_value=False), \
             patch(f"{_ROUTER}.record_auth_event") as audit:
            r = self._post(api_client, 5, True)
        assert r.status_code == 500
        audit.assert_not_called()


class TestSubscriptionGrant:
    """Issue #1603 — a one-time, time-boxed comp; never a standing override (§3.4.2)."""

    def _post(self, api_client, user_id, days, token="tok"):
        return api_client.post(f"/api/admin/users/{user_id}/subscription-grant",
                               json={"session_token": token, "days": days})

    def test_401_without_a_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = self._post(api_client, 5, 14, token="bad")
        assert r.status_code == 401

    def test_403_for_a_non_admin(self, api_client):
        session, admin, step_up = _auth(_NON_ADMIN)
        with session, admin, step_up:
            r = self._post(api_client, 5, 14)
        assert r.status_code == 403

    def test_step_up_refusal_stops_the_write(self, api_client):
        from fastapi import HTTPException
        session, admin, _ = _auth(_ADMIN)
        refusal = HTTPException(status_code=403, detail={"code": "step_up_required"})
        with session, admin, \
             patch("cqc_lem.api.routers.user._require_step_up", side_effect=refusal), \
             patch(f"{_ROUTER}.grant_subscription_extension") as writer:
            r = self._post(api_client, 5, 14)
        assert r.status_code == 403
        writer.assert_not_called()

    def test_404_for_an_unknown_target(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=None):
            r = self._post(api_client, 999, 14)
        assert r.status_code == 404

    def test_days_out_of_range_is_422(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up:
            r = self._post(api_client, 5, 0)
        assert r.status_code == 422
        r = self._post(api_client, 5, 400)
        assert r.status_code == 422

    def test_grants_and_audits_against_the_target(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin",
                   return_value=_row(id=5, subscription_current_period_end="2026-02-01T00:00:00")), \
             patch(f"{_ROUTER}.grant_subscription_extension", return_value=True) as writer, \
             patch(f"{_ROUTER}.record_auth_event", return_value=True) as audit:
            r = self._post(api_client, 5, 14)
        assert r.status_code == 200
        detail = r.json()["detail"]
        assert detail["user_id"] == 5
        assert detail["days_granted"] == 14
        writer.assert_called_once_with(5, 14)
        assert str(audit.call_args[0][0]) == "admin_subscription_granted"
        assert audit.call_args.kwargs["user_id"] == 5
        assert audit.call_args.kwargs["details"] == {"actor_user_id": 1, "days_granted": 14}

    def test_a_failed_grant_is_500(self, api_client):
        session, admin, step_up = _auth(_ADMIN)
        with session, admin, step_up, \
             patch(f"{_ROUTER}.get_user_for_admin", return_value=_row(id=5)), \
             patch(f"{_ROUTER}.grant_subscription_extension", return_value=False), \
             patch(f"{_ROUTER}.record_auth_event") as audit:
            r = self._post(api_client, 5, 14)
        assert r.status_code == 500
        audit.assert_not_called()


class TestAuditLog:
    """Issue #1603 — the admin viewer over `auth_audit_log`. Never returns `ip_hash`."""

    def test_401_without_a_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = api_client.get("/api/admin/audit-log", params={"session_token": "bad"})
        assert r.status_code == 401

    def test_403_for_a_non_admin(self, api_client):
        session, admin, _ = _auth(_NON_ADMIN)
        with session, admin:
            r = api_client.get("/api/admin/audit-log", params={"session_token": "tok"})
        assert r.status_code == 403

    def test_returns_the_page_and_never_leaks_ip_hash(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        row = {"id": 1, "user_id": 5, "email": "member@x.com", "event": "admin_granted",
               "success": 1, "user_agent": "curl", "session_id": None,
               "details": {"actor_user_id": 1}, "created_at": "2026-01-01T00:00:00",
               "ip_hash": "deadbeef"}
        with session, admin, \
             patch(f"{_ROUTER}.list_auth_audit_log_for_admin", return_value=[row]), \
             patch(f"{_ROUTER}.count_auth_audit_log_for_admin", return_value=1):
            r = api_client.get("/api/admin/audit-log", params={"session_token": "tok"})
        assert r.status_code == 200
        detail = r.json()["detail"]
        assert detail["total"] == 1
        assert "deadbeef" not in r.text
        assert "ip_hash" not in detail["items"][0]
        assert detail["items"][0]["event"] == "admin_granted"

    def test_filters_by_user_id(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin, \
             patch(f"{_ROUTER}.list_auth_audit_log_for_admin", return_value=[]) as lister, \
             patch(f"{_ROUTER}.count_auth_audit_log_for_admin", return_value=0):
            r = api_client.get("/api/admin/audit-log",
                               params={"session_token": "tok", "user_id": 5})
        assert r.status_code == 200
        assert lister.call_args.kwargs["user_id"] == 5

    def test_an_unreadable_page_is_503(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin, \
             patch(f"{_ROUTER}.list_auth_audit_log_for_admin", return_value=None), \
             patch(f"{_ROUTER}.count_auth_audit_log_for_admin", return_value=0):
            r = api_client.get("/api/admin/audit-log", params={"session_token": "tok"})
        assert r.status_code == 503

    def test_an_unreadable_total_is_503_too(self, api_client):
        session, admin, _ = _auth(_ADMIN)
        with session, admin, \
             patch(f"{_ROUTER}.list_auth_audit_log_for_admin", return_value=[]), \
             patch(f"{_ROUTER}.count_auth_audit_log_for_admin", return_value=None):
            r = api_client.get("/api/admin/audit-log", params={"session_token": "tok"})
        assert r.status_code == 503


class TestScopeSurface:
    """A restricted session must not reach this surface.

    `_scope_allows` matches on PATH, not method, so an entry added "just to read the list" would
    hand that token the role write too.
    """

    def test_the_user_management_paths_are_in_no_scope_surface(self):
        from cqc_lem.api import main
        for surface in main._SCOPE_SURFACES.values():
            for path in surface:
                assert not path.startswith("/admin/users")
                assert not path.startswith("/admin/audit-log")

    def test_an_agent_session_is_refused(self):
        from cqc_lem.api import main
        assert main._scope_allows(main.SESSION_SCOPE_AGENT, "/api/admin/users") is False
        assert main._scope_allows(main.SESSION_SCOPE_AGENT, "/api/admin/users/5/admin") is False
        assert main._scope_allows(main.SESSION_SCOPE_AGENT, "/api/admin/users/5/disable") is False
        assert main._scope_allows(
            main.SESSION_SCOPE_AGENT, "/api/admin/users/5/subscription-grant") is False
        assert main._scope_allows(main.SESSION_SCOPE_AGENT, "/api/admin/audit-log") is False
