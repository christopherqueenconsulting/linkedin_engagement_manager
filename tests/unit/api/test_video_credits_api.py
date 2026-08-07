"""Unit tests for video credit + upgrade API endpoints."""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.db import PostType

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
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


class TestBalance:
    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = client.get("/api/video/credits", params={"session_token": "x"})
        assert r.status_code == 401

    def test_200(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_video_credit_balance", return_value=12):
            r = client.get("/api/video/credits", params={"session_token": "x"})
        assert r.status_code == 200 and r.json()["detail"]["balance"] == 12


class TestCheckout:
    def test_200(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_user_subscription_info", return_value={"stripe_customer_id": "cus_1"}), \
             patch("cqc_lem.utilities.stripe_util.create_video_credits_checkout", return_value="https://stripe"):
            r = client.post("/api/video/credits/checkout",
                            json={"session_token": "x", "package": "medium", "success_url": "a", "cancel_url": "b"})
        assert r.status_code == 200 and r.json()["detail"]["checkout_url"] == "https://stripe"

    def test_unknown_package_400(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_user_subscription_info", return_value={"stripe_customer_id": "cus_1"}):
            r = client.post("/api/video/credits/checkout",
                            json={"session_token": "x", "package": "nope", "success_url": "a", "cancel_url": "b"})
        assert r.status_code == 400

    def test_no_customer_400(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_user_subscription_info", return_value={"stripe_customer_id": None}):
            r = client.post("/api/video/credits/checkout",
                            json={"session_token": "x", "package": "medium", "success_url": "a", "cancel_url": "b"})
        assert r.status_code == 400


class TestUpgrade:
    def test_not_owner_403(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_post_user_id", return_value=2):
            r = client.post("/api/video/upgrade", json={"session_token": "x", "post_id": 9, "tier": "premium"})
        assert r.status_code == 403

    def test_not_video_404(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_post_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_post_type", return_value=PostType.TEXT):
            r = client.post("/api/video/upgrade", json={"session_token": "x", "post_id": 9, "tier": "premium"})
        assert r.status_code == 404

    def test_insufficient_402(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_post_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_post_type", return_value=PostType.VIDEO), \
             patch("cqc_lem.api.main.get_video_credit_balance", return_value=0):
            r = client.post("/api/video/upgrade", json={"session_token": "x", "post_id": 9, "tier": "premium"})
        assert r.status_code == 402

    def test_queues_200(self, client):
        task = MagicMock()
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_post_user_id", return_value=1), \
             patch("cqc_lem.api.main.get_post_type", return_value=PostType.VIDEO), \
             patch("cqc_lem.api.main.get_video_credit_balance", return_value=5), \
             patch("cqc_lem.api.main.update_post_video_quality", return_value=True), \
             patch("cqc_lem.app.run_content_plan.regenerate_post_video_task", task):
            r = client.post("/api/video/upgrade", json={"session_token": "x", "post_id": 9, "tier": "premium_top"})
        assert r.status_code == 200
        assert r.json()["detail"]["status"] == "queued" and r.json()["detail"]["credits_required"] == 3
        task.apply_async.assert_called_once()


class TestWebhookFulfillment:
    def test_video_credits_granted_on_paid(self, client):
        event = {"type": "checkout.session.completed", "data": {"object": {
            "payment_status": "paid", "customer": "cus_1", "id": "sess_1",
            "metadata": {"type": "video_credits", "package": "medium", "credits": "15"},
        }}}
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch("cqc_lem.api.main.get_video_credit_ledger_entry_by_session", return_value=None), \
             patch("cqc_lem.api.main.get_user_by_stripe_customer_id", return_value={"id": 7}), \
             patch("cqc_lem.api.main.add_video_credits") as add:
            resp = client.post("/api/billing/webhook", content=b"{}",
                               headers={"Stripe-Signature": "sig", "Content-Type": "application/json"})
        assert resp.status_code == 200
        add.assert_called_once()
        assert add.call_args[0][0] == 7 and add.call_args[0][1] == 15

    def test_video_credits_idempotent_skips_duplicate(self, client):
        event = {"type": "checkout.session.completed", "data": {"object": {
            "payment_status": "paid", "customer": "cus_1", "id": "sess_dup",
            "metadata": {"type": "video_credits", "package": "small", "credits": "5"},
        }}}
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch("cqc_lem.api.main.get_video_credit_ledger_entry_by_session", return_value={"id": 1}), \
             patch("cqc_lem.api.main.add_video_credits") as add:
            resp = client.post("/api/billing/webhook", content=b"{}",
                               headers={"Stripe-Signature": "sig", "Content-Type": "application/json"})
        assert resp.status_code == 200
        add.assert_not_called()
