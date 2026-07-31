"""Integration tests for the avatar endpoints."""

import io
import zipfile
import pytest
from unittest.mock import patch


SESSION = "test-session-token"
USER_ID = 42


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("photo_1.jpg", b"fake-image-data")
    return buf.getvalue()


def _client():
    from fastapi.testclient import TestClient
    from cqc_lem.api.main import app
    return TestClient(app)


@pytest.mark.integration
class TestAvatarCreditsEndpoint:
    def test_get_credits_returns_balance_and_active_avatar(self):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_avatar_credit_balance", return_value=3), \
             patch("cqc_lem.api.main.get_active_avatar", return_value=None):
            r = _client().get("/api/avatar/credits", params={"session_token": SESSION})

        assert r.status_code == 200
        detail = r.json()["detail"]
        assert detail["balance"] == 3
        assert detail["active_avatar"] is None

    def test_get_credits_returns_401_for_invalid_session(self):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = _client().get("/api/avatar/credits", params={"session_token": "bad"})

        assert r.status_code == 401

    def test_get_credits_includes_active_avatar_when_set(self):
        active = {
            "id": 1, "training_id": "train-1", "model_ref": "user/model:v1",
            "trigger_word": "LEMAVTR42", "status": "succeeded",
        }
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_avatar_credit_balance", return_value=2), \
             patch("cqc_lem.api.main.get_active_avatar", return_value=active):
            r = _client().get("/api/avatar/credits", params={"session_token": SESSION})

        assert r.status_code == 200
        assert r.json()["detail"]["active_avatar"]["trigger_word"] == "LEMAVTR42"


@pytest.mark.integration
class TestAvatarCreditCheckoutEndpoint:
    def test_returns_checkout_url_for_valid_package(self):
        subscription = {"stripe_customer_id": "cus_test123"}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_user_subscription_info", return_value=subscription), \
             patch(
                 "cqc_lem.utilities.stripe_util.create_avatar_credits_checkout",
                 return_value="https://checkout.stripe.com/pay/test",
             ):
            r = _client().post("/api/avatar/credits/checkout", json={
                "session_token": SESSION,
                "package": "value",
                "success_url": "http://localhost/avatars?credits=purchased",
                "cancel_url": "http://localhost/avatars",
            })

        assert r.status_code == 200
        assert r.json()["detail"]["checkout_url"].startswith("https://checkout.stripe.com/")

    def test_returns_400_for_unknown_package(self):
        subscription = {"stripe_customer_id": "cus_test123"}
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_user_subscription_info", return_value=subscription):
            r = _client().post("/api/avatar/credits/checkout", json={
                "session_token": SESSION,
                "package": "notapackage",
                "success_url": "http://localhost/avatars",
                "cancel_url": "http://localhost/avatars",
            })

        assert r.status_code == 400

    def test_returns_401_for_invalid_session(self):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = _client().post("/api/avatar/credits/checkout", json={
                "session_token": "bad",
                "package": "starter",
                "success_url": "http://localhost/avatars",
                "cancel_url": "http://localhost/avatars",
            })

        assert r.status_code == 401


@pytest.mark.integration
class TestAvatarTrainingEndpoint:
    def test_returns_402_when_no_credits(self):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_avatar_credit_balance", return_value=0):
            r = _client().post(
                "/api/avatar/training",
                data={"session_token": SESSION, "trigger_word": "LEMAVTR42"},
                files={"photos": ("photos.zip", _make_zip(), "application/zip")},
            )

        assert r.status_code == 402
        assert "credits" in r.json()["detail"].lower()

    def test_returns_401_for_invalid_session(self):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = _client().post(
                "/api/avatar/training",
                data={"session_token": "bad", "trigger_word": "TOK"},
                files={"photos": ("photos.zip", _make_zip(), "application/zip")},
            )

        assert r.status_code == 401

    def test_returns_200_and_deducts_credit_when_training_starts(self):
        mock_training_id = "train-success-001"
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_avatar_credit_balance", return_value=2), \
             patch(
                 "cqc_lem.utilities.avatar.replicate_avatar.start_avatar_training",
                 return_value=mock_training_id,
             ), \
             patch("cqc_lem.api.main.deduct_avatar_credit", return_value=True) as mock_deduct, \
             patch("cqc_lem.api.main.insert_avatar_training", return_value=5):
            r = _client().post(
                "/api/avatar/training",
                data={"session_token": SESSION, "trigger_word": "LEMAVTR42"},
                files={"photos": ("photos.zip", _make_zip(), "application/zip")},
            )

        assert r.status_code == 200
        assert r.json()["detail"]["training_id"] == mock_training_id
        mock_deduct.assert_called_once_with(USER_ID, mock_training_id)


@pytest.mark.integration
class TestListAvatarTrainings:
    def test_returns_empty_list_when_no_trainings(self):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_avatar_trainings", return_value=[]):
            r = _client().get("/api/avatar/trainings", params={"session_token": SESSION})

        assert r.status_code == 200
        assert r.json()["detail"] == []

    def test_returns_trainings_list(self):
        trainings = [
            {
                "id": 1, "training_id": "train-1", "model_ref": None,
                "trigger_word": "LEMAVTR42", "status": "processing",
                "is_active": False, "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        ]
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_avatar_trainings", return_value=trainings):
            r = _client().get("/api/avatar/trainings", params={"session_token": SESSION})

        assert r.status_code == 200
        assert len(r.json()["detail"]) == 1
        assert r.json()["detail"][0]["trigger_word"] == "LEMAVTR42"


@pytest.mark.integration
class TestActivateAvatar:
    def test_returns_400_for_non_succeeded_training(self):
        avatar = {
            "id": 1, "training_id": "train-1", "model_ref": None,
            "trigger_word": "TOK", "status": "processing", "approval_status": "pending",
            "is_active": False, "created_at": None, "updated_at": None,
        }
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_avatar_training", return_value=avatar):
            r = _client().put("/api/avatar/training/1/activate", json={"session_token": SESSION})

        assert r.status_code == 400

    def test_returns_200_for_approved_succeeded_training(self):
        avatar = {
            "id": 2, "training_id": "train-2", "model_ref": "user/model:v1",
            "trigger_word": "TOK", "status": "succeeded", "approval_status": "approved",
            "is_active": False, "created_at": None, "updated_at": None,
        }
        with patch("cqc_lem.api.main.get_session_user_id", return_value=USER_ID), \
             patch("cqc_lem.api.main.get_avatar_training", return_value=avatar), \
             patch("cqc_lem.api.main.set_active_avatar", return_value=True):
            r = _client().put("/api/avatar/training/2/activate", json={"session_token": SESSION})

        assert r.status_code == 200


@pytest.mark.integration
class TestStripeWebhookAvatarCredits:
    def test_adds_credits_on_checkout_session_completed(self):
        user_row = {"id": USER_ID, "stripe_customer_id": "cus_test"}
        with patch("cqc_lem.utilities.stripe_util.validate_webhook", return_value={
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer": "cus_test",
                    "id": "cs_test_session",
                    "payment_status": "paid",
                    "metadata": {
                        "type": "avatar_credits",
                        "package": "value",
                        "credits": "3",
                    },
                }
            },
        }), patch("cqc_lem.api.main.get_user_by_stripe_customer_id", return_value=user_row), \
             patch("cqc_lem.api.main.get_avatar_credit_ledger_entry_by_session", return_value=None), \
             patch("cqc_lem.api.main.add_avatar_credits", return_value=True) as mock_add:
            r = _client().post(
                "/api/billing/webhook",
                content=b'{}',
                headers={"Stripe-Signature": "t=1,v1=sig"},
            )

        assert r.status_code == 200
        mock_add.assert_called_once_with(USER_ID, 3, "purchase_value", "cs_test_session")
