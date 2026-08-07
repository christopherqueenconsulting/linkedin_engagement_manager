"""Unit tests for video credit Stripe packages + checkout."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def test_packages_present():
    from cqc_lem.utilities.stripe_util import VIDEO_CREDIT_PACKAGES
    assert set(VIDEO_CREDIT_PACKAGES) == {"small", "medium", "large", "max"}
    assert VIDEO_CREDIT_PACKAGES["medium"]["credits"] == 15


def test_creates_session_with_video_metadata():
    fake_session = MagicMock()
    fake_session.url = "https://stripe/checkout"
    stripe_mock = MagicMock()
    stripe_mock.checkout.Session.create.return_value = fake_session
    with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test"), \
         patch("cqc_lem.utilities.stripe_util._get_stripe", return_value=stripe_mock):
        from cqc_lem.utilities.stripe_util import create_video_credits_checkout
        url = create_video_credits_checkout("cus_1", "medium", "ok", "cancel")
    assert url == "https://stripe/checkout"
    kwargs = stripe_mock.checkout.Session.create.call_args[1]
    assert kwargs["metadata"]["type"] == "video_credits"
    assert kwargs["metadata"]["credits"] == "15"


def test_unknown_package_returns_none():
    with patch("cqc_lem.utilities.stripe_util.STRIPE_API_KEY", "sk_test"):
        from cqc_lem.utilities.stripe_util import create_video_credits_checkout
        assert create_video_credits_checkout("cus_1", "nope", "a", "b") is None
