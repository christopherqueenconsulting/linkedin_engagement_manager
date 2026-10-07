"""The ONE place a headline meets a render (issue #2241, round 6).

Legibility, margins, exact brand colors and never clipping are guaranteed by construction here, so
they are asserted here — not left to a vision judge to guess at.
"""
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from cqc_lem.utilities.ai import image_compose as ic
from cqc_lem.utilities.ai.image_compose import (
    BAND_HEIGHT,
    LAYOUTS,
    PANEL_WIDTH,
    SAFE_MARGIN,
    BrandStyle,
    brand_style,
    compose_headline,
    fit_text,
    plan_layout,
)

pytestmark = pytest.mark.unit

_COVER = (1536, 1024)
_POST = (1024, 1280)
_LONG_HOOK = "Unbelievably comprehensive organizational transformation programmes"
_HOOKS = ("Hidden buyers cost you deals", "45% less engagement on AI posts", "Who buys?",
          _LONG_HOOK)


def _render(tmp_path, size, color=(235, 230, 220), name="raw.png"):
    path = tmp_path / name
    Image.new("RGB", size, color).save(path)
    return str(path)


def _text_bbox(plan, hook, size):
    """The union bbox of every drawn line, measured with the font fit_text chose."""
    font, lines, line_height = fit_text(hook, plan.text_box,
                                        round(size[1] * ic.MIN_LINE_FRACTION["newsletter"]))
    draw = ImageDraw.Draw(Image.new("RGB", size))
    boxes = [draw.textbbox(origin, line, font=font)
             for origin, line in zip(ic.line_origins(plan.text_box, lines, font, line_height),
                                     lines)]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)), lines, line_height


class TestLayoutBoxes:
    @pytest.mark.parametrize("size", [_COVER, _POST])
    @pytest.mark.parametrize("layout", LAYOUTS)
    def test_every_text_box_keeps_the_safe_margin(self, size, layout):
        w, h = size
        box = plan_layout(size, layout, Image.new("RGB", size)).text_box
        assert box.left >= w * SAFE_MARGIN - 1 and w - box.right >= w * SAFE_MARGIN - 1
        assert box.top >= h * SAFE_MARGIN - 1 and h - box.bottom >= h * SAFE_MARGIN - 1

    def test_panels_cover_38_percent_of_the_width(self):
        left = plan_layout(_COVER, "panel_left")
        right = plan_layout(_COVER, "panel_right")
        assert left.backing.left == 0 and left.backing.width == round(_COVER[0] * PANEL_WIDTH)
        assert right.backing.right == _COVER[0]
        assert right.backing.width == round(_COVER[0] * PANEL_WIDTH)
        assert left.text_box.right <= left.backing.right
        assert right.text_box.left >= right.backing.left

    def test_band_top_sits_below_a_six_percent_margin(self):
        plan = plan_layout(_POST, "band_top")
        assert plan.backing.top == round(_POST[1] * SAFE_MARGIN)
        assert plan.backing.height == round(_POST[1] * BAND_HEIGHT)

    def test_a_centred_cover_headline_stays_in_the_central_60_percent(self):
        box = plan_layout(_COVER, "lower_third_band").text_box
        assert box.left >= _COVER[0] * 0.2 - 1 and box.right <= _COVER[0] * 0.8 + 1

    @pytest.mark.parametrize("dark_index", [0, 1, 2])
    def test_full_bleed_sets_the_headline_over_the_darkest_third(self, dark_index):
        image = Image.new("RGB", _COVER, (240, 240, 240))
        third = _COVER[0] // 3
        image.paste((20, 20, 20), (dark_index * third, 0, (dark_index + 1) * third, _COVER[1]))
        plan = plan_layout(_COVER, "full_bleed", image)
        assert plan.backing is None
        assert plan.scrim.left == dark_index * third
        assert plan.text_box.left >= plan.scrim.left and plan.text_box.right <= plan.scrim.right

    def test_an_unknown_layout_takes_panel_left(self):
        assert plan_layout(_COVER, "made_up").layout == "panel_left"


class TestFit:
    @pytest.mark.parametrize("size", [_COVER, _POST])
    @pytest.mark.parametrize("layout", LAYOUTS)
    @pytest.mark.parametrize("hook", _HOOKS)
    def test_text_never_leaves_its_box(self, size, layout, hook):
        plan = plan_layout(size, layout, Image.new("RGB", size))
        (left, top, right, bottom), lines, _ = _text_bbox(plan, hook, size)
        assert len(lines) <= ic.MAX_LINES
        box = plan.text_box
        assert left >= box.left - 1 and right <= box.right + 1
        assert top >= box.top - 1 and bottom <= box.bottom + 1

    @pytest.mark.parametrize("layout", ["panel_left", "panel_right", "lower_third_band"])
    def test_a_normal_cover_headline_is_legible_at_400px(self, layout):
        plan = plan_layout(_COVER, layout)
        _, _, line_height = _text_bbox(plan, "Hidden buyers cost you deals", _COVER)
        assert line_height >= _COVER[1] * ic.MIN_LINE_FRACTION["newsletter"]

    def test_text_that_cannot_reach_the_floor_still_fits_and_says_so(self):
        box = ic.Box(0, 0, 200, 60)
        with patch.object(ic, "log_warning") as warn:
            font, lines, line_height = fit_text(_LONG_HOOK, box, min_line_px=200)
        assert line_height * len(lines) <= box.height and warn.called


class TestCompose:
    @pytest.mark.parametrize("layout", ["panel_left", "panel_right", "band_top",
                                        "lower_third_band"])
    def test_panel_and_headline_use_the_brand_colors_exactly(self, tmp_path, layout):
        brand = BrandStyle(primary="#E9D437", neutral_dark="#1F1F1F", accent="#A89816")
        size = _POST if layout == "band_top" else _COVER
        out = compose_headline(_render(tmp_path, size), "Hidden buyers cost you deals",
                               layout=layout, brand=brand)
        image = Image.open(out).convert("RGB")
        plan = ic.fit_headline(size, layout, "Hidden buyers cost you deals").plan
        corner = (plan.backing.left + 2, plan.backing.top + 2)
        assert image.getpixel(corner) == (0x1F, 0x1F, 0x1F)
        colors = {c for _, c in image.crop((plan.text_box.left, plan.text_box.top,
                                            plan.text_box.right, plan.text_box.bottom))
                  .getcolors(maxcolors=1 << 20)}
        assert (0xE9, 0xD4, 0x37) in colors and (0xA8, 0x98, 0x16) in colors

    @pytest.mark.parametrize("size,layout", [(_COVER, "panel_left"), (_POST, "band_top"),
                                             (_COVER, "full_bleed")])
    def test_every_headline_pixel_sits_inside_the_safe_area(self, tmp_path, size, layout):
        out = compose_headline(_render(tmp_path, size, color=(250, 250, 250)), _LONG_HOOK,
                               layout=layout, brand=BrandStyle())
        image = Image.open(out).convert("RGB")
        gold = ic._hex(BrandStyle().primary)
        from PIL import ImageChops
        diff = ImageChops.difference(image, Image.new("RGB", size, gold)).convert("L")
        left, top, right, bottom = diff.point(lambda v: 255 if v == 0 else 0).getbbox()
        w, h = size
        assert left >= w * SAFE_MARGIN - 1 and right <= w - w * SAFE_MARGIN + 1
        assert top >= h * SAFE_MARGIN - 1 and bottom <= h - h * SAFE_MARGIN + 1

    def test_the_composite_lands_beside_the_render_by_default(self, tmp_path):
        raw = _render(tmp_path, _COVER)
        assert compose_headline(raw, "Who buys?") == raw[:-4] + "_headline.png"

    def test_full_bleed_adds_a_scrim_not_a_panel(self, tmp_path):
        out = compose_headline(_render(tmp_path, _COVER, color=(250, 250, 250)), "Who buys?",
                               layout="full_bleed")
        image = Image.open(out).convert("RGB")
        # The scrim darkens the chosen third's centre, but never to the solid panel color.
        centre = image.getpixel((_COVER[0] // 6, _COVER[1] - 5))
        assert centre != (250, 250, 250) and centre != (0x1F, 0x1F, 0x1F)

    def test_a_missing_bundled_font_falls_back_to_a_system_bold(self, tmp_path):
        with patch.object(ic, "BRAND_FONT", tmp_path / "nope.ttf"):
            font = ic.load_font(40)
        assert font is not None
        out = compose_headline(_render(tmp_path, _COVER), "Who buys?")
        assert Image.open(out).size == ic.CANVAS["newsletter"]

    def test_the_bundled_font_ships_with_its_license(self):
        assert ic.BRAND_FONT.is_file() and (ic.FONT_DIR / "OFL.txt").is_file()
        assert "ExtraBold" in ic.load_font(40).getname()[1]


class TestBrandStyle:
    def test_named_hexes_set_the_exact_colors(self):
        kit = ("Brand palette: light gold (#e9d437) and dark gold (#a89816) as accents against "
               "charcoal (#1f1f1f) and off-white (#f7f5ef)")
        assert brand_style(kit) == BrandStyle(primary="#E9D437", neutral_dark="#1F1F1F",
                                              accent="#A89816")

    def test_no_kit_is_the_reference_brand(self):
        assert brand_style(None) == BrandStyle()



class TestEditorialCoverSystem:
    """Round 7 of #2241: kicker, hero numeral, headline and byline — typeset by code."""

    @pytest.mark.parametrize("hook,hero,rest", [
        ("$30K wasted on AI", "$30K", "Wasted on AI"),
        ("53.7% miss the mark", "53.7%", "Miss the mark"),
        ("45% less engagement on AI posts", "45%", "Less engagement on AI posts"),
        ("AI posts: 45% less engagement", "45%", "AI posts: less engagement"),
        ("Who really buys your AI?", "", "Who really buys your AI?"),
    ])
    def test_the_hero_numeral_splits_from_the_hook(self, hook, hero, rest):
        assert ic.split_hero(hook) == (hero, rest)

    def test_headlines_are_sentence_cased_with_acronyms_kept(self):
        parts = ic.headline_parts("audit stops AI waste", "ai content audit", "  Chris  Q ")
        assert parts.rest == "Audit stops AI waste"
        assert parts.kicker == "AI CONTENT AUDIT" and parts.signature == "Chris Q"

    def test_the_hierarchy_scales_from_the_headline_size(self):
        fit = ic.fit_cover(_COVER, "panel_left",
                           ic.headline_parts("45% less engagement on AI posts", "AI CONTENT"))
        assert ic._hero_size(fit.size) == round(fit.size * 2.0)
        assert ic._kicker_size(fit.size) == round(fit.size * 0.35)
        assert fit.block_height <= fit.text_box.height

    @pytest.mark.parametrize("size,layout", [(_COVER, "panel_left"), (_COVER, "panel_right"),
                                             (_COVER, "lower_third_band"), (_POST, "band_top")])
    def test_every_element_stays_in_the_safe_area(self, tmp_path, size, layout):
        out = compose_headline(_render(tmp_path, size, color=(250, 250, 250)),
                               "45% less engagement on AI posts", layout=layout,
                               kicker="AI CONTENT AUDIT", signature="Christopher Queen")
        image = Image.open(out).convert("RGB")
        w, h = size
        from PIL import ImageChops
        for color in (BrandStyle().primary, BrandStyle().accent, BrandStyle().neutral_light):
            diff = ImageChops.difference(image, Image.new("RGB", size, ic._hex(color)))
            bbox = diff.convert("L").point(lambda v: 255 if v == 0 else 0).getbbox()
            assert bbox, f"{color} never drawn"
            left, top, right, bottom = bbox
            assert left >= w * SAFE_MARGIN - 1 and right <= w - w * SAFE_MARGIN + 1
            assert top >= h * SAFE_MARGIN - 1 and bottom <= h - h * SAFE_MARGIN + 1

    def _colors(self, path):
        return {c for _, c in Image.open(path).convert("RGB").getcolors(maxcolors=1 << 22)}

    def test_a_number_led_hook_sets_the_rest_in_off_white(self, tmp_path):
        colors = self._colors(compose_headline(_render(tmp_path, _COVER), "$30K wasted on AI"))
        assert ic._hex(BrandStyle().primary) in colors
        assert ic._hex(BrandStyle().neutral_light) in colors

    def test_a_hook_without_a_number_is_all_gold(self, tmp_path):
        colors = self._colors(compose_headline(_render(tmp_path, _COVER), "Who buys your AI?"))
        assert ic._hex(BrandStyle().primary) in colors
        assert ic._hex(BrandStyle().neutral_light) not in colors

    def test_the_signature_is_omitted_when_empty(self, tmp_path):
        raw = _render(tmp_path, _COVER)
        bare = compose_headline(raw, "Who buys your AI?", out_path=str(tmp_path / "a.png"))
        signed = compose_headline(raw, "Who buys your AI?", signature="Christopher Queen",
                                  out_path=str(tmp_path / "b.png"))
        empty = compose_headline(raw, "Who buys your AI?", signature="  ",
                                 out_path=str(tmp_path / "c.png"))
        assert Image.open(bare).tobytes() == Image.open(empty).tobytes()
        assert Image.open(bare).tobytes() != Image.open(signed).tobytes()

    def test_the_signature_is_off_white_at_half_opacity(self, tmp_path):
        out = compose_headline(_render(tmp_path, _COVER), "Who buys your AI?",
                               signature="Christopher Queen")
        dark, light = ic._hex(BrandStyle().neutral_dark), ic._hex(BrandStyle().neutral_light)
        half = tuple(round(d + (lt - d) * 128 / 255) for d, lt in zip(dark, light))
        colors = self._colors(out)
        assert any(all(abs(a - b) <= 2 for a, b in zip(c, half)) for c in colors)

    def test_the_kicker_is_drawn_in_the_accent_color(self, tmp_path):
        out = compose_headline(_render(tmp_path, _COVER), "Who buys your AI?", kicker="B2B BUYING")
        assert ic._hex(BrandStyle().accent) in self._colors(out)

    def test_a_short_cover_headline_meets_the_nine_percent_floor(self):
        fit = ic.fit_cover(_COVER, "panel_left", ic.headline_parts("Who buys?"))
        assert fit.cap >= _COVER[1] * ic.MIN_CAP_FRACTION["landscape"] - 1

    def test_a_long_headline_widens_the_panel_before_settling(self):
        fit = ic.fit_cover(_COVER, "panel_left",
                           ic.headline_parts("Hidden buyers cost you real deals now"))
        assert fit.plan.backing.width > round(_COVER[0] * ic.PANEL_WIDTHS[0])

    def test_text_fills_most_of_its_box_width(self):
        from PIL import ImageDraw
        fit = ic.fit_cover(_COVER, "panel_left", ic.headline_parts("Who catches silent failures?"))
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        widest = max(ic._ink_width(draw, line, fit.font) for line in fit.lines)
        assert widest >= fit.text_box.width * 0.7


_SPLITS = ("split_left", "split_right", "split_top", "split_bottom")


def _overlaps(a, b):
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def _inside(inner, outer):
    return (inner.left >= outer.left and inner.top >= outer.top and inner.right <= outer.right
            and inner.bottom <= outer.bottom)


class TestSplitLayouts:
    @pytest.mark.parametrize("layout", _SPLITS)
    @pytest.mark.parametrize("grow", [0, 1, 2])
    def test_the_scene_never_sits_under_the_type(self, layout, grow):
        surface = "newsletter" if layout in ("split_left", "split_right") else "post_image"
        size = ic.CANVAS[surface]
        plan = plan_layout(size, layout, grow=grow)
        assert plan.scene is not None and plan.backing is not None
        assert not _overlaps(plan.scene, plan.backing)
        assert not _overlaps(plan.scene, plan.text_box)
        assert _inside(plan.text_box, plan.backing)
        assert plan.scene.width * plan.scene.height + plan.backing.width * plan.backing.height \
            == size[0] * size[1]

    @pytest.mark.parametrize("surface,layout", [("newsletter", "split_left"),
                                                ("newsletter", "split_right"),
                                                ("post_image", "split_top"),
                                                ("post_image", "split_bottom")])
    def test_a_square_render_lands_on_the_final_canvas(self, tmp_path, surface, layout):
        out = compose_headline(_render(tmp_path, (1024, 1024), color=(10, 200, 30)),
                               "Hidden buyers cost you deals", layout=layout, surface=surface)
        image = Image.open(out).convert("RGB")
        assert image.size == ic.CANVAS[surface]
        plan = plan_layout(image.size, layout)
        cx = (plan.scene.left + plan.scene.right) // 2
        cy = (plan.scene.top + plan.scene.bottom) // 2
        assert image.getpixel((cx, cy)) == (10, 200, 30)

    def test_the_seam_rule_is_the_brand_accent(self, tmp_path):
        out = compose_headline(_render(tmp_path, (1024, 1024)), "Who buys?",
                               layout="split_left", surface="newsletter")
        image = Image.open(out).convert("RGB")
        plan = plan_layout(image.size, "split_left")
        accent = tuple(int(BrandStyle().accent[i:i + 2], 16) for i in (1, 3, 5))
        assert image.getpixel((plan.backing.right - 2, image.size[1] // 2)) == accent

    def test_cover_fit_returns_exactly_the_region(self):
        box = ic.Box(0, 0, 960, 900)
        assert ic.cover_fit(Image.new("RGB", (1024, 1024)), box).size == (960, 900)
        tall = ic.Box(0, 0, 1080, 891)
        assert ic.cover_fit(Image.new("RGB", (1024, 1024)), tall).size == (1080, 891)

    def test_the_defaults_are_splits(self):
        assert ic.DEFAULT_LAYOUT == {"newsletter": "split_left", "post_image": "split_top"}
