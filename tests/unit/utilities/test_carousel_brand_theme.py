"""Deck engine rules from the #2241 showcase run: limits, brand theme, slide elements, fonts.

Each class pins one engine rule the showcase broke, with the exact shape that broke it.
"""

import json
import os
from unittest.mock import patch

import pytest
from pydantic import BaseModel

pytest.importorskip("PIL")

from PIL import Image, ImageDraw  # noqa: E402

from cqc_lem.utilities import carousel_creator as cc  # noqa: E402
from cqc_lem.utilities.ai import image_graphics as ig  # noqa: E402
from cqc_lem.utilities.brand_kit import (  # noqa: E402
    MIN_TEXT_CONTRAST,
    brand_kit_for_user,
    contrast_ratio,
    deck_theme,
    parse_brand_kit,
    readable_on,
)

pytestmark = pytest.mark.unit

REFERENCE_KIT = {"primary_hex": "#e9d437", "secondary_hex": "#a89816",
                 "neutral_dark_hex": "#1f1f1f", "neutral_light_hex": "#f7f5ef"}
# A kit that tries to break legibility: a pale accent and low-contrast neutrals.
HOSTILE_KIT = {"primary_hex": "#fafafa", "accent_hex": "#eeeeee",
               "neutral_dark_hex": "#777777", "neutral_light_hex": "#888888"}
ALL_MODELS = (cc.EducationalContentCarousel, cc.CaseStudyCarousel, cc.PersonalStoryCarousel,
              cc.IndustryInsightsCarousel, cc.EventRecapCarousel, cc.TestimonialCarousel,
              cc.ProductDemoCarousel)


def _slide(n: int = 0, **extra) -> dict:
    return {"title": f"Slide {n}", "content": "Short body.", **extra}


def _overfull(model_cls: type[BaseModel]) -> dict:
    """Every field of `model_cls` filled, with every list field 6 items long."""
    deck = {}
    for name, field in model_cls.model_fields.items():
        nested = cc._nested_model(field)
        item = _slide(client_name="Ana") if nested is cc.TestimonialSlide else _slide()
        if "list" in str(field.annotation).lower():
            deck[name] = [dict(item, title=f"{name} {n}") for n in range(6)]
        else:
            deck[name] = item
    return deck


class TestFitCarouselToModel:
    def test_the_exact_showcase_shape_is_trimmed_and_builds(self):
        deck = {"cover": _slide(), "main_feature": _slide(),
                "additional_features": [_slide(n) for n in range(4)],
                "call_to_action": _slide()}
        fitted, notes = cc.fit_carousel_to_model(cc.ProductDemoCarousel, deck)
        assert notes == ["additional_features: 4 -> 2 items"]
        built = cc.ProductDemoCarousel(**fitted)
        assert [s.title for s in built.additional_features] == ["Slide 0", "Slide 1"]
        assert len(deck["additional_features"]) == 4  # the input is never mutated

    @pytest.mark.parametrize("model_cls", ALL_MODELS, ids=lambda m: m.__name__)
    def test_every_carousel_model_survives_an_overfull_deck(self, model_cls):
        # The latent version of the same bug: every model with a capped list would crash too.
        fitted, notes = cc.fit_carousel_to_model(model_cls, _overfull(model_cls))
        model_cls(**fitted)
        has_list = any("list" in str(f.annotation).lower() for f in model_cls.model_fields.values())
        assert bool(notes) == has_list

    def test_an_over_long_slide_body_is_cut_at_a_sentence(self):
        body = "First sentence stays. " * 30
        deck = {"cover": _slide(content=body), "contents": [_slide()],
                "call_to_action": _slide()}
        fitted, notes = cc.fit_carousel_to_model(cc.EducationalContentCarousel, deck)
        assert notes == [f"cover.content: {len(body)} -> 500 chars"]
        assert fitted["cover"]["content"].endswith("stays.")
        cc.EducationalContentCarousel(**fitted)

    def test_a_long_word_run_is_cut_at_a_word(self):
        assert cc._trim_text("alpha beta gamma", 12) == "alpha beta"
        assert cc._trim_text("alpha beta", 10) == "alpha beta"
        assert cc._trim_text("abcdefghij", 4) == "abcd"

    def test_a_non_dict_and_missing_fields_pass_through(self):
        assert cc.fit_carousel_to_model(cc.ProductDemoCarousel, None) == (None, [])
        fitted, notes = cc.fit_carousel_to_model(cc.ProductDemoCarousel, {"cover": None})
        assert fitted == {"cover": None} and notes == []

    def test_build_logs_the_trim_at_info(self):
        deck = {"cover": _slide(), "main_feature": _slide(),
                "additional_features": [_slide(n) for n in range(3)],
                "call_to_action": _slide()}
        with patch("cqc_lem.utilities.logger.log_info") as log_info:
            cc.build_carousel_model(cc.ProductDemoCarousel, deck, user_id=1, post_id=133)
        assert "additional_features: 3 -> 2 items" in log_info.call_args.args[0]


class TestLimitsDirective:
    def test_it_names_each_list_bound_and_outranks_the_archetype(self):
        text = cc.carousel_limits_directive(cc.ProductDemoCarousel)
        assert '"additional_features" holds AT MOST 2 slides' in text
        assert "outrank the archetype" in text and "MERGE" in text

    def test_a_model_without_lists_says_nothing(self):
        assert cc.carousel_limits_directive(cc.CaseStudyCarousel) == ""


class TestDeckTheme:
    def test_the_reference_kit_is_charcoal_off_white_and_gold(self):
        theme = deck_theme(parse_brand_kit(REFERENCE_KIT))
        assert theme.branded and theme.dark == (0x1F, 0x1F, 0x1F)
        assert theme.light == (0xF7, 0xF5, 0xEF) and theme.accent == (0xE9, 0xD4, 0x37)
        assert theme.accent_deep == (0xA8, 0x98, 0x16)

    def test_no_kit_is_a_neutral_default_never_another_brands_gold(self):
        for kit in (None, parse_brand_kit({})):
            theme = deck_theme(kit)
            assert not theme.branded and theme.accent != (0xE9, 0xD4, 0x37)

    def test_a_kit_with_only_a_primary_derives_its_deep_accent(self):
        theme = deck_theme(parse_brand_kit({"primary_hex": "#e9d437"}))
        assert theme.accent == (0xE9, 0xD4, 0x37) and theme.accent_deep != theme.accent

    def test_unreadable_neutrals_fall_back(self):
        theme = deck_theme(parse_brand_kit(HOSTILE_KIT))
        assert contrast_ratio(theme.dark, theme.light) >= MIN_TEXT_CONTRAST

    @pytest.mark.parametrize("kit", [REFERENCE_KIT, HOSTILE_KIT, None],
                             ids=["reference", "hostile", "none"])
    @pytest.mark.parametrize("template", sorted(cc.CAROUSEL_TEMPLATES))
    def test_every_template_meets_text_contrast(self, kit, template):
        tmpl = cc.themed_template(template, deck_theme(parse_brand_kit(kit) if kit else None))
        assert contrast_ratio(tmpl["title_color"], tmpl["content_bg"]) >= MIN_TEXT_CONTRAST
        assert contrast_ratio(tmpl["body_color"], tmpl["content_bg"]) >= MIN_TEXT_CONTRAST
        assert contrast_ratio(tmpl["cover_text"], tmpl["cover_bg"]) >= MIN_TEXT_CONTRAST
        assert contrast_ratio(tmpl["cover_accent"], tmpl["cover_bg"]) >= MIN_TEXT_CONTRAST
        for badge in tmpl["badge_colors"]:
            assert contrast_ratio(badge, tmpl["bottom_bar"]) >= MIN_TEXT_CONTRAST
        assert tmpl["layout"] == cc.CAROUSEL_TEMPLATES[template]["layout"]

    def test_no_template_keeps_a_hard_coded_palette(self):
        for skin in cc.CAROUSEL_TEMPLATES.values():
            assert not {"cover_bg", "badge_colors", "content_bg"} & set(skin)

    def test_readable_on_falls_back_to_the_ink(self):
        white, black = (255, 255, 255), (0, 0, 0)
        assert readable_on(white, black, (black, white)) == white
        pushed = readable_on((250, 250, 250), white, (black, white))
        assert contrast_ratio(pushed, white) >= MIN_TEXT_CONTRAST
        mid = (128, 128, 128)
        assert readable_on(mid, mid, (mid, mid)) == mid  # nothing passes: the best ink

    def test_the_user_kit_is_read_and_a_fault_is_none(self):
        with patch("cqc_lem.utilities.db.get_brand_kit", return_value=REFERENCE_KIT):
            assert brand_kit_for_user(1).primary_hex == "#e9d437"
        with patch("cqc_lem.utilities.db.get_brand_kit", side_effect=RuntimeError("1054")):
            assert brand_kit_for_user(1) is None
        assert brand_kit_for_user(None) is None


class TestSlideFonts:
    def test_headings_and_body_are_the_bundled_montserrat(self):
        assert os.path.exists(cc.SLIDE_HEADING_FONT) and os.path.exists(cc.SLIDE_BODY_FONT)
        heading = cc.load_slide_font(40, bold=True)
        body = cc.load_slide_font(40, bold=False)
        assert heading.getname()[0] == "Montserrat" and "ExtraBold" in heading.getname()[1]
        assert body.getname()[0] == "Montserrat" and "Medium" in body.getname()[1]


class TestSlideGraphic:
    def test_a_figure_with_a_following_phrase_is_a_stat_card(self):
        archetype, graphic = ig.slide_graphic(
            "AI posts", "Yet they got about 45% less engagement than human-written content.")
        assert archetype == "stat_card"
        assert graphic["stat"]["display"] == "45%"
        assert graphic["stat"]["label"] == "less engagement than human-written content"

    def test_a_figure_with_nothing_after_it_is_typographic(self):
        # "cut AI costs by" over a hero would be a sentence with a hole in it.
        assert ig.slide_graphic("Costs", "My client cut AI costs by 45%.") is None

    def test_from_to_is_a_before_after(self):
        archetype, graphic = ig.slide_graphic(
            "Spend", "Costs fell from $12,000 a month to $2,000 a month after the audit.")
        pair = graphic["before_after"]
        assert archetype == "before_after"
        assert (pair["before"]["display"], pair["after"]["display"]) == ("$12,000", "$2,000")
        assert pair["before"]["label"] == pair["after"]["label"] == "a month"

    def test_a_short_point_list_is_a_checklist(self):
        archetype, graphic = ig.slide_graphic(
            "Run the audit", "- List every tool\n- Flag duplicate tools\n- Cancel idle seats")
        assert archetype == "checklist"
        assert [s["text"] for s in graphic["steps"]] == [
            "List every tool", "Flag duplicate tools", "Cancel idle seats"]

    def test_a_long_point_is_never_shortened_into_a_checklist(self):
        body = ("List every AI tool you pay for each month across teams\nFlag duplicates\n"
                "Cancel idle seats")
        assert ig.slide_graphic("Audit", body) is None

    @pytest.mark.parametrize("body", ["We shipped in July 2026 and grew.",
                                      "Step 12 is where it breaks.",
                                      "Only 3 tools mattered here today.", ""])
    def test_years_dates_steps_and_small_counts_are_not_figures(self, body):
        assert ig.slide_graphic("Title", body) is None

    def test_a_tampered_figure_is_refused_before_drawing(self):
        _archetype, graphic = ig.slide_graphic("x", "We saved $30K per quarter at last.")
        graphic["stat"]["display"] = "$90K"
        with pytest.raises(ig.UngroundedFactError):
            ig.render_slide_graphic("stat_card", graphic, size=(1080, 360))

    def test_render_is_exactly_the_band_with_no_source_line(self, tmp_path):
        _archetype, graphic = ig.slide_graphic("x", "We saved $30K per quarter at last.")
        out = str(tmp_path / "el.png")
        drawn = ig.render_slide_graphic("stat_card", graphic, size=(1080, 360), out_path=out)
        with Image.open(out) as im:
            assert im.size == (1080, 360)
        assert "source" not in {p.role for p in drawn.placements}
        assert drawn.facts[0]["display"] == "$30K"
        with pytest.raises(ig.GraphicError):
            ig.render_slide_graphic("people_scene", graphic, size=(1080, 360))


def _deck(body: str) -> "cc.EducationalContentCarousel":
    return cc.EducationalContentCarousel(
        cover=cc.EducationalContentSlide(title="Cover", content="Intro"),
        contents=[cc.EducationalContentSlide(title="Body", content=body)],
        call_to_action=cc.EducationalContentSlide(title="Save this", content="For later."))


def _painted(deck, tmp_path, template="bold_listicle"):
    """Render `deck`; return (paths, painted strings per slide, receipt)."""
    slides: list[list[str]] = []
    original_text, original_new = ImageDraw.ImageDraw.text, Image.new

    def _record(self, xy, text, *a, **k):
        if slides:
            slides[-1].append(text)
        return original_text(self, xy, text, *a, **k)

    def _new(mode, size, *a, **k):
        if size == (1080, 1080):
            slides.append([])
        return original_new(mode, size, *a, **k)

    out = str(tmp_path / "deck")
    with patch.object(ImageDraw.ImageDraw, "text", _record), patch.object(Image, "new", _new), \
            patch("cqc_lem.utilities.brand_kit.brand_kit_for_user", return_value=None), \
            patch(f"{cc.__name__}.retain_carousel_keyframes"):
        paths = cc.create_carousel_slide_images(deck, post_id=9, output_dir=out,
                                                template=template)
    with open(os.path.join(out, "deck_render.json")) as fh:
        receipt = json.load(fh)
    return paths, slides, receipt


class TestSlideRendering:
    def test_a_stat_slide_gets_its_element_and_no_stock(self, tmp_path):
        with patch.object(cc, "get_pexels_image_path") as pexels:
            paths, _slides, receipt = _painted(
                _deck("We saved $30K per quarter, and lead quality improved."), tmp_path)
        pexels.assert_not_called()
        assert len(paths) == 3
        body = [s for s in receipt["slides"] if s["role"] == "body"][0]
        assert body["element"] == "stat_card" and body["band"] is True

    def test_a_checklist_slide_prints_its_points_once(self, tmp_path):
        body = "List every tool\nFlag duplicate tools\nCancel idle seats"
        _paths, slides, receipt = _painted(_deck(body), tmp_path, template="step_framework")
        # The element is drawn before its slide opens, so count across the whole deck.
        painted = [text for slide in slides for text in slide]
        for point in body.split("\n"):
            assert painted.count(point) == 1, point
        assert [s["element"] for s in receipt["slides"]] == [None, "checklist", None]

    @pytest.mark.parametrize("template", sorted(cc.CAROUSEL_TEMPLATES))
    def test_a_plain_slide_is_typographic_on_every_template(self, tmp_path, template):
        with patch.object(cc, "derive_image_query") as query:
            _paths, slides, receipt = _painted(
                _deck("Every team buys its own tool and nobody owns the list."), tmp_path,
                template=template)
        query.assert_not_called()  # Pexels is opt-in: no query, no LLM call
        body = [s for s in receipt["slides"] if s["role"] == "body"][0]
        assert body["element"] is None and body["band"] is False
        assert "Every team buys its own tool" in " ".join(slides[1])

    def test_pexels_is_still_reachable_behind_its_flag(self):
        with patch("cqc_lem.utilities.env_constants.CAROUSEL_PEXELS_ENABLED", True), \
                patch("cqc_lem.utilities.env_constants.CAROUSEL_IMAGE_QUERY_LLM", False), \
                patch.object(cc, "get_pexels_image_path", return_value="/tmp/p.jpg") as pexels:
            path = cc.select_slide_image(title="Stack", content="tool audit", content_type="x",
                                         post_id=1, slide_index=2)
        assert path == "/tmp/p.jpg" and pexels.call_count == 1

    def test_a_refused_element_leaves_no_temp_file(self, tmp_path):
        made = []

        def _refuse(*a, **k):
            made.append(k["out_path"])
            raise ig.GraphicLayoutError("does not fit")

        with patch.object(ig, "render_slide_graphic", _refuse):
            assert cc._slide_element("x", "We saved $30K per quarter here.",
                                     deck_theme(None), 1, 2) is None
        assert made and not os.path.exists(made[0])
