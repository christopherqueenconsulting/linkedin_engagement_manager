"""Showcase round 4: monotony fixes — style gate, typographic cards, composed covers, deck rhythm.

An independent critic failed the showcase on sameness: two claymation renders back to back, a
ring-and-bar placeholder card on repeat, a cover shipped as a bare photo, five decks on two
templates with a "?" watermark, and "From the article" on posts with no article. Each class pins
one of those rules.
"""
import dataclasses
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from cqc_lem.utilities import (
    carousel_creator as cc,  # noqa: E402
    newsletter_cover as nc,  # noqa: E402
)
from cqc_lem.utilities.ai import (
    image_graphics as ig,  # noqa: E402
    post_treatment as pt,  # noqa: E402
)
from cqc_lem.utilities.brand_kit import deck_theme  # noqa: E402
from tests.unit.utilities.test_post_rhythm import (  # noqa: E402
    _OPINION,
    _REPORT,
    _concept,
    _Env,
)

pytestmark = pytest.mark.unit


# --- The style dimension ---------------------------------------------------------------------

class TestStyleOf:
    @pytest.mark.parametrize("args,style", [
        (("quote_card",), pt.STYLE_QUOTE_CARD),
        (("data_card",), pt.STYLE_CODE_DRAWN),
        (("typeset_card", "checklist"), pt.STYLE_CODE_DRAWN),
        (("typeset_card", "typeset_card"), pt.STYLE_CODE_DRAWN),
        (("photo_only", "people_scene"), pt.STYLE_PEOPLE_PHOTO),
        (("photo_only", "editorial_concept", "claymation"), "claymation"),
        (("photo_only", "editorial_concept", ""), None),
        (("photo_only", None, None, True), pt.STYLE_CODE_DRAWN),
        ((None,), None),
    ])
    def test_every_image_has_one_style(self, args, style):
        assert pt.style_of(*args) == style


class TestStyleBlocks:
    def test_a_quote_card_never_follows_a_quote_card(self):
        assert pt.TREATMENT_QUOTE_CARD in pt.style_blocks([pt.STYLE_QUOTE_CARD])

    def test_quote_cards_are_capped_at_two_per_six(self):
        recent = [pt.STYLE_PEOPLE_PHOTO, pt.STYLE_QUOTE_CARD, "risograph", pt.STYLE_QUOTE_CARD,
                  "claymation"]
        assert "capped" in pt.style_blocks(recent)[pt.TREATMENT_QUOTE_CARD]
        # The sixth post back has left the window.
        assert pt.TREATMENT_QUOTE_CARD not in pt.style_blocks(
            ["a", "b", "c", "d", pt.STYLE_QUOTE_CARD, pt.STYLE_QUOTE_CARD])

    def test_a_data_card_never_follows_a_code_drawn_card(self):
        assert pt.TREATMENT_DATA_CARD in pt.style_blocks([pt.STYLE_CODE_DRAWN])
        assert pt.style_blocks([]) == {} and pt.style_blocks([None]) == {}

    def test_blocked_archetypes(self):
        assert pt.blocked_archetypes(pt.STYLE_PEOPLE_PHOTO) == {"people_scene"}
        assert "checklist" in pt.blocked_archetypes(pt.STYLE_CODE_DRAWN)
        assert pt.blocked_archetypes("claymation") == frozenset()

    def test_the_plan_skips_a_blocked_treatment_with_its_reason(self):
        concept = _concept(_OPINION, hook_shape="contrast")
        history = {"treatment": ["quote_card"], "style": [pt.STYLE_QUOTE_CARD]}
        rhythm = pt.plan_post_rhythm(concept, _OPINION, history, 0.4, ("charcoal",),
                                     byline="Jane")
        assert pt.TREATMENT_QUOTE_CARD not in rhythm.plan.chain
        assert dict(rhythm.plan.skipped)[pt.TREATMENT_QUOTE_CARD].startswith("style repeats")
        assert rhythm.last_style == pt.STYLE_QUOTE_CARD

    def test_an_old_receipt_reads_its_style_off_what_rendered(self):
        receipts = [{"rhythm": {"treatment": "photo_only"},
                     "concept": {"archetype": "editorial_concept", "art_style": "claymation"}},
                    {"hook_text": "x", "archetype_rendered": "people_scene", "concept": {}},
                    {"rhythm": {"treatment": "typeset_card"}, "gate_verdict": "last_resort",
                     "concept": {}}]
        assert pt.rhythm_history(receipts)["style"] == ["claymation", pt.STYLE_PEOPLE_PHOTO,
                                                        pt.STYLE_CODE_DRAWN]


class TestAvoidLastStyle:
    def _rhythm(self, last, recent=()):
        return SimpleNamespace(last_style=last, recent_styles=tuple(recent))

    def test_a_people_photo_never_follows_a_people_photo(self):
        from cqc_lem.utilities.post_image import _avoid_last_style

        concept = _concept(_REPORT)
        out = _avoid_last_style(concept, self._rhythm(pt.STYLE_PEOPLE_PHOTO, ["claymation"]))
        assert out.archetype_ranking == ("editorial_concept",)
        assert out.treatment == "editorial_concept"
        assert out.art_style and out.art_style != "claymation"

    def test_a_code_drawn_head_is_dropped_after_a_code_drawn_card(self):
        from cqc_lem.utilities.post_image import _avoid_last_style

        concept = _concept(_REPORT, archetype="checklist",
                           archetype_ranking=("checklist", "people_scene"))
        out = _avoid_last_style(concept, self._rhythm(pt.STYLE_CODE_DRAWN))
        assert out.archetype_ranking == ("people_scene",) and out.archetype == "people_scene"

    def test_nothing_to_avoid_is_unchanged(self):
        from cqc_lem.utilities.post_image import _avoid_last_style

        concept = _concept(_REPORT)
        assert _avoid_last_style(concept, self._rhythm("risograph")) is concept
        empty = dataclasses.replace(concept, archetype_ranking=())
        assert _avoid_last_style(empty, self._rhythm(pt.STYLE_PEOPLE_PHOTO)) is empty


def test_two_people_posts_in_a_row_become_two_styles(tmp_path):
    env = _Env(tmp_path)
    styles = []
    for _ in range(3):
        url, _reason, receipt = env.post(_REPORT,
                                         **{"cqc_lem.utilities.ai.post_treatment.plan_treatments":
                                            {"return_value": pt.TreatmentPlan(
                                                chain=("photo_only",), skipped=(),
                                                card_wanted=False, rerolled=False)}})
        assert url
        styles.append(receipt["rhythm"]["style"])
    assert all(styles[i] != styles[i + 1] for i in range(len(styles) - 1)), styles


# --- The typographic last-resort card --------------------------------------------------------

class TestTypesetLayouts:
    def test_availability(self):
        assert ig.typeset_layouts_for("Shipping became a non-event") == (ig.CARD_POSTER,
                                                                         ig.CARD_GRID_RULE)
        assert ig.CARD_QUOTE_MARKS in ig.typeset_layouts_for("Exact words here.", verbatim=True)
        assert ig.CARD_NUMBER_LED in ig.typeset_layouts_for("160 releases without downtime")
        assert ig.CARD_NUMBER_LED not in ig.typeset_layouts_for("We shipped 160 releases")

    @pytest.mark.parametrize("layout,hook", [
        (ig.CARD_POSTER, "Shipping became a non-event"),
        (ig.CARD_GRID_RULE, "Cost per success beats cost per token"),
        (ig.CARD_QUOTE_MARKS, "A cheap model that fails one call in three costs more."),
        (ig.CARD_NUMBER_LED, "160 releases without a minute of downtime"),
    ])
    @pytest.mark.parametrize("panel", ["charcoal", "off_white", "gold"])
    @pytest.mark.parametrize("surface", ["post_image", "newsletter"])
    def test_every_layout_sets_its_type_inside_the_safe_area(self, layout, hook, panel, surface,
                                                             tmp_path):
        drawn = ig.render_typeset_card(hook, surface=surface, kicker="ROUTING",
                                       signature="Jane Doe", panel=panel, card_layout=layout,
                                       out_path=str(tmp_path / "c.png"))
        with Image.open(drawn.path) as im:
            assert im.size == ig.GRAPHIC_CANVAS[surface]
        texts = [p for p in drawn.placements if p.text]
        assert {"headline", "byline"} <= {p.role for p in texts}
        for p in drawn.placements:
            assert p.bounds[0] <= p.box[0] and p.box[2] <= p.bounds[2], (p.role, p.box)
            assert p.bounds[1] <= p.box[1] + 1 and p.box[3] <= p.bounds[3] + 1, (p.role, p.box)
        assert not any(p.role == "accent" for p in drawn.placements)

    def test_number_led_lifts_the_figure_once(self, tmp_path):
        drawn = ig.render_typeset_card("160 releases without downtime", card_layout=ig.CARD_NUMBER_LED,
                                       out_path=str(tmp_path / "n.png"))
        assert [p.text for p in drawn.placements if p.role == "hero"] == ["160"]
        assert "160" not in " ".join(p.text for p in drawn.placements if p.role == "headline")

    def test_number_led_needs_a_leading_figure(self, tmp_path):
        with pytest.raises(ig.GraphicError):
            ig.render_typeset_card("We shipped it", card_layout=ig.CARD_NUMBER_LED,
                                   out_path=str(tmp_path / "x.png"))

    def test_an_unsettable_headline_refuses(self, tmp_path):
        with pytest.raises(ig.GraphicLayoutError):
            ig.render_typeset_card("Pneumonoultramicroscopicsilicovolcanoconiosis" * 3,
                                   out_path=str(tmp_path / "x.png"))

    def test_a_kicker_or_byline_too_wide_is_left_out(self, tmp_path):
        drawn = ig.render_typeset_card("Short hook", kicker="K" * 200, signature="S" * 300,
                                       out_path=str(tmp_path / "x.png"))
        assert {p.role for p in drawn.placements if p.text} == {"headline"}


def test_the_last_resort_card_rotates_its_layout_from_the_receipts(tmp_path):
    env = _Env(tmp_path)
    layouts = []
    for _ in range(3):
        url, _reason, receipt = env.post(
            _REPORT, **{"cqc_lem.utilities.ai.post_treatment.plan_treatments": {
                "return_value": pt.TreatmentPlan(chain=("photo_only",), skipped=(),
                                                 card_wanted=False, rerolled=False)},
               "cqc_lem.utilities.ai.image_brief.build_image_brief": {
                   "side_effect": RuntimeError("author down")}})
        assert url and receipt["gate_verdict"] == "last_resort"
        assert receipt["rhythm"]["style"] == pt.STYLE_CODE_DRAWN
        layouts.append(receipt["rhythm"]["card_layout"])
    # Stage 1's hook is neither verbatim nor number-led: poster and grid rule alternate.
    assert layouts[0] != layouts[1] and layouts[1] != layouts[2], layouts
    assert set(layouts) == {ig.CARD_POSTER, ig.CARD_GRID_RULE}


# --- Source lines come from the caller -------------------------------------------------------

def test_render_code_drawn_passes_the_callers_source_line():
    from cqc_lem.utilities.ai import image_gen

    concept = _concept(_REPORT, archetype="stat_card", archetype_ranking=("stat_card",),
                       graphic={"stat": {}})
    with patch("cqc_lem.utilities.ai.image_graphics.render_graphic",
               side_effect=ig.GraphicError("stop")) as render, \
            patch.object(image_gen, "_code_drawn", return_value=True), \
            patch.object(image_gen, "_drawn_archetypes", return_value=("stat_card",)):
        image_gen.render_code_drawn(concept, surface="post_image", hook_text="Hook",
                                    source_line="Source: Example Co")
    assert render.call_args.kwargs["source_line"] == "Source: Example Co"


# --- Covers are always composed --------------------------------------------------------------

class TestCoverHeadline:
    def test_stage_1s_hook_first_then_the_title(self):
        assert nc.cover_headline("Payroll eats first", None, "T", "S") == "Payroll eats first"
        assert nc.cover_headline(None, None, "Why payroll eats first", "S") == \
            "Why payroll eats first"
        assert nc.cover_headline("", None, "", "The subtitle line") == "The subtitle line"
        assert nc.cover_headline(None, None, "", "") is None


class TestEnsureComposedCover:
    def _raw(self, tmp_path):
        path = str(tmp_path / "raw.png")
        Image.new("RGB", (1024, 1024), (90, 90, 90)).save(path)
        return path

    def test_an_already_composed_render_is_kept(self, tmp_path):
        raw = self._raw(tmp_path)
        assert nc.ensure_composed_cover(raw, "Hook", {"raw_render_path": "/x"}, concept=None,
                                        brand="", byline=None) == raw
        assert nc.ensure_composed_cover(raw, "Hook", {"archetype_rendered": "checklist"},
                                        concept=None, brand="", byline=None) == raw

    def test_a_bare_render_is_composed_with_the_headline(self, tmp_path):
        info: dict = {}
        out = nc.ensure_composed_cover(self._raw(tmp_path), "Payroll eats first", info,
                                       concept=None, brand="", byline="The Letter")
        assert out and info["cover_composed"] == "late_compose"
        with Image.open(out) as im:
            assert im.size == (1920, 1080)

    def test_a_compose_failure_becomes_a_typeset_cover(self, tmp_path):
        info: dict = {}
        with patch("cqc_lem.utilities.ai.image_compose.compose_headline",
                   side_effect=ValueError("no fit")):
            out = nc.ensure_composed_cover(self._raw(tmp_path), "Payroll eats first", info,
                                           concept=None, brand="", byline="The Letter")
        assert out and info["cover_composed"] == "typeset_card"

    def test_never_bare(self, tmp_path):
        raw = self._raw(tmp_path)
        assert nc.ensure_composed_cover(raw, None, {}, concept=None, brand="",
                                        byline=None) is None
        with patch("cqc_lem.utilities.ai.image_compose.compose_headline",
                   side_effect=ValueError("no fit")), \
                patch("cqc_lem.utilities.ai.image_graphics.render_typeset_card",
                      side_effect=ig.GraphicLayoutError("no")):
            assert nc.ensure_composed_cover(raw, "Hook", {}, concept=None, brand="",
                                            byline=None) is None


# --- Deck rhythm -----------------------------------------------------------------------------

def _deck(title="160 releases in 32 days", body="Zero downtime, no Kubernetes."):
    slide = cc.ProductDemoSlide
    return cc.ProductDemoCarousel(
        cover=slide(title=title, content=body),
        main_feature=slide(title="Gate every deploy", content="We shipped 160 releases in 32 "
                                                              "days."),
        additional_features=[slide(title="Batch four a day", content="Releases ship 4x daily.")],
        call_to_action=slide(title="Save this", content="For your next release review."),
    )


class TestDeckRhythm:
    def test_the_chain_offers_only_what_the_deck_can_take(self):
        chain = cc.deck_cover_chain("Route by cost", False, False, [])
        assert chain == [cc.DECK_COVER_POSTER, cc.DECK_COVER_TEMPLATE]
        full = cc.deck_cover_chain("160 releases", True, True, [])
        assert set(full[:-1]) == set(cc.DECK_COVER_TREATMENTS)
        assert full[-1] == cc.DECK_COVER_TEMPLATE

    def test_the_last_decks_cover_and_motif_go_last(self):
        recent = [cc.DECK_COVER_POSTER, cc.DECK_COVER_DRAWN]
        chain = cc.deck_cover_chain("160 releases", True, False, recent)
        assert chain[0] == cc.DECK_COVER_NUMBER and chain[-2] == cc.DECK_COVER_POSTER
        assert cc.deck_motif(["arc", "dots"]) in ("bracket", "stripes")

    def test_receipts_are_read_per_author_newest_first(self, tmp_path):
        for n, (user, cover, motif) in enumerate([(1, "poster", "arc"), (2, "number_led", "dots"),
                                                  (1, "code_drawn", "bracket")]):
            deck = tmp_path / f"deck{n}"
            deck.mkdir()
            path = deck / "deck_render.json"
            path.write_text(json.dumps({"user_id": user, "cover_treatment": cover,
                                        "motif": motif, "slides": []}))
            os.utime(path, (1000 + n, 1000 + n))
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "deck_render.json").write_text("{")
        got = cc.recent_deck_choices(str(tmp_path), 1)
        assert got == {"cover_treatment": ["code_drawn", "poster"], "motif": ["bracket", "arc"],
                       "badge": [None, None], "template": [None, None]}
        assert cc.recent_deck_choices(str(tmp_path), None)["motif"] == []
        assert cc.recent_deck_choices(str(tmp_path / "missing"), 1)["motif"] == []

    def test_consecutive_decks_rotate_cover_and_motif(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECK_AI_COVER_ENABLED", "false")
        seen = []
        for n in range(3):
            out = tmp_path / f"{n}"
            with patch.object(cc, "retain_carousel_keyframes"):
                cc.create_carousel_slide_images(_deck(), post_id=n, output_dir=str(out),
                                                template="stat_reveal", user_id=7,
                                                theme=deck_theme(None))
            receipt = json.loads((out / "deck_render.json").read_text())
            os.utime(out / "deck_render.json", (2000 + n, 2000 + n))
            assert receipt["user_id"] == 7
            seen.append((receipt["cover_treatment"], receipt["motif"]))
        covers, motifs = [c for c, _ in seen], [m for _, m in seen]
        assert len(set(covers)) == 3 and len(set(motifs)) == 3, seen
        assert cc.DECK_COVER_TEMPLATE not in covers

    @pytest.mark.parametrize("treatment", list(cc.DECK_COVER_TREATMENTS))
    def test_every_cover_treatment_draws_the_writers_whole_text(self, treatment, tmp_path):
        art = str(tmp_path / "art.png")
        Image.new("RGB", (1024, 1024), (200, 120, 40)).save(art)
        with patch.object(cc, "retain_carousel_keyframes"):
            paths = cc.create_carousel_slide_images(_deck(), post_id=5,
                                                    output_dir=str(tmp_path / "d"),
                                                    template="bold_listicle", theme=deck_theme(None),
                                                    cover_treatment=treatment,
                                                    cover_image_path=art)
        receipt = json.loads((tmp_path / "d" / "deck_render.json").read_text())
        assert receipt["cover_treatment"] == treatment
        cover = receipt["slides"][0]
        assert cover["chars_dropped"] == 0 and cover["chars_drawn"] > 0
        assert len(paths) == 4

    def test_a_cover_treatment_that_cannot_draw_falls_through(self, tmp_path):
        with patch.object(cc, "retain_carousel_keyframes"):
            cc.create_carousel_slide_images(_deck(title="Route by cost"), post_id=5,
                                            output_dir=str(tmp_path / "d"),
                                            template="bold_listicle", theme=deck_theme(None),
                                            cover_treatment=cc.DECK_COVER_NUMBER)
        receipt = json.loads((tmp_path / "d" / "deck_render.json").read_text())
        assert receipt["cover_treatment"] == cc.DECK_COVER_TEMPLATE

    @pytest.mark.parametrize("motif", list(cc.DECK_MOTIFS))
    def test_every_motif_stays_clear_of_text(self, motif, tmp_path):
        draws = []
        real = cc.make_ink_draw

        def _capture(img):
            draw = real(img)
            draws.append(draw)
            return draw

        with patch.object(cc, "make_ink_draw", _capture), \
                patch.object(cc, "retain_carousel_keyframes"):
            cc.create_carousel_slide_images(_deck(), post_id=5, output_dir=str(tmp_path / motif),
                                            template="bold_listicle", theme=deck_theme(None),
                                            motif=motif)
        for draw in draws:
            for box in draw.decor_boxes:
                assert not any(cc.boxes_intersect(box, text) for text in draw.text_boxes)


class TestDeckCoverConcept:
    class _Scope:
        text = "We shipped 160 releases."

        def __init__(self, concept):
            self._c = concept

        def concept(self):
            return self._c

        def brand(self):
            return ""

    def _brief(self, prompt="A risograph still life of a release calendar."):
        from cqc_lem.utilities.ai.image_brief import ImageBrief

        return ImageBrief(prompt=prompt, ratio="1:1", surface="carousel",
                          style_preset="carousel", focal_concept="calendar")

    def test_off_by_flag(self, monkeypatch):
        monkeypatch.setenv("DECK_AI_COVER_ENABLED", "false")
        assert cc.render_deck_cover_concept(self._Scope(_concept(_REPORT)), 1, 2) is None

    def test_no_concept_no_render(self):
        assert cc.render_deck_cover_concept(self._Scope(None), 1, 2) is None

    def test_an_accepted_object_only_render_is_used(self, tmp_path):
        art = str(tmp_path / "a.png")
        Image.new("RGB", (64, 64)).save(art)

        def _gated(prompt, *, render_info, enforce, concept, **_kw):
            assert enforce is True and concept.treatment == "editorial_concept"
            assert concept.archetype_ranking == ("editorial_concept",) and concept.art_style
            render_info["gate_verdict"] = "accepted"
            return art

        def _author(text, *, concept, **_kw):
            return dataclasses.replace(self._brief(), concept=concept)

        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief", side_effect=_author), \
                patch("cqc_lem.utilities.ai.image_gen.render_image_gated", side_effect=_gated), \
                patch("cqc_lem.utilities.logger.log_debug") as debug:
            got = cc.render_deck_cover_concept(self._Scope(_concept(_REPORT)), 1, 2)
        assert got == art, debug.call_args_list

    def test_a_person_in_the_prompt_is_never_rendered(self):
        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=self._brief("A manager holding a calendar.")), \
                patch("cqc_lem.utilities.ai.image_gen.render_image_gated") as gated:
            assert cc.render_deck_cover_concept(self._Scope(_concept(_REPORT)), 1, 2) is None
        gated.assert_not_called()

    def test_a_rejected_render_is_none(self, tmp_path):
        art = str(tmp_path / "a.png")
        Image.new("RGB", (64, 64)).save(art)

        def _gated(prompt, *, render_info, **_kw):
            render_info["gate_verdict"] = "rejected"
            return art

        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   return_value=self._brief()), \
                patch("cqc_lem.utilities.ai.image_gen.render_image_gated", side_effect=_gated):
            assert cc.render_deck_cover_concept(self._Scope(_concept(_REPORT)), 1, 2) is None

    def test_any_fault_is_none(self):
        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                   side_effect=RuntimeError("down")):
            assert cc.render_deck_cover_concept(self._Scope(_concept(_REPORT)), 1, 2) is None
