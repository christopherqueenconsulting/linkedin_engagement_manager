"""The image engine's writer side and checking side must stay pinned to each other (issue #1141).

`image_brief` has two halves that can silently disagree: the shared system prompt, which states
the photographic hard rules, and the per-surface presets, which are pasted into the SAME request.
The `thumbnail` preset asked for an "illustration" — the exact word the system prompt calls a dead
word — and nothing compared the two, so the contradiction shipped. These tests are that comparison.

They also pin the OTHER half of the audit's finding: a preset that no caller ever passes is a
preset nobody can see is wrong. `thumbnail` was unreachable for its whole life because the one
surface that wanted it hand-wrote its own prompt instead.
"""

import re
from pathlib import Path

import pytest

from cqc_lem.utilities.ai.image_brief import (
    _STYLE_PRESETS,
    _SYSTEM_PROMPT,
    _TREATMENT_TEMPLATES,
    DEAD_QUALITY_TAGS,
    DEAD_STYLE_WORDS,
    NEGATION_MARKERS,
    _fallback_brief,
)
from cqc_lem.utilities.ai.image_concept import ART_STYLES

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[4] / "src" / "cqc_lem"

# Negation is not a style question: FLUX has no negative prompting and renders what a prompt
# NAMES, so "no logos" is how a logo gets there. Every one of these shipped in a real prompt. The
# list moved into `image_brief` (issue #1376) so the render-side mark guard greps the SAME one.
_NEGATION_MARKERS = NEGATION_MARKERS


class TestPresetsObeyTheSharedSystemPrompt:
    def test_the_system_prompt_names_the_shared_ban_lists(self):
        """One list, named where the model reads it and where the test greps it."""
        for word in DEAD_STYLE_WORDS + DEAD_QUALITY_TAGS:
            assert word in _SYSTEM_PROMPT, f"{word!r} is banned but never stated to the writer"

    @pytest.mark.parametrize("surface", sorted(_STYLE_PRESETS))
    def test_no_preset_asks_for_a_medium_the_system_prompt_bans(self, surface):
        lowered = _STYLE_PRESETS[surface].lower()
        for word in DEAD_STYLE_WORDS + DEAD_QUALITY_TAGS:
            assert word.lower() not in lowered, (
                f"the {surface!r} preset asks for {word!r}, which the shared system prompt "
                f"forbids in the same request")

    @pytest.mark.parametrize("surface", sorted(_STYLE_PRESETS))
    def test_no_preset_phrases_a_constraint_as_negation(self, surface):
        lowered = _STYLE_PRESETS[surface].lower()
        for marker in _NEGATION_MARKERS:
            assert marker not in lowered, (
                f"the {surface!r} preset uses {marker!r}; the renderer ignores negation and "
                f"renders what the prompt names")

    @pytest.mark.parametrize("treatment", sorted(_TREATMENT_TEMPLATES))
    def test_no_treatment_template_breaks_the_shared_rules(self, treatment):
        """Issue #2241: a treatment template is pasted into the same request as a preset."""
        lowered = _TREATMENT_TEMPLATES[treatment].lower()
        for word in DEAD_STYLE_WORDS + DEAD_QUALITY_TAGS:
            assert word.lower() not in lowered, f"{treatment!r} asks for {word!r}"
        for marker in _NEGATION_MARKERS:
            assert marker not in lowered, f"{treatment!r} uses {marker!r}"

    def test_photorealistic_is_allowed_again_and_only_that_word(self):
        """Gauntlet round 1 of #2241 removed ONE word from the dead list, on evidence.

        OpenAI's gpt-image prompting guide recommends "photorealistic" paired with real texture
        (pores, fabric wear, imperfections) and no studio polish:
        https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide
        Every other dead tag stays dead.
        """
        assert "photorealistic" not in DEAD_QUALITY_TAGS
        assert set(DEAD_QUALITY_TAGS) == {"cinematic", "8k", "masterpiece", "ultra-detailed"}

    def test_every_treatment_has_a_template(self):
        from cqc_lem.utilities.ai.image_concept import TREATMENT_EDITORIAL, TREATMENTS
        # editorial_concept is set by the archetype selection, never offered to the analyst.
        assert set(_TREATMENT_TEMPLATES) == set(TREATMENTS) | {TREATMENT_EDITORIAL}

    @pytest.mark.parametrize("style", sorted(ART_STYLES))
    def test_every_art_style_obeys_the_shared_rules(self, style):
        """Archetype round: a rotated art style lands in the same request as the template."""
        from cqc_lem.utilities.ai.image_brief import prop_failure, treatment_text
        from cqc_lem.utilities.ai.image_concept import TREATMENT_EDITORIAL

        text = ART_STYLES[style]
        lowered = treatment_text(TREATMENT_EDITORIAL, type("C", (), {"art_style": style})())
        lowered = lowered.lower()
        assert text in treatment_text(TREATMENT_EDITORIAL,
                                      type("C", (), {"art_style": style})())
        for word in DEAD_STYLE_WORDS + DEAD_QUALITY_TAGS:
            assert word.lower() not in lowered, f"{style!r} asks for {word!r}"
        for marker in _NEGATION_MARKERS:
            assert marker not in lowered, f"{style!r} uses {marker!r}"
        assert prop_failure(text) is None, f"{style!r} names a prop the brief refuses"

    def test_the_deterministic_fallback_obeys_the_same_rules(self):
        """The fallback is a working code path — it renders when the author is down."""
        prompt = _fallback_brief("a post about routing costs", surface="thumbnail",
                                 ratio="16:9", context="").prompt.lower()
        for marker in _NEGATION_MARKERS:
            assert marker not in prompt
        for word in DEAD_STYLE_WORDS + DEAD_QUALITY_TAGS:
            assert word.lower() not in prompt


class TestEveryPresetIsReachable:
    """A preset no caller selects is art direction that never reaches a render."""

    def test_every_preset_is_named_by_a_real_caller(self):
        sources = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                            for p in _SRC.rglob("*.py"))
        for surface in _STYLE_PRESETS:
            assert re.search(rf"""surface\s*=\s*["']{re.escape(surface)}["']""", sources), (
                f"no caller passes surface={surface!r}, so its preset can never be applied — "
                f"either wire the surface up or drop the preset")
