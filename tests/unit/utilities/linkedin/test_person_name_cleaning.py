"""Scraped display-name hygiene + connection-degree detection (issue #623).

The strings here are the shapes LinkedIn's SDUI actually produces inside a profile link — including
the exact value that landed in production's one and only connection_requests row.
"""

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def helper():
    # Imported lazily: helper.py pulls in the AI client, which needs the session env fixture.
    from cqc_lem.utilities.linkedin import helper as h
    return h


class TestCleanPersonName:
    def test_strips_verified_badge_and_degree_from_the_production_row(self, helper):
        assert helper.clean_person_name("Harshal Karanpuriya Verified Profile 1st") == \
            "Harshal Karanpuriya"

    def test_strips_bullet_separated_degree(self, helper):
        assert helper.clean_person_name("Jane Doe • 2nd") == "Jane Doe"
        assert helper.clean_person_name("Jane Doe · 3rd+") == "Jane Doe"

    def test_strips_multiline_sdui_stack(self, helper):
        raw = "Jane Doe\nJane Doe\n• 3rd+\nSenior Director at Acme"
        assert helper.clean_person_name(raw) == "Jane Doe"

    def test_takes_the_name_out_of_an_aria_label(self, helper):
        assert helper.clean_person_name("View Jane Doe’s profile") == "Jane Doe"
        assert helper.clean_person_name("View Jane Doe's profile") == "Jane Doe"

    def test_skips_leading_status_badges(self, helper):
        assert helper.clean_person_name("Status is online\nJane Doe\n• 1st") == "Jane Doe"
        assert helper.clean_person_name("Status is offline\nJane Doe") == "Jane Doe"

    def test_collapses_the_duplicated_screen_reader_copy(self, helper):
        assert helper.clean_person_name("Jane Doe Jane Doe") == "Jane Doe"

    def test_strips_degree_written_out_in_full(self, helper):
        assert helper.clean_person_name("Jane Doe 1st degree connection") == "Jane Doe"

    def test_strips_premium_influencer_and_hiring_badges(self, helper):
        assert helper.clean_person_name("Jane Doe Premium") == "Jane Doe"
        assert helper.clean_person_name("Jane Doe Influencer 2nd") == "Jane Doe"
        assert helper.clean_person_name("Jane Doe is hiring") == "Jane Doe"
        assert helper.clean_person_name("Jane Doe Open to work") == "Jane Doe"

    def test_normalizes_whitespace_and_non_breaking_spaces(self, helper):
        assert helper.clean_person_name("  Jane   Doe  ") == "Jane Doe"
        assert helper.clean_person_name("Jane Doe  • 1st") == "Jane Doe"

    def test_keeps_credentials_and_punctuation_in_a_real_name(self, helper):
        assert helper.clean_person_name("Jane Doe, PhD") == "Jane Doe, PhD"
        assert helper.clean_person_name("Renée O'Brien-Smith 2nd") == "Renée O'Brien-Smith"

    def test_returns_empty_when_nothing_name_like_survives(self, helper):
        assert helper.clean_person_name("• 1st") == ""
        assert helper.clean_person_name("Status is online") == ""
        assert helper.clean_person_name("") == ""
        assert helper.clean_person_name(None) == ""

    def test_caps_at_the_column_width(self, helper):
        assert len(helper.clean_person_name("x" * 400)) == 255


class TestConnectionDegree:
    @pytest.mark.parametrize("raw,expected", [
        ("Jane Doe Verified Profile 1st", "1st"),
        ("Jane Doe • 2nd", "2nd"),
        ("Jane Doe • 3rd", "3rd+"),
        ("Jane Doe • 3rd+", "3rd+"),
        ("Jane Doe 1ST DEGREE CONNECTION", "1st"),
        ("Jane Doe", None),
        ("", None),
        (None, None),
    ])
    def test_reads_the_badge(self, helper, raw, expected):
        assert helper.connection_degree(raw) == expected

    def test_does_not_fire_on_a_word_that_merely_contains_a_degree_token(self, helper):
        assert helper.connection_degree("Jane Doe1st") is None
        assert helper.connection_degree("Christ2ndsen") is None

    def test_is_first_degree_only_on_an_explicit_first_degree_badge(self, helper):
        assert helper.is_first_degree("Jane Doe • 1st") is True
        assert helper.is_first_degree("Jane Doe • 2nd") is False
        # A missing badge is UNKNOWN, not "not connected" — callers must fail open.
        assert helper.is_first_degree("Jane Doe") is False
        assert helper.is_first_degree("") is False
