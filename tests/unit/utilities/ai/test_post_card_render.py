"""The post rotation's renderers (#2241, anti-monotony round).

Typeset panel variants and their contrast, the verbatim quote card, the photo-grade clause, and
their wiring through ``image_gen``.
"""

from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import image_compose as ic, image_gen, image_graphics as ig
from cqc_lem.utilities.ai.image_brief import _DARK_SCENE, PHOTO_GRADES, grade_clause, with_grade
from cqc_lem.utilities.ai.image_concept import ImageConcept

pytestmark = pytest.mark.unit

_QUOTE = "I think the real problem is that nobody owns the calendar."


def _render(tmp_path, size=(512, 512), color=(90, 120, 150)):
    from PIL import Image

    path = tmp_path / "scene.png"
    Image.new("RGB", size, color).save(path)
    return str(path)


def _pixel(path, xy):
    from PIL import Image

    with Image.open(path) as img:
        return img.convert("RGB").getpixel(xy)


# --- Panel variants -----------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ic.PANEL_VARIANTS)
def test_every_panel_variant_keeps_the_headline_at_4_5_to_1(variant):
    colors = ic.panel_colors(ic.BrandStyle(), variant)
    for line in (colors.headline, colors.hero, colors.rest):
        assert ic.contrast_ratio(line, colors.panel) >= ic.MIN_HEADLINE_CONTRAST, (variant, line)
    assert ic.panel_headline_contrast(ic.BrandStyle(), variant) >= 4.5


def test_contrast_ratio_matches_wcag_endpoints():
    assert ic.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert ic.contrast_ratio("#777777", "#777777") == pytest.approx(1.0)


def test_the_charcoal_variant_is_the_card_as_it_always_was():
    brand = ic.BrandStyle()
    colors = ic.panel_colors(brand, None)
    assert colors.variant == "charcoal"
    assert (colors.panel, colors.headline, colors.kicker, colors.seam, colors.byline) == (
        brand.neutral_dark, brand.primary, brand.accent, brand.accent, brand.neutral_light)
    assert ic.panel_colors(brand, "nonsense") == colors


def test_light_panels_set_their_type_in_charcoal():
    brand = ic.BrandStyle()
    for variant in ("off_white", "gold"):
        colors = ic.panel_colors(brand, variant)
        assert colors.headline == colors.hero == brand.neutral_dark
    assert ic.panel_colors(brand, "gold").panel == brand.primary
    assert ic.panel_colors(brand, "off_white").panel == brand.neutral_light


def test_a_variant_below_contrast_is_unavailable_for_that_brand():
    pale = ic.BrandStyle(primary="#CCCCCC", neutral_dark="#999999", accent="#888888",
                         neutral_light="#EEEEEE")
    assert ic.available_panels(pale) == ("charcoal",), "charcoal is always the floor"
    assert ic.available_panels(ic.BrandStyle()) == ic.PANEL_VARIANTS


@pytest.mark.parametrize("variant", ic.PANEL_VARIANTS)
def test_compose_paints_the_variant_panel(tmp_path, variant):
    brand = ic.BrandStyle()
    out = ic.compose_headline(_render(tmp_path), "Nobody owns the calendar", layout="split_top",
                              brand=brand, surface="post_image", kicker="CONTENT",
                              out_path=str(tmp_path / f"{variant}.png"), panel=variant)
    expected = ic._hex(ic.panel_colors(brand, variant).panel)
    assert _pixel(out, (3, 3)) == expected


def test_an_overlay_layout_ignores_the_panel_variant(tmp_path):
    out = ic.compose_headline(_render(tmp_path), "Nobody owns the calendar", layout="band_top",
                              surface="post_image", out_path=str(tmp_path / "o.png"),
                              panel="gold")
    backing = ic.plan_layout((512, 512), "band_top").backing
    assert _pixel(out, (backing.left + 1, backing.top + 1)) == ic._hex(
        ic.BrandStyle().neutral_dark)


def test_a_data_graphic_carries_the_rotated_panel(tmp_path):
    source = "Late invoices cost the agency 38% of revenue last quarter."
    graphic = ig.validate_graphic_facts(
        {"thesis_stat": {"label": "of revenue", "value": "38", "unit": "%",
                         "source_sentence": source}}, source)
    drawn = ig.render_graphic(ig.STAT_CARD, graphic, surface="post_image", hook="Late invoices",
                              layout="split_top", out_path=str(tmp_path / "s.png"),
                              panel="off_white")
    assert _pixel(drawn.path, (3, 3)) == ic._hex(ic.BrandStyle().neutral_light)


# --- The quote card -----------------------------------------------------------------------------

def _quote_graphic(text=_QUOTE, sentence=_QUOTE, byline="Jane Doe"):
    return {"quote": {"text": text, "source_sentence": sentence}, "byline": byline}


def test_the_quote_card_draws_the_sentence_verbatim_inside_its_bounds(tmp_path):
    drawn = ig.render_quote_card(_quote_graphic(), surface="post_image",
                                 out_path=str(tmp_path / "q.png"))
    assert drawn.canvas == (1080, 1350) and drawn.archetype == ig.QUOTE_CARD
    quote_lines = [p.text for p in drawn.placements if p.role == "quote"]
    assert " ".join(quote_lines) == _QUOTE, "every word, in order, nothing added"
    for placement in drawn.placements:
        left, top, right, bottom = placement.box
        b_left, b_top, b_right, b_bottom = placement.bounds
        assert b_left <= left and right <= b_right and b_top <= top and bottom <= b_bottom, (
            placement)
    assert any(p.role == "byline" and p.text.endswith("Jane Doe") for p in drawn.placements)
    assert drawn.facts[0]["text"] == _QUOTE
    assert min(p.size for p in drawn.placements if p.role == "quote") >= round(1080 * 0.045)


def test_render_graphic_routes_a_quote_card_without_a_headline(tmp_path):
    drawn = ig.render_graphic(ig.QUOTE_CARD, _quote_graphic(), surface="post_image", hook="",
                              out_path=str(tmp_path / "q.png"))
    assert drawn.archetype == ig.QUOTE_CARD


def test_a_paraphrased_quote_is_refused_before_ink():
    with pytest.raises(ig.UngroundedFactError):
        ig.render_quote_card(_quote_graphic(text="Nobody owns your calendar."),
                             surface="post_image")


@pytest.mark.parametrize("graphic,error", [
    (_quote_graphic(byline=""), ig.GraphicError),
    (_quote_graphic(byline="A" * 200), ig.GraphicLayoutError),
    (_quote_graphic(text="word " * 120, sentence="word " * 120), ig.GraphicLayoutError),
    ({"byline": "Jane"}, ig.GraphicError),
])
def test_a_quote_card_that_cannot_be_set_legibly_is_refused(graphic, error):
    with pytest.raises(error):
        ig.render_quote_card(graphic, surface="post_image")


# --- The photo grade ----------------------------------------------------------------------------

@pytest.mark.parametrize("grade", list(PHOTO_GRADES))
def test_every_grade_stays_bright_and_never_names_a_dark_scene(grade):
    clause = grade_clause(grade)
    assert clause.startswith("Photo grade:")
    assert not _DARK_SCENE.search(clause), "naming dark summons it"
    assert any(word in clause for word in ("bright", "fully and evenly lit"))


def test_warm_dusk_is_warm_and_readable():
    clause = grade_clause("warm_dusk")
    assert "golden" in clause and "readable" in clause and "evenly lit" in clause


def test_with_grade_appends_once_and_ignores_unknown_grades():
    graded = with_grade("A founder at a window", "daylight")
    assert graded.startswith("A founder at a window. Photo grade:")
    assert with_grade(graded, "daylight") == graded
    assert with_grade("Prompt.", "") == "Prompt." and with_grade("Prompt.", "neon") == "Prompt."
    assert grade_clause(None) == ""


# --- Wiring through image_gen -------------------------------------------------------------------

def _concept(**overrides):
    base = dict(thesis="Nobody owns the calendar, so content stalls.", audience="founders",
                specific_entities=(), emotional_beat="resolve", hook_phrase="Nobody owns it",
                treatment="editorial_concept", treatment_rationale="")
    base.update(overrides)
    return ImageConcept(**base)


def test_a_quote_card_is_code_drawn_without_a_hook_and_judged_without_one(tmp_path):
    concept = _concept(archetype=ig.QUOTE_CARD, archetype_ranking=(ig.QUOTE_CARD,),
                       graphic=_quote_graphic())
    accepted = image_gen.QualityVerdict(acceptable=True)
    info: dict = {}
    with patch.object(image_gen, "assets_dir", str(tmp_path)), \
         patch.object(image_gen, "inspect_render_quality", return_value=accepted) as judge, \
         patch("cqc_lem.utilities.observability.track_image_gate_verdict"):
        path = image_gen.render_code_drawn(concept, surface="post_image", hook_text=None,
                                           render_info=info, signature="Jane Doe")
    assert path and info["archetype_rendered"] == ig.QUOTE_CARD
    assert info["graphic_facts"][0]["text"] == _QUOTE
    assert judge.call_args.kwargs["hook_text"] is None
    assert image_gen._code_drawn(concept) and image_gen._shows_no_face(concept)


def test_a_data_graphic_still_needs_a_hook():
    concept = _concept(archetype="stat_card", archetype_ranking=("stat_card", "editorial_concept"))
    assert image_gen.render_code_drawn(concept, surface="post_image", hook_text=None) is None


def test_the_panel_reaches_the_compositor_from_the_gated_renderer(tmp_path):
    raw = _render(tmp_path)
    with patch.object(image_gen, "_render_with_backend", return_value=(raw, "gpt-image")), \
         patch.object(image_gen, "inspect_render_quality",
                      return_value=image_gen.QualityVerdict(acceptable=True)), \
         patch("cqc_lem.utilities.ai.image_compose.compose_headline",
               return_value=raw) as compose, \
         patch("cqc_lem.utilities.observability.track_image_gate_verdict"):
        image_gen.render_image_gated("p", surface="post_image", hook_text="Nobody owns it",
                                     concept=_concept(), panel="gold")
    assert compose.call_args.kwargs["panel"] == "gold"


def test_the_panel_reaches_the_avatar_renderer_composite(tmp_path):
    raw = _render(tmp_path)
    with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
               return_value=(raw, True)), \
         patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
         patch.object(image_gen, "inspect_render_quality",
                      return_value=image_gen.QualityVerdict(acceptable=True)), \
         patch("cqc_lem.utilities.ai.image_compose.compose_headline",
               return_value=raw) as compose, \
         patch("cqc_lem.utilities.observability.track_image_gate_verdict"):
        image_gen.render_avatar_image_gated("p", avatar={"model_ref": "o/l:v"}, user_id=1,
                                            surface="post_image", hook_text="Nobody owns it",
                                            concept=_concept(), panel="off_white")
    assert compose.call_args.kwargs["panel"] == "off_white"
