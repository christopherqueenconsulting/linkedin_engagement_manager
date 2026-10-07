"""#2241 showcase B: floors, the last-resort card, the final hook gate, clichés and settings.

Each class pins one engine rule round B broke, with the exact string or receipt that broke it.
"""

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from cqc_lem.utilities.ai import (
    image_concept as ic,  # noqa: E402
    image_gen,  # noqa: E402
    image_graphics as ig,  # noqa: E402
    post_treatment as pt,  # noqa: E402
)
from cqc_lem.utilities.ai.image_brief import cliche_hit  # noqa: E402
from tests.unit.utilities.test_post_rhythm import _REPORT, _concept, _Env, _force_chain  # noqa: E402

pytestmark = pytest.mark.unit

ROUTING_THESIS = ("Routing prompts to models based on complexity reduces AI costs while "
                  "maintaining successful resolution rates.")
BROKEN_HOOK = "Routing prompts to models based costs"


class TestHeadlineFreeFloors:
    """Owner-delegated decision: the post text sits above a headline-free image in the feed."""

    def test_a_photo_only_post_passes_specificity_at_3_but_must_be_strong(self):
        floors = image_gen.rubric_floors("post_image", None)
        assert floors["specificity"] == 3
        assert floors["scroll_stop"] == 4 and floors["craft"] == 4
        assert floors["no_cliche"] == 5 and floors["text_accuracy"] == 4

    def test_a_post_with_a_headline_keeps_specificity_4(self):
        assert image_gen.rubric_floors("post_image", "Late invoices starve agencies")[
            "specificity"] == 4

    @pytest.mark.parametrize("hook", [None, "Payroll eats first"])
    def test_a_video_frame_takes_the_photo_floors_with_or_without_a_caption(self, hook):
        floors = image_gen.rubric_floors("video", hook)
        assert (floors["specificity"], floors["scroll_stop"], floors["craft"]) == (3, 4, 4)
        assert floors["no_cliche"] == 5

    def test_covers_and_code_drawn_graphics_are_unchanged(self):
        assert image_gen.rubric_floors("newsletter", "Hook")["specificity"] == 4
        assert image_gen.rubric_floors("post_image", None, code_drawn=True) == dict(
            image_gen._CODE_DRAWN_FLOORS)

    def _judge(self, tmp_path, surface, hook, **scores):
        rubric = {"specificity": 3, "no_cliche": 5, "thumbnail_read": 4, "craft": 4,
                  "scroll_stop": 4, "brand_fit": 4, **scores}
        blind = MagicMock(choices=[MagicMock(message=MagicMock(content="An open office."))])
        answer = MagicMock(choices=[MagicMock(message=MagicMock(
            content='{"rubric": %s}' % str(rubric).replace("'", '"')))])
        path = str(tmp_path / "r.png")
        Image.new("RGB", (64, 64)).save(path)
        concept = _concept(_REPORT, archetype="people_scene")
        with patch.object(image_gen.client.chat.completions, "create",
                          side_effect=[blind, answer]):
            return image_gen._staged_inspect(path, concept, hook, surface)

    def test_round_bs_photo_only_scores_now_pass(self, tmp_path):
        assert self._judge(tmp_path, "post_image", None).acceptable

    @pytest.mark.parametrize("criterion", ["scroll_stop", "craft"])
    def test_but_a_weak_photo_still_fails(self, tmp_path, criterion):
        verdict = self._judge(tmp_path, "post_image", None, **{criterion: 3})
        assert not verdict.acceptable and criterion in verdict.failing

    def test_a_typeset_card_scene_at_specificity_3_still_fails(self, tmp_path):
        verdict = self._judge(tmp_path, "post_image", "Late invoices starve agencies")
        assert "specificity" in verdict.failing


class TestLastResortCard:
    def test_the_card_draws_the_hook_on_a_brand_panel_with_no_photo(self, tmp_path):
        out = str(tmp_path / "card.png")
        drawn = ig.render_typeset_card("Late invoices starve agencies", panel="gold",
                                       layout="split_bottom", kicker="CASH FLOW", out_path=out)
        with Image.open(drawn.path) as im:
            assert im.size == ig.GRAPHIC_CANVAS["post_image"]
        assert drawn.archetype == ig.TYPESET_CARD and drawn.facts == ()
        assert [p.role for p in drawn.placements] == ["accent"]

    def test_no_headline_no_card(self):
        with pytest.raises(ig.GraphicError):
            ig.render_typeset_card("  ")

    def test_the_hook_is_stage_1s_else_the_posts_own_first_line(self):
        from cqc_lem.utilities.post_image import last_resort_hook

        assert last_resort_hook(_concept(_REPORT), _REPORT) == "Late invoices starve agencies"
        assert last_resort_hook(None, _REPORT) == "We moved support to a shared queue."
        # #2241 showcase C: a long opening is never a reason to ship bare — its first clause,
        # else its first 12 words marked as cut. Links and hashtags are never set.
        long = ("One two three four five six seven eight nine ten eleven twelve thirteen "
                "fourteen fifteen sixteen seventeen eighteen nineteen.")
        assert last_resort_hook(None, long) == ("One two three four five six seven eight nine "
                                                "ten eleven twelve…")
        assert last_resort_hook(None, "See https://x.co now.") == "See now."
        assert last_resort_hook(None, "#tag https://x.co") is None

    def test_a_post_whose_every_treatment_fails_ships_the_card(self, tmp_path):
        env = _Env(tmp_path)
        url, reason, receipt = env.post(
            _REPORT, **_force_chain("photo_only"),
            **{"cqc_lem.utilities.ai.image_brief.build_image_brief": {
                "side_effect": RuntimeError("author down")}})
        assert url and reason is None
        assert receipt["gate_verdict"] == "last_resort"
        assert receipt["render_path"] == "code_drawn_last_resort"
        assert receipt["rhythm"]["treatment"] == "typeset_card"
        assert receipt["rhythm"]["panel"] and receipt["hook_text"]
        assert any(f["treatment"] == "photo_only" for f in receipt["rhythm"]["fallbacks"])

    def test_a_judge_rejection_ships_the_card_not_nothing(self, tmp_path):
        rejected = image_gen.QualityVerdict(acceptable=False, rubric={"specificity": 2},
                                            failing=["specificity"], issues=["specificity 2/5"])
        env = _Env(tmp_path, verdict=rejected)
        url, _reason, receipt = env.post(_REPORT, **_force_chain("photo_only"))
        assert url and receipt["gate_verdict"] == "last_resort"

    def test_only_when_even_the_card_fails_does_the_post_go_bare(self, tmp_path):
        env = _Env(tmp_path)
        url, reason, _ = env.post(
            _REPORT, **_force_chain("photo_only"),
            **{"cqc_lem.utilities.ai.image_brief.build_image_brief": {
                "side_effect": RuntimeError("author down")},
               "cqc_lem.utilities.ai.image_graphics.render_typeset_card": {
                "side_effect": ig.GraphicLayoutError("does not fit")}})
        assert url is None and reason == "Could not write an image prompt"


class TestFinalHookGate:
    def test_the_showcase_hook_is_caught_deterministically(self):
        assert ic.dropped_preposition(BROKEN_HOOK, ROUTING_THESIS) == "based costs"

    def test_the_trim_no_longer_builds_it(self):
        hook = ic.clause_hook(ROUTING_THESIS)
        assert hook == "Routing prompts reduces AI costs"
        assert ic.dropped_preposition(hook, ROUTING_THESIS) == ""
        assert ic.fit_hook(BROKEN_HOOK, ROUTING_THESIS) == hook

    def test_compressing_a_nouns_own_modifier_is_allowed(self):
        assert ic.dropped_preposition("Routing prompts reduces AI costs", ROUTING_THESIS) == ""
        assert ic.dropped_preposition("", ROUTING_THESIS) == ""

    def _concept(self, hook, verified=""):
        return dataclasses.replace(_concept(_REPORT), thesis=ROUTING_THESIS, hook_phrase=hook,
                                   hook_verified=verified)

    def test_a_verified_hook_is_not_judged_again(self):
        concept = self._concept("Routing cuts AI costs", verified="Routing cuts AI costs")
        with patch.object(ic, "check_hook_against_thesis") as judge:
            assert ic._gate_final_hook(concept, {}, ROUTING_THESIS, None, "post_image", 1) \
                is concept
        judge.assert_not_called()

    def test_an_unverified_fallback_hook_is_judged(self):
        concept = self._concept("Routing cuts AI costs")
        with patch.object(ic, "check_hook_against_thesis", return_value=(True, "", "")) as judge:
            out = ic._gate_final_hook(concept, {}, ROUTING_THESIS, None, "post_image", 1)
        assert judge.call_count == 1 and out.hook_verified == "Routing cuts AI costs"

    def test_the_showcase_hook_is_regenerated(self):
        concept = self._concept(BROKEN_HOOK)
        redo = dataclasses.replace(concept, hook_phrase="Complexity routing cuts AI costs")
        with patch.object(ic, "_ensure_hook", return_value=redo) as ensure, \
                patch.object(ic, "check_hook_against_thesis", return_value=(True, "", "")):
            out = ic._gate_final_hook(concept, {}, ROUTING_THESIS, None, "post_image", 1)
        assert "based costs" in ensure.call_args.kwargs["extra_reasons"][0]
        assert out.hook_phrase == "Complexity routing cuts AI costs"
        assert out.hook_verified == out.hook_phrase

    def test_a_failed_regeneration_falls_back_to_the_clause(self):
        concept = self._concept(BROKEN_HOOK)
        redo = dataclasses.replace(concept, hook_phrase=BROKEN_HOOK)
        with patch.object(ic, "_ensure_hook", return_value=redo), \
                patch.object(ic, "check_hook_against_thesis", return_value=(True, "", "")):
            out = ic._gate_final_hook(concept, {}, ROUTING_THESIS, None, "post_image", 1)
        assert out.hook_phrase == "Routing prompts reduces AI costs"

    def test_with_nothing_grammatical_the_image_ships_without_a_headline(self):
        concept = self._concept("Routing cuts AI costs")
        with patch.object(ic, "_ensure_hook", return_value=concept), \
                patch.object(ic, "check_hook_against_thesis",
                             return_value=(False, "", "not grammatical")):
            out = ic._gate_final_hook(concept, {}, ROUTING_THESIS, None, "post_image", 1)
        assert out.hook_phrase == "" and out.hook_options is None

    def test_final_hook_runs_the_gate(self):
        concept = self._concept(BROKEN_HOOK)
        with patch.object(ic, "_gate_final_hook", side_effect=lambda c, *a: c) as gate:
            ic._final_hook(concept, {}, ROUTING_THESIS, None, "post_image", 1)
        assert gate.call_count == 1


class TestBrainCliche:
    @pytest.mark.parametrize("text", ["a row of human brains around an alarm clock",
                                      "a plastic brain model", "a brain on a desk"])
    def test_brains_are_stock_symbols(self, text):
        assert cliche_hit(text)

    def test_brainstorm_is_not(self):
        assert cliche_hit("a brainstorm on sticky notes") is None


class TestSettingRotation:
    @pytest.mark.parametrize("text,cls", [
        ("a warehouse aisle stacked with boxes", "warehouse"),
        ("at the kitchen table in her home office", "home_office"),
        ("a home office by a window", "home_office"),
        ("a busy coffee shop", "cafe"), ("a conference hallway", "conference"),
        ("the factory floor", "shop_floor"), ("an open-plan office", "office"),
        ("a billing review late at night", ""), ("", "")])
    def test_setting_classes(self, text, cls):
        assert pt.setting_class(text) == cls

    def test_two_warehouses_in_a_row_are_rerolled_toward_the_article(self):
        article = "We rebuilt the routing at our shop counter, then took it to a trade show."
        setting, rerolled = pt.reroll_setting("a warehouse floor", ["warehouse"], article)
        assert rerolled and pt.setting_class(setting) == "retail"

    def test_an_empty_setting_is_rerolled_too(self):
        setting, rerolled = pt.reroll_setting("", ["warehouse", None], "No places here.")
        assert rerolled and setting == pt.DEFAULT_SETTINGS["office"]

    def test_card_posts_between_ai_renders_do_not_reset_the_gate(self):
        setting, rerolled = pt.reroll_setting("a warehouse", [None, "warehouse"], "")
        assert rerolled and pt.setting_class(setting) != "warehouse"

    @pytest.mark.parametrize("setting,recent", [("a warehouse", ["office"]),
                                                ("a billing review", ["warehouse"]),
                                                ("a warehouse", [])])
    def test_a_fresh_setting_is_kept(self, setting, recent):
        assert pt.reroll_setting(setting, recent, "warehouse") == ("", False)

    def test_legacy_receipts_read_their_setting_off_the_brief(self):
        history = pt.rhythm_history([
            {"concept": {"setting": ""}, "prompt": "A candid photo in a warehouse with boxes",
             "hook_text": None},
            {"concept": {"setting": "a café"}, "hook_text": "Hook",
             "rhythm": {"treatment": "typeset_card"}},
            {"concept": {}, "rhythm": {"treatment": "data_card", "setting": None}},
        ])
        assert history["setting"] == ["warehouse", "cafe", None]

    def test_the_plan_rerolls_and_the_receipt_records_the_class(self, tmp_path):
        warehouse = dataclasses.replace(_concept(_REPORT), setting="a warehouse with boxes")
        env = _Env(tmp_path, analyze=lambda text, **_: warehouse)
        first = env.post(_REPORT, **_force_chain("photo_only"))[2]
        second = env.post(_REPORT + " We met at the coffee shop.",
                          **_force_chain("photo_only"))[2]
        assert first["rhythm"]["setting"] == "warehouse"
        assert "setting" in second["rhythm"]["rerolled"]
        assert second["rhythm"]["setting"] == "cafe"
        assert second["concept"]["setting"] == "coffee shop"
