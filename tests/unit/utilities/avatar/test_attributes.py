"""Unit tests for user-declared avatar likeness attributes (issue #744, decision 3A).

The load-bearing rule here is negative: an undeclared attribute must render NOTHING. Every
inference this module could plausibly make is a bug, so most of these tests assert absence.
"""
import pytest

from cqc_lem.utilities.avatar.attributes import (
    AGE_BANDS,
    GENDER_PRESENTATIONS,
    apply_subject_clause,
    normalize_age_band,
    normalize_gender_presentation,
    subject_clause,
    subject_directive,
)

pytestmark = pytest.mark.unit


class TestNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("man", "man"),
        ("WOMAN", "woman"),
        ("  Non-Binary  ", "non-binary"),
        ("non_binary", "non-binary"),
        ("prefer-not-to-say", "prefer-not-to-say"),
    ])
    def test_recognized_values(self, raw, expected):
        assert normalize_gender_presentation(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "male-presenting-ish", "unknown", "42"])
    def test_unrecognized_is_none(self, raw):
        assert normalize_gender_presentation(raw) is None

    @pytest.mark.parametrize("raw,expected", [("40s", "40s"), (" 70+ ", "70+"), ("20S", "20s")])
    def test_age_bands(self, raw, expected):
        assert normalize_age_band(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "middle-aged", "45", "80s"])
    def test_unrecognized_age_is_none(self, raw):
        assert normalize_age_band(raw) is None

    def test_every_declared_band_normalizes(self):
        assert all(normalize_age_band(b) == b for b in AGE_BANDS)

    def test_every_declared_gender_normalizes(self):
        assert all(normalize_gender_presentation(g) == g for g in GENDER_PRESENTATIONS)


class TestSubjectClause:
    @pytest.mark.parametrize("avatar", [
        None,
        {},
        {"gender_presentation": None, "age_band": None},
        {"gender_presentation": "", "age_band": ""},
        {"gender_presentation": "garbage", "age_band": "garbage"},
    ])
    def test_unset_renders_nothing(self, avatar):
        """Never guessed: no attributes means no clause at all, not a default person."""
        assert subject_clause(avatar) == ""

    def test_gender_and_age(self):
        assert subject_clause({"gender_presentation": "man", "age_band": "40s"}) \
            == "a man in his 40s"

    def test_woman_uses_her(self):
        assert subject_clause({"gender_presentation": "woman", "age_band": "30s"}) \
            == "a woman in her 30s"

    def test_non_binary_uses_their(self):
        assert subject_clause({"gender_presentation": "non-binary", "age_band": "50s"}) \
            == "a non-binary person in their 50s"

    def test_gender_only(self):
        assert subject_clause({"gender_presentation": "man"}) == "a man"

    def test_age_only_uses_a_neutral_noun(self):
        """Declaring an age is not a claim about gender — the noun stays neutral."""
        assert subject_clause({"age_band": "30s"}) == "a person in their 30s"

    def test_prefer_not_to_say_contributes_no_noun(self):
        assert subject_clause({"gender_presentation": "prefer-not-to-say"}) == ""
        assert subject_clause({"gender_presentation": "prefer-not-to-say", "age_band": "60s"}) \
            == "a person in their 60s"

    def test_seventy_plus_is_not_rendered_as_a_decade(self):
        assert subject_clause({"gender_presentation": "woman", "age_band": "70+"}) \
            == "a woman in her 70s or older"


class TestSubjectDirective:
    def test_empty_when_nothing_declared(self):
        assert subject_directive({}) == ""
        assert subject_directive(None) == ""

    def test_names_the_author_and_forbids_contradiction(self):
        directive = subject_directive({"gender_presentation": "man", "age_band": "40s"})
        assert "a man in his 40s" in directive
        assert "IS the author" in directive
        assert directive.endswith("\n\n")


class TestApplySubjectClause:
    def test_trigger_word_leads_then_clause_then_scene(self):
        avatar = {"trigger_word": "LEMAVTR1", "gender_presentation": "man", "age_band": "40s"}
        assert apply_subject_clause("at a whiteboard", avatar) \
            == "LEMAVTR1, a man in his 40s, at a whiteboard"

    def test_no_attributes_leaves_trigger_plus_prompt(self):
        avatar = {"trigger_word": "LEMAVTR1"}
        assert apply_subject_clause("at a whiteboard", avatar) == "LEMAVTR1, at a whiteboard"

    def test_no_avatar_returns_the_prompt_unchanged(self):
        assert apply_subject_clause("at a whiteboard", None) == "at a whiteboard"

    def test_blank_pieces_are_dropped(self):
        assert apply_subject_clause("  ", {"trigger_word": " LEMAVTR1 "}) == "LEMAVTR1"
