"""Coverage tests for previously untested api/main.py endpoints: automation triggers,
token status, LinkedIn OAuth, user settings/groups/lead-magnet, avatar/video credits,
admin tools, and Stripe credit webhooks."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"


@pytest.fixture(scope="module")
def client():
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


_TOK = "session-token"
_UID = 7


class TestAutomationTriggerEndpoints:
    def test_automate_reply_commenting_schedules_task(self, client):
        with patch(f"{_M}.get_post_user_id", return_value=_UID), \
             patch(f"{_M}.automate_reply_commenting") as task:
            resp = client.post("/api/automate_reply_commenting?post_id=9")
        assert resp.status_code == 200
        kwargs = task.apply_async.call_args[1]["kwargs"]
        assert kwargs["user_id"] == _UID and kwargs["post_id"] == 9

    def test_automate_reply_commenting_403_when_no_user(self, client):
        with patch(f"{_M}.get_post_user_id", return_value=None):
            resp = client.post("/api/automate_reply_commenting?post_id=9")
        assert resp.status_code == 403

    def test_automate_reply_commenting_404_on_schedule_failure(self, client):
        with patch(f"{_M}.get_post_user_id", return_value=_UID), \
             patch(f"{_M}.automate_reply_commenting") as task:
            task.apply_async.side_effect = RuntimeError("broker down")
            resp = client.post("/api/automate_reply_commenting?post_id=9")
        assert resp.status_code == 404

    def test_schedule_post_success(self, client):
        with patch(f"{_M}.get_user_id", return_value=_UID), \
             patch(f"{_M}.insert_post", return_value=True) as ins:
            resp = client.post("/api/schedule_post/", json={
                "content": "hello", "scheduled_datetime": "2026-07-10T15:00:00",
                "email": "a@x.com", "post_type": "text"})
        assert resp.status_code == 200
        assert ins.call_args[1].get("video_quality") == "standard"

    def test_schedule_post_403_unknown_user(self, client):
        with patch(f"{_M}.get_user_id", return_value=None):
            resp = client.post("/api/schedule_post/", json={
                "content": "hello", "scheduled_datetime": "2026-07-10T15:00:00",
                "email": "a@x.com"})
        assert resp.status_code == 403

    def test_schedule_post_404_on_insert_failure(self, client):
        with patch(f"{_M}.get_user_id", return_value=_UID), \
             patch(f"{_M}.insert_post", return_value=False):
            resp = client.post("/api/schedule_post/", json={
                "content": "hello", "scheduled_datetime": "2026-07-10T15:00:00",
                "email": "a@x.com"})
        assert resp.status_code == 404

    def test_schedule_post_forwards_approved_status(self, client):
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_M}.get_user_id", return_value=_UID), \
             patch(f"{_M}.insert_post", return_value=True) as ins:
            resp = client.post("/api/schedule_post/", json={
                "content": "hello", "scheduled_datetime": "2026-07-10T15:00:00",
                "email": "a@x.com", "post_type": "text", "status": "approved"})
        assert resp.status_code == 200
        assert ins.call_args[1].get("status") == PostStatus.APPROVED

    def test_schedule_post_defaults_to_pending_status(self, client):
        from cqc_lem.utilities.db import PostStatus
        with patch(f"{_M}.get_user_id", return_value=_UID), \
             patch(f"{_M}.insert_post", return_value=True) as ins:
            resp = client.post("/api/schedule_post/", json={
                "content": "hello", "scheduled_datetime": "2026-07-10T15:00:00",
                "email": "a@x.com", "post_type": "text"})
        assert resp.status_code == 200
        assert ins.call_args[1].get("status") == PostStatus.PENDING

    def test_create_weekly_content_chains_plan_then_create(self, client):
        chain_obj = MagicMock()
        with patch(f"{_M}.celery_chain", return_value=chain_obj) as chain, \
             patch(f"{_M}.plan_content_for_user") as plan, \
             patch(f"{_M}.auto_create_weekly_content") as weekly:
            resp = client.post("/api/create_weekly_content/?user_id=7")
        assert resp.status_code == 200
        plan.si.assert_called_once_with(user_id=7)
        weekly.si.assert_called_once_with(user_id=7)
        chain_obj.apply_async.assert_called_once()

    def test_create_weekly_content_400_without_user(self, client):
        resp = client.post("/api/create_weekly_content/?user_id=0")
        assert resp.status_code == 400

    def test_invite_to_company_page(self, client):
        with patch(f"{_M}.automate_invites_to_company_page_for_user") as task:
            resp = client.post("/api/invite_to_li_company_page/?user_id=7")
        assert resp.status_code == 200
        assert task.apply_async.call_args[1]["kwargs"] == {"user_id": 7}

    def test_invite_400_without_user(self, client):
        resp = client.post("/api/invite_to_li_company_page/?user_id=0")
        assert resp.status_code == 400

    def test_aws_test_get_my_profile(self, client):
        with patch(f"{_M}.test_get_my_profile") as task:
            resp = client.post("/api/aws_test_get_my_profile/?user_id=7")
        assert resp.status_code == 200
        assert task.apply_async.call_args[1]["kwargs"] == {"user_id": 7}

    def test_aws_test_400_without_user(self, client):
        resp = client.post("/api/aws_test_get_my_profile/?user_id=0")
        assert resp.status_code == 400


class TestAuthLogoutAndTokenStatus:
    def test_logout_deletes_session(self, client):
        with patch(f"{_M}.delete_session") as ds:
            resp = client.post("/api/auth/logout", json={"session_token": _TOK})
        assert resp.status_code == 200
        ds.assert_called_once_with(_TOK)

    def test_token_status_401(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.get(f"/api/user/token_status?session_token=bad")
        assert resp.status_code == 401

    def test_token_status_no_token_is_expired(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_token_info", return_value=None):
            resp = client.get(f"/api/user/token_status?session_token={_TOK}")
        detail = resp.json()["detail"]
        assert detail["is_expired"] is True and detail["token_expiry_date"] is None

    def test_token_status_healthy_token(self, client):
        from datetime import datetime
        expiry = datetime(2026, 12, 1, 0, 0)
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_token_info",
                   return_value={"access_token": "at", "refresh_token": None}), \
             patch(f"{_M}.is_token_expiring_soon", return_value=False), \
             patch(f"{_M}.is_token_expired", return_value=False), \
             patch(f"{_M}.get_token_expiry", return_value=expiry):
            resp = client.get(f"/api/user/token_status?session_token={_TOK}")
        detail = resp.json()["detail"]
        assert detail["is_expired"] is False
        assert detail["refresh_attempted"] is False
        assert detail["token_expiry_date"] == expiry.isoformat()

    def test_token_status_expiring_triggers_refresh(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_token_info",
                   side_effect=[{"access_token": "old", "refresh_token": "rt"},
                                {"access_token": "new", "refresh_token": "rt"}]), \
             patch(f"{_M}.is_token_expiring_soon", side_effect=[True, False]), \
             patch(f"{_M}.is_token_expired", side_effect=[True, False]), \
             patch(f"{_M}.attempt_token_refresh", return_value=(True, "ok")) as refresh, \
             patch(f"{_M}.get_token_expiry", return_value=None):
            resp = client.get(f"/api/user/token_status?session_token={_TOK}")
        detail = resp.json()["detail"]
        refresh.assert_called_once_with(_UID)
        assert detail["refresh_attempted"] is True
        assert detail["refresh_succeeded"] is True
        assert detail["is_expired"] is False


def _token_response(access_token="at", refresh_token="rt"):
    return SimpleNamespace(access_token=access_token, expires_in=3600,
                           refresh_token=refresh_token, refresh_token_expires_in=86400)


class TestLinkedInOAuth:
    @pytest.fixture(autouse=True)
    def _redirect_url(self):
        # CI has no .env: LI_REDIRECT_URL is empty there and _account_redirect's urlparse would
        # 500. Pin it so these tests are environment-independent.
        with patch(f"{_M}.LI_REDIRECT_URL", "https://app.example.com/auth/linkedin/callback"):
            yield

    def test_auth_init_redirects_to_linkedin(self, client):
        auth_client = MagicMock()
        auth_client.generate_member_auth_url.return_value = "https://linkedin.com/oauth/x"
        with patch(f"{_M}.AuthClient", return_value=auth_client):
            resp = client.get("/api/auth/linkedin/?session_token=tok",
                              follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "https://linkedin.com/oauth/x"
        # session token embedded in state
        state = auth_client.generate_member_auth_url.call_args[1]["state"]
        assert state.endswith(":tok")

    def test_callback_invalid_state_salt_400(self, client):
        with patch(f"{_M}.LI_STATE_SALT", "goodsalt"):
            resp = client.get("/auth/linkedin/callback?code=c&state=badsalt:tok",
                              follow_redirects=False)
        assert resp.status_code == 400

    def test_callback_token_exchange_failure_redirects_with_error(self, client):
        auth_client = MagicMock()
        auth_client.exchange_auth_code_for_access_token.side_effect = RuntimeError("nope")
        with patch(f"{_M}.AuthClient", return_value=auth_client):
            resp = client.get("/auth/linkedin/callback?code=c", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert "li_error=token_exchange_failed" in resp.headers["location"]

    def test_callback_no_access_token_redirects_with_error(self, client):
        auth_client = MagicMock()
        auth_client.exchange_auth_code_for_access_token.return_value = _token_response(
            access_token=None)
        with patch(f"{_M}.AuthClient", return_value=auth_client):
            resp = client.get("/auth/linkedin/callback?code=c", follow_redirects=False)
        assert "li_error=no_access_token" in resp.headers["location"]

    def test_callback_userinfo_failure_redirects_with_error(self, client):
        auth_client = MagicMock()
        auth_client.exchange_auth_code_for_access_token.return_value = _token_response()
        restli = MagicMock()
        restli.get.side_effect = RuntimeError("api down")
        with patch(f"{_M}.AuthClient", return_value=auth_client), \
             patch(f"{_M}.RestliClient", return_value=restli):
            resp = client.get("/auth/linkedin/callback?code=c", follow_redirects=False)
        assert "li_error=userinfo_failed" in resp.headers["location"]

    def test_callback_with_session_updates_logged_in_user(self, client):
        auth_client = MagicMock()
        auth_client.exchange_auth_code_for_access_token.return_value = _token_response()
        restli = MagicMock()
        restli.get.return_value = SimpleNamespace(
            entity={"email": "li@x.com", "sub": "SUB1"})
        with patch(f"{_M}.AuthClient", return_value=auth_client), \
             patch(f"{_M}.RestliClient", return_value=restli), \
             patch(f"{_M}.LI_STATE_SALT", "salt"), \
             patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.update_user_linkedin_token") as upd, \
             patch(f"{_M}.add_user_with_access_token") as add:
            resp = client.get("/auth/linkedin/callback?code=c&state=salt:tok",
                              follow_redirects=False)
        assert "li_connected=1" in resp.headers["location"]
        upd.assert_called_once()
        assert upd.call_args[0][0] == _UID and upd.call_args[0][1] == "SUB1"
        assert upd.call_args[1]["linkedin_email"] == "li@x.com"
        add.assert_not_called()

    def test_callback_without_session_upserts_by_email(self, client):
        auth_client = MagicMock()
        auth_client.exchange_auth_code_for_access_token.return_value = _token_response()
        restli = MagicMock()
        restli.get.return_value = SimpleNamespace(
            entity={"email": "li@x.com", "sub": "SUB1"})
        with patch(f"{_M}.AuthClient", return_value=auth_client), \
             patch(f"{_M}.RestliClient", return_value=restli), \
             patch(f"{_M}.add_user_with_access_token") as add:
            resp = client.get("/auth/linkedin/callback?code=c", follow_redirects=False)
        assert "li_connected=1" in resp.headers["location"]
        add.assert_called_once()
        assert add.call_args[0][0] == "li@x.com"

    def test_callback_without_session_or_email_errors(self, client):
        auth_client = MagicMock()
        auth_client.exchange_auth_code_for_access_token.return_value = _token_response()
        restli = MagicMock()
        restli.get.return_value = SimpleNamespace(entity={})
        with patch(f"{_M}.AuthClient", return_value=auth_client), \
             patch(f"{_M}.RestliClient", return_value=restli):
            resp = client.get("/auth/linkedin/callback?code=c", follow_redirects=False)
        assert "li_error=no_email" in resp.headers["location"]


class TestUserSettingsAndGroups:
    def test_update_user_settings(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.update_user_preferences", return_value=True) as upd:
            resp = client.put("/api/user/settings", json={
                "session_token": _TOK, "last_login_inactivate_delay": 30,
                "auto_schedule_posts": False})
        assert resp.status_code == 200
        assert upd.call_args[1]["inactivate_delay"] == 30
        assert upd.call_args[1]["auto_schedule_posts"] is False

    def test_update_user_settings_500(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.update_user_preferences", return_value=False):
            resp = client.put("/api/user/settings", json={"session_token": _TOK})
        assert resp.status_code == 500

    def test_get_groups(self, client):
        groups = [{"group_id": "g1", "group_name": "AI", "enabled": True}]
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_groups", return_value=groups):
            resp = client.get(f"/api/user/groups?session_token={_TOK}")
        assert resp.status_code == 200 and resp.json()["detail"] == groups

    def test_put_groups(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.set_groups_enabled", return_value=True) as setg:
            resp = client.put("/api/user/groups", json={
                "session_token": _TOK, "groups": {"g1": False}})
        assert resp.status_code == 200
        assert setg.call_args[0] == (_UID, {"g1": False})

    def test_put_groups_500(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.set_groups_enabled", return_value=False):
            resp = client.put("/api/user/groups", json={"session_token": _TOK})
        assert resp.status_code == 500

    def test_post_stats_recommendations(self, client):
        rows = [("2026-07-01T15:00:00", 10, 2, 1)]
        recs = [{"weekday_num": 2, "hour": 15}]
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_post_engagement_rows", return_value=rows), \
             patch("cqc_lem.utilities.post_stats.recommend_post_times",
                   return_value=recs):
            resp = client.get(f"/api/user/post-stats?session_token={_TOK}")
        detail = resp.json()["detail"]
        assert detail["recommendations"] == recs and detail["sample_size"] == 1


class TestEngagementAnalytics:
    def test_returns_per_post_table_and_trend(self, client):
        from datetime import datetime
        rows = [
            {"post_id": 9, "scheduled_time": datetime(2026, 7, 20, 14, 0), "reactions": 20,
             "comments": 10, "reposts": 5, "saves": 3, "impressions": 1000, "archetype": "how_to",
             "hook_style": "question", "format": "carousel", "topic": "AI", "buyer_stage": "aware"},
        ]
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_post_performance_rows", return_value=rows) as fetch:
            resp = client.get(f"/api/user/engagement-analytics?session_token={_TOK}&days=30")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["sample_size"] == 1 and detail["days"] == 30
        assert fetch.call_args[1]["days"] == 30
        post = detail["per_post"][0]
        assert post["post_id"] == 9 and post["engagement"] == 50
        assert post["engagement_rate"] == pytest.approx(0.05)
        assert detail["trend"][0]["date"] == "2026-07-20"

    def test_days_clamped_to_valid_window(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_post_performance_rows", return_value=[]) as fetch:
            resp = client.get(f"/api/user/engagement-analytics?session_token={_TOK}&days=9999")
        assert resp.status_code == 200
        assert fetch.call_args[1]["days"] == 365          # clamped upper bound

    def test_401_without_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.get(f"/api/user/engagement-analytics?session_token=bad")
        assert resp.status_code == 401


class TestLeadMagnetAndPassword:
    def test_get_lead_magnet(self, client):
        settings = {"enabled": True, "keyword": "GUIDE", "message": "here you go"}
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_lead_magnet_settings", return_value=settings):
            resp = client.get(f"/api/user/lead-magnet?session_token={_TOK}")
        assert resp.json()["detail"] == settings

    def test_put_lead_magnet_excludes_session_token(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.update_lead_magnet_settings", return_value=True) as upd:
            resp = client.put("/api/user/lead-magnet", json={
                "session_token": _TOK, "enabled": True, "keyword": "GUIDE"})
        assert resp.status_code == 200
        assert "session_token" not in upd.call_args[0][1]
        assert upd.call_args[0][1]["keyword"] == "GUIDE"

    @pytest.mark.parametrize("bad_kw", ["YES", "agree", "BELOW", "AMEN", "ME"])
    def test_put_lead_magnet_rejects_bait_colliding_keyword(self, client, bad_kw):
        # Keywords that trip the engagement-bait filter would be stripped from generated posts —
        # reject at config time with a clear message.
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.update_lead_magnet_settings", return_value=True) as upd:
            resp = client.put("/api/user/lead-magnet", json={
                "session_token": _TOK, "enabled": True, "keyword": bad_kw})
        assert resp.status_code == 422
        assert "engagement-bait" in resp.json()["detail"]
        upd.assert_not_called()

    def test_put_lead_magnet_500(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.update_lead_magnet_settings", return_value=False):
            resp = client.put("/api/user/lead-magnet", json={
                "session_token": _TOK, "enabled": False})
        assert resp.status_code == 500

    def test_linkedin_password_saved(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.update_user_linkedin_password", return_value=True) as upd:
            resp = client.put("/api/user/linkedin-password", json={
                "session_token": _TOK, "linkedin_password": "dummy-test-value"})
        assert resp.status_code == 200
        upd.assert_called_once_with(_UID, "dummy-test-value")

    def test_linkedin_password_empty_400(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID):
            resp = client.put("/api/user/linkedin-password", json={
                "session_token": _TOK, "linkedin_password": ""})
        assert resp.status_code == 400

    def test_linkedin_password_save_failure_500(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.update_user_linkedin_password", return_value=False):
            resp = client.put("/api/user/linkedin-password", json={
                "session_token": _TOK, "linkedin_password": "dummy-test-value"})
        assert resp.status_code == 500


class TestAvatarEndpoints:
    def test_get_avatar_credits(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_avatar_credit_balance", return_value=3), \
             patch(f"{_M}.get_active_avatar", return_value={"id": 1}):
            resp = client.get(f"/api/avatar/credits?session_token={_TOK}")
        detail = resp.json()["detail"]
        assert detail["balance"] == 3 and detail["active_avatar"] == {"id": 1}

    def test_avatar_checkout_success(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_subscription_info",
                   return_value={"stripe_customer_id": "cus_1"}), \
             patch("cqc_lem.utilities.stripe_util.create_avatar_credits_checkout",
                   return_value="https://stripe/checkout") as cc:
            resp = client.post("/api/avatar/credits/checkout", json={
                "session_token": _TOK, "package": "single",
                "success_url": "https://x/ok", "cancel_url": "https://x/no"})
        if resp.status_code == 400:
            # package name not in AVATAR_CREDIT_PACKAGES — acceptable guard, assert it
            assert "Unknown package" in resp.json()["detail"]
        else:
            assert resp.status_code == 200
            assert resp.json()["detail"]["checkout_url"] == "https://stripe/checkout"

    def test_avatar_checkout_no_customer_400(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_subscription_info", return_value=None):
            resp = client.post("/api/avatar/credits/checkout", json={
                "session_token": _TOK, "package": "single",
                "success_url": "https://x/ok", "cancel_url": "https://x/no"})
        assert resp.status_code == 400

    def test_list_avatar_trainings(self, client):
        trainings = [{"id": 1, "training_id": "t1", "status": "succeeded"}]
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_avatar_trainings", return_value=trainings):
            resp = client.get(f"/api/avatar/trainings?session_token={_TOK}")
        assert resp.json()["detail"] == trainings

    def test_sync_training_status_terminal_state_returns_as_is(self, client):
        trainings = [{"id": 5, "training_id": "t5", "status": "succeeded"}]
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_avatar_trainings", return_value=trainings):
            resp = client.get(f"/api/avatar/training/5/status?session_token={_TOK}")
        assert resp.json()["detail"]["status"] == "succeeded"

    def test_sync_training_status_polls_replicate_for_running(self, client):
        trainings = [{"id": 5, "training_id": "t5", "status": "processing",
                      "model_ref": None}]
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_avatar_trainings", return_value=trainings), \
             patch("cqc_lem.utilities.avatar.replicate_avatar.poll_training_status",
                   return_value=("succeeded", "owner/model:v1")), \
             patch(f"{_M}.update_avatar_training_status") as upd:
            resp = client.get(f"/api/avatar/training/5/status?session_token={_TOK}")
        detail = resp.json()["detail"]
        assert detail["status"] == "succeeded" and detail["model_ref"] == "owner/model:v1"
        upd.assert_called_once_with("t5", "succeeded", "owner/model:v1")

    def test_sync_training_status_404_unknown_id(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_avatar_trainings", return_value=[]):
            resp = client.get(f"/api/avatar/training/99/status?session_token={_TOK}")
        assert resp.status_code == 404

    def test_activate_avatar_success(self, client):
        trainings = [{"id": 5, "training_id": "t5", "status": "succeeded"}]
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_avatar_trainings", return_value=trainings), \
             patch(f"{_M}.set_active_avatar", return_value=True) as act:
            resp = client.put("/api/avatar/training/5/activate",
                              json={"session_token": _TOK})
        assert resp.status_code == 200
        act.assert_called_once_with(_UID, 5)

    def test_activate_avatar_rejects_unfinished_training(self, client):
        trainings = [{"id": 5, "training_id": "t5", "status": "processing"}]
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_avatar_trainings", return_value=trainings):
            resp = client.put("/api/avatar/training/5/activate",
                              json={"session_token": _TOK})
        assert resp.status_code == 400

    def test_activate_avatar_404(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_avatar_trainings", return_value=[]):
            resp = client.put("/api/avatar/training/99/activate",
                              json={"session_token": _TOK})
        assert resp.status_code == 404


class TestAdminEndpoints:
    _HDR = {"X-Admin-Secret": "sekret"}

    def test_forbidden_without_secret(self, client):
        with patch(f"{_M}.ADMIN_SECRET", "sekret"):
            resp = client.post("/api/admin/fix-video-urls", json={
                "old_base": "http://a", "new_base": "http://b"})
        assert resp.status_code == 403

    def test_automation_pause(self, client):
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch("cqc_lem.utilities.linkedin.rate_limit.pause_automation", return_value=True) as pause:
            resp = client.post("/api/admin/automation-pause?hours=6", headers=self._HDR)
        assert resp.status_code == 200
        assert resp.json()["detail"] == {"paused": True, "seconds": 6 * 3600}
        assert pause.call_args[0][0] == 6 * 3600

    def test_automation_pause_forbidden_without_secret(self, client):
        with patch(f"{_M}.ADMIN_SECRET", "sekret"):
            resp = client.post("/api/admin/automation-pause", headers={})
        assert resp.status_code == 403

    def test_automation_resume(self, client):
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch("cqc_lem.utilities.linkedin.rate_limit.resume_automation", return_value=True):
            resp = client.post("/api/admin/automation-resume", headers=self._HDR)
        assert resp.status_code == 200
        assert resp.json()["detail"] == {"resumed": True}

    def test_automation_status(self, client):
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch("cqc_lem.utilities.linkedin.rate_limit.automation_pause_remaining", return_value=120), \
             patch("cqc_lem.utilities.linkedin.rate_limit.rate_limit_cooldown_remaining", return_value=900):
            resp = client.get("/api/admin/automation-status", headers=self._HDR)
        assert resp.status_code == 200
        d = resp.json()["detail"]
        assert d == {"paused": True, "pause_remaining_s": 120, "breaker_remaining_s": 900}

    def test_fix_video_urls(self, client):
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch(f"{_M}.replace_video_url_base", return_value=3) as rep:
            resp = client.post("/api/admin/fix-video-urls", headers=self._HDR, json={
                "old_base": "http://old", "new_base": "http://new"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["updated_rows"] == 3
        assert rep.call_args[0][:2] == ("http://old", "http://new")

    def test_regenerate_carousel_success(self, client):
        from cqc_lem.utilities.db import PostType
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch(f"{_M}.get_post_type", return_value=PostType.CAROUSEL), \
             patch(f"{_M}.get_post_buyer_stage", return_value=None), \
             patch("cqc_lem.app.run_content_plan.create_carousel_content",
                   return_value="new caption") as gen, \
             patch("cqc_lem.utilities.db.update_db_post_content") as upd:
            resp = client.post("/api/admin/regenerate-carousel", headers=self._HDR,
                               json={"post_id": 9, "user_id": 1})
        assert resp.status_code == 200
        assert gen.call_args[1]["stage"] == "awareness"  # None stage defaults
        upd.assert_called_once_with(9, "new caption")

    def test_regenerate_carousel_404_for_non_carousel(self, client):
        from cqc_lem.utilities.db import PostType
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch(f"{_M}.get_post_type", return_value=PostType.TEXT):
            resp = client.post("/api/admin/regenerate-carousel", headers=self._HDR,
                               json={"post_id": 9, "user_id": 1})
        assert resp.status_code == 404

    def test_regenerate_carousel_500_on_failure(self, client):
        from cqc_lem.utilities.db import PostType
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch(f"{_M}.get_post_type", return_value=PostType.CAROUSEL), \
             patch(f"{_M}.get_post_buyer_stage", return_value="awareness"), \
             patch("cqc_lem.app.run_content_plan.create_carousel_content",
                   side_effect=RuntimeError("AI fail")):
            resp = client.post("/api/admin/regenerate-carousel", headers=self._HDR,
                               json={"post_id": 9, "user_id": 1})
        assert resp.status_code == 500

    def test_regenerate_video_success(self, client):
        from cqc_lem.utilities.db import PostType
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch(f"{_M}.get_post_type", return_value=PostType.VIDEO), \
             patch("cqc_lem.app.run_content_plan.regenerate_video_for_post",
                   return_value="https://api/assets?file_name=v.mp4"):
            resp = client.post("/api/admin/regenerate-video", headers=self._HDR,
                               json={"post_id": 9, "user_id": 1})
        assert resp.status_code == 200
        assert resp.json()["detail"]["video_url"].endswith("v.mp4")

    def test_regenerate_video_404_for_non_video(self, client):
        from cqc_lem.utilities.db import PostType
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch(f"{_M}.get_post_type", return_value=PostType.TEXT):
            resp = client.post("/api/admin/regenerate-video", headers=self._HDR,
                               json={"post_id": 9, "user_id": 1})
        assert resp.status_code == 404

    def test_regenerate_video_500_when_no_asset(self, client):
        from cqc_lem.utilities.db import PostType
        with patch(f"{_M}.ADMIN_SECRET", "sekret"), \
             patch(f"{_M}.get_post_type", return_value=PostType.VIDEO), \
             patch("cqc_lem.app.run_content_plan.regenerate_video_for_post",
                   return_value=None):
            resp = client.post("/api/admin/regenerate-video", headers=self._HDR,
                               json={"post_id": 9, "user_id": 1})
        assert resp.status_code == 500


class TestCarouselTemplatesAndPreview:
    def test_list_templates_returns_catalog(self, client):
        resp = client.get("/api/carousel-templates")
        assert resp.status_code == 200
        templates = resp.json()["detail"]["templates"]
        assert templates and {"key", "label", "description"} <= set(templates[0])

    def test_generate_carousel_403_bad_session(self, client):
        with patch("cqc_lem.utilities.db.get_session_user_id", return_value=None):
            resp = client.post("/api/generate-carousel", json={
                "session_token": "bad", "stage": "awareness"})
        assert resp.status_code == 403

    def test_generate_carousel_success(self, client, tmp_path):
        carousel_dict = {"cover": {"title": "Cover"},
                         "contents": [{"title": "Tip 1", "content": "Do the thing"}],
                         "call_to_action": {"title": "Follow me"}}
        img = tmp_path / "slide_1.png"
        img.write_bytes(b"png")
        with patch("cqc_lem.utilities.db.get_session_user_id", return_value=_UID), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("caption text", carousel_dict)), \
             patch("cqc_lem.utilities.carousel_creator.create_carousel_slide_images",
                   return_value=[str(img)]):
            resp = client.post("/api/generate-carousel", json={
                "session_token": _TOK, "stage": "awareness"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["caption"] == "caption text"
        assert detail["template"] == "bold_listicle"
        assert detail["slide_urls"][0].endswith("slide_1.png")

    def test_generate_carousel_500_on_generation_error(self, client):
        with patch("cqc_lem.utilities.db.get_session_user_id", return_value=_UID), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   side_effect=RuntimeError("model error")):
            resp = client.post("/api/generate-carousel", json={
                "session_token": _TOK, "stage": "decision"})
        assert resp.status_code == 500


class TestAssetsCompatRedirect:
    def test_redirects_to_api_assets(self, client):
        resp = client.get("/assets?file_name=videos/x.mp4", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/api/assets?file_name=videos/x.mp4"

    def test_404_without_file_name(self, client):
        resp = client.get("/assets", follow_redirects=False)
        assert resp.status_code == 404


def _event(event_type, data):
    return {"type": event_type, "data": {"object": data}}


class TestStripeCreditWebhooks:
    BASE = "/api/billing/webhook"
    HDRS = {"Stripe-Signature": "sig", "Content-Type": "application/json"}

    def _post(self, client, event, extra_patches=()):
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event):
            for p in extra_patches:
                p.start()
            try:
                return client.post(self.BASE, content=b"{}", headers=self.HDRS)
            finally:
                for p in extra_patches:
                    p.stop()

    def test_invoice_payment_succeeded_refetches_subscription(self, client):
        event = _event("invoice.payment_succeeded", {
            "customer": "cus_1", "subscription": "sub_1"})
        sub = {"items": {"data": [{"price": {"id": "price_pro"}}]},
               "current_period_end": 1893456000}
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch("cqc_lem.utilities.stripe_util.fetch_subscription",
                   return_value=sub) as fetch, \
             patch("cqc_lem.utilities.stripe_util.get_subscription_tier_from_price", return_value="pro"), \
             patch(f"{_M}.update_subscription_from_stripe") as upd:
            resp = client.post(self.BASE, content=b"{}", headers=self.HDRS)
        assert resp.status_code == 200
        fetch.assert_called_once_with("sub_1")
        args = upd.call_args[0]
        assert args[0] == "cus_1" and args[1] == "active" and args[2] == "pro"

    def test_avatar_credit_checkout_grants_credits(self, client):
        event = _event("checkout.session.completed", {
            "id": "cs_1", "customer": "cus_1", "payment_status": "paid",
            "metadata": {"type": "avatar_credits", "credits": "5", "package": "pack5"}})
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch(f"{_M}.get_avatar_credit_ledger_entry_by_session", return_value=None), \
             patch(f"{_M}.get_user_by_stripe_customer_id", return_value={"id": _UID}), \
             patch(f"{_M}.add_avatar_credits") as add:
            resp = client.post(self.BASE, content=b"{}", headers=self.HDRS)
        assert resp.status_code == 200
        add.assert_called_once_with(_UID, 5, "purchase_pack5", "cs_1")

    def test_avatar_credit_checkout_idempotent_on_retry(self, client):
        event = _event("checkout.session.completed", {
            "id": "cs_1", "customer": "cus_1", "payment_status": "paid",
            "metadata": {"type": "avatar_credits", "credits": "5"}})
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch(f"{_M}.get_avatar_credit_ledger_entry_by_session",
                   return_value={"id": 1, "delta": 5}), \
             patch(f"{_M}.add_avatar_credits") as add:
            resp = client.post(self.BASE, content=b"{}", headers=self.HDRS)
        assert resp.status_code == 200
        add.assert_not_called()

    def test_avatar_credit_checkout_unpaid_skips_grant(self, client):
        event = _event("checkout.session.completed", {
            "id": "cs_1", "customer": "cus_1", "payment_status": "unpaid",
            "metadata": {"type": "avatar_credits", "credits": "5"}})
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch(f"{_M}.add_avatar_credits") as add:
            resp = client.post(self.BASE, content=b"{}", headers=self.HDRS)
        assert resp.status_code == 200
        add.assert_not_called()

    def test_video_credit_checkout_grants_credits(self, client):
        event = _event("checkout.session.completed", {
            "id": "cs_2", "customer": "cus_1", "payment_status": "paid",
            "metadata": {"type": "video_credits", "credits": "10", "package": "large"}})
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch(f"{_M}.get_video_credit_ledger_entry_by_session", return_value=None), \
             patch(f"{_M}.get_user_by_stripe_customer_id", return_value={"id": _UID}), \
             patch(f"{_M}.add_video_credits") as add:
            resp = client.post(self.BASE, content=b"{}", headers=self.HDRS)
        assert resp.status_code == 200
        add.assert_called_once_with(_UID, 10, "purchase_large", "cs_2")

    def test_full_refund_deducts_avatar_credits(self, client):
        event = _event("charge.refunded", {
            "payment_intent": "pi_1", "customer": "cus_1",
            "amount": 1000, "amount_refunded": 1000})
        session = {"id": "cs_1", "metadata": {"type": "avatar_credits"}}
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch("cqc_lem.utilities.stripe_util.get_checkout_session_by_payment_intent",
                   return_value=session), \
             patch(f"{_M}.get_avatar_credit_ledger_entry_by_session",
                   return_value={"id": 1, "user_id": _UID, "delta": 5}), \
             patch(f"{_M}.get_user_by_stripe_customer_id", return_value={"id": _UID}), \
             patch(f"{_M}.add_avatar_credits") as add:
            resp = client.post(self.BASE, content=b"{}", headers=self.HDRS)
        assert resp.status_code == 200
        assert add.call_args[0] == (_UID, -5, "refund_cs_1")

    def test_partial_refund_makes_no_adjustment(self, client):
        event = _event("charge.refunded", {
            "payment_intent": "pi_1", "customer": "cus_1",
            "amount": 1000, "amount_refunded": 400})
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch(f"{_M}.add_avatar_credits") as add_a, \
             patch(f"{_M}.add_video_credits") as add_v:
            resp = client.post(self.BASE, content=b"{}", headers=self.HDRS)
        assert resp.status_code == 200
        add_a.assert_not_called()
        add_v.assert_not_called()

    def test_refund_of_non_credit_charge_ignored(self, client):
        event = _event("charge.refunded", {
            "payment_intent": "pi_1", "customer": "cus_1",
            "amount": 1000, "amount_refunded": 1000})
        session = {"id": "cs_1", "metadata": {"type": "subscription"}}
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch("cqc_lem.utilities.stripe_util.get_checkout_session_by_payment_intent",
                   return_value=session), \
             patch(f"{_M}.add_avatar_credits") as add:
            resp = client.post(self.BASE, content=b"{}", headers=self.HDRS)
        assert resp.status_code == 200
        add.assert_not_called()
