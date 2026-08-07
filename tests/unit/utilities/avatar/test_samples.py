"""Unit tests for avatar preview sample rendering (issue #744, decision 4A)."""
import os
from unittest.mock import patch

import pytest

from cqc_lem.utilities.avatar.samples import (
    AVATAR_SAMPLE_SCENES,
    render_avatar_samples,
    sample_asset_url,
    sample_payload,
    sample_relative_dir,
)

pytestmark = pytest.mark.unit

_AVATAR = {"id": 12, "model_ref": "owner/lora:v1", "trigger_word": "TOK",
           "gender_presentation": "man", "age_band": "40s"}

_GEN = "cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar"


@pytest.fixture
def fake_assets(tmp_path):
    with patch("cqc_lem.utilities.avatar.samples.assets_dir", str(tmp_path)):
        yield tmp_path


def _source_image(tmp_path, name="out.webp"):
    src = tmp_path / name
    src.write_bytes(b"fake-image")
    return str(src)


class TestRenderAvatarSamples:
    def test_renders_every_scene_and_returns_relative_paths(self, fake_assets, tmp_path):
        src = _source_image(tmp_path)
        with patch(_GEN, return_value=(src, True)) as gen, \
             patch("cqc_lem.utilities.avatar.samples._sign_best_effort") as sign:
            samples = render_avatar_samples(_AVATAR)

        assert [s["label"] for s in samples] == [label for label, _ in AVATAR_SAMPLE_SCENES]
        assert all(s["path"].startswith("images/avatar_samples/12/") for s in samples)
        assert gen.call_count == len(AVATAR_SAMPLE_SCENES)
        # Every sample is C2PA-signed: it is a synthetic likeness of a real person.
        assert sign.call_count == len(AVATAR_SAMPLE_SCENES)
        for s in samples:
            assert os.path.exists(os.path.join(str(fake_assets), *s["path"].split("/")))

    def test_prompt_carries_the_trigger_word_and_the_declared_clause(self, fake_assets, tmp_path):
        """What the user approves must be built the same way a production image is."""
        src = _source_image(tmp_path)
        with patch(_GEN, return_value=(src, True)) as gen, \
             patch("cqc_lem.utilities.avatar.samples._sign_best_effort"):
            render_avatar_samples(_AVATAR)

        prompt = gen.call_args_list[0][0][0]
        assert prompt.startswith("TOK, a man in his 40s, ")
        assert gen.call_args_list[0][1]["ratio"] == "1:1"

    def test_base_flux_fallback_is_discarded(self, fake_assets, tmp_path):
        """A fallback render is a picture of a stranger — showing it as 'your avatar' is worse
        than showing nothing.
        """
        src = _source_image(tmp_path)
        with patch(_GEN, return_value=(src, False)), \
             patch("cqc_lem.utilities.avatar.samples._sign_best_effort"):
            assert render_avatar_samples(_AVATAR) == []

    def test_one_failed_scene_does_not_lose_the_others(self, fake_assets, tmp_path):
        src = _source_image(tmp_path)
        outcomes = [RuntimeError("boom"), (src, True), (src, True)]
        with patch(_GEN, side_effect=outcomes), \
             patch("cqc_lem.utilities.avatar.samples._sign_best_effort"):
            samples = render_avatar_samples(_AVATAR)
        assert len(samples) == 2

    def test_never_raises_when_every_scene_fails(self, fake_assets):
        with patch(_GEN, side_effect=RuntimeError("replicate down")):
            assert render_avatar_samples(_AVATAR) == []

    @pytest.mark.parametrize("avatar", [{}, {"id": 1}, {"model_ref": "m"}])
    def test_incomplete_avatar_renders_nothing(self, avatar, fake_assets):
        with patch(_GEN) as gen:
            assert render_avatar_samples(avatar) == []
        gen.assert_not_called()

    def test_signing_failure_is_swallowed(self, fake_assets, tmp_path):
        src = _source_image(tmp_path)
        with patch(_GEN, return_value=(src, True)), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials",
                   side_effect=RuntimeError("no cert")):
            assert len(render_avatar_samples(_AVATAR)) == len(AVATAR_SAMPLE_SCENES)


class TestSampleSerialization:
    def test_relative_dir(self):
        assert sample_relative_dir(9) == "images/avatar_samples/9"

    def test_asset_url(self):
        url = sample_asset_url("images/avatar_samples/9/headshot.webp", api_url="https://x")
        assert url == "https://x/api/assets?file_name=images/avatar_samples/9/headshot.webp"

    def test_payload_skips_malformed_entries(self):
        avatar = {"sample_paths": [
            {"label": "headshot", "path": "images/avatar_samples/9/headshot.webp"},
            {"label": "broken"},          # no path
            "not-a-dict",
        ]}
        payload = sample_payload(avatar)
        assert len(payload) == 1 and payload[0]["label"] == "headshot"

    def test_payload_empty_when_no_samples(self):
        assert sample_payload({}) == []
