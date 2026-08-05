"""Single-image text posts: generation is best-effort, pref-gated, and avatar-guardrailed."""
import os
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai.image_brief import ImageBrief

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"


def _brief(prompt="a rendered prompt", focal="the focal idea"):
    return ImageBrief(prompt=prompt, ratio="1:1", surface="post_image",
                      style_preset="post_image", focal_concept=focal)


class TestGenerateTextPostImage:
    def _generate(self, tmp_path, *, prefs=None, avatar=None, rendered="render.png",
                  render_raises=False):
        from cqc_lem.app.run_content_plan import _generate_text_post_image

        rendered_path = None
        if rendered:
            rendered_path = str(tmp_path / rendered)
            with open(rendered_path, "wb") as fh:
                fh.write(b"png")
        assets = tmp_path / "assets"
        assets.mkdir(exist_ok=True)

        with patch("cqc_lem.utilities.db.get_engagement_preferences",
                   return_value=prefs if prefs is not None else {"text_post_images": True}), \
             patch("cqc_lem.utilities.db.update_db_post_image_url", return_value=True) as store, \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None), \
             patch("cqc_lem.utilities.post_image.assets_dir", str(assets)), \
             patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=avatar), \
             patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief()) as brief, \
             patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                   side_effect=RuntimeError("render down") if render_raises else None,
                   return_value=rendered_path) as render, \
             patch("cqc_lem.utilities.ai.image_gen.render_avatar_image_gated",
                   return_value=rendered_path) as lora:
            result = _generate_text_post_image(7, "My post text", 42)
        return result, store, brief, render, lora, assets

    def test_happy_path_stores_an_api_asset_url(self, tmp_path):
        result, store, brief, render, lora, assets = self._generate(tmp_path)
        assert result and "file_name=images/posts/42/" in result
        store.assert_called_once_with(42, result)
        # The copy that publish + purge operate on lives under the shared assets volume.
        post_dir = os.path.join(str(assets), "images", "posts", "42")
        assert [f for f in os.listdir(post_dir) if f.endswith(".png")]
        # No avatar -> the vision-gated base renderer on the post_image surface.
        assert render.call_args[1]["surface"] == "post_image"
        assert render.call_args[1]["focal_concept"] == "the focal idea"
        lora.assert_not_called()

    def test_pref_off_generates_nothing(self, tmp_path):
        result, store, brief, render, _, _ = self._generate(
            tmp_path, prefs={"text_post_images": False})
        assert result is None
        brief.assert_not_called()
        render.assert_not_called()
        store.assert_not_called()

    def test_avatar_goes_through_the_guardrailed_lora_path(self, tmp_path):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK",
                  "gender_presentation": "man", "age_band": "40s"}
        result, store, brief, render, lora, _ = self._generate(tmp_path, avatar=avatar)
        assert result is not None
        # Avatar resolved BEFORE the brief (the #744 subject-clause ordering)...
        assert brief.call_args[1]["avatar"] == avatar
        # ...and the likeness renders through the GATED avatar path, so an off-subject headshot
        # is caught rather than shipped (C2PA/provenance stays inside that helper).
        lora.assert_called_once()
        assert lora.call_args[1]["surface"] == "post_image"
        assert lora.call_args[1]["post_id"] == 42
        assert lora.call_args[1]["avatar"] == avatar
        assert lora.call_args[1]["focal_concept"] == "the focal idea"
        render.assert_not_called()

    def test_render_failure_never_raises_and_ships_bare(self, tmp_path):
        result, store, _, _, _, _ = self._generate(tmp_path, render_raises=True)
        assert result is None
        store.assert_not_called()

    def test_no_post_id_is_a_no_op(self):
        from cqc_lem.app.run_content_plan import _generate_text_post_image
        with patch("cqc_lem.utilities.db.get_engagement_preferences") as prefs:
            assert _generate_text_post_image(7, "text", None) is None
        prefs.assert_not_called()


class TestPurgeIncludesPostImages:
    def test_post_image_dir_is_removed(self, tmp_path):
        from cqc_lem.utilities import utils
        img_dir = tmp_path / "images" / "posts" / "42"
        img_dir.mkdir(parents=True)
        (img_dir / "a.png").write_bytes(b"png")
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            removed = utils.purge_post_assets(42)
        assert str(img_dir) in removed
        assert not img_dir.exists()
