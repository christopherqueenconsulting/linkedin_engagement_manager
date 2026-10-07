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
            build_image_brief("content", surface="carousel")
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
            brief = build_image_brief("My post about growth", surface="carousel")
        assert llm.call_count == 1, "a fenced-but-valid reply must not cost a retry"
        assert not brief.fallback
        assert brief.prompt == _GOOD["prompt"]

    def test_invalid_json_retries_then_falls_back_deterministically(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp("not json at all")) as llm:
            brief = build_image_brief("Quarterly revenue lessons", surface="carousel")

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
        ("A photorealistic scene of an engineer explaining large language model routing costs "
         "to a colleague, shallow depth of field, dramatic rim light, modern office at dusk."),
        ("A close-up of a founder describing an AI language model pipeline to her team, warm "
         "window light, one bold orange accent, editorial photograph."),
    ])
    def test_legitimate_ai_subject_matter_is_accepted(self, prompt_text):
        payload = {"focal_concept": "LLM routing cost risk", "prompt": prompt_text}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(payload)):
            brief = build_image_brief("post about LLM cost routing", surface="carousel")
        assert brief.prompt == prompt_text, "an AI-topic brief must not fall back"

    @pytest.mark.parametrize("refusal", [
        "I'm sorry, I cannot create that image for you because it violates the guidelines here.",
        "As an AI language model, I am unable to generate the requested description at all.",
        "I can't fulfill this request, but here is a generic description of an office scene.",
    ])
    def test_actual_refusals_are_still_rejected(self, refusal):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp({"focal_concept": "n/a", "prompt": refusal * 2})):
            brief = build_image_brief("some content", surface="carousel")
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
            build_image_brief("content", surface="carousel")
        assert llm.call_args[1]["max_tokens"] == _BRIEF_MAX_TOKENS

    def test_an_empty_response_retries_rather_than_crashing(self):
        empty = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=[empty, _resp(_GOOD)]) as llm:
            brief = build_image_brief("content", surface="carousel")
        assert llm.call_count == 2
        assert brief.prompt == _GOOD["prompt"], "a retry that succeeds must not fall back"

    def test_the_fallback_warning_says_why(self):
        empty = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=empty), \
             patch("cqc_lem.utilities.ai.image_brief.log_warning") as warn:
            build_image_brief("content", surface="carousel")
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
            build_image_brief("content", surface="carousel")
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
                        "late at night, jaw tight over the payroll run ahead, warm gold lamp light "
                        "from camera left, shot on a 35mm lens at f/2, Kodak Portra 400 tones, "
                        "subtle film grain."),
             "required_entities": ["payroll run", "agency owner"],
             "hook_text": None}

# Round 5: a cover carries no paper prop, so newsletter-surface tests use this paper-free twin.
_GROUNDED_COVER = {"focal_concept": "an agency owner wincing before payroll",
                   "prompt": ("A candid editorial photograph of an agency owner at a kitchen "
                              "table late at night, wincing at the payroll run ahead, warm gold "
                              "lamp light from camera left, shot on a 35mm lens at f/2, Kodak "
                              "Portra 400 tones, subtle film grain.")}

_HOOK = "Payroll eats first"
# Round 10: even a graphic's RENDER prompt describes only the picture — never "a LinkedIn
# newsletter cover" or "negative space" for a headline (gpt-image then designs a cover).
_GRAPHIC = {"focal_concept": "an agency owner on a flat charcoal field",
            "prompt": ('A candid documentary photograph: a photographic cutout of an agency owner '
                       'mid-wince, centred on a flat charcoal color field and filling the square '
                       'frame.'),
            "required_entities": ["agency owner"], "hook_text": None}


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
      "prompt": ('A candid documentary photograph: a photographic cutout of an outreach team '
                 'trading relieved grins, centred on a flat charcoal color field, high '
                 'contrast.')}),
    ("My Multi-Agent Content System Broke Quietly",
     _concept(thesis="An automated content system failed silently for weeks",
              specific_entities=("content calendar", "draft queue", "editor")),
     {"focal_concept": "an editor finding empty weeks on the wall planner",
      "prompt": ("A candid editorial photograph of an editor frowning at a wall planner with "
                 "whole weeks left empty, an empty queue tray beside her, a warm gold desk lamp, "
                 "overcast window light, shot on a 35mm lens at f/2.8, muted color grade, subtle "
                 "film grain.")}),
    ("Audit AI LinkedIn Engagement to Cut Costs",
     _concept(thesis="Auditing engagement spend line by line finds the waste",
              specific_entities=("marketing lead", "monthly spend report", "comment replies"),
              treatment="people_scene"),
     {"focal_concept": "a marketing lead in a tense standoff over spend",
      "prompt": ("A candid documentary photograph of a marketing lead and her analyst arguing "
                 "across a meeting-room table, her arms crossed in a tense standoff, a gold pen "
                 "rolling between them, window light from camera left, eye-level medium shot, "
                 "35mm f/2, natural skin texture.")}),
    ("The Overlooked Costs of Your AI LinkedIn Strategy",
     _concept(thesis="The API bill is not the real cost of an AI LinkedIn strategy",
              specific_entities=("API bill", "founder", "credit card statement"),
              visual_anchors=("a founder", "a credit card statement", "a printed usage bill")),
     {"focal_concept": "a founder wincing at the card in her hand",
      # Round 12: no credit card in hand — a held card is a symbolic prop.
      "prompt": ("A candid editorial photograph of a founder at a cluttered table wincing, her "
                 "hands empty, warm evening gold lamp light, shot on a 50mm lens at f/2, tactile "
                 "texture, subtle film grain.")}),
    ("Spot the Leak in Your LinkedIn AI Budget",
     _concept(thesis="Unused seats are where an AI budget quietly goes",
              specific_entities=("budget spreadsheet", "finance manager", "unused seats")),
     {"focal_concept": "a finance manager highlighting unused seats",
      "prompt": ("A candid editorial photograph of a finance manager frowning across a "
                 "meeting table at a colleague in a charcoal blazer, morning light, shot on a "
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
        assert len(brief.required_entities) >= 1, "one anchor in frame is enough since round 3"
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
            brief = build_image_brief("the piece", surface="carousel")
        assert stage1.call_args[0][0] == "the piece"
        assert stage1.call_args[1]["surface"] == "carousel"
        assert brief.concept == _concept()

    def test_a_caller_whose_stage_one_came_back_none_is_never_charged_twice(self):
        """Cost guard (issue #2241): None means "ran, nothing usable" — only NOT_ANALYZED runs it."""
        with patch("cqc_lem.utilities.ai.image_brief.analyze_content_for_image") as stage1, \
             patch(_LLM, return_value=_resp(_GOOD)):
            brief = build_image_brief("the piece", surface="post_image", concept=None)
        stage1.assert_not_called()
        assert brief.concept is None

    def test_a_caller_concept_is_used_as_is(self):
        with patch("cqc_lem.utilities.ai.image_brief.analyze_content_for_image") as stage1, \
             patch(_LLM, return_value=_resp(_GROUNDED)):
            brief = build_image_brief("the piece", surface="post_image", concept=_concept())
        stage1.assert_not_called()
        assert brief.concept == _concept()

    def test_the_sentinel_names_itself(self):
        from cqc_lem.utilities.ai.image_brief import NOT_ANALYZED
        assert repr(NOT_ANALYZED) == "NOT_ANALYZED"

    def test_a_raising_stage_one_still_briefs(self):
        with patch("cqc_lem.utilities.ai.image_brief.analyze_content_for_image",
                   side_effect=RuntimeError("boom")), \
             patch(_LLM, return_value=_resp(_GOOD)):
            brief = build_image_brief("the piece", surface="carousel")
        assert not brief.fallback and brief.concept is None

    def test_an_avatar_always_takes_people_scene(self):
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK)
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm:
            brief = build_image_brief("c", surface="newsletter", avatar=avatar, concept=concept)
        assert brief.treatment == "people_scene"
        assert _TREATMENT_TEMPLATES["people_scene"] in llm.call_args[1]["messages"][1]["content"]
        # Round 3: a COVER carries its hook whatever the treatment — so the avatar path's
        # unquoted fixture is refused until it quotes the hook, and the template ships.
        assert brief.hook_text == _HOOK

    @pytest.mark.parametrize("surface", ["post_image", "carousel", "newsletter"])
    def test_a_cliche_is_rejected_on_every_surface(self, surface):
        # The cover recipe (no paper props) covers post images too since round 5.
        base = _GROUNDED_COVER if surface in ("newsletter", "post_image") else _GROUNDED
        piped = dict(base, prompt=base["prompt"] + " Copper pipes run along the wall.")
        with patch(_LLM, side_effect=[_resp(piped), _resp(base)]) as llm:
            brief = build_image_brief("c", surface=surface, concept=_concept())
        assert llm.call_count == 2
        assert brief.prompt == base["prompt"]
        retry = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "REJECTED" in retry and "'pipes'" in retry

    def test_a_cliche_is_rejected_on_the_avatar_path_too(self):
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        bulb = dict(_GROUNDED_COVER,
                    prompt=_GROUNDED_COVER["prompt"] + " A glowing light bulb overhead.")
        with patch(_LLM, side_effect=[_resp(bulb), _resp(_GROUNDED_COVER)]) as llm:
            brief = build_image_brief("c", surface="newsletter", avatar=avatar,
                                      concept=_concept())
        assert llm.call_count == 2 and brief.prompt == _GROUNDED_COVER["prompt"]

    def test_fewer_than_two_entities_is_rejected_with_the_entities_named(self):
        generic = {"focal_concept": "a founder thinking",
                   "prompt": ("A candid editorial photograph of a founder looking out of a "
                              "rain-streaked window in a quiet loft, soft overcast light, shot "
                              "on a 35mm lens at f/2, subtle film grain, muted tones.")}
        with patch(_LLM, side_effect=[_resp(generic), _resp(_GROUNDED)]) as llm:
            brief = build_image_brief("c", surface="carousel", concept=_concept())
        retry = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "show at least one of: payroll run; agency owner" in retry
        # Paper anchors ("unpaid invoices") are never asked for since round 7.
        assert set(brief.required_entities) == {"payroll run", "agency owner"}

    def test_a_weak_concept_skips_entity_coverage(self):
        weak = _concept(specific_entities=("payroll run",), weak=True)
        with patch(_LLM, return_value=_resp(_GOOD)) as llm:
            brief = build_image_brief("c", surface="carousel", concept=weak)
        assert llm.call_count == 1 and not brief.fallback

    def test_quoted_text_is_rejected_off_the_graphic_treatment(self):
        quoted = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + ' A sticky note reads "PAY ME".')
        with patch(_LLM, side_effect=[_resp(quoted), _resp(_GROUNDED)]) as llm:
            brief = build_image_brief("c", surface="carousel", concept=_concept())
        assert llm.call_count == 2 and brief.hook_text is None
        assert "'PAY ME'" in llm.call_args_list[1][1]["messages"][1]["content"]

    def test_a_declared_hook_is_rejected_on_every_treatment(self):
        """Round 6: the render carries no text — the headline is composited by image_compose."""
        hooked = dict(_GROUNDED, hook_text="Payroll eats first")
        with patch(_LLM, side_effect=[_resp(hooked), _resp(_GROUNDED)]) as llm:
            build_image_brief("c", surface="carousel", concept=_concept())
        assert "the image carries no text — the headline is typeset later" in \
            llm.call_args_list[1][1]["messages"][1]["content"]

    def test_a_graphic_cover_keeps_its_hook_for_compositing_not_in_the_prompt(self):
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK)
        with patch(_LLM, return_value=_resp(_GRAPHIC)) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=concept)
        assert llm.call_count == 1 and not brief.fallback
        assert brief.hook_text == _HOOK and _HOOK not in brief.prompt
        user = llm.call_args[1]["messages"][1]["content"]
        assert f"HEADLINE (typeset onto the image later by the system — never draw it, never " \
               f"quote it): {_HOOK}" in user

    def test_a_prompt_quoting_the_headline_is_rejected(self):
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK)
        quoted = dict(_GRAPHIC, prompt=_GRAPHIC["prompt"] + f' Type reading "{_HOOK}" on the left.')
        with patch(_LLM, side_effect=[_resp(quoted), _resp(_GRAPHIC)]) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=concept)
        assert llm.call_count == 2 and brief.prompt == _GRAPHIC["prompt"]
        assert "carries no words at all" in llm.call_args_list[1][1]["messages"][1]["content"]

    def test_avoid_terms_are_a_soft_steer_never_a_gate(self):
        with patch(_LLM, return_value=_resp(_GROUNDED_COVER)) as llm:
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
        brief = _fallback_brief("c", surface="carousel", ratio="16:9", context="",
                                concept=_concept())
        assert brief.prompt.startswith("A photorealistic candid editorial photograph for a "
                                       "LinkedIn carousel slide of payroll run and agency owner")
        assert brief.fallback and brief.treatment == "concrete_scene"
        assert len(brief.required_entities) == 2

    def test_a_graphic_falls_back_to_a_photograph_never_a_cutout_of_a_number(self):
        """Gauntlet round 1: the graphic fallback rendered "a photographic cutout of $30K"."""
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK,
                           specific_entities=("$30K", "Terralogic", "diagnostic audit"),
                           visual_anchors=("a consultant", "a printed audit checklist"))
        brief = _fallback_brief("c", surface="newsletter", ratio="16:9", context="",
                                concept=concept)
        assert brief.treatment == "concrete_scene"
        # A cover keeps its hook for compositing; the render prompt carries no text at all.
        assert brief.hook_text == _HOOK and '"' not in brief.prompt
        assert "cutout" not in brief.prompt
        assert "30K" not in brief.prompt and "Terralogic" not in brief.prompt
        # A cover's fallback drops its paper anchor (round 5) and keeps the person.
        assert "a consultant" in brief.prompt and "checklist" not in brief.prompt

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
        prompt = _fallback_brief("c", surface="carousel", ratio="1:1", context="",
                                 concept=concept).prompt
        for name in ("Terralogic", "30K", "53.7", "Kanopy", "Labs"):
            assert name not in prompt

    def test_a_people_fallback_uses_the_audience(self):
        brief = _fallback_brief("c", surface="carousel", ratio="1:1", context="",
                                concept=_concept(treatment="people_scene"))
        assert "agency owners caught mid-conversation around payroll run" in brief.prompt

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
        # A fallback is a photograph with no text: the cover's headline is composited later.
        from cqc_lem.utilities.ai.image_brief import _quoted_strings
        assert _quoted_strings(prompt) == [] and _HOOK not in prompt
        assert brief_treatment_is_photographic(treatment)

    def test_no_concept_scrubs_cliches_from_the_excerpt(self):
        prompt = _fallback_brief("Fix the leaking valve in your sales pipeline", surface="carousel",
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
            brief = build_image_brief("c", surface="carousel", concept=_concept())
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
            brief = build_image_brief("c", surface="carousel", concept=_concept())
        assert not brief.fallback and brief.prompt == _GROUNDED["prompt"]
        warn.assert_not_called()

    def test_a_passing_check_is_recorded_on_the_brief(self):
        with patch(_JUDGE, return_value=_resp({"guessable": True})), \
             patch(_LLM, return_value=_resp(_GROUNDED)):
            brief = build_image_brief("c", surface="carousel", concept=_concept())
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
                        "meeting room, arms crossed, a gold pencil tucked behind her ear, "
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
            brief = build_image_brief("c", surface="carousel", concept=_FACTS_CONCEPT)
        assert llm.call_count == 2 and brief.prompt == _ANCHORED["prompt"]
        retry = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "a fact the image cannot draw" in retry

    def test_a_hooks_number_lives_in_the_headline_never_the_prompt(self):
        concept = _concept(treatment="people_scene", hook_phrase="53.7% never noticed",
                           specific_entities=("53.7%", "marketing team"),
                           visual_anchors=("a marketing team", "a printed survey"))
        ok = {"focal_concept": "a marketing team",
              "prompt": ("A photorealistic candid photograph of a marketing team around a "
                         "table, one frowning, a gold scarf, bright daylight, 35mm f/2.")}
        with patch(_LLM, return_value=_resp(ok)) as llm:
            brief = build_image_brief("c", surface="carousel", concept=concept)
        assert llm.call_count == 1 and "53.7" not in brief.prompt

    def test_the_facts_reach_the_author_as_context_only(self):
        with patch(_LLM, return_value=_resp(_ANCHORED)) as llm:
            build_image_brief("c", surface="carousel", concept=_FACTS_CONCEPT)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert '"facts_context_only_never_drawn": ["$30K", "Terralogic"' in user_msg
        assert '"visual_anchors_to_depict": ["a marketing lead"' in user_msg
        system = llm.call_args[1]["messages"][0]["content"]
        assert "NEVER put a company, brand, product, model or report name" in system

    def test_anchors_not_facts_drive_coverage(self):
        with patch(_LLM, return_value=_resp(_ANCHORED)):
            brief = build_image_brief("c", surface="carousel", concept=_FACTS_CONCEPT)
        # The checklist anchor is a prop (round 7) and is never asked for.
        assert set(brief.required_entities) == {"a marketing lead", "a cluttered meeting room"}

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
            build_image_brief("c", surface="carousel", concept=_FACTS_CONCEPT)
        assert "puts words on a surface" in llm.call_args_list[1][1]["messages"][1]["content"]

    @pytest.mark.parametrize("phrase", [
        "a man reading over a colleague's shoulder", "a colleague leaning in to listen",
        "a phone held face-down on the table",
    ])
    def test_reading_as_an_activity_and_blank_screens_pass(self, phrase):
        ok = dict(_ANCHORED, prompt=_ANCHORED["prompt"] + f" Nearby, {phrase}.")
        with patch(_LLM, return_value=_resp(ok)) as llm:
            brief = build_image_brief("c", surface="carousel", concept=_FACTS_CONCEPT)
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
            brief = build_image_brief("c", surface="carousel", concept=_FACTS_CONCEPT)
        assert brief.fallback and len(brief.rejections) == 3
        assert "'pipes'" in brief.rejections[0] and "'Terralogic'" in brief.rejections[1]
        assert brief.rejections[2].startswith("unparsable JSON")
        logged = info.call_args[1]
        assert "pipes" in logged["rejections"] and "Terralogic" in logged["rejections"]
        warn.assert_not_called()  # a validation fallback is the engine working, not an outage

    def test_an_all_outage_fallback_still_warns(self):
        with patch(_LLM, side_effect=RuntimeError("proxy down")), \
             patch("cqc_lem.utilities.ai.image_brief.log_warning") as warn:
            brief = build_image_brief("c", surface="carousel", concept=_FACTS_CONCEPT)
        assert brief.fallback and warn.called

    def test_a_retry_that_succeeds_still_records_what_was_rejected(self):
        piped = dict(_ANCHORED, prompt=_ANCHORED["prompt"] + " Copper pipes on the wall.")
        with patch(_LLM, side_effect=[_resp(piped), _resp(_ANCHORED)]):
            brief = build_image_brief("c", surface="carousel", concept=_FACTS_CONCEPT)
        assert not brief.fallback and len(brief.rejections) == 1

    def test_the_brand_clause_never_reaches_the_fallback(self):
        brand = "Brand palette: light gold; avoid: pipes, gears, lightbulbs"
        with patch(_LLM, side_effect=RuntimeError("down")):
            brief = build_image_brief("c", surface="carousel", concept=_FACTS_CONCEPT,
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


# Round 3 of #2241: the hook carries the thesis, the visual carries emotion and specificity.
_IDEA = ("An agency owner wincing at the payroll run ahead as the office empties, a warm gold "
         "lamp the only light.")
_COVER_CONCEPT = _concept(hook_phrase=_HOOK, treatment="people_scene",
                          emotional_beat="a wince of dread", chosen_idea=_IDEA,
                          rejected_ideas=("A stack of unpaid invoices under a paperweight.",))
_COVER = {"focal_concept": "an agency owner wincing at payroll",
          "prompt": ('A photorealistic close medium shot of an agency owner wincing at the '
                     'payroll run ahead, a warm gold lamp the only light, visible skin pores, '
                     '35mm f/2, a plain charcoal wall calm on the left.')}


@pytest.mark.unit
class TestEveryCoverCarriesAHook:
    def test_a_people_scene_cover_carries_the_hook_for_compositing(self):
        concept = _concept(hook_phrase=_HOOK, treatment="people_scene",
                           emotional_beat="a wince of dread", chosen_idea=_IDEA,
                           layout="panel_left")
        with patch(_LLM, return_value=_resp(_COVER)) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=concept)
        assert llm.call_count == 1 and not brief.fallback
        assert brief.hook_text == _HOOK and brief.treatment == "people_scene"
        assert _HOOK not in brief.prompt
        user = llm.call_args[1]["messages"][1]["content"]
        assert "NEGATIVE SPACE: keep the LEFT half of the frame calm" in user

    def test_a_cover_prompt_drawing_its_hook_is_rejected(self):
        drawn = dict(_COVER, prompt=_COVER["prompt"] + f' The headline "{_HOOK}" on the left.')
        with patch(_LLM, side_effect=[_resp(drawn), _resp(_COVER)]) as llm:
            build_image_brief("c", surface="newsletter", concept=_COVER_CONCEPT)
        assert "carries no words at all" in llm.call_args_list[1][1]["messages"][1]["content"]

    @pytest.mark.parametrize("surface", ["carousel", "video"])
    def test_other_surfaces_keep_the_hook_optional(self, surface):
        no_hook = dict(_COVER, prompt=_COVER["prompt"].split(" The headline")[0] + ".")
        with patch(_LLM, return_value=_resp(no_hook)) as llm:
            brief = build_image_brief("c", surface=surface, concept=_COVER_CONCEPT)
        assert brief.hook_text is None and llm.call_count == 1
        assert "COVER LAYOUT" not in llm.call_args[1]["messages"][1]["content"]

    def test_a_graphic_cover_uses_its_own_layout_not_the_cover_one(self):
        concept = _concept(treatment="editorial_graphic", hook_phrase=_HOOK)
        with patch(_LLM, return_value=_resp(_GRAPHIC)) as llm:
            build_image_brief("c", surface="newsletter", concept=concept)
        assert "COVER LAYOUT" not in llm.call_args[1]["messages"][1]["content"]


@pytest.mark.unit
class TestTheChosenIdeaAndTheEmotion:
    def test_the_author_builds_on_the_chosen_idea_and_shows_the_emotion(self):
        with patch(_LLM, return_value=_resp(_COVER)) as llm:
            build_image_brief("c", surface="newsletter", concept=_COVER_CONCEPT)
        user = llm.call_args[1]["messages"][1]["content"]
        assert f"CHOSEN VISUAL IDEA — build the image on this: {_IDEA}" in user
        assert '"emotional_beat_to_show": "a wince of dread"' in user
        system = llm.call_args[1]["messages"][0]["content"]
        assert "Build the image on the CHOSEN VISUAL IDEA" in system

    def test_the_people_template_demands_a_visible_specific_reaction(self):
        template = _TREATMENT_TEMPLATES["people_scene"]
        assert "a wince at a number, mid-laugh smile, a raised eyebrow at a colleague" in template
        assert "face reads at thumbnail size" in template and "no laptop or screen in frame" in template

    @pytest.mark.parametrize("text", [
        "a founder looking at a laptop", "a marketer typing on her laptop",
        "three colleagues focused on their laptops", "a manager with a neutral expression",
        "a professional typing at a desk",
    ])
    def test_laptop_as_the_action_and_blank_faces_are_cliches(self, text):
        assert cliche_hit(text)

    @pytest.mark.parametrize("text", ["a closed laptop on the table beside her",
                                      "a laptop pushed aside as she laughs"])
    def test_a_laptop_as_a_prop_is_fine(self, text):
        assert cliche_hit(text) is None

    def test_one_anchor_in_frame_is_enough_now(self):
        """Round 3: ed16 fell back twice for "depicts 1 of the anchors"."""
        one = {"focal_concept": "an agency owner",
               "prompt": ("A photorealistic close shot of an agency owner laughing in relief at "
                          "her kitchen table at dusk, a warm gold lamp beside her, 35mm f/2, "
                          "visible skin pores, subtle film grain.")}
        with patch(_LLM, return_value=_resp(one)) as llm:
            build_image_brief("c", surface="carousel", concept=_concept())
        assert llm.call_count == 1

    def test_no_anchor_at_all_is_still_rejected(self):
        none = {"focal_concept": "a quiet room",
                "prompt": ("A photorealistic wide shot of an empty seaside café at dawn, a gold "
                           "awning, soft light, 35mm f/2, subtle film grain, worn wooden chairs.")}
        with patch(_LLM, side_effect=[_resp(none), _resp(_GROUNDED)]) as llm:
            build_image_brief("c", surface="carousel", concept=_concept())
        assert "depicts none of the piece's visual anchors" in \
            llm.call_args_list[1][1]["messages"][1]["content"]

    @pytest.mark.parametrize("phrase", ["a printed three-step checklist", "a numbered list",
                                        "a page of bullet points", "a framed list of numbers"])
    def test_documents_carrying_numerals_or_list_markers_are_rejected(self, phrase):
        bad = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + f" On the wall, {phrase}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_GROUNDED)]) as llm:
            build_image_brief("c", surface="carousel", concept=_concept())
        assert "puts words on a surface" in llm.call_args_list[1][1]["messages"][1]["content"]

    def test_the_rejected_ideas_reach_the_receipt(self, tmp_path):
        from cqc_lem.utilities import media_provenance as mp
        with patch(_LLM, return_value=_resp(_COVER)):
            brief = build_image_brief("c", surface="newsletter", concept=_COVER_CONCEPT)
        with patch.object(mp, "_assets_root", return_value=str(tmp_path)):
            url = "https://x/api/assets?file_name=images/c.png"
            mp.write_brief_receipt(url, brief)
            receipt = mp.read_brief_receipt(url)
        assert receipt["concept"]["chosen_idea"] == _IDEA
        assert receipt["concept"]["rejected_ideas"] == list(_COVER_CONCEPT.rejected_ideas)


@pytest.mark.unit
class TestBrandAccent:
    def test_a_cover_without_a_brand_color_is_rejected(self):
        plain = dict(_COVER, prompt=_COVER["prompt"].replace("warm gold lamp", "desk lamp")
                     .replace("a plain charcoal wall", "a plain wall"))
        with patch(_LLM, side_effect=[_resp(plain), _resp(_COVER)]) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=_COVER_CONCEPT)
        assert brief.prompt == _COVER["prompt"]
        assert "no deliberate brand-color element" in \
            llm.call_args_list[1][1]["messages"][1]["content"]

    def test_the_brand_kit_decides_the_colors(self):
        from cqc_lem.utilities.ai.image_brief import brand_colors
        assert brand_colors("Palette: forest green and cream; type: Inter") == {"forest", "green",
                                                                                "cream"}
        assert brand_colors(None) == {"gold", "golden", "charcoal", "off-white"}

    def test_every_surface_is_asked_for_one_but_only_covers_are_gated(self):
        plain = dict(_GROUNDED, prompt=_GROUNDED["prompt"].replace("warm gold lamp", "lamp"))
        with patch(_LLM, return_value=_resp(plain)) as llm:
            brief = build_image_brief("c", surface="carousel", concept=_concept())
        assert llm.call_count == 1 and not brief.fallback
        assert "BRAND ACCENT: brand colors are ACCENTS, never the overall tone" in \
            llm.call_args[1]["messages"][1]["content"]


@pytest.mark.unit
class TestRoundThreeFallback:
    def test_the_fallback_builds_from_the_chosen_idea_and_keeps_the_hook(self):
        brief = _fallback_brief("c", surface="newsletter", ratio="16:9", context="",
                                concept=_COVER_CONCEPT, treatment="people_scene")
        assert "agency owner wincing at the payroll run ahead" in brief.prompt
        assert brief.hook_text == _HOOK and _HOOK not in brief.prompt
        assert "a SQUARE frame with the subject centred and filling it" in brief.prompt
        assert "caught mid-conversation around" not in brief.prompt, "never a bare anchor list"
        assert "gold accent" in brief.prompt

    def test_a_post_fallback_carries_no_hook(self):
        brief = _fallback_brief("c", surface="carousel", ratio="1:1", context="",
                                concept=_COVER_CONCEPT, treatment="people_scene")
        assert brief.hook_text is None and '"' not in brief.prompt

    def test_the_brand_kit_colors_the_fallback_accent_without_its_clause(self):
        brief = _fallback_brief("c", surface="carousel", ratio="1:1", context="",
                                concept=_COVER_CONCEPT, brand_kit="forest green; avoid: pipes")
        assert "deliberate forest accent" in brief.prompt or "deliberate green accent" in brief.prompt
        assert "avoid" not in brief.prompt and "pipes" not in brief.prompt


@pytest.mark.unit
class TestPromptCheckSeesTheHeadline:
    def test_the_judge_is_told_the_headline(self):
        with patch(_JUDGE, return_value=_resp({"guessable": True})) as judge:
            check_prompt_against_concept(_COVER["prompt"], _COVER_CONCEPT, _HOOK)
        text = judge.call_args[1]["messages"][0]["content"]
        assert f'The image\'s headline: "{_HOOK}"' in text
        assert "GIST of the claim" in text


@pytest.mark.unit
class TestPostImageGauntletOn2249:
    """The avatar brief carries the beat and refuses a posed author; five new clichés."""

    _AVATAR = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
    _POSED = {**_GROUNDED, "prompt": _GROUNDED["prompt"].replace(
        "at a kitchen table late at night", "smiling at the camera at a kitchen table")}

    @pytest.mark.parametrize("text", [
        "rows of server racks", "a long data center aisle", "blinking server lights overhead",
        "a wall of monitors", "a stock trading chart on screen",
    ])
    def test_the_gauntlet_scenes_are_cliches(self, text):
        assert cliche_hit(text), f"{text!r} came back from a text post's render"

    def test_the_avatar_directive_carries_the_concepts_beat(self):
        concept = _concept(emotional_beat="relief after finally saying no")
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm, patch(_JUDGE):
            build_image_brief("c", surface="post_image", avatar=self._AVATAR, concept=concept)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "show relief after finally saying no" in user_msg
        assert "never posing" in user_msg and "blank or softly out of focus" in user_msg
        assert "likeness is NOT available" not in user_msg

    def test_without_a_beat_the_directive_still_asks_for_a_reaction(self):
        from cqc_lem.utilities.ai.image_brief import avatar_directive
        assert "a specific, genuine reaction" in avatar_directive(None)
        assert "a specific, genuine reaction" in avatar_directive(_concept(emotional_beat=" "))

    @pytest.mark.parametrize("pose", [
        "smiling at the camera", "looking directly into the lens", "making eye contact",
        "posed by the window", "a coffee mug on the desk", "a headshot of the founder",
    ])
    def test_a_posed_author_is_refused_on_the_avatar_path(self, pose):
        posed = {**_GROUNDED, "prompt": _GROUNDED["prompt"].replace(
            "at a kitchen table late at night", f"{pose} at a kitchen table")}
        with patch(_LLM, return_value=_resp(posed)), patch(_JUDGE):
            brief = build_image_brief("c", surface="post_image", avatar=self._AVATAR,
                                      concept=_concept())
        assert brief.fallback is True
        assert any("the author is posed" in r for r in brief.rejections)

    def test_the_pose_rule_is_avatar_only(self):
        with patch(_LLM, return_value=_resp(self._POSED)), patch(_JUDGE):
            brief = build_image_brief("c", surface="post_image", concept=_concept())
        assert not any("the author is posed" in r for r in brief.rejections)


@pytest.mark.unit
class TestRoundFourBrief:
    """Round 4 of #2241: profile text never renders, documents stay unreadable, fixed type."""

    _PROFILE = SimpleNamespace(job_title="founder", industry="Artificial Intelligence, E-commerce",
                               company_name=None)

    def test_the_brief_author_gets_the_budget_and_low_effort(self):
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm:
            build_image_brief("c", surface="carousel", concept=_concept())
        assert llm.call_args[1]["max_tokens"] >= 6000
        assert llm.call_args[1]["reasoning_effort"] == "low"

    def test_a_length_cut_buys_one_extra_attempt(self):
        cut = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch(_LLM, side_effect=[cut, _resp("x"), _resp("x"), _resp(_GROUNDED)]) as llm:
            brief = build_image_brief("c", surface="carousel", concept=_concept())
        assert llm.call_count == 4 and not brief.fallback

    def test_only_one_extra_attempt_however_many_cuts(self):
        cut = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length")])
        with patch(_LLM, return_value=cut) as llm:
            brief = build_image_brief("c", surface="carousel", concept=_concept())
        assert llm.call_count == _BRIEF_ATTEMPTS + 1 and brief.fallback

    def test_profile_text_never_reaches_a_fallback_prompt(self):
        """ed18: the fallback opened "The author is in the Artificial Intelligence… industry"."""
        with patch(_LLM, side_effect=RuntimeError("down")) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=_COVER_CONCEPT,
                                      profile=self._PROFILE)
        assert "The author is" in llm.call_args[1]["messages"][1]["content"], "author context"
        assert brief.fallback
        assert "The author is" not in brief.prompt and "Make the visual feel" not in brief.prompt
        assert "Artificial Intelligence" not in brief.prompt

    def test_profile_text_an_author_echoes_is_stripped_from_its_prompt(self):
        echoed = dict(_GROUNDED, prompt=("The author is a founder in the Artificial Intelligence "
                                         "industry. Make the visual feel on-brand, credible. "
                                         + _GROUNDED["prompt"]))
        with patch(_LLM, return_value=_resp(echoed)):
            brief = build_image_brief("c", surface="carousel", concept=_concept(),
                                      profile=self._PROFILE)
        assert brief.prompt == _GROUNDED["prompt"]

    def test_strip_author_context(self):
        from cqc_lem.utilities.ai.image_brief import strip_author_context
        text = ("When a person appears in the image it IS the author — a man in his 40s. Describe "
                "that person consistently. A candid photo of a blank page.")
        assert strip_author_context(text) == "A candid photo of a blank page."

    @pytest.mark.parametrize("phrase", [
        "a printed billing dashboard screen", "a paper check", "a chart with labels",
        "an invoice showing the totals", "a framed bank cheque",
    ])
    def test_readable_documents_are_refused(self, phrase):
        bad = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + f" Beside it, {phrase}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_GROUNDED)]) as llm:
            build_image_brief("c", surface="carousel", concept=_concept())
        assert "asks for a readable document" in llm.call_args_list[1][1]["messages"][1]["content"]

    @pytest.mark.parametrize("prop", ["a blank invoice", "an out of focus report",
                                      "a sheet seen at a steep angle", "a clipboard"])
    def test_a_document_is_refused_even_with_an_unreadable_qualifier(self, prop):
        """Round 7: the renderer ignores "blank", so paper is refused outright."""
        bad = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + f" On the table, {prop}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_GROUNDED)]) as llm:
            build_image_brief("c", surface="carousel", concept=_concept())
        assert "paper, charts and code render legible text" in \
            llm.call_args_list[1][1]["messages"][1]["content"]

    def test_the_fallback_blanks_documents_and_states_the_type(self):
        brief = _fallback_brief("c", surface="newsletter", ratio="16:9", context="The author is x.",
                                concept=_COVER_CONCEPT, treatment="people_scene")
        assert "hands empty and relaxed" in brief.prompt and "mug" not in brief.prompt
        from cqc_lem.utilities.ai.image_brief import prop_failure
        assert prop_failure(brief.prompt) is None
        assert "sans-serif" not in brief.prompt, "type is set by image_compose, never rendered"
        assert "The author is" not in brief.prompt


@pytest.mark.unit
class TestNoPropsAnywhere:
    """Round 7 of #2241: stray text on props on every surface — the props are refused outright."""

    @pytest.mark.parametrize("surface", ["newsletter", "post_image", "carousel", "video",
                                         "thumbnail"])
    @pytest.mark.parametrize("prop", ["a proposal on a clipboard", "a cost checklist",
                                      "a blank form", "a monthly bill", "a ring binder",
                                      "a chart on the wall", "code on the wall",
                                      "a laptop open in front of her"])
    def test_props_are_refused_on_every_surface(self, surface, prop):
        bad = dict(_GROUNDED_COVER, prompt=_GROUNDED_COVER["prompt"] + f" Beside her, {prop}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_GROUNDED_COVER)]) as llm:
            build_image_brief("c", surface=surface, concept=_concept())
        reason = llm.call_args_list[1][1]["messages"][1]["content"]
        assert ("legible text" in reason or "screens are the stock" in reason
                or "face-down" in reason)

    def test_only_a_face_down_phone_survives(self):
        """Round 8: no screens at all — not even from behind (gpt-4.1: "laptop stock trope")."""
        from cqc_lem.utilities.ai.image_brief import prop_failure
        base = _GROUNDED_COVER["prompt"]
        assert prop_failure(base + " A phone held face-down beside her.") is None
        for screen in ("the back of a laptop on the table", "a screen facing away from camera",
                       "a keyboard", "a monitor", "a phone in her hand"):
            assert prop_failure(f"{base} {screen}."), screen

    def test_every_brief_is_told_the_alternatives(self):
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm:
            build_image_brief("c", surface="carousel", concept=_concept())
        user = llm.call_args[1]["messages"][1]["content"]
        assert "PROPS: no paper of any kind in frame" in user
        assert "No mugs, cups, coffee or tea. Hands are empty, gesturing, or a phone held " \
            "face-down" in user
        assert "people interacting and gesturing in a real environment" in user
        assert "no screens at all: no laptop, monitor, keyboard, tablet or computer" in user

    def test_prop_anchors_are_never_asked_for(self):
        from cqc_lem.utilities.ai.image_brief import usable_anchors
        concept = _concept(visual_anchors=("an agency owner", "a stack of invoices",
                                           "a laptop", "a busy kitchen"))
        assert usable_anchors(concept) == ["an agency owner", "a busy kitchen"]


_POST_CONCEPT = _concept(hook_phrase=_HOOK, treatment="people_scene",
                         emotional_beat="a wince of dread",
                         chosen_idea="An agency owner wincing at the payroll run ahead.")
_POST = {"focal_concept": "an agency owner wincing at payroll",
         "prompt": ('A photorealistic close shot of an agency owner wincing at the payroll run '
                    'ahead, bright warm daylight, a gold scarf, 35mm f/2, a plain wall calm in the '
                    'top third.')}


@pytest.mark.unit
class TestPostImageCoverRecipe:
    """Round 5 (post gauntlet): the post image gets the cover recipe, bright and hooked."""

    def test_a_post_carries_its_hook_for_a_top_band(self):
        with patch(_LLM, return_value=_resp(_POST)) as llm:
            brief = build_image_brief("c", surface="post_image", concept=_POST_CONCEPT)
        assert llm.call_count == 1 and brief.hook_text == _HOOK and not brief.fallback
        user = llm.call_args[1]["messages"][1]["content"]
        assert "NEGATIVE SPACE: compose a SQUARE frame with the subject centred" in user
        assert "PROPS: no paper of any kind in frame" in user

    def test_a_post_with_a_paper_prop_is_rejected(self):
        bad = dict(_POST, prompt=_POST["prompt"].replace("a gold scarf", "a gold scarf, a "
                                                                          "clipboard"))
        with patch(_LLM, side_effect=[_resp(bad), _resp(_POST)]) as llm:
            build_image_brief("c", surface="post_image", concept=_POST_CONCEPT)
        assert "paper, charts and code render legible text" in \
            llm.call_args_list[1][1]["messages"][1]["content"]

    @pytest.mark.parametrize("surface", ["post_image", "newsletter"])
    @pytest.mark.parametrize("dark", ["moody low-key light", "a dimly lit office",
                                      "a dark room", "shadowy noir lighting"])
    def test_a_dark_scene_is_rejected_on_the_recipe_surfaces(self, surface, dark):
        concept = _concept(hook_phrase="", emotional_beat="relief")
        base = _GROUNDED_COVER
        bad = dict(base, prompt=base["prompt"] + f" Shot in {dark}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(base)]) as llm:
            build_image_brief("c", surface=surface, concept=concept)
        assert "asks for a dark scene" in llm.call_args_list[1][1]["messages"][1]["content"]

    def test_a_dark_beat_may_be_dark(self):
        concept = _concept(hook_phrase="", emotional_beat="quiet dread")
        dark = dict(_GROUNDED_COVER, prompt=_GROUNDED_COVER["prompt"] + " Moody low-key light.")
        with patch(_LLM, return_value=_resp(dark)) as llm:
            build_image_brief("c", surface="post_image", concept=concept)
        assert llm.call_count == 1

    def test_a_carousel_is_not_held_to_the_recipe(self):
        dark = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + " Moody low-key light.")
        with patch(_LLM, return_value=_resp(dark)) as llm:
            brief = build_image_brief("c", surface="carousel", concept=_concept())
        assert llm.call_count == 1 and brief.hook_text is None

    @pytest.mark.parametrize("treatment", ["people_scene", "concrete_scene"])
    def test_the_photographic_templates_ask_for_bright_light(self, treatment):
        template = _TREATMENT_TEMPLATES[treatment]
        assert "BRIGHT — high-key or warm daylight with strong subject contrast" in template
        assert "never a dark, moody, low-key" in template

    def test_charcoal_belongs_only_behind_the_hook(self):
        from cqc_lem.utilities.ai.image_brief import BRAND_ACCENT_DIRECTIVE
        assert "brand colors are ACCENTS, never the overall tone" in BRAND_ACCENT_DIRECTIVE
        assert "Charcoal belongs only behind the hook" in BRAND_ACCENT_DIRECTIVE

    def test_a_post_fallback_is_bright_and_keeps_its_top_third_calm(self):
        brief = _fallback_brief("c", surface="post_image", ratio="4:5", context="",
                                concept=_POST_CONCEPT, treatment="people_scene")
        assert "Compose to compose a SQUARE frame" not in brief.prompt
        assert "a SQUARE frame with the subject centred and filling it" in brief.prompt
        assert "bright, high-key warm daylight" in brief.prompt
        assert brief.hook_text == _HOOK and _HOOK not in brief.prompt



@pytest.mark.unit
class TestRoundSixBrief:
    """Round 6 of #2241: cast rotation, valence, video frames, a headline never drawn."""

    _CAST = {"gender": "woman", "age": "50s", "ethnicity": "South Asian",
             "setting": "a client's open-plan office", "role": "agency owner"}

    def test_the_cast_reaches_the_author(self):
        concept = _concept(treatment="people_scene", cast=self._CAST, layout="panel_left")
        with patch(_LLM, return_value=_resp(_GROUNDED_COVER)) as llm:
            build_image_brief("c", surface="newsletter", concept=concept)
        user = llm.call_args[1]["messages"][1]["content"]
        assert ("CAST: the person in frame is a South Asian woman in her 50s, an agency owner, at a "
                "client's open-plan office") in user

    def test_an_avatar_is_never_recast(self):
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        concept = _concept(treatment="people_scene", cast=self._CAST, layout="panel_left")
        with patch(_LLM, return_value=_resp(_GROUNDED_COVER)) as llm:
            build_image_brief("c", surface="newsletter", concept=concept, avatar=avatar)
        assert "CAST:" not in llm.call_args[1]["messages"][1]["content"]

    def test_a_callers_concept_is_used_exactly_as_given(self):
        """Round 7: the caller's concept is never decorated (PR #2249 relies on it)."""
        concept = _concept(treatment="people_scene")
        with patch(_LLM, return_value=_resp(_GROUNDED_COVER)):
            brief = build_image_brief("c", surface="newsletter", concept=concept)
        assert brief.concept is concept and not concept.layout and concept.cast is None

    def test_a_concept_this_call_computes_gets_a_rotation(self):
        with patch("cqc_lem.utilities.ai.image_brief.analyze_content_for_image",
                   return_value=_concept(treatment="people_scene")), \
             patch(_LLM, return_value=_resp(_GROUNDED_COVER)):
            brief = build_image_brief("c", surface="newsletter")
        assert brief.concept.layout and brief.concept.cast

    def test_valence_reaches_the_author_and_the_people_template(self):
        concept = _concept(treatment="people_scene", valence="positive")
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm:
            build_image_brief("c", surface="carousel", concept=concept)
        assert "VALENCE: positive" in llm.call_args[1]["messages"][1]["content"]
        assert "never shock or despair for good news" in _TREATMENT_TEMPLATES["people_scene"]

    def test_the_people_template_asks_for_an_authentic_face(self):
        assert "authentic and restrained" in _TREATMENT_TEMPLATES["people_scene"]
        assert "never cartoonish or crying" in _TREATMENT_TEMPLATES["people_scene"]

    def test_a_video_frame_carries_no_paper_and_names_a_brand_accent(self):
        papered = dict(_GROUNDED_COVER,
                       prompt=_GROUNDED_COVER["prompt"] + " A printed monthly bill, out of focus.")
        plain = dict(_GROUNDED_COVER, prompt=_GROUNDED_COVER["prompt"].replace("warm gold lamp",
                                                                                "lamp"))
        with patch(_LLM, side_effect=[_resp(papered), _resp(plain), _resp(_GROUNDED_COVER)]) as llm:
            brief = build_image_brief("c", surface="video", concept=_concept())
        reasons = [c[1]["messages"][1]["content"] for c in llm.call_args_list[1:]]
        assert "paper, charts and code render legible text" in reasons[0]
        assert "no deliberate brand-color element" in reasons[1]
        assert brief.prompt == _GROUNDED_COVER["prompt"] and brief.hook_text is None

    def test_the_system_prompt_restores_the_no_text_invariant(self):
        from cqc_lem.utilities.ai.image_brief import _SYSTEM_PROMPT
        assert "The image carries NO text at all, ever" in _SYSTEM_PROMPT
        assert "one exception" not in _SYSTEM_PROMPT



@pytest.mark.unit
class TestRoundSevenBrief:
    @pytest.mark.parametrize("pain", ["eyes closed in relief", "her eyes squeezed shut",
                                      "a hand on her chest"])
    def test_good_news_never_reads_as_pain(self, pain):
        concept = _concept(valence="positive", emotional_beat="relief")
        bad = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + f" She smiles, {pain}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_GROUNDED)]) as llm:
            build_image_brief("c", surface="carousel", concept=concept)
        assert "good news reads as pain" in llm.call_args_list[1][1]["messages"][1]["content"]

    def test_a_negative_piece_may_close_its_eyes(self):
        concept = _concept(valence="negative")
        ok = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + " Eyes closed for a beat.")
        with patch(_LLM, return_value=_resp(ok)) as llm:
            build_image_brief("c", surface="carousel", concept=concept)
        assert llm.call_count == 1

    def test_the_positive_face_is_spelled_out(self):
        concept = _concept(valence="positive")
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm:
            build_image_brief("c", surface="carousel", concept=concept)
        assert ("VALENCE: positive — a relaxed, genuine smile, eyes bright, shoulders loose, "
                "engaged with the other person or the task — never eyes closed, never a hand on "
                "the chest, head or face") in \
            llm.call_args[1]["messages"][1]["content"]

    def test_lower_third_keeps_every_subject_up_top(self):
        from cqc_lem.utilities.ai.image_brief import NEGATIVE_SPACE
        assert "faces, torsos and hands — in the UPPER 55% of the frame" in \
            NEGATIVE_SPACE["lower_third_band"]
        assert set(NEGATIVE_SPACE) >= {"panel_left", "panel_right", "lower_third_band",
                                       "band_top", "full_bleed"}


@pytest.mark.unit
class TestRoundEightBrief:
    _LEAKED = ("A developer leaning back in a chair with a look of relief, the soft blue glow of a "
               "server room illuminating their face")

    def test_the_leaked_server_room_phrase_is_a_cliche(self):
        from cqc_lem.utilities.ai.image_brief import cliche_hit
        assert cliche_hit(self._LEAKED)

    @pytest.mark.parametrize("scene", ["rows of servers humming", "a data center aisle",
                                       "racks blinking behind her", "two server cabinets",
                                       "a data centre at night"])
    def test_server_and_data_center_scenes_are_cliches(self, scene):
        from cqc_lem.utilities.ai.image_brief import cliche_hit
        assert cliche_hit(scene), scene

    def test_a_video_frame_refuses_a_laptop(self):
        laptop = dict(_GROUNDED_COVER,
                      prompt=_GROUNDED_COVER["prompt"] + " A laptop sits open beside her.")
        with patch(_LLM, side_effect=[_resp(laptop), _resp(_GROUNDED_COVER)]) as llm:
            brief = build_image_brief("c", surface="video", concept=_concept())
        reason = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "screens are the stock" in reason
        assert "laptop" not in brief.prompt.lower()

    def test_a_composited_surface_renders_a_square_scene(self):
        with patch(_LLM, return_value=_resp(_POST)):
            brief = build_image_brief("c", surface="post_image", ratio="4:5",
                                      concept=_POST_CONCEPT)
        assert brief.hook_text == _HOOK and brief.ratio == "1:1"

    def test_the_fallback_reads_as_a_sentence(self):
        brief = _fallback_brief("c", surface="newsletter", ratio="16:9", context="",
                                concept=_COVER_CONCEPT, treatment="people_scene")
        assert "Compose to compose" not in brief.prompt
        assert "Compose a SQUARE frame with the subject centred" in brief.prompt


@pytest.mark.unit
class TestRoundNineBrief:
    @pytest.mark.parametrize("label", ["Founders & Content Teams in conversation",
                                       "a poster for Engineering and Security",
                                       "founders and content teams around a table"])
    def test_a_group_caption_is_refused(self, label):
        bad = dict(_GROUNDED_COVER, prompt=f"{_GROUNDED_COVER['prompt']} {label}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_GROUNDED_COVER)]) as llm:
            build_image_brief("c", surface="newsletter", concept=_concept())
        reason = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "group caption" in reason or "posters" in reason

    def test_the_author_is_told_to_describe_people_by_appearance(self):
        from cqc_lem.utilities.ai.image_brief import PROPS_DIRECTIVE
        assert "by APPEARANCE and ACTION" in PROPS_DIRECTIVE
        assert "founders and content teams" in PROPS_DIRECTIVE

    @pytest.mark.parametrize("prop", ["a PRICING CONTRACT sheet", "a signed agreement",
                                      "a name badge on a lanyard", "a poster on the wall",
                                      "warehouse signage"])
    def test_label_carrying_props_are_refused(self, prop):
        from cqc_lem.utilities.ai.image_brief import prop_failure
        assert prop_failure(f"A man in a grey jumper beside {prop}."), prop

    def test_a_video_frame_refuses_a_contract_and_gets_the_props_directive(self):
        papered = dict(_GROUNDED_COVER,
                       prompt=_GROUNDED_COVER["prompt"] + " A pricing contract on the table.")
        with patch(_LLM, side_effect=[_resp(papered), _resp(_GROUNDED_COVER)]) as llm:
            brief = build_image_brief("c", surface="video", concept=_concept())
        first = llm.call_args_list[0][1]["messages"][1]["content"]
        assert "PROPS: no paper of any kind" in first
        assert "no screens at all" in first
        reason = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "'contract'" in reason
        assert "contract" not in brief.prompt.lower()

    @pytest.mark.parametrize("scene", ["a man turning a safe dial", "a heavy bank vault door",
                                       "a padlock on the cabinet", "her hand on a light switch",
                                       "a combination lock", "pulling a lever"])
    def test_security_hardware_is_a_cliche(self, scene):
        from cqc_lem.utilities.ai.image_brief import cliche_hit
        assert cliche_hit(scene), scene

    def test_safe_as_an_adjective_is_not_a_cliche(self):
        from cqc_lem.utilities.ai.image_brief import cliche_hit
        assert cliche_hit("she looks relieved and safe in the meeting room") is None

    @pytest.mark.parametrize("pain", ["holding his head", "his head in his hands",
                                      "hands on his temples", "a hand over her face",
                                      "rubbing her forehead"])
    def test_positive_valence_never_touches_the_head(self, pain):
        concept = _concept(valence="positive", emotional_beat="relief")
        bad = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + f" He sits, {pain}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_GROUNDED)]) as llm:
            build_image_brief("c", surface="post_image", concept=concept)
        reason = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "good news reads as pain" in reason
        assert "engaged with the other person or the task" in reason

    def test_a_fallback_never_interpolates_a_contract_thesis(self):
        concept = _concept(thesis="The pricing contract hides the real cost", chosen_idea="")
        brief = _fallback_brief("c", surface="video", ratio="16:9", context="", concept=concept,
                                treatment="people_scene")
        assert "contract" not in brief.prompt.lower()


# The exact round-9 fallback render prompts whose renders came back with typeset fake-cover text
# ("ENGINEERING LEAD / Billing Dashboard", "AI GOVERNANCE / LinkedIn Newsletter").
_R9_ED16 = ("A photorealistic candid editorial photograph for a LinkedIn newsletter cover of "
            "engineering lead and billing dashboard in the real place this happens, with marketing "
            "team meeting in the frame, shot on a 50mm lens at f/2.8, composed for a 1:1 aspect "
            "ratio with the subject large in the frame, bright, high-key warm daylight with strong "
            "subject contrast and one deliberate gold accent, subtle film grain, real texture with "
            "fabric wear and everyday imperfections, plain unbranded surfaces, clean unmarked "
            "walls, hands empty and relaxed. Compose a SQUARE frame with the subject centred "
            "and filling it, the headline is set beside the image, never on it.")
_R9_ED19 = ("A photorealistic editorial photograph for a LinkedIn newsletter cover: small group of "
            "operations professionals gathered around a conference table, their expressions a mix "
            "of curiosity and caution, shot on a 50mm lens at f/2.8, bright, high-key warm "
            "daylight. Compose a SQUARE frame with the subject centred and filling it, the "
            "headline is set beside the image, never on it.")


@pytest.mark.unit
class TestRoundTenPhotoOnly:
    @pytest.mark.parametrize("prompt", [_R9_ED16, _R9_ED19])
    def test_the_r9_prompts_fail_the_framing_check(self, prompt):
        from cqc_lem.utilities.ai.image_brief import render_framing_failure
        assert render_framing_failure(prompt, "Unused tools vs hidden costs", "AI CONTENT AUDIT")

    @pytest.mark.parametrize("prompt", [_R9_ED16, _R9_ED19])
    def test_the_r9_prompts_are_repaired_to_a_photograph(self, prompt):
        from cqc_lem.utilities.ai.image_brief import (
            PHOTO_OPENING,
            photo_only_prompt,
            render_framing_failure,
        )
        hook, kicker = "Unused tools vs hidden costs", "AI CONTENT AUDIT"
        out = photo_only_prompt(prompt, hook, kicker, ["engineering lead"])
        assert out.startswith(PHOTO_OPENING)
        for word in ("linkedin", "newsletter", "cover", "headline", "editorial photograph"):
            assert word not in out.lower(), word
        assert "centred" in out
        assert render_framing_failure(out, hook, kicker, ["engineering lead"]) is None

    @pytest.mark.parametrize("framing", ["a LinkedIn post", "a magazine layout",
                                         "calm negative space for the title",
                                         "a text area on the left", "an editorial layout"])
    def test_any_framing_word_is_refused(self, framing):
        from cqc_lem.utilities.ai.image_brief import render_framing_failure
        assert render_framing_failure(f"A candid photograph of a man, {framing}.", "Who buys?")

    def test_hook_and_kicker_words_are_refused_unless_they_are_an_anchor(self):
        from cqc_lem.utilities.ai.image_brief import render_framing_failure
        prompt = "A candid documentary photograph of an agency owner reviewing governance."
        assert "governance" in render_framing_failure(prompt, "Who buys?", "AI GOVERNANCE")
        assert render_framing_failure(prompt, "Who buys?", "AI GOVERNANCE",
                                      ["governance review"]) is None
        assert render_framing_failure(prompt, "Agency owners lose sleep", None,
                                      ["an agency owner"]) is None

    def test_an_authored_cover_brief_naming_its_use_is_retried(self):
        framed = dict(_GROUNDED_COVER,
                      prompt=_GROUNDED_COVER["prompt"] + " For a LinkedIn newsletter cover.")
        with patch(_LLM, side_effect=[_resp(framed), _resp(_GROUNDED_COVER)]) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=_COVER_CONCEPT)
        reason = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "the render is ONLY a photograph" in reason
        assert "linkedin" not in brief.prompt.lower()

    def test_the_fallback_is_a_photograph_on_a_composited_surface(self):
        brief = _fallback_brief("c", surface="newsletter", ratio="16:9", context="",
                                concept=_COVER_CONCEPT, treatment="people_scene")
        assert brief.prompt.startswith("A candid documentary photograph")
        assert "linkedin" not in brief.prompt.lower() and "cover" not in brief.prompt.lower()
        assert "headline" not in brief.prompt.lower()

    def test_an_uncomposited_surface_keeps_its_use_case(self):
        brief = _fallback_brief("c", surface="carousel", ratio="1:1", context="",
                                concept=_concept(), treatment="people_scene")
        assert "carousel slide" in brief.prompt

    def test_the_use_case_stays_author_context(self):
        with patch(_LLM, return_value=_resp(_GROUNDED_COVER)) as llm:
            brief = build_image_brief("c", surface="newsletter", concept=_COVER_CONCEPT)
        assert brief.hook_text
        assert "USE CASE: A LinkedIn newsletter cover" in llm.call_args[1]["messages"][1]["content"]
        assert "LinkedIn" not in brief.prompt

    @pytest.mark.parametrize("prop", ["a glossy AI governance whitepaper", "a billing dashboard"])
    def test_whitepapers_and_dashboards_are_props(self, prop):
        from cqc_lem.utilities.ai.image_brief import prop_failure
        assert prop_failure(f"A team around a table with {prop}."), prop


@pytest.mark.unit
class TestRoundTwelveBrief:
    @pytest.mark.parametrize("prop", ["toy figurines on the table", "blank placeholder cards",
                                      "coloured pin flags", "a chess board", "wooden blocks",
                                      "a sticky note", "a game token"])
    def test_symbolic_props_are_refused(self, prop):
        from cqc_lem.utilities.ai.image_brief import prop_failure
        assert "symbolic prop" in (prop_failure(f"Two people beside {prop}.") or "")

    @pytest.mark.parametrize("face", ["her mouth open in alarm", "a gasping founder",
                                      "a furrowed brow", "brows furrowed", "an alarmed look",
                                      "an open-mouthed stare"])
    def test_overacted_faces_are_refused(self, face):
        bad = dict(_GROUNDED_COVER, prompt=_GROUNDED_COVER["prompt"] + f" {face}.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_GROUNDED_COVER)]) as llm:
            build_image_brief("c", surface="newsletter", concept=_COVER_CONCEPT)
        assert "overacts the face" in llm.call_args_list[1][1]["messages"][1]["content"]

    @pytest.mark.parametrize("color,bad", [("bright red flags", True), ("a blue scarf", True),
                                           ("a green folder chair", True),
                                           ("a soft blue sweater", False),
                                           ("green leaves outside", False),
                                           ("blue hour light", False),
                                           ("a navy blazer", False)])
    def test_saturated_objects_are_refused(self, color, bad):
        from cqc_lem.utilities.ai.image_brief import saturated_color
        assert bool(saturated_color(f"A man with {color}.")) is bad

    def test_the_brand_directive_bounds_every_color(self):
        from cqc_lem.utilities.ai.image_brief import BRAND_ACCENT_DIRECTIVE
        assert "gold, charcoal, off-white and natural tones" in BRAND_ACCENT_DIRECTIVE

    def test_negative_valence_is_restrained(self):
        from cqc_lem.utilities.ai.image_brief import _VALENCE_FACES
        assert "mouth closed" in _VALENCE_FACES["negative"]

    def test_a_video_frame_keeps_the_near_foreground_empty(self):
        with patch(_LLM, return_value=_resp(_GROUNDED_COVER)) as llm:
            build_image_brief("c", surface="video", concept=_concept())
        assert "no hands or objects close to the camera" in \
            llm.call_args[1]["messages"][1]["content"]

    def test_a_cover_brief_has_no_video_directive(self):
        with patch(_LLM, return_value=_resp(_GROUNDED_COVER)) as llm:
            build_image_brief("c", surface="newsletter", concept=_COVER_CONCEPT)
        assert "VIDEO FRAME" not in llm.call_args[1]["messages"][1]["content"]

    def test_the_articles_setting_reaches_the_author(self):
        concept = _concept(setting="a billing review late on a Tuesday")
        with patch(_LLM, return_value=_resp(_GROUNDED_COVER)) as llm:
            build_image_brief("c", surface="video", concept=concept)
        assert "SETTING (from the article — use it): a billing review late on a Tuesday" in \
            llm.call_args[1]["messages"][1]["content"]


@pytest.mark.unit
class TestRoundThirteenBrief:
    @pytest.mark.parametrize("prop", ["a coffee mug", "a paper cup", "a latte", "a cup of tea",
                                      "two mugs"])
    def test_mugs_and_cups_are_refused(self, prop):
        from cqc_lem.utilities.ai.image_brief import prop_failure
        assert prop_failure(f"Two people talking beside {prop}.")

    def test_relief_alone_is_refused_for_good_news(self):
        concept = _concept(valence="positive", emotional_beat="relief")
        bad = dict(_GROUNDED, prompt=_GROUNDED["prompt"] + " Her face shows relief.")
        with patch(_LLM, side_effect=[_resp(bad), _resp(_GROUNDED)]) as llm:
            build_image_brief("c", surface="post_image", concept=concept)
        reason = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "a relaxed, genuine smile, eyes bright, shoulders loose" in reason

    def test_relief_with_a_smile_passes(self):
        from cqc_lem.utilities.ai.image_brief import _deterministic_failure
        prompt = "A woman with a relaxed, genuine smile of relief, eyes bright, gold scarf."
        assert _deterministic_failure(prompt, anchors=[], weak=True, hook_text=None,
                                      positive=True) is None

    def test_the_positive_face_is_literal(self):
        from cqc_lem.utilities.ai.image_brief import _VALENCE_FACES
        assert _VALENCE_FACES["positive"].startswith(
            "a relaxed, genuine smile, eyes bright, shoulders loose")

    def test_the_shot_reaches_the_author_and_the_fallback(self):
        shot = "an over-the-shoulder two-shot, the camera behind one person looking at the other"
        concept = _concept(treatment="people_scene", shot=shot,
                           chosen_idea="an agency owner wincing at the payroll run")
        with patch(_LLM, return_value=_resp(_GROUNDED)) as llm:
            build_image_brief("c", surface="post_image", concept=concept)
        assert f"SHOT (framing — use it): {shot}" in llm.call_args[1]["messages"][1]["content"]
        brief = _fallback_brief("c", surface="post_image", ratio="4:5", context="",
                                concept=concept, treatment="people_scene")
        assert f"framed as {shot}" in brief.prompt
