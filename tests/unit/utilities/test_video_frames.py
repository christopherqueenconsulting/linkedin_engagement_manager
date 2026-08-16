"""Pins `utilities/video_frames.py` — the retained keyframes rubric rows R1/R8 are graded on.

The failure mode that matters is a frame that lies: one reported without ffmpeg having written it,
a midpoint invented for a clip whose duration was never read, or a sidecar named so that
`purge_post_assets` takes it with the MP4 — which would put the audit back where #1363 started.
"""

import pathlib

import pytest

from cqc_lem.utilities import video_frames
from cqc_lem.utilities.video_frames import (
    OPEN_FRAME_SECONDS,
    extract_frames,
    frame_timestamps,
    keyframe_path,
    retain_keyframes,
    retained_keyframes,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def clip(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return video


@pytest.fixture
def ffmpeg(monkeypatch):
    """An ffmpeg on PATH, writing a non-empty JPEG wherever it is pointed."""
    monkeypatch.setattr(video_frames.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        pathlib.Path(cmd[-1]).write_bytes(b"jpeg-bytes")
        return None

    monkeypatch.setattr(video_frames.subprocess, "run", fake_run)
    return fake_run


class TestFrameTimestamps:
    def test_an_unread_duration_only_yields_the_opening_frame(self):
        assert frame_timestamps(None) == [("open", OPEN_FRAME_SECONDS)]
        assert frame_timestamps("n/a") == [("open", OPEN_FRAME_SECONDS)]

    def test_a_clip_shorter_than_the_offsets_only_yields_the_opening_frame(self):
        assert frame_timestamps(0.5) == [("open", OPEN_FRAME_SECONDS)]

    def test_a_normal_clip_yields_open_mid_and_close(self):
        assert frame_timestamps(6) == [("open", 0.5), ("mid", 3.0), ("close", 5.7)]


class TestKeyframePath:
    def test_the_sidecar_shares_the_stem_but_is_never_the_mp4(self, tmp_path):
        video = str(tmp_path / "videos" / "runwayml" / "clip.mp4")
        path = keyframe_path(video, "open")
        assert path.endswith(".frame-open.jpg")
        # The purge removes the exact `.mp4` named by posts.video_url — this must not be it.
        assert path != video and not path.endswith(".mp4")

    def test_no_video_is_no_path(self):
        assert keyframe_path(None, "open") is None
        assert keyframe_path("   ", "open") is None


class TestExtractFrames:
    def test_no_ffmpeg_writes_nothing(self, clip, tmp_path, monkeypatch):
        monkeypatch.setattr(video_frames.shutil, "which", lambda name: None)
        assert extract_frames(str(clip), 6, lambda label: str(tmp_path / f"{label}.jpg")) == []

    def test_a_missing_asset_writes_nothing(self, tmp_path, ffmpeg):
        out = lambda label: str(tmp_path / f"{label}.jpg")  # noqa: E731
        assert extract_frames(str(tmp_path / "gone.mp4"), 6, out) == []
        assert extract_frames(None, 6, out) == []

    def test_writes_one_frame_per_timestamp(self, clip, tmp_path, ffmpeg):
        frames = extract_frames(str(clip), 6, lambda label: str(tmp_path / f"{label}.jpg"))
        assert [pathlib.Path(f).name for f in frames] == ["open.jpg", "mid.jpg", "close.jpg"]

    def test_a_destination_of_none_skips_that_frame(self, clip, tmp_path, ffmpeg):
        frames = extract_frames(str(clip), 6,
                                lambda label: None if label == "mid" else str(tmp_path / f"{label}.jpg"))
        assert [pathlib.Path(f).name for f in frames] == ["open.jpg", "close.jpg"]

    def test_an_empty_output_is_not_reported_as_a_frame(self, clip, tmp_path, monkeypatch):
        monkeypatch.setattr(video_frames.shutil, "which", lambda name: "/usr/bin/ffmpeg")
        monkeypatch.setattr(video_frames.subprocess, "run",
                            lambda cmd, **kwargs: pathlib.Path(cmd[-1]).write_bytes(b""))
        assert extract_frames(str(clip), None, lambda label: str(tmp_path / f"{label}.jpg")) == []

    def test_an_ffmpeg_crash_costs_one_frame_not_the_run(self, clip, tmp_path, monkeypatch):
        monkeypatch.setattr(video_frames.shutil, "which", lambda name: "/usr/bin/ffmpeg")

        def fake_run(cmd, **kwargs):
            if cmd[-1].endswith("mid.jpg"):
                raise OSError("ffmpeg died")
            pathlib.Path(cmd[-1]).write_bytes(b"jpeg-bytes")

        monkeypatch.setattr(video_frames.subprocess, "run", fake_run)
        frames = extract_frames(str(clip), 6, lambda label: str(tmp_path / f"{label}.jpg"))
        assert [pathlib.Path(f).name for f in frames] == ["open.jpg", "close.jpg"]


class TestRetainKeyframes:
    def test_frames_land_beside_the_video_and_read_back(self, clip, ffmpeg):
        written = retain_keyframes(str(clip), 6)
        assert [pathlib.Path(p).name for p in written] == ["clip.frame-open.jpg",
                                                           "clip.frame-mid.jpg",
                                                           "clip.frame-close.jpg"]
        assert retained_keyframes(str(clip)) == [("open", written[0]), ("mid", written[1]),
                                                 ("close", written[2])]

    def test_they_survive_the_publish_time_purge_of_the_mp4(self, clip, ffmpeg):
        # `purge_post_assets` removes only the exact `.mp4`; that is the whole reason these are
        # sidecars rather than a directory of their own.
        retain_keyframes(str(clip), 6)
        clip.unlink()
        assert [label for label, _ in retained_keyframes(str(clip))] == ["open", "mid", "close"]

    def test_an_unmeasured_clip_retains_the_opening_frame_only(self, clip, ffmpeg):
        assert [label for label, _ in
                retained_keyframes(str(clip)) or []] == []
        retain_keyframes(str(clip), None)
        assert [label for label, _ in retained_keyframes(str(clip))] == ["open"]

    def test_nothing_retained_is_reported_as_nothing(self, clip, monkeypatch):
        monkeypatch.setattr(video_frames.shutil, "which", lambda name: None)
        assert retain_keyframes(str(clip), 6) == []
        assert retained_keyframes(str(clip)) == []

    def test_an_empty_sidecar_is_not_a_retained_frame(self, clip):
        pathlib.Path(keyframe_path(str(clip), "open")).write_bytes(b"")
        assert retained_keyframes(str(clip)) == []
