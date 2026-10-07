"""The $0 branded motion title card — the default video fallback (showcase round 4).

ffmpeg is mocked everywhere except the one ``slow`` test, which encodes a real clip when an ffmpeg
binary is on the PATH. What these pin: the card sets the post's OWN words, the stock query can never
be a raw homonym, frame 0 (LinkedIn's thumbnail) carries the whole hook while the clip still moves,
the three layouts rotate, and every encoder failure leaves no file behind.
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

    def test_an_unsettable_hook_is_refused(self):
        with pytest.raises(ValueError):
            tc.plan_title_card("  ", size=(1080, 1080), palette=tc.title_card_palette())
        with pytest.raises(ValueError):
            tc.plan_title_card("Supercalifragilisticexpialidocious" * 4, size=(1080, 1080),
                               palette=tc.title_card_palette())

    def test_frame_zero_shows_the_whole_hook_and_the_clip_still_moves(self):
        # Round 5: LinkedIn's thumbnail IS frame 0 — slot_123 sat in the feed as a lone "AI".
        layout = tc.plan_title_card("Our cron caught a retired model", size=(540, 540),
                                    palette=tc.title_card_palette(), kicker="OPS",
                                    byline="Jane Doe", seconds=7.0)
        first = tc.render_title_card_frame(layout, 0.0)
        later = tc.render_title_card_frame(layout, 3.0)
        last = tc.render_title_card_frame(layout, 6.9)
        for word in layout.words:  # every word's box is inked on frame 0 exactly as later
            box = (word.x, word.y, word.x + layout.hook_size, word.y + layout.hook_size)
            assert first.crop(box).tobytes() == later.crop(box).tobytes()
            assert len(set(first.crop(box).getdata())) > 1
        byline_box = (*layout.byline_xy, layout.byline_xy[0] + 60, layout.byline_xy[1] + 20)
        assert len(set(first.crop(byline_box).getdata())) > 1  # the byline is there at t=0
        assert list(later.getdata()) != list(last.getdata())  # the slab drifts, the rule extends

    def test_the_rule_extends_from_its_full_frame_zero_length(self):
        layout = tc.plan_title_card("Our cron caught a retired model", size=(540, 540),
                                    palette=tc.title_card_palette(), seconds=7.0)
        left, top, right, _ = layout.rule_box
        y = top + 1

        def rule_end(img):
            return max(x for x in range(img.size[0]) if img.getpixel((x, y)) == layout.palette.rule)

        assert rule_end(tc.render_title_card_frame(layout, 0.0)) >= right - 1
        assert rule_end(tc.render_title_card_frame(layout, 6.9)) > right + 10


class TestVariants:
    def test_number_led_needs_a_leading_figure(self):
        assert tc.VARIANT_NUMBER in tc.available_variants("30% of AI spend buys nothing")
        assert tc.VARIANT_NUMBER not in tc.available_variants("We saved $30K per quarter")
        assert tc.VARIANT_NUMBER not in tc.available_variants("30%")

    def test_number_led_lifts_the_hero_and_sets_the_rest(self):
        layout = tc.plan_title_card("30% of AI spend buys nothing you can see", size=(540, 540),
                                    palette=tc.title_card_palette(), variant=tc.VARIANT_NUMBER)
        assert layout.variant == tc.VARIANT_NUMBER and layout.hero == "30%"
        assert " ".join(w.text for w in layout.words) == "Of AI spend buys nothing you can see"
        assert min(w.y for w in layout.words) > layout.hero_xy[1]
        frame = tc.render_title_card_frame(layout, 0.0)
        assert frame.size == (540, 540)

    def test_number_led_without_a_figure_is_the_poster(self):
        layout = tc.plan_title_card("Our cron caught a retired model", size=(540, 540),
                                    palette=tc.title_card_palette(), variant=tc.VARIANT_NUMBER)
        assert layout.variant == tc.VARIANT_POSTER and layout.hero == ""

    def test_an_unfittable_hero_falls_back_to_the_poster(self):
        real = tc._fit_lines
        calls = []

        def hero_never_fits(draw, text, *args):
            calls.append(text)
            return None if len(calls) == 1 else real(draw, text, *args)

        with patch(f"{_MOD}._fit_lines", side_effect=hero_never_fits):
            layout = tc.plan_title_card("30% of AI spend buys nothing", size=(320, 320),
                                        palette=tc.title_card_palette(),
                                        variant=tc.VARIANT_NUMBER)
        assert calls[0] == "30%" and layout.variant == tc.VARIANT_POSTER and not layout.hero
        assert " ".join(w.text for w in layout.words) == "30% of AI spend buys nothing"

    @pytest.mark.parametrize("ground", tc.GROUNDS)
    def test_question_kicker_centres_the_hook_under_a_tag(self, ground):
        layout = tc.plan_title_card("What is your AI budget buying?", size=(540, 540),
                                    palette=tc.title_card_palette(None, ground), kicker="budget",
                                    byline="Jane Doe", variant=tc.VARIANT_QUESTION)
        assert layout.kicker_box and layout.kicker_box[3] < min(w.y for w in layout.words)
        left = min(w.x for w in layout.words)
        right = max(w.x for w in layout.words)
        assert abs((540 - left) - right) < 540 * 0.6  # not hard left like the poster
        frame = tc.render_title_card_frame(layout, 0.0)
        x0, y0, x1, y1 = layout.kicker_box
        assert frame.getpixel((x0 + 1, y0 + 1)) == layout.palette.rule

    def test_a_question_hook_prefers_the_question_layout_unless_it_just_ran(self):
        hook = "What is your AI budget buying?"
        assert tc.pick_variant(hook, []) == tc.VARIANT_QUESTION
        assert tc.pick_variant(hook, [tc.VARIANT_QUESTION]) != tc.VARIANT_QUESTION

    def test_consecutive_cards_never_repeat_a_layout(self):
        hook = "30% of AI spend buys nothing you can see"
        recent: list = []
        picks = []
        for _ in range(6):
            pick = tc.pick_variant(hook, recent, "seed")
            picks.append(pick)
            recent.insert(0, pick)
        assert all(a != b for a, b in zip(picks, picks[1:]))
        assert set(picks) == set(tc.TITLE_CARD_VARIANTS)

    def test_history_round_trips_on_disk_and_is_capped(self):
        assert tc.recent_variants(None) == [] and tc.recent_variants(5) == []
        tc.record_variant(None, tc.VARIANT_POSTER)  # no author: a no-op
        for variant in (tc.VARIANT_POSTER, tc.VARIANT_NUMBER, tc.VARIANT_QUESTION) * 3:
            tc.record_variant(5, variant)
        history = tc.recent_variants(5)
        assert history[0] == tc.VARIANT_QUESTION and len(history) == 6

    def test_unreadable_or_alien_history_reads_as_none(self):
        path = tc.variant_history_path(6)
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not json")
        assert tc.recent_variants(6) == []
        with open(path, "w") as fh:
            fh.write('{"a": 1}')
        assert tc.recent_variants(6) == []
        with open(path, "w") as fh:
            fh.write('["number_led", "spiral"]')
        assert tc.recent_variants(6) == [tc.VARIANT_NUMBER]

    def test_an_unwritable_history_never_raises(self):
        with patch("os.makedirs", side_effect=OSError("ro")), \
                patch(f"{_MOD}.log_debug") as debug:
            tc.record_variant(7, tc.VARIANT_POSTER)
        debug.assert_called_once()


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

    def test_the_layout_rotates_per_author_and_is_recorded(self):
        concept = _concept(hook_phrase="30% of AI spend buys nothing you can see")
        layouts = []
        with patch(f"{_MOD}.write_title_card_video", return_value=True) as write:
            for _ in range(3):
                tc.create_title_card_video(POST, user_id=11, post_id=7, concept=concept)
                layouts.append(write.call_args[0][0].variant)
        assert len(set(layouts)) == 3
        assert tc.recent_variants(11) == layouts[::-1]

    def test_a_question_card_without_a_kicker_derives_one(self):
        concept = _concept(hook_phrase="What is your AI budget buying?", kicker="")
        with patch(f"{_MOD}.write_title_card_video", return_value=True) as write:
            tc.create_title_card_video("Your AI budget is leaking. " + POST, user_id=12,
                                       post_id=8, concept=concept)
        layout = write.call_args[0][0]
        assert layout.variant == tc.VARIANT_QUESTION

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
