"""Engine rules from the #2241 showcase run: covers, video frames and story rotation.

The sentences below are verbatim from user 1's real editions in that run — the ones whose stat or
steps the graphic validator refused, so every cover fell to a people or editorial render.
"""

import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import image_brief as ib, image_gen as ig_gen, image_graphics as ig, story_bank as sb
from cqc_lem.utilities.ai.image_concept import (
    TREATMENT_EDITORIAL,
    ImageConcept,
    rank_archetypes,
)

pytestmark = pytest.mark.unit

ED16 = ("The numbers changed quickly. We saved $30K per quarter, lead quality improved, and the "
        "content felt more human.")
ED16_SENTENCE = "We saved $30K per quarter, lead quality improved, and the content felt more human."
ED18 = ("53.7% of long-form LinkedIn posts in 2025 were probably AI-generated. Yet they got about "
        "45% less engagement than human-written content.")


class TestGraphicFactValidation:
    @pytest.mark.parametrize("value,unit", [("30K", "$"), ("$30K", ""), ("$30K", "$"),
                                            ("30", "$")])
    def test_ed16s_thirty_k_validates_however_stage_1_wrote_it(self, value, unit):
        fact, reason = ig.validate_fact({"label": "per quarter", "value": value, "unit": unit,
                                         "source_sentence": ED16_SENTENCE}, ED16)
        assert reason == "" and fact["display"] == "$30K" and fact["kind"] == "currency:$"
        assert ig.assert_traceable("stat_card", {"stat": fact}) == [fact]

    @pytest.mark.parametrize("value,unit", [("30M", "$"), ("30K", "%"), ("€30K", "$"),
                                            ("90K", "$")])
    def test_a_different_multiplier_unit_or_number_is_still_refused(self, value, unit):
        assert ig.verbatim_figure(value, unit, ED16_SENTENCE) is None

    def test_a_plain_count_with_a_multiplier_must_match_it(self):
        assert ig.verbatim_figure("12K", "", "We sent 12K invites last year.") == "12K"
        assert ig.verbatim_figure("12M", "", "We sent 12K invites last year.") is None
        assert ig.verbatim_figure("12K", "invites", "We sent 12K invites in all.") == "12K invites"
        assert ig.verbatim_figure("12M", "invites", "We sent 12K invites in all.") is None

    def test_split_value_leaves_what_it_cannot_read(self):
        assert ig.split_value("$30K", "") == ("30", "$", "K")
        assert ig.split_value("45%", "") == ("45", "%", "")
        assert ig.split_value("$30K", "€") == ("$30K", "€", "")
        assert ig.split_value("about 30", "") == ("about 30", "", "")

    def test_ed18s_non_breaking_hyphen_sentence_is_found(self):
        # Stage 1 copied "human-written" back with U+2011; the article has a plain hyphen.
        quoted = "Yet they got about 45% less engagement than human‑written content."
        fact, reason = ig.validate_fact({"label": "less engagement", "value": "45", "unit": "%",
                                         "source_sentence": quoted}, ED18)
        assert reason == "" and fact["display"] == "45%"

    def test_markdown_emphasis_is_ignored_on_either_side(self):
        source = "Yet they got about **45%** less engagement than human-written content."
        assert ig.sentence_in_source(
            "Yet they got about 45% less engagement than human-written content.", source)

    @pytest.mark.parametrize("step,sentence,expected", [
        ("Let AI handle the grunt work of research, outlining and basic editing",
         "• Let AI handle the grunt work of research, outlining and basic editing.",
         "Let AI handle the grunt work of research"),
        ("Sprinkle your posts with personal insights and real-world examples",
         "• Sprinkle your posts with personal insights and real-world examples.",
         "Sprinkle your posts with personal insights"),
        ("Keep a balance; AI can speed things up", "• Keep a balance.", "Keep a balance"),
        ("Ground: start each post with a source paragraph",
         "Start each post with a source paragraph you trust.",
         "start each post with a source paragraph"),
        ("Invent a brand new step nobody wrote", "Keep a balance.", ""),
    ])
    def test_a_long_step_keeps_its_first_grounded_clause(self, step, sentence, expected):
        assert ig.fit_step(step, sentence) == expected

    def test_ed2s_four_steps_now_make_a_checklist(self):
        body = ("• Let AI handle the grunt work of research, outlining and basic editing.\n"
                "• Sprinkle your posts with personal insights and real-world examples.\n"
                "• Talk to your audience like a person.\n• Keep a balance.")
        steps = [{"text": line.lstrip("• ").rstrip("."), "source_sentence": line}
                 for line in body.split("\n")]
        graphic = ig.validate_graphic_facts({"steps": steps}, body)
        assert len(graphic["steps"]) == 4
        assert "checklist" in ig.available_archetypes(graphic)


class TestArchetypeSelection:
    def test_a_validated_graphic_beats_a_hinted_people_scene(self):
        # cover_19: checklist=3 lost to people_scene=3.5 on the analyst's tiebreak alone.
        ranked = rank_archetypes(["checklist", "people_scene", "editorial_concept"],
                                 hint="people_scene")
        assert ranked[0][0] == "checklist"

    def test_people_still_wins_without_a_graphic(self):
        ranked = rank_archetypes(["people_scene", "editorial_concept"], hint="people_scene")
        assert ranked[0][0] == "people_scene"

    def test_a_recent_graphic_rotates_to_people(self):
        ranked = rank_archetypes(["stat_card", "people_scene", "editorial_concept"],
                                 recent=["stat_card", "editorial_concept"])
        assert ranked[0][0] == "people_scene"


class TestStrayTextTolerance:
    @pytest.mark.parametrize("token", ["O", "12", "3", "o", "IV"])
    def test_a_one_or_two_character_mark_is_ignored(self, token):
        blind = f'A woman at a desk; a small mark "{token}" on a mug.'
        assert ig_gen.stray_texts(blind) == []

    def test_unless_the_blind_look_calls_it_legible(self):
        blind = 'A wall shows the legible text "12" in large print.'
        assert ig_gen.stray_texts(blind) == ["12"]

    @pytest.mark.parametrize("token", ["pause", "AI", "OK", "MONTHLY BILL"])
    def test_words_stay_strict(self, token):
        assert ig_gen.stray_texts(f'A screen reading "{token}".') == [token]


def _concept(**overrides) -> ImageConcept:
    concept = ImageConcept(
        thesis="Web search does not stop AI hallucinations", audience="marketers",
        specific_entities=(), emotional_beat="wry", hook_phrase="Search will not save you",
        treatment=TREATMENT_EDITORIAL, treatment_rationale="", visual_anchors=("a notebook",),
        archetype="editorial_concept", archetype_ranking=("editorial_concept",))
    return dataclasses.replace(concept, **overrides)


class TestFacelessRepairs:
    def test_an_editorial_render_is_never_repaired_toward_a_face(self):
        verdict = SimpleNamespace(failing=["scroll_stop", "craft"], issues=["scroll_stop 3/5"])
        text = ig_gen.rubric_repair_directive(verdict, "gpt-image", _concept())
        assert "face" not in text.lower() and "objects only" in text

    def test_a_people_scene_still_gets_the_face_repair(self):
        verdict = SimpleNamespace(failing=["scroll_stop"], issues=[])
        concept = _concept(archetype="people_scene", treatment="people_scene")
        assert "face" in ig_gen.rubric_repair_directive(verdict, "gpt-image", concept)

    def _judged(self, concept):
        blind = MagicMock()
        blind.choices = [MagicMock(message=MagicMock(content="A notebook on a desk."))]
        targeted = MagicMock()
        targeted.choices = [MagicMock(message=MagicMock(content=(
            '{"face_emotion": false, "emotion_matches": false, "rubric": {"specificity": 4, '
            '"no_cliche": 5, "thumbnail_read": 4, "craft": 4, "scroll_stop": 3, '
            '"brand_fit": 4}}')))]
        with patch.object(ig_gen.client.chat.completions, "create",
                          side_effect=[blind, targeted]), \
                patch.object(ig_gen, "_image_part", return_value={"type": "text", "text": ""}):
            return ig_gen._staged_inspect("/tmp/x.png", concept, "Search will not save you",
                                          "newsletter")

    def test_a_faceless_render_is_never_emotion_weak(self):
        assert self._judged(_concept()).emotion_weak is False

    def test_a_people_scene_can_be_emotion_weak(self):
        concept = _concept(archetype="people_scene", treatment="people_scene")
        assert self._judged(concept).emotion_weak is True


class TestVideoFrameBrief:
    def test_the_directive_bans_text_props_and_asks_for_an_accent(self):
        text = ib.VIDEO_FRAME_DIRECTIVE
        assert "No clocks, no signs, no labelled buttons" in text
        assert "gold or charcoal accent object or wardrobe" in text

    @pytest.mark.parametrize("prompt", ["a wall clock above the desk", "an exit sign glowing",
                                        "a laptop with a pause button", "a keypad by the door"])
    def test_text_props_are_refused(self, prompt):
        assert ib.video_frame_failure(prompt)

    @pytest.mark.parametrize("prompt", ["a man in a charcoal button-down shirt",
                                        "a designer signs the contract", ""])
    def test_clean_frames_pass(self, prompt):
        assert ib.video_frame_failure(prompt) is None

    def test_the_accent_is_never_the_off_white_neutral(self):
        assert ib.video_accent_colors(frozenset({"gold", "off-white"})) == {"gold"}
        assert ib.video_accent_colors(frozenset({"navy", "white"})) == {"navy"}
        assert ib.video_accent_colors(frozenset({"off-white"})) == ib.VIDEO_ACCENT_COLORS

    def _brief(self, *prompts):
        replies = [MagicMock(choices=[MagicMock(message=MagicMock(content=(
            '{"focal_concept": "a team at work", "prompt": "%s"}' % p)), finish_reason="stop")])
            for p in prompts]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=replies), \
                patch.object(ib, "check_prompt_against_concept", return_value=(True, "ok")):
            return ib.build_image_brief("A post about outages.", surface="video", concept=None)

    def test_a_clock_frame_is_retried_and_an_accented_one_ships(self):
        clean = ("A candid photograph of an operations lead at mid-distance in a bright office, "
                 "wearing a gold scarf, reviewing a plan with a colleague.")
        clock = clean.replace("reviewing a plan", "under a large wall clock, reviewing a plan")
        brief = self._brief(clock, clean)
        assert not brief.fallback and brief.prompt == clean
        assert any("clock" in r for r in brief.rejections)

    def test_a_frame_with_only_an_off_white_accent_is_refused(self):
        neutral = ("A candid photograph of an operations lead at mid-distance in a bright office "
                   "with off-white walls, reviewing a plan with a colleague.")
        brief = self._brief(neutral, neutral, neutral)
        assert brief.fallback and any("brand-color" in r for r in brief.rejections)


class TestStoryRotation:
    ENTRIES = [
        {"id": 1, "title": "The 429 doom loop", "used_count": 0,
         "body": "On July 20 2026 LinkedIn returned 429s and the breaker tripped for 4 days."},
        {"id": 2, "title": "The $30K audit", "used_count": 1,
         "body": "We cut 11 tools and saved $30,000 per quarter."},
        {"id": 3, "title": "Routing saves money", "used_count": 2, "body": "Model routing works."},
    ]

    def test_an_entry_echoed_in_a_recent_post_is_detected(self):
        post = "When LinkedIn started returning 429s on July 20 2026, my breaker tripped."
        assert sb.entry_echoed_in(self.ENTRIES[0], post)
        assert not sb.entry_echoed_in(self.ENTRIES[1], post)

    def test_a_title_echo_counts_for_an_entry_without_numbers(self):
        assert sb.entry_echoed_in(self.ENTRIES[2], "Routing saves real money every month.")
        assert not sb.entry_echoed_in(self.ENTRIES[2], "")

    def test_the_last_posts_story_is_skipped_deterministically(self):
        recent = ["Three steps after the 429s of July 20 2026."]
        for _ in range(3):
            assert sb.select_story(self.ENTRIES, recent_texts=recent)["id"] == 2

    def test_without_history_least_used_still_wins(self):
        assert sb.select_story(self.ENTRIES)["id"] == 1

    def test_when_every_entry_is_recent_a_repeat_beats_no_anchor(self):
        recent = ["429s on July 20 2026", "saved $30,000 across 11 tools",
                  "Routing saves money here"]
        assert sb.select_story(self.ENTRIES, recent_texts=recent)["id"] == 1

    def test_the_plan_reads_the_last_three_posts(self):
        from cqc_lem.app import run_content_plan as rcp

        with patch.object(rcp, "get_story_bank_entries", return_value=self.ENTRIES), \
                patch.object(rcp, "get_recent_post_records",
                             return_value=[{"content": "the 429s of July 20 2026",
                                            "story_id": None}]) as recent:
            story = rcp._select_story_for_post(1, {})
        # The cooldown window (showcase round 4): last 10 posts or 14 days, whichever is longer.
        recent.assert_called_once_with(1, limit=sb.STORY_COOLDOWN_POSTS, exclude_post_id=None,
                                       within_days=sb.STORY_COOLDOWN_DAYS)
        assert story["id"] == 2

    def test_unreadable_history_falls_back_to_rotation(self):
        from cqc_lem.app import run_content_plan as rcp

        with patch.object(rcp, "get_story_bank_entries", return_value=self.ENTRIES), \
                patch.object(rcp, "get_recent_post_records", side_effect=RuntimeError("db")):
            assert rcp._select_story_for_post(1, {})["id"] == 1


ROUTING = "Our routing change saved $12,000 in model costs last quarter."


class TestDataCardRedundancy:
    """#2241 data_card: the headline repeated the hero, and the context line had a hole."""

    @pytest.mark.parametrize("label,expected", [
        ("Our routing change saved in model costs", "in model costs last quarter"),
        ("saved by one routing change", "in model costs last quarter"),
        ("in model costs", "in model costs"),
        ("model costs last quarter", "model costs last quarter"),
    ])
    def test_the_context_line_is_a_complete_phrase(self, label, expected):
        assert ig.complete_context(label, ROUTING, "12,000") == expected

    def test_a_figure_with_no_complete_phrase_draws_no_stat(self):
        sentence = "My client cut AI costs by 45%."
        graphic = ig.validate_graphic_facts({"thesis_stat": {
            "label": "cut AI costs by", "value": "45", "unit": "%",
            "source_sentence": sentence}}, sentence)
        assert "stat" not in graphic
        assert any("complete context" in r for r in graphic["rejected"])

    def test_a_hole_label_is_replaced_at_validation(self):
        graphic = ig.validate_graphic_facts({"thesis_stat": {
            "label": "Our routing change saved in model costs", "value": "12,000", "unit": "$",
            "source_sentence": ROUTING}}, ROUTING)
        assert graphic["stat"]["label"] == "in model costs last quarter"
        assert ig.assert_traceable("stat_card", graphic)

    @pytest.mark.parametrize("hook,repeats", [("Our routing change saved $12,000", True),
                                              ("Routing saved $12K", True),
                                              ("Routing pays for itself", False),
                                              ("We cut 12 tools", False)])
    def test_a_headline_repeating_the_hero_is_detected_by_amount(self, hook, repeats):
        assert ig.hook_repeats_figure(hook, {"amount": 12000.0}) is repeats

    def test_a_fact_without_an_amount_never_repeats(self):
        assert ig.hook_repeats_figure("Saved $12,000", {"display": "$12,000"}) is True
        assert ig.hook_repeats_figure("Saved $12,000", {}) is False

    # Round 8: the panel keeps the hook's words WITHOUT the figure (cover_16 sat ~70% empty).
    @pytest.mark.parametrize("hook,set_hook", [
        ("Our routing change saved $12,000", "Our routing change saved"),
        ("Saved $12,000", ""),
        ("Routing pays for itself", "Routing pays for itself")])
    def test_the_stat_card_panel_drops_a_repeating_headline(self, tmp_path, hook, set_hook):
        graphic = ig.validate_graphic_facts({"thesis_stat": {
            "label": "in model costs", "value": "12,000", "unit": "$",
            "source_sentence": ROUTING}}, ROUTING)
        with patch.object(ig, "compose_headline", return_value=str(tmp_path / "c.png")) as comp:
            ig.render_graphic("stat_card", graphic, surface="post_image", hook=hook,
                              kicker="AI COSTS", signature="Chris")
        assert comp.call_args.args[1] == set_hook
        assert comp.call_args.kwargs["kicker"] == "AI COSTS"
