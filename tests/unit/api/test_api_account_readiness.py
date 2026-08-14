"""Unit tests for GET /api/user/account-readiness."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"


def _patches(*, oauth, session, password, sub_status, lat, display_name="Jordan Alvarez"):
    return [
        patch(f"{_M}.get_session_user_id", return_value=42),
        patch(f"{_USER}.get_user_linkedin_display_name", return_value=display_name),
        patch(f"{_USER}.get_user_token_info",
              return_value={"access_token": "tok"} if oauth else None),
        patch(f"{_USER}.has_linkedin_session", return_value=session),
        patch(f"{_USER}.has_linkedin_password", return_value=password),
        patch(f"{_USER}.get_user_subscription_info",
              return_value={"subscription_status": sub_status}),
        patch(f"{_USER}.get_user_geo",
              return_value={"latitude": lat} if lat is not None else None),
    ]


def _run(api_client, **kw):
    ctxs = _patches(**kw)
    for c in ctxs:
        c.start()
    try:
        return api_client.get("/api/user/account-readiness?session_token=tok")
    finally:
        for c in ctxs:
            c.stop()


class TestAccountReadiness:
    def test_ready_when_all_required_ok(self, api_client):
        resp = _run(api_client, oauth=True, session=True, password=False, sub_status="active", lat=1.0)
        assert resp.status_code == 200
        d = resp.json()["detail"]
        assert d["ready"] is True

    def test_session_satisfied_by_password_alone(self, api_client):
        resp = _run(api_client, oauth=True, session=False, password=True, sub_status="trial", lat=None)
        d = resp.json()["detail"]
        # location is not required → still ready
        assert d["ready"] is True
        item = next(i for i in d["items"] if i["key"] == "linkedin_session")
        assert item["ok"] is True

    def test_not_ready_when_no_engagement_login(self, api_client):
        resp = _run(api_client, oauth=True, session=False, password=False, sub_status="active", lat=1.0)
        d = resp.json()["detail"]
        assert d["ready"] is False
        item = next(i for i in d["items"] if i["key"] == "linkedin_session")
        assert item["ok"] is False and item["required"] is True

    def test_not_ready_when_no_oauth(self, api_client):
        resp = _run(api_client, oauth=False, session=True, password=False, sub_status="active", lat=1.0)
        assert resp.json()["detail"]["ready"] is False

    def test_not_ready_when_subscription_inactive(self, api_client):
        resp = _run(api_client, oauth=True, session=True, password=False, sub_status="canceled", lat=1.0)
        assert resp.json()["detail"]["ready"] is False

    def test_location_is_optional(self, api_client):
        resp = _run(api_client, oauth=True, session=True, password=False, sub_status="active", lat=None)
        d = resp.json()["detail"]
        assert d["ready"] is True
        loc = next(i for i in d["items"] if i["key"] == "location")
        assert loc["required"] is False and loc["ok"] is False

    def test_display_name_is_required(self, api_client):
        # Issue #731: without it every DM reply check is UNKNOWN and follow-ups stop silently.
        resp = _run(api_client, oauth=True, session=True, password=False, sub_status="active", lat=1.0,
                    display_name=None)
        d = resp.json()["detail"]
        assert d["ready"] is False
        item = next(i for i in d["items"] if i["key"] == "linkedin_display_name")
        assert item["ok"] is False and item["required"] is True

    def test_display_name_present_clears_the_item(self, api_client):
        resp = _run(api_client, oauth=True, session=True, password=False, sub_status="active", lat=1.0)
        item = next(i for i in resp.json()["detail"]["items"] if i["key"] == "linkedin_display_name")
        assert item["ok"] is True

    def test_hint_points_at_the_cookie_not_the_password(self, api_client):
        # Issue #745: the password path is deprecated, so the required-item hint must not
        # advertise it as an equal option.
        resp = _run(api_client, oauth=True, session=False, password=False, sub_status="active", lat=1.0)
        item = next(i for i in resp.json()["detail"]["items"] if i["key"] == "linkedin_session")
        assert "cookie" in item["hint"] and "password" not in item["hint"]


class TestCookieMigrationFlag:
    """Issue #745 §5.4 — accounts whose only login is a stored password get the one-time prompt."""

    def test_flagged_when_password_only(self, api_client):
        resp = _run(api_client, oauth=True, session=False, password=True, sub_status="active", lat=1.0)
        assert resp.json()["detail"]["cookie_migration_needed"] is True

    def test_not_flagged_once_a_cookie_exists(self, api_client):
        resp = _run(api_client, oauth=True, session=True, password=True, sub_status="active", lat=1.0)
        assert resp.json()["detail"]["cookie_migration_needed"] is False

    def test_not_flagged_without_a_password(self, api_client):
        resp = _run(api_client, oauth=True, session=True, password=False, sub_status="active", lat=1.0)
        assert resp.json()["detail"]["cookie_migration_needed"] is False


class TestAccountReadinessAuth:
    def test_401_invalid_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.get("/api/user/account-readiness?session_token=bad")
        assert resp.status_code == 401
