"""Unit tests for general FastAPI endpoints in cqc_lem.api.main."""

import time
from datetime import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_MAIN = "cqc_lem.api.main"
_AUTH = "cqc_lem.api.routers.auth"
_USER = "cqc_lem.api.routers.user"

from tests.unit.api.conftest import SESSION_TOKEN, SESSION_USER_ID  # noqa: E402


@pytest.fixture(autouse=True)
def _auth_hardening_side_effects():
    """Issue #745 (2b): every login now stamps `email_verified_at`, writes an `auth_audit_log` row
    and reads the PIN lockout, and /auth/session resolves the account's public_uid. Those are DB
    calls these tests never mocked — pin them so each test still exercises the flow it was written
    for. The hardening itself has its own suite (tests/unit/api/test_auth_hardening.py).
    """
    with patch(f"{_MAIN}.record_auth_event", return_value=True), \
         patch(f"{_USER}.record_auth_event", return_value=True), \
         patch(f"{_AUTH}.record_auth_event", return_value=True), \
         patch(f"{_AUTH}.mark_email_verified", return_value=True), \
         patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
         patch(f"{_USER}.get_pin_lockout", return_value=None), \
         patch(f"{_AUTH}.get_user_public_uid", return_value="pub-uid-1"), \
         patch(f"{_USER}.get_user_public_uid", return_value="pub-uid-1"):
        yield


@pytest.fixture(scope="module")
def client():
    """TestClient with all heavy module-level imports pre-mocked."""
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.engagement.invites.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.engagement.posting.automate_reply_commenting"),
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

    def test_no_session_returns_401(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=None):
            resp = client.get(self.BASE)
        assert resp.status_code == 401

    _ZEROS = {"scheduled_this_week": 0, "pending_review": 0, "posted_total": 0}

    def test_known_user_with_no_posts_returns_zeros(self, client, signed_in):
        with patch(f"{_MAIN}.get_dashboard_counts", return_value=dict(self._ZEROS)):
            resp = client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"] == self._ZEROS

    def test_start_of_month_does_not_crash(self, client, signed_in):
        # Regression: week_start was computed with replace(day=day-weekday()), which
        # goes out of range in the first days of a month (Wed the 1st → day=-1 → 500).
        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 7, 1, tzinfo=tz)  # Wednesday the 1st

        with patch(f"{_MAIN}.get_dashboard_counts", return_value=dict(self._ZEROS)), \
             patch(f"{_MAIN}.datetime", _FixedDatetime):
            resp = client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200

    def test_returns_counts_from_db_helper_over_all_posts(self, client, signed_in):
        # The endpoint now delegates to the SQL-aggregate helper (counts over ALL posts, not the
        # 10-oldest get_posts() slice that made these stale). It passes a tz-aware Monday week_start.
        counts = {"scheduled_this_week": 4, "pending_review": 1, "posted_total": 37}
        with patch(f"{_MAIN}.get_dashboard_counts", return_value=counts) as gdc:
            resp = client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"] == counts
        user_id_arg, week_start_arg = gdc.call_args[0][0], gdc.call_args[0][1]
        assert user_id_arg == SESSION_USER_ID
        assert week_start_arg.weekday() == 0 and week_start_arg.hour == 0  # Monday 00:00


# ---------------------------------------------------------------------------
# GET /api/activity/
# ---------------------------------------------------------------------------

class TestGetActivity:
    BASE = "/api/activity/"

    def test_no_session_returns_401(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=None):
            resp = client.get(self.BASE)
        assert resp.status_code == 401

    def test_valid_user_returns_200_with_list(self, client, signed_in):
        log_row = {
            "id": 1,
            "action_type": "POST",
            "result": "success",
            "post_id": 10,
            "post_url": "https://linkedin.com/p/123",
            "message": "Posted OK",
            "created_at": datetime(2024, 1, 15, 12, 0, 0),
        }
        with patch(f"{_MAIN}.get_recent_logs", return_value=[log_row]):
            resp = client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert len(detail) == 1
        assert detail[0]["id"] == 1
        assert detail[0]["action_type"] == "POST"

    def test_empty_log_list_returns_empty_array(self, client, signed_in):
        with patch(f"{_MAIN}.get_recent_logs", return_value=[]):
            resp = client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"] == []

    def test_created_at_serialized_as_explicit_utc(self, client, signed_in):
        log_row = {
            "id": 1, "action_type": "post", "result": "success", "post_id": 10,
            "post_url": "https://linkedin.com/p/123", "message": "ok",
            "created_at": datetime(2024, 1, 15, 12, 0, 0),  # naive == UTC
        }
        with patch(f"{_MAIN}.get_recent_logs", return_value=[log_row]):
            resp = client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.json()["detail"][0]["created_at"] == "2024-01-15T12:00:00Z"

    def test_synthetic_feedpost_url_blanked(self, client, signed_in):
        rows = [
            {"id": 1, "action_type": "comment", "result": "success", "post_id": None,
             "post_url": "feedpost://abc123", "message": "nice", "created_at": datetime(2024, 1, 1)},
            {"id": 2, "action_type": "post", "result": "success", "post_id": 3,
             "post_url": "https://www.linkedin.com/feed/update/x", "message": "up",
             "created_at": datetime(2024, 1, 2)},
        ]
        with patch(f"{_MAIN}.get_recent_logs", return_value=rows):
            detail = client.get(self.BASE, params={"session_token": SESSION_TOKEN}).json()["detail"]
        assert detail[0]["post_url"] is None                                      # feedpost:// hidden
        assert detail[1]["post_url"] == "https://www.linkedin.com/feed/update/x"  # real permalink kept


# ---------------------------------------------------------------------------
# PUT /api/user/
# ---------------------------------------------------------------------------

class TestUpdateUser:
    BASE = "/api/user/"

    def test_no_session_returns_401(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=None):
            resp = client.put(self.BASE, json={"blog_url": "https://blog.example.com"})
        assert resp.status_code == 401

    def test_no_update_fields_returns_unchanged(self, client, signed_in):
        with patch(f"{_USER}.update_user") as upd:
            resp = client.put(self.BASE, json={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert "unchanged" in resp.json()["detail"]
        upd.assert_not_called()

    def test_valid_update_returns_200(self, client, signed_in):
        with patch(f"{_USER}.update_user", return_value=True) as upd:
            resp = client.put(self.BASE, json={"session_token": SESSION_TOKEN,
                                               "blog_url": "https://blog.example.com"})
        assert resp.status_code == 200
        assert "updated" in resp.json()["detail"]
        assert upd.call_args[0][0] == SESSION_USER_ID

    def test_update_user_returns_false_gives_404(self, client, signed_in):
        with patch(f"{_USER}.update_user", return_value=False):
            resp = client.put(self.BASE, json={"session_token": SESSION_TOKEN,
                                               "blog_url": "https://blog.example.com"})
        assert resp.status_code == 404

    def test_new_email_is_no_longer_an_account_takeover_lever(self, client, signed_in):
        """`new_email` no longer moves the address — and says so rather than answering 200."""
        with patch(f"{_USER}.update_user", return_value=True) as upd:
            resp = client.put(self.BASE, json={"session_token": SESSION_TOKEN,
                                               "new_email": "attacker@evil.example",
                                               "blog_url": "https://blog.example.com"})
        assert resp.status_code == 400
        upd.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/user_id/
# ---------------------------------------------------------------------------

class TestGetUserIdEndpoint:
    BASE = "/api/user_id/"

    def test_no_session_returns_401(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=None):
            resp = client.get(self.BASE)
        assert resp.status_code == 401

    def test_returns_the_callers_own_user_id(self, client, signed_in):
        resp = client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"] == SESSION_USER_ID

    def test_another_address_is_not_an_id_oracle(self, client, signed_in):
        resp = client.get(self.BASE, params={"session_token": SESSION_TOKEN,
                                             "email": "victim@example.com"})
        assert resp.status_code == 403


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
             patch(f"{_AUTH}.get_user_email", return_value="me@example.com"), \
             patch(f"{_AUTH}.get_user_analytics_profile", return_value={}), \
             patch(f"{_AUTH}.is_user_admin", return_value=False):
            resp = client.get(self.BASE, params={"session_token": "valid-token-abc"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["user_id"] == 7
        assert detail["email"] == "me@example.com"
        assert detail["is_admin"] is False

    def test_returns_person_facts_for_posthog_identify(self, client):
        profile = {
            "subscription_tier": "premium",
            "subscription_status": "active",
            "timezone": "America/Chicago",
            "created_at": datetime(2026, 1, 2, 3, 4, 5),
            "onboarding_completed_at": datetime(2026, 2, 3, 4, 5, 6),
            "posts_approved": 12,
        }
        with patch(f"{_MAIN}.get_session_user_id", return_value=7), \
             patch(f"{_AUTH}.get_user_email", return_value="me@example.com"), \
             patch(f"{_AUTH}.get_user_analytics_profile", return_value=profile), \
             patch(f"{_AUTH}.is_user_admin", return_value=True):
            resp = client.get(self.BASE, params={"session_token": "valid-token-abc"})
        detail = resp.json()["detail"]
        assert detail["plan"] == "premium"
        assert detail["plan_status"] == "active"
        assert detail["timezone"] == "America/Chicago"
        assert detail["created_at"] == "2026-01-02T03:04:05Z"
        # What PostHog Surveys target on (issue #653).
        assert detail["onboarding_completed_at"] == "2026-02-03T04:05:06Z"
        assert detail["posts_approved"] == 12
        assert detail["is_admin"] is True

    def test_missing_profile_row_leaves_person_facts_null(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=7), \
             patch(f"{_AUTH}.get_user_email", return_value="me@example.com"), \
             patch(f"{_AUTH}.get_user_analytics_profile", return_value={}), \
             patch(f"{_AUTH}.is_user_admin", return_value=False):
            resp = client.get(self.BASE, params={"session_token": "valid-token-abc"})
        detail = resp.json()["detail"]
        assert detail["plan"] is None
        assert detail["plan_status"] is None
        assert detail["timezone"] is None
        assert detail["created_at"] is None
        assert detail["onboarding_completed_at"] is None
        # A user who has approved nothing is 0, never null — the survey rule is a `>=` comparison.
        assert detail["posts_approved"] == 0
        assert detail["is_admin"] is False


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
             patch(f"{_USER}.get_user_subscription_info", return_value=sub), \
             patch(f"{_USER}.get_user_preferences", return_value=prefs), \
             patch(f"{_USER}.get_user_blog_url", return_value="https://blog.example.com"), \
             patch(f"{_USER}.get_user_sitemap_url", return_value="https://blog.example.com/sitemap.xml"), \
             patch(f"{_USER}.get_company_linked_in_url_for_user", return_value=None):
            resp = client.get(self.BASE, params={"session_token": "valid-tok"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["subscription"]["status"] == "active"
        assert detail["subscription"]["tier"] == "starter"
        assert detail["preferences"]["last_login_inactivate_delay"] == 90
        # Buffer knobs fall back to the defaults for a user who never set them (issue #544)
        from cqc_lem.utilities.db import DEFAULT_CONTENT_BUFFER_DAYS, DEFAULT_CONTENT_BUFFER_MAX_POSTS
        assert detail["preferences"]["content_buffer_days"] == DEFAULT_CONTENT_BUFFER_DAYS
        assert detail["preferences"]["content_buffer_max_posts"] == DEFAULT_CONTENT_BUFFER_MAX_POSTS
        assert detail["blog_url"] == "https://blog.example.com"
        assert detail["sitemap_url"] == "https://blog.example.com/sitemap.xml"

    def test_none_subscription_returns_null_subscription(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=5), \
             patch(f"{_USER}.get_user_subscription_info", return_value=None), \
             patch(f"{_USER}.get_user_preferences", return_value=None), \
             patch(f"{_USER}.get_user_blog_url", return_value=None), \
             patch(f"{_USER}.get_user_sitemap_url", return_value=None), \
             patch(f"{_USER}.get_company_linked_in_url_for_user", return_value=None):
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
             patch(f"{_USER}.get_linkedin_profile_url_by_user_id",
                   return_value="https://www.linkedin.com/in/christopherqueen/"):
            resp = client.get(self.BASE, params={"session_token": "valid-tok"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["linkedin_profile_url"] == \
            "https://www.linkedin.com/in/christopherqueen/"

    def test_missing_profile_returns_null(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=5), \
             patch(f"{_USER}.get_linkedin_profile_url_by_user_id", return_value=None):
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
        # The inbound webhook is the single-shot trigger, so it must dispatch slot 0 — the golden-hour
        # amplifier owns every other slot (cqc_lem.app.queue_once keys the lock on it, #989).
        assert sweep.apply_async.call_args.kwargs["kwargs"] == {"user_id": 7, "sweep_slot": 0}

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

    _TRACK = "cqc_lem.utilities.observability.track_inbound_email"

    def test_accepted_comment_logs_verdict(self, client):
        with patch(self._DB, return_value=7), \
             patch(f"{_MAIN}._reply_sweep_debounced", return_value=True), \
             patch(f"{_MAIN}.sweep_reply_comments"), \
             patch(self._TRACK) as track:
            client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "subject": "Chris, Jane commented on your post"})
        track.assert_called_once_with("comment_accepted", user_id=7)

    def test_unknown_token_logs_verdict(self, client):
        with patch(self._DB, return_value=None), \
             patch(f"{_MAIN}.sweep_reply_comments"), \
             patch(self._TRACK) as track:
            client.post(self.BASE, data={
                "to": "reply+stale@parse.example.com",
                "subject": "someone commented on your post"})
        track.assert_called_once_with("unknown_reply_token", user_id=None)

    def test_linkedin_reaction_logs_verdict(self, client):
        with patch(self._DB, return_value=7), \
             patch(f"{_MAIN}.sweep_reply_comments"), \
             patch(self._TRACK) as track:
            client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "from": "notifications-noreply@linkedin.com",
                "subject": "Jane liked your post", "text": ""})
        track.assert_called_once_with("linkedin_not_comment", user_id=7)

    def test_unrelated_mail_logs_verdict(self, client):
        with patch(self._DB, return_value=7), \
             patch(f"{_MAIN}.sweep_reply_comments"), \
             patch(self._TRACK) as track:
            client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "from": "spam@example.com", "subject": "hello", "text": "buy now"})
        track.assert_called_once_with("unrelated", user_id=7)

    def test_pin_endpoint_spam_logs_verdict(self, client):
        with patch(self._TRACK) as track:
            resp = client.post("/api/linkedin/verification-pin/inbound", data={
                "to": "random@parse.example.com", "subject": "spam", "text": "spam"})
        assert resp.json()["detail"] == "ignored"
        track.assert_called_once_with("no_pin_token", user_id=None)

    def test_tracking_failure_never_breaks_webhook(self, client):
        with patch(self._DB, return_value=7), \
             patch(f"{_MAIN}._reply_sweep_debounced", return_value=True), \
             patch(f"{_MAIN}.sweep_reply_comments") as sweep, \
             patch(self._TRACK, side_effect=RuntimeError("posthog down")):
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "subject": "Jane commented on your post"})
        assert resp.json()["detail"] == "accepted"
        sweep.apply_async.assert_called_once()

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


class TestForwardingConfirmedByDelivery:
    """Issue #813: our server-side click of Gmail's verify link often fails, so a user who finished
    the confirmation by hand was stuck at confirmed=False forever. Mail actually ARRIVING on their
    private reply+<token> address is the real end-to-end proof, so record it.
    """
    BASE = "/api/linkedin/comment-notification/inbound"
    _RL = "cqc_lem.utilities.linkedin.rate_limit._redis_client"

    @staticmethod
    def _redis(existing=None):
        import json as _json
        from unittest.mock import MagicMock
        r = MagicMock()
        r.get.return_value = _json.dumps(existing) if existing is not None else None
        return r

    def _post(self, client, redis, **data):
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch(f"{_MAIN}._reply_sweep_debounced", return_value=True), \
             patch(f"{_MAIN}.sweep_reply_comments"), \
             patch(self._RL, return_value=redis):
            return client.post(self.BASE, data={"to": "reply+tok9@parse.example.com", **data})

    def test_forwarded_comment_email_confirms_forwarding(self, client):
        import json
        redis = self._redis()
        resp = self._post(client, redis, **{
            "from": "LinkedIn <messages-noreply@linkedin.com>",
            "subject": "Jane commented on your post"})
        assert resp.json()["detail"] == "accepted"
        stored = json.loads(redis.set.call_args.args[1])
        assert stored == {"confirmed": True, "source": "forwarded_email"}

    def test_confirmed_record_never_expires(self, client):
        # The Gmail filter is set up once and forwards indefinitely — a TTL would re-raise the
        # "not confirmed" warning a week later on a setup that is working fine.
        redis = self._redis()
        self._post(client, redis, **{"from": "messages-noreply@linkedin.com",
                                     "subject": "Jane commented on your post"})
        assert "ex" not in redis.set.call_args.kwargs

    def test_forwarded_reaction_email_confirms_but_starts_no_sweep(self, client):
        redis = self._redis()
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch(f"{_MAIN}.sweep_reply_comments") as sweep, \
             patch(self._RL, return_value=redis):
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "from": "messages-noreply@linkedin.com",
                "subject": "Jane liked your post"})
        assert resp.json()["detail"] == "ignored"
        sweep.apply_async.assert_not_called()
        redis.set.assert_called_once()

    def test_unrelated_mail_is_not_evidence(self, client):
        redis = self._redis()
        resp = self._post(client, redis, **{"from": "spam@example.com",
                                            "subject": "Your invoice is ready"})
        assert resp.json()["detail"] == "ignored"
        redis.set.assert_not_called()

    def test_already_confirmed_is_not_rewritten(self, client):
        redis = self._redis({"confirmed": True, "source": "auto_click"})
        self._post(client, redis, **{"from": "messages-noreply@linkedin.com",
                                     "subject": "Jane commented on your post"})
        redis.set.assert_not_called()

    def test_pending_confirmation_never_downgrades_a_confirmed_one(self, client):
        # A second Gmail confirmation email (re-added address, new code) whose auto-click fails must
        # not undo forwarding we have already seen working.
        redis = self._redis({"confirmed": True, "source": "forwarded_email"})
        body = "verify permission https://mail.google.com/mail/vf-x\nConfirmation code: 55667788"
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch(f"{_MAIN}.requests.get", side_effect=RuntimeError("net")), \
             patch(f"{_MAIN}.log_warning"), \
             patch("cqc_lem.utilities.db.get_user_email", return_value=None), \
             patch(self._RL, return_value=redis):
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "from": "forwarding-noreply@google.com",
                "subject": "Gmail Forwarding Confirmation", "text": body})
        assert resp.status_code == 200
        redis.set.assert_not_called()

    def test_pending_confirmation_outlives_the_code_it_carries(self, client):
        # A TTL on the PENDING record is the same bug one step later: the user who added the address
        # and is waiting on their first forwarded comment email would drop back to "Forwarding not
        # confirmed yet — finish the 3-step setup" after a quiet week. Only the CODE goes stale.
        import json
        redis = self._redis()
        body = "verify permission https://mail.google.com/mail/vf-x\nConfirmation code: 55667788"
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch(f"{_MAIN}.requests.get", side_effect=RuntimeError("net")), \
             patch(f"{_MAIN}.log_warning"), \
             patch("cqc_lem.utilities.db.get_user_email", return_value=None), \
             patch(self._RL, return_value=redis):
            client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "from": "forwarding-noreply@google.com",
                "subject": "Gmail Forwarding Confirmation", "text": body})
        assert "ex" not in redis.set.call_args.kwargs
        stored = json.loads(redis.set.call_args.args[1])
        assert stored["confirmed"] is False
        assert stored["code_expires_at"] > time.time()

    def test_stale_code_is_dropped_but_the_record_survives(self):
        from cqc_lem.api.main import get_gmail_forward_confirmation
        redis = self._redis({"code": "55667788", "confirmed": False, "url_found": True,
                             "code_expires_at": int(time.time()) - 1})
        with patch(self._RL, return_value=redis):
            record = get_gmail_forward_confirmation(7)
        # Still "pending" for the UI (the record exists), but no code Gmail would reject.
        assert record is not None and "code" not in record

    def test_fresh_code_is_still_offered(self):
        from cqc_lem.api.main import get_gmail_forward_confirmation
        redis = self._redis({"code": "55667788", "confirmed": False,
                             "code_expires_at": int(time.time()) + 60})
        with patch(self._RL, return_value=redis):
            assert get_gmail_forward_confirmation(7)["code"] == "55667788"

    def test_redis_outage_on_the_delivery_path_does_not_warn(self, client):
        # Runs per inbound email — a repeated log_warning re-emits at ERROR and files a defect.
        redis = self._redis()
        redis.set.side_effect = ConnectionError("redis down")
        with patch("cqc_lem.utilities.db.get_user_id_by_reply_token", return_value=7), \
             patch(f"{_MAIN}._reply_sweep_debounced", return_value=True), \
             patch(f"{_MAIN}.sweep_reply_comments"), \
             patch(f"{_MAIN}.log_warning") as warn, \
             patch(self._RL, return_value=redis):
            resp = client.post(self.BASE, data={
                "to": "reply+tok9@parse.example.com",
                "from": "messages-noreply@linkedin.com",
                "subject": "Jane commented on your post"})
        assert resp.status_code == 200
        warn.assert_not_called()


class TestSharedInboundRouting:
    """SendGrid posts ALL parse-host mail to the PIN URL, so that endpoint must also route
    reply+<token> mail (Gmail confirmations + comment notifications).
    """
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
