"""Unit tests for the avatar-likeness probe (issue #1279)."""
import base64
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

pytestmark = pytest.mark.unit

_AVATAR = {"id": 3, "trigger_word": "LEMAVTR1", "model_ref": "owner/lora:v1",
           "status": "succeeded", "approval_status": "approved",
           "gender_presentation": "man", "age_band": "40s"}


def _make_image(tmp_path, name: str = "frame.png") -> str:
    path = str(tmp_path / name)
    Image.new("RGB", (64, 64), color=(10, 40, 80)).save(path)
    return path


def _vision_response(present: bool, reason: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"present": present, "reason": reason})))
    ])


class TestEncodeImage:
    def test_encodes_real_image_to_base64(self, tmp_path):
        from cqc_lem.utilities.avatar.likeness_probe import _encode_image
        path = _make_image(tmp_path)
        encoded, mime = _encode_image(path)
        assert base64.b64decode(encoded) == open(path, "rb").read()
        assert mime == "png"

    def test_stored_bytes_go_out_under_their_own_mime(self, tmp_path):
        """A webp frame is sent as webp — re-encoding it to PNG only inflates the request body."""
        from cqc_lem.utilities.avatar.likeness_probe import _encode_image
        path = str(tmp_path / "frame.webp")
        Image.new("RGB", (32, 32), color=(1, 2, 3)).save(path)
        _, mime = _encode_image(path)
        assert mime == "webp"

    def test_jpg_extension_maps_to_jpeg(self, tmp_path):
        from cqc_lem.utilities.avatar.likeness_probe import _encode_image
        path = str(tmp_path / "frame.jpg")
        Image.new("RGB", (32, 32), color=(1, 2, 3)).save(path)
        assert _encode_image(path)[1] == "jpeg"


class TestProbeAvatarLikeness:
    def test_empty_attributes_returns_unchecked(self, tmp_path):
        from cqc_lem.utilities.avatar.likeness_probe import probe_avatar_likeness
        path = _make_image(tmp_path)
        verdict = probe_avatar_likeness(path, {"gender_presentation": None, "age_band": None})
        assert verdict["checked"] is False
        assert verdict["present"] is None
        assert "No declared likeness attributes" in verdict["reason"]

    def test_missing_image_returns_unchecked(self):
        from cqc_lem.utilities.avatar.likeness_probe import probe_avatar_likeness
        verdict = probe_avatar_likeness("/no/such/image.png", _AVATAR)
        assert verdict["checked"] is False
        assert verdict["present"] is None

    def test_positive_verdict(self, tmp_path):
        from cqc_lem.utilities.ai.client import client
        from cqc_lem.utilities.avatar.likeness_probe import probe_avatar_likeness
        path = _make_image(tmp_path)
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.return_value = _vision_response(True, "looks like the author")
            verdict = probe_avatar_likeness(path, _AVATAR, user_id=7, post_id=9)
        assert verdict["checked"] is True
        assert verdict["present"] is True
        assert "a man in his 40s" in mock_chat.completions.create.call_args[1]["messages"][0]["content"][0]["text"]
        assert mock_chat.completions.create.call_args[1]["model"] == "lem-vision"

    def test_negative_verdict(self, tmp_path):
        from cqc_lem.utilities.ai.client import client
        from cqc_lem.utilities.avatar.likeness_probe import probe_avatar_likeness
        path = _make_image(tmp_path)
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.return_value = _vision_response(False, "wrong person")
            verdict = probe_avatar_likeness(path, _AVATAR)
        assert verdict["checked"] is True
        assert verdict["present"] is False
        assert verdict["reason"] == "wrong person"

    def test_empty_vision_response_returns_unchecked(self, tmp_path):
        from cqc_lem.utilities.ai.client import client
        from cqc_lem.utilities.avatar.likeness_probe import probe_avatar_likeness
        path = _make_image(tmp_path)
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.return_value = SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=""))
            ])
            verdict = probe_avatar_likeness(path, _AVATAR)
        assert verdict["checked"] is False
        assert "Empty vision response" in verdict["reason"]

    def test_invalid_json_returns_unchecked(self, tmp_path):
        from cqc_lem.utilities.ai.client import client
        from cqc_lem.utilities.avatar.likeness_probe import probe_avatar_likeness
        path = _make_image(tmp_path)
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.return_value = SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content="not-json"))
            ])
            verdict = probe_avatar_likeness(path, _AVATAR)
        assert verdict["checked"] is False
        assert "Probe error" in verdict["reason"]

    def test_vision_exception_returns_unchecked(self, tmp_path):
        from cqc_lem.utilities.ai.client import client
        from cqc_lem.utilities.avatar.likeness_probe import probe_avatar_likeness
        path = _make_image(tmp_path)
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.side_effect = RuntimeError("proxy down")
            verdict = probe_avatar_likeness(path, _AVATAR)
        assert verdict["checked"] is False
        assert verdict["present"] is None
        assert "proxy down" in verdict["reason"]


class TestUncheckedCause:
    """Why a probe never ran, carried as a closed vocabulary rather than free text (issue #1598).

    Production read 152 unchecked events and finding the cause meant scanning `reason`, which is
    whatever the vision model wrote.
    """

    def test_no_declared_attributes(self, tmp_path):
        from cqc_lem.utilities.avatar.likeness_probe import (
            UNCHECKED_NO_DECLARED_ATTRIBUTES,
            probe_avatar_likeness,
        )
        verdict = probe_avatar_likeness(_make_image(tmp_path), {"gender_presentation": None})
        assert verdict["unchecked_cause"] == UNCHECKED_NO_DECLARED_ATTRIBUTES

    def test_a_declination_alone_is_the_same_inert_state(self, tmp_path):
        """`prefer-not-to-say` renders no noun, so on its own the clause is still empty."""
        from cqc_lem.utilities.avatar.likeness_probe import (
            UNCHECKED_NO_DECLARED_ATTRIBUTES,
            probe_avatar_likeness,
        )
        verdict = probe_avatar_likeness(
            _make_image(tmp_path),
            {"gender_presentation": "prefer-not-to-say", "age_band": None})
        assert verdict["unchecked_cause"] == UNCHECKED_NO_DECLARED_ATTRIBUTES

    def test_missing_image(self):
        from cqc_lem.utilities.avatar.likeness_probe import (
            UNCHECKED_NO_IMAGE,
            probe_avatar_likeness,
        )
        assert probe_avatar_likeness("/no/such/image.png", _AVATAR)["unchecked_cause"] == \
            UNCHECKED_NO_IMAGE

    def test_empty_vision_response(self, tmp_path):
        from cqc_lem.utilities.ai.client import client
        from cqc_lem.utilities.avatar.likeness_probe import (
            UNCHECKED_VISION_ERROR,
            probe_avatar_likeness,
        )
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.return_value = SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=""))
            ])
            verdict = probe_avatar_likeness(_make_image(tmp_path), _AVATAR)
        assert verdict["unchecked_cause"] == UNCHECKED_VISION_ERROR

    def test_vision_exception(self, tmp_path):
        from cqc_lem.utilities.ai.client import client
        from cqc_lem.utilities.avatar.likeness_probe import (
            UNCHECKED_VISION_ERROR,
            probe_avatar_likeness,
        )
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.side_effect = RuntimeError("proxy down")
            verdict = probe_avatar_likeness(_make_image(tmp_path), _AVATAR)
        assert verdict["unchecked_cause"] == UNCHECKED_VISION_ERROR

    @pytest.mark.parametrize("present", [True, False])
    def test_a_checked_verdict_reports_none_not_a_missing_property(self, tmp_path, present):
        from cqc_lem.utilities.ai.client import client
        from cqc_lem.utilities.avatar.likeness_probe import CHECKED, probe_avatar_likeness
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.return_value = _vision_response(present)
            verdict = probe_avatar_likeness(_make_image(tmp_path), _AVATAR)
        assert verdict["unchecked_cause"] == CHECKED

    def test_every_cause_the_probe_can_report_is_in_the_vocabulary(self, tmp_path):
        from cqc_lem.utilities.ai.client import client
        from cqc_lem.utilities.avatar.likeness_probe import (
            UNCHECKED_CAUSES,
            probe_avatar_likeness,
        )
        path = _make_image(tmp_path)
        verdicts = [
            probe_avatar_likeness(path, {}),
            probe_avatar_likeness("/no/such/image.png", _AVATAR),
        ]
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.side_effect = RuntimeError("proxy down")
            verdicts.append(probe_avatar_likeness(path, _AVATAR))
        with patch.object(client, "chat") as mock_chat:
            mock_chat.completions.create.return_value = _vision_response(True)
            verdicts.append(probe_avatar_likeness(path, _AVATAR))
        assert {v["unchecked_cause"] for v in verdicts} <= UNCHECKED_CAUSES

    @pytest.mark.parametrize("verdict,expected", [
        ({"checked": True, "present": True, "reason": "ok"}, "none"),
        ({"checked": False, "present": None, "reason": "hand-rolled"}, "unknown"),
    ])
    def test_the_trackers_fallback_stays_inside_this_vocabulary(self, verdict, expected):
        """`observability` cannot import this module (that closes a cycle through `ai.client`).

        So its two fallback literals are pinned here instead — a rename landing in only one of the
        two files would ship a value no PostHog breakdown has a bucket for.
        """
        from cqc_lem.utilities.avatar.likeness_probe import UNCHECKED_CAUSES
        from cqc_lem.utilities.observability import track_avatar_likeness_probe

        with patch("cqc_lem.utilities.observability.posthog") as mock_ph:
            track_avatar_likeness_probe(1, 2, verdict, used_avatar="true")
        cause = mock_ph.capture.call_args.kwargs["properties"]["unchecked_cause"]
        assert cause == expected
        assert cause in UNCHECKED_CAUSES


class TestExtractFirstFrame:
    def test_success(self, tmp_path):
        from cqc_lem.utilities.avatar.likeness_probe import extract_first_frame
        video = str(tmp_path / "clip.mp4")
        open(video, "w").close()
        out_path = str(tmp_path / "clip_first_frame.png")
        with patch("cqc_lem.utilities.avatar.likeness_probe._ffmpeg_bin", return_value="/bin/ffmpeg"), \
             patch("subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stderr="")
            result = extract_first_frame(video, out_dir=str(tmp_path))
        assert result == out_path
        run.assert_called_once()
        cmd = run.call_args[0][0]
        assert cmd[0] == "/bin/ffmpeg"
        assert "-vframes" in cmd

    def test_returns_none_when_ffmpeg_missing(self, tmp_path):
        from cqc_lem.utilities.avatar.likeness_probe import extract_first_frame
        with patch("cqc_lem.utilities.avatar.likeness_probe._ffmpeg_bin", return_value=None):
            assert extract_first_frame(str(tmp_path / "x.mp4")) is None

    def test_returns_none_when_ffmpeg_fails(self, tmp_path):
        from cqc_lem.utilities.avatar.likeness_probe import extract_first_frame
        video = str(tmp_path / "clip.mp4")
        open(video, "w").close()
        with patch("cqc_lem.utilities.avatar.likeness_probe._ffmpeg_bin", return_value="/bin/ffmpeg"), \
             patch("subprocess.run") as run:
            run.side_effect = subprocess.CalledProcessError(1, "ffmpeg")
            assert extract_first_frame(video, out_dir=str(tmp_path)) is None
