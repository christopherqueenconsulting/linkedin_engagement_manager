"""Every carousel construction site resolves a stage to the SAME model (issue #1681).

The generator names one model's fields in the prompt; the 30-day plan and the preview route each
build the reply into a model of their own. While those were three separate stage maps they
disagreed: the SPA's "Personal Story" stage asked the LLM for an `IndustryInsightsCarousel` and was
then validated as a `PersonalStoryCarousel`, so the deck was shape-valid for the model it was asked
for and structurally impossible for the model it was checked against — every personal-story preview
failed, and no retry could change it. These tests hold all three to one resolver.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.carousel_creator import (
    CaseStudyCarousel,
    EducationalContentCarousel,
    IndustryInsightsCarousel,
    PersonalStoryCarousel,
    ProductDemoCarousel,
    carousel_model_for_stage,
    carousel_schema_hint,
)

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_CC = "cqc_lem.utilities.carousel_creator"

# Every stage value the SPA's carousel picker can send (`CAROUSEL_STAGES` in ComposePost.tsx).
SPA_STAGES = ["awareness", "consideration", "decision", "personal"]

_SLIDE = {"title": "A Title Here", "content": "One short sentence of slide body copy."}

# One deck per model, in the shape that model requires — what the LLM is asked to return once the
# prompt names that model's fields.
_DECKS: dict[type, dict] = {
    EducationalContentCarousel: {"cover": _SLIDE, "contents": [_SLIDE, _SLIDE],
                                 "call_to_action": _SLIDE},
    CaseStudyCarousel: {"cover": _SLIDE, "challenge": _SLIDE, "solution": _SLIDE,
                        "results": _SLIDE, "call_to_action": _SLIDE},
    ProductDemoCarousel: {"cover": _SLIDE, "main_feature": _SLIDE,
                          "additional_features": [_SLIDE], "call_to_action": _SLIDE},
    PersonalStoryCarousel: {"cover": _SLIDE, "story_slides": [_SLIDE, _SLIDE],
                            "takeaway": _SLIDE, "call_to_action": _SLIDE},
    IndustryInsightsCarousel: {"cover": _SLIDE, "insights": [_SLIDE, _SLIDE],
                               "call_to_action": _SLIDE},
}


def _deck_for(stage: str) -> dict:
    """The deck an obedient LLM returns for *stage*, per the shared map."""
    return _DECKS[carousel_model_for_stage(stage)]


class TestTheResolverIsTheMap:
    @pytest.mark.parametrize("stage,expected", [
        ("awareness", EducationalContentCarousel),
        ("consideration", CaseStudyCarousel),
        ("decision", ProductDemoCarousel),
        ("personal", PersonalStoryCarousel),
        ("story", PersonalStoryCarousel),
        ("Personal Story", PersonalStoryCarousel),
        ("awareness/education", EducationalContentCarousel),
    ])
    def test_a_stage_resolves_to_its_deck_model(self, stage, expected):
        assert carousel_model_for_stage(stage) is expected

    @pytest.mark.parametrize("stage", ["", None, "  ", "quarterly-recap"])
    def test_an_unmapped_stage_still_gets_a_renderable_deck(self, stage):
        # Never an exception: an unrecognised stage falls to the shared default so the deck still
        # builds, which is what let a new stage reach the SPA without breaking generation.
        assert carousel_model_for_stage(stage) is IndustryInsightsCarousel

    @pytest.mark.parametrize("stage", SPA_STAGES)
    def test_the_schema_hint_names_the_model_that_stage_builds(self, stage):
        model_cls = carousel_model_for_stage(stage)
        assert carousel_schema_hint(model_cls).startswith(model_cls.__name__)

    @pytest.mark.parametrize("stage", SPA_STAGES)
    def test_the_hint_names_every_required_field_of_that_model(self, stage):
        # The prompt is the only thing that decides the shape that comes back, so a required field
        # missing from the hint is a deck the construction site can legitimately reject.
        model_cls = carousel_model_for_stage(stage)
        hint = carousel_schema_hint(model_cls)
        required = [name for name, field in model_cls.model_fields.items() if field.is_required()]
        assert [name for name in required if name not in hint] == []


class TestTheGeneratorAsksForTheSameModelTheSitesBuild:
    """The prompt half of the parity: what the LLM is ASKED for."""

    def _prompt_for(self, stage: str) -> str:
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(
            content='{"post_text": "A caption.", "carousel": {}}'))]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=response) as call, \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
                   side_effect=Exception("no db")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair",
                   side_effect=Exception("no driver")), \
             patch("cqc_lem.utilities.linkedin.helper.load_profile_for_user", return_value=None):
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            generate_carousel_content(user_id=1, stage=stage)
        return str(call.call_args)

    @pytest.mark.parametrize("stage", SPA_STAGES)
    def test_the_prompt_names_the_resolved_model(self, stage):
        assert carousel_model_for_stage(stage).__name__ in self._prompt_for(stage)

    def test_the_personal_story_prompt_asks_for_story_slides_not_insights(self):
        # The exact regression: `personal` fell to the generator's `else`, so the LLM was asked for
        # `insights` while the preview route demanded `story_slides` + `takeaway`.
        prompt = self._prompt_for("personal")
        assert "story_slides" in prompt and "takeaway" in prompt
        assert "IndustryInsightsCarousel" not in prompt


class TestThePreviewRouteBuildsTheResolvedModel:
    """`POST /api/generate-carousel` — the site the SPA's stage picker reaches."""

    def _preview(self, api_client, stage: str):
        rendered = {}

        def _capture(carousel_obj, *args, **kwargs):
            rendered["model"] = type(carousel_obj)
            return ["/tmp/slide_1.png", "/tmp/slide_2.png"]

        with patch("cqc_lem.api.main.require_session_user_id", return_value=1), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("A caption.", _deck_for(stage))), \
             patch(f"{_CC}.create_carousel_slide_images", side_effect=_capture):
            resp = api_client.post("/api/generate-carousel",
                                   json={"session_token": "good", "stage": stage})
        return resp, rendered.get("model")

    @pytest.mark.parametrize("stage", SPA_STAGES)
    def test_the_route_builds_the_resolved_model(self, api_client, stage):
        resp, model = self._preview(api_client, stage)
        assert resp.status_code == 200
        assert model is carousel_model_for_stage(stage)

    def test_a_personal_story_preview_returns_slide_urls(self, api_client):
        # The acceptance criterion: `stage=personal` is a 200 with slides, where before the deck the
        # generator produced could never satisfy the model this route validated.
        resp, _ = self._preview(api_client, "personal")
        assert resp.status_code == 200
        assert len(resp.json()["detail"]["slide_urls"]) == 2


class TestThePlanBuildsTheResolvedModel:
    """`run_content_plan.create_carousel_content` — the 30-day plan's construction site."""

    def _create(self, stage: str):
        rendered = {}

        def _capture(carousel_obj, *args, **kwargs):
            rendered["model"] = type(carousel_obj)
            return ["/tmp/slide_1.png"]

        from cqc_lem.app.run_content_plan import create_carousel_content
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.update_db_post_carousel_slides"), \
             patch(f"{_RCP}._report_carousel_fact_grounding"), \
             patch(f"{_RCP}._report_carousel_slide_slop"), \
             patch(f"{_RCP}._score_carousel_caption_authenticity"), \
             patch(f"{_CC}.create_ppt"), \
             patch(f"{_CC}.create_carousel_slide_images", side_effect=_capture), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("A caption.", _deck_for(stage))):
            create_carousel_content(1, stage, post_id=7)
        return rendered.get("model")

    @pytest.mark.parametrize("stage", SPA_STAGES)
    def test_the_plan_builds_the_resolved_model(self, stage):
        assert self._create(stage) is carousel_model_for_stage(stage)


class TestNoSiteKeepsItsOwnStageMap:
    """The map drifted because it was copied. A second copy anywhere is the regression."""

    @pytest.mark.parametrize("module", [
        "cqc_lem.api.main",
        "cqc_lem.app.run_content_plan",
        "cqc_lem.utilities.ai.ai_helper",
    ])
    def test_the_site_reads_the_shared_resolver_and_names_no_model_itself(self, module):
        import importlib
        import inspect
        source = inspect.getsource(importlib.import_module(module))
        assert "carousel_model_for_stage" in source
        named = [m.__name__ for m in _DECKS if m.__name__ in source]
        assert named == [], f"{module} still names carousel model(s) {named} of its own"
