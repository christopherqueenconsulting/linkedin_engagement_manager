"""Pins the flip-rate measurement behind issue #1266.

The script is what turns "tightening the proof detector costs regenerations" from an assumption
into a number, so its two halves have to be right: the LEGACY regex it compares against must be
the one production actually ran, and a flip must mean the post LOST its only proof — not that it
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
# Proof under neither — it was already being regenerated, so it is not part of this change's cost.
_NEVER = "Authenticity is what wins on LinkedIn, and consistency is how influence compounds."


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("measure_proof_gate_impact", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(*texts) -> list:
    return [{"ref_id": str(i), "text": t} for i, t in enumerate(texts, start=1)]


class TestMeasure:
    def test_only_a_post_that_loses_its_proof_counts_as_a_flip(self, tool):
        result = tool.measure(_rows(_FLIPS, _KEEPS, _NEVER))
        assert (result["posts"], result["had_proof"], result["flipped"]) == (3, 2, 1)
        assert [f["ref_id"] for f in result["flips"]] == ["1"]
        assert result["flip_rate"] == 50.0

    def test_the_flip_names_the_sentence_that_used_to_pass(self, tool):
        # Without this the report is a bare count and nobody can judge whether the old verdict was
        # ever real proof.
        assert tool.measure(_rows(_FLIPS))["flips"][0]["was_proof"] == [_FLIPS]

    def test_a_post_with_a_second_anchor_does_not_flip(self, tool):
        # The tightening removes ONE signal from a sentence, never a whole post.
        text = f"{_FLIPS}\n\nI rewrote our scope template in one afternoon after that."
        assert tool.measure(_rows(text))["flipped"] == 0

    def test_empty_corpus_reports_zero_rather_than_dividing_by_it(self, tool):
        assert tool.measure([]) == {"posts": 0, "had_proof": 0, "flipped": 0, "flip_rate": 0.0,
                                    "flips": []}

    def test_a_missing_body_is_not_a_crash(self, tool):
        assert tool.measure([{"ref_id": "9"}, None])["posts"] == 2


class TestCollect:
    def test_only_shipped_posts_are_read(self, tool, monkeypatch):
        # The reader returns all three writing surfaces; comments and newsletters run different
        # gates and would make the measured rate answer a question nobody asked.
        import cqc_lem.utilities.db as db

        monkeypatch.setattr(db, "get_shipped_content_for_quality", lambda user_id, days: [
            {"surface": "post", "ref_id": "1", "text": _KEEPS},
            {"surface": "comment", "ref_id": "2", "text": _FLIPS},
            {"surface": "newsletter", "ref_id": "3", "text": _FLIPS},
        ])
        rows = tool.collect([1], days=30)
        assert [r["ref_id"] for r in rows] == ["1"]


class TestLegacyDetector:
    def test_legacy_regex_still_counts_the_determiner(self, tool):
        # If this stops holding, the script compares the new detector against itself and every
        # flip rate it prints is 0%.
        assert tool.legacy_proof_sentences(_FLIPS)

    def test_legacy_and_current_agree_on_real_proof(self, tool):
        from cqc_lem.utilities.ai.content_framework import has_first_person_proof

        assert tool.legacy_proof_sentences(_KEEPS)
        assert has_first_person_proof(_KEEPS)
