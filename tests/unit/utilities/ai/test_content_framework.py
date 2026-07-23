"""Unit tests for the SHARED content framework core (blueprint menus + variety engine) used by
newsletters, posts, AND comments. The newsletter-menu tests preserve the exact behavior contracts
of the pre-unification newsletter blueprint system."""

import pytest

from cqc_lem.utilities.ai import content_framework as fw

pytestmark = pytest.mark.unit


class TestMenusAreConsistent:
    def test_newsletter_formats_non_empty_and_complete(self):
        assert len(fw.NEWSLETTER_FORMATS) >= 6
        for key, meta in fw.NEWSLETTER_FORMATS.items():
            assert key == key.lower().replace(" ", "_")
            assert meta["label"] and meta["guidance"]
            assert isinstance(meta["structure"], list) and len(meta["structure"]) >= 3
            # Every skeleton opens with the hook and closes with the CTA.
            assert "hook" in meta["structure"][0].lower()
            assert "cta" in meta["structure"][-1].lower()

    def test_post_formats_non_empty_and_complete(self):
        assert len(fw.POST_FORMATS) >= 6
        for key, meta in fw.POST_FORMATS.items():
            assert key == key.lower().replace(" ", "_")
            assert meta["label"] and meta["guidance"]
            assert isinstance(meta["structure"], list) and len(meta["structure"]) >= 3
            assert "hook" in meta["structure"][0].lower()
            assert "cta" in meta["structure"][-1].lower()

    def test_comment_formats_non_empty_and_grounded(self):
        assert len(fw.COMMENT_FORMATS) >= 5
        for key, meta in fw.COMMENT_FORMATS.items():
            assert meta["label"] and meta["guidance"]
            assert isinstance(meta["structure"], list) and len(meta["structure"]) >= 2
            # Every comment angle stays anchored to the TARGET POST.
            blob = (meta["guidance"] + " ".join(meta["structure"])).lower()
            assert "post" in blob

    def test_hooks_and_ctas_non_empty(self):
        assert len(fw.HOOK_STYLES) >= 6
        for key, meta in fw.HOOK_STYLES.items():
            assert meta["label"] and meta["guidance"]
        assert len(fw.CTA_STYLES) >= 4
        for key, meta in fw.CTA_STYLES.items():
            assert meta["label"] and meta["guidance"]

    def test_expected_newsletter_archetypes_present(self):
        for expected in ("deep_dive", "framework", "case_study", "contrarian", "listicle",
                         "teardown", "roundup", "personal_lesson"):
            assert expected in fw.NEWSLETTER_FORMATS

    def test_expected_post_and_comment_archetypes_present(self):
        for expected in ("personal_lesson", "contrarian_take", "tactical_list", "how_to",
                         "case_snapshot", "industry_observation", "question_starter"):
            assert expected in fw.POST_FORMATS
        for expected in ("expander", "storyteller", "questioner", "respectful_contrarian",
                         "connector", "practical_add"):
            assert expected in fw.COMMENT_FORMATS

    def test_hooks_shared_between_newsletter_and_post(self):
        # ONE hook menu object — the definition can't drift between content types.
        assert fw.MENUS["newsletter"]["hooks"] is fw.MENUS["post"]["hooks"]

    def test_post_ctas_reuse_shared_definitions(self):
        for key in ("reply_question", "challenge", "debate", "poll_prompt"):
            assert fw.POST_CTA_STYLES[key] is fw.CTA_STYLES[key]
        assert "teaser_next" not in fw.POST_CTA_STYLES  # subscribe-teasers are newsletter-only

    def test_unknown_content_type_raises(self):
        with pytest.raises(ValueError):
            fw.options_text("tweet")


class TestNormalize:
    def test_exact_keys(self):
        assert fw.normalize_key("newsletter", "format", "case_study") == "case_study"
        assert fw.normalize_key("newsletter", "hook", "bold_claim") == "bold_claim"
        assert fw.normalize_key("newsletter", "cta", "reply_question") == "reply_question"
        assert fw.normalize_key("post", "format", "contrarian_take") == "contrarian_take"
        assert fw.normalize_key("comment", "format", "storyteller") == "storyteller"

    def test_tolerates_labels_spaces_and_case(self):
        assert fw.normalize_key("newsletter", "format", "Case Study") == "case_study"
        assert fw.normalize_key("newsletter", "format", "Deep Dive / How-To") == "deep_dive"
        assert fw.normalize_key("newsletter", "hook", "Surprising Statistic") == "surprising_stat"
        assert fw.normalize_key("newsletter", "cta", "reply question") == "reply_question"
        assert fw.normalize_key("comment", "format", "Thoughtful Questioner") == "questioner"

    def test_unknown_returns_none(self):
        assert fw.normalize_key("newsletter", "format", "interpretive dance") is None
        assert fw.normalize_key("post", "hook", None) is None
        assert fw.normalize_key("newsletter", "cta", "") is None
        # A newsletter-only format is NOT a valid post format.
        assert fw.normalize_key("post", "format", "roundup") is None

    def test_comment_menu_has_no_hooks_or_ctas(self):
        assert fw.normalize_key("comment", "hook", "bold_claim") is None
        assert fw.normalize_key("comment", "cta", "reply_question") is None


class TestEnforceVariety:
    @pytest.mark.parametrize("content_type", ["newsletter", "post"])
    def test_consecutive_formats_and_hooks_never_repeat(self, content_type):
        first = list(fw.MENUS[content_type]["formats"])[0]
        raw = [{"subject": f"S{i}", "format": first, "hook_style": "question",
                "cta_style": "reply_question"} for i in range(5)]
        out = fw.enforce_variety(content_type, raw)
        for a, b in zip(out, out[1:]):
            assert a["format"] != b["format"]
            assert a["hook_style"] != b["hook_style"]
            assert a["cta_style"] != b["cta_style"]

    def test_avoids_recent_history(self):
        raw = [{"subject": "S1"}, {"subject": "S2"}]
        out = fw.enforce_variety("newsletter", raw, recent_formats=["case_study", "listicle"],
                                 recent_hook_styles=["micro_story", "bold_claim"])
        for item in out:
            assert item["format"] not in ("case_study", "listicle")
            assert item["hook_style"] not in ("micro_story", "bold_claim")

    def test_valid_non_repeating_choices_kept(self):
        raw = [{"subject": "A", "format": "case_study", "hook_style": "micro_story", "cta_style": "debate"},
               {"subject": "B", "format": "listicle", "hook_style": "bold_claim", "cta_style": "challenge"}]
        out = fw.enforce_variety("newsletter", raw)
        assert [o["format"] for o in out] == ["case_study", "listicle"]
        assert [o["hook_style"] for o in out] == ["micro_story", "bold_claim"]

    def test_invalid_values_replaced_with_known_keys(self):
        out = fw.enforce_variety("newsletter",
                                 [{"subject": "A", "format": "sonnet", "hook_style": "smoke signal"}])
        assert out[0]["format"] in fw.NEWSLETTER_FORMATS
        assert out[0]["hook_style"] in fw.HOOK_STYLES
        assert out[0]["cta_style"] in fw.CTA_STYLES

    def test_attaches_structure_and_keeps_subject_angle(self):
        out = fw.enforce_variety("newsletter",
                                 [{"subject": "A", "angle": "the angle", "format": "framework",
                                   "hook_style": "direct_promise", "cta_style": "teaser_next"}])
        assert out[0]["subject"] == "A" and out[0]["angle"] == "the angle"
        assert out[0]["structure"] == fw.NEWSLETTER_FORMATS["framework"]["structure"]

    def test_no_repeat_within_batch_even_when_pool_pressured(self):
        # 8 formats, batch of 6 + 3 recent: still no within-batch repeats.
        raw = [{"subject": f"S{i}"} for i in range(6)]
        out = fw.enforce_variety("newsletter", raw,
                                 recent_formats=["deep_dive", "framework", "case_study"])
        formats = [o["format"] for o in out]
        assert len(set(formats)) == len(formats)

    def test_comment_variety_rotates_without_hooks(self):
        raw = [{"format": "expander"} for _ in range(4)]
        out = fw.enforce_variety("comment", raw)
        formats = [o["format"] for o in out]
        assert len(set(formats)) == len(formats)
        for item in out:
            assert item["format"] in fw.COMMENT_FORMATS
            assert "hook_style" not in item and "cta_style" not in item
            assert item["structure"] == fw.COMMENT_FORMATS[item["format"]]["structure"]

    def test_empty_and_garbage_input(self):
        assert fw.enforce_variety("newsletter", []) == []
        assert fw.enforce_variety("newsletter", None) == []
        assert fw.enforce_variety("newsletter", ["not a dict", 42]) == []


class TestSelectBlueprint:
    def test_rotates_away_from_recent_shape(self):
        out = fw.select_blueprint("newsletter", subject="S",
                                  recent_formats=["deep_dive", "framework"],
                                  recent_hook_styles=["question", "surprising_stat"])
        assert out["format"] in fw.NEWSLETTER_FORMATS and out["format"] not in ("deep_dive", "framework")
        assert out["hook_style"] in fw.HOOK_STYLES
        assert out["hook_style"] not in ("question", "surprising_stat")
        assert out["cta_style"] in fw.CTA_STYLES
        assert out["structure"] == fw.NEWSLETTER_FORMATS[out["format"]]["structure"]
        assert out["subject"] == "S"

    def test_guidance_format_hint_wins(self):
        out = fw.select_blueprint("newsletter", subject="S", recent_formats=["listicle"],
                                  guidance="make this one a Case Study of a real client")
        assert out["format"] == "case_study"

    def test_no_history_still_valid(self):
        out = fw.select_blueprint("newsletter")
        assert out["format"] in fw.NEWSLETTER_FORMATS and out["hook_style"] in fw.HOOK_STYLES

    def test_post_selection_rotates_and_carries_post_menu(self):
        out = fw.select_blueprint("post", recent_formats=["personal_lesson", "contrarian_take"],
                                  recent_hook_styles=["question"])
        assert out["format"] in fw.POST_FORMATS
        assert out["format"] not in ("personal_lesson", "contrarian_take")
        assert out["hook_style"] in fw.HOOK_STYLES and out["hook_style"] != "question"
        assert out["cta_style"] in fw.POST_CTA_STYLES

    def test_comment_selection_has_format_only(self):
        out = fw.select_blueprint("comment", recent_formats=["expander", "storyteller"])
        assert out["format"] in fw.COMMENT_FORMATS
        assert out["format"] not in ("expander", "storyteller")
        assert out["hook_style"] is None and out["cta_style"] is None

    def test_comment_rotation_covers_menu_before_repeating(self):
        used = []
        rounds = len(fw.COMMENT_FORMATS)
        for _ in range(rounds):
            picked = fw.select_blueprint("comment", recent_formats=used)["format"]
            used.insert(0, picked)
        # A full pass through never repeats until the whole menu has been used.
        assert len(set(used)) == rounds


class TestDirectivesAndCompact:
    def test_newsletter_directive_includes_structure_hook_cta(self):
        blueprint = {"subject": "S", "format": "contrarian", "hook_style": "bold_claim",
                     "cta_style": "debate"}
        text = fw.blueprint_directive("newsletter", blueprint)
        assert "ASSIGNED BLUEPRINT" in text
        assert fw.NEWSLETTER_FORMATS["contrarian"]["label"] in text
        for i, section in enumerate(fw.NEWSLETTER_FORMATS["contrarian"]["structure"], 1):
            assert f"{i}. {section}" in text
        assert fw.HOOK_STYLES["bold_claim"]["guidance"] in text
        assert fw.CTA_STYLES["debate"]["guidance"] in text

    def test_post_directive_uses_post_menu(self):
        blueprint = {"format": "tactical_list", "hook_style": "surprising_stat",
                     "cta_style": "save_worthy"}
        text = fw.blueprint_directive("post", blueprint)
        assert "THIS POST'S ASSIGNED BLUEPRINT" in text
        assert fw.POST_FORMATS["tactical_list"]["label"] in text
        assert fw.HOOK_STYLES["surprising_stat"]["guidance"] in text
        assert fw.POST_CTA_STYLES["save_worthy"]["guidance"] in text

    def test_comment_directive_has_beats_but_no_hook_or_cta(self):
        text = fw.blueprint_directive("comment", {"format": "respectful_contrarian"})
        assert "THIS COMMENT'S ASSIGNED BLUEPRINT" in text
        assert fw.COMMENT_FORMATS["respectful_contrarian"]["label"] in text
        assert "OPENING HOOK STYLE" not in text and "CTA STYLE" not in text

    def test_blueprint_directive_empty_without_format(self):
        assert fw.blueprint_directive("newsletter", None) == ""
        assert fw.blueprint_directive("newsletter", {}) == ""
        assert fw.blueprint_directive("post", {"format": "not real"}) == ""

    def test_compact_drops_structure(self):
        blueprint = {"subject": "S", "angle": "a", "format": "listicle", "hook_style": "question",
                     "cta_style": "poll_prompt", "structure": ["x", "y"]}
        compact = fw.compact_blueprint(blueprint)
        assert compact == {"subject": "S", "angle": "a", "format": "listicle",
                           "hook_style": "question", "cta_style": "poll_prompt"}
        assert fw.compact_blueprint(None) is None

    @pytest.mark.parametrize("content_type", ["newsletter", "post", "comment"])
    def test_options_text_lists_all_keys(self, content_type):
        menu = fw.MENUS[content_type]
        text = fw.options_text(content_type)
        for key in list(menu["formats"]) + list(menu["hooks"]) + list(menu["ctas"]):
            assert key in text

    def test_post_writing_directive_carries_craft_rules(self):
        text = fw.post_writing_directive()
        assert "210 characters" in text          # hook before the '...more' fold
        assert "1300-2000" in text               # optimal short-form length
        assert "engagement-bait" in text
        assert "NEVER invent statistics" in text
        # The old one-size-fits-all viral template is gone for good.
        assert "10 relevant hashtags" not in text
        assert "SUCKS" not in text


class TestPersonalProofSlot:
    """A2: every post/newsletter blueprint carries a mandatory first-person proof slot, and comments
    (grounded in the target post) are exempt."""

    def test_post_directive_carries_proof_slot(self):
        text = fw.blueprint_directive("post", {"format": "contrarian_take", "hook_style": "bold_claim"})
        assert fw.PERSONAL_PROOF_SLOT in text
        assert "MANDATORY PERSONAL-PROOF SLOT" in text

    def test_newsletter_directive_carries_proof_slot(self):
        text = fw.blueprint_directive("newsletter", {"format": "case_study"})
        assert fw.PERSONAL_PROOF_SLOT in text

    def test_comment_directive_has_no_proof_slot(self):
        text = fw.blueprint_directive("comment", {"format": "expander"})
        assert "MANDATORY PERSONAL-PROOF SLOT" not in text

    def test_proof_slot_is_not_self_promo(self):
        # The slot asks for lived expertise, never a plug — it must stay inside the guardrail.
        assert "not self-promotion" in fw.PERSONAL_PROOF_SLOT
        assert "first person" in fw.PERSONAL_PROOF_SLOT.lower()


class TestFirstPersonProofDetector:
    """The deterministic gate: a draft with a concrete first-person lived detail passes; a generic,
    could-be-anyone draft (no such detail) is rejected."""

    _WITH_PROOF = ("Three years ago I shipped a feature that tanked our activation rate by 40%.\n\n"
                   "I learned that shipping fast without a rollback plan is how you lose a quarter.")

    _NAMED_MONTH_PROOF = ("Last March I sat across from a skeptical CFO who cut my budget in half.\n\n"
                          "We rebuilt the roadmap around what actually moved revenue.")

    _GENERIC = ("Authenticity is what wins on LinkedIn.\n\n"
                "Great leaders build trust by being consistent, showing up, and adding value to "
                "their audience every single day. That is how influence compounds over time.")

    _NUMBER_BUT_NO_FIRST_PERSON = ("Studies show that 70% of B2B buyers research on LinkedIn.\n\n"
                                   "The platform rewards consistency and genuine engagement.")

    _FIRST_PERSON_BUT_ABSTRACT = ("I believe authenticity is everything.\n\n"
                                  "We should all strive to add value and build real relationships.")

    def test_draft_with_first_person_number_passes(self):
        assert fw.has_first_person_proof(self._WITH_PROOF)
        sents = fw.first_person_proof_sentences(self._WITH_PROOF)
        assert any("40%" in s for s in sents)

    def test_named_month_lived_detail_passes(self):
        assert fw.has_first_person_proof(self._NAMED_MONTH_PROOF)

    def test_generic_draft_is_rejected(self):
        assert not fw.has_first_person_proof(self._GENERIC)

    def test_number_without_first_person_is_rejected(self):
        # A stat elsewhere in the post is not the AUTHOR's own proof.
        assert not fw.has_first_person_proof(self._NUMBER_BUT_NO_FIRST_PERSON)

    def test_first_person_without_specificity_is_rejected(self):
        assert not fw.has_first_person_proof(self._FIRST_PERSON_BUT_ABSTRACT)

    def test_empty_or_none_is_rejected(self):
        assert not fw.has_first_person_proof("")
        assert not fw.has_first_person_proof(None)
        assert fw.first_person_proof_sentences(None) == []

    def test_detector_is_deterministic(self):
        assert fw.has_first_person_proof(self._WITH_PROOF) == fw.has_first_person_proof(self._WITH_PROOF)


class TestPerformanceWeights:
    """Issue #389 / B4 — outcome aggregates become selection weights that down-weight
    under-performing shapes while keeping rotation and exploration intact."""

    def _agg(self, samples, reactions=0, comments=0, reposts=0,
             impressions=0, impression_samples=0):
        return {"samples": samples, "reactions": reactions, "comments": comments,
                "reposts": reposts, "impressions": impressions,
                "impression_samples": impression_samples}

    def test_below_min_samples_returns_empty(self):
        # Too little data → no steering (empty ⇒ pure rotation, behavior unchanged).
        perf = {"question": self._agg(2, reactions=1)}
        assert fw.performance_weights(perf, fw.HOOK_STYLES.keys(), min_samples=5) == {}

    def test_none_perf_returns_empty(self):
        assert fw.performance_weights(None, fw.HOOK_STYLES.keys()) == {}
        assert fw.performance_weights({}, fw.HOOK_STYLES.keys()) == {}

    def test_under_performer_down_weighted_others_neutral(self):
        perf = {
            "question": self._agg(4, reactions=2),          # weak: metric 0.5/post
            "bold_claim": self._agg(4, reactions=40),       # strong: metric 10/post
        }
        w = fw.performance_weights(perf, fw.HOOK_STYLES.keys(), min_samples=5, floor=0.25)
        assert w["question"] < w["bold_claim"]
        assert w["bold_claim"] == 1.0                        # at/above mean stays full
        assert w["question"] >= 0.25                         # never below the exploration floor
        # Unsampled shapes stay neutral so exploration keeps sampling them.
        assert w["micro_story"] == 1.0

    def test_floor_is_respected(self):
        perf = {
            "question": self._agg(5, reactions=0),           # metric 0 → would be 0 without floor
            "bold_claim": self._agg(5, reactions=100),
        }
        w = fw.performance_weights(perf, fw.HOOK_STYLES.keys(), min_samples=5, floor=0.3)
        assert w["question"] == 0.3

    def test_rate_mode_when_all_impressions_present(self):
        # Same raw engagement, very different impressions → the low-CTR shape is down-weighted.
        perf = {
            "question": self._agg(5, reactions=50, impressions=10000, impression_samples=5),
            "bold_claim": self._agg(5, reactions=50, impressions=500, impression_samples=5),
        }
        w = fw.performance_weights(perf, fw.HOOK_STYLES.keys(), min_samples=5)
        assert w["question"] < w["bold_claim"]

    def test_partial_impressions_falls_back_to_per_post(self):
        # Mixed coverage must NOT use rate mode (would mix incomparable scales).
        perf = {
            "question": self._agg(5, reactions=10, impressions=1000, impression_samples=3),
            "bold_claim": self._agg(5, reactions=20),
        }
        w = fw.performance_weights(perf, fw.HOOK_STYLES.keys(), min_samples=5)
        assert w["question"] < w["bold_claim"] == 1.0

    def test_one_shape_fully_covered_does_not_rate_scale_when_another_lacks_it(self):
        # Regression for the scale-mixing bug: 'question' has full per-shape impression coverage
        # but 'bold_claim' has none, so rate_mode must be OFF and BOTH scored avg-per-post. If
        # 'question' were rate-scored (0.01/post) while 'bold_claim' stayed per-post (10/post) the
        # scales would be incomparable and 'question' would be spuriously crushed to the floor.
        perf = {
            "question": self._agg(5, reactions=50, impressions=5000, impression_samples=5),
            "bold_claim": self._agg(5, reactions=50, impressions=0, impression_samples=0),
        }
        w = fw.performance_weights(perf, fw.HOOK_STYLES.keys(), min_samples=5, floor=0.25)
        # Equal avg-per-post (10 each) ⇒ equal weights, both at the mean (1.0), none floored.
        assert w["question"] == w["bold_claim"] == 1.0


class TestPerformanceAwareSelection:
    """The end-to-end acceptance: a seeded under-performing hook is selected less often once
    enough data exists, and selection is unchanged below the min-sample threshold."""

    def _count_hook(self, hook, performance, n=600, min_samples=5):
        import os
        import random
        random.seed(1337)  # deterministic RNG so the statistical assertions never flake
        os.environ["PERF_MIN_SAMPLES"] = str(min_samples)
        try:
            hits = 0
            for _ in range(n):
                bp = fw.select_blueprint("post", performance=performance)
                if bp["hook_style"] == hook:
                    hits += 1
            return hits
        finally:
            os.environ.pop("PERF_MIN_SAMPLES", None)

    def _perf(self, weak_hook, strong_hook, samples):
        agg = lambda s, r: {"samples": s, "reactions": r, "comments": 0, "reposts": 0,
                            "impressions": 0, "impression_samples": 0}
        return {"format": {}, "hook": {weak_hook: agg(samples, 1), strong_hook: agg(samples, 200)}}

    def test_underperformer_selected_less_after_enough_data(self):
        weak, strong = "question", "bold_claim"
        perf = self._perf(weak, strong, samples=10)          # well above min_samples
        weak_hits = self._count_hook(weak, perf)
        strong_hits = self._count_hook(strong, perf)
        assert weak_hits < strong_hits

    def test_behavior_unchanged_below_min_sample_threshold(self):
        weak, strong = "question", "bold_claim"
        # Only 2 samples each (4 total) < min_samples(5) → no steering, roughly equal.
        perf = self._perf(weak, strong, samples=2)
        weak_hits = self._count_hook(weak, perf)
        strong_hits = self._count_hook(strong, perf)
        # Without steering both are equally likely among the same tie group.
        assert abs(weak_hits - strong_hits) < weak_hits * 0.4
