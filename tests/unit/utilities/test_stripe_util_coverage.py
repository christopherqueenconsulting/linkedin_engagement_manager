"""Coverage tests for stripe_util credit checkouts and payment-intent lookups."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_S = "cqc_lem.utilities.stripe_util"  # lgtm[py/unused-global-variable]


class TestValidateWebhookGenericError:
    def test_non_signature_exception_returns_none(self):
        import cqc_lem.utilities.stripe_util as su
        stripe = MagicMock()
        stripe.error.SignatureVerificationError = type(
            "SigErr", (Exception,), {})
        stripe.Webhook.construct_event.side_effect = RuntimeError("boom")
        with patch.object(su, "STRIPE_WEBHOOK_SECRET", "whsec"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            assert su.validate_webhook(b"{}", "sig") is None


class TestAvatarCreditsCheckout:
    def test_unknown_package_returns_none(self):
        from cqc_lem.utilities.stripe_util import create_avatar_credits_checkout
        assert create_avatar_credits_checkout("cus_1", "nope", "s", "c") is None

    def test_no_api_key_returns_none(self):
        import cqc_lem.utilities.stripe_util as su
        with patch.object(su, "STRIPE_API_KEY", ""):
            assert su.create_avatar_credits_checkout("cus_1", "starter", "s", "c") is None

    def test_creates_session_with_package_metadata(self):
        import cqc_lem.utilities.stripe_util as su
        stripe = MagicMock()
        stripe.checkout.Session.create.return_value = MagicMock(
            url="https://checkout.stripe.com/x")
        with patch.object(su, "STRIPE_API_KEY", "sk_test"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            url = su.create_avatar_credits_checkout(
                "cus_1", "value", "https://x/ok", "https://x/no")
        assert url == "https://checkout.stripe.com/x"
        kwargs = stripe.checkout.Session.create.call_args[1]
        assert kwargs["customer"] == "cus_1"
        assert kwargs["mode"] == "payment"
        assert kwargs["metadata"] == {"type": "avatar_credits", "package": "value",
                                      "credits": "3"}
        assert kwargs["line_items"][0]["price_data"]["unit_amount"] == \
            su.AVATAR_CREDIT_PACKAGES["value"]["amount_cents"]

    def test_stripe_error_returns_none(self):
        import cqc_lem.utilities.stripe_util as su
        stripe = MagicMock()
        stripe.checkout.Session.create.side_effect = RuntimeError("card declined")
        with patch.object(su, "STRIPE_API_KEY", "sk_test"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            assert su.create_avatar_credits_checkout("cus_1", "starter", "s", "c") is None


class TestVideoCreditsCheckout:
    def test_unknown_package_returns_none(self):
        from cqc_lem.utilities.stripe_util import create_video_credits_checkout
        assert create_video_credits_checkout("cus_1", "nope", "s", "c") is None

    def test_no_api_key_returns_none(self):
        import cqc_lem.utilities.stripe_util as su
        pkg = next(iter(su.VIDEO_CREDIT_PACKAGES))
        with patch.object(su, "STRIPE_API_KEY", ""):
            assert su.create_video_credits_checkout("cus_1", pkg, "s", "c") is None

    def test_creates_session_with_video_metadata(self):
        import cqc_lem.utilities.stripe_util as su
        pkg = next(iter(su.VIDEO_CREDIT_PACKAGES))
        stripe = MagicMock()
        stripe.checkout.Session.create.return_value = MagicMock(
            url="https://checkout.stripe.com/v")
        with patch.object(su, "STRIPE_API_KEY", "sk_test"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            url = su.create_video_credits_checkout("cus_1", pkg, "s", "c")
        assert url == "https://checkout.stripe.com/v"
        meta = stripe.checkout.Session.create.call_args[1]["metadata"]
        assert meta["type"] == "video_credits" and meta["package"] == pkg

    def test_stripe_error_returns_none(self):
        import cqc_lem.utilities.stripe_util as su
        pkg = next(iter(su.VIDEO_CREDIT_PACKAGES))
        stripe = MagicMock()
        stripe.checkout.Session.create.side_effect = RuntimeError("nope")
        with patch.object(su, "STRIPE_API_KEY", "sk_test"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            assert su.create_video_credits_checkout("cus_1", pkg, "s", "c") is None


class TestGetCheckoutSessionByPaymentIntent:
    def test_no_api_key_returns_none(self):
        import cqc_lem.utilities.stripe_util as su
        with patch.object(su, "STRIPE_API_KEY", ""):
            assert su.get_checkout_session_by_payment_intent("pi_1") is None

    def test_returns_first_session(self):
        import cqc_lem.utilities.stripe_util as su
        stripe = MagicMock()
        stripe.checkout.Session.list.return_value = {
            "data": [{"id": "cs_1", "metadata": {"type": "avatar_credits"}}]}
        with patch.object(su, "STRIPE_API_KEY", "sk_test"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            session = su.get_checkout_session_by_payment_intent("pi_1")
        assert session["id"] == "cs_1"
        stripe.checkout.Session.list.assert_called_once_with(
            payment_intent="pi_1", limit=1)

    def test_empty_list_returns_none(self):
        import cqc_lem.utilities.stripe_util as su
        stripe = MagicMock()
        stripe.checkout.Session.list.return_value = {"data": []}
        with patch.object(su, "STRIPE_API_KEY", "sk_test"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            assert su.get_checkout_session_by_payment_intent("pi_1") is None

    def test_api_error_returns_none(self):
        import cqc_lem.utilities.stripe_util as su
        stripe = MagicMock()
        stripe.checkout.Session.list.side_effect = RuntimeError("api down")
        with patch.object(su, "STRIPE_API_KEY", "sk_test"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            assert su.get_checkout_session_by_payment_intent("pi_1") is None
