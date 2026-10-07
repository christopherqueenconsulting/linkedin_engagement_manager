"""Unit tests for muted-autoplay video captions (issue #1278, decision 1A).

ffmpeg is the only external boundary and it is mocked everywhere, so what these pin is the policy:
what text reaches the frame, who is allowed to paint over an avatar, and — the part that decides
whether this feature can ever cost a user a post — that every failure path hands the ORIGINAL video
back untouched.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import video_captions as vc

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.video_captions"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Unit lane resolves flags from the env var, so this IS turning the flag on."""
    monkeypatch.setenv("VIDEO_CAPTIONS_ENABLED", "true")


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "post.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"0" * 512)
    return str(path)


@pytest.fixture(autouse=True)
def _assets(tmp_path, monkeypatch):
    monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path / "assets"), raising=False)


class TestCaptionLines:
    def test_takes_the_posts_own_hook(self):
        lines = vc.caption_lines("Most teams ship AI nobody asked for.\n\nHere is the fix.")
        assert lines and lines[0].startswith("Most teams ship AI")

    def test_drops_hashtag_and_link_lines(self):
        assert vc.caption_lines("#ai #saas\nhttps://example.com") == []

    def test_emoji_never_reach_the_frame(self):
        # libass renders non-BMP as tofu — the same characters ChromeDriver refuses to type.
        assert "🚀" not in " ".join(vc.caption_lines("🚀 Shipping beats planning every time."))

    def test_respects_the_line_and_width_caps(self):
        lines = vc.caption_lines("Short hook here, then a long tail of words " + "word " * 40,
                                 max_lines=2, max_chars=20)
        assert lines == ["Short hook here"]
        assert all(len(line) <= 20 for line in lines)

    def test_never_an_ellipsis_mid_thought(self):
        """Showcase round 4: the critic's exhibit was a caption cut at "…turn into a hidden…"."""
        post = ("Most AI gateways that look cheap on a pricing page turn into a hidden tax on "
                "every request, because retries and fallbacks are billed too.\n\nHere is why.")
        lines = vc.caption_lines(post)
        text = " ".join(lines)
        assert "..." not in text and "…" not in text
        assert text == ("Most AI gateways that look cheap on a pricing page turn into a hidden "
                        "tax on every request")
        assert len(lines) <= vc.VIDEO_CAPTION_MAX_LINES

    def test_a_hyphenated_word_is_never_split(self):
        lines = vc.caption_lines("Did you ever think a silent model retirement could turn into a "
                                 "hidden cost-saving win?")
        assert "cost-saving" in " ".join(lines)

    def test_the_full_first_sentence_when_it_fits(self):
        assert " ".join(vc.caption_lines("A model retired at 2am. Nobody noticed. Then more.")) \
            == "A model retired at 2am. Nobody noticed."

    def test_a_first_clause_that_cannot_fit_is_no_caption(self):
        assert vc.caption_lines("word " * 200, max_lines=2, max_chars=20) == []

    def test_a_dangling_clause_is_never_the_caption(self):
        post = "We cut costs with routing, by the way " + "word " * 40
        assert vc.caption_lines(post, max_lines=1, max_chars=30) == ["We cut costs with routing"]

    def test_empty_content_is_no_caption(self):
        assert vc.caption_lines(None) == []
        assert vc.caption_lines("   \n\n") == []


class TestSrt:
    def test_one_cue_over_the_muted_window(self, tmp_path):
        out = vc.build_caption_srt(["Line one", "Line two"], str(tmp_path / "a.srt"),
                                   hold_seconds=3.0)
        body = open(out, encoding="utf-8").read()
        assert body.startswith("1\n00:00:00,000 --> 00:00:03,000\n")
        assert "Line one\nLine two" in body

    def test_no_lines_writes_nothing(self, tmp_path):
        assert vc.build_caption_srt([], str(tmp_path / "a.srt")) is None
        assert not (tmp_path / "a.srt").exists()

    def test_timestamp_clamps_at_zero(self):
        assert vc.srt_timestamp(-5) == "00:00:00,000"


@pytest.fixture
def srt(tmp_path):
    return vc.build_caption_srt(["A model retired at 2am and", "nobody noticed."],
                                str(tmp_path / "a.srt"), hold_seconds=3.0)


@pytest.fixture
def sized():
    with patch(f"{_MOD}.video_dimensions", return_value=(540, 960)):
        yield


class TestBurn:
    def test_the_cue_is_read_back_from_the_sidecar(self, srt):
        assert vc.read_srt_cue(srt) == (["A model retired at 2am and", "nobody noticed."], 3.0)
        assert vc.read_srt_cue("/no/such.srt") == ([], 0.0)

    def test_the_band_is_on_brand_inside_the_safe_margins(self, tmp_path):
        from PIL import Image

        from cqc_lem.utilities.ai.image_compose import BrandStyle, _hex

        out = str(tmp_path / "band.png")
        assert vc.render_caption_overlay(["A model retired at 2am and nobody noticed."],
                                         (540, 960), out) is True
        with Image.open(out) as im:
            assert im.size == (540, 960) and im.mode == "RGBA"
            box = im.getbbox()
            pixels = set(im.getdata())
        left, top, right, bottom = box
        assert left >= round(540 * vc.CAPTION_SIDE_MARGIN) - 1
        assert right <= 540 - round(540 * vc.CAPTION_SIDE_MARGIN) + 1
        assert bottom <= 960 - round(960 * vc.CAPTION_BOTTOM_MARGIN) + 1
        assert top > 960 // 2  # a lower band, never over the face zone
        brand = BrandStyle()
        assert (*_hex(brand.primary), 255) in pixels  # the gold rule
        assert (*_hex(brand.neutral_dark), vc.CAPTION_BAND_ALPHA) in pixels  # translucent band

    def test_a_caption_that_cannot_be_set_legibly_is_refused(self, tmp_path):
        assert vc.render_caption_overlay(["Supercalifragilistic" * 6], (200, 200),
                                         str(tmp_path / "b.png")) is False
        assert vc.render_caption_overlay([], (200, 200), str(tmp_path / "c.png")) is False

    def test_video_dimensions_parses_ffprobe(self, video):
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffprobe"), \
             patch("subprocess.run", return_value=MagicMock(stdout="720x1280\n")):
            assert vc.video_dimensions(video) == (720, 1280)
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffprobe"), \
             patch("subprocess.run", return_value=MagicMock(stdout="N/A")):
            assert vc.video_dimensions(video) is None
        with patch(f"{_MOD}._ffmpeg_bin", return_value=None):
            assert vc.video_dimensions(video) is None

    def test_an_unreadable_size_is_false(self, video, srt, tmp_path):
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch(f"{_MOD}.video_dimensions", return_value=None), \
             patch("subprocess.run") as run:
            assert vc.burn_captions(video, srt, str(tmp_path / "o.mp4")) is False
        run.assert_not_called()

    def test_an_unfittable_caption_is_false(self, video, srt, sized, tmp_path):
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch(f"{_MOD}.render_caption_overlay", return_value=False), \
             patch("subprocess.run") as run:
            assert vc.burn_captions(video, srt, str(tmp_path / "o.mp4")) is False
        run.assert_not_called()

    def test_an_encoder_that_cannot_start_is_false(self, video, srt, sized, tmp_path):
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", side_effect=OSError("exec")):
            assert vc.burn_captions(video, srt, str(tmp_path / "o.mp4")) is False
        assert not os.path.exists(str(tmp_path / "o.mp4.band.png"))

    def test_missing_ffmpeg_is_a_false_not_a_raise(self, video, tmp_path):
        with patch(f"{_MOD}._ffmpeg_bin", return_value=None):
            assert vc.burn_captions(video, str(tmp_path / "a.srt"), str(tmp_path / "o.mp4")) is False

    def test_non_zero_exit_is_false(self, video, srt, sized, tmp_path):
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")):
            assert vc.burn_captions(video, srt, str(tmp_path / "o.mp4")) is False

    def test_the_failure_warning_stays_a_stable_template(self, video, srt, sized, tmp_path):
        """The warning message carries no per-file detail.

        A stderr tail in it would keep the recurrence key from ever repeating, so an ffmpeg that
        fails on EVERY video post would never escalate into a filed defect.
        """
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", return_value=MagicMock(
                 returncode=1, stderr="frame= 41 /assets/videos/runwayml/abc.mp4: no libass")), \
             patch(f"{_MOD}.log_warning") as warn:
            assert vc.burn_captions(video, srt, str(tmp_path / "o.mp4")) is False
        message = warn.call_args[0][0]
        assert message == "Caption burn-in failed — shipping the video uncaptioned"

    def test_empty_output_is_false(self, video, srt, sized, tmp_path):
        out = tmp_path / "o.mp4"
        out.write_bytes(b"")
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            assert vc.burn_captions(video, srt, str(out)) is False

    def test_audio_is_stream_copied_and_the_band_overlays_the_muted_window(self, video, srt,
                                                                            sized, tmp_path):
        out = tmp_path / "o.mp4"
        out.write_bytes(b"x" * 100)
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as run:
            assert vc.burn_captions(video, srt, str(out)) is True
        cmd = run.call_args[0][0]
        assert cmd[cmd.index("-c:a") + 1] == "copy"
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "overlay=" in graph and "between(t,0,3.000)" in graph
        assert "subtitles=" not in graph  # no libass / system font dependency any more
        assert cmd[cmd.index("-i", cmd.index("-i") + 1) + 1].endswith(".band.png")
        # The band PNG is a temp: it never lingers on the assets volume.
        assert not os.path.exists(f"{out}.band.png")


def _burn_ok(src, srt, out, timeout=600, **_kw):
    with open(out, "wb") as f:
        f.write(b"CAPTIONED")
    return True


class TestApplyCaptions:
    def test_burns_in_place_and_reports_the_sidecar(self, video):
        with patch(f"{_MOD}.burn_captions", side_effect=_burn_ok), \
             patch(f"{_MOD}._track_burn_cost"):
            result = vc.apply_captions_to_video(video, "Hook line for the video.", post_id=1,
                                               user_id=2)
        assert result.burned is True
        assert result.video_path == video
        # The BURNED bytes are what the stored posts.video_url now points at.
        assert open(video, "rb").read() == b"CAPTIONED"
        assert result.srt_path and os.path.exists(result.srt_path)
        assert result.caption_text.startswith("Hook line")

    def test_flag_off_touches_nothing(self, video, monkeypatch):
        monkeypatch.setenv("VIDEO_CAPTIONS_ENABLED", "false")
        with patch(f"{_MOD}.burn_captions") as burn:
            result = vc.apply_captions_to_video(video, "Hook line.", post_id=1, user_id=2)
        burn.assert_not_called()
        assert result.burned is False and result.srt_path is None
        assert result.skipped_reason == "flag_off"

    def test_no_usable_hook_writes_no_sidecar(self, video):
        with patch(f"{_MOD}.burn_captions") as burn:
            result = vc.apply_captions_to_video(video, "#ai\nhttps://x.test", post_id=1, user_id=2)
        burn.assert_not_called()
        assert result.srt_path is None and result.skipped_reason == "no_caption_text"

    def test_avatar_led_video_is_sidecar_only_without_the_opt_in(self, video):
        with patch(f"{_MOD}.captions_allowed_on_avatar_video", return_value=False), \
             patch(f"{_MOD}.burn_captions") as burn:
            result = vc.apply_captions_to_video(video, "Hook line.", post_id=1, user_id=2,
                                                avatar_led=True)
        burn.assert_not_called()
        assert result.burned is False
        assert result.skipped_reason == "avatar_opt_out"
        # The sidecar still ships — opting out costs the burn, not the captions.
        assert result.srt_path and os.path.exists(result.srt_path)

    def test_avatar_led_video_burns_once_the_user_opts_in(self, video):
        with patch(f"{_MOD}.captions_allowed_on_avatar_video", return_value=True), \
             patch(f"{_MOD}.burn_captions", side_effect=_burn_ok), \
             patch(f"{_MOD}._track_burn_cost"):
            result = vc.apply_captions_to_video(video, "Hook line.", post_id=1, user_id=2,
                                                avatar_led=True)
        assert result.burned is True

    def test_failed_burn_leaves_the_original_video(self, video):
        original = open(video, "rb").read()
        with patch(f"{_MOD}.burn_captions", return_value=False):
            result = vc.apply_captions_to_video(video, "Hook line.", post_id=1, user_id=2)
        assert result.burned is False and result.skipped_reason == "burn_failed"
        assert open(video, "rb").read() == original

    def test_failed_burn_leaves_no_temp_render_behind(self, video):
        """Nothing purges the temp — `purge_post_assets` only knows the post's own video name."""
        def _half_written(src, srt, out, timeout=600, **_kw):
            with open(out, "wb") as f:
                f.write(b"PARTIAL")
            return False

        with patch(f"{_MOD}.burn_captions", side_effect=_half_written):
            vc.apply_captions_to_video(video, "Hook line.", post_id=1, user_id=2)
        assert not os.path.exists(f"{video}.captioned.mp4")

    def test_a_burn_the_probe_rejects_never_replaces_the_original(self, video):
        """The input was probed; the re-encode is a DIFFERENT file and has to earn the same pass."""
        original = open(video, "rb").read()
        with patch(f"{_MOD}.burn_captions", side_effect=_burn_ok), \
             patch(f"{_MOD}._track_burn_cost") as cost:
            result = vc.apply_captions_to_video(video, "Hook line.", post_id=1, user_id=2,
                                                validate=lambda path: False)
        assert result.burned is False and result.skipped_reason == "burn_rejected"
        assert open(video, "rb").read() == original
        assert not os.path.exists(f"{video}.captioned.mp4")
        # Nothing shipped, so nothing is billed.
        cost.assert_not_called()

    def test_a_burn_the_probe_accepts_ships(self, video):
        seen: list = []
        with patch(f"{_MOD}.burn_captions", side_effect=_burn_ok), \
             patch(f"{_MOD}._track_burn_cost"):
            result = vc.apply_captions_to_video(
                video, "Hook line.", post_id=1, user_id=2,
                validate=lambda path: seen.append(path) is None)
        assert result.burned is True
        assert open(video, "rb").read() == b"CAPTIONED"
        # The probe ran on the BURNED temp, not on the original it is about to replace.
        assert seen == [f"{video}.captioned.mp4"]

    def test_any_error_fails_open(self, video):
        with patch(f"{_MOD}.caption_lines", side_effect=RuntimeError("nope")):
            result = vc.apply_captions_to_video(video, "Hook line.", post_id=1, user_id=2)
        assert result.video_path == video and result.burned is False
        assert result.skipped_reason == "error"

    def test_cost_is_attributed_per_burn(self, video):
        with patch(f"{_MOD}.burn_captions", side_effect=_burn_ok), \
             patch(f"{_MOD}.video_duration_seconds", return_value=60.0), \
             patch("cqc_lem.utilities.observability.track_media_cost") as track:
            vc.apply_captions_to_video(video, "Hook line.", post_id=9, user_id=3)
        track.assert_called_once()
        args, kwargs = track.call_args
        assert args[0] == "video" and args[1] == "local-ffmpeg" and args[2] > 0
        assert kwargs["post_id"] == 9 and kwargs["meta"]["kind"] == "caption_burn"

    def test_cost_tracking_failure_never_loses_the_caption(self, video):
        with patch(f"{_MOD}.burn_captions", side_effect=_burn_ok), \
             patch("cqc_lem.utilities.observability.track_media_cost",
                   side_effect=RuntimeError("posthog down")):
            result = vc.apply_captions_to_video(video, "Hook line.", post_id=1, user_id=2)
        assert result.burned is True


class TestAvatarOptIn:
    def test_unreadable_preference_leaves_the_likeness_alone(self):
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   side_effect=RuntimeError("db down")):
            assert vc.captions_allowed_on_avatar_video(7) is False

    def test_no_user_is_never_allowed(self):
        assert vc.captions_allowed_on_avatar_video(None) is False

    def test_opt_in_is_honoured(self):
        with patch("cqc_lem.utilities.db.get_avatar_preferences",
                   return_value={"avatar_caption_overlay": True}):
            assert vc.captions_allowed_on_avatar_video(7) is True


class TestDuration:
    def test_unreadable_duration_falls_back(self, video):
        with patch(f"{_MOD}._ffmpeg_bin", return_value=None):
            assert vc.video_duration_seconds(video) == vc._FALLBACK_DURATION_SECONDS

    def test_unparseable_ffprobe_output_falls_back(self, video):
        with patch(f"{_MOD}._ffmpeg_bin", return_value="/usr/bin/ffprobe"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="N/A")):
            assert vc.video_duration_seconds(video) == vc._FALLBACK_DURATION_SECONDS


class TestCaptionBrand:
    def test_the_authors_brand_colours_the_band(self):
        with patch("cqc_lem.utilities.brand_kit.brand_clause_for_user",
                   return_value="light gold (#ffcc00) against charcoal (#101010)"):
            brand = vc._caption_brand(5)
        assert brand.primary == "#FFCC00" and brand.neutral_dark == "#101010"

    def test_an_unreadable_kit_is_the_reference_brand(self):
        from cqc_lem.utilities.ai.image_compose import BrandStyle

        with patch("cqc_lem.utilities.brand_kit.brand_clause_for_user",
                   side_effect=RuntimeError("db")):
            assert vc._caption_brand(5) == BrandStyle()
        assert vc._caption_brand(None) == BrandStyle()

    def test_apply_passes_the_brand_to_the_burn(self, video):
        with patch(f"{_MOD}.burn_captions", side_effect=_burn_ok) as burn, \
             patch(f"{_MOD}._track_burn_cost"), \
             patch(f"{_MOD}._caption_brand", return_value="BRAND"):
            vc.apply_captions_to_video(video, "Hook line.", post_id=1, user_id=2)
        assert burn.call_args.kwargs["brand"] == "BRAND"
