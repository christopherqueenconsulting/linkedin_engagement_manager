"""Post videos (issue #1443): the gate before a byte is stored, and what a URL may resolve to.

Two halves, deliberately not the same posture. Size and the ISO `ftyp` brand are deterministic, so a
file that is not a video LinkedIn would take is a 400 at upload. Duration, frame size and codec need
ffprobe, which is not installed everywhere this API runs, so that half FAILS OPEN — a probe that
cannot run accepts what the head check already proved, and a probe that CAN run and reports a
violation refuses.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_PV = "cqc_lem.utilities.post_video"


def _ftyp(brand: bytes = b"isom", payload_bytes: int = 200 * 1024) -> bytes:
    """A file that opens with a valid `ftyp` box of `brand`, padded to a believable size."""
    box = (24).to_bytes(4, "big") + b"ftyp" + brand + b"\x00" * 12
    return box + b"\x00" * max(0, payload_bytes - len(box))


def _write(tmp_path, data: bytes, name: str = "clip.mp4") -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _probe(duration="12.5", width=1280, height=720, codec="h264"):
    """A `subprocess.run` result carrying what ffprobe would print for that stream."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = json.dumps({
        "streams": [{"codec_name": codec, "width": width, "height": height}],
        "format": {"duration": duration},
    })
    return result


class TestInspectPostVideoFile:
    def test_accepts_a_normal_mp4(self, tmp_path):
        from cqc_lem.utilities.post_video import inspect_post_video_file
        path = _write(tmp_path, _ftyp())
        with patch(f"{_PV}.shutil.which", return_value="/usr/bin/ffprobe"), \
             patch(f"{_PV}.subprocess.run", return_value=_probe()):
            verdict = inspect_post_video_file(path)
        assert verdict.ok and verdict.extension == ".mp4"
        assert verdict.duration_seconds == 12.5 and verdict.codec == "h264"

    def test_a_quicktime_file_is_stored_as_mov(self, tmp_path):
        """The stored extension comes from the file's own brand, never the upload's name.

        That extension is what `determine_media_type` reads at publish to pick LinkedIn's share
        category, so a `.mp4` that is really QuickTime has to be corrected here.
        """
        from cqc_lem.utilities.post_video import inspect_post_video_file
        path = _write(tmp_path, _ftyp(b"qt  "), name="whatever.mp4")
        with patch(f"{_PV}.shutil.which", return_value="/usr/bin/ffprobe"), \
             patch(f"{_PV}.subprocess.run", return_value=_probe()):
            verdict = inspect_post_video_file(path)
        assert verdict.ok and verdict.extension == ".mov"

    def test_rejects_a_file_that_is_not_an_iso_media_container(self, tmp_path):
        from cqc_lem.utilities.post_video import inspect_post_video_file
        path = _write(tmp_path, b"GIF89a" + b"\x00" * (100 * 1024))
        verdict = inspect_post_video_file(path)
        assert not verdict.ok and "MP4 or MOV" in verdict.reason

    def test_rejects_a_container_brand_we_do_not_ship(self, tmp_path):
        from cqc_lem.utilities.post_video import inspect_post_video_file
        verdict = inspect_post_video_file(_write(tmp_path, _ftyp(b"heic")))
        assert not verdict.ok and "MP4 or MOV" in verdict.reason

    def test_rejects_a_truncated_upload(self, tmp_path):
        from cqc_lem.utilities.post_video import inspect_post_video_file
        verdict = inspect_post_video_file(_write(tmp_path, _ftyp(payload_bytes=1024)))
        assert not verdict.ok and "too small" in verdict.reason

    def test_rejects_a_file_over_the_cap(self, tmp_path):
        from cqc_lem.utilities.post_video import MAX_POST_VIDEO_BYTES, inspect_post_video_file
        path = _write(tmp_path, _ftyp())
        with patch(f"{_PV}.os.path.getsize", return_value=MAX_POST_VIDEO_BYTES + 1):
            verdict = inspect_post_video_file(path)
        assert not verdict.ok and "larger than" in verdict.reason

    def test_a_path_that_is_not_there_is_not_a_video(self, tmp_path):
        from cqc_lem.utilities.post_video import inspect_post_video_file
        verdict = inspect_post_video_file(str(tmp_path / "gone.mp4"))
        assert not verdict.ok and "readable video" in verdict.reason

    @pytest.mark.parametrize("probe_kwargs,expected", [
        ({"duration": "1.2"}, "at least 3 seconds"),
        ({"duration": "1801"}, "shorter than 15 minutes"),
        ({"width": 160, "height": 90}, "too small"),
        ({"codec": "prores"}, "H.264 or HEVC"),
    ])
    def test_a_measured_violation_is_refused_with_the_reason(self, tmp_path, probe_kwargs,
                                                             expected):
        from cqc_lem.utilities.post_video import inspect_post_video_file
        path = _write(tmp_path, _ftyp())
        with patch(f"{_PV}.shutil.which", return_value="/usr/bin/ffprobe"), \
             patch(f"{_PV}.subprocess.run", return_value=_probe(**probe_kwargs)):
            verdict = inspect_post_video_file(path)
        assert not verdict.ok and expected in verdict.reason

    def test_a_container_with_no_readable_duration_is_refused(self, tmp_path):
        from cqc_lem.utilities.post_video import inspect_post_video_file
        path = _write(tmp_path, _ftyp())
        with patch(f"{_PV}.shutil.which", return_value="/usr/bin/ffprobe"), \
             patch(f"{_PV}.subprocess.run", return_value=_probe(duration="N/A")):
            verdict = inspect_post_video_file(path)
        assert not verdict.ok and "readable video" in verdict.reason

    def test_no_ffprobe_accepts_what_the_head_check_proved(self, tmp_path):
        """Fail OPEN, like `_probe_video_file` (issue #1280).

        The API container does not necessarily ship ffmpeg, and refusing every upload there would
        take the feature down over a measurement we could not take. Nothing is claimed about the
        stream in that case — the measured fields stay None rather than reading as zero.
        """
        from cqc_lem.utilities.post_video import inspect_post_video_file
        path = _write(tmp_path, _ftyp())
        with patch(f"{_PV}.shutil.which", return_value=None), \
             patch(f"{_PV}.subprocess.run") as ran:
            verdict = inspect_post_video_file(path)
        assert verdict.ok and verdict.duration_seconds is None and verdict.codec is None
        ran.assert_not_called()

    def test_an_ffprobe_that_will_not_run_is_advisory_not_a_refusal(self, tmp_path):
        from cqc_lem.utilities.post_video import inspect_post_video_file
        path = _write(tmp_path, _ftyp())
        failed = MagicMock(returncode=1, stdout="")
        with patch(f"{_PV}.shutil.which", return_value="/usr/bin/ffprobe"), \
             patch(f"{_PV}.subprocess.run", return_value=failed):
            assert inspect_post_video_file(path).ok


class TestSaveAndRemove:
    def _stored(self, tmp_path, user_id=7):
        from cqc_lem.utilities.post_video import save_post_video_file
        source = _write(tmp_path, _ftyp(), name="upload.tmp")
        with patch(f"{_PV}.assets_dir", str(tmp_path / "assets")), \
             patch(f"{_PV}.shutil.which", return_value=None):
            return save_post_video_file(user_id, source), source

    def test_an_upload_lands_under_the_authors_preview_dir(self, tmp_path):
        url, source = self._stored(tmp_path)
        assert "file_name=videos/post_previews/7/" in url
        assert os.listdir(tmp_path / "assets" / "videos" / "post_previews" / "7")
        # Moved, not copied: there is no reason to hold two copies of a 200 MB upload.
        assert not os.path.exists(source)

    def test_stored_names_are_unpredictable(self, tmp_path):
        """/api/assets is public, so a guessable name would expose an unpublished video."""
        first, _ = self._stored(tmp_path)
        second, _ = self._stored(tmp_path)
        assert first != second

    def test_a_rejected_upload_writes_nothing(self, tmp_path):
        from cqc_lem.utilities.post_video import PostVideoRejected, save_post_video_file
        source = _write(tmp_path, b"not a video", name="upload.tmp")
        with patch(f"{_PV}.assets_dir", str(tmp_path / "assets")):
            with pytest.raises(PostVideoRejected):
                save_post_video_file(7, source)
        assert not (tmp_path / "assets").exists()

    def test_remove_deletes_the_file_behind_a_stored_url(self, tmp_path):
        from cqc_lem.utilities.post_video import post_video_abs_path, remove_post_video_file
        url, _ = self._stored(tmp_path)
        with patch(f"{_PV}.assets_dir", str(tmp_path / "assets")):
            assert post_video_abs_path(url)
            assert remove_post_video_file(url) is True
            assert post_video_abs_path(url) is None

    def test_remove_is_a_no_op_for_a_url_we_never_issued(self, tmp_path):
        from cqc_lem.utilities.post_video import remove_post_video_file
        with patch(f"{_PV}.assets_dir", str(tmp_path)):
            assert remove_post_video_file("https://example.com/someone-elses.mp4") is False
            assert remove_post_video_file(None) is False


class TestUrlResolution:
    def test_a_traversal_path_never_resolves_outside_assets(self, tmp_path):
        from cqc_lem.utilities.post_video import post_video_abs_path
        outside = tmp_path / "secret.mp4"
        outside.write_bytes(b"mp4")
        assets = tmp_path / "assets"
        assets.mkdir()
        url = "https://api.test/api/assets?file_name=videos/post_previews/../../secret.mp4"
        with patch(f"{_PV}.assets_dir", str(assets)):
            assert post_video_abs_path(url) is None

    def test_ownership_accepts_only_this_users_preview(self):
        from cqc_lem.utilities.post_video import owns_post_video_url
        mine = "https://api.test/api/assets?file_name=videos/post_previews/7/vid_a.mp4"
        theirs = "https://api.test/api/assets?file_name=videos/post_previews/8/vid_a.mp4"
        assert owns_post_video_url(7, mine) is True
        assert owns_post_video_url(7, theirs) is False
        assert owns_post_video_url(7, "https://evil.test/x.mp4") is False
        assert owns_post_video_url(7, None) is False

    def test_a_video_preview_is_not_an_image_preview(self):
        """The halves stay separate so `/schedule_post/`'s image gate cannot be handed an MP4."""
        from cqc_lem.utilities.post_image import owns_post_image_url
        from cqc_lem.utilities.post_video import owns_post_video_url
        video = "https://api.test/api/assets?file_name=videos/post_previews/7/vid_a.mp4"
        image = "https://api.test/api/assets?file_name=images/post_previews/7/img_a.png"
        assert owns_post_image_url(7, video) is False
        assert owns_post_video_url(7, image) is False


class TestEitherKindResolvers:
    """The union the group post reads — it is the one surface that takes both kinds."""

    _IMAGE = "https://api.test/api/assets?file_name=images/post_previews/7/img_a.png"
    _VIDEO = "https://api.test/api/assets?file_name=videos/post_previews/7/vid_a.mp4"

    def test_ownership_covers_both_halves(self):
        from cqc_lem.utilities.post_video import owns_post_media_url
        assert owns_post_media_url(7, self._IMAGE) is True
        assert owns_post_media_url(7, self._VIDEO) is True
        assert owns_post_media_url(8, self._VIDEO) is False
        assert owns_post_media_url(7, "https://evil.test/x.mp4") is False

    def test_the_path_resolver_finds_a_file_of_either_kind(self, tmp_path):
        from cqc_lem.utilities.post_video import post_media_abs_path
        assets = tmp_path / "assets"
        for relative in ("images/post_previews/7/img_a.png", "videos/post_previews/7/vid_a.mp4"):
            target = assets / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
        with patch(f"{_PV}.assets_dir", str(assets)), \
             patch("cqc_lem.utilities.post_image.assets_dir", str(assets)):
            assert post_media_abs_path(self._IMAGE)
            assert post_media_abs_path(self._VIDEO)
            assert post_media_abs_path("https://evil.test/x.mp4") is None

    def test_removal_covers_both_halves(self, tmp_path):
        from cqc_lem.utilities.post_video import post_media_abs_path, remove_post_media_file
        assets = tmp_path / "assets"
        for relative in ("images/post_previews/7/img_a.png", "videos/post_previews/7/vid_a.mp4"):
            target = assets / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
        with patch(f"{_PV}.assets_dir", str(assets)), \
             patch("cqc_lem.utilities.post_image.assets_dir", str(assets)):
            assert remove_post_media_file(self._VIDEO) is True
            assert remove_post_media_file(self._IMAGE) is True
            assert post_media_abs_path(self._VIDEO) is None
            assert remove_post_media_file(None) is False
