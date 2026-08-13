"""Coverage tests for stripe_util credit checkouts and payment-intent lookups.

Every guard here answers the same way — None, and no Stripe call charged — so they are
one parametrized table (issue #1216) rather than the same test written per package type.
The success paths stay plain: each asserts a different payload.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _stripe(create_exc=None, list_return=None, list_exc=None):
    stripe = MagicMock()
    if create_exc is not None:
        stripe.checkout.Session.create.side_effect = create_exc
    if list_exc is not None:
        stripe.checkout.Session.list.side_effect = list_exc
    elif list_return is not None:
        stripe.checkout.Session.list.return_value = list_return
    return stripe


def _first_video_package():
    import cqc_lem.utilities.stripe_util as su
    return next(iter(su.VIDEO_CREDIT_PACKAGES))


# (case id, function name, package name or None, STRIPE_API_KEY, stripe kwargs)
_NONE_GUARDS = [
    ("avatar_unknown_package", "create_avatar_credits_checkout", "nope", "sk_test", {}),
    ("avatar_no_api_key", "create_avatar_credits_checkout", "starter", "", {}),
    ("avatar_stripe_error", "create_avatar_credits_checkout", "starter", "sk_test",
     {"create_exc": RuntimeError("card declined")}),
    ("video_unknown_package", "create_video_credits_checkout", "nope", "sk_test", {}),
    ("video_no_api_key", "create_video_credits_checkout", "<first>", "", {}),
    ("video_stripe_error", "create_video_credits_checkout", "<first>", "sk_test",
     {"create_exc": RuntimeError("nope")}),
    ("session_lookup_no_api_key", "get_checkout_session_by_payment_intent", None, "", {}),
    ("session_lookup_empty_list", "get_checkout_session_by_payment_intent", None, "sk_test",
     {"list_return": {"data": []}}),
    ("session_lookup_api_error", "get_checkout_session_by_payment_intent", None, "sk_test",
     {"list_exc": RuntimeError("api down")}),
]


class TestNoneGuards:
    @pytest.mark.parametrize("case_id,fname,package,api_key,stripe_kwargs",
                             _NONE_GUARDS, ids=[c[0] for c in _NONE_GUARDS])
    def test_returns_none_without_charging_anything(self, case_id, fname, package, api_key,
                                                    stripe_kwargs):
        import cqc_lem.utilities.stripe_util as su
        if package == "<first>":
            package = _first_video_package()
        args = ("pi_1",) if package is None else ("cus_1", package, "s", "c")
        stripe = _stripe(**stripe_kwargs)
        with patch.object(su, "STRIPE_API_KEY", api_key), \
             patch.object(su, "_get_stripe", return_value=stripe):
            assert getattr(su, fname)(*args) is None
        # A guard that refused BEFORE reaching Stripe must not have reached it — otherwise a
        # checkout for an unpriced package would still read as a clean None. The two
        # `*_stripe_error` cases and the two session lookups with a key DID reach Stripe; they
        # are the ones whose kwargs configure the call.
        if not stripe_kwargs:
            call = (stripe.checkout.Session.list if package is None
                    else stripe.checkout.Session.create)
            call.assert_not_called()


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


class TestVideoCreditsCheckout:
    def test_creates_session_with_video_metadata(self):
        import cqc_lem.utilities.stripe_util as su
        pkg = _first_video_package()
        stripe = MagicMock()
        stripe.checkout.Session.create.return_value = MagicMock(
            url="https://checkout.stripe.com/v")
        with patch.object(su, "STRIPE_API_KEY", "sk_test"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            url = su.create_video_credits_checkout("cus_1", pkg, "s", "c")
        assert url == "https://checkout.stripe.com/v"
        meta = stripe.checkout.Session.create.call_args[1]["metadata"]
        assert meta["type"] == "video_credits" and meta["package"] == pkg


class TestGetCheckoutSessionByPaymentIntent:
    def test_returns_first_session(self):
        import cqc_lem.utilities.stripe_util as su
        stripe = _stripe(list_return={
            "data": [{"id": "cs_1", "metadata": {"type": "avatar_credits"}}]})
        with patch.object(su, "STRIPE_API_KEY", "sk_test"), \
             patch.object(su, "_get_stripe", return_value=stripe):
            session = su.get_checkout_session_by_payment_intent("pi_1")
        assert session["id"] == "cs_1"
        stripe.checkout.Session.list.assert_called_once_with(
            payment_intent="pi_1", limit=1)
