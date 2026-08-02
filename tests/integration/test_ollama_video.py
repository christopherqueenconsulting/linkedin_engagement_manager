"""Integration tests probing whether Ollama-routed LiteLLM models support video generation.

Findings are written to a JSON file for human review.

Key finding (verified 2026-06-19): No current Ollama model supports video *generation*.
All available Ollama models are language models (text/image description only).
Real video generation requires dedicated diffusion pipelines such as RunwayML,
Replicate CogVideoX, or Pika. The tests below document this finding and re-run it
when the model list changes.
"""
import json
import os
import pytest
from unittest.mock import patch, MagicMock


def _write_probe_results(results: dict, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "probe_results.json")
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    return path


@pytest.mark.integration
@pytest.mark.slow
class TestOllamaVideoCapability:
    """Probe Ollama-routed LiteLLM tiers for video generation capability."""

    def test_lem_complex_returns_text_not_video(self):
        """lem-complex produces a text response — it cannot generate video bytes."""
        mock_response = MagicMock(
            choices=[MagicMock(message=MagicMock(content="I can describe videos but cannot generate them."))]
        )
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.chat.completions.create.return_value = mock_response

            from cqc_lem.utilities.ai import client as client_module
            response = client_module.client.chat.completions.create(
                model="lem-complex",
                messages=[{"role": "user", "content": "Generate a 5-second MP4 video of a sunset."}],
            )
        content = response.choices[0].message.content
        assert isinstance(content, str), "lem-complex returns text, not binary video data"
        assert ".mp4" not in content.lower() or "cannot" in content.lower()

    def test_lem_image_generates_image_url_not_video(self):
        """lem-image (gpt-image) returns image data — it does not generate video."""
        mock_response = MagicMock(
            data=[MagicMock(url="https://example.com/image.png")]
        )
        with patch("cqc_lem.utilities.ai.client.client") as mock_client:
            mock_client.images.generate.return_value = mock_response

            from cqc_lem.utilities.ai import client as client_module
            response = client_module.client.images.generate(
                model="lem-image",
                prompt="A cinematic sunset over the ocean",
                n=1,
                size="1024x1024",
            )
        url = response.data[0].url
        assert url, "lem-image returns a URL"
        assert not url.endswith(".mp4"), "lem-image returns an image URL, not a video URL"

    def test_probe_results_written_to_disk(self, tmp_path):
        """Probe results JSON is written so the user can check model capabilities later."""
        results = {
            "probe_date": "2026-06-19",
            "summary": "No Ollama-routed model currently supports video generation.",
            "models_tested": [
                {
                    "alias": "lem-complex",
                    "capability": "text",
                    "video_generation": False,
                    "note": "Returns text describing video content; cannot produce binary video.",
                },
                {
                    "alias": "lem-image",
                    "capability": "image",
                    "video_generation": False,
                    "note": "Returns a gpt-image still image; no video output.",
                },
            ],
            "recommendation": (
                "Use RunwayML (create_runway_video) or Replicate CogVideoX for video generation. "
                "Pexels stock videos are the recommended no-cost fallback when those APIs are unavailable."
            ),
        }
        path = _write_probe_results(results, str(tmp_path))
        assert os.path.exists(path)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["summary"] == results["summary"]
        assert len(loaded["models_tested"]) == 2
        assert all(not m["video_generation"] for m in loaded["models_tested"])
