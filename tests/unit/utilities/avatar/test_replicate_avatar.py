"""Unit tests for the avatar Replicate utility."""

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_replicate():
    with patch("cqc_lem.utilities.avatar.replicate_avatar.replicate") as m:
        training = MagicMock()
        training.id = "train-abc123"
        training.status = "starting"
        training.output = None
        m.trainings.create.return_value = training
        m.trainings.get.return_value = training

        uploaded_file = MagicMock()
        uploaded_file.urls = {"get": "https://replicate.delivery/files/photos.zip"}
        m.files.create.return_value = uploaded_file

        yield m


@pytest.fixture
def mock_replicate_username():
    with patch(
        "cqc_lem.utilities.avatar.replicate_avatar.REPLICATE_USERNAME",
        "testuser",
    ):
        yield


@pytest.mark.unit
class TestStartAvatarTraining:
    def test_calls_replicate_trainings_create(self, mock_replicate, mock_replicate_username):
        from cqc_lem.utilities.avatar.replicate_avatar import start_avatar_training

        training_id = start_avatar_training(42, b"PK\x03\x04fakezip", "LEMAVTR42")

        assert mock_replicate.trainings.create.called
        assert training_id == "train-abc123"

    def test_destination_includes_username_and_user_id(self, mock_replicate, mock_replicate_username):
        from cqc_lem.utilities.avatar.replicate_avatar import start_avatar_training

        start_avatar_training(7, b"PK\x03\x04fakezip", "MYAVATAR")

        call_kwargs = mock_replicate.trainings.create.call_args
        destination = call_kwargs.kwargs.get("destination") or call_kwargs[1].get("destination")
        assert destination is not None
        assert "testuser" in destination
        assert "7" in destination

    def test_raises_when_replicate_username_not_set(self, mock_replicate):
        with patch("cqc_lem.utilities.avatar.replicate_avatar.REPLICATE_USERNAME", ""):
            from cqc_lem.utilities.avatar.replicate_avatar import start_avatar_training
            with pytest.raises(ValueError, match="REPLICATE_USERNAME"):
                start_avatar_training(1, b"PK\x03\x04fakezip", "TOK")

    def test_input_images_is_uploaded_file_url(self, mock_replicate, mock_replicate_username):
        from cqc_lem.utilities.avatar.replicate_avatar import start_avatar_training

        start_avatar_training(1, b"PK\x03\x04fakezip", "TOK")

        # ZIP must be uploaded via replicate.files.create, not embedded as base64
        mock_replicate.files.create.assert_called_once()
        call_kwargs = mock_replicate.trainings.create.call_args
        input_data = call_kwargs.kwargs.get("input") or call_kwargs[1].get("input")
        assert input_data["input_images"] == "https://replicate.delivery/files/photos.zip"
        assert input_data["trigger_word"] == "TOK"


@pytest.mark.unit
class TestPollTrainingStatus:
    def test_returns_starting_status(self, mock_replicate):
        mock_replicate.trainings.get.return_value.status = "starting"
        mock_replicate.trainings.get.return_value.output = None

        from cqc_lem.utilities.avatar.replicate_avatar import poll_training_status

        status, model_ref = poll_training_status("train-abc123")

        assert status == "starting"
        assert model_ref is None

    def test_returns_succeeded_with_model_ref(self, mock_replicate):
        mock_replicate.trainings.get.return_value.status = "succeeded"
        mock_replicate.trainings.get.return_value.output = {"version": "testuser/mymodel:abc123"}

        from cqc_lem.utilities.avatar.replicate_avatar import poll_training_status

        status, model_ref = poll_training_status("train-abc123")

        assert status == "succeeded"
        assert model_ref == "testuser/mymodel:abc123"

    def test_returns_processing_on_exception(self, mock_replicate):
        mock_replicate.trainings.get.side_effect = RuntimeError("network error")

        from cqc_lem.utilities.avatar.replicate_avatar import poll_training_status

        status, model_ref = poll_training_status("train-abc123")

        assert status == "processing"
        assert model_ref is None


@pytest.mark.unit
class TestGenerateImageWithAvatar:
    def test_delegates_to_flux_helper(self):
        # Patch the function at its source module — lazy imports in the function body
        # will pick up the patched version when the module attribute is replaced.
        with patch(
            "cqc_lem.utilities.ai.ai_helper.get_flux_image_via_replicate",
            return_value="/assets/images/replicate/test/out.webp",
        ) as mock_flux:
            from cqc_lem.utilities.avatar.replicate_avatar import generate_image_with_avatar

            result = generate_image_with_avatar("a portrait photo", "testuser/model:v1",
                                                ratio="9:16")

            mock_flux.assert_called_once_with("a portrait photo", ref="testuser/model:v1",
                                              aspect_ratio="9:16")
            assert result == ("/assets/images/replicate/test/out.webp", True)

    def test_falls_back_to_base_flux_on_error(self):
        with patch(
            "cqc_lem.utilities.ai.ai_helper.get_flux_image_via_replicate",
            side_effect=RuntimeError("inference failed"),
        ), patch(
            "cqc_lem.utilities.ai.ai_helper.generate_flux1_image_from_prompt",
            return_value="/assets/images/replicate/fallback/out.webp",
        ) as mock_fallback:
            from cqc_lem.utilities.avatar.replicate_avatar import generate_image_with_avatar

            result = generate_image_with_avatar("LEMAVTR42, a portrait photo",
                                                "testuser/model:v1",
                                                fallback_prompt="a portrait photo")

            # The base model gets the CLEAN prompt — the LoRA trigger word is a nonsense token
            # to it — and used_avatar is False so the caller can't claim avatar provenance.
            mock_fallback.assert_called_once_with("a portrait photo", ratio="1:1")
            assert result == ("/assets/images/replicate/fallback/out.webp", False)


@pytest.mark.unit
class TestGeneratePostImage:
    def test_uses_avatar_when_active_and_succeeded(self):
        active_avatar = {
            "id": 1,
            "training_id": "train-1",
            "model_ref": "testuser/model:v1",
            "trigger_word": "LEMAVTR42",
            "status": "succeeded",
        }
        # Patch generate_image_with_avatar at its source module so the lazy import
        # inside generate_post_image picks up the mock.
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=active_avatar), \
             patch(
                 "cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                 return_value=("/avatar/image.webp", True),
             ) as mock_gen, \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"):
            from cqc_lem.utilities.ai.ai_helper import generate_post_image

            result = generate_post_image("professional headshot", 42)

            assert result == "/avatar/image.webp"
            called_prompt = mock_gen.call_args[0][0]
            assert "LEMAVTR42" in called_prompt

    def test_falls_back_when_no_active_avatar(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch(
                 "cqc_lem.utilities.ai.ai_helper.generate_flux1_image_from_prompt",
                 return_value="/flux/image.webp",
             ) as mock_flux:
            from cqc_lem.utilities.ai.ai_helper import generate_post_image

            result = generate_post_image("a business photo", 99)

            mock_flux.assert_called_once()
            assert mock_flux.call_args[0][0] == "a business photo"
            assert result == "/flux/image.webp"

    def test_falls_back_when_avatar_not_succeeded(self):
        active_avatar = {
            "id": 1,
            "training_id": "train-1",
            "model_ref": None,
            "trigger_word": "LEMAVTR42",
            "status": "processing",
        }
        with patch("cqc_lem.utilities.db.get_active_avatar", return_value=active_avatar), \
             patch(
                 "cqc_lem.utilities.ai.ai_helper.generate_flux1_image_from_prompt",
                 return_value="/flux/image.webp",
             ) as mock_flux:
            from cqc_lem.utilities.ai.ai_helper import generate_post_image

            result = generate_post_image("a business photo", 1)

            mock_flux.assert_called_once()
            assert mock_flux.call_args[0][0] == "a business photo"
            assert result == "/flux/image.webp"
