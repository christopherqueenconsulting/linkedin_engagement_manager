"""The ONE renderer facade: backend policy, bounded Replicate runs, and the vision gate."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import image_gen
from cqc_lem.utilities.ai.image_gen import (QualityVerdict, render_image_from_prompt,
                                            render_image_gated, run_replicate_bounded,
                                            size_for_ratio)

pytestmark = pytest.mark.unit


class TestSizeForRatio:
    @pytest.mark.parametrize("ratio,size", [
        ("1:1", "1024x1024"), ("16:9", "1536x1024"), ("9:16", "1024x1536"),
        ("4:5", "1024x1024"),  # unknown ratios fall back to square
    ])
    def test_mapping(self, ratio, size):
        assert size_for_ratio(ratio) == size


class TestBackendPolicy:
    def test_auto_prefers_gpt_image(self):
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png") as gpt, \
             patch.object(image_gen, "_render_via_flux") as flux:
            assert render_image_from_prompt("p", ratio="16:9", user_id=3) == "/tmp/g.png"
        gpt.assert_called_once()
        flux.assert_not_called()

    def test_auto_falls_back_to_flux_on_gpt_failure(self):
        with patch.object(image_gen, "_render_via_gpt_image",
                          side_effect=RuntimeError("403 org not verified")), \
             patch.object(image_gen, "_render_via_flux", return_value="/tmp/f.webp") as flux:
            assert render_image_from_prompt("p", user_id=3) == "/tmp/f.webp"
        flux.assert_called_once()

    def test_forced_gpt_image_does_not_fall_back(self):
        with patch.object(image_gen, "IMAGE_BACKEND", "gpt-image"), \
             patch.object(image_gen, "_render_via_gpt_image", side_effect=RuntimeError("down")), \
             patch.object(image_gen, "_render_via_flux") as flux:
            with pytest.raises(RuntimeError):
                render_image_from_prompt("p")
        flux.assert_not_called()

    def test_forced_flux_never_touches_gpt_image(self):
        with patch.object(image_gen, "IMAGE_BACKEND", "flux"), \
             patch.object(image_gen, "_render_via_gpt_image") as gpt, \
             patch.object(image_gen, "_render_via_flux", return_value="/tmp/f.webp"):
            assert render_image_from_prompt("p") == "/tmp/f.webp"
        gpt.assert_not_called()


class TestRunReplicateBounded:
    def test_returns_on_first_success(self):
        with patch("replicate.run", return_value=["url"]) as run:
            assert run_replicate_bounded("m/ref", {"prompt": "p"}) == ["url"]
        run.assert_called_once()

    def test_retries_once_then_raises(self):
        with patch("replicate.run", side_effect=RuntimeError("boom")) as run, \
             patch("time.sleep"):
            with pytest.raises(RuntimeError, match="boom"):
                run_replicate_bounded("m/ref", {"prompt": "p"}, attempts=2)
        assert run.call_count == 2


class TestVisionGate:
    def _verdict_response(self, payload: dict) -> SimpleNamespace:
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)))])

    def test_gate_reads_the_image_with_lem_vision(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": False, "relevance": 1, "issues": ["garbled text"]})
            verdict = image_gen.inspect_render_quality(str(img), "a focal concept")
        assert verdict.checked and not verdict.acceptable
        assert verdict.issues == ["garbled text"]
        assert mock_client.chat.completions.create.call_args[1]["model"] == "lem-vision"

    def test_gate_fails_open_on_vision_outage(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.side_effect = RuntimeError("proxy down")
            verdict = image_gen.inspect_render_quality(str(img), "focal")
        assert verdict.acceptable and not verdict.checked

    def test_enforced_surface_regenerates_with_the_issues_appended(self):
        verdicts = [QualityVerdict(acceptable=False, issues=["distorted face"]),
                    QualityVerdict(acceptable=True)]
        with patch.object(image_gen, "render_image_from_prompt",
                          side_effect=["/tmp/1.png", "/tmp/2.png"]) as render, \
             patch.object(image_gen, "inspect_render_quality", side_effect=verdicts):
            path = render_image_gated("base prompt", surface="newsletter")
        assert path == "/tmp/2.png"
        assert render.call_count == 2
        assert "distorted face" in render.call_args_list[1][0][0]

    def test_attempts_are_bounded_and_the_last_render_ships(self):
        bad = QualityVerdict(acceptable=False, issues=["filler"])
        with patch.object(image_gen, "render_image_from_prompt",
                          return_value="/tmp/x.png") as render, \
             patch.object(image_gen, "inspect_render_quality", return_value=bad):
            path = render_image_gated("p", surface="newsletter")
        assert path == "/tmp/x.png"
        assert render.call_count == image_gen.IMAGE_GATE_MAX_ATTEMPTS

    def test_unenforced_surface_is_advisory_only(self):
        bad = QualityVerdict(acceptable=False, issues=["filler"])
        with patch.object(image_gen, "IMAGE_QUALITY_GATE_SURFACES", ("newsletter",)), \
             patch.object(image_gen, "render_image_from_prompt",
                          return_value="/tmp/x.png") as render, \
             patch.object(image_gen, "inspect_render_quality", return_value=bad):
            assert render_image_gated("p", surface="carousel") == "/tmp/x.png"
        render.assert_called_once()
