"""Unit tests for the deterministic reference-value gate on save-targeted decks (issue #728).

The live 'The 160-Release Build Receipt' document post shipped five slides of restated claims while
its caption promised 'the exact stack'. These tests pin both halves of the fix: a slide has to carry
something a reader could act from, and a caption cannot promise something the slides never deliver.
"""

import pytest

from cqc_lem.utilities.ai import content_framework as fw

pytestmark = pytest.mark.unit


# The deck that shipped: claims and narrative, nothing reusable.
_NARRATIVE_DECK = {
    "cover": {"title": "The 160-Release Build Receipt",
              "content": "What it actually took to ship 160 releases."},
    "insights": [
        {"title": "Release count is not the goal",
         "content": "Shipping often is a side effect of small reversible changes. The count is the "
                    "symptom, not the target."},
        {"title": "You do not need Kubernetes",
         "content": "A single box carried the whole thing. Complexity was the enemy here."},
        {"title": "Automation compounds",
         "content": "Every manual step removed paid for itself many times over as the work grew."},
    ],
    "call_to_action": {"title": "Save this", "content": "Save this for your next release plan."},
}

# The same deck rebuilt so each slide carries a reusable artifact.
_REFERENCE_DECK = {
    "cover": {"title": "The 5 checks I run before every release",
              "content": "The exact stack and the checklist."},
    "insights": [
        {"title": "1. Pin the image tag",
         "content": "Set IMAGE_TAG to the release tag, never latest. A floating tag makes rollback "
                    "guesswork."},
        {"title": "2. Migrate first",
         "content": "Run `flyway migrate` before the app flips. If a migration fails, roll back "
                    "before anything else."},
        {"title": "3. Health check under 2 seconds",
         "content": "The health probe must answer in under 2 seconds or the deploy aborts."},
        {"title": "What changed",
         "content": "Deploys went from 14 minutes of downtime to 0."},
    ],
    "call_to_action": {"title": "Save this", "content": "Save this for your next deploy."},
}

_STACK_CAPTION = "Here is the exact stack behind it, end to end."


class TestSlideArtifacts:
    @pytest.mark.parametrize("text,kind", [
        ("Set IMAGE_TAG to the release tag.", fw.ARTIFACT_CONFIG),
        ("Run `flyway migrate` before the flip.", fw.ARTIFACT_COMMAND),
        ("Pass --no-deps so the sidecars stay up.", fw.ARTIFACT_COMMAND),
        ("Step 2: warm the cache.", fw.ARTIFACT_STEP),
        ("Keep the probe under 2 seconds.", fw.ARTIFACT_THRESHOLD),
        ("If the migration fails, roll back before anything else.", fw.ARTIFACT_DECISION_RULE),
        ("Never hardcode the key in the call site.", fw.ARTIFACT_DECISION_RULE),
        ("Downtime went from 14 minutes to 0.", fw.ARTIFACT_COMPARISON),
        ("Blue/green vs a rolling restart.", fw.ARTIFACT_COMPARISON),
        ("- Pin the tag\n- Migrate first", fw.ARTIFACT_CHECKLIST),
        ("It cut the release cycle to 9 minutes.", fw.ARTIFACT_METRIC),
    ])
    def test_each_reusable_shape_is_detected(self, text, kind):
        assert kind in fw.slide_artifacts(text)

    def test_narrative_claims_carry_nothing(self):
        assert fw.slide_artifacts("Release count is not the goal. The count is the symptom.") == []

    def test_empty_text_carries_nothing(self):
        assert fw.slide_artifacts("") == []
        assert fw.slide_artifacts(None) == []

    def test_a_tool_version_is_not_reference_value(self):
        # `numeric_claims` already discards product versions — "GPT-4o" is a name, not a metric.
        assert fw.ARTIFACT_METRIC not in fw.slide_artifacts("We used GPT-4o for extraction.")


class TestDeckSlides:
    def test_slides_come_back_in_schema_order_with_list_slides_expanded(self):
        keys = [(s["key"], s["index"]) for s in fw.deck_slides(_NARRATIVE_DECK)]
        assert keys == [("cover", None), ("insights", 0), ("insights", 1), ("insights", 2),
                        ("call_to_action", None)]

    def test_cover_and_cta_are_never_graded(self):
        graded = {s["key"] for s in fw.deck_slides(_NARRATIVE_DECK) if s["graded"]}
        assert graded == {"insights"}

    def test_a_testimonial_quote_is_exempt_too(self):
        deck = {"cover": {"title": "A"}, "testimonial": {"title": "They loved it"},
                "results": {"title": "Cut costs 40%"}}
        graded = {s["key"] for s in fw.deck_slides(deck) if s["graded"]}
        assert graded == {"results"}

    def test_a_non_dict_deck_has_no_slides(self):
        assert fw.deck_slides(None) == []
        assert fw.deck_slides("not a deck") == []


class TestDeckPromises:
    @pytest.mark.parametrize("caption,promise", [
        ("The checklist I run before every launch.", "a checklist"),
        ("The 5 checks that catch every bad deploy.", "a checklist"),
        ("Here is the exact stack.", "the exact stack, commands or config"),
        ("My step-by-step process for this.", "a step-by-step process"),
        ("The numbers behind it.", "real numbers"),
        ("Blue/green versus rolling restarts.", "a before/after comparison"),
        ("The rules I use to decide.", "a decision rule"),
    ])
    def test_promise_keywords_are_picked_up(self, caption, promise):
        assert promise in [p["promise"] for p in fw.deck_promises(caption)]

    def test_a_caption_that_promises_nothing_reports_nothing(self):
        assert fw.deck_promises("Some thoughts on where the industry is heading.") == []
        assert fw.deck_promises(None) == []


class TestDeckReferenceReport:
    def test_a_reference_deck_passes(self):
        report = fw.deck_reference_report(_REFERENCE_DECK, _STACK_CAPTION, save_targeted=True)
        assert report["checked"] is True
        assert report["required"] is True
        assert report["passes"] is True
        assert report["empty_slides"] == []
        assert report["reasons"] == []

    def test_a_narrative_only_deck_fails_and_names_every_empty_slide(self):
        report = fw.deck_reference_report(_NARRATIVE_DECK, _STACK_CAPTION, save_targeted=True)
        assert report["passes"] is False
        assert report["empty_slides"] == ["Release count is not the goal",
                                          "You do not need Kubernetes",
                                          "Automation compounds"]
        assert all("carries nothing reusable" in r for r in report["reasons"][:3])

    def test_a_promise_the_slides_never_deliver_fails_the_deck(self):
        # Every body slide carries a number, so the per-slide rule is satisfied — only the caption's
        # "exact stack" promise is unfulfilled, and that alone must reject the deck.
        deck = {
            "cover": {"title": "The build", "content": "How it went."},
            "insights": [
                {"title": "It took 12 weeks", "content": "Twelve weeks from first commit."},
                {"title": "It cost 400 dollars", "content": "That covered every model call."},
            ],
            "call_to_action": {"title": "Save this", "content": "Save it for later."},
        }
        report = fw.deck_reference_report(deck, "Here is the exact stack.", save_targeted=True)
        assert report["empty_slides"] == []
        assert report["passes"] is False
        assert report["unfulfilled"] == ["the exact stack, commands or config"]
        assert any("promises the exact stack" in r for r in report["reasons"])

    def test_a_promise_fulfilled_anywhere_in_the_deck_counts(self):
        report = fw.deck_reference_report(_REFERENCE_DECK, "Here is the exact stack.",
                                          save_targeted=False)
        assert report["required"] is True  # the caption made the promise
        assert report["unfulfilled"] == []
        assert report["passes"] is True

    def test_a_deck_nobody_claimed_was_a_reference_is_reported_but_never_failed(self):
        report = fw.deck_reference_report(_NARRATIVE_DECK,
                                          "Some thoughts on where this is heading.",
                                          save_targeted=False)
        assert report["checked"] is True
        assert report["required"] is False
        assert report["passes"] is True
        # The gaps are still reported, so the caller can log what it shipped.
        assert report["empty_slides"]

    def test_a_save_targeted_archetype_is_graded_with_no_caption_at_all(self):
        report = fw.deck_reference_report(_NARRATIVE_DECK, None, save_targeted=True)
        assert report["required"] is True
        assert report["passes"] is False

    def test_an_empty_deck_fails_open(self):
        report = fw.deck_reference_report({}, _STACK_CAPTION, save_targeted=True)
        assert report["checked"] is False
        assert report["passes"] is True

    def test_a_deck_of_only_cover_and_cta_fails_open(self):
        deck = {"cover": {"title": "A"}, "call_to_action": {"title": "Save this"}}
        assert fw.deck_reference_report(deck, _STACK_CAPTION, save_targeted=True)["checked"] is False

    def test_the_gate_can_be_stood_down(self, monkeypatch):
        monkeypatch.setenv("DECK_REFERENCE_ENABLED", "false")
        report = fw.deck_reference_report(_NARRATIVE_DECK, _STACK_CAPTION, save_targeted=True)
        assert report["checked"] is False
        assert report["passes"] is True


class TestDeckRetryDirective:
    def test_it_names_the_failing_slides_and_the_broken_promise(self):
        report = fw.deck_reference_report(_NARRATIVE_DECK, _STACK_CAPTION, save_targeted=True)
        directive = fw.deck_retry_directive(report)
        assert "Release count is not the goal" in directive
        assert "promises the exact stack" in directive
        # And it hands back the shapes that ARE inherently save-worthy.
        assert "Numbered step" in directive and "Command / config block" in directive

    def test_it_forbids_inventing_material_to_fill_a_slide(self):
        directive = fw.deck_retry_directive(
            fw.deck_reference_report(_NARRATIVE_DECK, _STACK_CAPTION, save_targeted=True))
        assert "invent NOTHING" in directive

    def test_a_passing_report_produces_no_directive(self):
        assert fw.deck_retry_directive(
            fw.deck_reference_report(_REFERENCE_DECK, _STACK_CAPTION, save_targeted=True)) == ""
        assert fw.deck_retry_directive(None) == ""


class TestReferenceSlideDirective:
    def test_a_save_targeted_carousel_archetype_carries_the_reference_rules(self):
        text = fw.carousel_blueprint_directive({"format": "build_receipt"})
        assert "EVERY BODY SLIDE MUST CARRY SOMETHING REUSABLE" in text
        assert "Checklist" in text

    def test_an_ordinary_archetype_does_not(self):
        text = fw.carousel_blueprint_directive({"format": "personal_lesson"})
        assert "EVERY BODY SLIDE MUST CARRY SOMETHING REUSABLE" not in text


class TestMaxAttempts:
    def test_the_default_is_one_retry(self, monkeypatch):
        monkeypatch.delenv("DECK_REFERENCE_MAX_ATTEMPTS", raising=False)
        assert fw.deck_reference_max_attempts() == 2

    @pytest.mark.parametrize("raw,expected", [("3", 3), ("0", 1), ("99", 4), ("junk", 2), ("", 2)])
    def test_the_override_is_clamped(self, monkeypatch, raw, expected):
        monkeypatch.setenv("DECK_REFERENCE_MAX_ATTEMPTS", raw)
        assert fw.deck_reference_max_attempts() == expected
