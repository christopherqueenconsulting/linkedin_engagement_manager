"""The occasion/milestone archetype family (issue #1074).

These two archetypes announce a REAL dated event, so the thing worth testing is not that they
exist — it is that nothing can pick one by accident. A rotation, a planner menu or a variety repair
that could land on `project_launch` would eventually announce a launch that never happened.
"""

from collections import Counter

import pytest

from cqc_lem.utilities.ai.content_framework import (
    OCCASION_FORMAT_KEYS,
    blueprint_directive,
    enforce_variety,
    is_occasion_format,
    occasion_formats,
    occasion_stage,
    options_text,
    requires_fact_anchor,
    select_blueprint,
    structure_for,
)

pytestmark = pytest.mark.unit


class TestOccasionFamily:
    def test_both_archetypes_are_registered(self):
        assert occasion_formats("post") == list(OCCASION_FORMAT_KEYS)
        assert set(OCCASION_FORMAT_KEYS) == {"project_launch", "educational_milestone"}

    @pytest.mark.parametrize("key", OCCASION_FORMAT_KEYS)
    def test_each_carries_a_structure_and_the_occasion_flag(self, key):
        assert is_occasion_format("post", key) is True
        assert len(structure_for("post", key)) >= 4

    @pytest.mark.parametrize("key", OCCASION_FORMAT_KEYS)
    def test_each_is_fact_anchored(self, key):
        # The occasion the author typed is the only specific the writer may state.
        assert requires_fact_anchor("post", key) is True

    def test_a_normal_archetype_is_not_an_occasion(self):
        assert is_occasion_format("post", "personal_lesson") is False
        assert is_occasion_format("post", None) is False

    def test_stage_differs_by_archetype(self):
        assert occasion_stage("post", "project_launch") == "decision"
        assert occasion_stage("post", "educational_milestone") == "awareness"
        # An unknown key falls back rather than raising — stage never blocks a draft.
        assert occasion_stage("post", "not_a_format") == "awareness"


class TestNeverPickedAutomatically:
    def test_rotation_never_lands_on_an_occasion_archetype(self):
        picks = Counter(select_blueprint("post")["format"] for _ in range(300))
        assert not (set(picks) & set(OCCASION_FORMAT_KEYS))

    def test_day_type_family_cannot_pull_one_in_by_accident(self):
        # A day-type family that names none of them leaves them off the menu entirely.
        picks = {select_blueprint("post", preferred_formats=["how_to", "tactical_list"])["format"]
                 for _ in range(50)}
        assert picks <= {"how_to", "tactical_list"}

    def test_planner_menu_omits_them(self):
        menu = options_text("post")
        for key in OCCASION_FORMAT_KEYS:
            assert key not in menu
        assert "personal_lesson" in menu

    def test_variety_repair_reassigns_into_the_rotation_menu(self):
        # A batch of unknown formats is repaired — never into an occasion announcement.
        repaired = enforce_variety("post", [{"format": "nonsense"}] * 6)
        assert not (set(b["format"] for b in repaired) & set(OCCASION_FORMAT_KEYS))

    def test_variety_leaves_a_seeded_occasion_post_alone(self):
        # An occasion blueprint was seeded by a human naming a real event; repairing its shape
        # would silently rewrite the announcement into something else.
        repaired = enforce_variety("post", [{"format": "project_launch"}])
        assert repaired[0]["format"] == "project_launch"


class TestReachableWhenNamed:
    @pytest.mark.parametrize("key", OCCASION_FORMAT_KEYS)
    def test_preferred_formats_selects_it(self, key):
        assert select_blueprint("post", preferred_formats=[key])["format"] == key

    @pytest.mark.parametrize("key", OCCASION_FORMAT_KEYS)
    def test_guidance_selects_it(self, key):
        assert select_blueprint("post", guidance=key)["format"] == key

    @pytest.mark.parametrize("key", OCCASION_FORMAT_KEYS)
    def test_writer_directive_carries_the_shape(self, key):
        directive = blueprint_directive("post", select_blueprint("post", preferred_formats=[key]))
        assert "STRUCTURE" in directive
        # The no-fabrication rules ride along because the archetype is fact-anchored.
        assert "NO-FABRICATION" in directive.upper()
