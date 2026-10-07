"""Code-drawn archetypes: verbatim fact safety and legible, on-brand drawing (issue #2241)."""
import colorsys
import copy

import pytest
from PIL import Image

from cqc_lem.utilities.ai import image_graphics as g
from cqc_lem.utilities.ai.image_compose import BrandStyle

pytestmark = pytest.mark.unit

SOURCE = (
    "A 2025 Originality.ai study found that 53.7% of long-form posts were likely AI-written. "
    "Those AI-written posts earned 45% less engagement than posts written by people. "
    "Human-written posts drew 38.2% engagement in the same sample. "
    "Manual invoice follow-up cost $1,800 a month in staff time. "
    "Chasing late payments by phone took another $650 a month. "
    "Rework from copy-paste errors cost $420 a month. "
    "In total the agency lost $2,870 a month. "
    "Before the automation, month-end close took 6 days. "
    "After the automation, month-end close took 2 days. "
    "Our tooling bill was $30K per quarter. Routing cut costs 3x for small prompts. "
    "First, map every manual handoff in the process. "
    "Then automate the invoice reminders before anything else. "
    "Next, route exceptions to one named owner. "
    "Finally, review the error log every Friday.")


def _item(label, value, unit, sentence):
    return {"label": label, "value": value, "unit": unit, "source_sentence": sentence}


S_AI = "Those AI-written posts earned 45% less engagement than posts written by people."
S_HUMAN = "Human-written posts drew 38.2% engagement in the same sample."
S_SHARE = ("A 2025 Originality.ai study found that 53.7% of long-form posts were likely "
           "AI-written.")
S_FOLLOW = "Manual invoice follow-up cost $1,800 a month in staff time."
S_PHONE = "Chasing late payments by phone took another $650 a month."
S_REWORK = "Rework from copy-paste errors cost $420 a month."
S_TOTAL = "In total the agency lost $2,870 a month."
S_BEFORE = "Before the automation, month-end close took 6 days."
S_AFTER = "After the automation, month-end close took 2 days."

RAW = {
    "source_name": "Originality.ai",
    "thesis_stat": _item("less engagement on AI-written posts", "45", "%", S_AI),
    "comparison": {"measure": "engagement", "highlight": 0, "annotation": "AI-written posts",
                   "items": [_item("AI-written posts", "45", "%", S_AI),
                             _item("Human-written posts", "38.2", "%", S_HUMAN)]},
    "costs": {"subject": "invoice follow-up",
              "items": [_item("Manual invoice follow-up", "1,800", "$", S_FOLLOW),
                        _item("Chasing late payments", "650", "$", S_PHONE),
                        _item("Rework from errors", "420", "$", S_REWORK)],
              "total": _item("agency lost", "2,870", "$", S_TOTAL)},
    "before_after": {"before": _item("month-end close", "6", "days", S_BEFORE),
                     "after": _item("month-end close", "2", "days", S_AFTER)},
    "steps": [
        {"text": "Map every manual handoff",
         "source_sentence": "First, map every manual handoff in the process."},
        {"text": "Automate the invoice reminders",
         "source_sentence": "Then automate the invoice reminders before anything else."},
        {"text": "Route exceptions to one named owner",
         "source_sentence": "Next, route exceptions to one named owner."},
        {"text": "Review the error log every Friday",
         "source_sentence": "Finally, review the error log every Friday."}],
}


@pytest.fixture(scope="module")
def graphic():
    return g.validate_graphic_facts(RAW, SOURCE)


class TestVerbatimFigure:
    @pytest.mark.parametrize("value,unit,sentence,expected", [
        ("45", "%", S_AI, "45%"),
        ("45", "%", "engagement fell 45 percent last year", "45%"),
        ("1,800", "$", S_FOLLOW, "$1,800"),
        ("30", "$", "Our tooling bill was $30K per quarter.", "$30K"),
        ("3", "x", "Routing cut costs 3x for small prompts.", "3x"),
        ("6", "days", S_BEFORE, "6 days"),
        ("6", "day", S_BEFORE, "6 days"),
        ("2025", "", S_SHARE, "2025"),
    ])
    def test_the_drawn_string_is_the_sentences_own(self, value, unit, sentence, expected):
        assert g.verbatim_figure(value, unit, sentence) == expected

    @pytest.mark.parametrize("value,unit,sentence", [
        ("62", "%", S_AI),            # a number the sentence never states
        ("45", "$", S_AI),            # the right number in the wrong unit
        ("4", "%", S_AI),             # a prefix of a real number
        ("5", "%", S_AI),             # a suffix of a real number
        ("38", "%", S_HUMAN),         # the integer part of 38.2
        ("1,800", "€", S_FOLLOW),     # the wrong currency
        ("6", "hours", S_BEFORE),     # the wrong unit word
        ("forty", "%", S_AI),         # not a number at all
        ("45", "%", ""),
    ])
    def test_a_fabricated_or_mismatched_value_is_refused(self, value, unit, sentence):
        assert g.verbatim_figure(value, unit, sentence) is None


class TestFactValidation:
    def test_a_grounded_fact_validates_with_its_trace(self):
        fact, reason = g.validate_fact(_item("less engagement", "45", "%", S_AI), SOURCE)
        assert reason == "" and fact["display"] == "45%"
        assert fact["source_sentence"] == S_AI and fact["amount"] == 45.0

    def test_a_fabricated_value_is_refused_deterministically(self):
        """The fabrication guard: a number its own sentence never states is never drawn."""
        fact, reason = g.validate_fact(_item("less engagement", "62", "%", S_AI), SOURCE)
        assert fact is None and "not in its source sentence" in reason

    def test_a_sentence_the_article_never_says_is_refused(self):
        fact, reason = g.validate_fact(
            _item("less engagement", "45", "%", "AI posts earn 45% less engagement."), SOURCE)
        assert fact is None and "not in the article" in reason

    def test_an_invented_label_word_is_refused(self):
        fact, reason = g.validate_fact(_item("less reach on AI posts", "45", "%", S_AI), SOURCE)
        assert fact is None and "label" in reason

    @pytest.mark.parametrize("label", ["", "a label far too long to be one line on any graphic",
                                       "45 percent of 99 posts"])
    def test_label_rules(self, label):
        assert not g.label_grounded(label, S_AI)

    def test_not_an_object(self):
        assert g.validate_fact("45%", SOURCE) == (None, "not an object")


class TestValidateGraphicFacts:
    def test_every_section_validates(self, graphic):
        assert graphic["stat"]["display"] == "45%"
        assert [f["display"] for f in graphic["comparison"]["items"]] == ["45%", "38.2%"]
        assert graphic["comparison"]["highlight"] == 0
        assert graphic["costs"]["total"]["display"] == "$2,870"
        assert graphic["before_after"]["after"]["display"] == "2 days"
        assert len(graphic["steps"]) == 4
        assert graphic["source_line"] == "Source: Originality.ai"
        assert graphic["rejected"] == []
        assert g.available_archetypes(graphic) == g.CODE_DRAWN_ARCHETYPES

    def test_a_source_the_article_never_names_draws_no_source_line(self):
        # Showcase round 4: never a generic "From the article" — a post has no article.
        out = g.validate_graphic_facts(dict(RAW, source_name="Gartner"), SOURCE)
        assert out["source_line"] == g.NO_SOURCE_LINE == ""

    def test_a_comparison_needs_two_like_values(self):
        raw = copy.deepcopy(RAW)
        raw["comparison"]["items"] = [_item("AI-written posts", "45", "%", S_AI),
                                      _item("Manual invoice follow-up", "1,800", "$", S_FOLLOW)]
        assert "comparison" not in g.validate_graphic_facts(raw, SOURCE)

    def test_one_fabricated_item_drops_out_and_never_invents_a_baseline(self):
        raw = copy.deepcopy(RAW)
        raw["comparison"]["items"].append(_item("Industry baseline", "50", "%", S_AI))
        out = g.validate_graphic_facts(raw, SOURCE)
        assert [f["label"] for f in out["comparison"]["items"]] == ["AI-written posts",
                                                                   "Human-written posts"]
        assert any("not in its source sentence" in r for r in out["rejected"])

    def test_an_out_of_range_highlight_falls_to_the_largest_value(self):
        raw = copy.deepcopy(RAW)
        raw["comparison"]["highlight"] = 9
        raw["comparison"]["items"].reverse()
        assert g.validate_graphic_facts(raw, SOURCE)["comparison"]["highlight"] == 1

    def test_an_ungrounded_annotation_is_dropped(self):
        raw = copy.deepcopy(RAW)
        raw["comparison"]["annotation"] = "Bots are winning the feed"
        out = g.validate_graphic_facts(raw, SOURCE)
        assert out["comparison"]["annotation"] == ""

    def test_a_total_in_another_unit_is_refused_and_none_is_ever_summed(self):
        raw = copy.deepcopy(RAW)
        raw["costs"]["total"] = _item("month-end close", "6", "days", S_BEFORE)
        out = g.validate_graphic_facts(raw, SOURCE)
        assert out["costs"]["total"] is None
        raw["costs"]["total"] = None
        assert g.validate_graphic_facts(raw, SOURCE)["costs"]["total"] is None

    def test_costs_need_two_money_or_time_items(self):
        raw = copy.deepcopy(RAW)
        raw["costs"]["items"] = raw["costs"]["items"][:1] + [_item("posts", "45", "%", S_AI)]
        assert "costs" not in g.validate_graphic_facts(raw, SOURCE)

    def test_before_and_after_must_share_a_unit(self):
        raw = copy.deepcopy(RAW)
        raw["before_after"]["after"] = _item("less engagement", "45", "%", S_AI)
        out = g.validate_graphic_facts(raw, SOURCE)
        assert "before_after" not in out and any("before_after" in r for r in out["rejected"])

    def test_steps_need_three_grounded_ones(self):
        raw = copy.deepcopy(RAW)
        raw["steps"] = raw["steps"][:2] + [{"text": "Hire a growth hacker",
                                            "source_sentence": "Finally, review the error log "
                                                               "every Friday."}]
        out = g.validate_graphic_facts(raw, SOURCE)
        assert "steps" not in out and any("not grounded" in r for r in out["rejected"])

    def test_junk_is_tolerated(self):
        out = g.validate_graphic_facts("not a dict", SOURCE)
        assert g.available_archetypes(out) == ()
        assert g.available_archetypes(None) == ()


class TestTraceability:
    def test_drawn_facts_carry_their_sentences(self, graphic):
        for archetype in g.CODE_DRAWN_ARCHETYPES:
            for fact in g.assert_traceable(archetype, graphic):
                assert fact["source_sentence"] in SOURCE

    def test_a_tampered_figure_is_refused_before_ink(self, graphic):
        tampered = copy.deepcopy(graphic)
        tampered["stat"]["display"] = "62%"
        with pytest.raises(g.UngroundedFactError):
            g.assert_traceable(g.STAT_CARD, tampered)
        with pytest.raises(g.UngroundedFactError):
            g.render_graphic(g.STAT_CARD, tampered, surface="post_image", hook="A hook here")

    def test_a_tampered_label_or_step_is_refused(self, graphic):
        tampered = copy.deepcopy(graphic)
        tampered["comparison"]["items"][1]["label"] = "Industry baseline"
        with pytest.raises(g.UngroundedFactError):
            g.assert_traceable(g.HIGHLIGHT_CHART, tampered)
        tampered["steps"][0]["text"] = "Fire the whole team"
        with pytest.raises(g.UngroundedFactError):
            g.assert_traceable(g.CHECKLIST, tampered)

    def test_a_missing_section_is_a_graphic_error(self):
        with pytest.raises(g.GraphicError):
            g.drawn_facts(g.RECEIPT, {})


def _saturated_hues(path):
    with Image.open(path) as image:
        # NEAREST: a bicubic downsample rings at gold/charcoal edges and invents blue pixels.
        small = image.convert("RGB").resize((240, round(240 * image.height / image.width)),
                                            Image.NEAREST)
    raw = small.tobytes()
    hues = []
    for r, gr, b in zip(raw[0::3], raw[1::3], raw[2::3]):
        h, s, v = colorsys.rgb_to_hsv(r / 255, gr / 255, b / 255)
        if s > 0.3 and v > 0.15:
            hues.append(h * 360)
    return hues


class TestRenderGraphic:
    @pytest.mark.parametrize("surface", ["newsletter", "post_image"])
    @pytest.mark.parametrize("archetype", g.CODE_DRAWN_ARCHETYPES)
    def test_size_text_placement_floors_and_palette(self, graphic, tmp_path, surface, archetype):
        out = tmp_path / f"{surface}_{archetype}.png"
        render = g.render_graphic(archetype, graphic, surface=surface, hook="Who reads AI posts?",
                                  kicker="AI CONTENT", signature="The Brief",
                                  out_path=str(out))
        width, height = g.GRAPHIC_CANVAS[surface]
        with Image.open(render.path) as image:
            assert image.size == (width, height) == render.canvas
        assert render.placements
        for p in render.placements:
            left, top, right, bottom = p.box
            assert (p.bounds[0] <= left and p.bounds[1] <= top and right <= p.bounds[2]
                    and bottom <= p.bounds[3]), f"{p.role} {p.text!r} clips its bounds"
            if p.frame == "canvas":
                assert 0 <= p.bounds[0] and p.bounds[2] <= width and p.bounds[3] <= height
            if p.size:
                floor = g.SOURCE_MIN if p.role == "source" else g.BODY_MIN
                assert p.size >= round(width * floor), f"{p.role} is below the 400px floor"
        # Only the brand gold is ever a colour; everything else is charcoal, grey or off-white.
        hues = _saturated_hues(render.path)
        assert hues, "the gold highlight is missing"
        assert all(35 <= h <= 65 for h in hues), "a colour other than the brand gold"
        for fact in render.facts:
            assert fact["source_sentence"] in SOURCE

    def test_the_panel_never_repeats_the_figure_as_a_hero(self, graphic, tmp_path):
        from unittest.mock import patch

        with patch.object(g, "compose_headline", wraps=g.compose_headline) as compose:
            g.render_graphic(g.STAT_CARD, graphic, surface="post_image",
                             hook="45% less engagement on AI posts",
                             out_path=str(tmp_path / "a.png"))
        assert compose.call_args.kwargs["hero"] is False
        assert compose.call_args.kwargs["canvas"] == g.GRAPHIC_CANVAS["post_image"]

    def test_a_receipt_without_a_stated_total_circles_its_largest_line(self, graphic, tmp_path):
        no_total = copy.deepcopy(graphic)
        no_total["costs"]["total"] = None
        render = g.render_graphic(g.RECEIPT, no_total, surface="newsletter", hook="The real cost",
                                  out_path=str(tmp_path / "r.png"))
        assert "TOTAL" not in [p.text for p in render.placements]
        assert [f["display"] for f in render.facts] == ["$1,800", "$650", "$420"]

    def test_data_that_cannot_be_set_legibly_is_refused(self, graphic, monkeypatch, tmp_path):
        monkeypatch.setattr(g, "BODY_MIN", 0.2)
        for archetype in g.CODE_DRAWN_ARCHETYPES:
            with pytest.raises(g.GraphicLayoutError):
                g.render_graphic(archetype, graphic, surface="post_image", hook="A hook",
                                 out_path=str(tmp_path / f"{archetype}.png"))

    def test_an_overlong_source_name_is_dropped_never_replaced(self, graphic, monkeypatch,
                                                               tmp_path):
        long_source = dict(graphic, source_line="Source: " + "Very Long Institute " * 8)
        render = g.render_graphic(g.STAT_CARD, long_source, surface="post_image", hook="A hook",
                                  out_path=str(tmp_path / "s.png"))
        assert [p.text for p in render.placements if p.role == "source"] == []

    @pytest.mark.parametrize("archetype", [g.STAT_CARD, g.CHECKLIST])
    def test_no_card_ever_prints_from_the_article(self, graphic, archetype, tmp_path):
        unsourced = dict(graphic, source_line="")
        render = g.render_graphic(archetype, unsourced, surface="post_image", hook="A hook",
                                  out_path=str(tmp_path / "c.png"))
        assert render.placements
        assert not any("article" in p.text.lower() for p in render.placements)
        assert [p for p in render.placements if p.role == "source"] == []

    @pytest.mark.parametrize("archetype,hook", [("stock_photo", "A hook"), (g.STAT_CARD, " ")])
    def test_refusals(self, graphic, archetype, hook):
        with pytest.raises(g.GraphicError):
            g.render_graphic(archetype, graphic, surface="post_image", hook=hook)

    def test_the_brand_kit_colours_are_used_exactly(self, graphic, tmp_path):
        brand = BrandStyle(primary="#E9D437", neutral_dark="#101010", accent="#A89816",
                           neutral_light="#FAFAFA")
        render = g.render_graphic(g.STAT_CARD, graphic, surface="newsletter", hook="A hook",
                                  brand=brand, layout="split_right",
                                  out_path=str(tmp_path / "b.png"))
        with Image.open(render.path) as image:
            assert image.convert("RGB").getpixel((image.width - 5, 5)) == (16, 16, 16)

    def test_a_default_temp_output_is_written(self, graphic):
        import os

        render = g.render_graphic(g.CHECKLIST, graphic, surface="post_image", hook="A hook",
                                  layout="not_a_layout")
        try:
            assert os.path.isfile(render.path)
        finally:
            os.remove(render.path)


class TestChartAnnotation:
    def test_an_annotation_restating_the_highlight_label_is_not_drawn(self, graphic, tmp_path):
        render = g.render_graphic(g.HIGHLIGHT_CHART, graphic, surface="post_image", hook="A hook",
                                  out_path=str(tmp_path / "c.png"))
        assert graphic["comparison"]["annotation"] == "AI-written posts"
        assert not [p for p in render.placements if p.role == "annotation"]

    def test_a_distinct_annotation_is_drawn(self, graphic, tmp_path):
        noted = copy.deepcopy(graphic)
        noted["comparison"]["annotation"] = "less engagement than posts written by people"
        render = g.render_graphic(g.HIGHLIGHT_CHART, noted, surface="post_image", hook="A hook",
                                  out_path=str(tmp_path / "c.png"))
        assert [p for p in render.placements if p.role == "annotation"]

    def test_repeats_label(self):
        assert g._repeats_label("AI-written posts", "Fully AI-written posts")
        assert not g._repeats_label("Half the comments", "Fully AI-written posts")
        assert not g._repeats_label("", "x")

    def test_temp_cleanup_failure_is_logged_not_raised(self, graphic, tmp_path):
        from unittest.mock import patch

        with patch.object(g.os, "remove", side_effect=OSError("busy")), \
                patch.object(g, "log_debug") as debug:
            g.render_graphic(g.STAT_CARD, graphic, surface="post_image", hook="A hook",
                             out_path=str(tmp_path / "s.png"))
        debug.assert_called_once()


class TestChecklistStates:
    @pytest.mark.parametrize("count,expected", [
        (3, ("check", "gap", "open")),
        (4, ("check", "check", "gap", "open")),
        (5, ("check", "check", "gap", "open", "open")),
    ])
    def test_some_are_deliberately_left_open(self, count, expected):
        assert g.checklist_states(count) == expected
