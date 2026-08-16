"""The video half of the render-brief record — issue #1377 (P4).

A video post's file is downloaded and stored by the CALLER, not by the renderer, so the brief the
source frame was authored from has to travel to the store or it is lost — which is the state the
last image audit hit: it could only infer what row 10 of its table was meant to depict.

Two things worth pinning beyond "it writes a file": the brief reaches BOTH store paths (the birth
path in `_create_content_for_planned_post` and the regenerate/heal path in `_store_video_asset`),
and a Pexels fallback records NOTHING — stock footage was never what the brief described.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai.image_brief import ImageBrief
from cqc_lem.utilities.media_provenance import read_brief_receipt

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"


def _valid_mp4(directory, name: str = "clip.mp4") -> str:
    path = Path(directory) / name
    path.write_bytes(b'\x00\x00\x00 ftypisom' + b'\x00' * 56)
    return str(path)


def _brief(surface: str = "video") -> ImageBrief:
    return ImageBrief(prompt="a source frame brief", ratio="9:16", surface=surface,
                      style_preset=surface, focal_concept="a steering wheel, not a brake pedal")


class TestStorePathRecordsTheBrief:
    def test_the_stored_url_reads_back_the_source_frames_brief(self, tmp_path, monkeypatch):
        import cqc_lem.app.run_content_plan as rcp
        monkeypatch.setattr(rcp, "assets_dir", str(tmp_path))
        monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path))
        with patch(f"{_RCP}.save_video_url_to_dir",
                   side_effect=lambda url, directory: _valid_mp4(directory)), \
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch(f"{_RCP}._caption_video_asset"), \
             patch(f"{_RCP}._record_video_asset_measures"), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials"), \
             patch(f"{_RCP}.update_db_post_video_url"):
            api_url = rcp._store_video_asset(7, "https://runway.test/clip.mp4", user_id=3,
                                             brief=_brief())
        recovered = read_brief_receipt(api_url)
        assert recovered["focal_concept"] == "a steering wheel, not a brake pedal"
        assert recovered["surface"] == "video" and recovered["post_id"] == 7

    def test_no_brief_records_nothing_rather_than_an_empty_one(self, tmp_path, monkeypatch):
        import cqc_lem.app.run_content_plan as rcp
        monkeypatch.setattr(rcp, "assets_dir", str(tmp_path))
        monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path))
        with patch(f"{_RCP}.save_video_url_to_dir",
                   side_effect=lambda url, directory: _valid_mp4(directory)), \
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch(f"{_RCP}._caption_video_asset"), \
             patch(f"{_RCP}._record_video_asset_measures"), \
             patch(f"{_RCP}.update_db_post_video_url"):
            api_url = rcp._store_video_asset(7, "/local/pexels/stock.mp4")
        assert read_brief_receipt(api_url) is None


class TestBirthPathRecordsTheBrief:
    def test_the_url_persisted_at_birth_reads_back_the_brief(self, tmp_path, monkeypatch):
        import cqc_lem.app.run_content_plan as rcp
        monkeypatch.setattr(rcp, "assets_dir", str(tmp_path))
        monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path))
        stored: dict = {}

        def _create_content(*_args, brief_info=None, **_kwargs):
            brief_info["brief"] = _brief()
            return "Hook line", "http://runway/clip.mp4"

        video = _valid_mp4(tmp_path)
        with patch(f"{_RCP}.create_content", side_effect=_create_content), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=video), \
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch(f"{_RCP}._caption_video_asset"), \
             patch(f"{_RCP}._record_video_asset_measures"), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials"), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}._post_used_avatar_media", return_value=False), \
             patch(f"{_RCP}.update_db_post_video_url",
                   side_effect=lambda post_id, url: stored.update(url=url)):
            rcp._create_content_for_planned_post(
                {"user_id": 1, "id": 42, "post_type": "video", "buyer_stage": "awareness"},
                {"auto_schedule_posts": False})
        assert read_brief_receipt(stored["url"])["focal_concept"] == \
            "a steering wheel, not a brake pedal"


class TestPexelsFallbackKeepsNoBrief:
    def test_a_stock_clip_never_inherits_the_render_it_replaced(self):
        brief_info: dict = {}

        def _prompt(*_args, brief_info=None, **_kwargs):
            brief_info["brief"] = _brief()
            return "scene"

        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch(f"{_RCP}.get_flux_image_prompt_from_ai", side_effect=_prompt), \
             patch(f"{_RCP}.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt",
                   return_value="/tmp/frame.png"), \
             patch(f"{_RCP}.create_runway_video", side_effect=RuntimeError("runway down")), \
             patch(f"{_RCP}._persist_video_model"), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch("cqc_lem.utilities.pexels_helper.download_pexels_video",
                   return_value="/tmp/stock.mp4"):
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(1, "text", None, post_id=9, brief_info=brief_info)
        assert src == "/tmp/stock.mp4"
        assert brief_info == {}

    def test_a_successful_render_hands_the_brief_back(self):
        brief_info: dict = {}

        def _prompt(*_args, brief_info=None, **_kwargs):
            brief_info["brief"] = _brief()
            return "scene"

        with patch("cqc_lem.utilities.db.get_post_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_default_video_quality", return_value="standard"), \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=None), \
             patch(f"{_RCP}.get_flux_image_prompt_from_ai", side_effect=_prompt), \
             patch(f"{_RCP}.get_runway_ml_video_prompt_from_ai", return_value="motion"), \
             patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt",
                   return_value="/tmp/frame.png"), \
             patch(f"{_RCP}._persist_video_model"), \
             patch(f"{_RCP}.create_runway_video", return_value="https://runway.test/clip.mp4"):
            from cqc_lem.app.run_content_plan import _generate_video_src
            src = _generate_video_src(1, "text", None, post_id=9, brief_info=brief_info)
        assert src == "https://runway.test/clip.mp4"
        assert brief_info["brief"].focal_concept == "a steering wheel, not a brake pedal"


class TestTheWrapperKeepsTheBrief:
    def test_the_prompt_helper_hands_back_the_whole_brief_when_asked(self):
        brief_info: dict = {}
        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=_brief("post_image")):
            from cqc_lem.utilities.ai.ai_helper import get_flux_image_prompt_from_ai
            prompt = get_flux_image_prompt_from_ai("post body", brief_info=brief_info)
        assert prompt == "a source frame brief"
        assert brief_info["brief"].focal_concept == "a steering wheel, not a brake pedal"

    def test_a_caller_that_asks_for_nothing_is_unchanged(self):
        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=_brief()):
            from cqc_lem.utilities.ai.ai_helper import get_flux_image_prompt_from_ai
            assert get_flux_image_prompt_from_ai("post body") == "a source frame brief"
