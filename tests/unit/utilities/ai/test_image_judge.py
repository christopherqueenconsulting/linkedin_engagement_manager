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
        # A paper anchor is never asked about (round 7): it is never drawn.
        assert "payroll run; agency owner" in text and "unpaid invoices;" not in text
        assert f'The cover\'s headline (typeset onto it by the system): "{_HOOK}"' in text
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

    def test_no_anchor_seen_is_advisory_only(self, tmp_path):
        """Round 8: specificity is the descriptors alone; anchors never cap it."""
        seen = {"unpaid invoices": False, "payroll run": False, "agency owner": False}
        verdict, _ = _judge(tmp_path, answer=_answer(entities_depicted=seen))
        assert verdict.acceptable and verdict.rubric["specificity"] == 5
        assert "advisory: no anchor visibly depicted" in verdict.issues

    def test_one_anchor_seen_is_enough_the_count_is_advisory(self, tmp_path):
        """Round 3: the headline carries the thesis; two anchors is no longer required."""
        seen = {"unpaid invoices": False, "payroll run": True, "agency owner": False}
        verdict, _ = _judge(tmp_path, answer=_answer(entities_depicted=seen))
        assert verdict.acceptable and verdict.rubric["specificity"] == 5

    def test_the_raw_render_must_carry_no_text_at_all(self, tmp_path):
        """Round 6: the headline is composited; ANY text the blind look finds is a defect."""
        verdict, _ = _judge(tmp_path, hook_text=_HOOK,
                            blind=f'A worried owner. The visible text reads "{_HOOK}".')
        assert not verdict.acceptable and verdict.failing == ["text_accuracy"]

    def test_an_exact_hook_passes(self, tmp_path):
        verdict, _ = _judge(tmp_path, hook_text=_HOOK,
                            answer=_answer(dict(_GOOD_RUBRIC, text_accuracy=5),
                                           text_seen="PAYROLL EATS FIRST."))
        assert verdict.acceptable

    def test_the_judges_own_transcription_no_longer_decides_text(self, tmp_path):
        """The targeted look sees the COMPOSITE, which carries the headline by design."""
        verdict, _ = _judge(tmp_path, answer=_answer(text_seen="Q3 REVENUE"))
        assert verdict.acceptable and verdict.rubric["text_accuracy"] is None

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
        assert "payroll run; agency owner" in directive
        assert "a brass valve" in directive

    def test_flux_repair_never_names_the_defect(self):
        directive = rubric_repair_directive(self._verdict(["no_cliche"], ["a brass valve"]),
                                            "flux", _CONCEPT, None)
        assert directive.startswith("Render this scene again with")
        assert "valve" not in directive and "payroll run" in directive

    def test_a_text_failure_never_asks_for_the_hook_to_be_rendered(self):
        directive = rubric_repair_directive(self._verdict(["text_accuracy"]), "gpt-image",
                                            _CONCEPT, _HOOK)
        assert _HOOK not in directive
        assert "the image carries no text at all — the headline is typeset later" in directive

    def test_a_text_failure_without_a_hook_asks_for_blank_surfaces(self):
        directive = rubric_repair_directive(self._verdict(["text_accuracy"]), "flux",
                                            _CONCEPT, None)
        assert "every paper and screen blank and unmarked" in directive

    def test_no_failing_criteria_falls_back_to_the_legacy_directive(self):
        verdict = QualityVerdict(acceptable=False, issues=["six fingers"])
        assert rubric_repair_directive(verdict, "flux", _CONCEPT, None) == \
            image_gen.repair_directive(["six fingers"], "flux", _CONCEPT.thesis)


class TestStagedGateLoop:
    def test_concept_hook_and_composite_reach_the_judge_but_never_the_renderer(self, tmp_path):
        from PIL import Image
        raw = tmp_path / "raw.png"
        Image.new("RGB", (1536, 1024), (230, 225, 210)).save(raw)
        info: dict = {}
        with patch.object(image_gen, "_render_with_backend",
                          return_value=(str(raw), "gpt-image")) as render, \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)) as judge:
            path = render_image_gated("p", surface="newsletter", concept=_CONCEPT,
                                      hook_text=_HOOK, layout="panel_right", render_info=info)
        assert judge.call_args[1]["concept"] is _CONCEPT
        assert judge.call_args[1]["hook_text"] == _HOOK
        assert judge.call_args[0][0] == str(raw), "the blind look and text check see the RAW"
        assert judge.call_args[1]["composite_path"] == path != str(raw)
        assert "hook_text" not in render.call_args[1] and _HOOK not in render.call_args[0][0]
        assert info["raw_render_path"] == str(raw)

    def test_a_compositing_failure_ships_the_raw_render(self):
        with patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/missing.png", "gpt-image")), \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)) as judge, \
             patch.object(image_gen, "log_warning") as warn:
            path = render_image_gated("p", surface="newsletter", concept=_CONCEPT,
                                      hook_text=_HOOK)
        assert path == "/tmp/missing.png" and judge.call_args[1]["composite_path"] is None
        assert warn.called

    @pytest.mark.parametrize("surface", ["carousel", "video", "thumbnail"])
    def test_no_headline_is_composited_off_covers_and_posts(self, surface):
        with patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/1.png", "gpt-image")), \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)) as judge:
            assert render_image_gated("p", surface=surface, concept=_CONCEPT,
                                      hook_text=_HOOK) == "/tmp/1.png"
        assert judge.call_args[1]["composite_path"] is None

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


class TestNoTextInAnyRender:
    """Round 6: the text exception is gone — image_compose sets the headline afterwards."""

    def test_with_no_marks_has_no_hook_parameter(self):
        import inspect
        assert "hook_text" not in inspect.signature(with_no_marks).parameters

    @pytest.mark.parametrize("backend,blanket", [("gpt-image", _NO_MARKS_GPT),
                                                 ("flux", _NO_MARKS_FLUX)])
    def test_every_render_gets_the_blanket_ban(self, backend, blanket):
        assert with_no_marks("A cover.", backend).endswith(blanket)

    def test_the_renderer_never_receives_a_headline(self):
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png") as gpt:
            image_gen._render_with_backend("A scene.")
        assert gpt.call_args[0][0].endswith(_NO_MARKS_GPT)


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
    def test_a_gist_that_does_not_read_is_advisory_only(self, tmp_path):
        verdict, _ = _judge(tmp_path, answer=_answer(thesis_inferable=False))
        assert verdict.rubric["specificity"] == 5
        assert "advisory: the gist does not read" in verdict.issues

    def test_the_targeted_judge_is_asked_about_anchors_and_inference(self, tmp_path):
        concept = ImageConcept(
            thesis="t", audience="a", specific_entities=("Terralogic", "$30K"),
            emotional_beat="e", hook_phrase="", treatment="people_scene",
            treatment_rationale="r", visual_anchors=("a marketing lead", "a crowded meeting room"))
        _verdict, create = _judge(tmp_path, concept=concept,
                                  answer=_answer(entities_depicted={"a marketing lead": True,
                                                                    "a crowded meeting room": True}))
        text = create.call_args_list[1][1]["messages"][0]["content"][0]["text"]
        assert "a marketing lead; a crowded meeting room" in text
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
        assert "The cover's headline (typeset onto it by the system): \"Payroll eats first\"" \
            in text
        assert "Could this whole cover — kicker, headline and scene together — sit unchanged" \
            in text
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
        assert 'Does the visible emotion match "a wince of dread" with a mixed valence' in text
        assert "Is the emotion authentic rather than exaggerated or cartoonish?" in text
        assert "5 = with its headline, a scroller would correctly guess this piece's argument" \
            in text
        assert "3 = the scene could sit on many unrelated posts" in text
        assert "GIST of the claim" in text
        assert "sans-serif" not in text and "reused unchanged" not in text

    def test_a_mismatched_emotion_fails_scroll_stop(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter", _answer(emotion_matches=False))
        assert verdict.rubric["scroll_stop"] == 3 and "scroll_stop" in verdict.failing

    def test_a_cartoonish_face_caps_craft(self, tmp_path):
        """Round 6: a man wailing over a bill; caricature faces."""
        verdict, _ = _judge_on(tmp_path, "newsletter", _answer(emotion_authentic=False))
        assert verdict.rubric["craft"] == 3 and "craft" in verdict.failing

    def test_no_headline_means_no_typeface_cap(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter",
                               _answer(dict(_GOOD_RUBRIC, brand_fit=5), headline_sans_serif=False))
        assert verdict.rubric["brand_fit"] == 5

    def test_the_reuse_cap_is_gone_the_descriptors_carry_it(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter", _answer(reusable_elsewhere=True))
        assert verdict.rubric["specificity"] == 5 and verdict.acceptable

    def test_a_text_failure_repair_names_papers_and_screens(self):
        verdict = QualityVerdict(acceptable=False, failing=["text_accuracy"],
                                 issues=["text_accuracy 2/5"])
        directive = rubric_repair_directive(verdict, "gpt-image", _PEOPLE, "Payroll eats first")
        assert "remove every legible mark from papers and screens" in directive

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

class TestRoundFiveJudge:
    """Round 5 of #2241: headline + image judged as ONE cover; emotion pushes forward."""

    def test_without_a_headline_the_judge_grades_the_image_alone(self, tmp_path):
        _v, create = _judge_on(tmp_path, "post_image", _answer())
        text = create.call_args_list[1][1]["messages"][0]["content"][0]["text"]
        assert "(none — judge the image alone)" in text
        assert "Does the headline alone name its subject" in text

    def test_a_headline_that_does_not_name_its_subject_caps_specificity_at_four(self, tmp_path):
        verdict, _ = _judge_on(tmp_path, "newsletter",
                               _answer(dict(_GOOD_RUBRIC, text_accuracy=5),
                                       headline_names_subject=False,
                                       text_seen="Payroll eats first"),
                               hook_text="Payroll eats first")
        assert verdict.rubric["specificity"] == 5 and verdict.acceptable
        assert "advisory: the headline does not name its subject" in verdict.issues

    @pytest.mark.parametrize("answer,weak", [
        (_answer(face_emotion=False), True), (_answer(emotion_matches=False), True),
        (_answer(issues=["Enhance emotional expression for stronger impact"]), True),
        (_answer(issues=["tighten the crop"]), False),
    ])
    def test_a_weak_expression_is_flagged(self, tmp_path, answer, weak):
        verdict, _ = _judge_on(tmp_path, "newsletter", answer)
        assert verdict.emotion_weak is weak


class TestEmotionCarriesForward:
    """ed16: both candidates and the retry came out neutral — nothing carried the push forward."""

    _WEAK = QualityVerdict(acceptable=False, failing=["specificity"], issues=["specificity 3/5"],
                           rubric={"specificity": 3}, emotion_weak=True)
    _PLAIN = QualityVerdict(acceptable=False, failing=["specificity"],
                            issues=["specificity 3/5"], rubric={"specificity": 3})

    def test_candidate_two_and_every_retry_carry_the_emotion_directive(self, monkeypatch):
        monkeypatch.setenv("IMAGE_GATE_CANDIDATES", "2")
        with patch.object(image_gen, "IMAGE_GATE_MAX_ATTEMPTS", 2), \
             patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/x.png", "gpt-image")) as render, \
             patch.object(image_gen, "inspect_render_quality", return_value=self._WEAK):
            render_image_gated("base", surface="newsletter", concept=_PEOPLE)
        prompts = [c[0][0] for c in render.call_args_list]
        directive = ("The expression must be clearly readable at thumbnail size but authentic "
                     "and restrained: a wince of dread, the face a real person would make, never "
                     "cartoonish or crying.")
        assert len(prompts) == 4
        assert directive not in prompts[0]
        assert all(directive in p for p in prompts[1:]), "candidate 2 and the retry both carry it"
        assert "The previous render was rejected on: specificity" in prompts[2], \
            "the retry carries the rubric repair as well"

    def test_no_directive_when_the_expression_was_fine(self, monkeypatch):
        monkeypatch.setenv("IMAGE_GATE_CANDIDATES", "2")
        with patch.object(image_gen, "IMAGE_GATE_MAX_ATTEMPTS", 1), \
             patch.object(image_gen, "_render_with_backend",
                          return_value=("/tmp/x.png", "gpt-image")) as render, \
             patch.object(image_gen, "inspect_render_quality", return_value=self._PLAIN):
            render_image_gated("base", surface="newsletter", concept=_PEOPLE)
        assert all("authentic and restrained" not in c[0][0] for c in render.call_args_list)

    def test_the_avatar_path_carries_it_too(self, monkeypatch):
        monkeypatch.setenv("IMAGE_GATE_CANDIDATES", "2")
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        with patch.object(image_gen, "IMAGE_GATE_MAX_ATTEMPTS", 1), \
             patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/1.png", True)) as lora, \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch.object(image_gen, "inspect_render_quality", return_value=self._WEAK):
            image_gen.render_avatar_image_gated("base", avatar=avatar, user_id=3,
                                                surface="newsletter", concept=_PEOPLE)
        assert "authentic and restrained" in lora.call_args_list[1][0][0]


class TestBrightEnoughForAWhiteFeed:
    def test_the_thumbnail_question_asks_about_the_white_feed(self, tmp_path):
        _v, create = _judge_on(tmp_path, "post_image", _answer())
        text = create.call_args_list[1][1]["messages"][0]["content"][0]["text"]
        assert "stands out in a white social feed" in text

    @pytest.mark.parametrize("surface,fails", [("post_image", True), ("newsletter", True),
                                               ("carousel", False)])
    def test_a_dark_render_fails_the_feed_surfaces(self, tmp_path, surface, fails):
        verdict, _ = _judge_on(tmp_path, surface, _answer(bright_enough=False))
        assert verdict.rubric["thumbnail_read"] == 3
        assert ("thumbnail_read" in verdict.failing) is fails

    def test_the_repair_asks_for_bright_light(self):
        verdict = QualityVerdict(acceptable=False, failing=["thumbnail_read"],
                                 issues=["thumbnail_read 3/5"])
        assert "bright, high-key" in rubric_repair_directive(verdict, "flux", _PEOPLE, None)



class TestRoundSevenJudge:
    def test_the_kicker_and_cover_reading_reach_the_judge(self, tmp_path):
        from dataclasses import replace
        concept = replace(_PEOPLE, kicker="AGENCY PAYROLL")
        _v, create = _judge_on(tmp_path, "newsletter", _answer(), concept=concept,
                               hook_text="Payroll eats first")
        text = create.call_args_list[1][1]["messages"][0]["content"][0]["text"]
        assert 'The cover\'s kicker (topic tag, typeset by the system): "AGENCY PAYROLL"' in text
        assert "Read the kicker, headline and scene together as one cover." in text
        assert ("the kicker + headline name the exact topic and the scene shows the human stakes "
                "of it") in text

    def test_kicker_and_signature_reach_the_compositor(self, tmp_path):
        from dataclasses import replace

        from PIL import Image
        raw = tmp_path / "raw.png"
        Image.new("RGB", (1536, 1024), (230, 225, 210)).save(raw)
        concept = replace(_CONCEPT, kicker="AGENCY PAYROLL")
        with patch.object(image_gen, "_render_with_backend",
                          return_value=(str(raw), "gpt-image")), \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)), \
             patch("cqc_lem.utilities.ai.image_compose.compose_headline",
                   return_value=str(tmp_path / "c.png")) as compose:
            render_image_gated("p", surface="newsletter", concept=concept, hook_text=_HOOK,
                               signature="Christopher Queen")
        assert compose.call_args[1]["kicker"] == "AGENCY PAYROLL"
        assert compose.call_args[1]["signature"] == "Christopher Queen"


@pytest.mark.unit
class TestRoundEightJudge:
    def test_advisory_issues_never_touch_a_score(self):
        from cqc_lem.utilities.ai.image_gen import advisory_issues
        notes = advisory_issues({"entities_depicted": {"a ledger": False},
                                 "thesis_inferable": False, "headline_names_subject": False},
                                ["a ledger"], "Who buys?")
        assert notes == ["advisory: no anchor visibly depicted",
                         "advisory: the gist does not read",
                         "advisory: the headline does not name its subject"]
        assert advisory_issues({"entities_depicted": {"a ledger": True},
                                "headline_names_subject": False}, ["a ledger"], None) == []

    @pytest.mark.parametrize("surface", ["newsletter", "post_image"])
    def test_a_composited_render_is_square(self, surface):
        from cqc_lem.utilities.ai.image_gen import _scene_ratio
        assert _scene_ratio("16:9", "Who buys?", surface) == "1:1"
        assert _scene_ratio("4:5", None, surface) == "4:5"

    def test_an_uncomposited_surface_keeps_its_ratio(self):
        from cqc_lem.utilities.ai.image_gen import _scene_ratio
        assert _scene_ratio("16:9", "Who buys?", "video") == "16:9"


@pytest.mark.unit
class TestRoundNineNoLabels:
    @pytest.mark.parametrize("backend", ["gpt-image", "flux"])
    def test_every_backend_refuses_captions_badges_and_signage(self, backend):
        from cqc_lem.utilities.ai.image_gen import with_no_marks
        marked = with_no_marks("A woman in a green jumper pointing at a shelf.", backend)
        assert ("No captions, titles, posters, signage, name badges, lanyards with text, or "
                "labels of any kind.") in marked

    def test_every_composite_gets_a_kicker(self):
        from cqc_lem.utilities.ai.image_gen import _kicker_for
        concept = ImageConcept(thesis="Payroll audits catch payroll errors", audience="",
                               specific_entities=(), emotional_beat="", hook_phrase="Who pays?",
                               treatment="people_scene", treatment_rationale="", weak=False)
        assert _kicker_for(concept) == "PAYROLL"


@pytest.mark.unit
class TestRoundTenVideoCaption:
    def test_a_video_frame_is_judged_with_its_caption_as_the_headline(self, tmp_path):
        verdict, create = _judge_on(tmp_path, "video", _answer(), hook_text="Payroll eats first")
        text = create.call_args_list[1][1]["messages"][0]["content"][0]["text"]
        assert "The video's opening caption" in text and '"Payroll eats first"' in text
        assert "it is NOT in this still" in text
        assert verdict.checked

    def test_the_caption_is_never_composited_nor_squares_the_frame(self, tmp_path):
        from cqc_lem.utilities.ai.image_gen import _composite, _scene_ratio
        raw = tmp_path / "f.png"
        raw.write_bytes(b"png")
        assert _composite(str(raw), "Payroll eats first", "video", None, None) is None
        assert _scene_ratio("16:9", "Payroll eats first", "video") == "16:9"

    def test_a_cover_still_reads_its_typeset_headline(self):
        from cqc_lem.utilities.ai.image_gen import _headline_line
        assert _headline_line("Who buys?", "newsletter") == (
            'The cover\'s headline (typeset onto it by the system): "Who buys?"')
        assert "none — judge the image alone" in _headline_line(None, "video")


# The exact r10/judged/001 blind description that failed text_accuracy on its own negative.
_R10_001_BLIND = ('The main subject is a man with glasses sitting at a table with three other '
                  'people, smiling at the camera. The setting appears to be a casual meeting or '
                  'collaborative workspace, with a mug visible on the table. Visible text: - '
                  '"There is no visible text."')


@pytest.mark.unit
class TestRoundElevenJudge:
    def test_the_judge_quoting_its_own_negative_is_not_stray_text(self):
        from cqc_lem.utilities.ai.image_gen import stray_texts
        assert stray_texts(_R10_001_BLIND) == []

    @pytest.mark.parametrize("negative", ["There is no visible text.", "no visible text", "None",
                                          "No text visible", "There is no text",
                                          "Visible text: none", "No text is visible in the image"])
    def test_negative_statements_are_never_stray(self, negative):
        from cqc_lem.utilities.ai.image_gen import stray_texts
        assert stray_texts(f'A man at a table. Visible text: "{negative}"') == []

    @pytest.mark.parametrize("real", ["OPEN LATE", "No entry", "None of the above"])
    def test_real_text_is_still_stray(self, real):
        from cqc_lem.utilities.ai.image_gen import stray_texts
        assert stray_texts(f'A door with a sign: "{real}".') == [real]

    def test_r10_001_passes_text_accuracy(self, tmp_path):
        verdict, _ = _judge(tmp_path, blind=_R10_001_BLIND)
        assert verdict.rubric["text_accuracy"] is None
        assert not any(i.startswith("stray text") for i in verdict.issues)

    @pytest.mark.parametrize("backend", ["gpt-image", "flux"])
    def test_every_render_states_the_no_tech_scene(self, backend):
        from cqc_lem.utilities.ai.image_gen import with_no_marks
        marked = with_no_marks("Two people talking in a warehouse aisle.", backend)
        assert marked.endswith("The scene contains no computers, laptops, screens, servers, cables "
                               "or code; people interact with each other in a real place.")

    def test_the_no_tech_clause_never_summons_a_screen_clause(self):
        from cqc_lem.utilities.ai.image_gen import with_no_marks
        marked = with_no_marks("Two people talking in a warehouse aisle.", "flux")
        assert marked.count("screens") == 2  # the blanket constraint + the no-tech clause only

    def _video(self, tmp_path, specificity, hook_text=None, **rubric):
        answer = _answer(rubric=dict(_GOOD_RUBRIC, specificity=specificity, **rubric))
        verdict, _ = _judge_on(tmp_path, "video", answer, hook_text=hook_text)
        return verdict

    def test_an_uncaptioned_video_frame_passes_at_specificity_3(self, tmp_path):
        assert self._video(tmp_path, 3).acceptable

    def test_an_uncaptioned_video_frame_still_fails_at_2(self, tmp_path):
        verdict = self._video(tmp_path, 2)
        assert not verdict.acceptable and "specificity" in verdict.failing

    def test_a_captioned_video_frame_keeps_the_floor_of_4(self, tmp_path):
        verdict = self._video(tmp_path, 3, hook_text="Payroll eats first")
        assert not verdict.acceptable and "specificity" in verdict.failing

    @pytest.mark.parametrize("criterion,score", [("no_cliche", 4), ("craft", 3)])
    def test_the_relaxed_floor_relaxes_nothing_else(self, tmp_path, criterion, score):
        verdict = self._video(tmp_path, 3, **{criterion: score})
        assert not verdict.acceptable and criterion in verdict.failing

    def test_a_cover_keeps_specificity_4(self, tmp_path):
        answer = _answer(rubric=dict(_GOOD_RUBRIC, specificity=3))
        verdict, _ = _judge_on(tmp_path, "newsletter", answer)
        assert not verdict.acceptable


@pytest.mark.unit
class TestRoundTwelveJudge:
    def test_an_overacted_expression_caps_craft(self, tmp_path):
        verdict, create = _judge(tmp_path, answer=_answer(expression_overacted=True))
        assert verdict.rubric["craft"] == 3 and not verdict.acceptable
        text = create.call_args_list[1][1]["messages"][0]["content"][0]["text"]
        assert "Is any expression overacted" in text

    def test_a_restrained_face_passes(self, tmp_path):
        verdict, _ = _judge(tmp_path, answer=_answer(expression_overacted=False))
        assert verdict.acceptable


@pytest.mark.unit
class TestRoundThirteenEmotionRepair:
    def test_good_news_repairs_to_the_literal_face_never_relief(self):
        from cqc_lem.utilities.ai.image_gen import _emotion_beat
        concept = ImageConcept(thesis="t", audience="", specific_entities=(),
                               emotional_beat="relief and urgency", hook_phrase="",
                               treatment="people_scene", treatment_rationale="", weak=False,
                               valence="positive")
        assert _emotion_beat(concept) == "a relaxed, genuine smile, eyes bright, shoulders loose"

    def test_other_valences_keep_their_beat(self):
        from cqc_lem.utilities.ai.image_gen import _emotion_beat
        concept = ImageConcept(thesis="t", audience="", specific_entities=(),
                               emotional_beat="quiet dread", hook_phrase="",
                               treatment="people_scene", treatment_rationale="", weak=False,
                               valence="negative")
        assert _emotion_beat(concept) == "quiet dread"
