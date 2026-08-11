"""Unit tests for the asset-backfill safety net + missing-asset guard."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _db(fetchone=None, fetchall=None):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


class TestDbQueries:
    def test_missing_assets_query(self):
        rows = [(6, 1, 'video', 'awareness', 't'), (5, 1, 'carousel', 'awareness', 't')]
        conn, _ = _db(fetchall=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_unposted_posts_missing_assets
            assert get_unposted_posts_missing_assets() == rows

    def test_carousel_slides_getter(self):
        conn, _ = _db(fetchone={"carousel_slides": '["a"]'})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_carousel_slides
            assert get_post_carousel_slides(5) == '["a"]'


class TestMissingAssetGuard:
    def test_video_without_url_is_missing(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        assert _post_missing_required_asset(6, "video", None) is True
        assert _post_missing_required_asset(6, "video", "https://x.mp4") is False

    def test_carousel_slides_states(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        cases = [
            (None, True),
            ('[]', True),
            ('', True),
            ('["Just A Text Title", "Another Title"]', True),  # text slides count as missing now
            ('["https://x/assets?file_name=images/carousel/5/slide_01.png"]', False),
            ('["/app/assets/slide_01.png"]', False),
        ]
        for val, missing in cases:
            with patch("cqc_lem.utilities.db.get_post_carousel_slides", return_value=val):
                assert _post_missing_required_asset(5, "carousel", None) is missing

    def test_text_never_missing(self):
        from cqc_lem.app.run_content_plan import _post_missing_required_asset
        assert _post_missing_required_asset(4, "text", None) is False


class TestVideoAssetProbe:
    """Issue #1280: presence + parse probe before a video URL is accepted as the post's media."""

    def _make_valid_mp4(self, tmp_path, name="valid.mp4", size=112):
        p = tmp_path / name
        p.write_bytes(b'\x00\x00\x00 ftypisom' + b'\x00' * (size - 16))
        return p

    def test_zero_byte_file_fails(self, tmp_path):
        from cqc_lem.app.run_content_plan import _probe_video_file
        p = tmp_path / "empty.mp4"
        p.write_text("")
        ok, reason = _probe_video_file(str(p))
        assert ok is False
        assert "zero-byte" in reason

    def test_nonexistent_file_fails(self, tmp_path):
        from cqc_lem.app.run_content_plan import _probe_video_file
        ok, reason = _probe_video_file(str(tmp_path / "missing.mp4"))
        assert ok is False
        assert "not readable" in reason

    def test_wrong_content_type_fails(self, tmp_path):
        from cqc_lem.app.run_content_plan import _probe_video_file
        p = tmp_path / "wrong.bin"
        p.write_bytes(b"not an mp4 file" * 10)
        ok, reason = _probe_video_file(str(p))
        assert ok is False
        assert "ftyp" in reason

    def test_valid_mp4_head_passes(self, tmp_path):
        from cqc_lem.app.run_content_plan import _probe_video_file
        p = self._make_valid_mp4(tmp_path)
        ok, reason = _probe_video_file(str(p))
        assert ok is True
        assert reason == ""

    def test_valid_with_ffprobe_duration_passes(self, tmp_path):
        from types import SimpleNamespace

        from cqc_lem.app.run_content_plan import _probe_video_file
        p = self._make_valid_mp4(tmp_path)
        with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
             patch("subprocess.run", return_value=SimpleNamespace(returncode=0,
                                                                   stdout="duration=3.000000\n",
                                                                   stderr="")):
            ok, reason = _probe_video_file(str(p))
        assert ok is True
        assert reason == ""

    def test_ffprobe_zero_duration_fails(self, tmp_path):
        from types import SimpleNamespace

        from cqc_lem.app.run_content_plan import _probe_video_file
        p = self._make_valid_mp4(tmp_path)
        with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
             patch("subprocess.run", return_value=SimpleNamespace(returncode=0,
                                                                   stdout="duration=0.000000\n",
                                                                   stderr="")):
            ok, reason = _probe_video_file(str(p))
        assert ok is False
        assert "zero" in reason

    def test_ffprobe_unparseable_duration_fails(self, tmp_path):
        from types import SimpleNamespace

        from cqc_lem.app.run_content_plan import _probe_video_file
        p = self._make_valid_mp4(tmp_path)
        with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
             patch("subprocess.run", return_value=SimpleNamespace(returncode=0,
                                                                   stdout="garbage\n",
                                                                   stderr="")):
            ok, reason = _probe_video_file(str(p))
        assert ok is False
        assert "unparseable" in reason

    def test_store_video_asset_rejects_zero_byte_when_flag_on(self, tmp_path):
        from cqc_lem.app.run_content_plan import _store_video_asset
        p = tmp_path / "empty.mp4"
        p.write_text("")
        with patch("cqc_lem.app.run_content_plan.VIDEO_PROBE_ENABLED", True), \
             patch("cqc_lem.app.run_content_plan.create_folder_if_not_exists"), \
             patch("cqc_lem.app.run_content_plan.save_video_url_to_dir", return_value=str(p)), \
             patch("cqc_lem.app.run_content_plan.track_video_asset_probe") as track:
            with pytest.raises(RuntimeError, match="video asset probe failed"):
                _store_video_asset(9, "http://runway/clip.mp4")
        assert any(call.kwargs.get("probe_ok") is False for call in track.call_args_list)

    def test_store_video_asset_advisory_when_flag_off(self, tmp_path):
        from cqc_lem.app.run_content_plan import _store_video_asset
        p = tmp_path / "empty.mp4"
        p.write_text("")
        with patch("cqc_lem.app.run_content_plan.VIDEO_PROBE_ENABLED", False), \
             patch("cqc_lem.app.run_content_plan.create_folder_if_not_exists"), \
             patch("cqc_lem.app.run_content_plan.save_video_url_to_dir", return_value=str(p)), \
             patch("cqc_lem.app.run_content_plan.update_db_post_video_url") as upd, \
             patch("cqc_lem.app.run_content_plan.track_video_asset_probe") as track:
            url = _store_video_asset(9, "http://runway/clip.mp4")
        assert url is None
        upd.assert_not_called()
        assert any(call.kwargs.get("probe_ok") is False for call in track.call_args_list)

    def test_store_video_asset_accepts_valid_mp4(self, tmp_path):
        from cqc_lem.app.run_content_plan import _store_video_asset
        p = self._make_valid_mp4(tmp_path, size=112)
        with patch("cqc_lem.app.run_content_plan.create_folder_if_not_exists"), \
             patch("cqc_lem.app.run_content_plan.save_video_url_to_dir", return_value=str(p)), \
             patch("cqc_lem.app.run_content_plan.update_db_post_video_url") as upd, \
             patch("cqc_lem.app.run_content_plan.track_video_asset_probe") as track:
            url = _store_video_asset(9, "http://runway/clip.mp4")
        assert url is not None
        upd.assert_called_once()
        assert any(call.kwargs.get("probe_ok") is True for call in track.call_args_list)


class TestBackfillTask:
    def test_enqueues_regen_per_type(self):
        rows = [(6, 1, 'video', 'awareness', 't'),
                (5, 1, 'carousel', 'awareness', 't'),
                (4, 1, 'text', 'awareness', 't')]
        vid, car = MagicMock(), MagicMock()
        with patch("cqc_lem.utilities.db.get_unposted_posts_missing_assets", return_value=rows), \
             patch("cqc_lem.app.run_content_plan.regenerate_post_video_task", vid), \
             patch("cqc_lem.app.run_content_plan.regenerate_post_carousel_task", car):
            from cqc_lem.app.run_scheduler import auto_backfill_missing_assets
            result = auto_backfill_missing_assets()
        vid.apply_async.assert_called_once_with(kwargs={'post_id': 6})
        car.apply_async.assert_called_once_with(kwargs={'post_id': 5})
        assert "Queued 2" in result

    def test_no_missing_posts(self):
        with patch("cqc_lem.utilities.db.get_unposted_posts_missing_assets", return_value=[]):
            from cqc_lem.app.run_scheduler import auto_backfill_missing_assets
            assert "Queued 0" in auto_backfill_missing_assets()
