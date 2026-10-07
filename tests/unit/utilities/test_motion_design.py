"""Kinetic motion design for the $0 title card and GIF loops (docs/motion-design.md).

ffmpeg is mocked everywhere (the unit lane's ``_no_real_title_card_encode`` guard hides it) except
the one ``slow`` test. What these pin: frame 0 carries the whole hook and holds; a GIF ends in its
start state; the frame, fps and size caps hold on the file; the style rotates per author and never
repeats; and a counter only ever counts to a TRACEABLE figure, otherwise the piece is kinetic type.
"""
import os
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageChops  # noqa: E402

from cqc_lem.utilities import (
    motion_design as md,  # noqa: E402
    video_title_card as tc,  # noqa: E402
)
from cqc_lem.utilities.ai.image_graphics import (  # noqa: E402
    UngroundedFactError,
    validate_graphic_facts,
)
from cqc_lem.utilities.video_captions import caption_band_top  # noqa: E402

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.motion_design"
_TC = "cqc_lem.utilities.video_title_card"

POST = ("The starter plan is $25.99 a month vs $252 a month for the agency retainer.\n\n"
        "Open tickets fell from 529 to 432 after the triage bot shipped.\n\n"
        "We cut response time by 38% in one quarter.\n\n"
        "Audit every recurring invoice\nCancel the tools nobody opened\n"
        "Move support to one inbox\nReview the spend every month")
_SENT = "The starter plan is $25.99 a month vs $252 a month for the agency retainer."


def _graphic() -> dict:
    graphic = validate_graphic_facts({
        "comparison": {"items": [
            {"label": "starter plan", "value": "25.99", "unit": "$", "source_sentence": _SENT},
            {"label": "agency retainer", "value": "252", "unit": "$", "source_sentence": _SENT}],
            "highlight": 0, "annotation": "The starter plan is $25.99 a month"},
        "thesis_stat": {"label": "response time in one quarter", "value": "38", "unit": "%",
                        "source_sentence": "We cut response time by 38% in one quarter."},
    }, POST)
    graphic.update({k: v for k, v in md.graphic_from_post(POST).items() if k != "stat"})
    return graphic


GRAPHIC = _graphic()
SIZE = (540, 675)
HOOK = "Your AI budget is buying tools nobody opens"


@pytest.fixture(autouse=True)
def _assets(tmp_path, monkeypatch):
    monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path / "assets"), raising=False)


def _palette(ground=tc.GROUND_CHARCOAL):
    return tc.title_card_palette(None, ground)


def _plan(style, mode=md.MODE_GIF, size=SIZE, ground=tc.GROUND_CHARCOAL, hook=HOOK, **kw):
    if style in md.KINETIC_STYLES:
        layout = tc.plan_title_card(hook, size=size, palette=_palette(ground), kicker="AI SPEND",
                                    byline="Jane Doe", seconds=7.0,
                                    variant=kw.get("variant", tc.VARIANT_POSTER))
        return md.plan_kinetic(layout, style, mode)
    return md.plan_data(style, kw.get("graphic", GRAPHIC), hook, size=size,
                        palette=_palette(ground), kicker="AI SPEND", byline="Jane Doe",
                        seconds=7.0, mode=mode)


def _same(a, b) -> bool:
    return ImageChops.difference(a, b).getbbox() is None


class TestFacts:
    def test_the_synthetic_post_carries_every_section(self):
        assert set(md.available_styles(GRAPHIC)) == set(md.MOTION_STYLES)

    def test_the_posts_own_paragraphs_are_read_with_the_post_as_evidence(self):
        found = md.graphic_from_post(POST)
        assert found["before_after"]["before"]["display"] == "529"
        assert found["before_after"]["after"]["display"] == "432"
        assert [s["text"] for s in found["steps"]][-1] == "Review the spend every month"
        assert md.graphic_from_post("") == {} and md.graphic_from_post(None) == {}

    def test_a_paragraph_that_cannot_be_read_is_skipped(self):
        with patch("cqc_lem.utilities.ai.image_graphics.slide_graphic",
                   side_effect=RuntimeError("x")):
            assert md.graphic_from_post(POST) == {}

    def test_stage_1s_graphic_wins_over_the_posts_figures(self):
        concept = SimpleNamespace(graphic={"comparison": GRAPHIC["comparison"]})
        assert md.motion_graphic(POST, concept) == concept.graphic
        empty = SimpleNamespace(graphic={"rejected": []})
        assert "steps" in md.motion_graphic(POST, empty)

    def test_an_untraceable_figure_is_never_animated(self):
        tampered = dict(GRAPHIC, stat=dict(GRAPHIC["stat"], display="99%"))
        assert md.STYLE_STAT_COUNTER not in md.available_styles(tampered)
        with pytest.raises(UngroundedFactError):
            _plan(md.STYLE_STAT_COUNTER, graphic=tampered)
        plan = md.build_motion(HOOK, graphic=tampered, size=SIZE, palette=_palette(),
                               recent=[], prefer=md.STYLE_STAT_COUNTER)
        assert plan.style != md.STYLE_STAT_COUNTER

    def test_no_facts_is_kinetic_typography(self):
        assert md.available_styles(None) == md.KINETIC_STYLES
        plan = md.build_motion(HOOK, graphic={}, size=SIZE, palette=_palette())
        assert plan.style in md.KINETIC_STYLES

    def test_unreadable_facts_skip_the_style(self):
        with patch("cqc_lem.utilities.ai.image_graphics.assert_traceable",
                   side_effect=TypeError("bad")):
            assert md.available_styles(GRAPHIC) == md.KINETIC_STYLES


class TestCounter:
    def test_the_last_value_is_the_facts_own_display(self):
        for display in ("$25.99", "38%", "30,000", "$30K", "12 hours", "3x"):
            assert md.counter_text(display, 1.0) == display

    def test_it_counts_up_from_zero_at_the_displays_precision(self):
        assert md.counter_text("$25.99", 0.0) == "$0.00"
        assert md.counter_text("30,000", 0.5) == "15,000"
        assert md.counter_text("38%", 0.5) == "19%"
        values = [float(md.counter_text("$25.99", p / 10)[1:]) for p in range(11)]
        assert values == sorted(values) and values[-1] == 25.99

    def test_a_display_without_a_number_is_refused(self):
        with pytest.raises(ValueError):
            md.counter_text("n/a", 0.5)

    def test_the_counter_frame_ends_on_the_traced_figure(self):
        plan = _plan(md.STYLE_STAT_COUNTER, mode=md.MODE_MP4)
        assert plan.data["fact"]["display"] == "38%"
        drawn = []
        real = md.counter_text
        with patch(f"{_MOD}.counter_text", side_effect=lambda d, p: drawn.append(real(d, p))
                   or real(d, p)):
            for t in md.frame_times(plan, 24):
                md.render_motion_frame(plan, t)
        assert int(drawn[0][:-1]) <= 3 and drawn[-1] == "38%"
        assert all(int(v[:-1]) <= 38 for v in drawn)

    def test_ease_out_cubic_is_clamped_and_monotonic(self):
        assert md.ease_out_cubic(-1) == 0 and md.ease_out_cubic(2) == 1
        samples = [md.ease_out_cubic(p / 20) for p in range(21)]
        assert samples == sorted(samples) and samples[5] > 0.25  # fast start, soft landing


class TestFrameZero:
    @pytest.mark.parametrize("style", md.MOTION_STYLES)
    def test_frame_zero_holds_before_anything_moves(self, style):
        plan = _plan(style, mode=md.MODE_MP4)
        first = md.render_motion_frame(plan, 0.0)
        assert _same(first, md.render_motion_frame(plan, md.MOTION_HOLD_SECONDS - 0.01))
        assert not _same(first, md.render_motion_frame(plan, 2.5))  # and then it moves

    @pytest.mark.parametrize("style", md.KINETIC_STYLES)
    @pytest.mark.parametrize("variant", [tc.VARIANT_POSTER, tc.VARIANT_NUMBER])
    def test_kinetic_frame_zero_is_the_complete_static_card(self, style, variant):
        hook = "38% of AI spend buys nothing you can see"
        layout = tc.plan_title_card(hook, size=SIZE, palette=_palette(), seconds=7.0,
                                    variant=variant)
        plan = md.plan_kinetic(layout, style)
        first = md.render_motion_frame(plan, 0.0)
        card = tc.render_title_card_frame(layout, 0.0)
        diff = ImageChops.difference(first, card).convert("L")
        # Text composited from a sprite vs drawn in place: identical but for anti-aliasing.
        assert sum(diff.histogram()[49:]) < SIZE[0] * SIZE[1] * 0.002
        if variant == tc.VARIANT_NUMBER:
            assert any(s.hero for s in plan.sprites)

    @pytest.mark.parametrize("style", md.DATA_STYLES)
    def test_data_frame_zero_sets_the_hook_and_nothing_of_the_data(self, style):
        plan = _plan(style, mode=md.MODE_MP4)
        first = md.render_motion_frame(plan, 0.0)
        x0, y0, x1, y1 = plan.region
        region = first.crop((x0, y0, x1, y1))
        assert region.getcolors() == [(region.size[0] * region.size[1], plan.palette.ground)]
        hook_band = first.crop((0, round(SIZE[1] * 0.1), SIZE[0], y0))
        assert any(c == plan.palette.hook for _n, c in hook_band.getcolors(1 << 16))

    def test_a_hook_that_cannot_be_set_is_refused(self):
        with pytest.raises(ValueError):
            _plan(md.STYLE_STAT_COUNTER, hook="")
        with pytest.raises(ValueError):
            md.plan_data(md.STYLE_KINETIC_MASK, GRAPHIC, HOOK, size=SIZE, palette=_palette())
        with pytest.raises(ValueError):
            _plan(md.STYLE_STAT_COUNTER, hook="Supercalifragilisticexpialidocious" * 3)


class TestCaptionBand:
    @pytest.mark.parametrize("size", [(1080, 1080), (1080, 1350), (1080, 1920)])
    @pytest.mark.parametrize("style", md.DATA_STYLES)
    def test_nothing_sits_under_the_caption_band(self, style, size):
        plan = _plan(style, mode=md.MODE_MP4, size=size)
        band = caption_band_top(size)
        assert plan.region[3] <= band
        last = md.render_motion_frame(plan, plan.seconds - 0.05)
        below = last.crop((0, band, round(size[0] * 0.46) - 1, size[1]))
        assert below.getcolors() == [(below.size[0] * below.size[1], plan.palette.ground)]

    def test_a_byline_too_wide_for_the_top_row_is_refused(self):
        with pytest.raises(ValueError):
            md.plan_data(md.STYLE_STAT_COUNTER, GRAPHIC, HOOK, size=SIZE, palette=_palette(),
                         byline="Wolfeschlegelsteinhausenbergerdorff " * 3)

    def test_a_long_kicker_yields_to_the_byline(self):
        plan = md.plan_data(md.STYLE_STAT_COUNTER, GRAPHIC, HOOK, size=SIZE,
                            palette=_palette(), kicker="A VERY LONG TOPIC TAG INDEED",
                            byline="Jane Doe-Smithson")
        assert plan.style == md.STYLE_STAT_COUNTER


class TestLoop:
    @pytest.mark.parametrize("ground", tc.GROUNDS)
    @pytest.mark.parametrize("style", md.MOTION_STYLES)
    def test_a_gif_ends_in_its_start_state(self, style, ground):
        plan = _plan(style, ground=ground)
        times = md.frame_times(plan, md.MOTION_GIF_MAX_FPS)
        assert times[0] == 0.0 and times[-1] == plan.seconds
        assert len(times) <= 250
        frames = [md.render_motion_frame(plan, t) for t in times]
        assert _same(frames[0], frames[-1])
        assert not _same(frames[0], frames[len(frames) // 2])

    def test_an_mp4_is_six_to_eight_seconds_at_24_fps_and_holds_its_end(self):
        plan = _plan(md.STYLE_CHART_DRAW, mode=md.MODE_MP4)
        times = md.frame_times(plan, tc.TITLE_CARD_FPS)
        assert len(times) == 7 * 24 and times[-1] < plan.seconds
        assert _same(md.render_motion_frame(plan, 6.0), md.render_motion_frame(plan, 6.9))


class TestStyles:
    def test_the_gold_bar_lands_last_then_the_annotation(self):
        plan = _plan(md.STYLE_CHART_DRAW, mode=md.MODE_MP4)
        gold = next(n for n, r in enumerate(plan.data["rows"]) if r["gold"])
        assert plan.data["order"][-1] == gold
        frame = md.render_motion_frame(plan, md.MOTION_HOLD_SECONDS + 0.7)
        row = plan.data["rows"][gold]
        x = plan.region[0] + row["length"] - 2
        y = row["bar_y"] + plan.data["bar_h"] // 2
        assert frame.getpixel((x, y)) != plan.palette.rule  # still growing
        done = md.render_motion_frame(plan, 5.0)
        assert done.getpixel((x, y)) == plan.palette.rule

    def test_the_last_checklist_item_stays_unticked(self):
        plan = _plan(md.STYLE_CHECKLIST_TICK, mode=md.MODE_MP4)
        assert plan.data["states"][-1] == "open" and plan.data["states"][0] == "check"
        end = md.render_motion_frame(plan, 6.5)
        box = plan.data["box"]
        inside = lambda row: (plan.region[0] + box // 2 - 2, row["y"] + box // 4)  # noqa: E731
        assert end.getpixel(inside(plan.data["rows"][-1])) == plan.palette.ground
        assert end.getpixel(inside(plan.data["rows"][0])) == plan.palette.rule

    def test_items_appear_one_by_one(self):
        plan = _plan(md.STYLE_CHECKLIST_TICK, mode=md.MODE_MP4)
        frame = md.render_motion_frame(plan, md.MOTION_HOLD_SECONDS + 0.45)
        rows = plan.data["rows"]
        box = plan.data["box"]
        edge = lambda row: (plan.region[0] + 1, row["y"] + box // 2)  # noqa: E731
        assert frame.getpixel(edge(rows[0])) == plan.palette.rule
        assert frame.getpixel(edge(rows[-1])) == plan.palette.ground

    def test_the_gold_divider_wipes_before_into_after(self):
        plan = _plan(md.STYLE_BEFORE_AFTER_WIPE, mode=md.MODE_MP4)
        x0, y0, x1, y1 = plan.region
        before = md.render_motion_frame(plan, md._WIPE_AT - 0.1).crop(plan.region)
        mid = md.render_motion_frame(plan, md._WIPE_AT + md._WIPE_SECONDS / 3)
        after = md.render_motion_frame(plan, 6.5).crop(plan.region)
        assert _same(after, plan.data["layers"]["after"].convert("RGB"))
        assert _same(before, plan.data["layers"]["before"].convert("RGB"))
        golds = [x for x in range(x0, x1) if mid.getpixel((x, (y0 + y1) // 2))
                 == plan.palette.rule]
        assert golds and x0 < golds[0] < x1

    def test_kinetic_lines_reveal_and_the_key_word_is_underlined(self):
        plan = _plan(md.STYLE_KINETIC_MASK, mode=md.MODE_MP4)
        assert plan.underline
        x0, y0, x1, y1 = plan.underline
        start = md.render_motion_frame(plan, 0.0)
        end = md.render_motion_frame(plan, 6.5)
        assert start.getpixel(((x0 + x1) // 2, y0 + 1)) != plan.palette.rule
        assert end.getpixel(((x0 + x1) // 2, y0 + 1)) == plan.palette.rule
        mid = md.render_motion_frame(plan, md.MOTION_HOLD_SECONDS + 0.1)
        assert not _same(start, mid)

    def test_the_key_word_prefers_a_figure_else_the_longest_content_word(self):
        word = lambda t: SimpleNamespace(text=t)  # noqa: E731
        assert md._key_word([word("We"), word("saved"), word("$30K")]) == 2
        assert md._key_word([word("Your"), word("pipeline"), word("leaks")]) == 1
        assert md._key_word([word("is"), word("it")]) is None

    def test_slide_and_mask_differ_while_moving(self):
        slide = _plan(md.STYLE_KINETIC_SLIDE, mode=md.MODE_MP4)
        mask = _plan(md.STYLE_KINETIC_MASK, mode=md.MODE_MP4)
        t = md.MOTION_HOLD_SECONDS + 0.1
        assert not _same(md.render_motion_frame(slide, t), md.render_motion_frame(mask, t))

    def test_too_many_bars_or_steps_for_the_region_are_refused(self):
        tiny = (200, 200)
        with pytest.raises(ValueError):
            _plan(md.STYLE_CHART_DRAW, size=tiny)
        with pytest.raises(ValueError):
            _plan(md.STYLE_CHECKLIST_TICK, size=tiny)


class TestRotation:
    def test_never_the_same_style_twice_in_a_row(self):
        recent: list = []
        picks = []
        for _ in range(12):
            pick = md.pick_style(md.available_styles(GRAPHIC), recent, "seed")
            picks.append(pick)
            recent.insert(0, pick)
        assert all(a != b for a, b in zip(picks, picks[1:]))
        assert set(picks) == set(md.MOTION_STYLES)
        assert picks[0] in md.DATA_STYLES  # a verified figure leads when there is one

    def test_kinetic_alone_still_alternates(self):
        assert md.pick_style(md.KINETIC_STYLES, [md.STYLE_KINETIC_MASK]) == \
            md.STYLE_KINETIC_SLIDE
        assert md.pick_style([], [md.STYLE_KINETIC_SLIDE]) == md.STYLE_KINETIC_MASK

    def test_a_preferred_style_runs_unless_it_just_did(self):
        options = md.available_styles(GRAPHIC)
        assert md.pick_style(options, [], prefer=md.STYLE_CHART_DRAW) == md.STYLE_CHART_DRAW
        assert md.pick_style(options, [md.STYLE_CHART_DRAW],
                             prefer=md.STYLE_CHART_DRAW) != md.STYLE_CHART_DRAW

    def test_history_shares_the_card_file_and_keeps_its_layouts(self):
        assert md.recent_motions(None) == []
        tc.record_variant(5, tc.VARIANT_NUMBER)
        md.record_motion(5, md.STYLE_CHART_DRAW)
        md.record_motion(5, md.STYLE_KINETIC_MASK)
        md.record_motion(None, md.STYLE_KINETIC_MASK)  # no author: a no-op
        assert md.recent_motions(5) == [md.STYLE_KINETIC_MASK, md.STYLE_CHART_DRAW]
        assert tc.recent_variants(5) == [tc.VARIANT_NUMBER]
        for _ in range(10):
            md.record_motion(5, md.STYLE_STAT_COUNTER)
        assert len(md.recent_motions(5)) == 6

    def test_a_legacy_list_history_reads_as_layouts(self):
        path = tc.variant_history_path(9)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write('["poster" , "statement_poster"]')
        assert tc.recent_variants(9) == [tc.VARIANT_POSTER]
        assert md.recent_motions(9) == []
        md.record_motion(9, md.STYLE_CHECKLIST_TICK)
        assert tc.recent_variants(9) == [tc.VARIANT_POSTER]
        with open(path, "w") as fh:
            fh.write('"just a string"')
        assert tc.read_card_history(9) == {"variants": [], "motions": []}
        with open(path, "w") as fh:
            fh.write('{"motions": "chart_draw", "variants": [1, "number_led"]}')
        assert tc.read_card_history(9) == {"variants": ["number_led"], "motions": []}


class TestBuild:
    def test_an_unsettable_data_style_falls_to_the_next(self):
        with patch(f"{_MOD}.plan_data", side_effect=ValueError("no room")):
            plan = md.build_motion(HOOK, graphic=GRAPHIC, size=SIZE, palette=_palette())
        assert plan.style in md.KINETIC_STYLES

    def test_a_fault_is_warned_and_the_next_style_tried(self):
        with patch(f"{_MOD}.plan_data", side_effect=RuntimeError("boom")), \
                patch(f"{_MOD}.log_warning") as warn:
            plan = md.build_motion(HOOK, graphic=GRAPHIC, size=SIZE, palette=_palette())
        assert plan.style in md.KINETIC_STYLES and warn.call_count == 4

    def test_nothing_settable_is_none(self):
        with patch(f"{_MOD}.plan_kinetic", side_effect=ValueError("x")):
            assert md.build_motion(HOOK, graphic=None, size=SIZE, palette=_palette()) is None

    def test_a_card_layout_of_another_size_is_replanned(self):
        layout = tc.plan_title_card(HOOK, size=(300, 300), palette=_palette())
        plan = md.build_motion(HOOK, graphic=None, size=SIZE, palette=_palette(),
                               card_layout=layout)
        assert plan.size == SIZE


def _fake_encoder(out_path_bytes=b"GIF89a"):
    def encode(command, frames, out_path, **kw):
        encode.frames = sum(1 for _ in frames)
        encode.commands.append(command)
        with open(out_path, "wb") as fh:
            fh.write(out_path_bytes)
        return True
    encode.commands = []
    return encode


class TestGifEncode:
    def test_missing_ffmpeg_is_false(self, tmp_path):
        assert md.write_motion_gif(_plan(md.STYLE_KINETIC_MASK), str(tmp_path / "o.gif")) is False

    def test_an_mp4_plan_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            md.write_motion_gif(_plan(md.STYLE_KINETIC_MASK, mode=md.MODE_MP4), "o.gif")

    def test_fps_is_capped_and_the_file_checked(self, tmp_path):
        encode = _fake_encoder()
        out = tmp_path / "o.gif"
        with patch(f"{_TC}._ffmpeg", return_value="/usr/bin/ffmpeg"), \
                patch(f"{_TC}.encode_frames", side_effect=encode), \
                patch("cqc_lem.utilities.animated_loop.gif_within_limits",
                      return_value=True) as limits:
            assert md.write_motion_gif(_plan(md.STYLE_STAT_COUNTER), str(out), fps=30) is True
        cmd = encode.commands[0]
        assert cmd[cmd.index("-r") + 1] == "12" and cmd[cmd.index("-loop") + 1] == "0"
        assert "palettegen" in cmd[cmd.index("-filter_complex") + 1]
        assert encode.frames == 6 * 12 + 1
        limits.assert_called_once_with(str(out))

    def test_over_limits_steps_down_then_gives_up(self, tmp_path):
        encode = _fake_encoder()
        out = tmp_path / "o.gif"
        with patch(f"{_TC}._ffmpeg", return_value="/usr/bin/ffmpeg"), \
                patch(f"{_TC}.encode_frames", side_effect=encode), \
                patch("cqc_lem.utilities.animated_loop.gif_within_limits", return_value=False):
            assert md.write_motion_gif(_plan(md.STYLE_KINETIC_SLIDE), str(out)) is False
        widths = [c[c.index("-filter_complex") + 1].split(":")[0] for c in encode.commands]
        fps = [int(c[c.index("-r") + 1]) for c in encode.commands]
        assert len(encode.commands) == 3 and widths == sorted(widths, key=lambda w: -int(
            w.split("=")[1]))
        assert fps == sorted(fps, reverse=True) and all(f <= 12 for f in fps)
        assert not out.exists()

    def test_a_failed_encode_stops(self, tmp_path):
        with patch(f"{_TC}._ffmpeg", return_value="/usr/bin/ffmpeg"), \
                patch(f"{_TC}.encode_frames", return_value=False) as encode:
            assert md.write_motion_gif(_plan(md.STYLE_KINETIC_SLIDE),
                                       str(tmp_path / "o.gif")) is False
        encode.assert_called_once()

    def test_the_shared_encoder_pipes_every_frame(self, tmp_path):
        out = tmp_path / "o.gif"
        process = MagicMock(returncode=0)
        process.communicate.side_effect = lambda **kw: (out.write_bytes(b"GIF"), (None, b""))[1]
        with patch("subprocess.Popen", return_value=process):
            assert tc.encode_frames(["ffmpeg"], [b"a", b"b"], str(out), what="Motion GIF")
        assert process.stdin.write.call_count == 2

    def test_the_title_card_encodes_the_motion_frames(self, tmp_path):
        plan = _plan(md.STYLE_CHECKLIST_TICK, mode=md.MODE_MP4)
        out = tmp_path / "o.mp4"
        process = MagicMock(returncode=0)
        process.communicate.side_effect = lambda **kw: (out.write_bytes(b"MP4"), (None, b""))[1]
        with patch(f"{_TC}._ffmpeg", return_value="/usr/bin/ffmpeg"), \
                patch("subprocess.Popen", return_value=process) as popen:
            assert tc.write_title_card_video(None, str(out), fps=4, motion=plan) is True
        cmd = popen.call_args[0][0]
        assert cmd[cmd.index("-s") + 1] == f"{SIZE[0]}x{SIZE[1]}"
        assert process.stdin.write.call_count == 28  # 7 s x 4 fps


class TestCreate:
    def test_no_hook_no_gif(self):
        assert md.create_motion_gif("  ") is None

    def test_a_gif_is_written_and_its_style_recorded(self, tmp_path):
        out = tmp_path / "m.gif"
        with patch(f"{_MOD}.write_motion_gif", return_value=True) as write:
            path = md.create_motion_gif(HOOK, graphic=GRAPHIC, user_id=3, post_id=4,
                                        ratio="4:5", out_path=str(out),
                                        prefer=md.STYLE_CHART_DRAW)
        plan = write.call_args[0][0]
        assert path == str(out) and plan.style == md.STYLE_CHART_DRAW
        assert plan.size == (md.MOTION_GIF_WIDTH, 900) and plan.mode == md.MODE_GIF
        assert md.recent_motions(3) == [md.STYLE_CHART_DRAW]

    def test_a_failed_write_is_none_and_records_nothing(self):
        with patch(f"{_MOD}.write_motion_gif", return_value=False):
            assert md.create_motion_gif(HOOK, user_id=3) is None
        assert md.recent_motions(3) == []

    def test_nothing_settable_is_none(self):
        with patch(f"{_MOD}.build_motion", return_value=None):
            assert md.create_motion_gif(HOOK) is None

    def test_a_fault_never_raises(self):
        with patch(f"{_MOD}.build_motion", side_effect=RuntimeError("x")), \
                patch(f"{_MOD}.log_warning") as warn:
            assert md.create_motion_gif(HOOK) is None
        warn.assert_called_once()

    def test_a_receipt_animates_the_stills_own_archetype(self):
        receipt = {"hook_text": "The cheapest plan was the right one",
                   "archetype_rendered": "highlight_chart",
                   "concept": {"graphic": GRAPHIC, "kicker": "AI SPEND"}}
        with patch(f"{_MOD}.create_motion_gif", return_value="/tmp/x.gif") as create:
            assert md.loop_from_receipt(receipt, user_id=1, post_id=2) == "/tmp/x.gif"
        kwargs = create.call_args.kwargs
        assert kwargs["prefer"] == md.STYLE_CHART_DRAW and kwargs["graphic"] is GRAPHIC
        assert create.call_args.args[0] == receipt["hook_text"] and kwargs["ratio"] == "4:5"
        with patch(f"{_MOD}.create_motion_gif", return_value=None) as create:
            md.loop_from_receipt(None, user_id=1, post_id=2)
        assert create.call_args.args[0] == "" and create.call_args.kwargs["graphic"] is None


class TestWiring:
    def test_the_title_card_ships_with_motion_and_records_it(self):
        concept = SimpleNamespace(hook_phrase="We cut response time in one quarter",
                                  kicker="OPS", graphic=GRAPHIC)
        with patch(f"{_TC}.write_title_card_video", return_value=True) as write:
            tc.create_title_card_video(POST, user_id=21, post_id=3, concept=concept)
        motion = write.call_args.kwargs["motion"]
        assert motion.style in md.DATA_STYLES and motion.mode == md.MODE_MP4
        assert motion.seconds == tc.title_card_seconds()
        assert md.recent_motions(21) == [motion.style]

    def test_consecutive_cards_rotate_their_motion(self):
        concept = SimpleNamespace(hook_phrase="We cut response time in one quarter",
                                  kicker="OPS", graphic=GRAPHIC)
        styles = []
        with patch(f"{_TC}.write_title_card_video", return_value=True) as write:
            for _ in range(4):
                tc.create_title_card_video(POST, user_id=22, post_id=3, concept=concept)
                styles.append(write.call_args.kwargs["motion"].style)
        assert all(a != b for a, b in zip(styles, styles[1:]))

    def test_a_motion_fault_ships_the_drifting_card(self):
        with patch(f"{_MOD}.build_motion", side_effect=RuntimeError("x")), \
                patch(f"{_TC}.write_title_card_video", return_value=True) as write, \
                patch(f"{_TC}.log_warning") as warn:
            assert tc.create_title_card_video(POST, user_id=23, post_id=3)
        assert write.call_args.kwargs["motion"] is None
        assert warn.call_args[0][0] == "Title card motion raised — the card ships without it"
        assert md.recent_motions(23) == []

    def test_a_code_drawn_still_gets_a_code_drawn_loop(self, monkeypatch, tmp_path):
        from cqc_lem.utilities import animated_loop as al

        monkeypatch.setenv("ANIMATED_POST_ENABLED", "true")
        assets = tmp_path / "assets"
        (assets / "images" / "posts" / "42").mkdir(parents=True)
        (assets / "images" / "posts" / "42" / "img_abc.png").write_bytes(b"png")
        url = "https://api.example.com/api/assets?file_name=images/posts/42/img_abc.png"
        gif = tmp_path / "made.gif"
        Image.new("RGB", (8, 8)).save(gif)
        receipt = {"archetype_rendered": "stat_card", "hook_text": "h"}
        with patch("cqc_lem.utilities.post_image.assets_dir", str(assets)), \
                patch("cqc_lem.utilities.media_provenance.read_brief_receipt",
                      return_value=receipt), \
                patch(f"{_MOD}.loop_from_receipt", return_value=str(gif)) as loop, \
                patch("cqc_lem.utilities.ai.video_models.create_runway_video") as runway:
            stored = al.produce_post_loop(7, 42, "text", url)
        assert stored == str(assets / "images" / "posts" / "42" / "img_abc.loop.gif")
        runway.assert_not_called()  # an image model never re-draws a verified figure
        assert loop.call_args.args[0] is receipt and not gif.exists()  # temp GIF cleaned up


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs a real ffmpeg")
def test_a_real_encode_is_a_seamless_gif_and_an_mp4_inside_the_limits(tmp_path):
    from cqc_lem.utilities.animated_loop import GIF_MAX_BYTES, GIF_MAX_FRAMES, gif_within_limits

    gif = tmp_path / "motion.gif"
    mp4 = tmp_path / "title_card_1.mp4"
    # The unit lane's hermetic guard hides ffmpeg; this one test encodes for real.
    with patch.object(tc, "_ffmpeg", return_value=shutil.which("ffmpeg")):
        assert md.write_motion_gif(_plan(md.STYLE_STAT_COUNTER), str(gif)) is True
        assert tc.write_title_card_video(None, str(mp4), fps=12,
                                         motion=_plan(md.STYLE_CHART_DRAW, mode=md.MODE_MP4,
                                                      size=(540, 540)))
    assert gif_within_limits(str(gif)) and os.path.getsize(gif) <= GIF_MAX_BYTES
    assert os.path.getsize(mp4) > 0
    with Image.open(gif) as im:
        assert im.n_frames <= GIF_MAX_FRAMES
        first = im.convert("RGB")
        im.seek(im.n_frames - 1)
        assert _same(first, im.convert("RGB"))
