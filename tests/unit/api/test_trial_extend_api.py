"""Unit tests for POST /api/trial/extend and the Stripe conversion mirror (issue #499)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_MAIN = "cqc_lem.api.main"
_ENV = "cqc_lem.utilities.env_constants"


@pytest.fixture(scope="module")
def client():
    from cqc_lem.api.main import app
    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


class TestTrialExtendEndpoint:
    BASE = "/api/trial/extend"

    def test_disabled_flag_returns_404_without_touching_db(self, client):
        with patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_ENABLED", False), \
             patch(f"{_MAIN}.extend_trial_for_user") as extend:
            resp = client.post(self.BASE, json={"session_token": "tok"})
        assert resp.status_code == 404
        extend.assert_not_called()

    def test_invalid_session_returns_401(self, client):
        with patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_ENABLED", True), \
             patch(f"{_MAIN}.get_session_user_id", return_value=None), \
             patch(f"{_MAIN}.extend_trial_for_user") as extend:
            resp = client.post(self.BASE, json={"session_token": "bad"})
        assert resp.status_code == 401
        extend.assert_not_called()

    def test_no_review_returns_prompt_and_grants_nothing(self, client):
        with patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_ENABLED", True), \
             patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_DAYS", 60), \
             patch(f"{_MAIN}.get_session_user_id", return_value=7), \
             patch(f"{_MAIN}.get_latest_review_feedback_id", return_value=None), \
             patch(f"{_MAIN}.extend_trial_for_user") as extend:
            resp = client.post(self.BASE, json={"session_token": "tok"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["granted"] is False
        assert detail["reason"] == "review_required"
        assert "60 days" in detail["message"]
        extend.assert_not_called()

    def test_review_present_grants_and_passes_feedback_id(self, client):
        ends = datetime(2026, 9, 1, 12, 0, 0)
        with patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_ENABLED", True), \
             patch(f"{_MAIN}.get_session_user_id", return_value=7), \
             patch(f"{_MAIN}.get_latest_review_feedback_id", return_value=31), \
             patch(f"{_MAIN}.extend_trial_for_user",
                   return_value={"granted": True, "reason": "granted", "cohort": "P0",
                                 "trial_days": 60, "trial_ends_at": ends}) as extend:
            resp = client.post(self.BASE, json={"session_token": "tok"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail == {"granted": True, "reason": "granted", "cohort": "P0",
                          "trial_days": 60, "trial_ends_at": ends.isoformat()}
        extend.assert_called_once_with(7, feedback_id=31)

    def test_exhausted_cohorts_are_a_200_not_an_error(self, client):
        with patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_ENABLED", True), \
             patch(f"{_MAIN}.get_session_user_id", return_value=7), \
             patch(f"{_MAIN}.get_latest_review_feedback_id", return_value=31), \
             patch(f"{_MAIN}.extend_trial_for_user",
                   return_value={"granted": False, "reason": "slots_exhausted", "cohort": None,
                                 "trial_days": 14, "trial_ends_at": None}):
            resp = client.post(self.BASE, json={"session_token": "tok"})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert (detail["granted"], detail["reason"], detail["trial_days"]) == (False, "slots_exhausted", 14)
        assert detail["trial_ends_at"] is None


class TestEarlyAdopterCheckoutExtras:
    def test_no_grant_means_no_trial_and_no_discount(self):
        from cqc_lem.api.main import _early_adopter_checkout_extras
        with patch(f"{_MAIN}.get_early_adopter_grant", return_value=None):
            assert _early_adopter_checkout_extras(7) == (None, None)

    def test_remaining_days_are_rounded_up_and_coupon_applied(self):
        from cqc_lem.api.main import _early_adopter_checkout_extras
        grant = {"trial_ends_at": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=40, hours=6)}
        with patch(f"{_MAIN}.get_early_adopter_grant", return_value=grant), \
             patch(f"{_ENV}.EARLY_ADOPTER_COUPON_ID", "EARLYBIRD"):
            days, discounts = _early_adopter_checkout_extras(7)
        assert days == 41  # 40d6h rounds up so a partial day isn't forfeited
        assert discounts == [{"coupon": "EARLYBIRD"}]

    def test_lookup_failure_degrades_to_a_normal_checkout(self):
        """A perk lookup must never stop a user from paying us."""
        from cqc_lem.api.main import _early_adopter_checkout_extras
        with patch(f"{_MAIN}.get_early_adopter_grant", side_effect=RuntimeError("db down")), \
             patch(f"{_MAIN}.log_warning") as warned:
            assert _early_adopter_checkout_extras(7) == (None, None)
        assert warned.called

    def test_expired_grant_sends_no_trial_days(self):
        from cqc_lem.api.main import _early_adopter_checkout_extras
        grant = {"trial_ends_at": datetime.now(timezone.utc) - timedelta(days=3)}
        with patch(f"{_MAIN}.get_early_adopter_grant", return_value=grant), \
             patch(f"{_ENV}.EARLY_ADOPTER_COUPON_ID", ""):
            assert _early_adopter_checkout_extras(7) == (None, None)

    def test_expired_grant_does_not_apply_the_coupon_either(self):
        """The coupon is part of the unfinished trial — a lapsed grant must not discount forever."""
        from cqc_lem.api.main import _early_adopter_checkout_extras
        grant = {"trial_ends_at": datetime.now(timezone.utc) - timedelta(days=90)}
        with patch(f"{_MAIN}.get_early_adopter_grant", return_value=grant), \
             patch(f"{_ENV}.EARLY_ADOPTER_COUPON_ID", "EARLYBIRD"):
            assert _early_adopter_checkout_extras(7) == (None, None)

    def test_grant_with_no_end_date_carries_nothing(self):
        from cqc_lem.api.main import _early_adopter_checkout_extras
        with patch(f"{_MAIN}.get_early_adopter_grant", return_value={"trial_ends_at": None}), \
             patch(f"{_ENV}.EARLY_ADOPTER_COUPON_ID", "EARLYBIRD"):
            assert _early_adopter_checkout_extras(7) == (None, None)


class TestCheckoutSessionMirrorsGrant:
    BASE = "/api/billing/create-checkout-session"
    PAYLOAD = {"session_token": "tok", "tier": "starter",
               "success_url": "https://e.com/s", "cancel_url": "https://e.com/c"}

    def test_grant_holder_conversion_forwards_trial_and_coupon(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=7), \
             patch(f"{_MAIN}.get_user_subscription_info",
                   return_value={"stripe_customer_id": "cus_1", "stripe_subscription_id": None,
                                 "subscription_status": "trial"}), \
             patch(f"{_MAIN}._early_adopter_checkout_extras",
                   return_value=(41, [{"coupon": "EARLYBIRD"}])), \
             patch("cqc_lem.utilities.stripe_util.create_checkout_session",
                   return_value="https://checkout.stripe.com/x") as create:
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 200
        assert create.call_args.kwargs["trial_period_days"] == 41
        assert create.call_args.kwargs["discounts"] == [{"coupon": "EARLYBIRD"}]

    def test_standard_user_conversion_is_unchanged(self, client):
        with patch(f"{_MAIN}.get_session_user_id", return_value=7), \
             patch(f"{_MAIN}.get_user_subscription_info",
                   return_value={"stripe_customer_id": "cus_1", "stripe_subscription_id": None,
                                 "subscription_status": "trial"}), \
             patch(f"{_MAIN}.get_early_adopter_grant", return_value=None), \
             patch("cqc_lem.utilities.stripe_util.create_checkout_session",
                   return_value="https://checkout.stripe.com/x") as create:
            resp = client.post(self.BASE, json=self.PAYLOAD)
        assert resp.status_code == 200
        assert create.call_args.kwargs["trial_period_days"] is None
        assert create.call_args.kwargs["discounts"] is None
