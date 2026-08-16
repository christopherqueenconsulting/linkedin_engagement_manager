"""Surface attribution and vision-gate verdict telemetry for image renders (issue #1291)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import image_gen
from cqc_lem.utilities.ai.image_gen import render_avatar_image_gated, render_image_gated

pytestmark = pytest.mark.unit


class TestSurfaceInMediaCost:
    def test_gpt_image_render_includes_surface_in_media_cost(self, tmp_path):
        b64 = __import__("base64").b64encode(b"png").decode("ascii")
        response = SimpleNamespace(
            data=[SimpleNamespace(b64_json=b64, url=None)],
            model="gpt-image-2",
        )
        with patch("cqc_lem.utilities.ai.image_gen.client") as mock_client, \
             patch("cqc_lem.utilities.ai.image_gen.assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.observability.track_media_cost") as track:
            mock_client.images.generate.return_value = response
            from cqc_lem.utilities.ai.image_gen import _render_via_gpt_image
            _render_via_gpt_image("a prompt", ratio="1:1", quality="medium",
                                  user_id=3, post_id=9, surface="post_image")

        meta = track.call_args[1]["meta"]
        assert meta["surface"] == "post_image"

    def test_ungated_render_passes_surface_to_backend(self):
        with patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/x.png", "gpt-image")) as render:
            image_gen.render_image_from_prompt("p", surface="thumbnail", user_id=3)
        assert render.call_args[1]["surface"] == "thumbnail"


class TestGateVerdictTelemetry:
    def _verdict_response(self, payload: dict) -> SimpleNamespace:
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=__import__("json").dumps(payload)))])

    def test_accepted_render_emits_verdict_event(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch("cqc_lem.utilities.observability.track_image_gate_verdict") as track, \
             patch.object(image_gen, "_render_with_backend",
                          return_value=(str(img), "gpt-image")) as _, \
             patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 4, "issues": []})
            render_image_gated("p", surface="post_image")

        track.assert_called_once()
        props = track.call_args[1]
        assert props["verdict"] == "accepted"
        assert props["surface"] == "post_image"
        assert props["checked"] is True
        assert props["acceptable"] is True
        assert props["issues"] == []

    def test_rejected_after_budget_emits_rejected_verdict(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch("cqc_lem.utilities.observability.track_image_gate_verdict") as track, \
             patch.object(image_gen, "_render_with_backend",
                          return_value=(str(img), "gpt-image")) as _, \
             patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": False, "relevance": 1, "issues": ["garbled text"]})
            render_image_gated("p", surface="newsletter")

        props = track.call_args[1]
        assert props["verdict"] == "rejected"
        assert props["attempt_count"] == image_gen.IMAGE_GATE_MAX_ATTEMPTS
        assert props["issues"] == ["garbled text"]

    def test_unchecked_gate_emits_unchecked_verdict(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch("cqc_lem.utilities.observability.track_image_gate_verdict") as track, \
             patch.object(image_gen, "_render_with_backend",
                          return_value=(str(img), "gpt-image")) as _, \
             patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.side_effect = RuntimeError("vision down")
            render_image_gated("p", surface="post_image")

        props = track.call_args[1]
        assert props["verdict"] == "unchecked"
        assert props["checked"] is False
        assert props["acceptable"] is True

    def test_avatar_gated_render_emits_verdict(self, tmp_path):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK",
                  "gender_presentation": "man", "age_band": "40s"}
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch("cqc_lem.utilities.observability.track_image_gate_verdict") as track, \
             patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=(str(img), True)), \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 5, "issues": []})
            render_avatar_image_gated("p", avatar=avatar, user_id=3,
                                      surface="newsletter", post_id=9)

        props = track.call_args[1]
        assert props["verdict"] == "accepted"
        assert props["surface"] == "newsletter"
        assert props["user_id"] == 3
        assert props["post_id"] == 9


class TestTheVerdictReachesTheCaller:
    """Issue #1377: the same verdict the event carries has to reach the STORE.

    PostHog answers "how often does the gate reject", but the brief receipt written beside a stored
    render answers "was THIS image checked" — and a render that shipped unchecked reads exactly like
    one that passed unless the verdict travels with it.
    """

    def _verdict_response(self, payload):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=__import__("json").dumps(payload)))])

    def test_render_info_carries_the_gate_verdict(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        render_info: dict = {}
        with patch("cqc_lem.utilities.observability.track_image_gate_verdict"), \
             patch.object(image_gen, "_render_with_backend",
                          return_value=(str(img), "gpt-image")), \
             patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 4, "issues": []})
            render_image_gated("p", surface="post_image", render_info=render_info)
        assert render_info["gate_verdict"] == "accepted"

    def test_an_outage_reads_as_unchecked_not_as_a_pass(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        render_info: dict = {}
        with patch("cqc_lem.utilities.observability.track_image_gate_verdict"), \
             patch.object(image_gen, "_render_with_backend",
                          return_value=(str(img), "gpt-image")), \
             patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.side_effect = RuntimeError("vision down")
            render_image_gated("p", surface="post_image", render_info=render_info)
        assert render_info["gate_verdict"] == "unchecked"

    def test_the_avatar_path_carries_it_beside_used_avatar(self, tmp_path):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK",
                  "gender_presentation": "man", "age_band": "40s"}
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        render_info: dict = {}
        with patch("cqc_lem.utilities.observability.track_image_gate_verdict"), \
             patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=(str(img), True)), \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": False, "relevance": 1, "issues": ["fused fingers"]})
            render_avatar_image_gated("p", avatar=avatar, user_id=3, surface="newsletter",
                                      post_id=9, render_info=render_info)
        assert render_info["gate_verdict"] == "rejected"
        assert render_info["used_avatar"] is True

    def test_generate_post_image_reports_the_verdict_on_the_base_flux_branch_too(self, tmp_path):
        # Both branches of `generate_post_image` are gated, so only one of them reporting would
        # make a base-Flux render read as ungraded in the brief receipt beside it (issue #1377).
        from cqc_lem.utilities.ai.ai_helper import generate_post_image

        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        render_info: dict = {}
        with patch("cqc_lem.utilities.observability.track_image_gate_verdict"), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None), \
             patch.object(image_gen, "_render_with_backend",
                          return_value=(str(img), "gpt-image")), \
             patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 4, "issues": []})
            generate_post_image("p", 3, post_id=9, render_info=render_info)
        assert render_info["gate_verdict"] == "accepted"
        assert render_info["used_avatar"] is False
