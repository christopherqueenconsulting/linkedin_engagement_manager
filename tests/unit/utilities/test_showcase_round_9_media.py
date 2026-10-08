"""Showcase round 9, media half: covers tied to their article, legible recomposes, one card per line.

Pins (docs/image-stack.md, round 9): covers 17/19 — stock people on the same layout, the second one
missed because it was checked as the code-drawn cover it PLANNED; cover_18 — a recomposed cover on a
translucent strip; cover_20 — a long title set small; rhythm_2 — "FLEXIBLE INFRASTRUCTURE" over
"Flexible infra…"; slot_133 — "1" over ". Free Work…"; slot_123 — "90 GB file line by line" twice
on one card; decks 128/133/144 on one template.
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from cqc_lem.utilities import carousel_creator as crc, motion_design as md, newsletter_cover as nc
from cqc_lem.utilities.ai import image_brief as ib, image_compose as icomp, image_concept as ic, image_gen

pytestmark = pytest.mark.unit


def _concept(**kw) -> ic.ImageConcept:
    base = dict(thesis="Web search does not stop AI hallucinations", audience="content teams",
                specific_entities=("Decoder benchmark", "Claude Opus 4.5", "GPT-5.2",
                                   "procurement officer", "AI solution"),
                emotional_beat="concern", hook_phrase="", treatment="people_scene",
                treatment_rationale="x")
    base.update(kw)
    return ic.ImageConcept(**base)


# --- covers 17 and 19: a people scene must show its article ---------------------------------------

class TestArticleTerms:
    def test_names_numbers_and_scene_furniture_never_count(self):
        assert ib.article_scene_terms(_concept()) == ["benchmark", "procurement"]
        assert ib.article_scene_terms(None) == []

    def test_a_generic_office_prompt_fails_stage_3_on_a_cover(self):
        prompt = ("In a bright open-plan office, a woman in her 20s listens to a colleague, "
                  "soft window light.")
        why = ib.article_scene_failure(prompt, _concept(), "newsletter")
        assert why and "benchmark" in why and "cliché" in why
        ok, reason = ib.check_prompt_against_concept(prompt, _concept(), None, "newsletter")
        assert not ok and "benchmark" in reason

    def test_an_article_object_in_frame_passes(self):
        prompt = "A founder frowns at a printed benchmark chart pinned to the wall, no legible text."
        assert ib.article_scene_failure(prompt, _concept(), "newsletter") is None

    @pytest.mark.parametrize("surface,treatment", [("post_image", "people_scene"),
                                                   ("newsletter", "editorial_concept")])
    def test_only_people_covers_bind(self, surface, treatment):
        assert ib.article_scene_failure("Two people talk.", _concept(treatment=treatment),
                                        surface) is None

    def test_no_depictable_term_means_no_rule(self):
        concept = _concept(specific_entities=("Stanford HAI", "$12,000"))
        assert ib.article_scene_failure("Two people talk.", concept, "newsletter") is None

    def test_the_brief_author_is_told_to_retry(self):
        generic = json.dumps({"prompt": "Two professionals walk side by side down a bright "
                                        "conference hallway, warm daylight, gold scarf accent, "
                                        "shot on a 35mm lens with soft natural shadows and grain.",
                              "focal_concept": "two professionals in a hallway"})
        replies = [SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=generic), finish_reason="stop")])] * 4
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=replies), \
                patch.object(ib, "check_prompt_against_concept",
                             return_value=(True, "judge passed")):
            brief = ib.build_image_brief("Web search does not stop hallucinations.",
                                         surface="newsletter", ratio="16:9",
                                         concept=_concept(layout="split_left", weak=True))
        assert any("benchmark" in r for r in brief.rejections), brief.rejections


def _judge(tmp_path, blind, answer, concept, surface="newsletter"):
    img = tmp_path / "r.png"
    img.write_bytes(b"png")

    def resp(content):
        text = content if isinstance(content, str) else json.dumps(content)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    with patch.object(image_gen, "client") as client:
        client.chat.completions.create.side_effect = [resp(blind), resp(answer)]
        verdict = image_gen.inspect_render_quality(str(img), "focal", surface=surface,
                                                   concept=concept, hook_text="Search still lies")
    return verdict, client.chat.completions.create


_GOOD = {"specificity": 5, "no_cliche": 5, "thumbnail_read": 5, "craft": 5, "scroll_stop": 5,
         "brand_fit": 5}


class TestStage4GenericPeople:
    def test_stock_people_with_nothing_from_the_article_fail(self, tmp_path):
        blind = "A man and a woman in business attire talk in a bright office. No visible text."
        answer = {"entities_depicted": {"benchmark": False}, "thesis_inferable": True,
                  "rubric": _GOOD, "issues": []}
        verdict, create = _judge(tmp_path, blind, answer, _concept(archetype="people_scene"))
        assert not verdict.acceptable
        assert verdict.rubric["no_cliche"] == 2 and verdict.rubric["specificity"] == 3
        assert any(i.startswith(image_gen.GENERIC_PEOPLE_ISSUE) for i in verdict.issues)
        # The judge is asked about the article's own terms.
        assert "benchmark" in create.call_args_list[1][1]["messages"][0]["content"][0]["text"]

    def test_an_article_term_seen_passes(self, tmp_path):
        blind = "A woman frowns at a benchmark chart on a monitor. No visible text."
        answer = {"entities_depicted": {}, "rubric": _GOOD, "issues": []}
        verdict, _ = _judge(tmp_path, blind, answer, _concept(archetype="people_scene"))
        assert verdict.acceptable

    def test_a_gist_that_does_not_read_fails_a_people_cover(self):
        answer = {"thesis_inferable": False}
        assert image_gen.generic_people_scene(answer, "benchmark", ["benchmark"])
        assert not image_gen.generic_people_scene(answer, "", [])

    def test_a_post_image_or_an_object_cover_is_untouched(self, tmp_path):
        blind = "Two people talk in an office. No visible text."
        answer = {"entities_depicted": {}, "rubric": _GOOD, "issues": []}
        verdict, _ = _judge(tmp_path, blind, answer, _concept(archetype="people_scene"),
                            surface="post_image")
        assert verdict.acceptable
        assert not image_gen._is_people_scene(_concept(archetype="editorial_concept"))
        assert image_gen._is_people_scene(_concept(archetype=""))


# --- covers 17/19: layout freshness over every family the cover can ship as -----------------------

class TestCoverLayoutWindow:
    def test_a_drawn_plan_that_falls_back_to_a_render_is_checked_as_both(self):
        # cover_19: planned checklist (drawn) -> people_scene (render) on cover_17's split_right.
        concept = _concept(layout="split_right", archetype="checklist",
                           archetype_ranking=("checklist", "people_scene"))
        recent = [("full_bleed", "render"), ("split_right", "render"), ("split_left", "drawn")]
        assert nc._families_for(concept) == {"drawn", "render"}
        assert nc.fresh_cover_layout(concept, recent).layout == "split_left"

    def test_the_previous_covers_layout_never_repeats(self):
        concept = _concept(layout="split_left", archetype="stat_card")
        got = nc.fresh_cover_layout(concept, [("split_left", "render")])
        assert got.layout != "split_left"

    def test_a_third_split_in_a_row_weighs(self):
        assert nc._layout_penalty("split_left", {"render"},
                                  [("split_right", "drawn"), ("split_left", "drawn")]) == 1

    def test_the_shipped_layout_is_read_before_the_concepts(self):
        receipts = [{"cover_layout": "split_left",
                     "concept": {"layout": "full_bleed", "archetype": "editorial_concept"}}]
        with patch.object(nc, "_recent_cover_receipts", return_value=receipts):
            assert nc.recent_cover_pairs(1, 4) == [("split_left", "render")]


# --- cover_18: a recompose is set on a solid panel -------------------------------------------------

class TestRecomposeOnASolidPanel:
    def test_a_full_bleed_concept_recomposes_on_a_split(self, tmp_path):
        raw = tmp_path / "raw.png"
        Image.new("RGB", (1024, 1024), (245, 245, 240)).save(raw)
        composed = tmp_path / "composed.png"
        Image.new("RGB", (1024, 1024)).save(composed)
        info = {"raw_render_path": str(raw)}
        out = nc.ensure_cover_ratio(str(composed), "53.7% of LinkedIn posts miss their mark",
                                    info, concept=_concept(layout="full_bleed", kicker="AI"),
                                    brand="", byline="Christopher Queen")
        assert nc.is_cover_ratio(out) and info["cover_layout"] == "split_left"
        # The type panel's corner is the solid charcoal, never a translucent strip.
        assert Image.open(out).convert("RGB").getpixel((5, 5)) == (0x1F, 0x1F, 0x1F)

    def test_solid_layout(self):
        assert nc.solid_cover_layout("split_right") == "split_right"
        assert nc.solid_cover_layout("full_bleed") == icomp.OVERLAY_FALLBACK_LAYOUT
        assert nc.solid_cover_layout(None) == icomp.OVERLAY_FALLBACK_LAYOUT

    def test_overlay_contrast(self):
        light = Image.new("RGB", (100, 100), (250, 250, 250))
        dark = Image.new("RGB", (100, 100), (20, 20, 20))
        box = icomp.Box(0, 0, 100, 100)
        assert icomp.overlay_contrast(light, box, "#E9D437") < icomp.MIN_HEADLINE_CONTRAST
        assert icomp.overlay_contrast(dark, box, "#E9D437") >= icomp.MIN_HEADLINE_CONTRAST
        assert icomp.overlay_contrast(dark, icomp.Box(0, 0, 0, 0), "#E9D437") == 21.0


# --- cover_20: a long title on a tall panel --------------------------------------------------------

_TITLE_20 = "OBSERVE-Loop: A Practical Guide to Real-Time AI Monitoring on LinkedIn"


class TestHeadlineSize:
    def test_a_tall_split_panel_takes_more_lines(self):
        parts = icomp.headline_parts(_TITLE_20, "AI OBSERVABILITY", "Christopher Queen")
        fit = icomp.fit_cover((1920, 1080), "split_left", parts)
        assert len(fit.lines) > icomp.MAX_LINES
        assert fit.cap >= round(1080 * icomp.HEADLINE_MIN_CAP_FRACTION)

    def test_below_the_hard_floor_the_title_is_cut_to_its_claim(self):
        parts = icomp.headline_parts(_TITLE_20, "AI OBSERVABILITY", "Christopher Queen")
        with patch.object(icomp, "HEADLINE_MIN_CAP_FRACTION", 0.2):
            fit = icomp.fit_cover((1920, 1080), "split_left", parts)
        assert "OBSERVE" not in " ".join(fit.lines)

    @pytest.mark.parametrize("text,out", [
        (_TITLE_20, "A practical guide to real-time AI monitoring on LinkedIn"),
        ("Cash flow - why owners miss it every month", "Why owners miss it every month"),
        ("Pricing: why", ""),
        ("No clause break here", ""),
    ])
    def test_shorter_headline(self, text, out):
        got = icomp.shorter_headline(text)
        assert got.lower() == out.lower() if out else got == ""


# --- rhythm_2 and slot_133: kicker stems and list markers ------------------------------------------

class TestKickerAndMarkers:
    @pytest.mark.parametrize("kicker,headline", [
        ("FLEXIBLE INFRASTRUCTURE", "Flexible infra beats policy"),
        ("AI COSTS", "AI cost routing pays off"),
    ])
    def test_a_kicker_that_stems_the_headline_repeats_it(self, kicker, headline):
        assert icomp.kicker_repeats_headline(kicker, headline)

    def test_a_kicker_with_its_own_word_stays(self):
        assert not icomp.kicker_repeats_headline("PEAK CHECKOUT", "Flexible infra beats policy")
        assert not icomp.kicker_repeats_headline("AI", "Aim higher")  # under the stem floor

    def test_a_leading_list_marker_is_never_a_hero(self):
        assert icomp.split_hero("1. Free Work Can Prolong Monthly Bills") == \
            ("", icomp.sentence_case("Free Work Can Prolong Monthly Bills"))
        assert icomp.split_hero("45% less reach on AI posts")[0] == "45%"

    def test_a_leading_numeral_is_bound_to_its_word(self):
        assert icomp.bind_leading_marker(["1.", "Free", "work"]) == ["1. Free", "work"]
        assert icomp.bind_leading_marker(["Free", "work"]) == ["Free", "work"]

    def test_the_deck_cover_drops_its_marker(self, tmp_path, monkeypatch):
        from PIL import ImageDraw

        monkeypatch.setenv("DECK_AI_COVER_ENABLED", "false")
        drawn: list = []
        real = ImageDraw.ImageDraw.text

        def spy(self, xy, text, *a, **k):
            drawn.append(str(text))
            return real(self, xy, text, *a, **k)

        deck = crc.EducationalContentCarousel(**{
            "cover": {"title": "1. Free Work Can Prolong Monthly Bills", "content": "Three checks."},
            "contents": [{"title": "Ask for a date", "content": "Tie it to something usable."},
                         {"title": "Agree on updates", "content": "Say when you will hear."}],
            "call_to_action": {"title": "Your turn", "content": "Which check is hardest?"}})
        with patch.object(ImageDraw.ImageDraw, "text", spy), \
                patch.object(crc, "retain_carousel_keyframes"):
            crc.create_carousel_slide_images(deck, post_id=133, output_dir=str(tmp_path / "d"),
                                             template="bold_listicle", user_id=1,
                                             cover_treatment=crc.DECK_COVER_POSTER)
        assert not any(t.strip().startswith(("1.", ". ")) for t in drawn)


# --- slot_123: one line printed twice on a card ----------------------------------------------------

class TestStatCounterNeverRepeatsTheHook:
    _HOOK = "Last August 9 2026, I was stuck in a script trying to read a 90 GB file line by line."

    def test_a_stat_the_hook_states_is_refused(self):
        assert md.stat_repeats_hook(self._HOOK, {"display": "90", "label": "GB file line by line"})
        assert md.stat_repeats_hook("A line by line read", {"label": "line by line"})
        assert not md.stat_repeats_hook("Tests now pass", {"display": "14", "label": "tests"})
        assert not md.stat_repeats_hook(self._HOOK, {})

    def test_plan_data_falls_to_another_style(self):
        graphic = {"stat": {"display": "90", "label": "GB file line by line", "value": "90",
                            "unit": "", "source_sentence": "x"}}
        with patch("cqc_lem.utilities.ai.image_graphics.assert_traceable"), \
                pytest.raises(ValueError, match="twice"):
            md.plan_data(md.STYLE_STAT_COUNTER, graphic, self._HOOK, size=(640, 640),
                         palette=None)


# --- decks 128/133/144: rotate the stage's template ------------------------------------------------

class TestDeckTemplateRotation:
    def test_a_template_one_of_the_last_two_decks_used_rotates(self):
        assert crc.rotate_deck_template("stat_reveal", ["stat_reveal"]) != "stat_reveal"
        assert crc.rotate_deck_template("stat_reveal", ["bold_listicle", "stat_reveal"]) != \
            "stat_reveal"

    def test_any_three_decks_in_a_row_are_three_templates(self):
        recent: list = []
        for _ in range(6):
            recent.insert(0, crc.rotate_deck_template("stat_reveal", recent))
        assert all(len(set(recent[i:i + 3])) == 3 for i in range(4))

    def test_a_fresh_template_and_a_full_block_keep_it(self):
        assert crc.rotate_deck_template("stat_reveal", ["bold_listicle", "story_arc",
                                                        "stat_reveal"]) == "stat_reveal"
        with patch.dict(crc.CAROUSEL_TEMPLATES, clear=True, values={"stat_reveal": {}}):
            assert crc.rotate_deck_template("stat_reveal", ["stat_reveal"]) == "stat_reveal"

    def test_the_renderer_rotates_off_a_recent_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECK_AI_COVER_ENABLED", "false")
        root = tmp_path / "carousel"
        old = root / "1"
        old.mkdir(parents=True)
        with open(crc.deck_render_receipt_path(str(old)), "w") as fh:
            json.dump({"user_id": 7, "template": "stat_reveal"}, fh)
        deck = crc.EducationalContentCarousel(**{
            "cover": {"title": "Three checks before you pay", "content": "From one mistake."},
            "contents": [{"title": "Ask for a date", "content": "Tie it to something usable."},
                         {"title": "Agree on updates", "content": "Say when you will hear."}],
            "call_to_action": {"title": "Your turn", "content": "Which check is hardest?"}})
        out = root / "2"
        with patch.object(crc, "retain_carousel_keyframes"):
            crc.create_carousel_slide_images(deck, post_id=2, output_dir=str(out),
                                             template="stat_reveal", user_id=7,
                                             rotate_template=True)
        with open(crc.deck_render_receipt_path(str(out))) as fh:
            assert json.load(fh)["template"] != "stat_reveal"
        assert os.path.isdir(out)
