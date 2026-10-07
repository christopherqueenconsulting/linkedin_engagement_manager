"""``generate_image_for_post`` driving the treatment rotation end to end (#2241, anti-monotony).

Receipts are REAL files in a temp assets dir, so each post's rotation reads the posts before it.
Stage 1, the brief author, the AI renderer and the vision judge are mocked; the code-drawn cards,
the compositor and the panel variants are the real renderers.
"""

import dataclasses
import json
import os
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import image_gen, post_treatment as pt
from cqc_lem.utilities.ai.image_brief import ImageBrief
from cqc_lem.utilities.ai.image_concept import ImageConcept, fit_hook
from cqc_lem.utilities.ai.image_graphics import validate_graphic_facts

pytestmark = pytest.mark.unit

_STAT_SENTENCE = "Late invoices cost our agency 38% of revenue last quarter."
_OPINION = ("Most founders think they need a bigger content team. I think the real problem is "
            "that nobody owns the calendar. Give one person the calendar and output doubles.")
_REPORT = "We moved support to a shared queue. Response times fell and weekends came back."


def _concept(text: str, **overrides) -> ImageConcept:
    graphic = validate_graphic_facts(
        {"thesis_stat": {"label": "of revenue", "value": "38", "unit": "%",
                         "source_sentence": _STAT_SENTENCE}}, text) if _STAT_SENTENCE in text \
        else {}
    base = dict(thesis=text.split(".")[0] + ".", audience="agency owners",
                specific_entities=(), emotional_beat="quiet resolve",
                hook_phrase="Late invoices starve agencies", treatment="people_scene",
                treatment_rationale="", valence="positive", hook_shape="plain_claim",
                kicker="CASH FLOW", graphic=graphic, human_moment=True,
                archetype="people_scene", archetype_ranking=("people_scene",))
    base.update(overrides)
    return ImageConcept(**base)


def _analyze(text, *, recent_layouts=None, recent_shots=None, **_kwargs):
    from cqc_lem.utilities.ai.image_concept import assign_layout_and_cast

    return assign_layout_and_cast(_concept(text), "post_image", recent_layouts, None,
                                  recent_shots)


def _brief(text, *, surface, ratio, concept=None, **_kwargs):
    hook = fit_hook(concept.hook_phrase, concept.thesis) if concept and concept.hook_phrase \
        else None
    return ImageBrief(prompt="A candid documentary photograph of an agency owner.",
                      ratio="1:1" if hook else ratio, surface=surface, style_preset=surface,
                      focal_concept=getattr(concept, "thesis", "") or "x", concept=concept,
                      treatment=getattr(concept, "treatment", None), hook_text=hook)


class _Env:
    """The mocked world one test runs posts through."""

    def __init__(self, tmp_path, *, card_share=0.4, verdict=None, analyze=_analyze,
                 byline="Jane Doe"):
        self.assets = str(tmp_path / "assets")
        os.makedirs(self.assets, exist_ok=True)
        self.tmp = tmp_path
        self.card_share = card_share
        self.verdict = verdict or image_gen.QualityVerdict(acceptable=True)
        self.analyze = analyze
        self.byline = byline
        self.renders: list[dict] = []
        self.clock = 1_000_000

    def _render(self, prompt, *, ratio="1:1", **_kwargs):
        from PIL import Image

        path = str(self.tmp / f"scene_{len(self.renders)}.png")
        Image.new("RGB", (1024, 1024) if ratio == "1:1" else (1024, 1280),
                  (120, 140, 160)).save(path)
        self.renders.append({"prompt": prompt, "ratio": ratio})
        return path, "gpt-image"

    def post(self, text, **patches):
        from cqc_lem.utilities.media_provenance import brief_receipt_path
        from cqc_lem.utilities.post_image import generate_image_for_post

        profile = type("Profile", (), {"full_name": self.byline})()
        with ExitStack() as stack:
            for target, value in {
                "cqc_lem.assets_dir": self.assets,
                "cqc_lem.utilities.post_image.assets_dir": self.assets,
                "cqc_lem.utilities.ai.image_gen.assets_dir": self.assets,
            }.items():
                stack.enter_context(patch(target, value))
            stack.enter_context(patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user",
                                      return_value=profile))
            stack.enter_context(patch(
                "cqc_lem.utilities.avatar.guardrails.resolve_avatar_for_concept",
                return_value=None))
            stack.enter_context(patch("cqc_lem.utilities.ai.image_concept."
                                      "analyze_content_for_image", side_effect=self.analyze))
            stack.enter_context(patch("cqc_lem.utilities.ai.image_brief.build_image_brief",
                                      side_effect=_brief))
            stack.enter_context(patch("cqc_lem.utilities.brand_kit.brand_clause_for_user",
                                      return_value=""))
            stack.enter_context(patch("cqc_lem.utilities.brand_kit.card_share_for_user",
                                      return_value=self.card_share))
            stack.enter_context(patch("cqc_lem.utilities.ai.post_treatment.pick_quote",
                                      side_effect=lambda c, t="", user_id=None: (c[0], "llm")))
            stack.enter_context(patch.object(image_gen, "_render_with_backend",
                                             side_effect=self._render))
            stack.enter_context(patch.object(image_gen, "inspect_render_quality",
                                             return_value=self.verdict))
            stack.enter_context(patch("cqc_lem.utilities.observability."
                                      "track_image_gate_verdict"))
            for target, value in patches.items():
                stack.enter_context(patch(target, **value))
            url, reason = generate_image_for_post(7, text)
            receipt = None
            if url:
                path = brief_receipt_path(url)
                self.clock += 10
                os.utime(path, (self.clock, self.clock))
                with open(path, encoding="utf-8") as handle:
                    receipt = json.load(handle)
        return url, reason, receipt


def _no_triple(values):
    return not any(values[i] and values[i] == values[i + 1] == values[i + 2]
                   for i in range(len(values) - 2))


@pytest.mark.parametrize("share", [0.4, 1.0])
def test_a_six_post_sequence_rotates_and_never_repeats_a_dimension_three_times(tmp_path, share):
    env = _Env(tmp_path, card_share=share)
    texts = [_STAT_SENTENCE + " Deposits fixed it.", _OPINION, _REPORT,
             _STAT_SENTENCE + " We changed terms.", _OPINION, _REPORT]
    rhythms = []
    for text in texts:
        url, reason, receipt = env.post(text)
        assert url and reason is None
        rhythms.append(receipt["rhythm"])
    for dim in pt.RHYTHM_DIMENSIONS:
        values = [r[dim] for r in rhythms]
        assert _no_triple(values), (dim, values)
    treatments = [r["treatment"] for r in rhythms]
    assert len(set(treatments)) >= (3 if share < 1 else 2), treatments
    assert treatments.count("typeset_card") <= 4, "the gate caps a full share at 2 in a row"
    panels = [r["panel"] for r in rhythms if r["panel"]]
    assert len(set(panels)) >= 2, panels
    for rhythm in rhythms:
        assert set(pt.RHYTHM_DIMENSIONS) <= set(rhythm)
        assert {"card_share", "card_wanted", "chain", "fallbacks", "rerolled"} <= set(rhythm)


def test_card_share_is_honoured_over_ten_posts(tmp_path):
    env = _Env(tmp_path, card_share=0.4)
    treatments = [env.post(_REPORT)[2]["rhythm"]["treatment"] for _ in range(10)]
    assert treatments.count("typeset_card") == 4, treatments
    assert _no_triple(treatments), treatments


def _force_chain(*chain):
    real = pt.plan_post_rhythm

    def planned(*args, **kwargs):
        rhythm = real(*args, **kwargs)
        plan = dataclasses.replace(rhythm.plan, chain=tuple(chain))
        return dataclasses.replace(rhythm, plan=plan)

    return {"cqc_lem.utilities.ai.post_treatment.plan_post_rhythm": {"side_effect": planned}}


def test_a_data_card_is_the_stat_card_with_its_source_line(tmp_path):
    env = _Env(tmp_path)
    url, _, receipt = env.post(_STAT_SENTENCE + " Deposits fixed it.",
                               **_force_chain("data_card", "photo_only"))
    assert receipt["rhythm"]["treatment"] == "data_card"
    assert receipt["archetype_rendered"] == "stat_card"
    assert receipt["graphic_facts"][0]["display"] == "38%"
    assert receipt["graphic_facts"][0]["source_sentence"] == _STAT_SENTENCE
    assert receipt["prompt"] == "", "no render prompt was authored for a $0 card"
    assert env.renders == [], "no AI render"


def test_a_refused_stat_falls_to_the_next_treatment_and_records_why(tmp_path):
    def analyze(text, **_kwargs):
        broken = {"stat": {"label": "of revenue", "value": "99", "unit": "%", "display": "99%",
                           "source_sentence": _STAT_SENTENCE}}
        return _concept(text, graphic=broken)

    env = _Env(tmp_path, analyze=analyze)
    url, _, receipt = env.post(_STAT_SENTENCE, **_force_chain("data_card", "photo_only"))
    assert url and receipt["rhythm"]["treatment"] == "photo_only"
    fallback = receipt["rhythm"]["fallbacks"][-1]
    assert fallback["treatment"] == "data_card" and "trace" in fallback["reason"]


def test_a_quote_card_quotes_the_post_verbatim_over_the_byline(tmp_path):
    env = _Env(tmp_path)
    url, _, receipt = env.post(_OPINION, **_force_chain("quote_card", "photo_only"))
    rhythm = receipt["rhythm"]
    assert rhythm["treatment"] == "quote_card"
    assert rhythm["quote"] in _OPINION and rhythm["quote_pick"] == "llm"
    assert rhythm["opinion"].startswith("stance marker")
    assert receipt["graphic_facts"][0]["text"] == rhythm["quote"]
    assert rhythm["panel"] is None and rhythm["grade"] is None


def test_a_non_verbatim_quote_falls_through(tmp_path):
    env = _Env(tmp_path)
    url, _, receipt = env.post(
        _OPINION, **_force_chain("quote_card", "photo_only"),
        **{"cqc_lem.utilities.ai.post_treatment.pick_quote": {
            "return_value": ("Nobody owns your calendar.", "llm")}})
    assert receipt["rhythm"]["treatment"] == "photo_only"
    assert receipt["rhythm"]["fallbacks"][-1] == {
        "treatment": "quote_card", "reason": "the quote is not a verbatim sentence of the post"}


def test_a_report_is_not_quoted_and_says_so(tmp_path):
    env = _Env(tmp_path)
    _, _, receipt = env.post(_REPORT)
    skipped = {f["treatment"]: f["reason"] for f in receipt["rhythm"]["fallbacks"]}
    assert skipped["quote_card"] == "not an opinion post"
    assert skipped["data_card"] == "no verified thesis stat"


def test_photo_only_renders_the_scene_alone_with_its_grade(tmp_path):
    env = _Env(tmp_path)
    url, _, receipt = env.post(_REPORT, **_force_chain("photo_only"))
    assert receipt["rhythm"]["treatment"] == "photo_only"
    assert receipt["hook_text"] is None and receipt["rhythm"]["panel"] is None
    assert env.renders[0]["ratio"] == "4:5", "no type panel, so the post's own ratio"
    grade = receipt["rhythm"]["grade"]
    assert grade and "Photo grade:" in env.renders[0]["prompt"]
    assert receipt["rhythm"]["shot"], "a people scene records its framing"


def test_a_typeset_card_uses_the_rotated_panel_and_records_it(tmp_path):
    env = _Env(tmp_path)
    url, _, receipt = env.post(_REPORT, **_force_chain("typeset_card"))
    rhythm = receipt["rhythm"]
    assert rhythm["treatment"] == "typeset_card" and rhythm["panel"] in (
        "charcoal", "off_white", "gold")
    assert rhythm["layout"] in ("split_top", "split_bottom") and rhythm["grade"]
    assert env.renders[0]["ratio"] == "1:1"


def test_a_typeset_card_never_draws_the_stat_card(tmp_path):
    def analyze(text, **_kwargs):
        return _concept(text, archetype="stat_card",
                        archetype_ranking=("stat_card", "people_scene"))

    env = _Env(tmp_path, analyze=analyze)
    _, _, receipt = env.post(_STAT_SENTENCE, **_force_chain("typeset_card"))
    assert receipt.get("archetype_rendered") == "people_scene"
    assert receipt["concept"]["archetype_ranking"] == ["people_scene"]


def test_without_a_concept_the_typeset_card_ships_as_photo_only(tmp_path):
    env = _Env(tmp_path, analyze=lambda text, **_k: None)
    url, _, receipt = env.post(_REPORT, **_force_chain("typeset_card"))
    assert url and receipt["rhythm"]["treatment"] == "photo_only"
    assert "no headline" in receipt["rhythm"]["fallbacks"][-1]["reason"]


def test_a_rejected_ai_render_falls_only_to_a_zero_cost_card(tmp_path):
    rejected = image_gen.QualityVerdict(acceptable=False, rubric={"specificity": 2},
                                        failing=["specificity"], issues=["specificity 2/5"])

    def judge(path, *_args, **kwargs):
        return (image_gen.QualityVerdict(acceptable=True)
                if "graphic" in os.path.basename(path) or "img_" in os.path.basename(path)
                else rejected)

    env = _Env(tmp_path)
    with patch.object(image_gen, "IMAGE_GATE_MAX_ATTEMPTS", 1):
        url, reason, receipt = env.post(
            _STAT_SENTENCE + " Deposits fixed it.",
            **_force_chain("typeset_card", "photo_only", "data_card"),
            **{"cqc_lem.utilities.ai.image_gen.inspect_render_quality": {"side_effect": judge}})
    assert url and receipt["rhythm"]["treatment"] == "data_card"
    assert len(env.renders) == 1, "photo_only was never rendered after the spend"
    assert receipt["rhythm"]["fallbacks"][-1]["treatment"] == "typeset_card"


def test_a_rejection_with_no_card_left_ships_bare_and_logs_the_why(tmp_path):
    from cqc_lem.utilities.post_image import GATE_REJECTED_REASON

    rejected = image_gen.QualityVerdict(acceptable=False, rubric={"specificity": 2},
                                        failing=["specificity"], issues=["specificity 2/5"])
    env = _Env(tmp_path, verdict=rejected)
    with patch.object(image_gen, "IMAGE_GATE_MAX_ATTEMPTS", 1), \
         patch("cqc_lem.utilities.post_image.log_info") as info:
        url, reason, _ = env.post(_REPORT, **_force_chain("typeset_card", "photo_only"))
    assert url is None and reason == GATE_REJECTED_REASON
    assert len(env.renders) == 1
    record = info.call_args_list[-1]
    assert "rejected by the quality gate" in record.args[0]
    assert "specificity" in record.kwargs["gate_failing"]


def test_nothing_renders_and_the_last_reason_is_returned(tmp_path):
    env = _Env(tmp_path)
    url, reason, _ = env.post(_REPORT, **_force_chain("photo_only"),
                              **{"cqc_lem.utilities.ai.image_brief.build_image_brief": {
                                  "side_effect": RuntimeError("author down")}})
    assert url is None and reason == "Could not write an image prompt"


def test_receipts_are_read_newest_first_and_only_the_users_own(tmp_path):
    from cqc_lem.utilities.post_image import recent_post_receipts

    root = tmp_path / "images" / "post_previews" / "7"
    root.mkdir(parents=True)
    for n, user in enumerate((7, 8, 7)):
        path = root / f"img_{n}.brief.json"
        path.write_text(json.dumps({"user_id": user, "n": n}))
        os.utime(path, (1000 + n, 1000 + n))
    (root / "img_bad.brief.json").write_text("{not json")
    with patch("cqc_lem.utilities.post_image.assets_dir", str(tmp_path)):
        receipts = recent_post_receipts(7, 5)
    assert [r["n"] for r in receipts] == [2, 0]


def test_an_advisory_gate_rejection_is_never_stored(tmp_path):
    from cqc_lem.utilities.post_image import GATE_REJECTED_REASON

    rejected = image_gen.QualityVerdict(acceptable=False, rubric={"specificity": 2},
                                        failing=["specificity"], issues=["specificity 2/5"])
    env = _Env(tmp_path, verdict=rejected)
    with patch.object(image_gen, "IMAGE_QUALITY_GATE_SURFACES", ()):
        url, reason, _ = env.post(_STAT_SENTENCE + " Deposits fixed it.",
                                  **_force_chain("data_card"))
    assert url is None and reason == GATE_REJECTED_REASON


@pytest.fixture(autouse=True)
def _no_last_resort_card():
    """Pin the treatment chain itself: the $0 last-resort typeset card is disabled here.

    Since #2241 showcase B a post whose every treatment fails ships the last-resort card instead
    of nothing; these tests assert the chain's own outcomes and the bare-ship path that remains
    when even that card cannot be drawn. The card has its own tests
    (``test_post_last_resort_card.py``).
    """
    import unittest.mock

    from cqc_lem.utilities import post_image as _post_image

    with unittest.mock.patch.object(
            _post_image, "_render_last_resort_card",
            return_value=_post_image._Rendered(reason="last resort disabled in this test")):
        yield
