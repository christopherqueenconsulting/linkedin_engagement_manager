"""Unit tests for animated loop posts (docs/animated-posts.md).

ffmpeg and Runway are the external boundaries and both are mocked; the GIFs the mocked ffmpeg
"writes" are real (Pillow), so the frame and byte caps are checked against actual files. What these
pin is the policy: LinkedIn's 250-frame cap is never exceeded, the step-down is bounded, every
failure hands back None instead of raising, and the flag OFF changes nothing.
"""
import os
import shutil
import subprocess
from unittest.mock import patch

import pytest

from cqc_lem.utilities import animated_loop as al

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.animated_loop"


def _write_gif(path, frames=4, size=(8, 8)):
    from PIL import Image
    images = [Image.new("RGB", size, (i * 7 % 255, 0, 0)) for i in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:], loop=0, duration=80)
    return str(path)


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"0" * 256)
    return str(path)


def _ffmpeg_writing(frames_per_call):
    """A subprocess.run stand-in whose Nth call writes a GIF of frames_per_call[N] frames."""
    calls = []

    def _run(args, **_kwargs):
        calls.append(args)
        frames = frames_per_call[len(calls) - 1]
        if frames is None:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        _write_gif(args[-1], frames=frames)
        return subprocess.CompletedProcess(args, 0, "", "")

    return _run, calls


class TestGifLimits:
    def test_a_small_gif_is_within_limits(self, tmp_path):
        assert al.gif_within_limits(_write_gif(tmp_path / "a.gif", frames=10))

    def test_more_than_250_frames_is_refused(self, tmp_path):
        assert al.gif_frame_count(_write_gif(tmp_path / "a.gif", frames=251)) == 251
        assert not al.gif_within_limits(str(tmp_path / "a.gif"))

    def test_exactly_250_frames_is_allowed(self, tmp_path):
        assert al.gif_within_limits(_write_gif(tmp_path / "a.gif", frames=250))

    def test_over_the_byte_budget_is_refused(self, tmp_path):
        path = _write_gif(tmp_path / "a.gif", frames=3)
        assert not al.gif_within_limits(path, max_bytes=10)

    def test_a_png_is_not_a_gif(self, tmp_path):
        from PIL import Image
        path = str(tmp_path / "a.png")
        Image.new("RGB", (8, 8)).save(path)
        assert al.gif_frame_count(path) is None
        assert not al.gif_within_limits(path)

    def test_missing_file(self):
        assert not al.gif_within_limits(None)
        assert not al.gif_within_limits("/nope/a.gif")


class TestAttemptPlan:
    def test_at_most_three_strictly_smaller_tries(self):
        plan = al._attempt_plan(720, 12)
        assert len(plan) == al.LOOP_MAX_ATTEMPTS == 3
        widths = [w for w, _ in plan]
        assert widths == sorted(widths, reverse=True) and len(set(widths)) == 3
        assert all(fps >= 6 for _, fps in plan)

    def test_frame_cap_never_exceeds_linkedin(self):
        assert al._max_frames_for(60, 30) == al.GIF_MAX_FRAMES
        assert al._max_frames_for(4, 12) == 48


class TestMakeLoopFromVideo:
    def test_no_ffmpeg_is_none_not_a_raise(self, clip):
        with patch(f"{_MOD}._ffmpeg_bin", return_value=None):
            assert al.make_loop_from_video(clip) is None

    def test_missing_source_is_none(self):
        assert al.make_loop_from_video("/nope.mp4") is None

    def test_first_fit_is_returned_with_palette_and_loop_args(self, clip):
        run, calls = _ffmpeg_writing([12])
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch(f"{_MOD}.subprocess.run", side_effect=run):
            out = al.make_loop_from_video(clip, seconds=4.0, fps=12, width=720)
        try:
            assert out and out.endswith(".gif") and os.path.isfile(out)
            args = calls[0]
            graph = args[args.index("-filter_complex") + 1]
            assert "palettegen" in graph and "paletteuse" in graph
            assert "fps=12" in graph and "scale=720:-1:flags=lanczos" in graph
            assert args[args.index("-loop") + 1] == "0"
            assert int(args[args.index("-frames:v") + 1]) <= al.GIF_MAX_FRAMES
        finally:
            os.remove(out)

    def test_steps_down_when_an_encode_breaks_the_frame_cap(self, clip):
        run, calls = _ffmpeg_writing([251, 20])
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch(f"{_MOD}.subprocess.run", side_effect=run):
            out = al.make_loop_from_video(clip)
        try:
            assert out and len(calls) == 2
            first = calls[0][calls[0].index("-filter_complex") + 1]
            second = calls[1][calls[1].index("-filter_complex") + 1]
            assert "scale=720:" in first and "scale=540:" in second
        finally:
            os.remove(out)

    def test_gives_up_after_three_tries_and_leaves_no_temp(self, clip):
        run, calls = _ffmpeg_writing([251, None, 251])
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch(f"{_MOD}.subprocess.run", side_effect=run):
            assert al.make_loop_from_video(clip) is None
        assert len(calls) == 3
        assert not any(os.path.exists(c[-1]) for c in calls)

    def test_ffmpeg_that_cannot_run_is_none(self, clip):
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch(f"{_MOD}.subprocess.run", side_effect=OSError("no exec")):
            assert al.make_loop_from_video(clip) is None


class TestMakeLoopMp4:
    def test_clamped_to_fifteen_seconds(self, clip):
        def run(args, **_kw):
            with open(args[-1], "wb") as fh:
                fh.write(b"mp4")
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch(f"{_MOD}.subprocess.run", side_effect=run) as mocked:
            out = al.make_loop_mp4(clip, seconds=40)
        try:
            args = mocked.call_args[0][0]
            assert args[args.index("-t") + 1] == "15.000"
            graph = args[args.index("-filter_complex") + 1]
            assert "reverse" in graph and "concat=n=2" in graph
            assert "libx264" in args
        finally:
            os.remove(out)

    def test_failure_is_none(self, clip):
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch(f"{_MOD}.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "x")):
            assert al.make_loop_mp4(clip) is None

    def test_no_ffmpeg_is_none(self, clip):
        with patch(f"{_MOD}._ffmpeg_bin", return_value=None):
            assert al.make_loop_mp4(clip) is None


class TestLoopMotionPrompt:
    def test_directive_leads_and_is_capped(self):
        prompt = al.loop_motion_prompt("The camera pushes in slowly. " * 100)
        assert prompt.startswith(al.LOOP_MOTION_DIRECTIVE)
        assert len(prompt) <= al.LOOP_PROMPT_MAX_CHARS

    def test_receipt_concept_is_attribute_readable(self):
        concept = al._receipt_concept({"concept": {"thesis": "t", "emotional_beat": "calm"}})
        assert concept.thesis == "t" and concept.emotional_beat == "calm"
        assert al._receipt_concept({"concept": {}}) is None
        assert al._receipt_concept(None) is None


@pytest.fixture
def stored_still(tmp_path):
    assets = tmp_path / "assets"
    (assets / "images" / "posts" / "42").mkdir(parents=True)
    (assets / "images" / "posts" / "42" / "img_abc.png").write_bytes(b"png")
    url = "https://api.example.com/api/assets?file_name=images/posts/42/img_abc.png"
    with patch("cqc_lem.utilities.post_image.assets_dir", str(assets)):
        yield url, assets


class TestProducePostLoop:
    def test_flag_off_spends_nothing(self, monkeypatch, stored_still):
        monkeypatch.setenv("ANIMATED_POST_ENABLED", "false")
        url, _ = stored_still
        with patch("cqc_lem.utilities.ai.video_models.create_runway_video") as runway:
            assert al.produce_post_loop(7, 42, "text", url) is None
        runway.assert_not_called()

    def test_flag_on_animates_the_stored_still_and_stores_the_loop_beside_it(
            self, monkeypatch, stored_still, tmp_path):
        monkeypatch.setenv("ANIMATED_POST_ENABLED", "true")
        url, assets = stored_still
        gif = _write_gif(tmp_path / "made.gif", frames=6)
        with patch("cqc_lem.utilities.ai.ai_helper.get_runway_ml_video_prompt_from_ai",
                   return_value="steam rises from the cup") as motion, \
             patch("cqc_lem.utilities.media_provenance.read_brief_receipt",
                   return_value={"prompt": "a cup", "concept": {"thesis": "t"}}), \
             patch("cqc_lem.utilities.ai.video_models.create_runway_video",
                   return_value="https://runway.example/clip.mp4") as runway, \
             patch(f"{_MOD}._download", return_value=str(tmp_path / "clip.mp4")), \
             patch(f"{_MOD}.make_loop_from_video", return_value=gif):
            stored = al.produce_post_loop(7, 42, "text", url)
        assert stored == str(assets / "images" / "posts" / "42" / "img_abc.loop.gif")
        assert os.path.isfile(stored)
        kwargs = runway.call_args.kwargs
        assert kwargs["duration"] == al.LOOP_SOURCE_DURATION
        assert runway.call_args.args[0].endswith("img_abc.png")  # the gated still, not a new frame
        assert runway.call_args.args[1].startswith(al.LOOP_MOTION_DIRECTIVE)
        assert motion.call_args.kwargs["concept"].thesis == "t"

    def test_a_runway_failure_never_raises(self, monkeypatch, stored_still):
        monkeypatch.setenv("ANIMATED_POST_ENABLED", "true")
        url, _ = stored_still
        with patch("cqc_lem.utilities.ai.ai_helper.get_runway_ml_video_prompt_from_ai",
                   return_value="m"), \
             patch("cqc_lem.utilities.media_provenance.read_brief_receipt", return_value=None), \
             patch("cqc_lem.utilities.ai.video_models.create_runway_video",
                   side_effect=RuntimeError("runway down")), \
             patch(f"{_MOD}.log_warning") as warn:
            assert al.produce_post_loop(7, 42, "text", url) is None
        warn.assert_called_once()

    def test_no_still_no_loop(self, monkeypatch):
        monkeypatch.setenv("ANIMATED_POST_ENABLED", "true")
        assert al.produce_post_loop(7, 42, "text", None) is None
        assert al.produce_post_loop(7, None, "text", "u") is None


class TestPostImageLoopStorage:
    def test_loop_is_named_off_the_still_and_removed_with_it(self, stored_still, tmp_path):
        from cqc_lem.utilities import post_image as pi
        url, assets = stored_still
        assert pi.post_loop_abs_path(url) is None
        stored = pi.store_post_loop(url, _write_gif(tmp_path / "x.gif"))
        assert pi.post_loop_abs_path(url) == stored
        assert pi.remove_post_image_file(url) is True
        assert not os.path.exists(stored)

    def test_a_non_gif_is_never_stored_as_a_loop(self, stored_still, tmp_path):
        from cqc_lem.utilities import post_image as pi
        url, _ = stored_still
        mp4 = tmp_path / "x.mp4"
        mp4.write_bytes(b"x")
        assert pi.store_post_loop(url, str(mp4)) is None

    def test_a_foreign_url_has_no_loop(self, tmp_path):
        from cqc_lem.utilities import post_image as pi
        assert pi.store_post_loop("https://evil.example/x.png", _write_gif(tmp_path / "x.gif")) is None
        assert pi.post_loop_abs_path("https://evil.example/x.png") is None


class TestFlag:
    def test_registered_off_by_default(self, monkeypatch):
        from cqc_lem.utilities.flags import ANIMATED_POST, FLAGS, env_default
        monkeypatch.delenv("ANIMATED_POST_ENABLED", raising=False)
        assert FLAGS[ANIMATED_POST].env_var == "ANIMATED_POST_ENABLED"
        assert env_default(ANIMATED_POST) is False

    def test_unreadable_flag_reads_off(self):
        with patch("cqc_lem.utilities.flags.flag_enabled", side_effect=RuntimeError("x")):
            assert al.animated_post_enabled(1) is False


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_ffmpeg_makes_a_gif_inside_linkedin_limits(tmp_path):
    src = str(tmp_path / "src.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=24",
                    "-pix_fmt", "yuv420p", src], capture_output=True, check=True, timeout=60)
    gif = al.make_loop_from_video(src, seconds=4.0, fps=12, width=320)
    try:
        assert gif and al.gif_within_limits(gif)
        assert al.gif_frame_count(gif) <= al.GIF_MAX_FRAMES
    finally:
        if gif:
            os.remove(gif)
    mp4 = al.make_loop_mp4(src, seconds=4)
    try:
        assert mp4 and os.path.getsize(mp4) > 0
    finally:
        if mp4:
            os.remove(mp4)


def test_module_uses_the_shared_ffmpeg_probe():
    """Availability is checked the same way video_captions.py does — one probe, not two."""
    from cqc_lem.utilities import video_captions
    assert al._ffmpeg_bin is video_captions._ffmpeg_bin
