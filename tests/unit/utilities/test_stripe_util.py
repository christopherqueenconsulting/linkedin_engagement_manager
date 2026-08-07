"""Unit tests for cqc_lem.utilities.stripe_util."""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stripe_mock(
    *,
    api_key_attr=None,
    customer_id="cus_abc123",
    checkout_url="https://checkout.stripe.com/pay/cs_test_abc",
    portal_url="https://billing.stripe.com/session/test_abc",
    subscription_id="sub_xyz789",
):
    """Return a MagicMock that mimics the stripe module surface used by stripe_util."""
    m = MagicMock()

    # Customer.create
    m.Customer.create.return_value = MagicMock(id=customer_id)

    # checkout.Session.create
    m.checkout.Session.create.return_value = MagicMock(url=checkout_url)

    # billing_portal.Session.create
    m.billing_portal.Session.create.return_value = MagicMock(url=portal_url)

    # Webhook helpers
    m.Webhook.construct_event.return_value = MagicMock()
    m.error.SignatureVerificationError = Exception  # use plain Exception as stand-in

    # Subscription.retrieve / modify
    m.Subscription.retrieve.return_value = {
        "id": subscription_id,
        "status": "active",
        "items": {"data": [{"id": "si_item1", "price": {"id": "price_starter"}}]},
        "current_period_end": 1700000000,
    }
    m.Subscription.modify.return_value = MagicMock()

    return m


# ---------------------------------------------------------------------------
# _get_stripe
# ---------------------------------------------------------------------------

class TestGetStripe:
    def test_sets_api_key_and_returns_stripe_module(self):
        """_get_stripe() must set stripe.api_key and return the stripe module."""
        import stripe as real_stripe

        from cqc_lem.utilities.stripe_util import _get_stripe

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_set_by_test"):
            result = _get_stripe()

        # Must return the actual stripe module (not a mock)
        assert result is real_stripe
        # Must have applied the key from env_constants to stripe.api_key
        assert real_stripe.api_key == "sk_test_set_by_test"

    def test_returns_stripe_module_even_without_key(self):
        """_get_stripe() succeeds even when STRIPE_API_KEY is empty."""
        import stripe as real_stripe

        from cqc_lem.utilities.stripe_util import _get_stripe

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", None):
            result = _get_stripe()

        assert result is real_stripe


# ---------------------------------------------------------------------------
# create_stripe_customer
# ---------------------------------------------------------------------------

class TestCreateStripeCustomer:
    def test_success_returns_customer_id(self):
        mock_stripe = _make_stripe_mock(customer_id="cus_new123")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_stripe_customer
            result = create_stripe_customer("user@example.com", 42)

        assert result == "cus_new123"
        mock_stripe.Customer.create.assert_called_once_with(
            email="user@example.com",
            metadata={"user_id": "42"},
        )

    def test_missing_api_key_returns_none(self):
        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", None):
            from cqc_lem.utilities.stripe_util import create_stripe_customer
            result = create_stripe_customer("user@example.com", 42)

        assert result is None

    def test_stripe_exception_returns_none(self):
        mock_stripe = _make_stripe_mock()
        mock_stripe.Customer.create.side_effect = Exception("Network error")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_stripe_customer
            result = create_stripe_customer("user@example.com", 99)

        assert result is None


# ---------------------------------------------------------------------------
# create_checkout_session
# ---------------------------------------------------------------------------

class TestCreateCheckoutSession:
    def test_success_returns_url(self):
        mock_stripe = _make_stripe_mock(checkout_url="https://checkout.stripe.com/pay/cs_abc")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_checkout_session
            result = create_checkout_session(
                "cus_abc", "starter", "https://example.com/success", "https://example.com/cancel"
            )

        assert result == "https://checkout.stripe.com/pay/cs_abc"

    def test_no_trial_or_discount_keeps_payload_unchanged(self):
        """Standard conversion must not gain subscription_data/discounts (issue #499)."""
        mock_stripe = _make_stripe_mock()

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_checkout_session
            create_checkout_session("cus_abc", "starter", "https://e.com/s", "https://e.com/c")

        kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
        assert "subscription_data" not in kwargs
        assert "discounts" not in kwargs

    def test_trial_days_and_coupon_are_forwarded(self):
        """An early-adopter extension mirrors into Stripe as a trial + coupon (issue #499)."""
        mock_stripe = _make_stripe_mock()

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_checkout_session
            create_checkout_session("cus_abc", "starter", "https://e.com/s", "https://e.com/c",
                                    trial_period_days=47, discounts=[{"coupon": "EARLYBIRD"}])

        kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
        assert kwargs["subscription_data"] == {"trial_period_days": 47}
        assert kwargs["discounts"] == [{"coupon": "EARLYBIRD"}]

    def test_trial_days_capped_at_stripe_maximum(self):
        mock_stripe = _make_stripe_mock()

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import STRIPE_MAX_TRIAL_DAYS, create_checkout_session
            create_checkout_session("cus_abc", "starter", "https://e.com/s", "https://e.com/c",
                                    trial_period_days=5000)

        kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
        assert kwargs["subscription_data"]["trial_period_days"] == STRIPE_MAX_TRIAL_DAYS

    def test_non_positive_trial_days_is_ignored(self):
        mock_stripe = _make_stripe_mock()

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_checkout_session
            create_checkout_session("cus_abc", "starter", "https://e.com/s", "https://e.com/c",
                                    trial_period_days=0)

        assert "subscription_data" not in mock_stripe.checkout.Session.create.call_args.kwargs

    def test_missing_price_id_for_tier_returns_none(self):
        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": None}):
            from cqc_lem.utilities.stripe_util import create_checkout_session
            result = create_checkout_session(
                "cus_abc", "starter", "https://example.com/success", "https://example.com/cancel"
            )

        assert result is None

    def test_unknown_tier_returns_none(self):
        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {}):
            from cqc_lem.utilities.stripe_util import create_checkout_session
            result = create_checkout_session(
                "cus_abc", "nonexistent_tier", "https://example.com/s", "https://example.com/c"
            )

        assert result is None

    def test_missing_api_key_returns_none(self):
        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", None), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}):
            from cqc_lem.utilities.stripe_util import create_checkout_session
            result = create_checkout_session(
                "cus_abc", "starter", "https://example.com/s", "https://example.com/c"
            )

        assert result is None

    def test_stripe_exception_returns_none(self):
        mock_stripe = _make_stripe_mock()
        mock_stripe.checkout.Session.create.side_effect = Exception("Stripe down")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_checkout_session
            result = create_checkout_session(
                "cus_abc", "starter", "https://example.com/s", "https://example.com/c"
            )

        assert result is None

    def test_correct_line_items_passed(self):
        mock_stripe = _make_stripe_mock()

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"professional": "price_pro_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_checkout_session
            create_checkout_session(
                "cus_xyz", "professional", "https://example.com/s", "https://example.com/c"
            )

        call_kwargs = mock_stripe.checkout.Session.create.call_args[1]
        assert call_kwargs["line_items"] == [{"price": "price_pro_id", "quantity": 1}]
        assert call_kwargs["mode"] == "subscription"


# ---------------------------------------------------------------------------
# create_portal_session
# ---------------------------------------------------------------------------

class TestCreatePortalSession:
    def test_success_returns_url(self):
        mock_stripe = _make_stripe_mock(portal_url="https://billing.stripe.com/p/session_abc")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_portal_session
            result = create_portal_session("cus_abc", "https://example.com/return")

        assert result == "https://billing.stripe.com/p/session_abc"
        mock_stripe.billing_portal.Session.create.assert_called_once_with(
            customer="cus_abc",
            return_url="https://example.com/return",
        )

    def test_missing_api_key_returns_none(self):
        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", None):
            from cqc_lem.utilities.stripe_util import create_portal_session
            result = create_portal_session("cus_abc", "https://example.com/return")

        assert result is None

    def test_stripe_exception_returns_none(self):
        mock_stripe = _make_stripe_mock()
        mock_stripe.billing_portal.Session.create.side_effect = Exception("Portal unavailable")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import create_portal_session
            result = create_portal_session("cus_abc", "https://example.com/return")

        assert result is None


# ---------------------------------------------------------------------------
# validate_webhook
# ---------------------------------------------------------------------------

class TestValidateWebhook:
    def test_missing_webhook_secret_returns_none(self):
        with patch("cqc_lem.utilities.stripe_util.STRIPE_WEBHOOK_SECRET", None):
            from cqc_lem.utilities.stripe_util import validate_webhook
            result = validate_webhook(b'{"type":"payment_intent.succeeded"}', "t=123,v1=abc")

        assert result is None

    def test_valid_signature_returns_plain_dict(self):
        payload = json.dumps({"type": "customer.subscription.updated", "id": "evt_1"}).encode()
        mock_stripe = _make_stripe_mock()
        # construct_event succeeds (no exception)
        mock_stripe.Webhook.construct_event.return_value = MagicMock()

        with patch("cqc_lem.utilities.stripe_util.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import validate_webhook
            result = validate_webhook(payload, "t=123,v1=sig")

        assert result is not None
        assert isinstance(result, dict)
        assert result["type"] == "customer.subscription.updated"
        assert result["id"] == "evt_1"

    def test_returned_dict_supports_get_access(self):
        payload = json.dumps({"type": "invoice.paid", "data": {"object": {"id": "in_1"}}}).encode()
        mock_stripe = _make_stripe_mock()
        mock_stripe.Webhook.construct_event.return_value = MagicMock()

        with patch("cqc_lem.utilities.stripe_util.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import validate_webhook
            result = validate_webhook(payload, "t=123,v1=sig")

        # Verify it is a plain Python dict, not a StripeObject
        assert type(result) is dict
        assert result.get("type") == "invoice.paid"

    def test_invalid_signature_returns_none(self):
        payload = b'{"type":"payment_intent.succeeded"}'
        mock_stripe = _make_stripe_mock()
        # Make SignatureVerificationError a unique class so we can raise it distinctly
        class FakeSignatureVerificationError(Exception):
            pass
        mock_stripe.error.SignatureVerificationError = FakeSignatureVerificationError
        mock_stripe.Webhook.construct_event.side_effect = FakeSignatureVerificationError("Invalid sig")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import validate_webhook
            result = validate_webhook(payload, "bad_sig_header")

        assert result is None

    def test_generic_exception_returns_none(self):
        payload = b'{"type":"test"}'
        mock_stripe = _make_stripe_mock()
        mock_stripe.Webhook.construct_event.side_effect = Exception("Unexpected error")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import validate_webhook
            result = validate_webhook(payload, "t=123,v1=abc")

        assert result is None


# ---------------------------------------------------------------------------
# upgrade_subscription
# ---------------------------------------------------------------------------

class TestUpgradeSubscription:
    def test_success_returns_true(self):
        mock_stripe = _make_stripe_mock(subscription_id="sub_abc")
        # Subscription.retrieve returns a dict with items
        mock_stripe.Subscription.retrieve.return_value = {
            "id": "sub_abc",
            "items": {"data": [{"id": "si_item1"}]},
        }

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import upgrade_subscription
            result = upgrade_subscription("sub_abc", "starter")

        assert result is True

    def test_calls_modify_with_correct_item_id_and_price_id(self):
        mock_stripe = _make_stripe_mock()
        mock_stripe.Subscription.retrieve.return_value = {
            "id": "sub_abc",
            "items": {"data": [{"id": "si_item_correct"}]},
        }

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"professional": "price_pro_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import upgrade_subscription
            upgrade_subscription("sub_abc", "professional")

        mock_stripe.Subscription.modify.assert_called_once_with(
            "sub_abc",
            items=[{"id": "si_item_correct", "price": "price_pro_id"}],
            proration_behavior="create_prorations",
        )

    def test_missing_price_id_for_tier_returns_false(self):
        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": None}):
            from cqc_lem.utilities.stripe_util import upgrade_subscription
            result = upgrade_subscription("sub_abc", "starter")

        assert result is False

    def test_missing_api_key_returns_false(self):
        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", None), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}):
            from cqc_lem.utilities.stripe_util import upgrade_subscription
            result = upgrade_subscription("sub_abc", "starter")

        assert result is False

    def test_stripe_exception_returns_false(self):
        mock_stripe = _make_stripe_mock()
        mock_stripe.Subscription.retrieve.side_effect = Exception("API error")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import upgrade_subscription
            result = upgrade_subscription("sub_abc", "starter")

        assert result is False

    def test_empty_line_items_returns_false(self):
        mock_stripe = _make_stripe_mock()
        mock_stripe.Subscription.retrieve.return_value = {
            "id": "sub_abc",
            "items": {"data": []},
        }

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {"starter": "price_starter_id"}), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import upgrade_subscription
            result = upgrade_subscription("sub_abc", "starter")

        assert result is False


# ---------------------------------------------------------------------------
# get_subscription_tier_from_price
# ---------------------------------------------------------------------------

class TestGetSubscriptionTierFromPrice:
    def test_known_price_returns_tier_name(self):
        with patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {
            "starter": "price_starter_id",
            "professional": "price_pro_id",
            "enterprise": "price_ent_id",
        }):
            from cqc_lem.utilities.stripe_util import get_subscription_tier_from_price
            assert get_subscription_tier_from_price("price_starter_id") == "starter"
            assert get_subscription_tier_from_price("price_pro_id") == "professional"
            assert get_subscription_tier_from_price("price_ent_id") == "enterprise"

    def test_unknown_price_returns_none(self):
        with patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {
            "starter": "price_starter_id",
        }):
            from cqc_lem.utilities.stripe_util import get_subscription_tier_from_price
            result = get_subscription_tier_from_price("price_unknown")
            assert result is None

    def test_none_price_returns_none(self):
        with patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {
            "starter": "price_starter_id",
        }):
            from cqc_lem.utilities.stripe_util import get_subscription_tier_from_price
            result = get_subscription_tier_from_price(None)
            assert result is None

    def test_tier_with_none_price_id_is_skipped(self):
        """A tier whose price ID is None should never match any input."""
        with patch("cqc_lem.utilities.stripe_util.TIER_PRICE_MAP", {
            "starter": None,
            "professional": "price_pro_id",
        }):
            from cqc_lem.utilities.stripe_util import get_subscription_tier_from_price
            # None in the map should not match None input
            result = get_subscription_tier_from_price(None)
            assert result is None


# ---------------------------------------------------------------------------
# stripe_status_to_db
# ---------------------------------------------------------------------------

class TestStripeStatusToDb:
    @pytest.mark.parametrize("stripe_status,expected_db_status", [
        ("active", "active"),
        ("trialing", "trial"),
        ("past_due", "past_due"),
        ("unpaid", "past_due"),
        ("canceled", "cancelled"),
        ("incomplete", "inactive"),
        ("incomplete_expired", "inactive"),
        ("paused", "inactive"),
    ])
    def test_known_statuses_map_correctly(self, stripe_status, expected_db_status):
        from cqc_lem.utilities.stripe_util import stripe_status_to_db
        assert stripe_status_to_db(stripe_status) == expected_db_status

    def test_unknown_string_maps_to_inactive(self):
        from cqc_lem.utilities.stripe_util import stripe_status_to_db
        assert stripe_status_to_db("some_future_status") == "inactive"
        assert stripe_status_to_db("") == "inactive"
        assert stripe_status_to_db("ACTIVE") == "inactive"  # case-sensitive


# ---------------------------------------------------------------------------
# fetch_subscription
# ---------------------------------------------------------------------------

class TestFetchSubscription:
    def test_success_returns_subscription_object(self):
        expected_sub = {"id": "sub_abc", "status": "active"}
        mock_stripe = _make_stripe_mock()
        mock_stripe.Subscription.retrieve.return_value = expected_sub

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import fetch_subscription
            result = fetch_subscription("sub_abc")

        assert result == expected_sub
        mock_stripe.Subscription.retrieve.assert_called_once_with("sub_abc")

    def test_missing_api_key_returns_none(self):
        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", None):
            from cqc_lem.utilities.stripe_util import fetch_subscription
            result = fetch_subscription("sub_abc")

        assert result is None

    def test_stripe_exception_returns_none(self):
        mock_stripe = _make_stripe_mock()
        mock_stripe.Subscription.retrieve.side_effect = Exception("Not found")

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import fetch_subscription
            result = fetch_subscription("sub_missing")

        assert result is None

    def test_stripe_object_non_dict_is_converted_to_dict(self):
        """Stripe SDK v5+ returns StripeObject, not dict; fetch_subscription must convert it."""

        class _FakeStripeObject:
            """Minimal stand-in for stripe.StripeObject (not a dict subclass)."""
            def __str__(self):
                return json.dumps({"id": "sub_xyz", "status": "active", "items": {}})

        mock_stripe = _make_stripe_mock()
        mock_stripe.Subscription.retrieve.return_value = _FakeStripeObject()

        with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test_key"), \
             patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=mock_stripe):
            from cqc_lem.utilities.stripe_util import fetch_subscription
            result = fetch_subscription("sub_xyz")

        assert isinstance(result, dict)
        assert result["id"] == "sub_xyz"
        assert result["status"] == "active"
