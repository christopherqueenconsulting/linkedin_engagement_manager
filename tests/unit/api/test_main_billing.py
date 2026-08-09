"""Unit tests for FastAPI billing and auth endpoints in cqc_lem.api.main."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_MAIN = "cqc_lem.api.main"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _auth_hardening_side_effects():
    """Issue #745 (2b): every login now stamps `email_verified_at`, writes an `auth_audit_log` row
    and reads the PIN lockout, and /auth/session resolves the account's public_uid. Those are DB
    calls these tests never mocked — pin them so each test still exercises the flow it was written
    for. The hardening itself has its own suite (tests/unit/api/test_auth_hardening.py).
    """
    with patch(f"{_MAIN}.record_auth_event", return_value=True), \
         patch(f"{_MAIN}.mark_email_verified", return_value=True), \
         patch(f"{_MAIN}.get_pin_lockout", return_value=None), \
         patch(f"{_MAIN}.get_user_public_uid", return_value="pub-uid-1"):
        yield


@pytest.fixture(scope="module")
def client():
    """Return a FastAPI TestClient with all heavy imports pre-mocked."""
    # Patch modules that are imported at *module level* inside main.py before
    # the app object is created, so the import succeeds in the test process.
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
        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# POST /api/auth/email/init
# ---------------------------------------------------------------------------

class TestAuthEmailInit:
    BASE = "/api/auth/email/init"

    def test_missing_email_whitespace_returns_400(self, client):
        """An email that strips to empty should return 400."""
        with patch("cqc_lem.api.main.get_user_id", return_value=None), \
             patch("cqc_lem.api.main.generate_pin", return_value="123456"), \
             patch("cqc_lem.api.main.hash_pin", return_value="hashed"), \
             patch("cqc_lem.api.main.send_pin_email", return_value=(True, False)):
            resp = client.post(self.BASE, json={"email": "   "})
        assert resp.status_code == 400

    def test_bypass_mode_existing_user_returns_session_token(self, client):
        """When send_pin_email probe returns bypassed=True and user exists, return session_token."""
        with patch("cqc_lem.api.main.get_user_id", return_value=42), \
             patch("cqc_lem.api.main.generate_pin", return_value="123456"), \
             patch("cqc_lem.api.main.hash_pin", return_value="hashed"), \
             patch("cqc_lem.api.main.send_pin_email", return_value=(True, True)), \
             patch("cqc_lem.api.main.add_user_by_email") as mock_add, \
             patch("cqc_lem.api.main.create_session", return_value="tok_abc123"):
            resp = client.post(self.BASE, json={"email": "user@example.com"})
        assert resp.status_code == 200
        data = resp.json()["detail"]
        assert data["bypass"] is True
        assert data["session_token"] == "tok_abc123"
        assert data["is_new_user"] is False
        mock_add.assert_not_called()

    def test_bypass_mode_new_user_creates_user_then_session(self, client):
        """Bypass mode with unknown email: add_user_by_email is called to create user."""
        with patch("cqc_lem.api.main.get_user_id", return_value=None), \
             patch("cqc_lem.api.main.generate_pin", return_value="123456"), \
             patch("cqc_lem.api.main.hash_pin", return_value="hashed"), \
             patch("cqc_lem.api.main.send_pin_email", return_value=(True, True)), \
             patch("cqc_lem.api.main.add_user_by_email", return_value=99) as mock_add, \
             patch("cqc_lem.api.main.create_session", return_value="tok_new_user"):
            resp = client.post(self.BASE, json={"email": "new@example.com"})
        assert resp.status_code == 200
        body = resp.json()["detail"]
        assert body["bypass"] is True
        assert body["is_new_user"] is True
        assert body["session_token"] == "tok_new_user"
        mock_add.assert_called_once_with("new@example.com")

    def test_bypass_mode_user_creation_fails_returns_500(self, client):
        """Bypass mode: if add_user_by_email returns None, endpoint raises 500."""
        with patch("cqc_lem.api.main.get_user_id", return_value=None), \
             patch("cqc_lem.api.main.generate_pin", return_value="123456"), \
             patch("cqc_lem.api.main.hash_pin", return_value="hashed"), \
             patch("cqc_lem.api.main.send_pin_email", return_value=(True, True)), \
             patch("cqc_lem.api.main.add_user_by_email", return_value=None):
            resp = client.post(self.BASE, json={"email": "fail@example.com"})
        assert resp.status_code == 500

    def test_normal_flow_pin_sent_returns_200_no_bypass(self, client):
        """Normal flow: create_pin_for_email succeeds and email is sent → 200 with bypass=False."""
        with patch("cqc_lem.api.main.get_user_id", return_value=7), \
             patch("cqc_lem.api.main.generate_pin", return_value="654321"), \
             patch("cqc_lem.api.main.hash_pin", return_value="hashed_val"), \
             patch("cqc_lem.api.main.send_pin_email", side_effect=[(False, False), (True, False)]), \
             patch("cqc_lem.api.main.create_pin_for_email", return_value=True):
            resp = client.post(self.BASE, json={"email": "existing@example.com"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["bypass"] is False

    def test_email_send_fails_deletes_pin_and_returns_500(self, client):
        """When email send fails, delete_pin_for_email is called and 500 is returned."""
        with patch("cqc_lem.api.main.get_user_id", return_value=5), \
             patch("cqc_lem.api.main.generate_pin", return_value="111111"), \
             patch("cqc_lem.api.main.hash_pin", return_value="h"), \
             patch("cqc_lem.api.main.send_pin_email", side_effect=[(False, False), (False, False)]), \
             patch("cqc_lem.api.main.create_pin_for_email", return_value=True), \
             patch("cqc_lem.api.main.delete_pin_for_email") as mock_del:
            resp = client.post(self.BASE, json={"email": "fail_send@example.com"})
        assert resp.status_code == 500
        mock_del.assert_called_once_with("fail_send@example.com")

    def test_create_pin_db_failure_returns_500(self, client):
        """If create_pin_for_email returns False, 500 is returned without calling send."""
        with patch("cqc_lem.api.main.get_user_id", return_value=3), \
             patch("cqc_lem.api.main.generate_pin", return_value="000000"), \
             patch("cqc_lem.api.main.hash_pin", return_value="h"), \
             patch("cqc_lem.api.main.send_pin_email", return_value=(False, False)), \
             patch("cqc_lem.api.main.create_pin_for_email", return_value=False):
            resp = client.post(self.BASE, json={"email": "dbfail@example.com"})
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/auth/email/verify
# ---------------------------------------------------------------------------

class TestAuthEmailVerify:
    BASE = "/api/auth/email/verify"

    def test_missing_email_returns_400(self, client):
        with patch("cqc_lem.api.main.hash_pin", return_value="h"), \
             patch("cqc_lem.api.main.verify_pin_for_email", return_value=True):
            resp = client.post(self.BASE, json={"email": "  ", "pin": "123456"})
        assert resp.status_code == 400

    def test_missing_pin_returns_400(self, client):
        with patch("cqc_lem.api.main.hash_pin", return_value="h"), \
             patch("cqc_lem.api.main.verify_pin_for_email", return_value=True):
            resp = client.post(self.BASE, json={"email": "user@example.com", "pin": "  "})
        assert resp.status_code == 400

    def test_invalid_pin_returns_401(self, client):
        with patch("cqc_lem.api.main.hash_pin", return_value="h"), \
             patch("cqc_lem.api.main.verify_pin_for_email", return_value=False):
            resp = client.post(self.BASE, json={"email": "user@example.com", "pin": "000000"})
        assert resp.status_code == 401

    def test_valid_pin_existing_user_returns_session(self, client):
        """Valid PIN for known user: no add_user_by_email call, session_token in response."""
        with patch("cqc_lem.api.main.hash_pin", return_value="h"), \
             patch("cqc_lem.api.main.verify_pin_for_email", return_value=True), \
             patch("cqc_lem.api.main.get_user_id", return_value=10), \
             patch("cqc_lem.api.main.add_user_by_email") as mock_add, \
             patch("cqc_lem.api.main.create_session", return_value="sess_existing"):
            resp = client.post(self.BASE, json={"email": "user@example.com", "pin": "123456"})
        assert resp.status_code == 200
        body = resp.json()["detail"]
        assert body["session_token"] == "sess_existing"
        assert body["is_new_user"] is False
        mock_add.assert_not_called()

    def test_valid_pin_new_user_creates_user_then_session(self, client):
        """Valid PIN for unknown email: add_user_by_email is called and new session returned."""
        with patch("cqc_lem.api.main.hash_pin", return_value="h"), \
             patch("cqc_lem.api.main.verify_pin_for_email", return_value=True), \
             patch("cqc_lem.api.main.get_user_id", return_value=None), \
             patch("cqc_lem.api.main.add_user_by_email", return_value=55) as mock_add, \
             patch("cqc_lem.api.main.create_session", return_value="sess_new"):
            resp = client.post(self.BASE, json={"email": "brand_new@example.com", "pin": "654321"})
        assert resp.status_code == 200
        body = resp.json()["detail"]
        assert body["session_token"] == "sess_new"
        assert body["is_new_user"] is True
        mock_add.assert_called_once_with("brand_new@example.com")

    def test_user_creation_failure_returns_500(self, client):
        with patch("cqc_lem.api.main.hash_pin", return_value="h"), \
             patch("cqc_lem.api.main.verify_pin_for_email", return_value=True), \
             patch("cqc_lem.api.main.get_user_id", return_value=None), \
             patch("cqc_lem.api.main.add_user_by_email", return_value=None):
            resp = client.post(self.BASE, json={"email": "create_fail@example.com", "pin": "111111"})
        assert resp.status_code == 500

    def test_session_creation_failure_returns_500(self, client):
        with patch("cqc_lem.api.main.hash_pin", return_value="h"), \
             patch("cqc_lem.api.main.verify_pin_for_email", return_value=True), \
             patch("cqc_lem.api.main.get_user_id", return_value=10), \
             patch("cqc_lem.api.main.create_session", return_value=None):
            resp = client.post(self.BASE, json={"email": "user@example.com", "pin": "123456"})
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/billing/create-checkout-session
# ---------------------------------------------------------------------------

class TestBillingCreateCheckoutSession:
    BASE = "/api/billing/create-checkout-session"
    PAYLOAD = {
        "session_token": "valid_token",
        "tier": "starter",
        "success_url": "https://example.com/success",
        "cancel_url": "https://example.com/cancel",
    }

    def test_invalid_session_returns_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 401

    def test_no_stripe_customer_id_returns_400(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info", return_value={"stripe_customer_id": None}):
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 400

    def test_no_subscription_info_returns_400(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info", return_value=None):
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 400

    def test_existing_active_subscription_calls_upgrade(self, client):
        """User with active subscription: upgrade_subscription is called, no checkout URL."""
        sub_info = {
            "stripe_customer_id": "cus_abc",
            "stripe_subscription_id": "sub_xyz",
            "subscription_status": "active",
        }
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info", return_value=sub_info), \
             patch("cqc_lem.utilities.stripe_util.upgrade_subscription", return_value=True) as mock_upgrade, \
             patch("cqc_lem.utilities.stripe_util.create_checkout_session") as mock_checkout:
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 200
        body = resp.json()["detail"]
        assert body["upgraded"] is True
        assert body["checkout_url"] is None
        mock_checkout.assert_not_called()

    def test_existing_trial_subscription_calls_upgrade(self, client):
        """User with trial subscription is also treated as upgradeable."""
        sub_info = {
            "stripe_customer_id": "cus_trial",
            "stripe_subscription_id": "sub_trial",
            "subscription_status": "trial",
        }
        with patch("cqc_lem.api.main.get_session_user_id", return_value=2), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info", return_value=sub_info), \
             patch("cqc_lem.utilities.stripe_util.upgrade_subscription", return_value=True):
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["detail"]["upgraded"] is True

    def test_no_existing_subscription_creates_checkout_session(self, client):
        """No active subscription: create_checkout_session is called and URL is returned."""
        sub_info = {
            "stripe_customer_id": "cus_new",
            "stripe_subscription_id": None,
            "subscription_status": None,
        }
        with patch("cqc_lem.api.main.get_session_user_id", return_value=3), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info", return_value=sub_info), \
             patch("cqc_lem.utilities.stripe_util.create_checkout_session",
                   return_value="https://checkout.stripe.com/pay/abc") as mock_checkout:
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 200
        body = resp.json()["detail"]
        assert body["checkout_url"] == "https://checkout.stripe.com/pay/abc"
        assert body["upgraded"] is False
        mock_checkout.assert_called_once_with(
            "cus_new", "starter",
            "https://example.com/success",
            "https://example.com/cancel",
            trial_period_days=None,   # no early-adopter grant (issue #499)
            discounts=None,
        )

    def test_create_checkout_session_returns_none_raises_500(self, client):
        sub_info = {
            "stripe_customer_id": "cus_fail",
            "stripe_subscription_id": None,
            "subscription_status": None,
        }
        with patch("cqc_lem.api.main.get_session_user_id", return_value=4), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info", return_value=sub_info), \
             patch("cqc_lem.utilities.stripe_util.create_checkout_session", return_value=None):
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 500

    def test_upgrade_fails_falls_back_to_checkout(self, client):
        """If upgrade_subscription returns False, falls back to create_checkout_session."""
        sub_info = {
            "stripe_customer_id": "cus_fallback",
            "stripe_subscription_id": "sub_old",
            "subscription_status": "active",
        }
        with patch("cqc_lem.api.main.get_session_user_id", return_value=5), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info", return_value=sub_info), \
             patch("cqc_lem.utilities.stripe_util.upgrade_subscription", return_value=False), \
             patch("cqc_lem.utilities.stripe_util.create_checkout_session",
                   return_value="https://checkout.stripe.com/pay/fallback"):
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["detail"]["checkout_url"] == "https://checkout.stripe.com/pay/fallback"


# ---------------------------------------------------------------------------
# POST /api/billing/create-portal-session
# ---------------------------------------------------------------------------

class TestBillingCreatePortalSession:
    BASE = "/api/billing/create-portal-session"
    PAYLOAD = {"session_token": "valid_token", "return_url": "https://example.com/account"}

    def test_invalid_session_returns_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 401

    def test_no_stripe_customer_id_returns_400(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info", return_value={"stripe_customer_id": None}):
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 400

    def test_success_returns_portal_url(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info",
                   return_value={"stripe_customer_id": "cus_portal"}), \
             patch("cqc_lem.utilities.stripe_util.create_portal_session",
                   return_value="https://billing.stripe.com/session/portal_abc") as mock_portal:
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["detail"]["portal_url"] == "https://billing.stripe.com/session/portal_abc"
        mock_portal.assert_called_once_with("cus_portal", "https://example.com/account")

    def test_portal_session_returns_none_raises_500(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.billing.get_user_subscription_info",
                   return_value={"stripe_customer_id": "cus_portal"}), \
             patch("cqc_lem.utilities.stripe_util.create_portal_session", return_value=None):
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/billing/webhook
# ---------------------------------------------------------------------------

def _build_webhook_event(event_type: str, data: dict) -> dict:
    """Minimal Stripe event envelope."""
    return {"type": event_type, "data": {"object": data}}


class TestBillingWebhook:
    BASE = "/api/billing/webhook"

    def _post(self, client, payload: bytes, sig: str, event: dict):
        """Helper: patches validate_webhook to return event and POSTs raw body."""
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch("cqc_lem.utilities.stripe_util.get_subscription_tier_from_price", return_value="starter"), \
             patch("cqc_lem.utilities.stripe_util.stripe_status_to_db", return_value="active"), \
             patch("cqc_lem.api.routers.billing.update_subscription_from_stripe") as mock_update:
            resp = client.post(
                self.BASE, content=payload,
                headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
            )
        return resp, mock_update

    def test_invalid_signature_returns_400(self, client):
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=None), \
             patch("cqc_lem.utilities.stripe_util.get_subscription_tier_from_price", return_value=None), \
             patch("cqc_lem.utilities.stripe_util.stripe_status_to_db", return_value="active"):
            resp = client.post(
                self.BASE, content=b'{"type":"test"}',
                headers={"Stripe-Signature": "bad_sig", "Content-Type": "application/json"},
            )
        assert resp.status_code == 400

    def test_subscription_created_calls_update(self, client):
        event = _build_webhook_event("customer.subscription.created", {
            "customer": "cus_abc",
            "id": "sub_001",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_starter"}}]},
            "current_period_end": 1893456000,
        })
        resp, mock_update = self._post(client, b"{}", "sig_ok", event)
        assert resp.status_code == 200
        assert resp.json() == {"received": True}
        mock_update.assert_called_once()
        call_args = mock_update.call_args[0]
        assert call_args[0] == "cus_abc"

    def test_subscription_deleted_calls_update_with_cancelled(self, client):
        event = _build_webhook_event("customer.subscription.deleted", {
            "customer": "cus_del",
            "id": "sub_002",
        })
        resp, mock_update = self._post(client, b"{}", "sig_ok", event)
        assert resp.status_code == 200
        mock_update.assert_called_once()
        call_args = mock_update.call_args[0]
        assert call_args[0] == "cus_del"
        assert call_args[1] == "cancelled"

    def test_invoice_payment_failed_calls_update_past_due(self, client):
        event = _build_webhook_event("invoice.payment_failed", {
            "customer": "cus_past_due",
            "subscription": "sub_past_due",
        })
        resp, mock_update = self._post(client, b"{}", "sig_ok", event)
        assert resp.status_code == 200
        mock_update.assert_called_once()
        call_args = mock_update.call_args[0]
        assert call_args[0] == "cus_past_due"
        assert call_args[1] == "past_due"

    def test_subscription_created_missing_customer_skips_update(self, client):
        """Missing customer field: update is NOT called and {"received": True} is returned."""
        event = _build_webhook_event("customer.subscription.created", {
            "id": "sub_no_cus",
            "status": "active",
        })
        resp, mock_update = self._post(client, b"{}", "sig_ok", event)
        assert resp.status_code == 200
        assert resp.json() == {"received": True}
        mock_update.assert_not_called()

    def test_subscription_deleted_missing_customer_skips_update(self, client):
        event = _build_webhook_event("customer.subscription.deleted", {
            "id": "sub_no_cus",
        })
        resp, mock_update = self._post(client, b"{}", "sig_ok", event)
        assert resp.status_code == 200
        mock_update.assert_not_called()

    def test_invoice_payment_failed_missing_customer_skips_update(self, client):
        event = _build_webhook_event("invoice.payment_failed", {
            "subscription": "sub_xyz",
        })
        resp, mock_update = self._post(client, b"{}", "sig_ok", event)
        assert resp.status_code == 200
        mock_update.assert_not_called()

    def test_unknown_event_type_returns_received_true_no_update(self, client):
        event = _build_webhook_event("charge.refunded", {"customer": "cus_abc"})
        resp, mock_update = self._post(client, b"{}", "sig_ok", event)
        assert resp.status_code == 200
        assert resp.json() == {"received": True}
        mock_update.assert_not_called()

    def test_subscription_updated_calls_update(self, client):
        event = _build_webhook_event("customer.subscription.updated", {
            "customer": "cus_upd",
            "id": "sub_upd",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_pro"}}]},
            "current_period_end": 1893456000,
        })
        resp, mock_update = self._post(client, b"{}", "sig_ok", event)
        assert resp.status_code == 200
        mock_update.assert_called_once()
        assert mock_update.call_args[0][0] == "cus_upd"
