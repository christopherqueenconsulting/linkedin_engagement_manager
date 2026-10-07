"""Showcase round 6: one card-layout rotation across every code-drawn card family.

No layout repeats within `CARD_LAYOUT_WINDOW` consecutive posts (slot_125 and rhythm_2
shipped the identical charcoal grid card).
"""

import pytest

from cqc_lem.utilities.ai import image_graphics as ig, post_treatment as pt
from tests.unit.utilities.test_post_rhythm import _OPINION, _STAT_SENTENCE, _Env, _force_chain

pytestmark = pytest.mark.unit


class TestCardLayoutOf:
    @pytest.mark.parametrize("rhythm,expected", [
        ({"card_layout": "grid_rule", "treatment": "typeset_card"}, "grid_rule"),
        ({"treatment": "quote_card"}, pt.CARD_LAYOUT_QUOTE),
        ({"treatment": "photo_only", "style": "quote_card"}, pt.CARD_LAYOUT_QUOTE),
        ({"treatment": "data_card"}, pt.CARD_LAYOUT_STAT),
        ({"treatment": "typeset_card", "layout": "split_top"}, "panel_split_top"),
        ({"treatment": "typeset_card"}, None),
        ({"treatment": "photo_only"}, None),
    ])
    def test_every_family_has_one_value(self, rhythm, expected):
        assert pt.card_layout_of(rhythm) == expected

    def test_history_reads_older_receipts_across_families(self):
        receipts = [{"rhythm": {"treatment": "quote_card", "style": "quote_card"}},
                    {"rhythm": {"treatment": "data_card"}},
                    {"rhythm": {"treatment": "typeset_card", "card_layout": "poster"}}]
        assert pt.rhythm_history(receipts)["card_layout"] == [
            pt.CARD_LAYOUT_QUOTE, pt.CARD_LAYOUT_STAT, "poster"]


class TestPickCardLayout:
    OPTIONS = (ig.CARD_POSTER, ig.CARD_GRID_RULE, ig.CARD_QUOTE_MARKS)

    def test_the_layout_two_posts_back_is_skipped_even_when_lru_wants_it(self):
        # grid_rule ran two posts ago, poster four posts ago: LRU alone would pick quote_marks,
        # then poster; grid_rule is never eligible while it sits in the window.
        recent = ["quote_card", ig.CARD_GRID_RULE, None, ig.CARD_POSTER]
        pick, _ = pt.pick_card_layout(self.OPTIONS, recent)
        assert pick == ig.CARD_QUOTE_MARKS

    def test_a_run_of_three_never_repeats(self):
        options = (ig.CARD_POSTER, ig.CARD_GRID_RULE, ig.CARD_QUOTE_MARKS)
        history: list = []
        for _ in range(9):
            pick, _ = pt.pick_card_layout(options, history)
            history.insert(0, pick)
        for i in range(len(history) - 2):
            assert len(set(history[i:i + 3])) == 3, history

    def test_every_option_in_the_window_keeps_the_least_recent(self):
        pick, rerolled = pt.pick_card_layout((ig.CARD_POSTER, ig.CARD_GRID_RULE),
                                             [ig.CARD_GRID_RULE, ig.CARD_POSTER])
        assert pick == ig.CARD_POSTER and rerolled is False

    def test_no_options(self):
        assert pt.pick_card_layout((), ["x"]) == (None, False)


class TestCardPanel:
    def test_a_layout_never_ships_twice_on_the_same_panel(self):
        # rhythm_2 then slot_125: the grid rule on charcoal, twice.
        panel = pt.card_panel(ig.CARD_GRID_RULE, "charcoal", [None, ig.CARD_GRID_RULE],
                              ["gold", "charcoal"], ("charcoal", "gold", "off_white"))
        assert panel == "gold"

    def test_a_fresh_pairing_keeps_the_rotations_panel(self):
        assert pt.card_panel(ig.CARD_POSTER, "charcoal", [ig.CARD_GRID_RULE], ["charcoal"],
                             ("charcoal", "gold")) == "charcoal"
        assert pt.card_panel(None, "charcoal", [], [], ()) == "charcoal"

    def test_every_panel_shown_keeps_the_rotations_panel(self):
        assert pt.card_panel(ig.CARD_POSTER, "charcoal", [ig.CARD_POSTER], ["charcoal"],
                             ("charcoal",)) == "charcoal"

    def test_the_last_resort_card_rotates_its_panel(self, tmp_path):
        from unittest.mock import patch

        from cqc_lem.utilities import post_image

        rhythm = pt.PostRhythm(plan=pt.TreatmentPlan((), (), False, False), panel="charcoal",
                               grade="", layout="", shot="", quote_candidates=(), opinion="",
                               rerolled=(), recent_card_layouts=(ig.CARD_POSTER,),
                               recent_card_panels=("charcoal",), panels=("charcoal", "gold"))
        with patch("cqc_lem.utilities.post_image.assets_dir", str(tmp_path)), \
             patch("cqc_lem.utilities.ai.image_graphics.typeset_layouts_for",
                   return_value=(ig.CARD_POSTER,)), \
             patch("cqc_lem.utilities.ai.image_graphics.render_typeset_card") as render:
            render.return_value.path = str(tmp_path / "card.png")
            out = post_image._render_last_resort_card(None, rhythm, "A post about pricing.",
                                                      user_id=1, post_id=2, brand="",
                                                      ratio="4:5")
        assert render.call_args.kwargs["panel"] == "gold"
        assert out.render_info["panel"] == "gold"


class TestSingleLayoutCards:
    def test_a_recent_quote_or_stat_card_blocks_another(self):
        blocks = pt.card_layout_blocks(["poster", pt.CARD_LAYOUT_QUOTE])
        assert pt.TREATMENT_QUOTE_CARD in blocks and pt.TREATMENT_DATA_CARD not in blocks
        blocks = pt.card_layout_blocks([pt.CARD_LAYOUT_STAT])
        assert set(blocks) == {pt.TREATMENT_DATA_CARD}

    def test_outside_the_window_nothing_is_blocked(self):
        assert pt.card_layout_blocks([None, None, pt.CARD_LAYOUT_QUOTE]) == {}

    def test_the_plan_skips_a_blocked_card_with_its_reason(self):
        history = {dim: [] for dim in pt.RHYTHM_DIMENSIONS}
        history["card_layout"] = [None, pt.CARD_LAYOUT_QUOTE]
        from unittest.mock import patch

        # The post itself could take a quote card; only the layout window stands in the way.
        with patch.object(pt, "unavailable_treatments", return_value={}):
            rhythm = pt.plan_post_rhythm(None, "A post.", history, 0.4, ("charcoal",))
        assert pt.TREATMENT_QUOTE_CARD not in rhythm.plan.chain
        assert any(t == pt.TREATMENT_QUOTE_CARD and "card layout repeats" in why
                   for t, why in rhythm.plan.skipped)


class TestTheReceiptRecordsEveryFamily:
    def test_a_data_card_records_its_layout(self, tmp_path):
        env = _Env(tmp_path)
        _, _, receipt = env.post(_STAT_SENTENCE + " Deposits fixed it.",
                                 **_force_chain("data_card", "photo_only"))
        assert receipt["rhythm"]["card_layout"] == pt.CARD_LAYOUT_STAT

    def test_a_quote_card_records_its_layout(self, tmp_path):
        env = _Env(tmp_path)
        _, _, receipt = env.post(_OPINION, **_force_chain("quote_card", "photo_only"))
        assert receipt["rhythm"]["card_layout"] == pt.CARD_LAYOUT_QUOTE

    def test_a_photo_records_none(self, tmp_path):
        env = _Env(tmp_path)
        _, _, receipt = env.post(_OPINION, **_force_chain("photo_only"))
        assert receipt["rhythm"]["card_layout"] is None
