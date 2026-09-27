"""The Pexels stock fallback stores its clip on BOTH video store paths.

`download_pexels_video` returns a LOCAL path, not a URL. Before the fix, both store paths passed it
to `save_video_url_to_dir`, whose `requests.get` raised `MissingSchema`: the valid clip was dropped,
the video model cleared, and the planned post failed. These run the real `save_video_url_to_dir`
against a real file, with only the probe and the DB writes mocked.
"""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"


class _MondayDatetime:
    @classmethod
    def now(cls, tz=None):
        from datetime import datetime
        return datetime(2024, 1, 8, 12, 0)  # Monday


def _stock_clip(assets_root) -> str:
    """A clip where `_generate_video_src` puts the Pexels download."""
    stock_dir = assets_root / "videos" / "pexels"
    stock_dir.mkdir(parents=True)
    clip = stock_dir / "pexels_123.mp4"
    clip.write_bytes(b'\x00\x00\x00 ftypisom' + b'\x00' * 56)
    return str(clip)


class TestRegeneratePathStoresTheStockClip:
    def test_the_local_clip_is_stored_under_its_pexels_name(self, tmp_path, monkeypatch):
        import cqc_lem.app.run_content_plan as rcp
        monkeypatch.setattr(rcp, "assets_dir", str(tmp_path))
        monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path))
        clip = _stock_clip(tmp_path)
        with patch(f"{_RCP}._accept_probed_video", return_value=True) as accept, \
             patch(f"{_RCP}._caption_video_asset"), \
             patch(f"{_RCP}._record_video_asset_measures"), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials") as c2pa, \
             patch(f"{_RCP}._persist_video_model") as persist_model, \
             patch(f"{_RCP}.update_db_post_video_url") as store_url:
            api_url = rcp._store_video_asset(9, clip)

        stored = tmp_path / "videos" / "runwayml" / "pexels_123.mp4"
        assert stored.is_file()
        assert accept.call_args.args[1] == str(stored)
        assert api_url.endswith("file_name=videos/runwayml/pexels_123.mp4")
        store_url.assert_called_once_with(9, api_url)
        persist_model.assert_not_called()
        # Stock footage is not AI output, so it gets no AI content credentials.
        c2pa.assert_not_called()


class TestBirthPathStoresTheStockClip:
    def test_the_planned_post_keeps_its_stock_video(self, tmp_path, monkeypatch):
        import cqc_lem.app.run_content_plan as rcp
        monkeypatch.setattr(rcp, "assets_dir", str(tmp_path))
        monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path))
        monkeypatch.setattr(f"{_RCP}.datetime", _MondayDatetime)
        clip = _stock_clip(tmp_path)
        with patch(f"{_RCP}.get_planned_posts_within_buffer",
                   return_value=[{"user_id": 1, "id": 42, "post_type": "video",
                                  "buyer_stage": "awareness"}]), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=("Hook line", clip)), \
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch(f"{_RCP}._caption_video_asset"), \
             patch(f"{_RCP}._record_video_asset_measures"), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials") as c2pa, \
             patch(f"{_RCP}._persist_video_model") as persist_model, \
             patch(f"{_RCP}.record_post_failed") as failed, \
             patch(f"{_RCP}.update_db_post_video_url") as store_url, \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch.object(rcp, "AI_DISCLOSURE_ENABLED", False):
            rcp.auto_create_weekly_content(user_id=1)

        assert (tmp_path / "videos" / "runwayml" / "pexels_123.mp4").is_file()
        failed.assert_not_called()
        persist_model.assert_not_called()
        c2pa.assert_not_called()
        post_id, api_url = store_url.call_args.args
        assert post_id == 42
        assert api_url.endswith("file_name=videos/runwayml/pexels_123.mp4")
