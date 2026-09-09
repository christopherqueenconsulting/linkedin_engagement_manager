"""The ONE image-brief engine: validated lem-medium JSON briefs with a deterministic fallback."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai.image_brief import _STYLE_PRESETS, ImageBrief, build_image_brief

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
        """The author must never NAME text/logos in a render prompt (FLUX summons what is
        named) — the system prompt instructs positive phrasing instead.
        """
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="post_image")
        system_msg = llm.call_args[1]["messages"][0]["content"]
        assert "NEVER mention" in system_msg
        assert "logos" in system_msg and "watermarks" in system_msg
        assert "plain unbranded clothing" in system_msg

    def test_invalid_json_retries_then_falls_back_deterministically(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp("not json at all")) as llm:
            brief = build_image_brief("Quarterly revenue lessons", surface="post_image")

        assert llm.call_count == 2
        assert "Quarterly revenue lessons" in brief.prompt
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
    """Regression: a bare "language model" ban rejected every legitimate brief an AI-focused
    author writes, so their briefs silently fell back to the deterministic template.
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
    """Regression: lem-medium is a REASONING model. At max_tokens=600 the whole budget went to
    thinking tokens, the response came back EMPTY with finish_reason='length', and every image
    silently rendered from the deterministic template.
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
    """A stranger's face on a PERSONAL-brand newsletter is the stock-photo look the engine
    exists to replace; with the author's own likeness, a person IS the point.
    """

    def test_no_avatar_steers_away_from_an_anonymous_face(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="newsletter", avatar=None)
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "do NOT make an anonymous person the focal subject" in user_msg

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


_BANNED_NOUNS = ("laptop", "notebook", "coffee", "desk", "office", "typing", "keyboard",
                 "screen", "monitor", "phone")

# Five fixture editions drawn from issue #1992's golden set — the same five real covers that
# collapsed to "person at laptop with notebook and coffee mug". Each fixture's "golden" LLM
# response is what a WORKING brief author returns for it: object-first, no stock-office nouns.
_FIVE_FIXTURE_EDITIONS = (
    ("The Routing Switch That Reduced Outreach Costs by 42%",
     {"focal_concept": "an industrial rotary switch routing power down the cheap path",
      "prompt": ("A heavy industrial rotary selector switch mounted on a steel panel, one "
                "contact glowing warm and lit, the other dark and unused, macro close-up, "
                "dramatic raking side light, shot on a 100mm macro lens at f/4, brushed metal "
                "texture, subtle film grain, editorial product photograph.")}),
    ("My Multi-Agent Content System Broke Quietly",
     {"focal_concept": "a row of dominoes with the middle piece fallen but the last still standing",
      "prompt": ("A row of wooden dominoes on a dark table, the middle piece toppled while the "
                "final domino still stands untouched, muted low-key lighting from camera left, "
                "shallow depth of field, shot on an 85mm lens at f/2, quiet desaturated color "
                "grade, editorial still life photograph.")}),
    ("Audit AI LinkedIn Engagement to Cut Costs",
     {"focal_concept": "a brass balance scale weighing coins against a single feather",
      "prompt": ("An antique brass balance scale on a wooden table, a stack of coins on one pan "
                "and a single feather on the other, warm directional light from camera right, "
                "shallow depth of field, shot on a 100mm macro lens at f/2.8, tactile aged "
                "metal texture, editorial still life photograph.")}),
    ("The Overlooked Costs of Your AI LinkedIn Strategy",
     {"focal_concept": "a sledgehammer resting beside a single thumbtack on a workbench",
      "prompt": ("A heavy sledgehammer resting on a worn wooden workbench beside one tiny "
                "thumbtack, dramatic side light emphasizing the size contrast, shallow depth "
                "of field, shot on an 85mm lens at f/2, subtle film grain, tactile workshop "
                "textures, editorial product photograph.")}),
    ("Spot the Leak in Your LinkedIn AI Budget",
     {"focal_concept": "a single water droplet falling from a copper pipe joint into a puddle",
      "prompt": ("A single water droplet caught mid-fall from a corroded copper pipe joint into "
                "a growing puddle below, macro close-up, high-contrast floor-level spotlight, "
                "shot on a 100mm macro lens at f/2.8, crisp water texture, dramatic editorial "
                "photograph.")}),
)


@pytest.mark.unit
class TestNewsletterPreset:
    """Issue #1992: five straight covers converged on one "person at laptop" scene.

    These pin the fix at the brief layer — the stock-office gate, the metaphor-vocabulary
    preset, and the format/hook_style shape hint.
    """

    @pytest.mark.parametrize("title,payload", _FIVE_FIXTURE_EDITIONS)
    def test_object_first_response_passes_through_unmodified(self, title, payload):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp(payload)) as llm:
            brief = build_image_brief(f"{title}\n\nSubtitle\n\nBody", surface="newsletter",
                                      ratio="16:9")
        assert llm.call_count == 1, "a valid object-first brief must not retry or fall back"
        assert not brief.fallback
        lowered = brief.prompt.lower()
        for noun in _BANNED_NOUNS:
            assert noun not in lowered, f"{title!r}: prompt still names {noun!r}"
        assert brief.focal_concept == payload["focal_concept"]

    def test_the_five_golden_concepts_are_mutually_distinct(self):
        concepts = {payload["focal_concept"] for _title, payload in _FIVE_FIXTURE_EDITIONS}
        assert len(concepts) == len(_FIVE_FIXTURE_EDITIONS)

    def test_stock_office_response_is_rejected_and_retried(self):
        stock = {"focal_concept": "a person at a laptop",
                 "prompt": ("A confident professional sits at a laptop on a wooden desk with a "
                           "notebook and coffee mug beside them, soft window light, shallow "
                           "depth of field, editorial photograph.")}
        good = _FIVE_FIXTURE_EDITIONS[0][1]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=[_resp(stock), _resp(good)]) as llm:
            brief = build_image_brief("content", surface="newsletter")
        assert llm.call_count == 2
        assert not brief.fallback
        assert "laptop" not in brief.prompt.lower()
        assert brief.prompt == good["prompt"]

    def test_stock_office_response_exhausts_retries_and_falls_back(self):
        stock = {"focal_concept": "a person at a laptop",
                 "prompt": ("A confident professional types on a laptop at a desk with a "
                           "notebook and coffee mug, soft window light, editorial photograph.")}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp(stock)) as llm, \
             patch("cqc_lem.utilities.ai.image_brief.log_warning") as warn:
            brief = build_image_brief("a newsletter about routing costs", surface="newsletter")
        assert llm.call_count == 2
        assert brief.fallback
        assert warn.called

    def test_the_gate_only_applies_to_the_newsletter_surface(self):
        stock = {"focal_concept": "a person at a laptop",
                 "prompt": ("A confident professional sits at a laptop on a desk with a "
                           "notebook, soft window light, editorial photograph.")}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(stock)) as llm:
            brief = build_image_brief("content", surface="post_image")
        assert llm.call_count == 1, "post_image is not gated on stock-office nouns"
        assert not brief.fallback
        assert brief.prompt == stock["prompt"]

    def test_the_gate_never_applies_when_the_avatar_is_in_frame(self):
        stock = {"focal_concept": "the author at a laptop",
                 "prompt": ("The author sits at a laptop on a desk with a notebook and coffee "
                           "mug, soft window light, editorial photograph.")}
        avatar = {"gender_presentation": "man", "age_band": "40s", "trigger_word": "TOK"}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(stock)) as llm:
            brief = build_image_brief("content", surface="newsletter", avatar=avatar)
        assert llm.call_count == 1, "a real person IS the point once the avatar is in frame"
        assert not brief.fallback
        assert brief.prompt == stock["prompt"]

    def test_content_shape_reaches_the_user_message(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="newsletter",
                              content_shape="format=case_study, hook_style=personal_story")
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "case_study" in user_msg and "personal_story" in user_msg

    def test_no_content_shape_adds_nothing(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(_GOOD)) as llm:
            build_image_brief("content", surface="newsletter")
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "Content shape:" not in user_msg

    def test_preset_offers_a_metaphor_vocabulary_for_abstract_topics(self):
        preset = _STYLE_PRESETS["newsletter"]
        for word in ("switch", "valve", "leak", "scale", "gear", "domino"):
            assert word in preset.lower()

    def test_word_boundary_gate_does_not_reject_substring_lookalikes(self):
        # A bare substring match on "phone" also flags saxophone/microphone/telephone; "screen"
        # also flags "screening"/"green screen". The gate must reject only the whole-word noun.
        clean = {"focal_concept": "a brass saxophone on a stage",
                 "prompt": ("A weathered brass saxophone rests on a velvet-lined stand under a "
                           "warm stage spotlight, soft window light from camera left, shot on "
                           "an 85mm lens at f/1.8, subtle film grain, editorial photograph.")}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(clean)) as llm:
            brief = build_image_brief("content", surface="newsletter")
        assert llm.call_count == 1, "saxophone must not be rejected as a 'phone' cliché"
        assert not brief.fallback
        assert brief.prompt == clean["prompt"]


@pytest.mark.unit
class TestLiveSampleRegressions:
    """Regressions from issue #1992's five-edition live sample run.

    Four of five covers shipped from the deterministic fallback, and the fallback drew the exact
    "man at a laptop" scene the issue was filed about.
    """

    _STOCK = {"focal_concept": "a person at a laptop",
              "prompt": ("A confident professional sits at a laptop on a wooden desk with a "
                        "notebook and coffee mug beside them, soft window light, editorial "
                        "photograph.")}

    def test_newsletter_fallback_never_asks_for_people_screens_or_clothing(self):
        """The fallback template is a RENDER prompt: every noun in it is a request.

        The generic one pasted the surface preset's "People and screens stay out of the frame"
        into it and then asked for "blank screens" and "plain unbranded clothing" — which is how
        a fallback cover came back as a man at a laptop.
        """
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(self._STOCK)):
            brief = build_image_brief("Spot the leak in your AI budget", surface="newsletter",
                                      ratio="16:9")
        assert brief.fallback
        lowered = brief.prompt.lower()
        for noun in ("people", "person", "clothing", "screen", "laptop", "desk", "notebook"):
            assert noun not in lowered, f"the newsletter fallback prompt still names {noun!r}"

    def test_the_editions_own_stock_nouns_are_stripped_from_the_fallback_summary(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_resp(self._STOCK)):
            brief = build_image_brief("How much did your last laptop draft cost on screen?",
                                      surface="newsletter", ratio="16:9")
        assert brief.fallback
        assert "laptop" not in brief.prompt.lower()
        assert "screen" not in brief.prompt.lower()

    def test_an_avatar_newsletter_fallback_keeps_the_generic_template(self):
        """A person and a desk genuinely belong once the author's own likeness is the subject."""
        avatar = {"status": "succeeded", "model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("down")):
            brief = build_image_brief("content", surface="newsletter", ratio="16:9",
                                      avatar=avatar)
        assert brief.fallback
        assert "A single professional photograph representing:" in brief.prompt

    def test_avoid_terms_reach_the_author_but_never_the_fallback_prompt(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp(self._STOCK)) as llm:
            brief = build_image_brief("an edition about token spend", surface="newsletter",
                                      ratio="16:9", avoid_terms=["laptop", "a prior gear"])
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert "must NOT be built on any of them" in user_msg
        assert "a prior gear" in user_msg
        assert brief.fallback
        assert "a prior gear" not in brief.prompt

    def test_the_retry_is_told_which_noun_was_rejected(self):
        """Re-sending the identical prompt just re-drew the rejected scene."""
        good = {"focal_concept": "a cracked brass gear",
                "prompt": ("A cracked brass gear resting on a worn workbench, dramatic raking "
                          "side light, macro still life, shot on a 100mm lens at f/2.8, subtle "
                          "film grain, editorial photograph.")}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=[_resp(self._STOCK), _resp(good)]) as llm:
            brief = build_image_brief("content", surface="newsletter", ratio="16:9")
        assert not brief.fallback
        first = llm.call_args_list[0][1]["messages"][1]["content"]
        second = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "REJECTED" not in first
        assert "Your previous attempt was REJECTED" in second
        assert "'laptop'" in second, "the retry must name the offending noun"


@pytest.mark.unit
class TestAvoidTermGate:
    """Issue #2000: `avoid_terms` as a prompt line alone had no teeth.

    Four consecutive live newsletter covers all chose a valve while every prior valve sat in the
    avoid list. It now gates the returned FOCAL CONCEPT and drives the same retry-with-reason the
    stock-office noun does.
    """

    _VALVE = {"focal_concept": "Budget leak shown as a dripping valve",
              "prompt": ("A brass valve dripping onto a worn workbench, dramatic raking side "
                        "light, macro still life, shot on a 100mm lens at f/2.8, subtle film "
                        "grain, editorial photograph.")}
    _GEAR = {"focal_concept": "A cracked gear stopped mid-turn",
             "prompt": ("A cracked cast-iron gear resting on a stone slab, low-key light from "
                       "camera left, macro still life, shot on a 100mm lens at f/2.8, subtle "
                       "film grain, editorial photograph.")}

    def test_a_repeated_object_is_rejected_and_retried(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=[_resp(self._VALVE), _resp(self._GEAR)]) as llm:
            brief = build_image_brief("content", surface="newsletter", ratio="16:9",
                                      avoid_terms=["valve"])
        assert llm.call_count == 2
        assert not brief.fallback
        assert brief.focal_concept == self._GEAR["focal_concept"]

    def test_the_retry_names_the_repeated_object(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   side_effect=[_resp(self._VALVE), _resp(self._GEAR)]) as llm:
            build_image_brief("content", surface="newsletter", ratio="16:9",
                              avoid_terms=["valve"])
        second = llm.call_args_list[1][1]["messages"][1]["content"]
        assert "'valve'" in second
        assert "different family" in second

    def test_a_distinct_object_passes_first_time(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp(self._GEAR)) as llm:
            brief = build_image_brief("content", surface="newsletter", ratio="16:9",
                                      avoid_terms=["valve", "leak"])
        assert llm.call_count == 1
        assert not brief.fallback

    def test_the_gate_reads_the_focal_concept_not_the_whole_prompt(self):
        """A valve in the background of an otherwise distinct scene is not a repeat."""
        background = {"focal_concept": "A cracked gear stopped mid-turn",
                      "prompt": ("A cracked cast-iron gear on a stone slab with a brass valve "
                                "far behind it out of focus, low-key light, macro still life, "
                                "shot on a 100mm lens at f/2.8, editorial photograph.")}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp(background)) as llm:
            brief = build_image_brief("content", surface="newsletter", ratio="16:9",
                                      avoid_terms=["valve"])
        assert llm.call_count == 1
        assert not brief.fallback

    def test_a_long_avoid_list_cannot_starve_the_author(self):
        from cqc_lem.utilities.ai.image_brief import _MAX_AVOID_TERMS

        avoid = [f"object{i}" for i in range(40)] + ["gear"]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp(self._GEAR)) as llm:
            brief = build_image_brief("content", surface="newsletter", ratio="16:9",
                                      avoid_terms=avoid)
        assert llm.call_count == 1, "the capped list drops the oldest steering, never the author"
        assert not brief.fallback
        user_msg = llm.call_args[1]["messages"][1]["content"]
        assert f"object{_MAX_AVOID_TERMS}" not in user_msg

    def test_the_gate_is_off_for_other_surfaces(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp(self._VALVE)) as llm:
            brief = build_image_brief("content", surface="post_image", avoid_terms=["valve"])
        assert llm.call_count == 1
        assert not brief.fallback

    def test_the_gate_is_off_when_an_avatar_is_in_frame(self):
        avatar = {"status": "succeeded", "model_ref": "owner/lora:v1", "trigger_word": "TOK"}
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_resp(self._VALVE)) as llm:
            brief = build_image_brief("content", surface="newsletter", ratio="16:9",
                                      avatar=avatar, avoid_terms=["valve"])
        assert llm.call_count == 1
        assert not brief.fallback


@pytest.mark.unit
class TestNewsletterPresetFamilies:
    def test_the_preset_offers_more_than_plumbing(self):
        """Cost and routing ideas both reach for plumbing; one family is not a vocabulary."""
        preset = _STYLE_PRESETS["newsletter"].lower()
        for family_word in ("scale", "gear", "domino", "caliper", "sieve"):
            assert family_word in preset
        assert "different family" in preset
