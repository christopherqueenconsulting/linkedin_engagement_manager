"""The ONE image-brief engine: validated lem-medium JSON briefs with a deterministic fallback."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai.image_brief import (
    _BRIEF_ATTEMPTS,
    _STYLE_PRESETS,
    _TREATMENT_TEMPLATES,
    CLICHE_OBJECTS,
    DEAD_QUALITY_TAGS,
    DEAD_STYLE_WORDS,
    NEGATION_MARKERS,
    ImageBrief,
    _fallback_brief,
    build_image_brief,
    check_prompt_against_concept,
    cliche_hit,
)
from cqc_lem.utilities.ai.image_concept import ImageConcept

_GOOD = {"focal_concept": "a founder reviewing a growth chart",
         "prompt": ("A confident founder stands beside a floor-to-ceiling window in a sunlit "
                    "industrial loft at golden hour, warm rim light, shallow depth of field, a "
                    "single bold teal accent in the scene, photorealistic editorial photograph.")}


def _resp(payload) -> SimpleNamespace:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.mark.unit
class TestBuildImageBrief:
    def test_valid_brief_comes_back_structured(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            brief = build_image_brief("My post about growth", surface="newsletter", ratio="16:9")

        assert isinstance(brief, ImageBrief)
        assert brief.prompt == _GOOD["prompt"]
        assert brief.focal_concept == _GOOD["focal_concept"]
        assert brief.ratio == "16:9" and brief.surface == "newsletter"
        kwargs = llm.call_args[1]
        assert kwargs["model"] == "lem-medium"
        assert kwargs["response_format"] == {"type": "json_object"}
        # The surface preset and the actual content must both reach the author.
        user_msg = kwargs["messages"][1]["content"]
        assert _STYLE_PRESETS["newsletter"] in user_msg
        assert "My post about growth" in user_msg

    def test_no_text_constraint_is_always_in_the_system_prompt(self):
        """The author must never NAME text/logos in a render prompt.

        FLUX summons what is named, so the system prompt instructs positive phrasing instead.
        """
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="post_image")
        system_msg = llm.call_args[1]["messages"][0]["content"]
        assert "NEVER mention" in system_msg
        assert "logos" in system_msg and "watermarks" in system_msg
        assert "plain unbranded clothing" in system_msg

    def test_a_fenced_json_reply_is_accepted_without_a_retry(self):
        """Issue #2013.

        `response_format={"type": "json_object"}` is a request, not a guarantee — lem-medium
        (a reasoning model) sometimes wraps the object in a ```json fence anyway, and a bare
        `json.loads` rejected that reply at character 0, burning the attempt and eventually the
        whole brief onto the deterministic fallback for a perfectly good brief.
        """
        fenced = "```json\n" + json.dumps(_GOOD) + "\n```"
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(fenced)) as llm:
            brief = build_image_brief("My post about growth", surface="post_image")
        assert llm.call_count == 1, "a fenced-but-valid reply must not cost a retry"
        assert not brief.fallback
        assert brief.prompt == _GOOD["prompt"]

    def test_invalid_json_retries_then_falls_back_deterministically(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp("not json at all")) as llm:
            brief = build_image_brief("Quarterly revenue lessons", surface="post_image")

        assert llm.call_count == _BRIEF_ATTEMPTS == 3, "the first try plus two retries"
        # A capitalised word is scrubbed as a possible name before the summary is interpolated.
        assert "revenue lessons" in brief.prompt
        # Positive phrasing only — FLUX ignores negation, so the fallback never says "No text".
        assert "plain unbranded" in brief.prompt
        assert brief.focal_concept  # never empty

    def test_refusal_text_is_rejected(self):
        refusal = {"focal_concept": "n/a",
                   "prompt": "I'm sorry, as an AI I cannot generate that image description " * 3}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(refusal)):
            brief = build_image_brief("content here", surface="carousel")
        assert "I'm sorry" not in brief.prompt

    def test_llm_outage_never_raises(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=RuntimeError("proxy down")):
            brief = build_image_brief("A story about mentorship", surface="video", ratio="9:16")
        assert "mentorship" in brief.prompt.lower()
        assert brief.ratio == "9:16"

    def test_unknown_surface_uses_default_preset(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)):
            brief = build_image_brief("content", surface="something_new")
        assert brief.style_preset == "post_image"

    def test_avatar_subject_clause_leads_the_context(self):
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="newsletter", avatar=avatar)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "it IS the author" in user_msg


@pytest.mark.unit
class TestRefusalFilterIsAnchoredToRefusals:
    """Regression: a bare "language model" ban rejected legitimate AI-topic briefs.

    Every brief an AI-focused author writes silently fell back to the deterministic template.
    """

    @pytest.mark.parametrize("prompt_text", [
        ("A photorealistic scene of an engineer studying a wall display of large language model "
         "routing costs, shallow depth of field, dramatic rim light, modern office at dusk."),
        ("A close-up of a founder at a whiteboard mapping an AI language model pipeline, warm "
         "window light, one bold orange accent, editorial photograph."),
    ])
    def test_legitimate_ai_subject_matter_is_accepted(self, prompt_text):
        payload = {"focal_concept": "LLM routing cost risk", "prompt": prompt_text}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(payload)):
            brief = build_image_brief("post about LLM cost routing", surface="post_image")
        assert brief.prompt == prompt_text, "an AI-topic brief must not fall back"

    @pytest.mark.parametrize("refusal", [
        "I'm sorry, I cannot create that image for you because it violates the guidelines here.",
        "As an AI language model, I am unable to generate the requested description at all.",
        "I can't fulfill this request, but here is a generic description of an office scene.",
    ])
    def test_actual_refusals_are_still_rejected(self, refusal):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp({"focal_concept": "n/a", "prompt": refusal * 2})):
            brief = build_image_brief("some content", surface="post_image")
        assert refusal not in brief.prompt


@pytest.mark.unit
class TestReasoningTokenBudget:
    """Regression: lem-medium is a REASONING model.

    At max_tokens=600 the whole budget went to thinking tokens, the response came back EMPTY with
    finish_reason='length', and every image silently rendered from the deterministic template.
    """

    def test_budget_leaves_room_for_reasoning_plus_json(self):
        from cqc_lem.utilities.ai.image_brief import _BRIEF_MAX_TOKENS
        # A real brief measured ~870 completion tokens including reasoning.
        assert _BRIEF_MAX_TOKENS >= 2000

    def test_the_budget_is_what_is_actually_sent(self):
        from cqc_lem.utilities.ai.image_brief import _BRIEF_MAX_TOKENS
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="post_image")
        assert llm.call_args[1]["max_tokens"] == _BRIEF_MAX_TOKENS

    def test_an_empty_response_retries_rather_than_crashing(self):
        empty = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=[empty, _resp(_GOOD)]) as llm:
            brief = build_image_brief("content", surface="post_image")
        assert llm.call_count == 2
        assert brief.prompt == _GOOD["prompt"], "a retry that succeeds must not fall back"

    def test_the_fallback_warning_says_why(self):
        empty = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=empty), \
             patch("cqc_lem.utilities.ai.image_brief.log_warning") as warn:
            build_image_brief("content", surface="post_image")
        reason = warn.call_args[1].get("reason", "")
        assert "empty response" in reason and "length" in reason


@pytest.mark.unit
class TestAnonymousPersonSteer:
    """A POSED stranger is the stock-photo look; with the author's own likeness, a person IS the point."""

    def test_no_avatar_allows_candid_people_but_never_a_posed_model(self):
        """Issue #2241: forbidding every identifiable person left only an object still-life.

        So every cover became a valve. Real people beat symbols; what reads as stock is the POSED
        model.
        """
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="newsletter", avatar=None)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "People may appear as real participants caught mid-task" in user_msg
        assert "never as a posed model smiling at the camera" in user_msg
        assert "do NOT make an anonymous person" not in user_msg

    def test_with_an_avatar_the_person_is_still_the_point(self):
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="newsletter", avatar=avatar)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "anonymous person" not in user_msg
        assert "it IS the author" in user_msg


@pytest.mark.unit
class TestHandsGuidance:
    """Owner verdict after the Aug 2026 bake-off: FLUX.1 stays; its one quality gap is hands.

    The brief must steer toward low-risk hand positions and never complex gestures.
    """

    def test_system_prompt_steers_hands_low_risk(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="post_image")
        sys_msg = llm.call_args[1]["messages"][0]["content"]
        assert "HANDS" in sys_msg
        assert "out of frame" in sys_msg
        assert "interlocked" in sys_msg  # the banned gesture class is named


_LLM = "cqc_lem.utilities.ai.ai_helper._call_llm"
_JUDGE = "cqc_lem.utilities.ai.client.client.chat.completions.create"


def _concept(**overrides) -> ImageConcept:
    fields = {"thesis": "Late invoices quietly starve a small agency's payroll",
              "audience": "agency owners",
              "specific_entities": ("unpaid invoices", "payroll run", "agency owner"),
              "emotional_beat": "quiet dread", "hook_phrase": "", "treatment": "concrete_scene",
              "treatment_rationale": "a tangible situation"}
    fields.update(overrides)
    return ImageConcept(**fields)


_GROUNDED = {"focal_concept": "an agency owner facing unpaid invoices before payroll",
             "prompt": ("A candid editorial photograph of an agency owner at a kitchen table "
                        "late at night, a stack of unpaid invoices fanned beside a printed "
                        "payroll run, soft lamp light from camera left, shot on a 35mm lens at "
                        "f/2, Kodak Portra 400 tones, subtle film grain."),
             "required_entities": ["unpaid invoices", "payroll run", "agency owner"],
             "hook_text": None}

_HOOK = "Payroll eats first"
_GRAPHIC = {"focal_concept": "the hook beside a stack of unpaid invoices",
            "prompt": (f'A designed editorial graphic for a LinkedIn newsletter cover: bold '
                       f'sans-serif type reading "{_HOOK}" set large in the left third, beside '
                       f'a photographic cutout of unpaid invoices clipped to a payroll run '
                       f'printout, on a flat navy color field with generous negative space.'),
            "required_entities": ["unpaid invoices", "payroll run"], "hook_text": _HOOK}


@pytest.mark.unit
class TestClicheList:
    """The ONE stock-symbol list (issue #2241), matched with inflections and article gaps."""

    @pytest.mark.parametrize("text", [
        "a dripping copper pipe", "industrial piping on the wall", "a brass valve",
        "a leaky faucet", "a cracked gear", "two gears meshing", "a glowing light bulb",
        "a single lightbulb", "a missing puzzle piece", "a chess board", "a robot arm",
        "a rocket launch", "an archery target", "a mountain summit at dawn", "an hourglass",
        "a row of dominoes", "a lock and key", "a crystal ball", "a magnifying glass",
        "a person at a laptop", "typing hands", "hands on a keyboard",
        "a coffee mug beside an open notebook",
    ])
    def test_stock_symbols_are_caught(self, text):
        assert cliche_hit(text), f"{text!r} names a stock symbol"

    @pytest.mark.parametrize("text", [
        "a data pipeline review", "their target audience list", "a brass saxophone",
        "a compassionate manager", "an amazed founder", "unpaid invoices on a desk",
        "a cognitive load chart", "a laptop on the table", "a notebook full of plans",
    ])
    def test_lookalikes_and_bare_office_nouns_are_not(self, text):
        assert cliche_hit(text) is None

    def test_every_cliche_is_named_to_the_author(self):
        from cqc_lem.utilities.ai.image_brief import _SYSTEM_PROMPT
        for term in CLICHE_OBJECTS:
            assert term in _SYSTEM_PROMPT

    @pytest.mark.parametrize("name,text", sorted(
        [(f"preset:{k}", v) for k, v in _STYLE_PRESETS.items()]
        + [(f"treatment:{k}", v) for k, v in _TREATMENT_TEMPLATES.items()]))
    def test_no_preset_or_treatment_offers_a_cliche_or_a_metaphor_menu(self, name, text):
        assert cliche_hit(text) is None, f"{name} names a stock symbol"
        assert "physical metaphor" not in text.lower()

    def test_presets_state_a_use_case_not_a_subject_menu(self):
        newsletter = _STYLE_PRESETS["newsletter"]
        assert "16:9" in newsletter and "400x225" in newsletter and "central 60%" in newsletter


# Issue #1992's five editions, re-briefed under the staged engine: each now has a grounded
# concept, and its golden brief is built from the entities the edition itself names.
_FIVE_EDITIONS = (
    ("The Routing Switch That Reduced Outreach Costs by 42%",
     _concept(thesis="Routing outreach by intent cut its cost 42% with the same replies",
              specific_entities=("outreach team", "routing rules", "42% cost drop"),
              treatment="editorial_graphic", hook_phrase="42% cheaper, same replies"),
     {"focal_concept": "the 42% hook beside an outreach team",
      "prompt": ('A designed editorial graphic for a LinkedIn newsletter cover: bold sans-serif '
                 'type reading "42% cheaper, same replies" set large in the left third, beside '
                 'a photographic cutout of an outreach team reviewing printed routing rules, on '
                 'a flat navy color field with generous negative space, high contrast.')}),
    ("My Multi-Agent Content System Broke Quietly",
     _concept(thesis="An automated content system failed silently for weeks",
              specific_entities=("content calendar", "draft queue", "editor")),
     {"focal_concept": "an editor finding blank weeks in the content calendar",
      "prompt": ("A candid editorial photograph of an editor frowning at a wall-sized printed "
                 "content calendar with whole weeks left blank, an empty draft queue tray "
                 "beside her, overcast window light, shot on a 35mm lens at f/2.8, muted color "
                 "grade, subtle film grain.")}),
    ("Audit AI LinkedIn Engagement to Cut Costs",
     _concept(thesis="Auditing engagement spend line by line finds the waste",
              specific_entities=("marketing lead", "monthly spend report", "comment replies"),
              treatment="people_scene"),
     {"focal_concept": "a marketing lead auditing the monthly spend report",
      "prompt": ("A candid documentary photograph of a marketing lead and her analyst leaning "
                 "over a printed monthly spend report on a meeting-room table, circling line "
                 "items, a stack of printed comment replies beside them, soft window light from "
                 "camera left, eye-level medium shot, 35mm f/2, natural skin texture.")}),
    ("The Overlooked Costs of Your AI LinkedIn Strategy",
     _concept(thesis="The API bill is not the real cost of an AI LinkedIn strategy",
              specific_entities=("API bill", "founder", "credit card statement"),
              visual_anchors=("a founder", "a credit card statement", "a printed usage bill")),
     {"focal_concept": "a founder comparing the API bill with the card statement",
      "prompt": ("A candid editorial photograph of a founder at a cluttered table holding a "
                 "credit card statement next to a printed usage bill marked in red pen, warm "
                 "evening lamp light, shot on a 50mm lens at f/2, tactile paper texture, subtle "
                 "film grain.")}),
    ("Spot the Leak in Your LinkedIn AI Budget",
     _concept(thesis="Unused seats are where an AI budget quietly goes",
              specific_entities=("budget spreadsheet", "finance manager", "unused seats")),
     {"focal_concept": "a finance manager highlighting unused seats",
      "prompt": ("A candid editorial photograph of a finance manager highlighting rows of "
                 "unused seats on a printed budget spreadsheet pinned to a corkboard, a "
                 "colleague looking over her shoulder, crisp morning window light, shot on a "
                 "35mm lens at f/2.8, natural skin texture, subtle film grain.")}),
)

# What the OLD engine shipped for the same five — the metaphor still-lifes of issue #2241.
_OLD_METAPHOR_BRIEFS = (
    {"focal_concept": "an industrial rotary switch",
     "prompt": ("A heavy industrial rotary selector switch mounted on a steel panel, one contact "
                "glowing warm, macro close-up, dramatic raking side light, shot on a 100mm "
                "macro lens at f/4, brushed metal texture, subtle film grain.")},
    {"focal_concept": "a row of dominoes",
     "prompt": ("A row of wooden dominoes on a dark table, the middle piece toppled while the "
                "final domino still stands, muted low-key lighting from camera left, shallow "
                "depth of field, shot on an 85mm lens at f/2.")},
    {"focal_concept": "a brass balance scale",
     "prompt": ("An antique brass balance scale on a wooden table, a stack of coins on one pan "
                "and a single feather on the other, warm directional light, shot on a 100mm "
                "macro lens at f/2.8, tactile aged metal texture.")},
    {"focal_concept": "a droplet from a copper pipe",
     "prompt": ("A single water droplet caught mid-fall from a corroded copper pipe joint into "
                "a growing puddle below, macro close-up, high-contrast floor-level spotlight, "
                "shot on a 100mm macro lens at f/2.8, crisp water texture.")},
)


@pytest.mark.unit
class TestFiveEditions:
    @pytest.mark.parametrize("title,concept,payload", _FIVE_EDITIONS)
    def test_a_grounded_brief_passes_first_time(self, title, concept, payload):
        with patch(_LLM, return_value=_resp(payload)) as llm:
            brief = build_image_brief(f"{title}\n\nBody", surface="newsletter", ratio="16:9",
                                      concept=concept)
        assert llm.call_count == 1, "a grounded brief must not retry or fall back"
        assert not brief.fallback
        assert brief.prompt == payload["prompt"]
        assert cliche_hit(brief.prompt) is None
        assert len(brief.required_entities) >= 2
        assert brief.treatment == concept.treatment

    @pytest.mark.parametrize("old", _OLD_METAPHOR_BRIEFS)
    def test_the_old_metaphor_still_life_is_now_rejected(self, old):
        concept = _FIVE_EDITIONS[4][1]
        with patch(_LLM, return_value=_resp(old)) as llm:
            brief = build_image_brief("Spot the Leak", surface="newsletter", ratio="16:9",
                                      concept=concept)
        assert llm.call_count == _BRIEF_ATTEMPTS
        assert brief.fallback
        assert cliche_hit(brief.prompt) is None


@pytest.mark.unit
class TestStage2Contract:
    def test_the_author_sees_use_case_treatment_analysis_and_an_excerpt(self):
        content = "Body sentence. " * 400  # ~6000 chars
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm:
            build_image_brief(content, surface="newsletter", ratio="16:9", concept=_concept(),
                              brand_kit="Palette: forest green and cream; type: Inter Bold")
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert f"USE CASE: {_STYLE_PRESETS['newsletter']}" in user_msg
        assert _TREATMENT_TEMPLATES["concrete_scene"] in user_msg
        assert "Late invoices quietly starve" in user_msg and "unpaid invoices" in user_msg
        assert "Brand: Palette: forest green and cream" in user_msg
        excerpt = user_msg.split("<content>")[1].split("</content>")[0]
        assert len(excerpt) == 3000, "the excerpt is capped, never the whole piece"

    def test_stage_one_runs_when_no_concept_is_passed(self):
        with patch("cqc_lem.utilities.ai.image_brief.analyze_content_for_image",
                   return_value=_concept()) as stage1, \
             patch(_LLM, return_value=_resp(_GROUNDED)):
            brief = build_image_brief("the piece", surface="post_image")
        assert stage1.call_args[0][0] == "the piece"
        assert stage1.call_args[1]["surface"] == "post_image"
        assert brief.concept == _concept()

    def test_a_raising_stage_one_still_briefs(self):
        with patch("cqc_lem.utilities.ai.image_brief.analyze_content_for_image",
                   side_effect=RuntimeError("boom")), \
             patch(_LLM, return_value=_resp(_GOOD)):
            brief = build_image_brief("the piece", surface="post_image")
        assert not brief.fallback and brief.concept is None

    def test_an_avatar_always_takes_people_scene(self):
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK)
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm:
            brief = build_image_brief("c", surface="newsletter", avatar=avatar, concept=concept)
        assert brief.treatment == "people_scene" and brief.hook_text is None
        assert _TREATMENT_TEMPLATES["people_scene"] in llm.call_args[1]["messages"][1]["content"]

    @pytest.mark.parametrize("surface", ["post_image", "carousel", "newsletter"])
    def test_a_cliche_is_rejected_on_every_surface(self, surface):
        piped = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + " Copper pipes run along the wall.")
        with patch(_LLM, side_effect=[_resp(piped), _resp(_GROUNDED)]) as llm:
            brief = build_image_brief("c", surface=surface, concept=_concept())
        assert llm.call_count == 2
        assert brief.prompt == _GROUNDED["prompt"]
        retry = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "REJECTED" in retry and "'pipes'" in retry

    def test_a_cliche_is_rejected_on_the_avatar_path_too(self):
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        bulb = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + " A glowing light bulb overhead.")
        with patch(_LLM, side_effect=[_resp(bulb), _resp(_GROUNDED)]) as llm:
            brief = build_image_brief("c", surface="newsletter", avatar=avatar,
                                      concept=_concept())
        assert llm.call_count == 2 and brief.prompt == _GROUNDED["prompt"]

    def test_fewer_than_two_entities_is_rejected_with_the_entities_named(self):
        generic = {"focal_concept": "a founder thinking",
                   "prompt": ("A candid editorial photograph of a founder looking out of a "
                              "rain-streaked window in a quiet loft, soft overcast light, shot "
                              "on a 35mm lens at f/2, subtle film grain, muted tones.")}
        with patch(_LLM, side_effect=[_resp(generic), _resp(_GROUNDED)]) as llm:
            brief = build_image_brief("c", surface="post_image", concept=_concept())
        retry = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "unpaid invoices; payroll run; agency owner" in retry
        assert set(brief.required_entities) == {"unpaid invoices", "payroll run", "agency owner"}

    def test_a_weak_concept_skips_entity_coverage(self):
        weak = _concept(specific_entities=("payroll run",), weak=True)
        with patch(_LLM, return_value=_resp(_GOOD)) as llm:
            brief = build_image_brief("c", surface="post_image", concept=weak)
        assert llm.call_count == 1 and not brief.fallback

    def test_quoted_text_is_rejected_off_the_graphic_treatment(self):
        quoted = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + ' A sticky note reads "PAY ME".')
        with patch(_LLM, side_effect=[_resp(quoted), _resp(_GROUNDED)]) as llm:
            brief = build_image_brief("c", surface="post_image", concept=_concept())
        assert llm.call_count == 2 and brief.hook_text is None
        assert "'PAY ME'" in llm.call_args_list[1][1]["messages"][1]["content"]

    def test_a_declared_hook_is_rejected_off_the_graphic_treatment(self):
        hooked = dict(_GROUNDED, hook_text="Payroll eats first")
        with patch(_LLM, side_effect=[_resp(hooked), _resp(_GROUNDED)]) as llm:
            build_image_brief("c", surface="post_image", concept=_concept())
        assert "only the editorial_graphic treatment has a hook" in \
            llm.call_args_list[1][1]["messages"][1]["content"]

    def test_the_graphic_treatment_carries_its_hook(self):
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK)
        with patch(_LLM, return_value=_resp(_GRAPHIC)) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=concept)
        assert llm.call_count == 1
        assert brief.hook_text == _HOOK and brief.treatment == "editorial_graphic"
        assert f'"{_HOOK}"' in llm.call_args[1]["messages"][1]["content"]

    def test_a_graphic_missing_or_misspelling_its_hook_is_rejected(self):
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK)
        wrong = dict(_GRAPHIC, prompt=_GRAPHIC["prompt"].replace(_HOOK, "Payrol eats first"))
        with patch(_LLM, side_effect=[_resp(wrong), _resp(_GRAPHIC)]) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=concept)
        assert llm.call_count == 2 and brief.prompt == _GRAPHIC["prompt"]
        assert "exactly as" in llm.call_args_list[1][1]["messages"][1]["content"]

    def test_a_graphic_quoting_a_second_string_is_rejected(self):
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK)
        extra = dict(_GRAPHIC, prompt=_GRAPHIC["prompt"] + ' A caption reads "Q3".')
        with patch(_LLM, side_effect=[_resp(extra), _resp(_GRAPHIC)]) as llm:
            build_image_brief("c", surface="newsletter", concept=concept)
        assert "other than the hook" in llm.call_args_list[1][1]["messages"][1]["content"]

    def test_avoid_terms_are_a_soft_steer_never_a_gate(self):
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=_concept(),
                                      avoid_terms=["concrete_scene: unpaid invoices on a table"])
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "visibly different in setting and composition" in user_msg
        assert llm.call_count == 1 and not brief.fallback

    def test_avoid_terms_never_reach_the_fallback(self):
        with patch(_LLM, side_effect=RuntimeError("down")):
            brief = build_image_brief("c", surface="newsletter", concept=_concept(),
                                      avoid_terms=["a prior gear"])
        assert brief.fallback and "a prior gear" not in brief.prompt


def brief_treatment_is_photographic(treatment: str) -> bool:
    concept = _concept(treatment=treatment, hook_phrase=_HOOK)
    return _fallback_brief("c", surface="newsletter", ratio="16:9", context="", concept=concept,
                           treatment=treatment).treatment in ("people_scene", "concrete_scene")


@pytest.mark.unit
class TestFallbackIsBuiltFromThePiece:
    _TREATMENTS = ("people_scene", "editorial_graphic", "concrete_scene", "metaphor_last_resort")

    def test_the_workshop_still_life_is_gone(self):
        from cqc_lem.utilities.ai import image_brief
        assert not hasattr(image_brief, "_NEWSLETTER_FALLBACK_SCENE")
        assert not hasattr(image_brief, "METAPHOR_FAMILIES")
        prompt = _fallback_brief("Spot the leak in your AI budget", surface="newsletter",
                                 ratio="16:9", context="").prompt.lower()
        for word in ("workshop", "still-life", "still life", "brass", "valve", "leak"):
            assert word not in prompt

    def test_a_concrete_fallback_names_the_entities(self):
        brief = _fallback_brief("c", surface="newsletter", ratio="16:9", context="",
                                concept=_concept())
        assert brief.prompt.startswith("A photorealistic candid editorial photograph for a "
                                       "LinkedIn newsletter cover of unpaid invoices and payroll "
                                       "run")
        assert "agency owner in the frame" in brief.prompt
        assert brief.fallback and brief.treatment == "concrete_scene"
        assert len(brief.required_entities) == 3

    def test_a_graphic_falls_back_to_a_photograph_never_a_cutout_of_a_number(self):
        """Gauntlet round 1: the graphic fallback rendered "a photographic cutout of $30K"."""
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK,
                           specific_entities=("$30K", "Terralogic", "diagnostic audit"),
                           visual_anchors=("a consultant", "a printed audit checklist"))
        brief = _fallback_brief("c", surface="newsletter", ratio="16:9", context="",
                                concept=concept)
        assert brief.treatment == "concrete_scene" and brief.hook_text is None
        assert '"' not in brief.prompt and "cutout" not in brief.prompt
        assert "30K" not in brief.prompt and "Terralogic" not in brief.prompt
        assert "a consultant and a printed audit checklist" in brief.prompt

    def test_a_graphic_with_no_anchors_falls_back_to_people(self):
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK,
                           specific_entities=("$30K", "Terralogic"))
        brief = _fallback_brief("c", surface="newsletter", ratio="16:9", context="",
                                concept=concept)
        assert brief.treatment == "people_scene"
        assert "30K" not in brief.prompt and "Terralogic" not in brief.prompt

    def test_the_thesis_summary_loses_its_names_and_numbers(self):
        concept = _concept(thesis="Terralogic saved $30K after a 53.7% audit drop at Kanopy Labs",
                           specific_entities=("Terralogic",))
        prompt = _fallback_brief("c", surface="post_image", ratio="1:1", context="",
                                 concept=concept).prompt
        for name in ("Terralogic", "30K", "53.7", "Kanopy", "Labs"):
            assert name not in prompt

    def test_a_people_fallback_uses_the_audience(self):
        brief = _fallback_brief("c", surface="post_image", ratio="1:1", context="",
                                concept=_concept(treatment="people_scene"))
        assert "agency owners caught mid-conversation around unpaid invoices" in brief.prompt

    def test_a_cliche_entity_is_never_interpolated(self):
        concept = _concept(specific_entities=("leaking pipes", "plumbing invoice", "van"),
                           thesis="Our plumbing firm lost money on leak callouts")
        prompt = _fallback_brief("c", surface="newsletter", ratio="16:9", context="",
                                 concept=concept).prompt
        assert cliche_hit(prompt) is None

    @pytest.mark.parametrize("treatment", _TREATMENTS)
    def test_every_fallback_obeys_the_shared_rules(self, treatment):
        concept = _concept(treatment=treatment, hook_phrase=_HOOK)
        prompt = _fallback_brief("a post about pipes, gears and valves", surface="newsletter",
                                 ratio="16:9", context="", concept=concept,
                                 treatment=treatment).prompt
        lowered = prompt.lower()
        assert cliche_hit(prompt) is None
        for marker in NEGATION_MARKERS:
            assert marker not in lowered
        for word in DEAD_STYLE_WORDS + DEAD_QUALITY_TAGS:
            assert word.lower() not in lowered
        assert '"' not in prompt, "a fallback is a photograph and never carries text"
        assert brief_treatment_is_photographic(treatment)

    def test_no_concept_scrubs_cliches_from_the_excerpt(self):
        prompt = _fallback_brief("Fix the leaking valve in your sales pipeline", surface="post_image",
                                 ratio="1:1", context="").prompt
        assert cliche_hit(prompt) is None and "sales pipeline" in prompt


@pytest.mark.unit
class TestPromptCheck:
    """Stage 3: deterministic first, then ONE lem-simple judge that fails open."""

    def test_deterministic_failures_never_reach_the_judge(self):
        with patch(_JUDGE) as judge:
            ok, why = check_prompt_against_concept("a valve in a factory " * 5, _concept(), None)
        assert not ok and "'valve'" in why
        judge.assert_not_called()

    def test_no_concept_runs_the_deterministic_half_only(self):
        with patch(_JUDGE) as judge:
            ok, _ = check_prompt_against_concept(_GROUNDED["prompt"], None, None)
        assert ok
        judge.assert_not_called()

    def test_the_judge_is_lem_simple_and_sees_the_thesis_and_prompt(self):
        reply = _resp({"guessable": True, "depicted_entities": ["unpaid invoices"]})
        with patch(_JUDGE, return_value=reply) as judge:
            ok, why = check_prompt_against_concept(_GROUNDED["prompt"], _concept(), None)
        assert ok and why == "judge passed"
        kwargs = judge.call_args[1]
        assert kwargs["model"] == "lem-simple"
        text = kwargs["messages"][0]["content"]
        assert "Late invoices quietly starve" in text and _GROUNDED["prompt"] in text

    def test_an_unguessable_prompt_fails_with_the_thesis_as_the_repair(self):
        reply = _resp({"guessable": False, "reason": "it reads as a generic late night"})
        with patch(_JUDGE, return_value=reply):
            ok, why = check_prompt_against_concept(_GROUNDED["prompt"], _concept(), None)
        assert not ok
        assert "generic late night" in why and "Late invoices quietly starve" in why

    @pytest.mark.parametrize("effect", [RuntimeError("proxy down"), None])
    def test_an_unreachable_or_unusable_judge_fails_open(self, effect):
        kwargs = ({"side_effect": effect} if effect else {"return_value": _resp("not json")})
        with patch(_JUDGE, **kwargs):
            ok, _ = check_prompt_against_concept(_GROUNDED["prompt"], _concept(), None)
        assert ok

    def test_a_failed_check_gets_one_repair_pass_through_the_author(self):
        repaired = dict(_GROUNDED, prompt=_GROUNDED["prompt"].replace("kitchen", "office"))
        verdict = _resp({"guessable": False, "reason": "too generic"})
        with patch(_JUDGE, return_value=verdict) as judge, \
             patch(_LLM, side_effect=[_resp(_GROUNDED), _resp(repaired)]) as llm:
            brief = build_image_brief("c", surface="post_image", concept=_concept())
        assert judge.call_count == 1, "the repaired brief is not judged again"
        assert llm.call_count == 2
        assert brief.prompt == repaired["prompt"]
        assert brief.prompt_check.startswith("repaired after: a stranger could not guess")
        assert "too generic" in llm.call_args_list[1][1]["messages"][1]["content"]

    def test_a_repair_that_falls_short_ships_the_judged_brief_not_the_fallback(self):
        verdict = _resp({"guessable": False, "reason": "too generic"})
        with patch(_JUDGE, return_value=verdict), \
             patch(_LLM, side_effect=[_resp(_GROUNDED), _resp("nope"), _resp("nope")]), \
             patch("cqc_lem.utilities.ai.image_brief.log_warning") as warn:
            brief = build_image_brief("c", surface="post_image", concept=_concept())
        assert not brief.fallback and brief.prompt == _GROUNDED["prompt"]
        warn.assert_not_called()

    def test_a_passing_check_is_recorded_on_the_brief(self):
        with patch(_JUDGE, return_value=_resp({"guessable": True})), \
             patch(_LLM, return_value=_resp(_GROUNDED)):
            brief = build_image_brief("c", surface="post_image", concept=_concept())
        assert brief.prompt_check == "judge passed"


@pytest.mark.unit
class TestBriefReceiptCarriesTheConcept:
    def test_the_receipt_records_concept_treatment_entities_and_hook(self, tmp_path):
        from cqc_lem.utilities import media_provenance as mp
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK)
        brief = ImageBrief(prompt="p", ratio="16:9", surface="newsletter",
                           style_preset="newsletter", focal_concept="f", concept=concept,
                           treatment="editorial_graphic", required_entities=("payroll run",),
                           hook_text=_HOOK, prompt_check="judge passed")
        with patch.object(mp, "_assets_root", return_value=str(tmp_path)):
            url = "https://x/api/assets?file_name=images/a.png"
            assert mp.write_brief_receipt(url, brief) is not None
            receipt = mp.read_brief_receipt(url)
        assert receipt["concept"]["hook_phrase"] == _HOOK
        assert receipt["treatment"] == "editorial_graphic"
        assert receipt["required_entities"] == ["payroll run"]
        assert receipt["hook_text"] == _HOOK and receipt["prompt_check"] == "judge passed"


# Gauntlet round 1 of #2241: the four real user-1 covers, as Stage 1 actually described them.
_FACTS_CONCEPT = _concept(
    thesis="Auditing AI tools reveals wasted spend and compliance risk",
    specific_entities=("$30K", "Terralogic", "GPT-5.2", "Stanford HAI", "diagnostic audit",
                       "2025 Edelman-LinkedIn B2B Thought Leadership Impact Report", "53.7%"),
    visual_anchors=("a marketing lead", "a printed audit checklist", "a cluttered meeting room"))
_ANCHORED = {"focal_concept": "a marketing lead working through an audit checklist",
             "prompt": ("A photorealistic candid photograph of a marketing lead in a cluttered "
                        "meeting room ticking through a printed audit checklist with a pencil, "
                        "soft window light from camera left, shot on a 35mm lens at f/2, visible "
                        "skin pores, fabric wear, subtle film grain.")}


@pytest.mark.unit
class TestFactsAreNeverDrawn:
    @pytest.mark.parametrize("name_phrase", [
        "beside a box representing GPT-5.2", "a faint Stanford HAI facade",
        "a tag showing $30K", "the Terralogic team", "a printed 2025 report",
        "a chart at 53.7% on the wall",
    ])
    def test_a_fact_name_or_number_in_the_prompt_is_rejected(self, name_phrase):
        bad = dict(_ANCHORED, prompt=_ANCHORED["prompt"] + f" Also {name_phrase}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_ANCHORED)]) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=_FACTS_CONCEPT)
        assert llm.call_count == 2 and brief.prompt == _ANCHORED["prompt"]
        retry = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "a fact the image cannot draw" in retry

    def test_a_number_inside_the_quoted_hook_is_allowed(self):
        concept = _concept(treatment="editorial_graphic", hook_phrase="53.7% never noticed",
                           specific_entities=("53.7%", "marketing team"),
                           visual_anchors=("a marketing team", "a printed survey"))
        graphic = {"focal_concept": "the hook beside a marketing team",
                   "prompt": ('A designed editorial graphic: bold sans-serif type reading "53.7% '
                              'never noticed" set large in the left third, beside a photographic '
                              'cutout of a marketing team around a printed survey, on a flat '
                              'navy color field with generous negative space.')}
        with patch(_LLM, return_value=_resp(graphic)) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=concept)
        assert llm.call_count == 1 and not brief.fallback

    def test_the_facts_reach_the_author_as_context_only(self):
        with patch(_LLM, return_value=_resp(_ANCHORED)) as llm:
            build_image_brief("c", surface="newsletter", concept=_FACTS_CONCEPT)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert '"facts_context_only_never_drawn": ["$30K", "Terralogic"' in user_msg
        assert '"visual_anchors_to_depict": ["a marketing lead"' in user_msg
        system = llm.call_args[1]["messages"][0]["content"]
        assert "NEVER put a company, brand, product, model or report name" in system

    def test_anchors_not_facts_drive_coverage(self):
        with patch(_LLM, return_value=_resp(_ANCHORED)):
            brief = build_image_brief("c", surface="newsletter", concept=_FACTS_CONCEPT)
        assert set(brief.required_entities) == {"a marketing lead", "a printed audit checklist",
                                                "a cluttered meeting room"}

    def test_without_anchors_only_plain_facts_stand_in(self):
        from cqc_lem.utilities.ai.image_brief import usable_anchors
        concept = _concept(specific_entities=("Terralogic", "$30K", "diagnostic audit",
                                              "GPT-5.2", "agency owner"))
        assert usable_anchors(concept) == ["diagnostic audit", "agency owner"]


@pytest.mark.unit
class TestWordsOnSurfaces:
    @pytest.mark.parametrize("phrase", [
        "a tablet displaying the Originality dashboard", "a report titled quarterly review",
        "a folder labelled urgent", "a sign reading open", "a magazine with text on its cover",
        "a screen showing the company website",
    ])
    def test_phrases_that_put_words_on_a_surface_are_rejected(self, phrase):
        bad = dict(_ANCHORED, prompt=_ANCHORED["prompt"] + f" Behind her, {phrase}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_ANCHORED)]) as llm:
            build_image_brief("c", surface="newsletter", concept=_FACTS_CONCEPT)
        assert "puts words on a surface" in llm.call_args_list[1][1]["messages"][1]["content"]

    @pytest.mark.parametrize("phrase", [
        "a man reading a printed report", "a blank monitor glowing softly",
        "a closed laptop with an abstract wallpaper",
    ])
    def test_reading_as_an_activity_and_blank_screens_pass(self, phrase):
        ok = dict(_ANCHORED, prompt=_ANCHORED["prompt"] + f" Nearby, {phrase}.")
        with patch(_LLM, return_value=_resp(ok)) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=_FACTS_CONCEPT)
        assert llm.call_count == 1 and not brief.fallback


@pytest.mark.unit
class TestRoundOneCliches:
    @pytest.mark.parametrize("text", [
        "a stack of cash, specifically bundles of hundred-dollar bills", "stacks of cash",
        "a pile of money", "crisp banknotes", "a pile of coins", "a piggy bank", "a money bag",
        "a calculator and a few coins", "two executives shaking hands", "a thumbs up",
        "a data visualization hologram", "a glowing dashboard", "abstract data streams",
    ])
    def test_money_and_stock_business_imagery_is_refused(self, text):
        assert cliche_hit(text)

    @pytest.mark.parametrize("text", ["a cash flow forecast on paper", "a calculator",
                                      "a single coin on a desk", "a utility bill"])
    def test_ordinary_neighbours_are_not(self, text):
        assert cliche_hit(text) is None


@pytest.mark.unit
class TestFallbackTellsWhy:
    def test_the_receipt_carries_every_attempts_rejection(self):
        piped = dict(_ANCHORED, prompt=_ANCHORED["prompt"] + " Copper pipes on the wall.")
        named = dict(_ANCHORED, prompt=_ANCHORED["prompt"] + " The Terralogic logo glows.")
        with patch(_LLM, side_effect=[_resp(piped), _resp(named), _resp("nope")]), \
             patch("cqc_lem.utilities.ai.image_brief.log_info") as info, \
             patch("cqc_lem.utilities.ai.image_brief.log_warning") as warn:
            brief = build_image_brief("c", surface="newsletter", concept=_FACTS_CONCEPT)
        assert brief.fallback and len(brief.rejections) == 3
        assert "'pipes'" in brief.rejections[0] and "'Terralogic'" in brief.rejections[1]
        assert brief.rejections[2].startswith("unparsable JSON")
        logged = info.call_args[1]
        assert "pipes" in logged["rejections"] and "Terralogic" in logged["rejections"]
        warn.assert_not_called()  # a validation fallback is the engine working, not an outage

    def test_an_all_outage_fallback_still_warns(self):
        with patch(_LLM, side_effect=RuntimeError("proxy down")), \
             patch("cqc_lem.utilities.ai.image_brief.log_warning") as warn:
            brief = build_image_brief("c", surface="newsletter", concept=_FACTS_CONCEPT)
        assert brief.fallback and warn.called

    def test_a_retry_that_succeeds_still_records_what_was_rejected(self):
        piped = dict(_ANCHORED, prompt=_ANCHORED["prompt"] + " Copper pipes on the wall.")
        with patch(_LLM, side_effect=[_resp(piped), _resp(_ANCHORED)]):
            brief = build_image_brief("c", surface="newsletter", concept=_FACTS_CONCEPT)
        assert not brief.fallback and len(brief.rejections) == 1

    def test_the_brand_clause_never_reaches_the_fallback(self):
        brand = "Brand palette: light gold; avoid: pipes, gears, lightbulbs"
        with patch(_LLM, side_effect=RuntimeError("down")):
            brief = build_image_brief("c", surface="newsletter", concept=_FACTS_CONCEPT,
                                      brand_kit=brand)
        assert "palette" not in brief.prompt.lower() and cliche_hit(brief.prompt) is None


@pytest.mark.unit
class TestPhotographicLanguage:
    """OpenAI's gpt-image guide recommends "photorealistic" plus real texture, no studio polish.

    https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide
    """

    def test_photorealistic_is_no_longer_a_dead_word(self):
        assert "photorealistic" not in DEAD_QUALITY_TAGS

    @pytest.mark.parametrize("treatment", ["people_scene", "concrete_scene"])
    def test_the_photographic_treatments_ask_for_real_texture(self, treatment):
        template = _TREATMENT_TEMPLATES[treatment].lower()
        assert "photorealistic" in template
        assert "fabric wear" in template and "studio polish" in template
