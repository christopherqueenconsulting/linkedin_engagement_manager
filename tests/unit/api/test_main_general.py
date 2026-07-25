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

    def test_created_at_serialized_as_explicit_utc(self, client):
        log_row = {
            "id": 1, "action_type": "post", "result": "success", "post_id": 10,
            "post_url": "https://linkedin.com/p/123", "message": "ok",
            "created_at": datetime(2024, 1, 15, 12, 0, 0),  # naive == UTC
        }
        with patch(f"{_MAIN}.get_user_id", return_value=5), \
             patch(f"{_MAIN}.get_recent_logs", return_value=[log_row]):
            resp = client.get(self.BASE, params={"email": "user@example.com"})
        assert resp.json()["detail"][0]["created_at"] == "2024-01-15T12:00:00Z"

    def test_synthetic_feedpost_url_blanked(self, client):
        rows = [
            {"id": 1, "action_type": "comment", "result": "success", "post_id": None,
             "post_url": "feedpost://abc123", "message": "nice", "created_at": datetime(2024, 1, 1)},
            {"id": 2, "action_type": "post", "result": "success", "post_id": 3,
             "post_url": "https://www.linkedin.com/feed/update/x", "message": "up",
             "created_at": datetime(2024, 1, 2)},
        ]
        with patch(f"{_MAIN}.get_user_id", return_value=5), \
             patch(f"{_MAIN}.get_recent_logs", return_value=rows):
            detail = client.get(self.BASE, params={"email": "user@example.com"}).json()["detail"]
        assert detail[0]["post_url"] is None                                      # feedpost:// hidden
        assert detail[1]["post_url"] == "https://www.linkedin.com/feed/update/x"  # real permalink kept


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
        # Buffer knobs fall back to the defaults for a user who never set them (issue #544)
        from cqc_lem.utilities.db import (DEFAULT_CONTENT_BUFFER_DAYS,
                                          DEFAULT_CONTENT_BUFFER_MAX_POSTS)
        assert detail["preferences"]["content_buffer_days"] == DEFAULT_CONTENT_BUFFER_DAYS
        assert detail["preferences"]["content_buffer_max_posts"] == DEFAULT_CONTENT_BUFFER_MAX_POSTS
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


class TestCommentNotificationInbound:
    """SendGrid Inbound Parse webhook for forwarded LinkedIn comment notifications."""
    BASE = "/api/linkedin/comment-notification/inbound"
    _DB = "cqc_lem.utilities.db.get_user_id_by_reply_token"

    def test_comment_triggers_debounced_sweep(self, client):
        with patch(self._DB, return_value=7), \
             patch(f"{_MAIN}._reply_sweep_debounced", return_value=True), \
             patch(f"{_MAIN}.sweep_reply_comments") as sweep:
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "subject": "Chris, Jane commented on your post",
                "text": "great post!"})
        assert resp.status_code == 200 and resp.json()["detail"] == "accepted"
        sweep.apply_async.assert_called_once()
        assert sweep.apply_async.call_args.kwargs["kwargs"] == {"user_id": 7}

    def test_reaction_email_ignored(self, client):
        with patch(self._DB, return_value=7), \
             patch(f"{_MAIN}.sweep_reply_comments") as sweep:
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "subject": "Jane liked your post", "text": ""})
        assert resp.json()["detail"] == "ignored"
        sweep.apply_async.assert_not_called()

    def test_unknown_token_ignored(self, client):
        with patch(self._DB, return_value=None), \
             patch(f"{_MAIN}.sweep_reply_comments") as sweep:
            resp = client.post(self.BASE, data={
                "to": "reply+stale@parse.example.com",
                "subject": "someone commented on your post"})
        assert resp.json()["detail"] == "ignored"
        sweep.apply_async.assert_not_called()

    def test_no_token_ignored(self, client):
        with patch(f"{_MAIN}.sweep_reply_comments") as sweep:
            resp = client.post(self.BASE, data={"to": "x@y.com", "subject": "commented on your post"})
        assert resp.json()["detail"] == "ignored"
        sweep.apply_async.assert_not_called()

    def test_debounced_second_notification(self, client):
        with patch(self._DB, return_value=7), \
             patch(f"{_MAIN}._reply_sweep_debounced", return_value=False), \
             patch(f"{_MAIN}.sweep_reply_comments") as sweep:
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "subject": "Jane commented on your post"})
        assert resp.json()["detail"] == "debounced"
        sweep.apply_async.assert_not_called()

    def test_gmail_forwarding_confirmation_auto_clicks(self, client):
        body = ("please click the link below to confirm the request:\n"
                "https://mail.google.com/mail/vf-%5Babc%5D-xyz\nConfirmation code: 123456789")
        got = type("R", (), {"status_code": 200})()
        with patch(self._DB, return_value=7), \
             patch(f"{_MAIN}.sweep_reply_comments") as sweep, \
             patch(f"{_MAIN}.requests.get", return_value=got) as rget:
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "from": "forwarding-noreply@google.com",
                "subject": "Gmail Forwarding Confirmation", "text": body})
        assert resp.json()["detail"] == "confirmed"
        rget.assert_called_once()
        assert rget.call_args.args[0] == "https://mail.google.com/mail/vf-%5Babc%5D-xyz"
        sweep.apply_async.assert_not_called()   # not a comment → no sweep

    def test_gmail_confirmation_click_fails_stores_code(self, client):
        body = "verify permission ... https://mail.google.com/mail/vf-x\nConfirmation code: 55667788"
        with patch(self._DB, return_value=7), \
             patch(f"{_MAIN}.requests.get", side_effect=RuntimeError("net")), \
             patch(f"{_MAIN}.log_warning"):
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "from": "forwarding-noreply@google.com",
                "subject": "Gmail Forwarding Confirmation", "text": body})
        assert resp.json()["detail"] == "code_stored"


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


class TestGmailForwardConfirmationStorage:
    _RL = "cqc_lem.utilities.linkedin.rate_limit._redis_client"

    def test_get_returns_stored(self):
        import json
        from unittest.mock import MagicMock
        redis = MagicMock(); redis.get.return_value = json.dumps({"code": "1234", "confirmed": True})
        with patch(self._RL, return_value=redis):
            from cqc_lem.api.main import get_gmail_forward_confirmation
            assert get_gmail_forward_confirmation(7) == {"code": "1234", "confirmed": True}

    def test_get_none_when_absent(self):
        from unittest.mock import MagicMock
        redis = MagicMock(); redis.get.return_value = None
        with patch(self._RL, return_value=redis):
            from cqc_lem.api.main import get_gmail_forward_confirmation
            assert get_gmail_forward_confirmation(7) is None

    def test_get_none_without_redis(self):
        with patch(self._RL, return_value=None):
            from cqc_lem.api.main import get_gmail_forward_confirmation
            assert get_gmail_forward_confirmation(7) is None

    def test_get_none_when_redis_read_raises(self):
        from unittest.mock import MagicMock
        redis = MagicMock()
        redis.get.side_effect = ConnectionError("redis down")
        with patch(self._RL, return_value=redis):
            from cqc_lem.api.main import get_gmail_forward_confirmation
            assert get_gmail_forward_confirmation(7) is None

    def test_confirmation_stores_status_in_redis(self, client):
        from unittest.mock import MagicMock
        redis = MagicMock()
        got = type("R", (), {"status_code": 200})()
        body = "confirm the request: https://mail.google.com/mail/vf-abc\nConfirmation code: 999888777"
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch("cqc_lem.api.main.requests.get", return_value=got), \
             patch(self._RL, return_value=redis):
            resp = client.post("/api/linkedin/comment-notification/inbound", data={
                "to": "reply+tok9@parse.example.com",
                "from": "forwarding-noreply@google.com",
                "subject": "Gmail Forwarding Confirmation", "text": body})
        assert resp.json()["detail"] == "confirmed"
        redis.set.assert_called_once()
        assert redis.set.call_args.args[0] == "linkedin:gmail_forward_confirm:7"

    def test_redis_write_failure_never_breaks_the_webhook(self, client):
        from unittest.mock import MagicMock
        redis = MagicMock()
        redis.set.side_effect = ConnectionError("redis down")
        got = type("R", (), {"status_code": 200})()
        body = ("confirm the request: https://mail.google.com/mail/vf-abc\n"
                "Confirmation code: 999888777")
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch("cqc_lem.api.main.requests.get", return_value=got), \
             patch(self._RL, return_value=redis):
            resp = client.post("/api/linkedin/comment-notification/inbound", data={
                "to": "reply+tok9@parse.example.com",
                "from": "forwarding-noreply@google.com",
                "subject": "Gmail Forwarding Confirmation", "text": body})
        assert resp.status_code == 200
        assert resp.json()["detail"] == "confirmed"


class TestSharedInboundRouting:
    """SendGrid posts ALL parse-host mail to the PIN URL, so that endpoint must also route
    reply+<token> mail (Gmail confirmations + comment notifications)."""
    PIN = "/api/linkedin/verification-pin/inbound"

    def test_pin_url_routes_reply_comment_to_sweep(self, client):
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch(f"{_MAIN}._reply_sweep_debounced", return_value=True), \
             patch(f"{_MAIN}.sweep_reply_comments") as sweep:
            resp = client.post(self.PIN, data={
                "to": "reply+tok9@parse.example.com",
                "subject": "Jane commented on your post"})
        assert resp.json()["detail"] == "accepted"
        sweep.apply_async.assert_called_once()

    def test_pin_url_routes_gmail_confirmation(self, client):
        got = type("R", (), {"status_code": 200})()
        body = "confirm: https://mail.google.com/mail/vf-abc\nConfirmation code: 12345678"
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch(f"{_MAIN}.requests.get", return_value=got):
            resp = client.post(self.PIN, data={
                "to": "reply+tok9@parse.example.com",
                "from": "forwarding-noreply@google.com",
                "subject": "Gmail Forwarding Confirmation", "text": body})
        assert resp.json()["detail"] == "confirmed"

    def test_pin_url_still_handles_pin(self, client):
        with patch(f"{_MAIN}.submit_pin_by_token", return_value=1) as m:
            resp = client.post(self.PIN, data={
                "to": "pin+abc123@parse.example.com", "text": "483920"})
        assert resp.json()["detail"] == "accepted"
        m.assert_called_once_with("abc123", "483920")


class TestGmailConfirmationForwardToUser:
    BASE = "/api/linkedin/comment-notification/inbound"

    def test_forwards_to_user_when_click_fails(self, client):
        body = "verify permission https://mail.google.com/mail/vf-x\nConfirmation code: 55667788"
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch("cqc_lem.api.main.requests.get", side_effect=RuntimeError("net")), \
             patch("cqc_lem.api.main.log_warning"), \
             patch("cqc_lem.utilities.db.get_user_email", return_value="chris@example.com"), \
             patch("cqc_lem.utilities.email.send_reply_forward_confirmation_email", return_value=True) as fwd:
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "from": "forwarding-noreply@google.com",
                "subject": "Gmail Forwarding Confirmation", "text": body})
        assert resp.json()["detail"] == "forwarded"
        fwd.assert_called_once()
        assert fwd.call_args.args[0] == "chris@example.com"
        assert fwd.call_args.args[2] == "55667788"  # code passed through


@pytest.mark.unit
class TestReplyForwardConfirmationEmail:
    def test_sends_with_button_and_code(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as disp:
            from cqc_lem.utilities.email import send_reply_forward_confirmation_email
            ok = send_reply_forward_confirmation_email("u@e.com", "https://mail.google.com/mail/vf-x", "123456789")
        assert ok is True
        html = disp.call_args.args[2]
        assert "https://mail.google.com/mail/vf-x" in html and "123456789" in html

    def test_noop_without_url_or_code(self):
        with patch("cqc_lem.utilities.email._dispatch_email") as disp:
            from cqc_lem.utilities.email import send_reply_forward_confirmation_email
            assert send_reply_forward_confirmation_email("u@e.com", None, None) is False
        disp.assert_not_called()
