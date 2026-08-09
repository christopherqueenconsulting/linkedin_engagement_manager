"""Funnel-event emission from the signup and billing endpoints (issue #503)."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_MAIN = "cqc_lem.api.main"
_AUTH = "cqc_lem.api.routers.auth"
_BILL = "cqc_lem.api.routers.billing"


@pytest.fixture(autouse=True)
def _auth_hardening_side_effects():
    """Issue #745 (2b): every login now stamps `email_verified_at`, writes an `auth_audit_log` row
    and reads the PIN lockout, and /auth/session resolves the account's public_uid. Those are DB
    calls these tests never mocked — pin them so each test still exercises the flow it was written
    for. The hardening itself has its own suite (tests/unit/api/test_auth_hardening.py).
    """
    with patch(f"{_MAIN}.record_auth_event", return_value=True), \
         patch(f"{_AUTH}.record_auth_event", return_value=True), \
         patch(f"{_AUTH}.mark_email_verified", return_value=True), \
         patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
         patch(f"{_AUTH}.get_user_public_uid", return_value="pub-uid-1"):
        yield


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
        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


ATTRIBUTION = {"utm_source": "linkedin", "utm_medium": "social", "utm_campaign": "beta",
               "landing_page": "/"}


def _events(mock_track) -> list:
    return [c.args[0] for c in mock_track.call_args_list]


class TestSignupFunnel:
    INIT = "/api/auth/email/init"
    VERIFY = "/api/auth/email/verify"

    def test_new_email_starts_the_funnel_with_its_attribution(self, client):
        with patch(f"{_AUTH}.get_user_id", return_value=None), \
             patch(f"{_AUTH}.generate_pin", return_value="123456"), \
             patch(f"{_AUTH}.hash_pin", return_value="hashed"), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, False)), \
             patch(f"{_AUTH}.create_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.track_funnel_event") as track:
            resp = client.post(self.INIT, json={"email": "New@Example.com",
                                                "attribution": ATTRIBUTION})

        assert resp.status_code == 200
        assert _events(track) == ["signup_started"]
        kwargs = track.call_args.kwargs
        assert kwargs["attribution"]["utm_source"] == "linkedin"
        assert kwargs["attribution"]["landing_page"] == "/"
        # Pre-signup there is no user row, so the event is keyed to a pseudonymous id.
        assert kwargs["distinct_id"].startswith("anon_")

    def test_a_referral_ref_survives_the_request_model(self, client):
        """Issue #658: `?ref=<user id>` is what a referral link carries. It has to be declared on
        FunnelAttribution or FastAPI drops it before normalize_attribution ever sees it.
        """
        with patch(f"{_AUTH}.get_user_id", return_value=None), \
             patch(f"{_AUTH}.generate_pin", return_value="123456"), \
             patch(f"{_AUTH}.hash_pin", return_value="hashed"), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, False)), \
             patch(f"{_AUTH}.create_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.track_funnel_event") as track:
            resp = client.post(self.INIT, json={
                "email": "referred@example.com",
                "attribution": {"utm_source": "referral", "utm_medium": "referral", "ref": "7"}})

        assert resp.status_code == 200
        assert track.call_args.kwargs["attribution"]["ref"] == "7"

    def test_known_email_is_a_login_not_a_signup(self, client):
        with patch(f"{_AUTH}.get_user_id", return_value=5), \
             patch(f"{_AUTH}.generate_pin", return_value="123456"), \
             patch(f"{_AUTH}.hash_pin", return_value="hashed"), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, False)), \
             patch(f"{_AUTH}.create_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.track_funnel_event") as track:
            resp = client.post(self.INIT, json={"email": "known@example.com"})

        assert resp.status_code == 200
        assert track.call_count == 0

    def test_attribution_is_optional(self, client):
        with patch(f"{_AUTH}.get_user_id", return_value=None), \
             patch(f"{_AUTH}.generate_pin", return_value="123456"), \
             patch(f"{_AUTH}.hash_pin", return_value="hashed"), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, False)), \
             patch(f"{_AUTH}.create_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.track_funnel_event") as track:
            resp = client.post(self.INIT, json={"email": "bare@example.com"})

        assert resp.status_code == 200
        assert track.call_args.kwargs["attribution"] == {}

    def test_bypass_signup_completes_and_starts_the_trial(self, client):
        with patch(f"{_AUTH}.get_user_id", return_value=None), \
             patch(f"{_AUTH}.generate_pin", return_value="123456"), \
             patch(f"{_AUTH}.hash_pin", return_value="hashed"), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, True)), \
             patch(f"{_AUTH}.add_user_by_email", return_value=99), \
             patch(f"{_AUTH}.create_session", return_value="tok"), \
             patch(f"{_AUTH}.track_funnel_event") as track:
            resp = client.post(self.INIT, json={"email": "bypass@example.com",
                                                "attribution": ATTRIBUTION})

        assert resp.status_code == 200
        assert _events(track) == ["signup_started", "signup_completed", "trial_started"]
        completed = track.call_args_list[1].kwargs
        assert completed["user_id"] == 99
        assert completed["pin_bypassed"] is True
        # The anonymous signup_started person is merged into the real user.
        assert completed["alias_from"] == track.call_args_list[0].kwargs["distinct_id"]
        assert track.call_args_list[2].kwargs["trial_days"] > 0

    def test_verify_completes_the_funnel_for_a_new_user(self, client):
        with patch(f"{_AUTH}.hash_pin", return_value="hashed"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_user_id", return_value=None), \
             patch(f"{_AUTH}.add_user_by_email", return_value=12), \
             patch(f"{_AUTH}.create_session", return_value="tok"), \
             patch(f"{_AUTH}.track_funnel_event") as track:
            resp = client.post(self.VERIFY, json={"email": "verify@example.com", "pin": "123456",
                                                  "attribution": ATTRIBUTION})

        assert resp.status_code == 200
        assert _events(track) == ["signup_completed", "trial_started"]
        assert track.call_args_list[0].kwargs["pin_bypassed"] is False
        assert track.call_args_list[1].kwargs["attribution"]["utm_campaign"] == "beta"

    def test_verify_emits_nothing_for_a_returning_user(self, client):
        with patch(f"{_AUTH}.hash_pin", return_value="hashed"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_user_id", return_value=8), \
             patch(f"{_AUTH}.create_session", return_value="tok"), \
             patch(f"{_AUTH}.track_funnel_event") as track:
            resp = client.post(self.VERIFY, json={"email": "back@example.com", "pin": "123456"})

        assert resp.status_code == 200
        assert track.call_count == 0


class TestBillingFunnel:
    BASE = "/api/billing/webhook"

    def _post(self, client, event: dict):
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch("cqc_lem.utilities.stripe_util.get_subscription_tier_from_price",
                   return_value="starter"), \
             patch("cqc_lem.utilities.stripe_util.stripe_status_to_db", return_value="active"), \
             patch(f"{_BILL}.update_subscription_from_stripe"), \
             patch(f"{_BILL}.get_user_by_stripe_customer_id", return_value={"id": 4}), \
             patch(f"{_BILL}.track_funnel_event") as track:
            resp = client.post(self.BASE, content=b"{}",
                               headers={"Stripe-Signature": "sig",
                                        "Content-Type": "application/json"})
        return resp, track

    def test_subscription_created_emits_subscription_started(self, client):
        resp, track = self._post(client, {"type": "customer.subscription.created", "data": {
            "object": {"customer": "cus_a", "id": "sub_1", "status": "active",
                       "items": {"data": [{"price": {"id": "price_starter"}}]}}}})
        assert resp.status_code == 200
        assert _events(track) == ["subscription_started"]
        kwargs = track.call_args.kwargs
        assert kwargs["user_id"] == 4
        assert kwargs["tier"] == "starter"
        assert kwargs["stripe_customer_id"] == "cus_a"

    def test_subscription_updated_does_not_double_count(self, client):
        resp, track = self._post(client, {"type": "customer.subscription.updated", "data": {
            "object": {"customer": "cus_a", "id": "sub_1", "status": "active",
                       "items": {"data": [{"price": {"id": "price_starter"}}]}}}})
        assert resp.status_code == 200
        assert track.call_count == 0

    def test_subscription_deleted_emits_churned(self, client):
        resp, track = self._post(client, {"type": "customer.subscription.deleted", "data": {
            "object": {"customer": "cus_b", "id": "sub_2"}}})
        assert resp.status_code == 200
        assert _events(track) == ["churned"]
        assert track.call_args.kwargs["reason"] == "subscription_deleted"

    def test_a_failed_lookup_never_fails_the_webhook(self, client):
        event = {"type": "customer.subscription.deleted",
                 "data": {"object": {"customer": "cus_c", "id": "sub_3"}}}
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value=event), \
             patch(f"{_BILL}.update_subscription_from_stripe"), \
             patch(f"{_BILL}.get_user_by_stripe_customer_id",
                   side_effect=RuntimeError("db down")), \
             patch(f"{_BILL}.track_funnel_event") as track:
            resp = client.post(self.BASE, content=b"{}",
                               headers={"Stripe-Signature": "sig",
                                        "Content-Type": "application/json"})
        assert resp.status_code == 200
        assert resp.json() == {"received": True}
        assert track.call_count == 0
