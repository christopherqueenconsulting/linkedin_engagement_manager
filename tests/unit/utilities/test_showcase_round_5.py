"""Showcase round 5: number fidelity, deck counters, the badge rotation and the setting gate.

Every case is a defect the round-5 critic found in the real run (``gauntlet/out/showE``): ed18's
cover drew 45% under a "53.7%" title, ed16's flipped "squandered" into "saved", slot_144's slide
card drew "63 recipients were execs" beside a post about 51 emails, rhythm_6 printed "85 %",
decks counted "1/3" inside a "1/5" cover and promised "4 slides" over six, two consecutive decks
both drew the numbered circle, and rhythm_3/rhythm_4 were warehouse scenes back to back.
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")

from cqc_lem.utilities import carousel_creator as cc  # noqa: E402
from cqc_lem.utilities.ai import (
    image_concept as ic,  # noqa: E402
    image_graphics as ig,  # noqa: E402
    post_treatment as pt,  # noqa: E402
)

pytestmark = pytest.mark.unit

_CREATE = "cqc_lem.utilities.ai.client.client.chat.completions.create"

ED18_TITLE = "53.7% of LinkedIn Posts Miss Their Mark"
ED18_BODY = ("Originality.ai looked at 3,000 posts. 53.7% of LinkedIn posts miss their mark "
             "because they read as machine-written. Yet they got about 45% less engagement than "
             "human-written content.")
ED16_TITLE = "The $30K We Nearly Squandered"
ED16_BODY = "We saved $30K per quarter, lead quality improved, and the content felt more human."


def _concept(**kw) -> ic.ImageConcept:
    base = dict(thesis="AI posts underperform", audience="", specific_entities=(),
                emotional_beat="", hook_phrase="", treatment="", treatment_rationale="")
    base.update(kw)
    return ic.ImageConcept(**base)


def _src(title: str, body: str) -> str:
    return f"{title}\n\n{body}"


# ── 1. The cover's number IS the title's ─────────────────────────────────────────────────────

class TestTitleClaim:
    def test_ed18_title(self):
        assert ic.title_claim(ED18_TITLE, _src(ED18_TITLE, ED18_BODY)) == (
            "53.7%", "of LinkedIn posts miss their mark",
            "53.7% of LinkedIn posts miss their mark")

    def test_ed16_title_keeps_squandered(self):
        assert ic.title_claim(ED16_TITLE, _src(ED16_TITLE, ED16_BODY)) == (
            "$30K", "we nearly squandered", "The $30K we nearly squandered")

    def test_a_trailing_figure_takes_the_words_before_it(self):
        figure, claim, _ = ic.title_claim("Why our audit cost $30K", "")
        assert (figure, claim) == ("$30K", "audit cost")

    def test_no_figure_no_claim(self):
        assert ic.title_claim("Why audits fail", "body") == ("", "", "")
        assert ic.title_claim(None) == ("", "", "")

    def test_a_long_title_gives_no_title_hook(self):
        title = "How we finally cut 60% of our AI spend in three hard months"
        figure, claim, hook = ic.title_claim(title, title)
        assert figure == "60%" and claim and hook == ""

    def test_a_proper_noun_the_body_capitalises_keeps_its_case(self):
        title = "40% of Acme Buyers Churn"
        _, claim, _ = ic.title_claim(title, _src(title, "We asked why Acme lost them."))
        assert claim == "of Acme buyers churn"


class TestApplyTitleStat:
    def test_ed18_cover_draws_the_title_figure_not_45(self):
        concept = _concept(hook_phrase="Polished posts, dropped interaction",
                           graphic={"source_line": "Source: Originality.ai",
                                    "stat": {"display": "45%", "label": "engagement"}})
        out = ic.apply_title_stat(concept, ED18_TITLE, _src(ED18_TITLE, ED18_BODY))
        stat = out.graphic["stat"]
        assert (stat["display"], stat["label"]) == ("53.7%", "of LinkedIn posts miss their mark")
        assert out.hook_phrase == "53.7% of LinkedIn posts miss their mark"
        # The 53.7% sentence names no source, so the old line (cited for the 45%) goes with it.
        assert out.graphic["source_line"] == ig.NO_SOURCE_LINE
        ig.assert_traceable(ig.STAT_CARD, out.graphic)  # the last gate before ink still holds

    def test_ed16_hook_never_flips_squandered_into_saved(self):
        concept = _concept(hook_phrase="$30K saved per quarter", graphic=None)
        out = ic.apply_title_stat(concept, ED16_TITLE, _src(ED16_TITLE, ED16_BODY))
        assert out.hook_phrase == "The $30K we nearly squandered"
        assert out.hook_verified == out.hook_phrase and out.hook_shape
        assert out.graphic["stat"]["label"] == "we nearly squandered"
        ig.assert_traceable(ig.STAT_CARD, out.graphic)

    def test_a_source_that_cites_the_title_figure_is_kept(self):
        body = "Per Originality.ai, 53.7% of LinkedIn posts miss their mark."
        concept = _concept(graphic={"source_line": "Source: Originality.ai",
                                    "stat": {"display": "45%"}})
        out = ic.apply_title_stat(concept, ED18_TITLE, _src(ED18_TITLE, body))
        assert out.graphic["source_line"] == "Source: Originality.ai"

    @pytest.mark.parametrize("hook,kept", [
        ("Routing beats one big model", True),          # no number: the stat card carries it
        ("60% of AI spend cut by routing", True),       # the title's figure, the same claim
        ("45% fewer hours on routing", False),          # a different number
        ("60% of budget wasted", False),                # the title's figure, a flipped claim
    ])
    def test_a_long_title_keeps_only_a_faithful_hook(self, hook, kept):
        title = "How we finally cut 60% of our AI spend in three hard months"
        body = "We cut 60% of our AI spend by routing. Hours fell 45% too."
        out = ic.apply_title_stat(_concept(hook_phrase=hook), title, _src(title, body))
        assert out.hook_phrase == (hook if kept else "")

    def test_a_title_without_a_figure_changes_nothing(self):
        concept = _concept(hook_phrase="45% less engagement", graphic={"stat": {"display": "45%"}})
        assert ic.apply_title_stat(concept, "Why AI posts flop", "x") is concept

    def test_an_untraceable_title_stat_draws_no_stat_card(self):
        concept = _concept(graphic={"stat": {"display": "45%"}, "source_line": ""})
        with patch("cqc_lem.utilities.ai.image_graphics.validate_fact",
                   return_value=(None, "nope")):
            out = ic.apply_title_stat(concept, ED18_TITLE, _src(ED18_TITLE, ED18_BODY))
        assert "stat" not in out.graphic

    def test_analysis_wires_the_title_rule_last(self):
        payload = {"thesis": "Machine-written LinkedIn posts miss their mark",
                   "audience": "owners", "specific_entities": ["53.7%", "45%"],
                   "visual_anchors": ["a comment section", "a professional profile"],
                   "emotional_beat": "uneasy realization",
                   "hook_phrase": "45% less engagement", "treatment": "people_scene",
                   "treatment_rationale": "x",
                   "graphic_facts": {"thesis_stat": {
                       "label": "less engagement than human-written content", "value": "45",
                       "unit": "%", "source_sentence": "Yet they got about 45% less engagement "
                                                       "than human-written content."}}}
        reply = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop")])
        with patch(_CREATE, return_value=reply):
            concept = ic.analyze_content_for_image(ED18_BODY, title=ED18_TITLE,
                                                   surface="newsletter")
        assert concept.graphic["stat"]["display"] == "53.7%"
        assert "45%" not in concept.hook_phrase


# ── 2. Number + unit typography, and a slide's figure must be the post's ─────────────────────

class TestTidyFigures:
    @pytest.mark.parametrize("raw,want", [
        ("Routing helps cost drop 85 %", "Routing helps cost drop 85%"),
        ("cost drop 85 %", "cost drop 85%"),
        ("$ 30K saved", "$30K saved"),
        ("30 K saved", "30K saved"),
        ("3 B2B teams", "3 B2B teams"),
        ("a b", "a b"),
        (None, ""),
    ])
    def test_tidy(self, raw, want):
        assert ic.tidy_figures(raw) == want

    def test_the_final_hook_restore_tidies(self):
        assert ic.restore_number_casing("Cost drop 85 %", "a 85 % cost drop") == \
            "Cost drop 85%"

    def test_every_drawn_headline_is_tidied(self):
        from cqc_lem.utilities.ai.image_compose import headline_parts

        assert headline_parts("Cost drop 85 %", hero=False).rest == "Cost drop 85%"
        card = ig.render_typeset_card("Cost drop 85 % with routing", kicker="AI COSTS")
        try:
            assert any("85%" in (p.text or "") for p in card.placements)
            assert not any("85 %" in (p.text or "") for p in card.placements)
        finally:
            os.remove(card.path)


POST_144 = ("I sent 51 cold emails, and not a single reply.\n\n"
            "Every recipient was a high-level executive, not the hiring manager.\n"
            "Additionally, 18 emails were dispatched in just 23 seconds.\n"
            "Spend fell from $900 a month to $300 a month.")


class TestSlideFiguresNeedThePost:
    def test_slot_144_card_number_the_post_never_states_is_refused(self):
        body = "63 recipients were execs, not hiring managers."
        assert ig.slide_graphic("Targeting the Wrong Audience", body) is not None  # slide-only
        assert ig.slide_graphic("Targeting the Wrong Audience", body, POST_144) is None

    def test_a_number_beside_another_claims_label_is_refused(self):
        # 51 is in the post, but "were execs" is a different sentence's claim.
        body = "51 recipients were execs, not hiring managers."
        assert ig.slide_graphic("Wrong audience", body, POST_144) is None

    def test_a_number_and_label_from_one_post_sentence_draws(self):
        picked = ig.slide_graphic("Too fast", "18 emails were dispatched in just 23 seconds.",
                                  POST_144)
        assert picked and picked[0] == ig.STAT_CARD
        assert picked[1]["stat"]["display"] == "18"

    def test_before_after_halves_must_both_be_the_posts(self):
        body = "Spend fell from $900 a month to $300 a month."
        assert ig.slide_graphic("Spend", body, POST_144)[0] == ig.BEFORE_AFTER
        # $200 is not the post's: no from->to pair; only the half the post states may draw.
        archetype, graphic = ig.slide_graphic(
            "Spend", "Spend fell from $900 a month to $200 a month.", POST_144)
        assert archetype == ig.STAT_CARD and graphic["stat"]["display"] == "$900"

    def test_no_evidence_keeps_the_slide_only_rule(self):
        assert ig.figure_in_evidence({"value": "63", "unit": "", "label": "x"}, None)


# ── 3. Deck counters, slide-count copy, the badge rotation ───────────────────────────────────

class TestDeckCopy:
    def test_counter(self):
        assert cc.slide_counter(2, 6) == "2 / 6"

    @pytest.mark.parametrize("raw,total,want", [
        ("Why sharing flaws builds trust. 4 slides.", 6, "Why sharing flaws builds trust. 6 slides."),
        ("Four slides on trust", 6, "Six slides on trust"),
        ("a 3-slide guide", 5, "a 5-slides guide"),
        ("2 pages", 1, "1 page"),
        ("Swipe through 3 slides", 14, "Swipe through 14 slides"),
        ("Nothing to count", 6, "Nothing to count"),
        (None, 6, ""),
    ])
    def test_slide_count_copy_is_the_final_count(self, raw, total, want):
        assert cc.fix_slide_count_copy(raw, total) == want

    def test_the_badge_retires_after_a_listicle_drew_it(self):
        assert cc.deck_badge("bold_listicle", []) == cc.BADGE_NUMBERED_CIRCLE
        assert cc.deck_badge("bold_listicle", [cc.BADGE_NUMBERED_CIRCLE]) == cc.BADGE_RULE
        assert cc.deck_badge("bold_listicle", [None, cc.BADGE_RULE]) == cc.BADGE_NUMBERED_CIRCLE
        assert cc.deck_badge("stat_reveal", []) == cc.BADGE_RULE

    def test_a_legacy_listicle_receipt_reads_as_the_circle(self, tmp_path):
        deck = tmp_path / "d1"
        deck.mkdir()
        (deck / "deck_render.json").write_text(json.dumps(
            {"user_id": 3, "template": "bold_listicle", "slides": []}))
        assert cc.recent_deck_choices(str(tmp_path), 3)["badge"] == [cc.BADGE_NUMBERED_CIRCLE]


def _deck():
    return cc.EducationalContentCarousel(**{
        "cover": {"title": "AI Outreach: A Case Snapshot",
                  "content": "51 emails, 0 replies. 4 slides."},
        "contents": [
            {"title": "Targeting the Wrong Audience",
             "content": "63 recipients were execs, not hiring managers."},
            {"title": "Too Fast", "content": "18 emails were dispatched in just 23 seconds."},
            {"title": "Stale Posts", "content": "Many job posts were over a year old."},
            {"title": "Mismatched Offer", "content": "The offer did not match the website."},
        ],
        "call_to_action": {"title": "Your turn", "content": "Which would you fix first?"},
    })


def _render_recording(tmp_path, template, **kw):
    """Render a deck and return (paths, every string drawn, the receipt)."""
    from PIL import ImageDraw

    drawn: list = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        drawn.append(str(text))
        return real(self, xy, text, *args, **kwargs)

    out = tmp_path / template
    with patch.object(ImageDraw.ImageDraw, "text", spy), \
            patch.object(cc, "retain_carousel_keyframes"):
        paths = cc.create_carousel_slide_images(_deck(), post_id=144, output_dir=str(out),
                                                template=template, user_id=1, **kw)
    with open(cc.deck_render_receipt_path(str(out))) as fh:
        return paths, drawn, json.load(fh)


class TestEveryTemplateCountsTheFinalDeck:
    @pytest.mark.parametrize("template", list(cc.CAROUSEL_TEMPLATES))
    def test_counters_and_copy(self, tmp_path, monkeypatch, template):
        import re

        monkeypatch.setenv("DECK_AI_COVER_ENABLED", "false")
        paths, drawn, receipt = _render_recording(tmp_path, template, evidence=POST_144)
        total = len(paths)
        assert total == 6
        counters = [m for t in drawn for m in re.findall(r"(\d+) (?:/|of) (\d+)", t)]
        assert counters, "every template prints a page counter somewhere"
        assert all(int(den) == total for _num, den in counters), counters
        assert not any(re.search(r"#\d", t) for t in drawn)  # no "#1" strip label
        assert not any("4 slides" in t for t in drawn)
        assert any("6 slides" in t for t in drawn)
        # The 63-execs slide draws no card: the post never states it.
        assert receipt["slides"][1]["element"] is None

    def test_two_listicles_in_a_row_do_not_both_draw_the_circle(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECK_AI_COVER_ENABLED", "false")
        badges = []
        for n in range(2):
            out = tmp_path / f"deck{n}"
            with patch.object(cc, "retain_carousel_keyframes"):
                cc.create_carousel_slide_images(_deck(), post_id=n, output_dir=str(out),
                                                template="bold_listicle", user_id=9)
            receipt_path = cc.deck_render_receipt_path(str(out))
            os.utime(receipt_path, (1000 + n, 1000 + n))
            with open(receipt_path) as fh:
                badges.append(json.load(fh)["badge"])
        assert badges == [cc.BADGE_NUMBERED_CIRCLE, cc.BADGE_RULE]


# ── 5. The setting gate across processes ─────────────────────────────────────────────────────

class TestSettingGate:
    @pytest.mark.parametrize("text", [
        "A bright e-commerce fulfillment center where a woman checks a shelf",
        "A fulfilment centre sorting area", "two people on a loading dock",
        "a distribution centre at dawn", "stacked pallets in a depot",
        "between tall warehouses", "a logistics hub at night",
    ])
    def test_warehouse_variants_classify(self, text):
        assert pt.setting_class(text) == "warehouse"

    def test_a_logistics_manager_in_an_office_is_an_office(self):
        assert pt.setting_class("A logistics manager in a bright open-plan office") == "office"

    def test_the_cap_blocks_a_class_used_twice_in_six(self):
        assert pt.blocked_settings([]) == frozenset()
        assert pt.blocked_settings(["office", None, "warehouse", "office"]) == {"office"}
        assert pt.blocked_settings(["cafe", "warehouse", None, "warehouse"]) == {
            "cafe", "warehouse"}
        old = ["cafe", "office", "outdoors", "retail", "conference", "home_office", "warehouse",
               "warehouse"]
        assert pt.blocked_settings(old) == {"cafe"}  # the pair fell out of the window

    def test_the_cap_rerolls_a_setting_that_is_not_the_last(self):
        setting, rerolled = pt.reroll_setting("a warehouse aisle", ["cafe", "warehouse",
                                                                    "warehouse"], "In an office.")
        assert rerolled and pt.setting_class(setting) == "office"

    def test_rhythm_3_and_4_receipts_reproduce_and_are_fixed(self):
        # The real receipts' shapes: rhythm_3 recorded setting null (no keyword matched
        # "fulfillment center"), rhythm_4 recorded warehouse.
        r4 = {"rhythm": {"treatment": "photo_only", "setting": "warehouse", "style": None},
              "prompt": "A bright e-commerce fulfillment center sorting area.", "concept": {}}
        r3 = {"rhythm": {"treatment": "photo_only", "setting": None, "style": None},
              "prompt": "A bright e-commerce fulfillment center where a woman looks at a shelf.",
              "concept": None}
        history = pt.rhythm_history([r4, r3])
        assert history["setting"] == ["warehouse", "warehouse"]
        rhythm = pt.plan_post_rhythm(None, "A post about scaling a small online shop.",
                                     history, 0.4, ("charcoal",))
        assert rhythm.setting and pt.setting_class(rhythm.setting) != "warehouse"
        assert "setting" in rhythm.rerolled

    def test_a_code_drawn_card_receipt_is_never_read_as_a_setting(self):
        receipt = {"rhythm": {"treatment": "typeset_card", "setting": None,
                              "style": pt.STYLE_CODE_DRAWN},
                   "prompt": "A still life in an office", "concept": {}}
        assert pt.rhythm_history([receipt])["setting"] == [None]

    def test_no_concept_still_briefs_the_rerolled_setting(self):
        from cqc_lem.utilities import post_image as pi

        rhythm = pt.plan_post_rhythm(None, "In a café we sketched the plan.",
                                     {"setting": ["warehouse"]}, 0.4, ("charcoal",))
        captured = {}

        def brief(*_a, **kw):
            captured.update(kw)
            raise RuntimeError("stop here")

        with patch("cqc_lem.utilities.ai.image_brief.build_image_brief", side_effect=brief), \
                patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for_concept",
                      return_value=None):
            out = pi._render_ai_post(None, "In a café we sketched the plan.", rhythm,
                                     "photo_only", user_id=1, post_id=2, profile=None,
                                     brand="", ratio="1:1")
        assert out.reason == "Could not write an image prompt"
        assert f"Setting for this image: {rhythm.setting}." in captured["extra_direction"]

    def test_the_receipt_reads_the_setting_off_the_prompt_first(self):
        from cqc_lem.utilities import post_image as pi

        concept = _concept(setting="a B2B project meeting room", treatment="people_scene")
        brief = SimpleNamespace(prompt="Inside a fulfillment center, two people talk.",
                                concept=concept, hook_text=None)
        done = pi._Rendered(path="x", brief=brief)
        rhythm = pt.plan_post_rhythm(concept, "text", {}, 0.4, ("charcoal",))
        receipt = pi._rhythm_receipt("photo_only", rhythm, done, 0.4, [])
        assert receipt["setting"] == "warehouse"
