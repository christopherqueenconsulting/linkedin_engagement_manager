"""Unit tests for where muted-autoplay captions hook into the video pipeline (issue #1278).

The captioning module itself is covered by `tests/unit/utilities/test_video_captions.py`; what
these pin is the wiring, which is where this feature can do damage: the burn has to run on the
PROBED file, BEFORE C2PA signs it (a re-encode after signing strips the credentials), and it must
never change what `posts.video_url` ends up pointing at.
"""
from datetime import datetime as _real_datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_CAPTIONS = "cqc_lem.utilities.video_captions"


class _MondayDatetime(_real_datetime):
    @classmethod
    def now(cls, tz=None):
        return _real_datetime(2024, 1, 8, 12, 0)  # Monday


def _valid_mp4(tmp_path, name: str = "clip.mp4", size: int = 72) -> str:
    p = tmp_path / name
    p.write_bytes(b'\x00\x00\x00 ftypisom' + b'\x00' * (size - 16))
    return str(p)


class _Result:
    def __init__(self, video_path, burned=True, srt_path=None, caption_text=None):
        self.video_path, self.burned = video_path, burned
        self.srt_path, self.caption_text = srt_path, caption_text
        self.skipped_reason = None


class TestCaptionVideoAsset:
    def test_persists_the_sidecar_url_and_burned_text(self, tmp_path):
        from cqc_lem.app.run_content_plan import _caption_video_asset
        video = _valid_mp4(tmp_path)
        with patch("cqc_lem.utilities.db.post_avatar_media_state", return_value=False), \
             patch("cqc_lem.utilities.db.update_db_post_captions") as store, \
             patch(f"{_CAPTIONS}.apply_captions_to_video",
                   return_value=_Result(video, srt_path=str(tmp_path / "clip.srt"),
                                        caption_text="Hook line")), \
             patch(f"{_CAPTIONS}.caption_srt_asset_url", return_value="https://lem.test/srt"):
            _caption_video_asset(7, video, "Hook line", user_id=3)
        store.assert_called_once_with(7, "Hook line", "https://lem.test/srt")

    def test_nothing_burned_writes_no_caption_row(self, tmp_path):
        from cqc_lem.app.run_content_plan import _caption_video_asset
        video = _valid_mp4(tmp_path)
        with patch("cqc_lem.utilities.db.post_avatar_media_state", return_value=False), \
             patch("cqc_lem.utilities.db.update_db_post_captions") as store, \
             patch(f"{_CAPTIONS}.apply_captions_to_video",
                   return_value=_Result(video, burned=False)):
            _caption_video_asset(7, video, "Hook line", user_id=3)
        store.assert_not_called()

    def test_the_burned_file_is_re_probed_before_it_replaces_the_original(self, tmp_path):
        """The probe accepted the DOWNLOAD; the re-encode is what LinkedIn actually receives."""
        from cqc_lem.app.run_content_plan import _caption_video_asset
        video = _valid_mp4(tmp_path)
        truncated = str(tmp_path / "truncated.mp4")
        open(truncated, "wb").write(b"not an mp4")
        with patch("cqc_lem.utilities.db.post_avatar_media_state", return_value=False), \
             patch("cqc_lem.utilities.db.update_db_post_captions"), \
             patch(f"{_CAPTIONS}.apply_captions_to_video",
                   return_value=_Result(video, burned=False)) as apply:
            _caption_video_asset(7, video, "Hook line", user_id=3)
        validate = apply.call_args.kwargs["validate"]
        assert validate(video) is True
        assert validate(truncated) is False

    def test_avatar_led_flag_comes_from_the_post_row(self, tmp_path):
        from cqc_lem.app.run_content_plan import _caption_video_asset
        video = _valid_mp4(tmp_path)
        with patch("cqc_lem.utilities.db.post_avatar_media_state", return_value=True), \
             patch("cqc_lem.utilities.db.update_db_post_captions"), \
             patch(f"{_CAPTIONS}.apply_captions_to_video",
                   return_value=_Result(video, burned=False)) as apply:
            _caption_video_asset(7, video, "Hook line", user_id=3)
        assert apply.call_args.kwargs["avatar_led"] is True

    def test_an_unreadable_avatar_flag_counts_as_avatar_led(self, tmp_path):
        """`None` is "could not read it" — and a guess must never paint text over a likeness."""
        from cqc_lem.app.run_content_plan import _caption_video_asset
        video = _valid_mp4(tmp_path)
        with patch("cqc_lem.utilities.db.post_avatar_media_state", return_value=None), \
             patch("cqc_lem.utilities.db.update_db_post_captions"), \
             patch(f"{_CAPTIONS}.apply_captions_to_video",
                   return_value=_Result(video, burned=False)) as apply:
            _caption_video_asset(7, video, "Hook line", user_id=3)
        assert apply.call_args.kwargs["avatar_led"] is True

    def test_a_readable_non_avatar_post_is_burned_normally(self, tmp_path):
        from cqc_lem.app.run_content_plan import _caption_video_asset
        video = _valid_mp4(tmp_path)
        with patch("cqc_lem.utilities.db.post_avatar_media_state", return_value=False), \
             patch("cqc_lem.utilities.db.update_db_post_captions"), \
             patch(f"{_CAPTIONS}.apply_captions_to_video",
                   return_value=_Result(video, burned=False)) as apply:
            _caption_video_asset(7, video, "Hook line", user_id=3)
        assert apply.call_args.kwargs["avatar_led"] is False

    def test_a_raising_captioner_never_reaches_the_caller(self, tmp_path):
        from cqc_lem.app.run_content_plan import _caption_video_asset
        video = _valid_mp4(tmp_path)
        with patch("cqc_lem.utilities.db.post_avatar_media_state", side_effect=RuntimeError("db")), \
             patch("cqc_lem.utilities.db.update_db_post_captions") as store:
            _caption_video_asset(7, video, "Hook line", user_id=3)
        store.assert_not_called()


class TestStoreVideoAsset:
    def test_captions_run_before_c2pa_signing(self, tmp_path):
        """C2PA credentials do not survive a re-encode, so the order here is load-bearing."""
        from cqc_lem.app.run_content_plan import _store_video_asset
        calls: list = []
        video = _valid_mp4(tmp_path, "xyz.mp4")
        with patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=video), \
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch(f"{_RCP}._caption_video_asset", side_effect=lambda *a, **k: calls.append("caption")), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials",
                   side_effect=lambda *a, **k: calls.append("c2pa")), \
             patch(f"{_RCP}.update_db_post_video_url") as store_url:
            url = _store_video_asset(7, "https://runway.test/xyz.mp4", content="Hook line",
                                     user_id=3)
        assert calls == ["caption", "c2pa"]
        assert url and url.endswith("videos/runwayml/xyz.mp4")
        store_url.assert_called_once()

    def test_a_rejected_probe_is_never_captioned(self, tmp_path):
        from cqc_lem.app.run_content_plan import _store_video_asset
        with patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=_valid_mp4(tmp_path)), \
             patch(f"{_RCP}._accept_probed_video", return_value=False), \
             patch(f"{_RCP}._caption_video_asset") as caption:
            assert _store_video_asset(7, "https://runway.test/xyz.mp4") is None
        caption.assert_not_called()

    def test_the_posts_own_caption_text_is_what_gets_burned(self, tmp_path):
        from cqc_lem.app.run_content_plan import _store_video_asset
        with patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=_valid_mp4(tmp_path)), \
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials"), \
             patch(f"{_RCP}.update_db_post_video_url"), \
             patch(f"{_RCP}._caption_video_asset") as caption:
            _store_video_asset(7, "https://runway.test/xyz.mp4", content="Hook line", user_id=3)
        assert caption.call_args.args[2] == "Hook line"
        assert caption.call_args.kwargs["user_id"] == 3


class TestBirthPath:
    """`auto_create_weekly_content` is where a video post is BORN — the ordering matters most here."""

    def test_captions_run_between_the_probe_and_c2pa(self, monkeypatch, tmp_path):
        import cqc_lem.app.run_content_plan as rcp
        monkeypatch.setattr(f"{_RCP}.datetime", _MondayDatetime)
        calls: list = []
        video = _valid_mp4(tmp_path)
        with patch(f"{_RCP}.get_planned_posts_within_buffer",
                   return_value=[{"user_id": 1, "id": 42, "post_type": "video",
                                  "buyer_stage": "awareness"}]), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=("Hook line", "http://runway/clip.mp4")), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=video), \
             patch(f"{_RCP}._accept_probed_video",
                   side_effect=lambda *a, **k: calls.append("probe") or True), \
             patch(f"{_RCP}._caption_video_asset",
                   side_effect=lambda *a, **k: calls.append("caption")) as caption, \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials",
                   side_effect=lambda *a, **k: calls.append("c2pa")), \
             patch(f"{_RCP}.update_db_post_video_url") as upd_video, \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch.object(rcp, "AI_DISCLOSURE_ENABLED", False):
            rcp.auto_create_weekly_content(user_id=1)
        assert calls == ["probe", "caption", "c2pa"]
        # The caption is the POST's own text, and the file captioned is the one the URL points at.
        assert caption.call_args.args[1] == video
        assert caption.call_args.args[2] == "Hook line"
        assert "file_name=videos/runwayml/clip.mp4" in upd_video.call_args[0][1]

    def test_a_rejected_probe_is_never_captioned(self, monkeypatch, tmp_path):
        import cqc_lem.app.run_content_plan as rcp
        monkeypatch.setattr(f"{_RCP}.datetime", _MondayDatetime)
        with patch(f"{_RCP}.get_planned_posts_within_buffer",
                   return_value=[{"user_id": 1, "id": 42, "post_type": "video",
                                  "buyer_stage": "awareness"}]), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=("Hook line", "http://runway/clip.mp4")), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=_valid_mp4(tmp_path)), \
             patch(f"{_RCP}._accept_probed_video", return_value=False), \
             patch(f"{_RCP}._caption_video_asset") as caption, \
             patch(f"{_RCP}.update_db_post_video_url") as upd_video, \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=True), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None):
            rcp.auto_create_weekly_content(user_id=1)
        caption.assert_not_called()
        upd_video.assert_not_called()
