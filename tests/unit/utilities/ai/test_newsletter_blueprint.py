"""Unit tests for the newsletter edition blueprint / variety system."""

import pytest

from cqc_lem.utilities.ai import newsletter_blueprint as bp

pytestmark = pytest.mark.unit


class TestSetsAreConsistent:
    def test_formats_non_empty_and_complete(self):
        assert len(bp.FORMATS) >= 6
        for key, meta in bp.FORMATS.items():
            assert key == key.lower().replace(" ", "_")
            assert meta["label"] and meta["guidance"]
            assert isinstance(meta["structure"], list) and len(meta["structure"]) >= 3
            # Every skeleton opens with the hook and closes with the CTA.
            assert "hook" in meta["structure"][0].lower()
            assert "cta" in meta["structure"][-1].lower()

    def test_hooks_non_empty_and_complete(self):
        assert len(bp.HOOK_STYLES) >= 6
        for key, meta in bp.HOOK_STYLES.items():
            assert meta["label"] and meta["guidance"]

    def test_ctas_non_empty_and_complete(self):
        assert len(bp.CTA_STYLES) >= 4
        for key, meta in bp.CTA_STYLES.items():
            assert meta["label"] and meta["guidance"]

    def test_expected_archetypes_present(self):
        for expected in ("deep_dive", "framework", "case_study", "contrarian", "listicle",
                         "teardown", "roundup", "personal_lesson"):
            assert expected in bp.FORMATS


class TestNormalize:
    def test_exact_keys(self):
        assert bp.normalize_format("case_study") == "case_study"
        assert bp.normalize_hook_style("bold_claim") == "bold_claim"
        assert bp.normalize_cta_style("reply_question") == "reply_question"

    def test_tolerates_labels_spaces_and_case(self):
        assert bp.normalize_format("Case Study") == "case_study"
        assert bp.normalize_format("Deep Dive / How-To") == "deep_dive"
        assert bp.normalize_hook_style("Surprising Statistic") == "surprising_stat"
        assert bp.normalize_cta_style("reply question") == "reply_question"

    def test_unknown_returns_none(self):
        assert bp.normalize_format("interpretive dance") is None
        assert bp.normalize_hook_style(None) is None
        assert bp.normalize_cta_style("") is None


class TestEnforceVariety:
    def test_consecutive_formats_and_hooks_never_repeat(self):
        raw = [{"subject": f"S{i}", "format": "deep_dive", "hook_style": "question",
                "cta_style": "reply_question"} for i in range(5)]
        out = bp.enforce_blueprint_variety(raw)
        for a, b in zip(out, out[1:]):
            assert a["format"] != b["format"]
            assert a["hook_style"] != b["hook_style"]
            assert a["cta_style"] != b["cta_style"]

    def test_avoids_recent_history(self):
        raw = [{"subject": "S1"}, {"subject": "S2"}]
        out = bp.enforce_blueprint_variety(raw, recent_formats=["case_study", "listicle"],
                                           recent_hook_styles=["micro_story", "bold_claim"])
        for item in out:
            assert item["format"] not in ("case_study", "listicle")
            assert item["hook_style"] not in ("micro_story", "bold_claim")

    def test_valid_non_repeating_choices_kept(self):
        raw = [{"subject": "A", "format": "case_study", "hook_style": "micro_story", "cta_style": "debate"},
               {"subject": "B", "format": "listicle", "hook_style": "bold_claim", "cta_style": "challenge"}]
        out = bp.enforce_blueprint_variety(raw)
        assert [o["format"] for o in out] == ["case_study", "listicle"]
        assert [o["hook_style"] for o in out] == ["micro_story", "bold_claim"]

    def test_invalid_values_replaced_with_known_keys(self):
        out = bp.enforce_blueprint_variety([{"subject": "A", "format": "sonnet", "hook_style": "smoke signal"}])
        assert out[0]["format"] in bp.FORMATS
        assert out[0]["hook_style"] in bp.HOOK_STYLES
        assert out[0]["cta_style"] in bp.CTA_STYLES

    def test_attaches_structure_and_keeps_subject_angle(self):
        out = bp.enforce_blueprint_variety([{"subject": "A", "angle": "the angle", "format": "framework",
                                             "hook_style": "direct_promise", "cta_style": "teaser_next"}])
        assert out[0]["subject"] == "A" and out[0]["angle"] == "the angle"
        assert out[0]["structure"] == bp.FORMATS["framework"]["structure"]

    def test_no_repeat_within_batch_even_when_pool_pressured(self):
        # 8 formats, batch of 6 + 3 recent: still no within-batch repeats.
        raw = [{"subject": f"S{i}"} for i in range(6)]
        out = bp.enforce_blueprint_variety(raw, recent_formats=["deep_dive", "framework", "case_study"])
        formats = [o["format"] for o in out]
        assert len(set(formats)) == len(formats)

    def test_empty_and_garbage_input(self):
        assert bp.enforce_blueprint_variety([]) == []
        assert bp.enforce_blueprint_variety(None) == []
        assert bp.enforce_blueprint_variety(["not a dict", 42]) == []


class TestRegenerationBlueprint:
    def test_rotates_away_from_recent_shape(self):
        out = bp.build_regeneration_blueprint(subject="S", recent_formats=["deep_dive", "framework"],
                                              recent_hook_styles=["question", "surprising_stat"])
        assert out["format"] in bp.FORMATS and out["format"] not in ("deep_dive", "framework")
        assert out["hook_style"] in bp.HOOK_STYLES
        assert out["hook_style"] not in ("question", "surprising_stat")
        assert out["cta_style"] in bp.CTA_STYLES
        assert out["structure"] == bp.FORMATS[out["format"]]["structure"]
        assert out["subject"] == "S"

    def test_guidance_format_hint_wins(self):
        out = bp.build_regeneration_blueprint(subject="S", recent_formats=["listicle"],
                                              guidance="make this one a Case Study of a real client")
        assert out["format"] == "case_study"

    def test_no_history_still_valid(self):
        out = bp.build_regeneration_blueprint()
        assert out["format"] in bp.FORMATS and out["hook_style"] in bp.HOOK_STYLES


class TestDirectivesAndCompact:
    def test_blueprint_directive_includes_structure_hook_cta(self):
        blueprint = {"subject": "S", "format": "contrarian", "hook_style": "bold_claim",
                     "cta_style": "debate"}
        text = bp.blueprint_directive(blueprint)
        assert "ASSIGNED BLUEPRINT" in text
        assert bp.FORMATS["contrarian"]["label"] in text
        for i, section in enumerate(bp.FORMATS["contrarian"]["structure"], 1):
            assert f"{i}. {section}" in text
        assert bp.HOOK_STYLES["bold_claim"]["guidance"] in text
        assert bp.CTA_STYLES["debate"]["guidance"] in text

    def test_blueprint_directive_empty_without_format(self):
        assert bp.blueprint_directive(None) == ""
        assert bp.blueprint_directive({}) == ""
        assert bp.blueprint_directive({"format": "not real"}) == ""

    def test_compact_drops_structure(self):
        blueprint = {"subject": "S", "angle": "a", "format": "listicle", "hook_style": "question",
                     "cta_style": "poll_prompt", "structure": ["x", "y"]}
        compact = bp.compact_blueprint(blueprint)
        assert compact == {"subject": "S", "angle": "a", "format": "listicle",
                           "hook_style": "question", "cta_style": "poll_prompt"}
        assert bp.compact_blueprint(None) is None

    def test_options_text_lists_all_keys(self):
        text = bp.blueprint_options_text()
        for key in list(bp.FORMATS) + list(bp.HOOK_STYLES) + list(bp.CTA_STYLES):
            assert key in text
