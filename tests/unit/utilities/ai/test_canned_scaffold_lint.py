"""Unit tests for the canned-scaffold slop check (issue #1138).

The check exists because LEM's own post system prompts used to hand the writer literal templates
("In my experience as a [Job Title]…"). Nothing in the pipeline caught them: they carry no tier-1
tell word, trip no other slop check, and — because "one" reads as a concrete-specificity signal —
they even satisfy the A2 first-person proof slot. These tests pin both halves: it fires on the
sampled scaffolds, and it never HOLDS a draft.
"""

import pytest

from cqc_lem.utilities.ai.content_framework import POST_BANNED_SCAFFOLDS, has_first_person_proof
from cqc_lem.utilities.ai.slop_lint import (
    CHECK_SCAFFOLD,
    banned_scaffolds,
    find_canned_scaffolds,
    lint_report,
)

pytestmark = pytest.mark.unit

# The sentence sampled from the pre-#1138 thought-leadership system prompt, with a real job title
# filled into the bracket — the exact shape the check has to catch.
CANNED = ("In my experience as a Solutions Architect, one of the biggest challenges in consulting "
          "today is scope creep.")
# Same author, same claim, written with the specifics the contract asks for.
SPECIFIC = ("Last March I rebuilt our CI pipeline after three prod outages traced to a flaky Redis "
            "lock. Deploys went from 22 minutes to 9.")


def _violation(report: dict) -> dict:
    return next((v for v in report["violations"] if v["check"] == CHECK_SCAFFOLD), None)


class TestFindCannedScaffolds:
    def test_matches_the_prefix_not_the_placeholder(self):
        assert "in my experience as a" in find_canned_scaffolds(CANNED)
        assert "in my experience as a" in find_canned_scaffolds(
            "In my experience as a [Job Title], deadlines slip.")

    def test_case_and_smart_quote_insensitive(self):
        assert find_canned_scaffolds("IN MY EXPERIENCE AS A director, headcount was the bottleneck.")
        assert find_canned_scaffolds("A strategy I’ve found effective is cutting the standup.")

    def test_specific_first_person_writing_is_left_alone(self):
        assert find_canned_scaffolds(SPECIFIC) == []

    def test_empty_text_is_not_a_violation(self):
        assert find_canned_scaffolds(None) == []
        assert find_canned_scaffolds("") == []


class TestScaffoldCheck:
    def test_fires_on_a_post_but_never_holds_it(self):
        report = lint_report(CANNED, "post")
        found = _violation(report)
        assert found is not None
        assert found["severity"] == "warn"
        # WARN means recorded, never blocking — the gate suite must be unchanged by this check.
        assert report["passes"] is True
        assert found["score"] == float(len(found["evidence"]))

    def test_counts_every_distinct_scaffold(self):
        report = lint_report(CANNED + " What strategies have you found effective for that?", "post")
        found = _violation(report)
        assert found["score"] >= 3.0
        assert "in my experience as a" in found["evidence"]
        assert "what strategies have you found effective for" in found["evidence"]

    def test_reason_names_the_phrase(self):
        reasons = " ".join(lint_report(CANNED, "post")["reasons"])
        assert "canned_scaffold" in reasons
        assert "in my experience as a" in reasons

    def test_specific_post_has_no_scaffold_violation(self):
        assert _violation(lint_report(SPECIFIC, "post")) is None

    @pytest.mark.parametrize("surface", ["comment", "newsletter", "dm"])
    def test_post_only(self, surface):
        # Comments run their own filler-opener contract; the post list was never sampled there.
        assert _violation(lint_report(CANNED, surface)) is None

    def test_severity_is_promotable(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_SEVERITY_CANNED_SCAFFOLD", "hard")
        report = lint_report(CANNED, "post")
        assert _violation(report)["severity"] == "hard"
        assert report["passes"] is False

    def test_severity_off_removes_it(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_SEVERITY_CANNED_SCAFFOLD", "off")
        assert _violation(lint_report(CANNED, "post")) is None

    def test_disabled_linter_fails_open(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_ENABLED", "false")
        report = lint_report(CANNED, "post")
        assert report == {"passes": True, "violations": [], "hard": [], "warnings": [],
                          "reasons": [], "checked": False}


class TestSharedList:
    def test_the_writer_side_and_the_checking_side_read_one_list(self):
        from cqc_lem.utilities.ai.content_framework import post_writing_directive

        directive = post_writing_directive()
        assert "NEVER reach for scaffolding" in directive
        # Whatever the directive names as banned must be what the checker actually greps for.
        for phrase in POST_BANNED_SCAFFOLDS[:8]:
            assert f"'{phrase}'" in directive
            assert phrase in banned_scaffolds()

    def test_env_extends_but_never_shrinks(self, monkeypatch):
        # Comma-separated, like every other SLOP_LINT_EXTRA_* knob — so an entry may not contain one.
        monkeypatch.setenv("SLOP_LINT_EXTRA_SCAFFOLDS", "as a seasoned professional in")
        extended = banned_scaffolds()
        assert "as a seasoned professional in" in extended
        assert set(POST_BANNED_SCAFFOLDS).issubset(set(extended))


class TestWhyThisCheckWasNeeded:
    def test_the_canned_sentence_slipped_every_other_gate(self):
        # Regression pin for the audit's core finding: before this check, the templated sentence
        # was invisible to the lint AND counted as the A2 first-person proof slot.
        report = lint_report(CANNED, "post")
        assert [v["check"] for v in report["violations"]] == [CHECK_SCAFFOLD]
        assert has_first_person_proof(CANNED) is True
