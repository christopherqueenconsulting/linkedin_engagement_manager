"""Showcase round 7: batch-level visual variety, and cover lines that never touch.

123/135 were the same title card, 142/143 the same deck, 125/r3/r6 three dark quote cards, and four
of five covers the same 50/50 split. docs/image-stack.md, "Showcase round 7".
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw, ImageFont

from cqc_lem.utilities import carousel_creator as cc, video_title_card as tc
from cqc_lem.utilities.ai import image_concept as ic, post_treatment as pt

pytestmark = pytest.mark.unit


def _concept(**kw) -> ic.ImageConcept:
    base = dict(thesis="AI spend hides in seat licences", audience="ops", specific_entities=(),
                emotional_beat="calm", hook_phrase="", treatment="editorial_concept",
                treatment_rationale="x")
    base.update(kw)
    return ic.ImageConcept(**base)


class TestPairs:
    def test_recent_pairs_is_the_previous_three(self):
        shown = pt.recent_pairs(["a", "b", None, "d", "e"], ["x", "y", "z", "w", "v"])
        assert shown == {("a", "x"), ("b", "y")}

    def test_the_second_dimension_moves_first(self):
        assert pt.fresh_pair("a", "x", ("a", "b"), ("x", "y"), ["a"], ["x"]) == ("a", "y", True)

    def test_then_the_first(self):
        got = pt.fresh_pair("a", "x", ("a", "b"), ("x", "y"), ["a", "a"], ["x", "y"])
        assert got == ("b", "x", True)

    def test_a_fresh_pair_or_no_way_out_keeps_the_pick(self):
        assert pt.fresh_pair("a", "y", ("a",), ("x", "y"), ["a"], ["x"]) == ("a", "y", False)
        assert pt.fresh_pair("a", "x", ("a",), ("x",), ["a"], ["x"]) == ("a", "x", False)
        assert pt.fresh_pair(None, "x", ("a",), ("x",), ["a"], ["x"]) == (None, "x", False)

    def test_a_post_image_never_repeats_a_recent_layout_and_panel(self):
        history = {"treatment": ["photo_only", "photo_only"], "layout": ["split_bottom", "split_top"],
                   "panel": ["charcoal", "off_white"], "shot": [], "grade": [], "setting": [],
                   "style": [], "card_layout": []}
        rhythm = pt.plan_post_rhythm(_concept(layout="split_top"), "A post.", history, 0.0,
                                     ("charcoal", "off_white"))
        assert (rhythm.layout, rhythm.panel) not in {("split_bottom", "charcoal"),
                                                     ("split_top", "off_white")}
        assert "layout_panel" in rhythm.rerolled


class TestQuoteCap:
    def test_one_quote_card_in_six_posts(self):
        # Round 7 set one in four; round 8 widened it to one in six (QUOTE_CAP_WINDOW).
        assert pt.TREATMENT_QUOTE_CARD in pt.style_blocks(
            [None, None, None, None, pt.STYLE_QUOTE_CARD])
        assert pt.TREATMENT_QUOTE_CARD not in pt.style_blocks(
            [None, None, None, None, None, pt.STYLE_QUOTE_CARD])


class TestTitleCards:
    def test_the_ground_joins_the_rotation(self):
        recent = [(tc.VARIANT_QUESTION, tc.GROUND_CHARCOAL), (tc.VARIANT_POSTER, tc.GROUND_CHARCOAL)]
        with patch.object(tc, "pick_ground", return_value=tc.GROUND_CHARCOAL):
            got = tc.pick_card_style("AI spend hides in seat licences", recent, "s")
        assert got == (tc.VARIANT_POSTER, tc.GROUND_OFF_WHITE)

    def test_history_reads_legacy_names_and_writes_pairs(self, tmp_path):
        with patch.object(tc, "title_card_dir", return_value=str(tmp_path)):
            path = tc.variant_history_path(3)
            import os
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                json.dump([tc.VARIANT_NUMBER, {"variant": tc.VARIANT_POSTER, "ground": "nope"}], fh)
            assert tc.recent_cards(3) == [(tc.VARIANT_NUMBER, None), (tc.VARIANT_POSTER, None)]
            tc.record_variant(3, tc.VARIANT_QUESTION, tc.GROUND_OFF_WHITE)
            assert tc.recent_cards(3)[0] == (tc.VARIANT_QUESTION, tc.GROUND_OFF_WHITE)
            assert tc.recent_variants(3)[:2] == [tc.VARIANT_QUESTION, tc.VARIANT_NUMBER]
            # The motion round keeps the cards under "variants", beside "motions".
            with open(path) as fh:
                assert json.load(fh)["variants"][0] == {"variant": tc.VARIANT_QUESTION,
                                                        "ground": tc.GROUND_OFF_WHITE}


class TestDecks:
    def test_a_recent_template_and_cover_pair_moves_back(self):
        chain = cc.deck_cover_chain("A title", True, False, ["code_drawn", "poster"], "s",
                                    template="bold_listicle",
                                    recent_templates=["stat_reveal", "bold_listicle"])
        assert chain == [cc.DECK_COVER_DRAWN, cc.DECK_COVER_POSTER, cc.DECK_COVER_TEMPLATE]
        chain = cc.deck_cover_chain("A title", True, False, ["code_drawn", "poster"], "s",
                                    template="stat_reveal",
                                    recent_templates=["stat_reveal", "bold_listicle"])
        assert chain == [cc.DECK_COVER_POSTER, cc.DECK_COVER_DRAWN, cc.DECK_COVER_TEMPLATE]

    def test_receipts_record_the_template(self, tmp_path):
        deck = tmp_path / "d1"
        deck.mkdir()
        (deck / "deck_render.json").write_text(json.dumps(
            {"user_id": 1, "template": "bold_listicle", "cover_treatment": "poster"}))
        assert cc.recent_deck_choices(str(tmp_path), 1)["template"] == ["bold_listicle"]


class TestCovers:
    def test_no_third_split_in_a_row(self):
        assert ic.split_run_exceeded("split_left", ["split_right", "split_left"])
        assert not ic.split_run_exceeded("split_left", ["split_right", "full_bleed"])
        assert not ic.split_run_exceeded("split_left", ["split_right"])
        assert not ic.split_run_exceeded("full_bleed", ["split_right", "split_left"])

    def test_the_cover_rotation_breaks_the_run(self):
        out = ic.assign_layout_and_cast(_concept(), "newsletter", ["split_right", "split_left"])
        assert out.layout == ic.COVER_BREAK_LAYOUT
        out = ic.assign_layout_and_cast(_concept(), "newsletter", ["split_right", "full_bleed"])
        assert out.layout in ic.COVER_LAYOUTS
        out = ic.assign_layout_and_cast(_concept(), "post_image", ["split_top", "split_bottom"])
        assert out.layout in ic.POST_LAYOUTS


class TestLinePitch:
    def _font(self, size=80):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # pragma: no cover - Pillow < 10.1
            pytest.skip("Pillow cannot size the default font")

    def test_a_cap_only_line_still_advances_a_full_em(self):
        font = self._font()
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        assert cc.line_pitch("HONEST AI", font, draw) >= 80
        assert cc._lines_height(["HONEST AI", "Planning"], font, 10, draw) >= 2 * (80 + 10)

    def test_a_font_without_a_size_falls_back_to_its_ink(self):
        draw = MagicMock()
        draw.textbbox.return_value = (0, 5, 100, 44)
        assert cc.line_pitch("Planning", object(), draw) == 39
