"""Unit tests for the stored-video measurement receipt — issue #1517.

The receipt is the only record of a shipped video's duration and aspect ratio once
`purge_post_assets` has deleted the MP4, so these cover what decides whether a reading exists at
all: a write that lands beside the file it describes, and a read that refuses to invent one.
"""

import json

import pytest

from cqc_lem.utilities import video_receipt as vr

pytestmark = pytest.mark.unit


class TestVideoReceiptPath:
    def test_shares_the_video_stem(self):
        assert vr.video_receipt_path("/assets/videos/runwayml/clip.mp4") == \
            "/assets/videos/runwayml/clip" + vr.VIDEO_RECEIPT_SUFFIX

    def test_no_video_no_path(self):
        assert vr.video_receipt_path(None) is None
        assert vr.video_receipt_path("   ") is None


class TestWriteVideoReceipt:
    def test_writes_exactly_the_measures_it_was_handed(self, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_text("fake")
        path = vr.write_video_receipt(str(video), 7, {
            "duration_seconds": 8, "aspect_ratio": "9:16", "asset_probe": "ok",
            "has_video_stream": True, "ignored": "extra"})
        assert path == str(tmp_path / ("clip" + vr.VIDEO_RECEIPT_SUFFIX))
        assert json.loads(open(path, encoding="utf-8").read()) == {
            "post_id": 7,
            "measures": {"duration_seconds": 8, "aspect_ratio": "9:16", "asset_probe": "ok",
                         "has_video_stream": True}}

    def test_an_unread_dimension_is_stored_null_not_zero(self, tmp_path):
        # A video recorded as "0 seconds" is indistinguishable from one nothing measured (#630).
        video = tmp_path / "clip.mp4"
        video.write_text("fake")
        path = vr.write_video_receipt(str(video), 7, {"asset_probe": "ok",
                                                      "has_video_stream": True})
        assert json.loads(open(path, encoding="utf-8").read())["measures"] == {
            "duration_seconds": None, "aspect_ratio": None, "asset_probe": "ok",
            "has_video_stream": True}

    def test_no_video_path_writes_nothing(self, tmp_path):
        assert vr.write_video_receipt(None, 7, {"asset_probe": "ok"}) is None


class TestReadVideoReceipt:
    def _write(self, tmp_path, payload):
        path = tmp_path / ("clip" + vr.VIDEO_RECEIPT_SUFFIX)
        path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
        return str(path)

    def test_reads_back_what_was_written(self, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_text("fake")
        measures = {"duration_seconds": 6, "aspect_ratio": "16:9", "asset_probe": "ok",
                    "has_video_stream": True}
        vr.write_video_receipt(str(video), 7, measures)
        assert vr.read_video_receipt(vr.video_receipt_path(str(video))) == measures

    def test_absent_receipt_reads_none(self, tmp_path):
        assert vr.read_video_receipt(str(tmp_path / "nope.probe.json")) is None
        assert vr.read_video_receipt(None) is None

    def test_a_broken_receipt_is_no_receipt(self, tmp_path):
        # Absent and broken answer the same because the caller's fallback — a live probe — is the
        # right move for both, and scoring half a parsed payload would not be.
        assert vr.read_video_receipt(self._write(tmp_path, "{truncated")) is None
        assert vr.read_video_receipt(self._write(tmp_path, ["not", "a", "receipt"])) is None
        assert vr.read_video_receipt(self._write(tmp_path, {"post_id": 7})) is None

    def test_a_receipt_with_no_probe_state_is_rejected(self, tmp_path):
        # `asset_probe` is what proves the payload is a reading rather than a shape that parses.
        path = self._write(tmp_path, {"post_id": 7, "measures": {"duration_seconds": 6}})
        assert vr.read_video_receipt(path) is None
        path = self._write(tmp_path, {"post_id": 7, "measures": {"asset_probe": "  "}})
        assert vr.read_video_receipt(path) is None

    def test_an_unreadable_file_never_raises(self, tmp_path, monkeypatch):
        path = self._write(tmp_path, {"post_id": 7, "measures": {"asset_probe": "ok"}})
        monkeypatch.setattr("builtins.open", _raise_os_error)
        assert vr.read_video_receipt(path) is None


def _raise_os_error(*args, **kwargs):
    raise OSError("permission denied")
