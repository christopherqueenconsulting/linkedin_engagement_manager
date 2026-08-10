"""The vision gate's repair round must not re-request the defect it just rejected (issue #1141).

`inspect_render_quality` reports what is WRONG ("garbled text on whiteboard"). The retry pasted
that straight back into the next render prompt — correct for instruction-following gpt-image,
and backwards for FLUX, which has no negative prompting and renders what a prompt names. So on
every FLUX path (which is EVERY avatar render) the repair asked for the defect a second time.
"""

from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai.image_gen import (
    QualityVerdict,
    _configured_backend,
    render_avatar_image_gated,
    repair_directive,
)

pytestmark = pytest.mark.unit

_AVATAR = {"model_ref": "owner/lora:v1", "trigger_word": "TOK"}


class TestRepairDirective:
    def test_gpt_image_is_told_what_to_avoid(self):
        clause = repair_directive(["garbled text on whiteboard"], "gpt-image")
        assert "garbled text on whiteboard" in clause
        assert "Avoid those problems" in clause

    @pytest.mark.parametrize("issue,wanted", [
        ("garbled text on the whiteboard", "plain and unmarked"),
        ("a LinkedIn logo in the corner", "plain and unmarked"),
        ("six fingers on the left hand", "hands relaxed and out of frame"),
        ("extra digit on the right hand", "hands relaxed and out of frame"),
        ("deformed anatomy in the background figure", "naturally proportioned"),
        # The gate phrases bad anatomy plenty of ways that never say "hand" or "finger".
        ("malformed face", "naturally proportioned"),
        ("distorted torso", "naturally proportioned"),
        ("does not relate to the stated subject", "literal, concrete depiction"),
    ])
    def test_flux_is_told_what_to_show_instead(self, issue, wanted):
        clause = repair_directive([issue], "flux")
        assert wanted in clause
        # The defect itself is never named back at a renderer that would draw it.
        for token in ("text", "logo", "finger", "deformed", "malformed", "distorted"):
            if token in issue:
                assert token not in clause.lower()

    def test_an_off_topic_verdict_names_the_focal_concept_back(self):
        """"the stated subject" is the vagueness that let the render drift in the first place."""
        clause = repair_directive(["does not relate to the stated subject"], "flux",
                                  "a freight dispatcher checking a loading dock")
        assert "a freight dispatcher checking a loading dock" in clause

    def test_a_missing_focal_concept_still_reads_as_an_instruction(self):
        clause = repair_directive(["unrelated to the subject"], "flux")
        assert "the subject the brief describes" in clause
        assert "{focal}" not in clause

    def test_an_unrecognised_verdict_still_yields_a_positive_directive(self):
        clause = repair_directive(["something the map has never seen"], "flux")
        assert clause.startswith("Render this scene again with ")
        assert "clear, concrete subject" in clause

    def test_an_empty_verdict_is_never_an_empty_retry(self):
        assert repair_directive([], "flux").strip().endswith(".")
        assert "low relevance to the subject" in repair_directive([], "gpt-image")

    def test_multiple_issues_collect_every_counter_once(self):
        clause = repair_directive(["garbled text", "fused fingers"], "flux")
        assert "plain and unmarked" in clause and "hands relaxed" in clause

    def test_configured_backend_follows_image_backend(self):
        for value, expected in (("flux", "flux"), ("auto", "gpt-image"),
                                ("gpt-image", "gpt-image"), ("", "gpt-image")):
            with patch("cqc_lem.utilities.ai.image_gen.IMAGE_BACKEND", value):
                assert _configured_backend() == expected


class TestAvatarRetryUsesFluxPhrasing:
    def test_the_second_render_never_names_the_rejected_defect(self):
        prompts: list[str] = []

        def _render(prompt, model_ref, **kwargs):
            prompts.append(prompt)
            return "/tmp/a.png", True

        rejected = QualityVerdict(acceptable=False, relevance=2,
                                  issues=["garbled text on the whiteboard"])
        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   side_effect=_render), \
             patch("cqc_lem.utilities.avatar.attributes.apply_subject_clause",
                   side_effect=lambda p, a: p), \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch("cqc_lem.utilities.ai.image_gen.inspect_render_quality",
                   return_value=rejected), \
             patch("cqc_lem.utilities.ai.image_gen.IMAGE_QUALITY_GATE_SURFACES", ("post_image",)), \
             patch("cqc_lem.utilities.ai.image_gen.IMAGE_GATE_MAX_ATTEMPTS", 2):
            render_avatar_image_gated("a quiet desk scene", avatar=_AVATAR, user_id=1,
                                      surface="post_image", focal_concept="a desk")

        assert len(prompts) == 2
        assert "garbled" not in prompts[1] and "text on the whiteboard" not in prompts[1]
        assert "plain and unmarked" in prompts[1]
