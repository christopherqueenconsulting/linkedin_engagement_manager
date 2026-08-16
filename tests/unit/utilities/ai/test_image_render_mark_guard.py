"""The RENDER side of R2, pinned the way `test_image_preset_drift.py` pins the author side (#1376).

Newsletter cover ed9 rendered four logo tiles and the letters "AI" onto a laptop screen while
travelling the fully-gated path: brief authored by `image_brief`, `with_no_marks` appended by
`image_gen`, verdict taken by the `lem-vision` gate. Belt and braces both held and the marks
appeared anyway, so the interesting question was never "which caller forgot the constraint" — it
was why a blanket prohibition fails on a prompt that NAMES a screen.

These tests pin the answer on both halves:

* the prompt that actually reaches a backend states what a named mark-carrying surface SHOWS, in
  positive phrasing that survives a renderer ignoring negation;
* the gate that is asked whether the render carries marks can actually read one.

Nothing here makes a live model call — the renderer and the vision client are both patched, and
every assertion is against the string handed to them.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import image_gen
from cqc_lem.utilities.ai.image_brief import NEGATION_MARKERS
from cqc_lem.utilities.ai.image_gen import QualityVerdict

pytestmark = pytest.mark.unit

# The wording a real cover brief takes: a laptop is a plausible object in an editorial scene about
# AI cost, and it is exactly the object ed9's render filled with someone else's trademark.
_SCREEN_BRIEF = ("A founder at a wooden desk beside an open laptop, soft window light from camera "
                 "left, shot on an 85mm lens at f/1.8")
_BOARD_BRIEF = "A team room with a whiteboard on the far wall, morning light, 35mm at f/2.8"


def _clauses() -> tuple[str, ...]:
    return tuple(clause for _pattern, clause in image_gen._MARK_MAGNET_PATTERNS)


class TestNamedSurfacesStateWhatTheyShow:
    """The control itself: a scene naming a screen carries that screen's blank state."""

    @pytest.mark.parametrize("backend", ["gpt-image", "flux"])
    def test_a_screen_in_the_scene_is_stated_switched_off(self, backend):
        marked = image_gen.with_no_marks(_SCREEN_BRIEF, backend)
        assert "switched off and uniformly dark" in marked

    @pytest.mark.parametrize("backend", ["gpt-image", "flux"])
    def test_a_board_in_the_scene_is_stated_bare(self, backend):
        marked = image_gen.with_no_marks(_BOARD_BRIEF, backend)
        assert "bare, smooth and evenly blank" in marked

    def test_a_scene_naming_neither_gains_neither_clause(self):
        """On FLUX naming a thing summons it, so an unrelated clause is a defect, not a spare."""
        marked = image_gen.with_no_marks("A worn leather satchel on a station bench", "flux")
        for clause in _clauses():
            assert clause not in marked

    def test_a_screen_scene_does_not_gain_the_board_clause(self):
        marked = image_gen.with_no_marks(_SCREEN_BRIEF, "flux")
        assert "bare, smooth and evenly blank" not in marked

    def test_triggers_match_whole_words_only(self):
        """`keyboard` is not a board, and a keyboard in frame must not summon a poster."""
        marked = image_gen.with_no_marks("Hands resting beside a mechanical keyboard", "flux")
        assert "bare, smooth and evenly blank" not in marked


class TestEveryAppendedClauseIsSafeOnFlux:
    """Mirrors the author-side rule: a constraint FLUX reads must state the desired state."""

    @pytest.mark.parametrize("clause", _clauses())
    def test_no_clause_is_phrased_as_negation(self, clause):
        lowered = clause.lower()
        for marker in NEGATION_MARKERS:
            assert marker not in lowered, (
                f"{clause!r} uses {marker!r}; the renderer ignores negation and renders what the "
                f"prompt names")

    @pytest.mark.parametrize("clause", _clauses())
    def test_no_clause_names_the_mark_it_prevents(self, clause):
        lowered = clause.lower()
        for word in ("logo", "text", "letter", "word", "icon", "brand", "watermark", "ui "):
            assert word not in lowered, (
                f"{clause!r} names {word!r} — on FLUX that summons the exact defect it exists to "
                f"prevent")

    def test_the_flux_blanket_constraint_is_still_positive(self):
        lowered = image_gen._NO_MARKS_FLUX.lower()
        for marker in NEGATION_MARKERS:
            assert marker not in lowered


class TestTheConstraintSurvivesEveryPathToARenderer:
    def test_the_gpt_image_path_carries_it(self):
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png") as gpt:
            image_gen.render_image_from_prompt(_SCREEN_BRIEF, user_id=3)
        assert "switched off and uniformly dark" in gpt.call_args[0][0]

    def test_the_flux_fallback_carries_it(self):
        with patch.object(image_gen, "_render_via_gpt_image", side_effect=RuntimeError("down")), \
             patch.object(image_gen, "_render_via_flux", return_value="/tmp/f.webp") as flux:
            image_gen.render_image_from_prompt(_SCREEN_BRIEF, user_id=3)
        assert "switched off and uniformly dark" in flux.call_args[0][0]

    def test_the_gated_cover_path_carries_it(self):
        """The path ed9 actually took — `newsletter` is a gate-ENFORCED surface."""
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png") as gpt, \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)):
            image_gen.render_image_gated(_SCREEN_BRIEF, surface="newsletter", ratio="16:9",
                                         focal_concept="the ceiling on an AI line item", user_id=1)
        assert "switched off and uniformly dark" in gpt.call_args[0][0]

    def test_the_repair_round_still_carries_it(self):
        """A retry re-renders from the original prompt, so it must re-acquire the clause."""
        verdicts = [QualityVerdict(acceptable=False, issues=["logo tiles on the laptop screen"]),
                    QualityVerdict(acceptable=True)]
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png") as gpt, \
             patch.object(image_gen, "inspect_render_quality", side_effect=verdicts):
            image_gen.render_image_gated(_SCREEN_BRIEF, surface="newsletter", user_id=1)
        assert gpt.call_count == 2
        assert "switched off and uniformly dark" in gpt.call_args[0][0]

    def test_the_repair_round_never_summons_a_surface_the_scene_never_had(self):
        """The clause set comes from the AUTHOR's scene, never from the repair round.

        `repair_directive`'s FLUX counter for a mark verdict says "screens blank", and its
        gpt-image phrasing quotes the gate's issue strings verbatim ("garbled text on
        whiteboard"). Matching the retry prompt would therefore hand a screenless, boardless
        scene a clause naming a screen — on the backend where naming a thing summons it, and on
        exactly the retries a mark verdict triggers.
        """
        satchel = "A worn leather satchel on a station bench, soft morning light, 50mm at f/2"
        verdicts = [QualityVerdict(acceptable=False, issues=["a company logo rendered into it"]),
                    QualityVerdict(acceptable=True)]
        with patch.object(image_gen, "_render_via_gpt_image", side_effect=RuntimeError("down")), \
             patch.object(image_gen, "_render_via_flux", return_value="/tmp/f.webp") as flux, \
             patch.object(image_gen, "inspect_render_quality", side_effect=verdicts):
            image_gen.render_image_gated(satchel, surface="newsletter", user_id=1)
        assert flux.call_count == 2
        retried = flux.call_args[0][0]
        assert "Render this scene again with" in retried, "the repair round did not run"
        for clause in _clauses():
            assert clause not in retried

    def test_a_repair_round_on_a_gpt_image_render_is_equally_clean(self):
        satchel = "A worn leather satchel on a station bench, soft morning light, 50mm at f/2"
        verdicts = [QualityVerdict(acceptable=False, issues=["garbled text on whiteboard"]),
                    QualityVerdict(acceptable=True)]
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png") as gpt, \
             patch.object(image_gen, "inspect_render_quality", side_effect=verdicts):
            image_gen.render_image_gated(satchel, surface="newsletter", user_id=1)
        assert gpt.call_count == 2
        for clause in _clauses():
            assert clause not in gpt.call_args[0][0]

    def test_the_avatar_repair_round_is_clean_too(self):
        """The likeness path builds its retry the same way, and always renders on FLUX."""
        satchel = "A worn leather satchel on a station bench, soft morning light, 50mm at f/2"
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK",
                  "gender_presentation": "man", "age_band": "40s"}
        verdicts = [QualityVerdict(acceptable=False, issues=["a company logo rendered into it"]),
                    QualityVerdict(acceptable=True)]
        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/a.png", True)) as lora, \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch.object(image_gen, "inspect_render_quality", side_effect=verdicts):
            image_gen.render_avatar_image_gated(satchel, avatar=avatar, user_id=3,
                                                surface="newsletter")
        assert lora.call_count == 2
        for clause in _clauses():
            assert clause not in lora.call_args[0][0]
            assert clause not in lora.call_args[1]["fallback_prompt"]

    def test_the_avatar_path_carries_it(self):
        avatar = {"model_ref": "owner/lora:v1", "trigger_word": "TOK",
                  "gender_presentation": "man", "age_band": "40s"}
        with patch("cqc_lem.utilities.avatar.replicate_avatar.generate_image_with_avatar",
                   return_value=("/tmp/a.png", True)) as lora, \
             patch("cqc_lem.utilities.ai.ai_helper._record_avatar_media"), \
             patch.object(image_gen, "inspect_render_quality",
                          return_value=QualityVerdict(acceptable=True)):
            image_gen.render_avatar_image_gated(_SCREEN_BRIEF, avatar=avatar, user_id=3,
                                                surface="post_image")
        assert "switched off and uniformly dark" in lora.call_args[0][0]
        # The base-Flux fallback render is published too, so it needs the clause as well.
        assert "switched off and uniformly dark" in lora.call_args[1]["fallback_prompt"]

    def test_the_clause_is_added_at_most_once(self):
        once = image_gen.with_no_marks(_SCREEN_BRIEF, "gpt-image")
        assert image_gen.with_no_marks(once, "gpt-image") == once
        # A prompt already carrying one backend's phrasing never gains the other's.
        assert image_gen.with_no_marks(once, "flux") == once
        once_flux = image_gen.with_no_marks(_SCREEN_BRIEF, "flux")
        assert image_gen.with_no_marks(once_flux, "gpt-image") == once_flux


class TestTheGateCanReadTheMarkItIsAskedAbout:
    def _verdict_response(self, payload: dict) -> SimpleNamespace:
        import json
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)))])

    def test_the_gate_prompt_names_screens_as_the_place_to_look(self):
        lowered = image_gen._VISION_GATE_PROMPT.lower()
        for word in ("logo", "app icon", "screen", "laptop display"):
            assert word in lowered

    def test_the_gate_reads_at_a_detail_that_resolves_a_mark(self, tmp_path):
        """At `low` the image is downsampled past the point a screen's logo tiles exist."""
        img = tmp_path / "cover.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.return_value = self._verdict_response(
                {"acceptable": True, "relevance": 5, "issues": []})
            image_gen.inspect_render_quality(str(img), "the ceiling on an AI line item")
        content = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"]
        image_part = next(p for p in content if p["type"] == "image_url")
        assert image_part["image_url"]["detail"] == "high"

    def test_the_gate_still_fails_open(self, tmp_path):
        """R7 is unchanged by making the gate part of this control.

        A vision outage never takes a cover down — it just leaves the human `pending_review`
        gate as the only one standing.
        """
        img = tmp_path / "cover.png"
        img.write_bytes(b"png")
        with patch.object(image_gen, "client") as mock_client:
            mock_client.chat.completions.create.side_effect = RuntimeError("vision down")
            verdict = image_gen.inspect_render_quality(str(img), "focal")
        assert verdict.acceptable and not verdict.checked

    def test_a_rejected_cover_never_blocks_the_render(self):
        """The gate is a safety net, not a bar — after the attempt budget the last render ships."""
        rejected = QualityVerdict(acceptable=False, issues=["a company mark on the laptop screen"])
        with patch.object(image_gen, "_render_via_gpt_image", return_value="/tmp/g.png"), \
             patch.object(image_gen, "inspect_render_quality", return_value=rejected):
            path = image_gen.render_image_gated(_SCREEN_BRIEF, surface="newsletter", user_id=1)
        assert path == "/tmp/g.png"
