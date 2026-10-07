"""Deck accents never sit over text (#2241 showcase B).

Decks 128, 133 and 144 shipped "Prevent AI Model Failures: 4-Step Guide" with the stat cover's
giant decorative "?" over "Model". Accents are now queued and placed at save time only where their
box meets no text box. These tests render EVERY template at several title lengths and assert the
geometry from the real draw calls: no accent box intersects any text box, and accents still draw.
"""

import pytest

pytest.importorskip("PIL")

from unittest.mock import patch  # noqa: E402

from cqc_lem.utilities import carousel_creator as cc  # noqa: E402
from cqc_lem.utilities.brand_kit import deck_theme  # noqa: E402

pytestmark = pytest.mark.unit

TITLES = {
    "short": "Route by cost",
    "showcase": "Prevent AI Model Failures: 4-Step Guide",
    "long": ("Prevent AI model failures before they cost you a quarter of pipeline and a "
             "client relationship you cannot win back"),
}


def _deck(title: str) -> "cc.ProductDemoCarousel":
    slide = cc.ProductDemoSlide
    return cc.ProductDemoCarousel(
        cover=slide(title=title, content="The exact checks we run before every model change."),
        main_feature=slide(title=title, content="Every team buys its own tool and nobody owns "
                                                "the list, so renewals land on autopilot."),
        additional_features=[slide(title="Score every draft", content="Run a scoring layer.")],
        call_to_action=slide(title=title, content="Save this for your next model review."),
    )


def _render(template: str, title: str, tmp_path) -> list:
    draws: list = []
    real = cc.make_ink_draw

    def _capture(img):
        draw = real(img)
        draws.append(draw)
        return draw

    with patch.object(cc, "make_ink_draw", _capture), \
            patch.object(cc, "retain_carousel_keyframes"):
        cc.create_carousel_slide_images(_deck(title), post_id=128,
                                        output_dir=str(tmp_path / template),
                                        template=template, theme=deck_theme(None))
    return draws


@pytest.mark.parametrize("length", sorted(TITLES))
@pytest.mark.parametrize("template", sorted(cc.CAROUSEL_TEMPLATES))
def test_no_accent_box_intersects_any_text_box(template, length, tmp_path):
    draws = _render(template, TITLES[length], tmp_path)
    assert len(draws) == 4
    for draw in draws:
        assert draw.text_boxes  # the slide really set text
        for accent in draw.decor_boxes:
            for text in draw.text_boxes:
                assert not cc.boxes_intersect(accent, text), (template, length, accent, text)


@pytest.mark.parametrize("template", sorted(cc.CAROUSEL_TEMPLATES))
def test_no_cover_ever_paints_the_question_mark_watermark(template, tmp_path):
    """Showcase round 4: the "?" undercut every case study, so it is gone, not moved."""
    painted: list = []
    real = cc.make_ink_draw

    def _capture(img):
        draw = real(img)
        original = draw.paint_glyph
        draw.paint_glyph = lambda xy, glyph, font, fill: (painted.append(glyph),
                                                          original(xy, glyph, font, fill))
        return draw

    for treatment in (*cc.DECK_COVER_TREATMENTS, cc.DECK_COVER_TEMPLATE):
        with patch.object(cc, "make_ink_draw", _capture), \
                patch.object(cc, "retain_carousel_keyframes"):
            cc.create_carousel_slide_images(_deck(TITLES["showcase"]), post_id=128,
                                            output_dir=str(tmp_path / template / treatment),
                                            template=template, theme=deck_theme(None),
                                            cover_treatment=treatment)
    assert "?" not in painted


def test_accents_still_draw_across_the_deck(tmp_path):
    drawn = sum(len(d.decor_boxes) for t in cc.CAROUSEL_TEMPLATES
                for d in _render(t, TITLES["short"], tmp_path))
    assert drawn >= 10


def test_an_accent_with_no_clear_spot_is_left_out(tmp_path):
    from PIL import Image

    draw = cc.make_ink_draw(Image.new("RGB", (200, 200)))
    draw.text((0, 0), "TEXT EVERYWHERE " * 3)
    painted = []
    draw.decorate([((0, 0, 50, 50), lambda: painted.append(1))])
    assert draw.finish_decor() == 1 and not painted and draw.decor_boxes == []


def test_box_intersection_excludes_touching_edges():
    assert cc.boxes_intersect((0, 0, 10, 10), (5, 5, 15, 15))
    assert not cc.boxes_intersect((0, 0, 10, 10), (10, 0, 20, 10))
