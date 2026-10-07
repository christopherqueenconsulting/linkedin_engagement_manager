"""Archetype round wiring: the editorial brief, the people gaze, receipts and rotation (#2241)."""
import dataclasses
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import image_brief as ib
from cqc_lem.utilities.ai.image_concept import (
    ART_STYLES,
    TREATMENT_EDITORIAL,
    ImageConcept,
)

pytestmark = pytest.mark.unit

_LLM = "cqc_lem.utilities.ai.ai_helper._call_llm"


def _resp(payload) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text),
                                                    finish_reason="stop")])


def _editorial(**overrides) -> ImageConcept:
    concept = ImageConcept(
        thesis="Most small firms lose a day a week to copy-paste", audience="agency owners",
        specific_entities=(), emotional_beat="wry", hook_phrase="A day a week, gone",
        treatment=TREATMENT_EDITORIAL, treatment_rationale="abstract",
        visual_anchors=("an agency owner", "a shared inbox"),
        chosen_idea="A galvanised bucket with holes in its side, brass door keys spilling out.",
        archetype="editorial_concept", archetype_ranking=("editorial_concept",),
        art_style="risograph", layout="split_top")
    return dataclasses.replace(concept, **overrides)


class TestEditorialBrief:
    def test_the_treatment_carries_the_rotated_style(self):
        text = ib.treatment_text(TREATMENT_EDITORIAL, _editorial())
        assert ART_STYLES["risograph"] in text and "Objects only" in text
        default = ib.treatment_text(TREATMENT_EDITORIAL, _editorial(art_style=""))
        assert ART_STYLES["editorial_photo"] in default

    def test_the_author_is_told_objects_only_and_a_person_is_refused(self):
        person = {"focal_concept": "a leaking bucket", "prompt": (
            "A risograph print in charcoal and mustard gold: a man holding a galvanised bucket "
            "with holes punched in its side, brass door keys spilling out, gold accent, flat "
            "calm ground.")}
        objects = {"focal_concept": "a leaking bucket", "prompt": (
            "A risograph print in charcoal and mustard gold: a galvanised bucket with holes "
            "punched in its side, brass door keys spilling out across the floor, one gold "
            "accent, centred on a flat calm ground with generous space around it.")}
        with patch(_LLM, side_effect=[_resp(person), _resp(objects)]) as llm, \
                patch.object(ib, "check_prompt_against_concept", return_value=(True, "ok")):
            brief = ib.build_image_brief("A post about copy-paste.", surface="post_image",
                                         concept=_editorial())
        user = llm.call_args_list[0][1]["messages"][1]["content"]
        assert "OBJECTS ONLY" in user and "People may appear" not in user
        assert "TREATMENT editorial_concept" in user
        assert not brief.fallback and "man holding" not in brief.prompt
        assert "shows no person ('man')" in brief.rejections[0]

    def test_anchor_coverage_stands_down_for_an_editorial_concept(self):
        assert ib._concept_is_weak(_editorial(), ["an agency owner", "a shared inbox"])

    def test_the_fallback_stays_an_object_in_the_rotated_style(self):
        brief = ib._fallback_brief("x", surface="post_image", ratio="1:1", context="",
                                   concept=_editorial())
        assert brief.treatment == TREATMENT_EDITORIAL
        assert brief.prompt.startswith("A vintage risograph print")
        assert "Objects only" in brief.prompt and "skin pores" not in brief.prompt

    def test_the_fallback_without_an_idea_stays_an_object_only_editorial(self):
        # #2241 showcase: this used to fall to a concrete_scene, and the render came back with a
        # person in it. It stays editorial, built from the Idea Miner's object nouns.
        concept = _editorial(chosen_idea="", idea_nouns=("invoice", "CFO", "server rack",
                                                         "stopwatch", "the founder"))
        brief = ib._fallback_brief("x", surface="post_image", ratio="1:1", context="",
                                   concept=concept)
        assert brief.treatment == TREATMENT_EDITORIAL
        assert "Objects only" in brief.prompt and ib.person_word(brief.prompt) is None
        assert "stopwatch" in brief.prompt and "CFO" not in brief.prompt

    def test_the_fallback_with_a_people_idea_drops_it(self):
        concept = _editorial(chosen_idea="An engineering manager frowning at an invoice",
                             idea_nouns=(), visual_anchors=("a shared inbox",))
        brief = ib._fallback_brief("x", surface="post_image", ratio="1:1", context="",
                                   concept=concept)
        assert ib.person_word(brief.prompt) is None and "shared inbox" in brief.prompt

    def test_the_fallback_with_nothing_left_is_still_an_object(self):
        concept = _editorial(chosen_idea="", idea_nouns=(), visual_anchors=("an agency owner",))
        brief = ib._fallback_brief("x", surface="post_image", ratio="1:1", context="",
                                   concept=concept)
        assert brief.treatment == TREATMENT_EDITORIAL and ib.person_word(brief.prompt) is None
        assert "everyday object" in brief.prompt

    @pytest.mark.parametrize("prompt", ["A CFO beside a ledger", "an engineer's desk",
                                        "a smiling barista", "the manager's chair"])
    def test_role_and_portrait_words_are_refused_for_an_editorial_concept(self, prompt):
        assert ib.person_word(prompt) is not None
        assert "shows no person" in ib._deterministic_failure(
            prompt, anchors=[], weak=True, hook_text=None, no_people=True)


class TestGaze:
    @pytest.mark.parametrize("layout,toward", [("split_left", "LEFT"), ("split_right", "RIGHT"),
                                               ("split_top", "TOP"),
                                               ("split_bottom", "BOTTOM")])
    def test_a_people_scene_looks_toward_the_panel(self, layout, toward):
        concept = _editorial(treatment="people_scene", layout=layout)
        line = ib.gaze_directive(concept)
        assert toward in line and "never toward the camera" in line

    def test_no_gaze_line_otherwise(self):
        assert ib.gaze_directive(_editorial()) == ""
        assert ib.gaze_directive(None) == ""
        assert ib.gaze_directive(_editorial(treatment="people_scene", layout="")) == ""


class TestNewsletterReceipts:
    def test_recent_archetypes_prefer_what_actually_shipped(self, tmp_path):
        from cqc_lem.utilities import newsletter_cover as nc

        cover_dir = tmp_path / "images" / "newsletter_covers" / "5"
        cover_dir.mkdir(parents=True)
        receipts = [
            {"focal_concept": "a", "archetype_rendered": "editorial_concept",
             "concept": {"archetype": "stat_card"}},
            {"focal_concept": "b", "concept": {"archetype": "receipt"}},
            {"focal_concept": "c", "concept": {}},
        ]
        for n, receipt in enumerate(receipts):
            path = cover_dir / f"ed{n}.brief.json"
            path.write_text(json.dumps(receipt))
            os.utime(path, (1000 + (10 - n), 1000 + (10 - n)))
        with patch.object(nc, "assets_dir", str(tmp_path)):
            assert nc._recent_cover_archetypes(5, 3) == ["editorial_concept", "receipt"]

    def test_the_cover_passes_its_history_and_brand_to_the_engine(self, tmp_path):
        from cqc_lem.utilities import newsletter_cover as nc

        with patch.object(nc, "assets_dir", str(tmp_path)), \
                patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                      return_value=None) as analyze, \
                patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                      side_effect=RuntimeError("stop here")), \
                patch("cqc_lem.utilities.brand_kit.brand_clause_for_user", return_value="kit"):
            assert nc.generate_cover_for_edition(5, 1, "T", "S", "B", use_avatar=False) == (
                None, "Could not write a cover prompt")
        kwargs = analyze.call_args.kwargs
        assert kwargs["recent_archetypes"] == [] and kwargs["recent_art_styles"] == []


class TestPostReceipts:
    def _write(self, root, sub, name, payload, stamp):
        directory = root / "images" / sub
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        os.utime(path, (stamp, stamp))
        os.utime(directory, (stamp, stamp))

    def test_recent_post_archetypes_are_this_authors_newest_first(self, tmp_path):
        from cqc_lem.utilities import post_image as pi

        self._write(tmp_path, "posts/1", "img_a.brief.json",
                    {"user_id": 9, "concept": {"archetype": "stat_card"}}, 1000)
        self._write(tmp_path, "posts/2", "img_b.brief.json",
                    {"user_id": 9, "archetype_rendered": "checklist"}, 3000)
        self._write(tmp_path, "posts/3", "img_c.brief.json",
                    {"user_id": 4, "archetype_rendered": "receipt"}, 4000)
        self._write(tmp_path, "posts/3", "img_d.brief.json", "{not json", 4000)
        self._write(tmp_path, "post_previews/9", "img_e.brief.json",
                    {"user_id": 9, "concept": {}}, 5000)
        with patch.object(pi, "assets_dir", str(tmp_path)):
            assert pi.recent_post_archetypes(9, 2) == ["checklist", "stat_card"]
            assert pi.recent_post_archetypes(9, 0) == []

    def test_no_history_is_no_rotation(self, tmp_path):
        from cqc_lem.utilities import post_image as pi

        with patch.object(pi, "assets_dir", str(tmp_path / "nowhere")):
            assert pi.recent_post_archetypes(9, 2) == []

    def test_the_drawn_facts_ride_into_the_post_receipt(self, tmp_path):
        from cqc_lem.utilities.ai.image_brief import ImageBrief
        from cqc_lem.utilities.post_image import generate_image_for_post

        rendered = tmp_path / "render.png"
        rendered.write_bytes(b"png")
        assets = tmp_path / "assets"
        assets.mkdir()
        brief = ImageBrief(prompt="a prompt", ratio="1:1", surface="post_image",
                           style_preset="post_image", focal_concept="the idea")
        facts = [{"label": "less engagement", "display": "45%",
                  "source_sentence": "AI posts earned 45% less engagement."}]

        def _render(*_args, render_info=None, **_kwargs):
            render_info.update(gate_verdict="accepted", archetype_rendered="stat_card",
                               graphic_facts=facts, gate_pop={"thesis_fit": 2})
            return str(rendered)

        with patch("cqc_lem.utilities.post_image.assets_dir", str(assets)), \
                patch("cqc_lem.assets_dir", str(assets)), \
                patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user",
                      return_value=None), \
                patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                      return_value=None), \
                patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                      return_value=None) as analyze, \
                patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=brief), \
                patch("cqc_lem.utilities.ai.image_gen.render_image_gated", side_effect=_render):
            url, reason = generate_image_for_post(9, "Post text", post_id=42)
        assert reason is None and analyze.call_args.kwargs["recent_archetypes"] == []
        receipts = [p for p in (assets / "images" / "posts" / "42").iterdir()
                    if p.name.endswith(".brief.json")]
        payload = json.loads(receipts[0].read_text())
        assert payload["archetype_rendered"] == "stat_card"
        assert payload["graphic_facts"] == facts and payload["gate_pop"] == {"thesis_fit": 2}


class TestFounderPhoto:
    def test_parsed_but_never_in_a_prompt(self):
        from cqc_lem.utilities.brand_kit import describe_for_prompt, parse_brand_kit

        kit = parse_brand_kit({"founder_photo": "images/brand/founder.jpg"})
        assert kit.founder_photo == "images/brand/founder.jpg" and not kit.is_empty()
        assert kit.to_dict() == {"founder_photo": "images/brand/founder.jpg"}
        assert describe_for_prompt(kit) == ""

    @pytest.mark.parametrize("value", [None, 42, "", "../../etc/passwd.jpg", "photo.gif",
                                       "a b.jpg", "x" * 400 + ".png"])
    def test_anything_else_is_dropped(self, value):
        from cqc_lem.utilities.brand_kit import parse_brand_kit

        assert parse_brand_kit({"founder_photo": value}).founder_photo is None
