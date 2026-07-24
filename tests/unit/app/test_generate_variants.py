"""Unit tests for the media-variant orchestrator."""

import json
import os
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


def _make_image(tmp_path):
    p = tmp_path / "src_img.webp"
    p.write_bytes(b"img-bytes")
    return str(p)


class TestGenerateMediaVariants:
    def test_happy_path_writes_metadata_and_urls(self, tmp_path):
        img = _make_image(tmp_path)

        def fake_save(url, d):
            path = os.path.join(d, "video.mp4")
            with open(path, "wb") as f:
                f.write(b"vid")
            return path

        with patch("cqc_lem.app.generate_variants.assets_dir", str(tmp_path)), \
             patch("cqc_lem.app.generate_variants.get_flux_image_prompt_from_ai", return_value="img prompt"), \
             patch("cqc_lem.app.generate_variants.generate_flux1_image_from_prompt", return_value=img), \
             patch("cqc_lem.app.generate_variants.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.app.generate_variants.create_runway_video", return_value="https://runway/v.mp4"), \
             patch("cqc_lem.app.generate_variants.save_video_url_to_dir", side_effect=fake_save):
            from cqc_lem.app.generate_variants import generate_media_variants
            payload = generate_media_variants(
                text="AI in healthcare",
                combos=[{"image_model": "black-forest-labs/flux-dev", "video_model": "gen4_turbo", "ratio": "1:1"}],
                timestamp=1000,
            )

        assert payload["batch_id"].startswith("1000_")
        v = payload["variants"][0]
        assert v["error"] is None
        assert "/api/assets?file_name=variants/" in v["image_url"]
        assert v["video_url"].endswith("variant_0_video.mp4")
        assert payload["total_estimated_cost_usd"] > 0

        meta_path = os.path.join(str(tmp_path), "variants", payload["batch_id"], "metadata.json")
        assert os.path.exists(meta_path)
        with open(meta_path) as f:
            data = json.load(f)
        assert data["request"]["text"] == "AI in healthcare"

    def test_requires_a_source(self):
        from cqc_lem.app.generate_variants import generate_media_variants
        with pytest.raises(ValueError):
            generate_media_variants()

    def test_per_combo_error_isolated(self, tmp_path):
        img = _make_image(tmp_path)
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return img

        with patch("cqc_lem.app.generate_variants.assets_dir", str(tmp_path)), \
             patch("cqc_lem.app.generate_variants.get_flux_image_prompt_from_ai", return_value="p"), \
             patch("cqc_lem.app.generate_variants.generate_flux1_image_from_prompt", side_effect=flaky), \
             patch("cqc_lem.app.generate_variants.get_runway_ml_video_prompt_from_ai", return_value="m"), \
             patch("cqc_lem.app.generate_variants.create_runway_video", return_value=None):
            from cqc_lem.app.generate_variants import generate_media_variants
            payload = generate_media_variants(
                text="x",
                combos=[{"include_video": False}, {"include_video": False}],
                timestamp=1,
            )

        assert payload["variants"][0]["error"] is not None
        assert payload["variants"][1]["error"] is None
        assert payload["variants"][1]["image_url"] is not None


class TestComboKey:
    def test_stable_key_encodes_models_and_ratio(self):
        from cqc_lem.app.generate_variants import combo_key
        key = combo_key({"image_model": "black-forest-labs/flux-1.1-pro",
                         "video_model": "gen4_turbo", "ratio": "1:1"})
        assert key == "black-forest-labs/flux-1.1-pro|gen4_turbo|1:1"

    def test_seed_only_encoded_when_present(self):
        from cqc_lem.app.generate_variants import combo_key
        base = {"image_model": "m", "video_model": "gen4_turbo", "ratio": "1:1"}
        assert combo_key(base) == "m|gen4_turbo|1:1"
        assert combo_key({**base, "seed": 42}) == "m|gen4_turbo|1:1|seed=42"

    def test_video_off_collapses_to_none(self):
        from cqc_lem.app.generate_variants import combo_key
        key = combo_key({"image_model": "m", "video_model": "gen4_turbo",
                         "ratio": "1:1", "include_video": False})
        assert key == "m|none|1:1"

    def test_resolution_alias_collapses_to_friendly_ratio(self):
        from cqc_lem.app.generate_variants import combo_key
        base = {"image_model": "m", "video_model": "gen4_turbo"}
        # A Runway resolution alias and its friendly aspect must yield the same key
        # so equivalent ratios don't fragment outcome attribution.
        assert combo_key({**base, "ratio": "960:960"}) == combo_key({**base, "ratio": "1:1"})
        assert combo_key({**base, "ratio": "960:960"}) == "m|gen4_turbo|1:1"


class TestVariantKeyInPayload:
    def test_each_variant_carries_its_key(self, tmp_path):
        img = _make_image(tmp_path)
        with patch("cqc_lem.app.generate_variants.assets_dir", str(tmp_path)), \
             patch("cqc_lem.app.generate_variants.get_flux_image_prompt_from_ai", return_value="p"), \
             patch("cqc_lem.app.generate_variants.generate_flux1_image_from_prompt", return_value=img), \
             patch("cqc_lem.app.generate_variants.create_runway_video", return_value=None):
            from cqc_lem.app.generate_variants import generate_media_variants
            payload = generate_media_variants(
                text="topic",
                combos=[{"image_model": "black-forest-labs/flux-dev", "video_model": "gen4_turbo",
                         "ratio": "1:1", "include_video": False}],
                timestamp=1,
            )
        assert payload["variants"][0]["variant_key"] == "black-forest-labs/flux-dev|none|1:1"

    def test_failed_variant_still_carries_key(self, tmp_path):
        with patch("cqc_lem.app.generate_variants.assets_dir", str(tmp_path)), \
             patch("cqc_lem.app.generate_variants.get_flux_image_prompt_from_ai",
                   side_effect=RuntimeError("boom")):
            from cqc_lem.app.generate_variants import generate_media_variants
            payload = generate_media_variants(
                text="topic",
                combos=[{"image_model": "m", "video_model": "gen4_turbo", "ratio": "1:1"}],
                timestamp=1,
            )
        v = payload["variants"][0]
        assert v["error"] is not None and v["variant_key"] == "m|gen4_turbo|1:1"


class TestRecordShippedVariant:
    def test_derives_key_and_delegates_to_db(self):
        with patch("cqc_lem.app.generate_variants._db_record_shipped_variant",
                   return_value=True) as rec:
            from cqc_lem.app.generate_variants import record_shipped_variant
            combo = {"image_model": "m", "video_model": "gen4_turbo", "ratio": "1:1", "seed": 7}
            ok = record_shipped_variant(1, 99, combo, batch_id="b1", variant_index=2)
        assert ok is True
        rec.assert_called_once_with(1, 99, "m|gen4_turbo|1:1|seed=7", combo=combo,
                                    batch_id="b1", variant_index=2)
