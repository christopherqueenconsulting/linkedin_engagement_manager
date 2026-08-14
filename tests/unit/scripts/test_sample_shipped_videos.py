"""Pins scripts/sample_shipped_videos.py — the #1363 shipped-video corpus sampler.

The script produces the scorecard `docs/content-quality-audits/video.md` is missing, so the failure
mode that matters is a confidently wrong measurement: a clip whose duration was never read counted
as in-band, a row with no readable asset counted as graded, or a frame cited for a clip ffmpeg never
wrote.
"""

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/sample_shipped_videos.py")


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("sample_shipped_videos", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _post(post_id, *, post_type="video", content="A real hook line about pricing.",
          scheduled_time="2026-08-01 09:00:00"):
    return {"id": post_id, "content": content, "post_type": post_type,
            "scheduled_time": scheduled_time, "status": "posted"}


def _probe(duration=6, ratio="9:16", probe="ok", render_ok=True, tier="gen4_turbo"):
    return {"video_render_ok": render_ok, "video_model_tier": tier,
            "video_duration_seconds": duration, "video_aspect_ratio": ratio,
            "video_asset_probe": probe}


class TestVideoPosts:
    def test_keeps_only_video_posts_that_have_a_body(self, tool):
        rows = tool.video_posts([_post(1), _post(2, post_type="text"), _post(3, content="  ")])
        assert [r["id"] for r in rows] == [1]

    def test_returns_newest_first_and_respects_the_limit(self, tool):
        rows = tool.video_posts([_post(1, scheduled_time="2026-07-01 09:00:00"),
                                 _post(2, scheduled_time="2026-08-05 09:00:00"),
                                 _post(3, scheduled_time="2026-08-03 09:00:00")], limit=2)
        assert [r["id"] for r in rows] == [2, 3]

    def test_a_failed_read_samples_nothing(self, tool):
        # `get_posted_posts` answers None when the read failed — never an exception here.
        assert tool.video_posts(None) == []


class TestFrameTimestamps:
    def test_an_unread_duration_only_yields_the_opening_frame(self, tool):
        assert tool.frame_timestamps(None) == [("open", tool.OPEN_FRAME_SECONDS)]
        assert tool.frame_timestamps("n/a") == [("open", tool.OPEN_FRAME_SECONDS)]

    def test_a_clip_shorter_than_the_offsets_only_yields_the_opening_frame(self, tool):
        assert tool.frame_timestamps(0.5) == [("open", tool.OPEN_FRAME_SECONDS)]

    def test_a_normal_clip_yields_open_mid_and_close(self, tool):
        assert tool.frame_timestamps(6) == [("open", 0.5), ("mid", 3.0), ("close", 5.7)]


class TestExtractFrames:
    def test_no_ffmpeg_writes_nothing(self, tool, tmp_path, monkeypatch):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        monkeypatch.setattr(tool.shutil, "which", lambda name: None)
        assert tool.extract_frames(str(video), str(tmp_path / "frames"), "post1", 6) == []

    def test_a_missing_asset_writes_nothing(self, tool, tmp_path, monkeypatch):
        monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")
        assert tool.extract_frames(str(tmp_path / "gone.mp4"), str(tmp_path), "post1", 6) == []
        assert tool.extract_frames(None, str(tmp_path), "post1", 6) == []

    def test_writes_one_frame_per_timestamp(self, tool, tmp_path, monkeypatch):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")

        def fake_run(cmd, **kwargs):
            pathlib.Path(cmd[-1]).write_bytes(b"jpeg-bytes")
            return None

        monkeypatch.setattr(tool.subprocess, "run", fake_run)
        frames = tool.extract_frames(str(video), str(tmp_path / "frames"), "post7", 6)
        assert [pathlib.Path(f).name for f in frames] == ["post7_open.jpg", "post7_mid.jpg",
                                                          "post7_close.jpg"]

    def test_an_empty_output_is_not_reported_as_a_frame(self, tool, tmp_path, monkeypatch):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")

        def fake_run(cmd, **kwargs):
            pathlib.Path(cmd[-1]).write_bytes(b"")
            return None

        monkeypatch.setattr(tool.subprocess, "run", fake_run)
        assert tool.extract_frames(str(video), str(tmp_path), "post7", None) == []

    def test_an_ffmpeg_crash_costs_one_frame_not_the_run(self, tool, tmp_path, monkeypatch):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/bin/ffmpeg")

        def fake_run(cmd, **kwargs):
            if cmd[-1].endswith("_mid.jpg"):
                raise OSError("ffmpeg died")
            pathlib.Path(cmd[-1]).write_bytes(b"jpeg-bytes")
            return None

        monkeypatch.setattr(tool.subprocess, "run", fake_run)
        frames = tool.extract_frames(str(video), str(tmp_path), "post7", 6)
        assert [pathlib.Path(f).name for f in frames] == ["post7_open.jpg", "post7_close.jpg"]


class TestSampleReport:
    def test_scores_the_body_and_the_asset_in_one_row(self, tool, monkeypatch):
        monkeypatch.setattr(tool, "score_video_asset", lambda **kwargs: _probe())
        row = tool.sample_report(_post(11), "http://x/api/assets?file_name=videos/runwayml/a.mp4",
                                 captions={"caption_text": "A real hook line",
                                           "caption_srt_url": "http://x/a.srt"},
                                 frames=["docs/f.jpg"], user_id=1)
        assert row["post_id"] == 11 and row["user_id"] == 1
        assert row["body_available"] is True and row["asset_available"] is True
        assert row["duration_in_band"] is True
        assert row["captioned"] is True and row["caption_srt"] is True
        assert row["frames"] == ["docs/f.jpg"]
        # The body measures come from the same scorer the nightly beat uses.
        assert row["surface"] == tool.SURFACE_POST and row["hook_chars"] is not None

    def test_an_unreadable_asset_is_not_gradable(self, tool, monkeypatch):
        monkeypatch.setattr(tool, "score_video_asset",
                            lambda **kwargs: _probe(duration=None, ratio=None,
                                                    probe="unreadable", render_ok=False))
        row = tool.sample_report(_post(12), "http://x/api/assets?file_name=videos/runwayml/a.mp4")
        assert row["asset_available"] is False
        # Unmeasured duration is None — never scored as out of band.
        assert row["duration_in_band"] is None
        assert row["captioned"] is False

    def test_the_duration_band_is_inclusive_at_both_ends(self, tool, monkeypatch):
        low, high = tool.DURATION_BAND
        for duration, expected in ((low, True), (high, True), (high + 1, False), (low - 1, False)):
            monkeypatch.setattr(tool, "score_video_asset",
                                lambda duration=duration, **kwargs: _probe(duration=duration))
            row = tool.sample_report(_post(13), "http://x/api/assets?file_name=videos/x.mp4")
            assert row["duration_in_band"] is expected


class TestSummarize:
    def _row(self, post_id, **overrides):
        row = {"post_id": post_id, "body_available": True, "asset_available": True,
               "duration_in_band": True, "captioned": True, "hook_within_budget": True,
               "slop_hard": 0, "video_aspect_ratio": "9:16", "video_model_tier": "gen4_turbo",
               "video_asset_probe": "ok", "frames": [], "shipped_on": "2026-08-01"}
        row.update(overrides)
        return row

    def test_only_rows_with_a_body_and_an_asset_are_gradable(self, tool):
        summary = tool.summarize([self._row(1),
                                  self._row(2, asset_available=False, video_asset_probe="missing"),
                                  self._row(3, body_available=False)])
        assert summary["sampled"] == 3
        assert summary["gradable"] == 1
        assert summary["asset_probes"] == {"ok": 2, "missing": 1}

    def test_a_corpus_under_the_minimum_is_flagged_as_insufficient(self, tool):
        assert tool.summarize([self._row(i) for i in range(tool.MIN_CORPUS - 1)])[
            "sufficient_corpus"] is False
        assert tool.summarize([self._row(i) for i in range(tool.MIN_CORPUS)])[
            "sufficient_corpus"] is True

    def test_unmeasured_clips_leave_the_band_denominator(self, tool):
        summary = tool.summarize([self._row(1), self._row(2, duration_in_band=False),
                                  self._row(3, duration_in_band=None)])
        assert summary["duration_measured"] == 2
        assert summary["duration_in_band"] == 1
        assert summary["duration_in_band_rate"] == 0.5

    def test_an_empty_corpus_reports_no_rate_rather_than_zero(self, tool):
        summary = tool.summarize([])
        assert summary["duration_in_band_rate"] is None
        assert summary["sufficient_corpus"] is False

    def test_collects_every_written_frame_path(self, tool):
        summary = tool.summarize([self._row(1, frames=["a.jpg", "b.jpg"]),
                                  self._row(2, frames=["c.jpg"])])
        assert summary["frames"] == ["a.jpg", "b.jpg", "c.jpg"]

    def test_renders_without_raising_on_an_empty_corpus(self, tool):
        assert "NOT ENOUGH" in tool._render(tool.summarize([]))


class TestCollect:
    def test_reads_through_the_facade_and_samples_newest_first(self, tool, monkeypatch):
        from cqc_lem.utilities import db

        posts = [_post(1, scheduled_time="2026-07-01 09:00:00"),
                 _post(2, scheduled_time="2026-08-05 09:00:00"),
                 _post(3, post_type="text", scheduled_time="2026-08-06 09:00:00")]
        monkeypatch.setattr(db, "get_posted_posts", lambda user_id: posts)
        monkeypatch.setattr(db, "get_post_video_url",
                            lambda post_id: f"http://x/api/assets?file_name=videos/runwayml/{post_id}.mp4")
        monkeypatch.setattr(db, "get_post_captions",
                            lambda post_id: {"caption_text": "hook", "caption_srt_url": None})
        monkeypatch.setattr(tool, "score_video_asset", lambda **kwargs: _probe())
        calls = []
        monkeypatch.setattr(tool, "extract_frames",
                            lambda *args, **kwargs: calls.append(args) or [])

        summary = tool.collect([1], limit=10, frames_dir="/tmp/frames")
        assert [r["post_id"] for r in summary["per_post"]] == [2, 1]
        assert summary["gradable"] == 2
        assert len(calls) == 2

    def test_no_frames_probes_without_writing_images(self, tool, monkeypatch):
        from cqc_lem.utilities import db

        monkeypatch.setattr(db, "get_posted_posts", lambda user_id: [_post(1)])
        monkeypatch.setattr(db, "get_post_video_url", lambda post_id: "http://x/a.mp4")
        monkeypatch.setattr(db, "get_post_captions",
                            lambda post_id: {"caption_text": None, "caption_srt_url": None})
        monkeypatch.setattr(tool, "score_video_asset", lambda **kwargs: _probe())

        def fail(*args, **kwargs):
            raise AssertionError("--no-frames must not touch ffmpeg")

        monkeypatch.setattr(tool, "extract_frames", fail)
        summary = tool.collect([1], limit=10, frames_dir="/tmp/frames", with_frames=False)
        assert summary["per_post"][0]["frames"] == []
