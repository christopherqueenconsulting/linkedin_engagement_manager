"""The visual archetype system: selection, the code-drawn first pass, and the POP judge (#2241)."""
import copy
import dataclasses
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import image_concept as ic, image_gen
from cqc_lem.utilities.ai.image_concept import (
    ImageConcept,
    ai_archetype_only,
    analyze_content_for_image,
    archetype_candidates,
    assign_art_style,
    parse_concept,
    pick_visual_idea,
    rank_archetypes,
    select_archetype,
)
from cqc_lem.utilities.ai.image_gen import (
    POP_SHIP_FLOOR,
    QualityVerdict,
    _apply_overlays,
    _pop_scores,
    inspect_render_quality,
    render_code_drawn,
    render_image_gated,
)

from .test_image_graphics import RAW, SOURCE

pytestmark = pytest.mark.unit

_CREATE = "cqc_lem.utilities.ai.client.client.chat.completions.create"

_PAYLOAD = {"thesis": "AI-written posts earn less engagement than human ones",
            "audience": "B2B founders", "specific_entities": ["AI-written posts"],
            "visual_anchors": ["a founder", "a content calendar"],
            "emotional_beat": "wry concern", "hook_phrase": "AI-written posts lose engagement",
            "treatment": "people_scene", "treatment_rationale": "people",
            "kicker": "AI CONTENT", "graphic_facts": RAW,
            "visual_ideas": ["A shiny megaphone shrinking to the size of a thimble beside a "
                             "full water jug, seen from overhead.",
                             "A founder frowning at a quiet room."]}


def _resp(content) -> SimpleNamespace:
    text = content if isinstance(content, str) else json.dumps(content)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _concept(**overrides) -> ImageConcept:
    concept = parse_concept(_PAYLOAD, SOURCE)
    return dataclasses.replace(concept, **overrides)


class TestParse:
    def test_graphic_facts_are_validated_into_the_concept(self):
        concept = _concept()
        assert concept.graphic["stat"]["display"] == "45%"
        assert concept.graphic["stat"]["source_sentence"] in SOURCE
        assert concept.archetype_hint == "" and concept.human_moment is False

    def test_human_moment_only_when_literally_true_and_the_hint_must_be_known(self):
        payload = dict(_PAYLOAD, human_moment="yes", archetype="meme")
        assert parse_concept(payload, SOURCE).human_moment is False
        assert parse_concept(payload, SOURCE).archetype_hint == ""
        payload = dict(_PAYLOAD, human_moment=True, archetype="Receipt",
                       people_idea="A founder laughing with a client, gaze toward the left.",
                       idea_nouns=["mug", "", "megaphone"])
        concept = parse_concept(payload, SOURCE)
        assert concept.human_moment and concept.archetype_hint == "receipt"
        assert concept.people_idea.startswith("A founder") and concept.idea_nouns == (
            "mug", "megaphone")

    def test_no_graphic_facts_is_none(self):
        payload = {k: v for k, v in _PAYLOAD.items() if k != "graphic_facts"}
        assert parse_concept(payload, SOURCE).graphic is None


class TestSelection:
    def test_candidates_follow_the_validated_data(self):
        assert archetype_candidates(_concept()) == list(ic.CODE_DRAWN_ARCHETYPES) + [
            "editorial_concept"]
        assert archetype_candidates(_concept(graphic=None, human_moment=True)) == [
            "people_scene", "editorial_concept"]

    def test_no_headline_no_code_drawn_graphic(self):
        assert archetype_candidates(_concept(hook_phrase="")) == ["editorial_concept"]

    def test_evidence_strength_then_code_drawn_on_ties(self):
        ranked = [a for a, _ in rank_archetypes(
            ["stat_card", "checklist", "people_scene", "editorial_concept", "receipt"])]
        assert ranked == ["receipt", "stat_card", "checklist", "people_scene",
                          "editorial_concept"]

    def test_the_hint_only_breaks_a_tie(self):
        ranked = rank_archetypes(["stat_card", "people_scene", "highlight_chart"],
                                 hint="people_scene")
        assert [a for a, _ in ranked] == ["highlight_chart", "people_scene", "stat_card"]

    def test_the_last_two_archetypes_are_penalised(self):
        ranked = rank_archetypes(["stat_card", "highlight_chart", "editorial_concept"],
                                 recent=["highlight_chart", "stat_card", "receipt"])
        # A recent archetype loses to EVERY fresh candidate, the editorial concept included.
        assert [a for a, _ in ranked] == ["editorial_concept", "highlight_chart", "stat_card"]
        scores = dict(ranked)
        assert scores["highlight_chart"] == 0.0 and scores["stat_card"] == -1.0

    def test_only_the_last_two_count(self):
        scores = dict(rank_archetypes(["receipt"], recent=["a", "b", "receipt"]))
        assert scores["receipt"] == 4.0

    def test_select_builds_the_chain_and_demotes_the_people_photo(self):
        concept = select_archetype(_concept(), "newsletter")
        assert concept.archetype == "highlight_chart"
        assert concept.archetype_ranking[-1] == "editorial_concept"
        assert concept.treatment == ic.TREATMENT_EDITORIAL and concept.cast is None
        assert "highlight_chart=4" in concept.archetype_rationale

    def test_a_human_moment_ends_the_chain_in_a_people_scene(self):
        concept = select_archetype(_concept(graphic=None, human_moment=True), "post_image")
        assert concept.archetype == "people_scene" and concept.treatment == "people_scene"

    def test_other_surfaces_are_untouched(self):
        concept = _concept()
        assert select_archetype(concept, "video") is concept

    def test_ai_archetype_only(self):
        selected = select_archetype(_concept(), "newsletter")
        stripped = ai_archetype_only(selected)
        assert stripped.archetype == "editorial_concept"
        assert stripped.archetype_ranking == ("editorial_concept",)
        assert ai_archetype_only(None) is None
        plain = _concept()
        assert ai_archetype_only(plain) is plain
        assert ai_archetype_only(dataclasses.replace(selected, archetype_ranking=())).archetype \
            == "editorial_concept"

    def test_art_style_rotates_least_recently_used(self):
        concept = select_archetype(_concept(), "newsletter")
        styled = assign_art_style(concept, ["risograph", "cut_collage", "claymation"])
        assert styled.art_style == "editorial_photo"
        assert assign_art_style(styled, ["editorial_photo"]) is styled
        people = select_archetype(_concept(graphic=None, human_moment=True), "post_image")
        assert assign_art_style(people).art_style == ""


class TestPickVisualIdea:
    def test_a_human_moment_takes_the_people_idea(self):
        concept = select_archetype(_concept(graphic=None, human_moment=True,
                                            people_idea="A founder grinning, gaze left."),
                                   "post_image")
        picked = pick_visual_idea(concept)
        assert picked.chosen_idea == "A founder grinning, gaze left."

    def test_an_editorial_concept_never_picks_a_person(self):
        concept = select_archetype(_concept(), "newsletter")
        with patch(_CREATE) as create:
            picked = pick_visual_idea(concept)
        create.assert_not_called()  # one object idea survives — nothing to rank
        assert picked.chosen_idea.startswith("A shiny megaphone")

    def test_the_editorial_ranker_scores_the_pop_rubric_and_demotes_a_desk(self):
        ideas = ("A brass bell resting on a desk under a lamp.",
                 "A megaphone shrinking to a thimble beside a water jug.")
        concept = select_archetype(_concept(visual_ideas=ideas), "newsletter")
        with patch(_CREATE, return_value=_resp({"ranking": [1, 2], "reason": "r"})) as create:
            picked = pick_visual_idea(concept)
        prompt = create.call_args[1]["messages"][0]["content"]
        assert "curiosity gap" in prompt and "small-business owner" in prompt
        assert picked.chosen_idea == ideas[1]
        assert "next object idea" in picked.idea_pick_reason


class TestAnalyzeEndToEnd:
    def test_a_cover_with_data_becomes_a_code_drawn_graphic_with_its_trace(self):
        with patch(_CREATE, return_value=_resp(_PAYLOAD)):
            concept = analyze_content_for_image(SOURCE, surface="newsletter",
                                                recent_archetypes=["highlight_chart"])
        assert concept.archetype in ("receipt", "before_after")
        assert concept.graphic["costs"]["items"][0]["source_sentence"] in SOURCE
        assert concept.art_style in ic.ART_STYLES
        assert concept.to_dict()["graphic"]["stat"]["display"] == "45%"


# ------------------------------------------------------------------------------------------
# The code-drawn first pass in image_gen.
# ------------------------------------------------------------------------------------------

def _selected(**overrides):
    return select_archetype(_concept(**overrides), "post_image")


def _verdict(acceptable=True, checked=True, failing=()):
    return QualityVerdict(acceptable=acceptable, checked=checked, failing=list(failing),
                          rubric={"specificity": 5}, issues=["pop 11/14"],
                          pop={"thesis_fit": 2})


@pytest.fixture
def assets(tmp_path):
    with patch.object(image_gen, "assets_dir", str(tmp_path)):
        yield tmp_path


class TestRenderCodeDrawn:
    def test_an_accepted_graphic_ships_with_its_trace_and_no_render_spend(self, assets):
        info: dict = {}
        with patch.object(image_gen, "inspect_render_quality", return_value=_verdict()), \
                patch.object(image_gen, "_render_with_backend") as backend, \
                patch("cqc_lem.utilities.observability.track_image_gate_verdict"):
            path = render_image_gated("unused prompt", surface="post_image",
                                      concept=_selected(), hook_text="Who reads AI posts?",
                                      render_info=info, user_id=7)
        backend.assert_not_called()
        assert os.path.isfile(path) and path.startswith(str(assets))
        assert info["archetype_rendered"] == "highlight_chart"
        assert info["gate_verdict"] == "accepted" and info["gate_pop"] == {"thesis_fit": 2}
        assert all(f["source_sentence"] in SOURCE for f in info["graphic_facts"])

    def test_a_fabricated_figure_falls_to_the_next_archetype(self, assets):
        concept = _selected()
        tampered = copy.deepcopy(concept.graphic)
        tampered["comparison"]["items"][0]["display"] = "62%"
        concept = dataclasses.replace(concept, graphic=tampered)
        info: dict = {}
        with patch.object(image_gen, "inspect_render_quality", return_value=_verdict()), \
                patch("cqc_lem.utilities.observability.track_image_gate_verdict"):
            path = render_code_drawn(concept, surface="post_image", hook_text="A hook",
                                     render_info=info)
        assert path and info["archetype_rendered"] == concept.archetype_ranking[1]
        assert "does not trace" in info["archetype_fallback_reason"]

    def test_a_judge_rejection_falls_through_to_the_ai_render(self, assets):
        info: dict = {}
        rejected = _verdict(acceptable=False, failing=["specificity"])
        with patch.object(image_gen, "inspect_render_quality", return_value=rejected), \
                patch.object(image_gen, "_gate_loop", return_value="ai.png") as loop:
            path = render_image_gated("prompt", surface="post_image", concept=_selected(),
                                      hook_text="A hook", render_info=info)
        assert path == "ai.png"
        assert info["archetype_rendered"] == "editorial_concept"
        assert "judge rejected (specificity)" in info["archetype_fallback_reason"]
        assert loop.call_args.kwargs["concept"].archetype == "editorial_concept"
        assert not [n for n in os.listdir(assets / "images" / "generated" / "system")]

    def test_an_ungradable_graphic_is_looked_at_twice_then_fails_open(self, assets):
        info: dict = {}
        unchecked = _verdict(acceptable=True, checked=False)
        with patch.object(image_gen, "inspect_render_quality", return_value=unchecked) as judge, \
                patch("cqc_lem.utilities.observability.track_image_gate_verdict"):
            path = render_code_drawn(_selected(), surface="post_image", hook_text="A hook",
                                     render_info=info, enforce=True)
        assert path and judge.call_count == 2 and info["gate_verdict"] == "unchecked"

    @pytest.mark.parametrize("kwargs", [
        {"hook_text": None}, {"surface": "video"},
    ])
    def test_nothing_to_draw(self, kwargs):
        args = dict(surface="post_image", hook_text="A hook")
        args.update(kwargs)
        assert render_code_drawn(_selected(), **args) is None
        assert render_code_drawn(None, surface="post_image", hook_text="A hook") is None
        ai = select_archetype(_concept(graphic=None), "post_image")
        assert render_code_drawn(ai, surface="post_image", hook_text="A hook") is None

    def test_the_avatar_renderer_tries_the_graphic_first(self, assets):
        info: dict = {}
        with patch.object(image_gen, "render_code_drawn", return_value="g.png") as drawn, \
                patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar"
                      ) as lora:
            path = image_gen.render_avatar_image_gated(
                "p", avatar={"model_ref": "x"}, user_id=1, surface="newsletter",
                concept=_selected(), hook_text="A hook", render_info=info)
        assert path == "g.png" and info["used_avatar"] is False
        drawn.assert_called_once()
        lora.assert_not_called()


# ------------------------------------------------------------------------------------------
# The judge: the "piques interest" rubric and the archetype-aware overlays.
# ------------------------------------------------------------------------------------------

_POP = {"thumbnail_read": 2, "thesis_fit": 2, "curiosity_gap": 1, "novelty": 2,
        "resolves_2s": 2, "credibility": 1, "icp_relevance": 1}


class TestPopRubric:
    def test_scores_parse_and_clamp(self):
        assert _pop_scores(dict(_POP, novelty=7))["novelty"] == 2
        assert _pop_scores(dict(_POP, novelty=True)) == {}
        assert _pop_scores({"thesis_fit": 2}) == {}
        assert _pop_scores(None) == {}

    def test_below_the_ship_floor_caps_scroll_stop(self):
        low = dict(_POP, curiosity_gap=0, novelty=0, icp_relevance=0)
        assert sum(low.values()) < POP_SHIP_FLOOR
        rubric = _apply_overlays({"scroll_stop": 5, "specificity": 5}, blind="", entities=[],
                                 answer={"pop": low}, hook_text=None)
        assert rubric["scroll_stop"] == 3 and rubric["specificity"] == 5

    def test_a_zero_thesis_fit_caps_specificity(self):
        rubric = _apply_overlays({"scroll_stop": 5, "specificity": 5}, blind="", entities=[],
                                 answer={"pop": dict(_POP, thesis_fit=0)}, hook_text=None)
        assert rubric["specificity"] == 3

    def test_a_code_drawn_graphic_is_credible_by_construction(self):
        borderline = {"thumbnail_read": 2, "thesis_fit": 2, "curiosity_gap": 1, "novelty": 1,
                      "resolves_2s": 1, "credibility": 0, "icp_relevance": 1}
        assert sum(borderline.values()) == POP_SHIP_FLOOR - 1
        capped = _apply_overlays({"scroll_stop": 5}, blind="", entities=[],
                                 answer={"pop": borderline}, hook_text=None)
        kept = _apply_overlays({"scroll_stop": 5}, blind='"45%"', entities=[],
                               answer={"pop": borderline}, hook_text=None, code_drawn=True)
        assert capped["scroll_stop"] == 3
        assert kept["scroll_stop"] == 5 and kept["text_accuracy"] is None

    def test_a_faceless_archetype_is_never_capped_for_emotion(self):
        answer = {"emotion_matches": False, "emotion_authentic": False}
        capped = _apply_overlays({"scroll_stop": 5, "craft": 5}, blind="", entities=[],
                                 answer=answer, hook_text=None)
        kept = _apply_overlays({"scroll_stop": 5, "craft": 5}, blind="", entities=[],
                               answer=answer, hook_text=None, no_face=True)
        assert capped == {"scroll_stop": 3, "craft": 3, "text_accuracy": None}
        assert kept["scroll_stop"] == 5 and kept["craft"] == 5


class TestCodeDrawnJudge:
    def _judge(self, tmp_path, answer, concept):
        img = tmp_path / "g.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as client:
            client.chat.completions.create.side_effect = [
                _resp('A chart reading "45%" and "38.2%".'), _resp(answer)]
            verdict = inspect_render_quality(str(img), "f", surface="post_image",
                                             concept=concept, hook_text="A hook",
                                             composite_path=str(img))
            prompt = client.chat.completions.create.call_args_list[1][1]["messages"][0][
                "content"][0]["text"]
        return verdict, prompt

    def test_only_specificity_scroll_stop_and_brand_are_gated(self, tmp_path):
        answer = {"rubric": {"specificity": 5, "no_cliche": 2, "thumbnail_read": 2, "craft": 2,
                             "scroll_stop": 4, "brand_fit": 4}, "pop": _POP}
        verdict, prompt = self._judge(tmp_path, answer, _selected())
        assert verdict.acceptable and verdict.rubric["text_accuracy"] is None
        assert "CODE-DRAWN data graphic" in prompt and "PIQUE INTEREST" in prompt
        assert verdict.pop["credibility"] == 2
        assert not any(i.startswith("stray text") for i in verdict.issues)

    def test_a_graphic_that_does_not_carry_the_piece_fails(self, tmp_path):
        answer = {"rubric": {"specificity": 3, "no_cliche": 5, "thumbnail_read": 5, "craft": 5,
                             "scroll_stop": 5, "brand_fit": 5}}
        verdict, _ = self._judge(tmp_path, answer, _selected())
        assert not verdict.acceptable and verdict.failing == ["specificity"]

    def test_an_ai_render_still_gets_the_full_floors_and_no_graphic_note(self, tmp_path):
        answer = {"rubric": {"specificity": 5, "no_cliche": 2, "thumbnail_read": 5, "craft": 5,
                             "scroll_stop": 5, "brand_fit": 5}}
        ai = ai_archetype_only(_selected())
        verdict, prompt = self._judge(tmp_path, answer, ai)
        assert "no_cliche" in verdict.failing and "CODE-DRAWN" not in prompt
