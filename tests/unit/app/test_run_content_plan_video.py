"""Unit tests for recording a stored video's asset measures — issue #1517.

`purge_post_assets` (#148) deletes the MP4 at publish and the nightly quality beat scores content
that has already shipped, so store time is the ONLY moment the measurement and the file exist
together. What these pin is that moment: the measures are taken off the bytes that ship (after the
caption burn and C2PA re-writes, before the URL is persisted), on BOTH store paths, and nothing is
ever recorded that the probe did not read.

The receipt format itself is covered by `tests/unit/utilities/test_video_receipt.py`.
"""
import os
from datetime import datetime as _real_datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from cqc_lem.utilities import content_quality as cq
from cqc_lem.utilities.content_quality import VIDEO_PROBE_OK, VIDEO_PROBE_UNREADABLE
from cqc_lem.utilities.video_receipt import read_video_receipt, video_receipt_path

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_CQ = "cqc_lem.utilities.content_quality"


class _MondayDatetime(_real_datetime):
    @classmethod
    def now(cls, tz=None):
        return _real_datetime(2024, 1, 8, 12, 0)  # Monday


def _valid_mp4(tmp_path, name: str = "clip.mp4", size: int = 72) -> str:
    p = tmp_path / name
    p.write_bytes(b'\x00\x00\x00 ftypisom' + b'\x00' * (size - 16))
    return str(p)


def _measures(**kw):
    base = {"duration_seconds": 8, "aspect_ratio": "9:16", "asset_probe": VIDEO_PROBE_OK,
            "has_video_stream": True}
    base.update(kw)
    return base


class TestRecordVideoAssetMeasures:
    def test_records_what_the_probe_read(self, tmp_path):
        from cqc_lem.app.run_content_plan import _record_video_asset_measures
        video = _valid_mp4(tmp_path)
        with patch(f"{_CQ}.probe_video_asset", return_value=_measures()) as probe:
            path = _record_video_asset_measures(7, video, user_id=3)
        assert probe.call_args.args[0] == video
        assert path == video_receipt_path(video)
        assert read_video_receipt(path) == _measures()

    def test_an_unread_probe_records_nothing_and_warns(self, tmp_path):
        # Unmeasured is never zero (#630): a receipt saying "0 seconds, ok" would read as a
        # measured clip forever, and it would be wrong.
        from cqc_lem.app.run_content_plan import _record_video_asset_measures
        video = _valid_mp4(tmp_path)
        unread = _measures(duration_seconds=None, aspect_ratio=None,
                           asset_probe=VIDEO_PROBE_UNREADABLE, has_video_stream=False)
        with patch(f"{_CQ}.probe_video_asset", return_value=unread), \
             patch(f"{_RCP}.log_warning") as warn:
            assert _record_video_asset_measures(7, video, user_id=3) is None
        assert read_video_receipt(video_receipt_path(video)) is None
        # The file just passed `_accept_probed_video`, so a probe that cannot read it means no
        # video post carries measures until someone looks.
        assert warn.call_count == 1

    def test_a_raising_probe_never_reaches_the_caller(self, tmp_path):
        from cqc_lem.app.run_content_plan import _record_video_asset_measures
        video = _valid_mp4(tmp_path)
        with patch(f"{_CQ}.probe_video_asset", side_effect=RuntimeError("ffprobe")), \
             patch(f"{_RCP}.log_warning") as warn:
            assert _record_video_asset_measures(7, video) is None
        assert warn.call_count == 1

    def test_an_unwritable_receipt_never_reaches_the_caller(self, tmp_path):
        # Telemetry never costs a user their video — it is already on disk by this point.
        from cqc_lem.app.run_content_plan import _record_video_asset_measures
        video = _valid_mp4(tmp_path)
        with patch(f"{_CQ}.probe_video_asset", return_value=_measures()), \
             patch("cqc_lem.utilities.video_receipt.open",
                   side_effect=OSError("read-only volume"), create=True), \
             patch(f"{_RCP}.log_warning") as warn:
            assert _record_video_asset_measures(7, video) is None
        assert warn.call_count == 1


class TestStoreVideoAssetRecordsMeasures:
    def test_measured_after_captions_and_signing_before_the_url_is_stored(self, tmp_path):
        """Both rewrite the file, so anything measured earlier describes bytes nobody ships."""
        from cqc_lem.app.run_content_plan import _store_video_asset
        calls: list = []
        video = _valid_mp4(tmp_path, "xyz.mp4")
        with patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=video), \
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch(f"{_RCP}._caption_video_asset",
                   side_effect=lambda *a, **k: calls.append("caption")), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials",
                   side_effect=lambda *a, **k: calls.append("c2pa")), \
             patch(f"{_RCP}._record_video_asset_measures",
                   side_effect=lambda *a, **k: calls.append("measure")) as measure, \
             patch(f"{_RCP}.update_db_post_video_url",
                   side_effect=lambda *a, **k: calls.append("store_url")):
            _store_video_asset(7, "https://runway.test/xyz.mp4", content="Hook line", user_id=3)
        assert calls == ["caption", "c2pa", "measure", "store_url"]
        assert measure.call_args.args[1] == video
        assert measure.call_args.kwargs["user_id"] == 3

    def test_a_rejected_probe_records_no_measures(self, tmp_path):
        from cqc_lem.app.run_content_plan import _store_video_asset
        with patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=_valid_mp4(tmp_path)), \
             patch(f"{_RCP}._accept_probed_video", return_value=False), \
             patch(f"{_RCP}._record_video_asset_measures") as measure:
            assert _store_video_asset(7, "https://runway.test/xyz.mp4") is None
        measure.assert_not_called()


class TestTheStoredUrlResolvesToTheRecording:
    """The JOIN the two halves each assume and neither pins on its own.

    The writer puts a receipt beside the file it was handed; the reader resolves one from
    `posts.video_url`. Both are covered separately, so a change to the store directory or to the
    `/api/assets` URL shape would silently put the receipt somewhere the scorer never looks — every
    shipped video back to reading `missing`, with both files still green. This runs the real store
    directory, the real URL builder, the real receipt write and the real resolver against each
    other, with only the download and the ffprobe call mocked.
    """

    def test_the_url_persisted_at_store_time_reads_back_the_recorded_measures(self, tmp_path,
                                                                              monkeypatch):
        import cqc_lem.app.run_content_plan as rcp
        stored: dict = {}
        monkeypatch.setattr(rcp, "assets_dir", str(tmp_path))
        monkeypatch.setattr(cq, "assets_dir", str(tmp_path))
        with patch(f"{_RCP}.save_video_url_to_dir",
                   side_effect=lambda url, directory: _valid_mp4(Path(directory))), \
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch(f"{_RCP}._caption_video_asset"), \
             patch(f"{_CQ}.probe_video_asset", return_value=_measures()), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials"), \
             patch(f"{_RCP}.update_db_post_video_url",
                   side_effect=lambda post_id, url: stored.update(url=url)):
            api_url = rcp._store_video_asset(7, "https://runway.test/clip.mp4")

        assert api_url == stored["url"]
        # purge_post_assets (#148) at publish: the MP4 goes, the receipt stays.
        os.remove(str(tmp_path / "videos" / "runwayml" / "clip.mp4"))
        result = cq.score_video_asset(video_url=api_url)
        assert result["video_duration_seconds"] == 8
        assert result["video_aspect_ratio"] == "9:16"
        assert result["video_asset_probe"] == VIDEO_PROBE_OK
        assert result["video_render_ok"] is True


class TestBirthPathRecordsMeasures:
    """`auto_create_weekly_content` is where a video post is BORN — most shipped video comes from here."""

    def test_the_stored_video_is_measured_before_its_url_is_persisted(self, monkeypatch, tmp_path):
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
             patch(f"{_RCP}._accept_probed_video", return_value=True), \
             patch(f"{_RCP}._caption_video_asset",
                   side_effect=lambda *a, **k: calls.append("caption")), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials",
                   side_effect=lambda *a, **k: calls.append("c2pa")), \
             patch(f"{_RCP}._record_video_asset_measures",
                   side_effect=lambda *a, **k: calls.append("measure")) as measure, \
             patch(f"{_RCP}.update_db_post_video_url",
                   side_effect=lambda *a, **k: calls.append("store_url")), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch.object(rcp, "AI_DISCLOSURE_ENABLED", False):
            rcp.auto_create_weekly_content(user_id=1)
        assert calls == ["caption", "c2pa", "measure", "store_url"]
        assert measure.call_args.args[0] == 42
        assert measure.call_args.args[1] == video

    def test_a_rejected_probe_records_no_measures(self, monkeypatch, tmp_path):
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
             patch(f"{_RCP}._caption_video_asset"), \
             patch(f"{_RCP}._record_video_asset_measures") as measure, \
             patch(f"{_RCP}.update_db_post_video_url"), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=True), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None):
            rcp.auto_create_weekly_content(user_id=1)
        measure.assert_not_called()
