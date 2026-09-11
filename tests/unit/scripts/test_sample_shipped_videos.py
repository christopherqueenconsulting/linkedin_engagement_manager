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


_ASSET_URL = "http://x/api/assets?file_name=videos/runwayml/clip.mp4"


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """A temp `assets_dir` holding `videos/runwayml/clip.mp4`'s slot — the file itself is opt-in.

    Returns the MP4 path. Helpers write what the store path writes (`write_video_receipt`,
    the `.frame-<label>.jpg` sidecars) so the sampler is exercised against the REAL receipt reader
    inside `score_video_asset`, not a mocked probe: the #1654 rule is about telling those apart.
    """
    from cqc_lem.utilities import content_quality

    monkeypatch.setattr(content_quality, "assets_dir", str(tmp_path))
    clip = tmp_path / "videos" / "runwayml" / "clip.mp4"
    clip.parent.mkdir(parents=True)
    return clip


def _write_receipt(clip, *, duration=6, probe="ok", has_stream=True, ratio="9:16"):
    from cqc_lem.utilities.video_receipt import write_video_receipt

    return write_video_receipt(str(clip), 7, {"duration_seconds": duration, "aspect_ratio": ratio,
                                              "asset_probe": probe, "has_video_stream": has_stream})


def _write_keyframes(clip, labels=("open", "mid", "close")):
    from cqc_lem.utilities.video_frames import keyframe_path

    for label in labels:
        pathlib.Path(keyframe_path(str(clip), label)).write_bytes(b"jpeg-bytes")


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


class TestFramesFor:
    """Frame timing and extraction are pinned in `tests/unit/utilities/test_video_frames.py`.

    What is this script's own is the CHOICE: extract from a clip still on disk, or fall back to the
    keyframes the store path retained beside a video the publish-time purge has already deleted —
    and report which of the two the reader is looking at.
    """

    @pytest.fixture
    def clip(self, tmp_path):
        video = tmp_path / "videos" / "runwayml" / "clip.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return video

    def _ffmpeg(self, tool, monkeypatch):
        from cqc_lem.utilities import video_frames

        monkeypatch.setattr(video_frames.shutil, "which", lambda name: "/usr/bin/ffmpeg")
        monkeypatch.setattr(
            video_frames.subprocess, "run",
            lambda cmd, **kwargs: pathlib.Path(cmd[-1]).write_bytes(b"jpeg-bytes"))

    def test_a_clip_still_on_disk_is_extracted_and_named_as_such(self, tool, clip, tmp_path,
                                                                 monkeypatch):
        self._ffmpeg(tool, monkeypatch)
        frames, source = tool.frames_for(str(clip), str(tmp_path / "frames"), "post7", 6)
        assert [pathlib.Path(f).name for f in frames] == ["post7_open.jpg", "post7_mid.jpg",
                                                          "post7_close.jpg"]
        assert source == "extracted"

    def test_a_shipped_post_falls_back_to_the_retained_keyframes(self, tool, clip, tmp_path,
                                                                 monkeypatch):
        from cqc_lem.utilities.video_frames import keyframe_path

        for label in ("open", "mid"):
            pathlib.Path(keyframe_path(str(clip), label)).write_bytes(b"jpeg-bytes")
        clip.unlink()  # what purge_post_assets does the moment the post publishes
        self._ffmpeg(tool, monkeypatch)

        frames, source = tool.frames_for(str(clip), str(tmp_path / "frames"), "post7", 6)
        assert source == "retained"
        # Copied into the frames directory so the audit doc can reference one path per frame.
        assert [pathlib.Path(f).name for f in frames] == ["post7_open.jpg", "post7_mid.jpg"]
        assert all(pathlib.Path(f).exists() for f in frames)

    def test_an_uncopyable_retained_frame_is_still_named_where_it_lives(self, tool, clip, tmp_path,
                                                                        monkeypatch):
        from cqc_lem.utilities.video_frames import keyframe_path

        retained = pathlib.Path(keyframe_path(str(clip), "open"))
        retained.write_bytes(b"jpeg-bytes")
        clip.unlink()
        self._ffmpeg(tool, monkeypatch)
        monkeypatch.setattr(tool.shutil, "copyfile",
                            lambda *a, **k: (_ for _ in ()).throw(OSError(30, "Read-only")))

        frames, source = tool.frames_for(str(clip), str(tmp_path / "frames"), "post7", 6)
        assert frames == [str(retained)] and source == "retained"

    def test_nothing_to_show_reports_no_source(self, tool, tmp_path, monkeypatch):
        self._ffmpeg(tool, monkeypatch)
        assert tool.frames_for(str(tmp_path / "gone.mp4"), str(tmp_path / "frames"),
                               "post7", 6) == ([], None)
        assert tool.frames_for(None, str(tmp_path / "frames"), "post7", 6) == ([], None)

    def test_an_unwritable_frames_dir_costs_the_frames_not_the_run(self, tool, clip, tmp_path,
                                                                   monkeypatch):
        # The documented run is a prod-image sidecar with the checkout mounted READ-ONLY, so
        # makedirs on the default frames dir raises — and must not take the measured corpus with it.
        self._ffmpeg(tool, monkeypatch)

        def readonly(*args, **kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(tool.os, "makedirs", readonly)
        assert tool.frames_for(str(clip), str(tmp_path / "frames"), "post1", 6) == ([], None)

    def test_an_unwritable_frames_dir_still_names_the_retained_keyframes(self, tool, clip,
                                                                        tmp_path, monkeypatch):
        # The read-only sidecar is the DOCUMENTED way to run this, and a shipped post's frames
        # already exist on the assets volume. Dropping them because the audit directory could not
        # be created would leave every shipped post as ungradable as it was before #1363.
        from cqc_lem.utilities.video_frames import keyframe_path

        retained = [pathlib.Path(keyframe_path(str(clip), label)) for label in ("open", "mid")]
        for frame in retained:
            frame.write_bytes(b"jpeg-bytes")
        clip.unlink()  # what purge_post_assets does the moment the post publishes
        self._ffmpeg(tool, monkeypatch)

        def readonly(*args, **kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(tool.os, "makedirs", readonly)
        frames, source = tool.frames_for(str(clip), str(tmp_path / "frames"), "post7", 6)
        assert frames == [str(f) for f in retained] and source == "retained"


class TestAssetSource:
    """The #1654 gradability rule: MP4 probes ok, OR the receipt plus at least one keyframe.

    `score_video_asset` answers `ok` from the receipt once the MP4 is purged, so the probe state
    alone cannot say what a row was graded from — and the 2026-08-30 reading was misread because
    of exactly that.
    """

    def test_a_clip_still_on_disk_is_graded_from_the_mp4(self, tool, volume):
        volume.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        assert tool.asset_source(str(volume), "ok") == ("mp4", 0)

    def test_the_mp4_wins_over_a_receipt_while_both_exist(self, tool, volume):
        # Not yet purged: the file is there to extract from, whatever the receipt says.
        volume.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        _write_receipt(volume)
        _write_keyframes(volume)
        assert tool.asset_source(str(volume), "ok") == ("mp4", 3)

    def test_a_purged_clip_with_receipt_and_keyframes_is_gradable(self, tool, volume):
        _write_receipt(volume)
        _write_keyframes(volume, labels=("open",))
        assert tool.asset_source(str(volume), "ok") == ("receipt+keyframes", 1)

    def test_a_receipt_with_no_keyframe_is_measured_but_not_gradable(self, tool, volume):
        _write_receipt(volume)
        assert tool.asset_source(str(volume), "ok") == ("receipt", 0)

    def test_a_receipt_that_recorded_a_failed_probe_grades_nothing(self, tool, volume):
        # A receipt is a MEASUREMENT: one that read `unreadable` at store time is evidence of a
        # failed render, not a source to grade a duration from — even with a keyframe beside it.
        _write_receipt(volume, duration=None, probe="unreadable", has_stream=False, ratio=None)
        _write_keyframes(volume, labels=("open",))
        assert tool.asset_source(str(volume), "unreadable") == ("missing", 1)

    def test_nothing_on_disk_is_missing(self, tool, volume):
        assert tool.asset_source(str(volume), "missing") == ("missing", 0)
        assert tool.asset_source(None, "missing") == ("missing", 0)


class TestSampleReport:
    def test_scores_the_body_and_the_asset_in_one_row(self, tool, monkeypatch):
        monkeypatch.setattr(tool, "score_video_asset", lambda **kwargs: _probe())
        row = tool.sample_report(_post(11), "http://x/api/assets?file_name=videos/runwayml/a.mp4",
                                 captions={"caption_text": "A real hook line",
                                           "caption_srt_url": "http://x/a.srt"},
                                 frames=["docs/f.jpg"], user_id=1)
        assert row["post_id"] == 11 and row["user_id"] == 1
        assert row["body_available"] is True and row["asset_available"] is True
        # A live `ok` with no receipt can only have come from the MP4 — the row unchanged by #1654.
        assert row["asset_source"] == "mp4" and row["keyframes_retained"] == 0
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
        assert row["asset_source"] == "missing"
        # Unmeasured duration is None — never scored as out of band.
        assert row["duration_in_band"] is None
        assert row["captioned"] is False

    def test_a_purged_post_grades_off_the_receipt_and_its_keyframes(self, tool, volume):
        # The shape #1517/#1595 leave behind: no MP4, a receipt, three keyframes. Nothing is mocked
        # — the measures reach the row through the real `score_video_asset` receipt read.
        _write_receipt(volume, duration=8, ratio="9:16")
        _write_keyframes(volume)
        row = tool.sample_report(_post(13), _ASSET_URL,
                                 captions={"caption_text": "A real hook line",
                                           "caption_srt_url": None})
        assert row["asset_available"] is True
        assert row["asset_source"] == "receipt+keyframes" and row["keyframes_retained"] == 3
        assert row["video_duration_seconds"] == 8 and row["duration_in_band"] is True
        assert row["video_aspect_ratio"] == "9:16" and row["video_asset_probe"] == "ok"
        assert row["captioned"] is True

    def test_a_receipt_without_keyframes_is_duration_graded_but_not_gradable(self, tool, volume):
        _write_receipt(volume, duration=12)
        row = tool.sample_report(_post(14), _ASSET_URL,
                                 captions={"caption_text": "hook", "caption_srt_url": None})
        # Says so in the row: measured (the band is answered, the caption is read) but frameless.
        assert row["asset_available"] is False
        assert row["asset_source"] == "receipt" and row["keyframes_retained"] == 0
        assert row["video_duration_seconds"] == 12 and row["duration_in_band"] is False
        assert row["captioned"] is True

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

    def test_receipt_sourced_rows_are_bucketed_apart_from_a_live_ok(self, tool):
        # Tonight's volume: MP4s still on disk, receipts with keyframes, one receipt retention
        # half-kept, and rows with nothing. The receipt rows read `ok` from the store-time probe,
        # and must not be counted as "the MP4 was probed live".
        summary = tool.summarize([
            self._row(1, asset_source="mp4"),
            self._row(2, asset_source="receipt+keyframes"),
            self._row(3, asset_source="receipt+keyframes"),
            self._row(4, asset_available=False, asset_source="receipt"),
            self._row(5, asset_available=False, asset_source="missing",
                      video_asset_probe="missing")])
        assert summary["gradable"] == 3
        assert summary["asset_sources"] == {"receipt+keyframes": 2, "mp4": 1, "receipt": 1,
                                            "missing": 1}
        assert summary["asset_probes"] == {"receipt": 3, "ok": 1, "missing": 1}
        assert summary["receipt_only"] == 1

    def test_receipt_and_keyframes_rows_clear_the_corpus_floor_but_frameless_ones_do_not(
            self, tool):
        framed = [self._row(i, asset_source="receipt+keyframes") for i in range(tool.MIN_CORPUS)]
        assert tool.summarize(framed)["sufficient_corpus"] is True
        frameless = [self._row(i, asset_available=False, asset_source="receipt")
                     for i in range(tool.MIN_CORPUS)]
        summary = tool.summarize(frameless)
        assert summary["gradable"] == 0 and summary["sufficient_corpus"] is False
        assert summary["receipt_only"] == tool.MIN_CORPUS

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

    def test_retained_and_extracted_frames_are_counted_apart(self, tool):
        # Only a RETAINED frame depicts the clip LinkedIn received; an extracted one came from a
        # video still on disk, i.e. one that has not published yet.
        summary = tool.summarize([
            self._row(1, frames=["a.jpg", "b.jpg"], frames_source="retained"),
            self._row(2, frames=["c.jpg"], frames_source="extracted"),
            self._row(3, frames=[], frames_source=None)])
        assert summary["frames_by_source"] == {"retained": 2, "extracted": 1}

    def test_renders_without_raising_on_an_empty_corpus(self, tool):
        assert "NOT ENOUGH" in tool._render(tool.summarize([]))


class TestPurgeHint:
    """The 2026-08-14 production run: 10 sampled, 0 gradable, 10 assets missing on disk.

    An all-missing corpus is what `purge_post_assets` produces on purpose, so the report has to
    name that cause — and must NOT name it when something else emptied the scorecard.
    """

    def _row(self, post_id, **overrides):
        return TestSummarize()._row(post_id, **overrides)

    def test_nothing_gradable_and_every_asset_missing_names_the_purge(self, tool):
        summary = tool.summarize([self._row(i, asset_available=False, video_asset_probe="missing")
                                  for i in range(10)])
        assert tool.purge_hint(summary) == tool.PURGE_HINT
        rendered = tool._render(summary)
        assert "purge_post_assets" in rendered and "#1517" in rendered
        # The 2026-08-14/08-30 "missing: 10" readings were partly a sidecar that never mounted
        # the assets volume where the app reads it; the hint names that too (#1654).
        assert "/app/src/cqc_lem/assets" in rendered

    def test_a_receipt_only_corpus_is_not_blamed_on_the_purge(self, tool):
        # Nothing gradable, but nothing MISSING either — retention kept the measures and lost the
        # frames, which is a different finding from "every MP4 was purged".
        summary = tool.summarize([self._row(i, asset_available=False, asset_source="receipt")
                                  for i in range(3)])
        assert summary["asset_probes"] == {"receipt": 3}
        assert tool.purge_hint(summary) is None

    def test_a_corpus_that_graded_something_gets_no_hint(self, tool):
        summary = tool.summarize([self._row(1),
                                  self._row(2, asset_available=False,
                                            video_asset_probe="missing")])
        assert tool.purge_hint(summary) is None
        assert "purge_post_assets" not in tool._render(summary)

    def test_an_empty_or_otherwise_unreadable_corpus_is_not_blamed_on_the_purge(self, tool):
        assert tool.purge_hint(tool.summarize([])) is None
        unreadable = tool.summarize([self._row(1, asset_available=False,
                                               video_asset_probe="unreadable")])
        assert tool.purge_hint(unreadable) is None


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
        monkeypatch.setattr(tool, "frames_for",
                            lambda *args, **kwargs: calls.append(args) or ([], None))

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

        monkeypatch.setattr(tool, "frames_for", fail)
        summary = tool.collect([1], limit=10, frames_dir="/tmp/frames", with_frames=False)
        assert summary["per_post"][0]["frames"] == []

    def test_frames_are_only_written_for_the_posts_the_scorecard_reports(self, tool, monkeypatch):
        # `--limit` is applied per user and again across users, so the multi-user default scores
        # more rows than it reports. A frame for a dropped row is a JPEG the audit could cite for
        # a video that is not in the corpus.
        from cqc_lem.utilities import db

        by_user = {
            1: [_post(11, scheduled_time="2026-08-09 09:00:00"),
                _post(12, scheduled_time="2026-08-08 09:00:00")],
            2: [_post(21, scheduled_time="2026-07-02 09:00:00"),
                _post(22, scheduled_time="2026-07-01 09:00:00")],
        }
        monkeypatch.setattr(db, "get_posted_posts", lambda user_id: by_user[user_id])
        monkeypatch.setattr(db, "get_post_video_url",
                            lambda post_id: f"http://x/api/assets?file_name=videos/runwayml/{post_id}.mp4")
        monkeypatch.setattr(db, "get_post_captions", lambda post_id: {})
        probes = []
        monkeypatch.setattr(tool, "score_video_asset",
                            lambda **kwargs: probes.append(kwargs) or _probe())
        framed = []
        monkeypatch.setattr(tool, "frames_for",
                            lambda path, out_dir, prefix, duration:
                            framed.append(prefix) or ([], None))

        summary = tool.collect([1, 2], limit=2, frames_dir="/tmp/frames")
        assert [r["post_id"] for r in summary["per_post"]] == [11, 12]
        assert framed == ["post11", "post12"]
        # One probe per sampled post — the frame timestamps read the duration the row reports.
        assert len(probes) == 4

    def test_a_purged_post_is_graded_and_framed_from_what_retention_kept(self, tool, volume,
                                                                        tmp_path, monkeypatch):
        # End to end through the facade with NO scorer mocked: the MP4 is gone, the receipt and
        # keyframes are what the store path left, and the row grades with `retained` frames.
        from cqc_lem.utilities import db

        _write_receipt(volume, duration=7)
        _write_keyframes(volume)
        monkeypatch.setattr(db, "get_posted_posts", lambda user_id: [_post(1)])
        monkeypatch.setattr(db, "get_post_video_url", lambda post_id: _ASSET_URL)
        monkeypatch.setattr(db, "get_post_captions",
                            lambda post_id: {"caption_text": "hook", "caption_srt_url": None})

        summary = tool.collect([1], limit=10, frames_dir=str(tmp_path / "frames"))
        assert summary["gradable"] == 1 and summary["asset_sources"] == {"receipt+keyframes": 1}
        assert summary["asset_probes"] == {"receipt": 1}
        assert summary["duration_measured"] == 1 and summary["duration_in_band"] == 1
        row = summary["per_post"][0]
        assert row["frames_source"] == "retained" and len(row["frames"]) == 3
        assert summary["frames_by_source"] == {"retained": 3}


class TestRender:
    def test_every_frame_is_printed_with_where_it_came_from(self, tool):
        summary = tool.summarize([
            TestSummarize()._row(1, frames=["assets/1363/post1_open.jpg"],
                                 frames_source="retained")])
        rendered = tool._render(summary)
        assert "Frames (1 retained):" in rendered
        assert "assets/1363/post1_open.jpg  [retained]" in rendered

    def test_every_row_prints_what_graded_it(self, tool):
        summary = tool.summarize([
            TestSummarize()._row(1, asset_source="receipt+keyframes"),
            TestSummarize()._row(2, asset_available=False, asset_source="receipt"),
            TestSummarize()._row(3, asset_available=False, asset_source="missing",
                                 video_asset_probe="missing")])
        rendered = tool._render(summary)
        assert "| ok | receipt+keyframes" in rendered
        assert "| ok | receipt" in rendered and "| missing | missing" in rendered
        assert "Receipt but no keyframes  : 1" in rendered
        assert "Asset sources:" in rendered and "   1  receipt+keyframes" in rendered

    def test_the_slop_count_carries_its_denominator(self, tool):
        rows = [TestSummarize()._row(i, asset_available=False, video_asset_probe="missing")
                for i in range(10)]
        # An all-missing corpus checked NO bodies for slop; a bare "0" reads as "checked, clean".
        assert "Hard slop violations      : 0 (over 0 graded)" in tool._render(tool.summarize(rows))
