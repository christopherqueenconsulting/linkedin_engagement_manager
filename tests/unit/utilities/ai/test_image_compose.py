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
        plan = plan_layout(size, layout)
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
        assert Image.open(out).size == _COVER

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
