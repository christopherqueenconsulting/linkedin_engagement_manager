"""The $0 branded motion title card — the default video fallback (showcase round 4).

ffmpeg is mocked everywhere except the one ``slow`` test, which encodes a real clip when an ffmpeg
binary is on the PATH. What these pin: the card sets the post's OWN words, the stock query can never
be a raw homonym, the frames actually animate, and every encoder failure leaves no file behind.
"""
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PIL")

from cqc_lem.utilities import video_title_card as tc  # noqa: E402
from cqc_lem.utilities.ai.image_compose import BrandStyle, contrast_ratio  # noqa: E402

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.video_title_card"
POST = ("A model retired at 2am and nobody noticed — until our cron caught it the next morning, "
        "before a single customer saw an error.\n\nHere is how the weekly check works.")


def _concept(**kw):
    base = dict(hook_phrase="", kicker="MODEL OPS", thesis="A weekly cron catches retired models.",
                visual_anchors=(), specific_entities=(), audience="", treatment="")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _assets(tmp_path, monkeypatch):
    monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path / "assets"), raising=False)


class TestHook:
    def test_stage_1s_hook_wins(self):
        assert tc.title_card_hook(POST, _concept(hook_phrase="Our cron caught it")) == \
            "Our cron caught it"

    def test_else_a_complete_clause_of_the_post_never_an_ellipsis(self):
        hook = tc.title_card_hook(POST, None)
        assert hook and "…" not in hook and "..." not in hook
        assert hook.split()[:6] == POST.split()[:6] and hook.endswith("morning")
        assert len(hook) <= tc.TITLE_HOOK_MAX_CHARS

    def test_no_prose_no_hook(self):
        assert tc.title_card_hook("#ai https://x.co", None) is None


class TestStockGuard:
    def test_no_concept_no_stock(self):
        assert tc.stock_query_from_concept(None) is None

    def test_a_homonym_anchor_never_reaches_the_query(self):
        concept = _concept(visual_anchors=("model retirement notice", "cron schedule printout"))
        with patch("cqc_lem.utilities.ai.image_brief.usable_anchors",
                   return_value=["model retirement notice", "cron schedule printout",
                                 "error log on paper"]):
            query = tc.stock_query_from_concept(concept)
        assert query == "cron schedule printout error log on paper"
        assert "retirement" not in query and "model" not in query

    def test_only_homonyms_means_no_stock(self):
        with patch("cqc_lem.utilities.ai.image_brief.usable_anchors",
                   return_value=["model retirement", "reaching hands"]):
            assert tc.stock_query_from_concept(_concept()) is None

    def test_a_person_anchor_is_dropped(self):
        with patch("cqc_lem.utilities.ai.image_brief.usable_anchors",
                   return_value=["a manager at a desk", "sticky notes"]):
            assert tc.stock_query_from_concept(_concept()) == "sticky notes"

    def test_pexels_is_opt_in(self, monkeypatch):
        monkeypatch.delenv("VIDEO_PEXELS_FALLBACK_ENABLED", raising=False)
        assert tc.pexels_fallback_enabled() is False
        monkeypatch.setenv("VIDEO_PEXELS_FALLBACK_ENABLED", "true")
        assert tc.pexels_fallback_enabled() is True


class TestPaletteAndLayout:
    def test_gold_hook_on_charcoal_and_charcoal_hook_on_off_white(self):
        brand = BrandStyle()
        dark = tc.title_card_palette(brand, tc.GROUND_CHARCOAL)
        light = tc.title_card_palette(brand, tc.GROUND_OFF_WHITE)
        hexed = "#{:02X}{:02X}{:02X}".format
        assert dark.hook == tuple(int(brand.primary[i:i + 2], 16) for i in (1, 3, 5))
        for pal in (dark, light):
            assert contrast_ratio(hexed(*pal.hook), hexed(*pal.ground)) >= 4.5

    def test_ground_is_stable_per_piece(self):
        assert tc.pick_ground("post-1") == tc.pick_ground("post-1")
        assert {tc.pick_ground(f"p{n}") for n in range(20)} == set(tc.GROUNDS)

    @pytest.mark.parametrize("ratio", ["1:1", "9:16"])
    def test_the_hook_fits_above_the_caption_band(self, ratio):
        size = tc.TITLE_CARD_SIZES[ratio]
        layout = tc.plan_title_card("One long string silently undid four settings sections.",
                                    size=size, palette=tc.title_card_palette(),
                                    kicker="database", byline="Jane Doe")
        assert layout.kicker == "DATABASE"
        bottom = max(w.y for w in layout.words) + layout.hook_size
        assert bottom < size[1] * (1 - tc.CAPTION_CLEARANCE)
        assert all(0 < w.x < size[0] for w in layout.words)
        starts = [w.start for w in layout.words]
        assert starts == sorted(starts) and layout.reveal_end > starts[-1]

    def test_an_unsettable_hook_is_refused(self):
        with pytest.raises(ValueError):
            tc.plan_title_card("  ", size=(1080, 1080), palette=tc.title_card_palette())
        with pytest.raises(ValueError):
            tc.plan_title_card("Supercalifragilisticexpialidocious" * 4, size=(1080, 1080),
                               palette=tc.title_card_palette())

    def test_the_frames_reveal_word_by_word_and_keep_moving(self):
        layout = tc.plan_title_card("Our cron caught a retired model", size=(540, 540),
                                    palette=tc.title_card_palette(), kicker="OPS",
                                    byline="Jane Doe", seconds=7.0)
        first = tc.render_title_card_frame(layout, 0.0)
        mid = tc.render_title_card_frame(layout, layout.words[2].start + 0.4)
        end = tc.render_title_card_frame(layout, 5.0)
        last = tc.render_title_card_frame(layout, 6.9)
        gold = tuple(layout.palette.hook)

        def gold_pixels(img):
            return sum(1 for px in img.getdata() if px == gold)

        # The rule is gold too, so only compare the hook's growth before the rule draws in.
        assert gold_pixels(first) < gold_pixels(mid) < gold_pixels(end)
        assert list(end.getdata()) != list(last.getdata())  # the slab still drifts


class TestEncode:
    def _layout(self):
        return tc.plan_title_card("Our cron caught a retired model", size=(64, 64),
                                  palette=tc.title_card_palette(), seconds=6.0)

    def test_missing_ffmpeg_is_false(self, tmp_path):
        with patch(f"{_MOD}._ffmpeg", return_value=None):
            assert tc.write_title_card_video(self._layout(), str(tmp_path / "o.mp4")) is False

    def test_frames_are_piped_raw_and_the_file_is_kept(self, tmp_path):
        out = tmp_path / "o.mp4"
        process = MagicMock(returncode=0)
        process.communicate.side_effect = lambda **kw: (out.write_bytes(b"MP4"), (None, b""))[1]
        with patch(f"{_MOD}._ffmpeg", return_value="/usr/bin/ffmpeg"), \
                patch("subprocess.Popen", return_value=process) as popen:
            assert tc.write_title_card_video(self._layout(), str(out), fps=4) is True
        cmd = popen.call_args[0][0]
        assert cmd[cmd.index("-f") + 1] == "rawvideo" and cmd[cmd.index("-s") + 1] == "64x64"
        assert "libx264" in cmd and cmd[-1] == str(out)
        assert process.stdin.write.call_count == 24  # 6 s x 4 fps
        assert len(process.stdin.write.call_args[0][0]) == 64 * 64 * 3

    def test_a_non_zero_exit_removes_the_partial_file(self, tmp_path):
        out = tmp_path / "o.mp4"
        out.write_bytes(b"partial")
        process = MagicMock(returncode=1)
        process.communicate.return_value = (None, b"boom")
        with patch(f"{_MOD}._ffmpeg", return_value="/usr/bin/ffmpeg"), \
                patch("subprocess.Popen", return_value=process), \
                patch(f"{_MOD}.log_warning") as warn:
            assert tc.write_title_card_video(self._layout(), str(out), fps=2) is False
        assert not out.exists()
        assert warn.call_args[0][0] == "Title card encode failed — no title card"

    def test_a_broken_pipe_kills_the_encoder(self, tmp_path):
        process = MagicMock(returncode=-9)
        process.stdin.write.side_effect = BrokenPipeError()
        process.communicate.return_value = (None, b"")
        with patch(f"{_MOD}._ffmpeg", return_value="/usr/bin/ffmpeg"), \
                patch("subprocess.Popen", return_value=process):
            assert tc.write_title_card_video(self._layout(), str(tmp_path / "o.mp4")) is False
        process.kill.assert_called_once()

    def test_an_unstartable_encoder_is_false(self, tmp_path):
        with patch(f"{_MOD}._ffmpeg", return_value="/usr/bin/ffmpeg"), \
                patch("subprocess.Popen", side_effect=OSError("exec")):
            assert tc.write_title_card_video(self._layout(), str(tmp_path / "o.mp4")) is False

    def test_an_empty_output_is_false(self, tmp_path):
        out = tmp_path / "o.mp4"
        process = MagicMock(returncode=0)
        process.communicate.side_effect = lambda **kw: (out.write_bytes(b""), (None, b""))[1]
        with patch(f"{_MOD}._ffmpeg", return_value="/usr/bin/ffmpeg"), \
                patch("subprocess.Popen", return_value=process):
            assert tc.write_title_card_video(self._layout(), str(out), fps=1) is False
        assert not out.exists()


class TestCreate:
    def test_writes_a_title_card_named_file_in_the_assets_volume(self, tmp_path):
        with patch(f"{_MOD}.write_title_card_video", return_value=True) as write:
            path = tc.create_title_card_video(POST, user_id=None, post_id=7,
                                              concept=_concept(hook_phrase="Our cron caught it"),
                                              ratio="9:16", byline="Jane Doe")
        assert path and "/videos/title_card/title_card_7_" in path and path.endswith(".mp4")
        layout = write.call_args[0][0]
        assert layout.size == (1080, 1920) and layout.byline == "Jane Doe"
        assert " ".join(w.text for w in layout.words) == "Our cron caught it"

    def test_no_hook_no_card(self):
        with patch(f"{_MOD}.write_title_card_video") as write:
            assert tc.create_title_card_video("#tag", post_id=1) is None
        write.assert_not_called()

    def test_a_failed_encode_is_none(self):
        with patch(f"{_MOD}.write_title_card_video", return_value=False):
            assert tc.create_title_card_video(POST, post_id=1) is None

    def test_an_unexpected_fault_never_raises(self):
        with patch(f"{_MOD}.plan_title_card", side_effect=RuntimeError("x")), \
                patch(f"{_MOD}.log_warning") as warn:
            assert tc.create_title_card_video(POST, post_id=1) is None
        warn.assert_called_once()

    def test_the_brand_falls_back_to_the_reference(self):
        with patch("cqc_lem.utilities.brand_kit.brand_clause_for_user",
                   side_effect=RuntimeError("db")):
            assert tc._brand_for(3) == BrandStyle()

    def test_seconds_are_clamped_to_the_designed_range(self, monkeypatch):
        monkeypatch.setenv("VIDEO_TITLE_CARD_SECONDS", "30")
        assert tc.title_card_seconds() == tc.TITLE_CARD_MAX_SECONDS
        monkeypatch.setenv("VIDEO_TITLE_CARD_SECONDS", "nope")
        assert tc.title_card_seconds() == 7.0


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="needs a real ffmpeg + ffprobe")
def test_a_real_encode_is_a_playable_mp4_of_the_designed_length(tmp_path):
    layout = tc.plan_title_card("Our cron caught a retired model", size=(320, 320),
                                palette=tc.title_card_palette(), kicker="OPS",
                                byline="Jane Doe", seconds=6.0)
    out = tmp_path / "title_card_1.mp4"
    # The unit lane's hermetic guard hides ffmpeg; this one test encodes for real.
    with patch.object(tc, "_ffmpeg", return_value=shutil.which("ffmpeg")):
        assert tc.write_title_card_video(layout, str(out), fps=12) is True
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=width,height,codec_name", "-of",
                            "default=noprint_wrappers=1", str(out)],
                           capture_output=True, text=True, check=True).stdout
    assert "codec_name=h264" in probe and "width=320" in probe
    duration = float(next(ln for ln in probe.splitlines() if ln.startswith("duration=")
                          ).split("=")[1])
    assert 5.5 <= duration <= 6.5
