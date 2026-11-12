"""Render smoke test for the three Pillow-drawn media paths (issue #2506).

A Pillow major bump cannot be judged by tests that stub the drawing code. This module runs the
real renderers, unmocked, against the installed Pillow — one carousel deck, one MP4 motion frame
and one video title card — writes each to a temp directory, and checks only that the output
opens, has the expected size and mode, and is not blank. Visual quality is out of scope (the
runtime quality gates own that); a raise anywhere in the drawing code fails the build.

The only stub is ``select_slide_image``: the deck must never reach the network for a photo.
"""

import os
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageStat  # noqa: E402

from cqc_lem.utilities import (  # noqa: E402
    carousel_creator as cc,
    motion_design as md,
    video_title_card as tc,
)
from cqc_lem.utilities.brand_kit import deck_theme  # noqa: E402

HOOK = "Your AI budget is buying tools nobody opens"


@pytest.fixture(autouse=True)
def _assets(tmp_path, monkeypatch):
    monkeypatch.setattr("cqc_lem.assets_dir", str(tmp_path / "assets"), raising=False)


def _assert_rendered(path: str, size: tuple, mode: str) -> None:
    """The file opens, is ``size`` in ``mode``, and carries more than one colour."""
    assert os.path.isfile(path), path
    with Image.open(path) as img:
        img.load()
        assert img.size == size
        assert img.mode == mode
        # A blank render is one flat colour: every band's extrema collapse to a single value.
        assert any(lo != hi for lo, hi in img.getextrema()), f"{path} is blank"
        assert max(ImageStat.Stat(img).stddev) > 1.0, f"{path} is near-uniform"


@pytest.mark.unit
class TestMediaRenderSmoke:
    def test_carousel_deck_renders_every_slide(self, tmp_path):
        carousel = cc.EducationalContentCarousel(
            cover=cc.EducationalContentSlide(title="Ship faster without breaking prod",
                                             content="Three release gates that hold."),
            contents=[cc.EducationalContentSlide(title="Gate one: a coverage floor",
                                                 content="Every diff keeps 80 percent covered.")],
            call_to_action=cc.EducationalContentSlide(title="Save this for your next retro",
                                                      content="Which gate would you add first?"),
        )
        out_dir = str(tmp_path / "deck")
        with patch.object(cc, "select_slide_image", return_value=None) as photo:
            paths = cc.create_carousel_slide_images(
                carousel, post_id=2506, output_dir=out_dir, theme=deck_theme(None),
                cover_treatment=cc.DECK_COVER_POSTER)

        assert len(paths) == 3
        for path in paths:
            assert os.path.dirname(path) == out_dir
            _assert_rendered(path, (1080, 1080), "RGB")
        assert photo.called

    def test_video_motion_frame_renders(self, tmp_path):
        layout = tc.plan_title_card(HOOK, size=tc.TITLE_CARD_SIZES["1:1"],
                                    palette=tc.title_card_palette(None, tc.GROUND_CHARCOAL),
                                    kicker="AI SPEND", byline="Jane Doe", seconds=7.0)
        plan = md.plan_kinetic(layout, md.STYLE_KINETIC_MASK, md.MODE_MP4)
        out = str(tmp_path / "frame.png")
        frame = md.render_motion_frame(plan, 1.5)
        frame.save(out, "PNG")

        _assert_rendered(out, (1080, 1080), frame.mode)
        assert frame.mode in ("RGB", "RGBA")

    def test_video_title_card_renders(self, tmp_path):
        layout = tc.plan_title_card(HOOK, size=tc.TITLE_CARD_SIZES["9:16"],
                                    palette=tc.title_card_palette(None, tc.GROUND_OFF_WHITE),
                                    kicker="AI SPEND", byline="Jane Doe", seconds=7.0)
        out = str(tmp_path / "title_card.png")
        tc.render_title_card_frame(layout, 0.0).save(out, "PNG")

        _assert_rendered(out, (1080, 1920), "RGB")
