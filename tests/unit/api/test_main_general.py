"""Unit tests for general FastAPI endpoints in cqc_lem.api.main."""

import pytest
from unittest.mock import patch
from datetime import datetime

pytestmark = pytest.mark.unit

_MAIN = "cqc_lem.api.main"


@pytest.fixture(scope="module")
def client():
    """TestClient with all heavy module-level imports pre-mocked."""
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
    ]
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient
        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# GET /api/dashboard/stats/
# ---------------------------------------------------------------------------

class TestDashboardStats:
    BASE = "/api/dashboard/stats/"

    def test_missing_email_param_returns_422(self, client):
        resp = client.get(self.BASE)
        assert resp.status_code == 422

    def test_empty_email_returns_400(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=None):
            resp = client.get(self.BASE, params={"email": ""})
        assert resp.status_code == 400

    def test_unknown_user_returns_403(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=None):
            resp = client.get(self.BASE, params={"email": "ghost@example.com"})
        assert resp.status_code == 403

    _ZEROS = {"scheduled_this_week": 0, "pending_review": 0, "posted_total": 0}

    def test_known_user_with_no_posts_returns_zeros(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=1), \
             patch(f"{_MAIN}.get_dashboard_counts", return_value=dict(self._ZEROS)):
            resp = client.get(self.BASE, params={"email": "user@example.com"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == self._ZEROS

    def test_start_of_month_does_not_crash(self, client):
        # Regression: week_start was computed with replace(day=day-weekday()), which
        # goes out of range in the first days of a month (Wed the 1st → day=-1 → 500).
        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 7, 1, tzinfo=tz)  # Wednesday the 1st

        with patch(f"{_MAIN}.get_user_id", return_value=1), \
             patch(f"{_MAIN}.get_dashboard_counts", return_value=dict(self._ZEROS)), \
             patch(f"{_MAIN}.datetime", _FixedDatetime):
            resp = client.get(self.BASE, params={"email": "user@example.com"})
        assert resp.status_code == 200

    def test_returns_counts_from_db_helper_over_all_posts(self, client):
        # The endpoint now delegates to the SQL-aggregate helper (counts over ALL posts, not the
        # 10-oldest get_posts() slice that made these stale). It passes a tz-aware Monday week_start.
        counts = {"scheduled_this_week": 4, "pending_review": 1, "posted_total": 37}
        with patch(f"{_MAIN}.get_user_id", return_value=1), \
             patch(f"{_MAIN}.get_dashboard_counts", return_value=counts) as gdc:
            resp = client.get(self.BASE, params={"email": "user@example.com"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == counts
        user_id_arg, week_start_arg = gdc.call_args[0][0], gdc.call_args[0][1]
        assert user_id_arg == 1
        assert week_start_arg.weekday() == 0 and week_start_arg.hour == 0  # Monday 00:00


# ---------------------------------------------------------------------------
# GET /api/activity/
# ---------------------------------------------------------------------------

class TestGetActivity:
    BASE = "/api/activity/"

    def test_missing_email_returns_422(self, client):
        resp = client.get(self.BASE)
        assert resp.status_code == 422

    def test_unknown_user_returns_403(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=None):
            resp = client.get(self.BASE, params={"email": "nobody@example.com"})
        assert resp.status_code == 403

    def test_valid_user_returns_200_with_list(self, client):
        log_row = {
            "id": 1,
            "action_type": "POST",
            "result": "success",
            "post_id": 10,
            "post_url": "https://linkedin.com/p/123",
            "message": "Posted OK",
            "created_at": datetime(2024, 1, 15, 12, 0, 0),
        }
        with patch(f"{_MAIN}.get_user_id", return_value=5), \
             patch(f"{_MAIN}.get_recent_logs", return_value=[log_row]):
            resp = client.get(self.BASE, params={"email": "user@example.com"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert len(detail) == 1
        assert detail[0]["id"] == 1
        assert detail[0]["action_type"] == "POST"

    def test_empty_log_list_returns_empty_array(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=5), \
             patch(f"{_MAIN}.get_recent_logs", return_value=[]):
            resp = client.get(self.BASE, params={"email": "user@example.com"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == []


# ---------------------------------------------------------------------------
# PUT /api/user/
# ---------------------------------------------------------------------------

class TestUpdateUser:
    BASE = "/api/user/"

    def test_empty_email_returns_400(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=None):
            resp = client.put(self.BASE, json={"email": ""})
        assert resp.status_code == 400

    def test_unknown_user_returns_403(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=None):
            resp = client.put(self.BASE, json={"email": "unknown@example.com"})
        assert resp.status_code == 403

    def test_no_update_fields_returns_unchanged(self, client):
        # email present but no new_email/blog_url/sitemap_url
        with patch(f"{_MAIN}.get_user_id", return_value=3):
            resp = client.put(self.BASE, json={"email": "user@example.com"})
        assert resp.status_code == 200
        assert "unchanged" in resp.json()["detail"]

    def test_valid_update_returns_200(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=3), \
             patch(f"{_MAIN}.update_user", return_value=True):
            resp = client.put(self.BASE, json={"email": "user@example.com", "blog_url": "https://blog.example.com"})
        assert resp.status_code == 200
        assert "updated" in resp.json()["detail"]

    def test_update_user_returns_false_gives_404(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=3), \
             patch(f"{_MAIN}.update_user", return_value=False):
            resp = client.put(self.BASE, json={"email": "user@example.com", "blog_url": "https://blog.example.com"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/user_id/
# ---------------------------------------------------------------------------

class TestGetUserIdEndpoint:
    BASE = "/api/user_id/"

    def test_missing_email_returns_422(self, client):
        resp = client.get(self.BASE)
        assert resp.status_code == 422

    def test_empty_email_returns_400(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=None):
            resp = client.get(self.BASE, params={"email": ""})
        assert resp.status_code == 400

    def test_unknown_user_returns_403(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=None):
            resp = client.get(self.BASE, params={"email": "ghost@example.com"})
        assert resp.status_code == 403

    def test_valid_email_returns_user_id(self, client):
        with patch(f"{_MAIN}.get_user_id", return_value=42):
            resp = client.get(self.BASE, params={"email": "user@example.com"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == 42


# ---------------------------------------------------------------------------
# GET /api/auth/session
# ---------------------------------------------------------------------------

class TestAuthCheckSession:
    BASE = "/api/auth/session"

    def test_invalid_session_token_returns_401(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=None):
            resp = client.get(self.BASE, params={"session_token": "bad-token"})
        assert resp.status_code == 401

    def test_valid_session_returns_user_id_and_email(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=7), \
             patch(f"{_MAIN}.get_user_email", return_value="me@example.com"):
            resp = client.get(self.BASE, params={"session_token": "valid-token-abc"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["user_id"] == 7
        assert detail["email"] == "me@example.com"


# ---------------------------------------------------------------------------
# GET /api/user/settings
# ---------------------------------------------------------------------------

class TestGetUserSettings:
    BASE = "/api/user/settings"

    def test_invalid_session_returns_401(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=None):
            resp = client.get(self.BASE, params={"session_token": "bad-token"})
        assert resp.status_code == 401

    def test_valid_session_returns_subscription_and_preferences(self, client):
        from datetime import datetime
        sub = {
            "subscription_status": "active",
            "subscription_tier": "starter",
            "trial_started_at": None,
            "trial_ends_at": None,
            "stripe_customer_id": "cus_abc",
        }
        prefs = {
            "last_login_inactivate_delay": 90,
            "auto_schedule_posts": False,
        }
        with patch(f"{_MAIN}.get_session_user_id", return_value=5), \
             patch(f"{_MAIN}.get_user_subscription_info", return_value=sub), \
             patch(f"{_MAIN}.get_user_preferences", return_value=prefs), \
             patch(f"{_MAIN}.get_user_blog_url", return_value="https://blog.example.com"), \
             patch(f"{_MAIN}.get_user_sitemap_url", return_value="https://blog.example.com/sitemap.xml"), \
             patch(f"{_MAIN}.get_company_linked_in_url_for_user", return_value=None):
            resp = client.get(self.BASE, params={"session_token": "valid-tok"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["subscription"]["status"] == "active"
        assert detail["subscription"]["tier"] == "starter"
        assert detail["preferences"]["last_login_inactivate_delay"] == 90
        assert detail["blog_url"] == "https://blog.example.com"
        assert detail["sitemap_url"] == "https://blog.example.com/sitemap.xml"

    def test_none_subscription_returns_null_subscription(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=5), \
             patch(f"{_MAIN}.get_user_subscription_info", return_value=None), \
             patch(f"{_MAIN}.get_user_preferences", return_value=None), \
             patch(f"{_MAIN}.get_user_blog_url", return_value=None), \
             patch(f"{_MAIN}.get_user_sitemap_url", return_value=None), \
             patch(f"{_MAIN}.get_company_linked_in_url_for_user", return_value=None):
            resp = client.get(self.BASE, params={"session_token": "valid-tok"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["subscription"] is None
        assert detail["preferences"] is None


# ---------------------------------------------------------------------------
# GET /api/user/linkedin-profile
# ---------------------------------------------------------------------------

class TestGetUserLinkedInProfile:
    BASE = "/api/user/linkedin-profile"

    def test_invalid_session_returns_401(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=None):
            resp = client.get(self.BASE, params={"session_token": "bad-token"})
        assert resp.status_code == 401

    def test_valid_session_returns_profile_url(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=5), \
             patch(f"{_MAIN}.get_linkedin_profile_url_by_user_id",
                   return_value="https://www.linkedin.com/in/christopherqueen/"):
            resp = client.get(self.BASE, params={"session_token": "valid-tok"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["linkedin_profile_url"] == \
            "https://www.linkedin.com/in/christopherqueen/"

    def test_missing_profile_returns_null(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=5), \
             patch(f"{_MAIN}.get_linkedin_profile_url_by_user_id", return_value=None):
            resp = client.get(self.BASE, params={"session_token": "valid-tok"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["linkedin_profile_url"] is None


class TestVerificationPinInbound:
    """SendGrid Inbound Parse webhook that receives the user's PIN reply."""
    BASE = "/api/linkedin/verification-pin/inbound"

    def test_valid_reply_stores_pin(self, client):
        with patch(f"{_MAIN}.submit_pin_by_token", return_value=1) as m:
            resp = client.post(self.BASE, data={
                "to": "pin+abc123XYZ@parse.example.com",
                "text": "483920\n\nSent from my phone",
                "subject": "Re: verification code",
            })
        assert resp.status_code == 200
        assert resp.json()["detail"] == "accepted"
        m.assert_called_once_with("abc123XYZ", "483920")

    def test_reply_but_unknown_token_ignored(self, client):
        with patch(f"{_MAIN}.submit_pin_by_token", return_value=None):
            resp = client.post(self.BASE, data={
                "to": "pin+stale@parse.example.com", "text": "483920"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == "ignored"

    def test_no_token_ignored_without_calling_store(self, client):
        with patch(f"{_MAIN}.submit_pin_by_token") as m:
            resp = client.post(self.BASE, data={"to": "someone@else.com", "text": "483920"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == "ignored"
        m.assert_not_called()

    def test_no_pin_ignored(self, client):
        with patch(f"{_MAIN}.submit_pin_by_token") as m:
            resp = client.post(self.BASE, data={
                "to": "pin+abc@parse.example.com", "text": "I can't find the code"})
        assert resp.json()["detail"] == "ignored"
        m.assert_not_called()

    def test_endpoint_is_public_no_auth(self, client):
        # Under the public prefix — must not 401/403 even with no auth header.
        resp = client.post(self.BASE, data={"to": "x", "text": "y"})
        assert resp.status_code == 200
