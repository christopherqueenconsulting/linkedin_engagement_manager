"""Pins the flip-rate measurement behind issue #1266.

The script is what turns "tightening the proof detector costs regenerations" from an assumption
into a number, so its two halves have to be right: the LEGACY regex it compares against must be
the one production actually ran, and a flip must mean the post lost its only proof — not that it
never had any.
"""

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/measure_proof_gate_impact.py")

# The canned scaffold from the #1138 audit: proof under the old detector, none under the new one.
_FLIPS = ("In my experience as a Solutions Architect, one of the biggest challenges in consulting "
          "today is scope creep.")
# Real proof under both.
_KEEPS = "Last March I cut our deploy time from 22 minutes to 9."
# Proof under neither — it was never counted, so it is not a flip.
_NEVER = "Authenticity is what wins on LinkedIn, and consistency is how influence compounds."


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("measure_proof_gate_impact", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMeasure:
    def test_only_a_post_that_loses_its_proof_counts_as_a_flip(self, tool):
        result = tool.measure([_FLIPS, _KEEPS, _NEVER])
        assert result == {"posts": 3, "had_proof": 2, "flipped": 1, "flipped_texts": [_FLIPS]}

    def test_a_post_with_a_second_anchor_does_not_flip(self, tool):
        # The tightening removes ONE signal from a sentence, never the whole post.
        text = f"{_FLIPS}\n\nI rewrote our scope template in one afternoon after that."
        assert tool.measure([text])["flipped"] == 0

    def test_empty_corpus_is_zero_not_a_crash(self, tool):
        assert tool.measure([]) == {"posts": 0, "had_proof": 0, "flipped": 0, "flipped_texts": []}


class TestLegacyDetector:
    def test_legacy_regex_still_counts_the_determiner(self, tool):
        # If this stops holding, the script is comparing the new detector against itself and every
        # measured flip rate it prints is 0%.
        assert tool.legacy_proof_sentences(_FLIPS)

    def test_legacy_and_current_agree_on_real_proof(self, tool):
        from cqc_lem.utilities.ai.content_framework import has_first_person_proof

        assert tool.legacy_proof_sentences(_KEEPS)
        assert has_first_person_proof(_KEEPS)
