"""Stage 4 of the image engine: the blind judge, its rubric, and the hook-text exception (#2241).

The old gate scored a render against the brief's OWN focal concept, so a valve scored 5/5 against
"a valve symbolising leaks". These pin the replacement: a first look that is shown nothing, then
targeted questions graded on a rubric that a stock symbol cannot pass.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import image_gen
from cqc_lem.utilities.ai.image_concept import ImageConcept
from cqc_lem.utilities.ai.image_gen import (
    _NO_MARKS_FLUX,
    _NO_MARKS_GPT,
    BLIND_JUDGE_PROMPT,
    QualityVerdict,
    inspect_render_quality,
    render_image_gated,
    rubric_repair_directive,
    stray_texts,
    with_no_marks,
)

pytestmark = pytest.mark.unit

_CONCEPT = ImageConcept(
    thesis="Late invoices quietly starve a small agency's payroll", audience="agency owners",
    specific_entities=("unpaid invoices", "payroll run", "agency owner"),
    emotional_beat="dread", hook_phrase="", treatment="concrete_scene",
    treatment_rationale="tangible")
_HOOK = "Payroll eats first"
_BLIND = "An agency owner at a kitchen table with a stack of invoices. No visible text."
_GOOD_RUBRIC = {"specificity": 5, "no_cliche": 5, "thumbnail_read": 4, "text_accuracy": None,
                "craft": 5, "scroll_stop": 4}


def _resp(content) -> SimpleNamespace:
    text = content if isinstance(content, str) else json.dumps(content)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _answer(rubric=None, **overrides) -> dict:
    answer = {"entities_depicted": {"unpaid invoices": True, "payroll run": True,
                                    "agency owner": True},
              "text_seen": "", "cliches_present": [], "rubric": dict(rubric or _GOOD_RUBRIC),
              "issues": []}
    answer.update(overrides)
    return answer


def _judge(tmp_path, *, blind=_BLIND, answer=None, hook_text=None, concept=_CONCEPT):
    img = tmp_path / "r.png"
    img.write_bytes(b"png")
    with patch.object(image_gen, "client") as client:
        client.chat.completions.create.side_effect = [_resp(blind), _resp(answer or _answer())]
        verdict = inspect_render_quality(str(img), "a focal concept", surface="newsletter",
                                         concept=concept, hook_text=hook_text)
    return verdict, client.chat.completions.create


class TestBlindFirst:
    def test_the_first_vision_call_is_shown_nothing_about_the_brief_or_the_piece(self, tmp_path):
        verdict, create = _judge(tmp_path)
        assert verdict.acceptable and verdict.checked
        assert create.call_count == 2
        first = create.call_args_list[0][1]
        assert first["model"] == "lem-vision"
        text_parts = [p["text"] for p in first["messages"][0]["content"] if p["type"] == "text"]
        assert text_parts == [BLIND_JUDGE_PROMPT]
        blob = json.dumps(first["messages"])
        for leak in ("a focal concept", _CONCEPT.thesis, "unpaid invoices", "payroll"):
            assert leak not in blob, f"the blind judge was shown {leak!r}"

    def test_the_targeted_call_carries_the_concept_and_the_blind_description(self, tmp_path):
        _verdict, create = _judge(tmp_path, hook_text=_HOOK)
        second = create.call_args_list[1][1]
        text = second["messages"][0]["content"][0]["text"]
        assert _CONCEPT.thesis in text and _BLIND in text
        assert "unpaid invoices; payroll run; agency owner" in text
        assert f'Does it equal exactly "{_HOOK}"?' in text
        assert "valve" in text, "the stock-symbol list is asked about by name"
        assert second["response_format"] == {"type": "json_object"}

    def test_the_verdict_carries_rubric_and_blind_description(self, tmp_path):
        verdict, _ = _judge(tmp_path)
        assert verdict.rubric["specificity"] == 5 and verdict.relevance == 5
        assert verdict.blind_description == _BLIND
        assert verdict.failing == []


class TestRubric:
    @pytest.mark.parametrize("criterion,score", [("specificity", 3), ("no_cliche", 4),
                                                 ("craft", 3)])
    def test_each_floor_rejects(self, tmp_path, criterion, score):
        verdict, _ = _judge(tmp_path, answer=_answer(dict(_GOOD_RUBRIC, **{criterion: score})))
        assert not verdict.acceptable
        assert verdict.failing == [criterion]
        assert f"{criterion} {score}/5" in verdict.issues

    def test_a_cliche_the_blind_judge_names_fails_whatever_the_rubric_says(self, tmp_path):
        verdict, _ = _judge(tmp_path, blind="A brass valve on a workbench, pipes behind it.")
        assert not verdict.acceptable and "no_cliche" in verdict.failing

    def test_a_cliche_the_targeted_judge_lists_fails(self, tmp_path):
        verdict, _ = _judge(tmp_path, answer=_answer(cliches_present=["gear"]))
        assert not verdict.acceptable and verdict.rubric["no_cliche"] <= 2

    def test_no_anchor_seen_at_all_caps_specificity(self, tmp_path):
        seen = {"unpaid invoices": False, "payroll run": False, "agency owner": False}
        verdict, _ = _judge(tmp_path, answer=_answer(entities_depicted=seen))
        assert not verdict.acceptable and verdict.rubric["specificity"] == 3

    def test_one_anchor_seen_is_enough_the_count_is_advisory(self, tmp_path):
        """Round 3: the headline carries the thesis; two anchors is no longer required."""
        seen = {"unpaid invoices": True, "payroll run": False, "agency owner": False}
        verdict, _ = _judge(tmp_path, answer=_answer(entities_depicted=seen))
        assert verdict.acceptable and verdict.rubric["specificity"] == 5

    def test_the_hook_must_be_transcribed_exactly(self, tmp_path):
        verdict, _ = _judge(tmp_path, hook_text=_HOOK,
                            answer=_answer(dict(_GOOD_RUBRIC, text_accuracy=5),
                                           text_seen="Payrol eats frist"))
        assert not verdict.acceptable and verdict.failing == ["text_accuracy"]

    def test_an_exact_hook_passes(self, tmp_path):
        verdict, _ = _judge(tmp_path, hook_text=_HOOK,
                            answer=_answer(dict(_GOOD_RUBRIC, text_accuracy=5),
                                           text_seen="PAYROLL EATS FIRST."))
        assert verdict.acceptable

    def test_stray_text_without_a_hook_fails(self, tmp_path):
        verdict, _ = _judge(tmp_path, answer=_answer(text_seen="Q3 REVENUE"))
        assert not verdict.acceptable and verdict.rubric["text_accuracy"] == 2

    @pytest.mark.parametrize("seen", ["", "none", "No visible text"])
    def test_no_text_is_not_applicable(self, tmp_path, seen):
        verdict, _ = _judge(tmp_path, answer=_answer(text_seen=seen))
        assert verdict.acceptable and verdict.rubric["text_accuracy"] is None

    @pytest.mark.parametrize("answer", [_answer(rubric={"specificity": 5}),
                                        _answer(rubric={"specificity": "high", "no_cliche": 5,
                                                        "craft": 5}),
                                        "not json"])
    def test_an_unusable_rubric_fails_open(self, tmp_path, answer):
        img = tmp_path / "r.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as client:
            client.chat.completions.create.side_effect = [_resp(_BLIND), _resp(answer)]
            verdict = inspect_render_quality(str(img), "f", concept=_CONCEPT)
        assert verdict.acceptable and not verdict.checked

    def test_a_vision_outage_fails_open(self, tmp_path):
        img = tmp_path / "r.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as client:
            client.chat.completions.create.side_effect = RuntimeError("down")
            verdict = inspect_render_quality(str(img), "f", concept=_CONCEPT)
        assert verdict.acceptable and not verdict.checked

    def test_without_a_concept_the_legacy_single_call_runs(self, tmp_path):
        img = tmp_path / "r.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as client:
            client.chat.completions.create.return_value = _resp(
                {"acceptable": True, "relevance": 5, "issues": []})
            verdict = inspect_render_quality(str(img), "a focal concept", surface="newsletter")
        assert client.chat.completions.create.call_count == 1
        assert verdict.acceptable and verdict.rubric == {}


class TestRubricRepair:
    def _verdict(self, failing, issues=()):
        return QualityVerdict(acceptable=False, failing=list(failing),
                              issues=[f"{f} 2/5" for f in failing] + list(issues))

    def test_gpt_repair_names_the_entities_and_the_failing_criteria(self):
        directive = rubric_repair_directive(self._verdict(["specificity", "no_cliche"],
                                                          ["a brass valve"]),
                                            "gpt-image", _CONCEPT, None)
        assert "specificity, no_cliche" in directive
        assert "unpaid invoices; payroll run; agency owner" in directive
        assert "a brass valve" in directive

    def test_flux_repair_never_names_the_defect(self):
        directive = rubric_repair_directive(self._verdict(["no_cliche"], ["a brass valve"]),
                                            "flux", _CONCEPT, None)
        assert directive.startswith("Render this scene again with")
        assert "valve" not in directive and "unpaid invoices" in directive

    def test_a_hook_failure_names_the_exact_hook(self):
        directive = rubric_repair_directive(self._verdict(["text_accuracy"]), "gpt-image",
                                            _CONCEPT, _HOOK)
        assert f'"{_HOOK}"' in directive

    def test_a_text_failure_without_a_hook_asks_for_blank_surfaces(self):
        directive = rubric_repair_directive(self._verdict(["text_accuracy"]), "flux",
                                            _CONCEPT, None)
        assert "plain and unmarked" in directive

    def test_no_failing_criteria_falls_back_to_the_legacy_directive(self):
        verdict = QualityVerdict(acceptable=False, issues=["six fingers"])
        assert rubric_repair_directive(verdict, "flux", _CONCEPT, None) == \
            image_gen.repair_directive(["six fingers"], "flux", _CONCEPT.thesis)


class TestStagedGateLoop:
    def test_concept_and_hook_reach_the_judge_and_the_renderer(self):
        with patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/1.png", "gpt-image")) as render, \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)) as judge:
            render_image_gated("p", surface="newsletter", concept=_CONCEPT, hook_text=_HOOK)
        assert judge.call_args[1]["concept"] is _CONCEPT
        assert judge.call_args[1]["hook_text"] == _HOOK
        assert render.call_args[1]["hook_text"] == _HOOK

    def test_without_a_concept_the_judge_is_called_the_legacy_way(self):
        with patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/1.png", "gpt-image")), \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)) as judge:
            render_image_gated("p", surface="newsletter", focal_concept="f")
        assert "concept" not in judge.call_args[1]

    def test_a_rubric_rejection_repairs_from_the_failing_criteria_and_records_why(self):
        bad = QualityVerdict(acceptable=False, failing=["no_cliche"], issues=["no_cliche 1/5"],
                             rubric={"no_cliche": 1, "specificity": 4, "craft": 5},
                             blind_description="A brass valve.")
        info: dict = {}
        with patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/x.png", "flux")) as render, \
             patch.object(image_gen, "inspect_render_quality", return_value=bad):
            render_image_gated("base", surface="newsletter", concept=_CONCEPT, render_info=info)
        # Covers render two candidates per attempt (round 4); the LAST render is the repair.
        retry = render.call_args_list[-1][0][0]
        assert retry.startswith("base\n\nRender this scene again with the whole frame built on")
        assert info["gate_verdict"] == "rejected"
        assert info["gate_rubric"]["no_cliche"] == 1
        assert info["gate_failing"] == ["no_cliche"]
        assert info["gate_blind_description"] == "A brass valve."

    def test_two_candidates_per_attempt_keep_the_better(self, monkeypatch):
        monkeypatch.setenv("IMAGE_GATE_CANDIDATES", "2")
        worse = QualityVerdict(acceptable=False, failing=["craft"], rubric={"craft": 2})
        better = QualityVerdict(acceptable=False, failing=["craft"], rubric={"craft": 3})
        with patch.object(image_gen, "IMAGE_GATE_MAX_ATTEMPTS", 1), \
             patch.object(image_gen, "_render_with_backend",
                          side_effect=[("/tmp/a.png", "gpt-image"),
                                       ("/tmp/b.png", "gpt-image")]) as render, \
             patch.object(image_gen, "inspect_render_quality", side_effect=[worse, better]):
            path = render_image_gated("p", surface="newsletter", concept=_CONCEPT)
        assert render.call_count == 2 and path == "/tmp/b.png"

    def test_an_accepted_first_candidate_skips_the_second(self, monkeypatch):
        monkeypatch.setenv("IMAGE_GATE_CANDIDATES", "2")
        with patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/a.png", "gpt-image")) as render, \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)):
            render_image_gated("p", surface="newsletter", concept=_CONCEPT)
        assert render.call_count == 1

    @pytest.mark.parametrize("value,concept,expected", [("2", _CONCEPT, 2), ("9", _CONCEPT, 2),
                                                        ("0", _CONCEPT, 1), ("x", _CONCEPT, 1),
                                                        ("2", None, 1)])
    def test_candidate_count_is_bounded_and_needs_a_concept(self, monkeypatch, value, concept,
                                                           expected):
        monkeypatch.setenv("IMAGE_GATE_CANDIDATES", value)
        assert image_gen._gate_candidates(concept) == expected

    def test_the_avatar_path_takes_the_concept_and_hook_too(self):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/1.png", True)) as lora, \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)) as judge:
            image_gen.render_avatar_image_gated("p", avatar=avatar, user_id=3,
                                                surface="newsletter", concept=_CONCEPT)
        assert judge.call_args[1]["concept"] is _CONCEPT
        assert lora.call_count == 1

    def test_an_avatar_render_that_produced_nothing_returns_none(self):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        info: dict = {}
        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=(None, False)), \
             patch.object(image_gen, "inspect_render_quality") as judge:
            assert image_gen.render_avatar_image_gated(
                "p", avatar=avatar, user_id=3, surface="newsletter", render_info=info) is None
        judge.assert_not_called()
        assert info == {"used_avatar": False}


class TestHookTextException:
    """The ONE text exception: an editorial_graphic's declared hook (issue #2241)."""

    def test_gpt_gets_the_hook_only_clause_instead_of_the_blanket_ban(self):
        marked = with_no_marks("A designed editorial graphic.", "gpt-image", hook_text=_HOOK)
        assert f'The only text in the image is exactly "{_HOOK}"' in marked
        assert _NO_MARKS_GPT not in marked

    def test_flux_gets_it_positively(self):
        marked = with_no_marks("A designed editorial graphic.", "flux", hook_text=_HOOK)
        assert f'the exact phrase "{_HOOK}"' in marked
        assert _NO_MARKS_FLUX not in marked and "no other" not in marked

    def test_the_printed_surface_clause_is_skipped_for_a_hook_but_screens_are_not(self):
        prompt = "Type on a poster beside a laptop."
        marked = with_no_marks(prompt, "gpt-image", hook_text=_HOOK)
        assert "printed surface in the frame is bare" not in marked
        assert "switched off and uniformly dark" in marked

    def test_the_hook_clause_is_added_at_most_once(self):
        once = with_no_marks("A graphic.", "gpt-image", hook_text=_HOOK)
        assert with_no_marks(once, "gpt-image", hook_text=_HOOK) == once

    def test_without_a_hook_nothing_changes(self):
        assert with_no_marks("A desk.", "gpt-image") == "A desk." + _NO_MARKS_GPT

    def test_the_renderer_receives_the_hook_clause(self):
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png") as gpt:
            image_gen._render_with_backend("A graphic.", hook_text=_HOOK)
        assert f'exactly "{_HOOK}"' in gpt.call_args[0][0]


# The blind descriptions gpt-4o-mini returned for gauntlet round 1 of #2241, verbatim.
_ED16_BLIND = ('The image features a stack of cash, specifically bundles of hundred-dollar bills, '
               'placed on a contrasting black and yellow background. The visible text reads, '
               '"Stop wasting your budget!" and "$30K" is displayed on a piece of torn paper next '
               'to the cash.')
_ED18_BLIND = ('The image features a magazine titled "AI posts fall flat" alongside a tablet '
               'displaying the text "Originality ai." The setting has a warm, neutral background, '
               'and the magazine includes a blue and green arrow graphic, with additional text at '
               'the bottom that reads "2013 Eletinain Lintedin Eath mae 3 Lugolictily. Impare '
               'Reporctt."')
_ED17_BLIND = ('The image features two black devices on a desk, set in a modern office. The '
               'visible text reads, "Web search isn’t enough."')


class TestStrayTextInTheBlindDescription:
    def test_ed16_the_number_beside_the_hook_is_stray(self):
        assert stray_texts(_ED16_BLIND, "Stop wasting your budget!") == ["$30K"]

    def test_ed18_the_tablet_and_report_text_is_stray(self):
        assert stray_texts(_ED18_BLIND, "AI posts fall flat") == [
            "Originality ai.", "2013 Eletinain Lintedin Eath mae 3 Lugolictily. Impare Reporctt."]

    def test_ed17_only_the_hook_is_clean(self):
        assert stray_texts(_ED17_BLIND, "Web search isn’t enough") == []

    @pytest.mark.parametrize("blind,expected", [
        ("A man reading a printed report at a desk. There is no visible text.", []),
        ("A storefront with a sign reading OPEN LATE. A woman walks past.", ["OPEN LATE"]),
        ('A folder labeled QUARTERLY on a desk.', ["QUARTERLY"]),
    ])
    def test_unquoted_text_after_a_text_verb_counts_but_reading_alone_does_not(self, blind,
                                                                              expected):
        found = stray_texts(blind, None)
        assert len(found) == len(expected)
        assert all(f.startswith(e) for f, e in zip(found, expected))

    @pytest.mark.parametrize("blind,hook", [(_ED16_BLIND, "Stop wasting your budget!"),
                                            (_ED18_BLIND, "AI posts fall flat")])
    def test_stray_text_fails_the_gate_even_when_the_judge_scores_text_5(self, tmp_path, blind,
                                                                         hook):
        verdict, _ = _judge(tmp_path, blind=blind, hook_text=hook,
                            answer=_answer(dict(_GOOD_RUBRIC, text_accuracy=5), text_seen=hook))
        assert not verdict.acceptable
        assert verdict.rubric["text_accuracy"] == 2 and "text_accuracy" in verdict.failing
        assert any(i.startswith("stray text:") for i in verdict.issues)

    def test_the_blind_prompt_asks_for_every_piece_of_text(self):
        assert "list EVERY piece of visible text verbatim" in BLIND_JUDGE_PROMPT
        assert "small or garbled text, labels, and text on devices or paper" in BLIND_JUDGE_PROMPT


class TestSpecificityNeedsTheThesis:
    def test_a_thesis_the_judge_says_a_stranger_cannot_infer_caps_specificity(self, tmp_path):
        verdict, _ = _judge(tmp_path, answer=_answer(thesis_inferable=False))
        assert not verdict.acceptable and verdict.rubric["specificity"] == 3

    def test_the_targeted_judge_is_asked_about_anchors_and_inference(self, tmp_path):
        concept = ImageConcept(
            thesis="t", audience="a", specific_entities=("Terralogic", "$30K"),
            emotional_beat="e", hook_phrase="", treatment="people_scene",
            treatment_rationale="r", visual_anchors=("a marketing lead", "a printed checklist"))
        _verdict, create = _judge(tmp_path, concept=concept,
                                  answer=_answer(entities_depicted={"a marketing lead": True,
                                                                    "a printed checklist": True}))
        text = create.call_args_list[1][1]["messages"][0]["content"][0]["text"]
        assert "a marketing lead; a printed checklist" in text
        assert "Terralogic" not in text, "a fact is never asked about as something visible"
        assert "thesis_inferable" in text


_PEOPLE = ImageConcept(
    thesis="Late invoices quietly starve a small agency's payroll", audience="agency owners",
    specific_entities=("unpaid invoices",), emotional_beat="a wince of dread",
    hook_phrase="Payroll eats first", treatment="people_scene", treatment_rationale="r",
    visual_anchors=("an agency owner", "a payroll run printout"))


def _judge_on(tmp_path, surface, answer, concept=_PEOPLE, hook_text=None):
    # This concept's own anchor is the one in frame, so only the criterion under test can fail.
    answer = dict(answer, entities_depicted={"an agency owner": True})
    img = tmp_path / "r.png"
    img.write_bytes(b"png")
    with patch.object(image_gen, "client") as client:
        client.chat.completions.create.side_effect = [_resp(_BLIND), _resp(answer)]
        verdict = inspect_render_quality(str(img), "f", surface=surface, concept=concept,
                                         hook_text=hook_text)
    return verdict, client.chat.completions.create


class TestRoundThreeJudge:
    """Round 3 of #2241: headline + image judged together; emotion and brand are graded."""

    def test_the_headline_reaches_the_targeted_judge(self, tmp_path):
        _v, create = _judge_on(tmp_path, "newsletter", _answer(), hook_text="Payroll eats first")
        text = create.call_args_list[1][1]["messages"][0]["content"][0]["text"]
        assert "The cover's headline: \"Payroll eats first\"" in text
        assert "Reading the headline together with the image, would a viewer get the GIST" in text
        assert "Does a face show a clear, specific emotion readable at 400x225?" in text
        assert "brand_fit" in text

    def test_the_blind_call_still_never_sees_the_headline(self, tmp_path):
        _v, create = _judge_on(tmp_path, "newsletter", _answer(), hook_text="Payroll eats first")
        assert "Payroll eats first" not in json.dumps(create.call_args_list[0][1]["messages"])

    def test_a_blank_face_caps_scroll_stop_and_fails_a_cover(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter", _answer(face_emotion=False))
        assert verdict.rubric["scroll_stop"] == 3 and "scroll_stop" in verdict.failing

    def test_face_emotion_is_only_demanded_of_a_people_scene(self, tmp_path):
        concrete = ImageConcept(**{**_PEOPLE.to_dict(), "treatment": "concrete_scene",
                                   "specific_entities": ("unpaid invoices",),
                                   "visual_anchors": ("an agency owner",)})
        verdict, _ = _judge_on(tmp_path, "newsletter", _answer(face_emotion=False), concrete)
        assert verdict.acceptable

    @pytest.mark.parametrize("surface,fails", [("newsletter", True), ("post_image", True),
                                               ("carousel", False)])
    def test_scroll_stop_of_three_fails_covers_and_post_images(self, tmp_path, surface, fails):
        verdict, _ = _judge_on(tmp_path, surface, _answer(dict(_GOOD_RUBRIC, scroll_stop=3)))
        assert ("scroll_stop" in verdict.failing) is fails

    def test_brand_fit_below_three_fails_and_is_recorded(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter", _answer(dict(_GOOD_RUBRIC, brand_fit=2)))
        assert not verdict.acceptable and verdict.failing == ["brand_fit"]
        assert verdict.rubric["brand_fit"] == 2

    def test_an_unanswered_brand_fit_is_not_a_fail(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter", _answer())
        assert verdict.acceptable and verdict.rubric["brand_fit"] is None

    def test_the_repair_names_the_emotion_and_the_gold_accent(self):
        verdict = QualityVerdict(acceptable=False, failing=["scroll_stop", "brand_fit"],
                                 issues=["scroll_stop 3/5", "brand_fit 2/5"])
        directive = rubric_repair_directive(verdict, "flux", _PEOPLE, None)
        assert "a wince of dread shows on the face" in directive
        assert "warm gold accent" in directive


class TestRoundFourJudge:
    """Round 4 of #2241: emotion must match, the headline must be sans, generic images capped."""

    def test_the_new_questions_reach_the_targeted_judge(self, tmp_path):
        _v, create = _judge_on(tmp_path, "newsletter", _answer(), hook_text="Payroll eats first")
        text = create.call_args_list[1][1]["messages"][0]["content"][0]["text"]
        assert 'Does the visible emotion match "a wince of dread"?' in text
        assert "Is the headline set in a sans-serif typeface?" in text
        assert "reused unchanged on an unrelated business article" in text
        assert "GIST of the claim" in text

    def test_a_mismatched_emotion_fails_scroll_stop(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter", _answer(emotion_matches=False))
        assert verdict.rubric["scroll_stop"] == 3 and "scroll_stop" in verdict.failing

    def test_a_serif_headline_caps_brand_fit(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter",
                               _answer(dict(_GOOD_RUBRIC, brand_fit=5, text_accuracy=5),
                                       headline_sans_serif=False, text_seen="Payroll eats first"),
                               hook_text="Payroll eats first")
        assert verdict.rubric["brand_fit"] == 3

    def test_no_headline_means_no_typeface_cap(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter",
                               _answer(dict(_GOOD_RUBRIC, brand_fit=5), headline_sans_serif=False))
        assert verdict.rubric["brand_fit"] == 5

    def test_an_image_reusable_on_any_business_article_caps_specificity(self, tmp_path):
        """ed18's generic flip-chart presentation scored specificity 5."""
        verdict, _ = _judge_on(tmp_path, "newsletter", _answer(reusable_elsewhere=True))
        assert verdict.rubric["specificity"] == 3 and "specificity" in verdict.failing

    def test_a_text_failure_repair_names_papers_and_screens(self):
        verdict = QualityVerdict(acceptable=False, failing=["text_accuracy"],
                                 issues=["text_accuracy 2/5"])
        directive = rubric_repair_directive(verdict, "gpt-image", _PEOPLE, "Payroll eats first")
        assert ("remove every legible mark from papers and screens; the only text is the hook "
                '"Payroll eats first"') in directive

    @pytest.mark.parametrize("surface,env,expected", [
        ("newsletter", None, 2), ("post_image", None, 1), ("carousel", None, 1),
        ("newsletter", "1", 1), ("post_image", "2", 2),
    ])
    def test_covers_default_to_two_candidates(self, monkeypatch, surface, env, expected):
        if env is None:
            monkeypatch.delenv("IMAGE_GATE_CANDIDATES", raising=False)
        else:
            monkeypatch.setenv("IMAGE_GATE_CANDIDATES", env)
        assert image_gen._gate_candidates(_PEOPLE, surface) == expected

    def test_the_render_clause_states_the_sans_serif_too(self):
        assert "heavy geometric sans-serif in sentence case" in with_no_marks(
            "A cover.", "gpt-image", hook_text="Payroll eats first")
        assert "heavy geometric sans-serif in sentence case" in with_no_marks(
            "A cover.", "flux", hook_text="Payroll eats first")
