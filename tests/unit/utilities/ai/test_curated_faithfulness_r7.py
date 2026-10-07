"""Showcase round 7: curated commentary rotates its structure and states only what the source does.

All four drafts ran "For a small-business owner…" + a 3-step checklist + "Which…?"; curated_3
described Copilot Home and Power Automate behaviour the source never did; curated_2 said "In
practice" about a model nobody here had run. docs/curated-sources.md, docs/content-core.md.
"""

from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import curated_commentary as ccm

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.ai.curated_commentary"

SOURCE = {"id": 9, "platform": "rss", "url": "https://example.com/a", "author": "Jane Doe",
          "publisher": "Example Co", "title": "Copilot agents for finance teams",
          "excerpt": "Microsoft announced Copilot agents that work across Office apps. Teams "
                     "using agents cut invoice handling time by 30% last year.",
          "licence": "editorial"}
BASE = ("Jane Doe reports that finance teams using Copilot agents cut invoice handling time by "
        "30%. The part worth copying is the boring one: the intake form nobody owned before, "
        "and the person who now owns it. Start with the one form your team retypes every morning "
        "and time it for a week before you buy anything at all.")


@pytest.fixture
def passthrough_lint():
    with patch("cqc_lem.utilities.ai.ai_helper.lint_repaired",
               side_effect=lambda text, *a, **k: text), \
            patch(f"{_M}._recent_curated_texts", return_value=[]):
        yield


class TestStructureRotation:
    def test_structures_are_read_off_text(self):
        assert ccm.curated_structure_of("1. a\n2. b\n3. c") == ccm.STRUCTURE_CHECKLIST
        assert ccm.curated_structure_of("The upside is speed.") == ccm.STRUCTURE_PROS_CONS
        assert ccm.curated_structure_of("What I'd test first is cost.") == ccm.STRUCTURE_TEST_FIRST
        assert ccm.curated_structure_of("Picture a bakery owner.") == ccm.STRUCTURE_EXAMPLE
        assert ccm.curated_structure_of("My takeaway: own it.") == ccm.STRUCTURE_TAKEAWAY
        assert ccm.curated_structure_of("Nothing here.") is None

    def test_least_recently_used(self):
        recent = ["Picture a bakery.", "My takeaway: x.", "The upside is speed."]
        assert ccm.select_curated_structure(recent, 0) == ccm.STRUCTURE_TEST_FIRST
        assert ccm.select_curated_structure([], 1) == ccm.CURATED_STRUCTURE_ORDER[1]

    def test_the_prompt_carries_the_shape_and_bans_the_template(self):
        system = ccm.commentary_messages(SOURCE, "link", "VOICE",
                                         structure=ccm.STRUCTURE_PROS_CONS)[0]["content"]
        assert "Weigh it" in system and "Never end on a 'Which…?' question" in system
        assert "You have NOT tested this yourself" in system
        assert ccm.structure_directive("nope") == ""

    def test_the_opener_and_the_which_close_are_cut(self):
        assert ccm.strip_template_opener("For a small-business owner, this matters.") == \
            "This matters."
        assert ccm.strip_template_opener("This matters.") == "This matters."
        assert ccm.strip_template_opener(None) is None
        text = "One.\n\nTwo.\n\nWhich workflow would you automate first?"
        assert ccm.strip_which_close(text) == "One.\n\nTwo."
        assert ccm.strip_which_close("One.\n\nWhich one?") == "One.\n\nWhich one?"

    def test_recent_curated_texts_are_the_credited_posts(self):
        with patch("cqc_lem.utilities.db.get_recent_post_texts",
                   return_value=["Plain post.", "Take.\n\nSource: Jane Doe, Example Co.", None]):
            assert ccm._recent_curated_texts(1) == ["Take.\n\nSource: Jane Doe, Example Co."]
        with patch("cqc_lem.utilities.db.get_recent_post_texts", side_effect=RuntimeError("db")):
            assert ccm._recent_curated_texts(1) == []


class TestSourceFacts:
    def test_names_the_source_never_states(self):
        text = ("Microsoft announced Copilot agents. Copilot Home can trigger a Power Automate "
                "script. It runs on CPU.")
        assert ccm.unsourced_names(text, ccm.source_anchors(SOURCE)) == [
            "Copilot Home", "Power Automate", "CPU"]

    def test_possessives_and_sentence_openers_are_not_names(self):
        assert ccm.unsourced_names("Microsoft's agents help. Start small.",
                                   ccm.source_anchors(SOURCE)) == []

    def test_drop_sentences_naming(self):
        assert ccm.drop_sentences_naming("Keep this. Power Automate runs it.\nAnd this.",
                                         ["Power Automate"]) == "Keep this.\nAnd this."

    def test_first_hand_claims_are_reattributed(self):
        text = "In practice, the model fits under a gigabyte. I tested it on my invoices. Good."
        assert ccm.first_hand_claims(text) == ["In practice", "I tested it"]
        assert ccm.attribute_first_hand(text, "Jane Doe") == \
            "According to Jane Doe, the model fits under a gigabyte. Good."

    def test_a_story_bank_fact_supports_first_hand(self):
        assert ccm.first_hand_supported(SOURCE, ["I piloted Copilot agents with a client"])
        assert not ccm.first_hand_supported(SOURCE, ["I ran a bakery"])
        assert not ccm.first_hand_supported(SOURCE, None)


class TestGenerate:
    def test_a_named_feature_gets_one_rewrite(self, passthrough_lint):
        invented = BASE + " Copilot Home can trigger a Power Automate script for you."
        with patch(f"{_M}._complete", side_effect=[invented, BASE]) as complete:
            out = ccm.generate_curated_commentary(1, SOURCE, "link", profile_synthesis="voice")
        assert complete.call_count == 2
        assert "NAMED PRODUCTS OR FEATURES" in complete.call_args.args[0][1]["content"]
        assert "Power Automate" not in out

    def test_a_rewrite_that_still_names_it_loses_the_sentence(self, passthrough_lint):
        invented = BASE + " Copilot Home can trigger a Power Automate script for you."
        with patch(f"{_M}._complete", side_effect=[invented, invented]):
            out = ccm.generate_curated_commentary(1, SOURCE, "link", profile_synthesis="voice")
        assert "Power Automate" not in out and out.startswith("Jane Doe reports")

    def test_in_practice_is_reattributed_without_a_fact(self, passthrough_lint):
        drafted = "In practice, the agents run inside Office apps. " + BASE
        with patch(f"{_M}._complete", return_value=drafted), \
                patch("cqc_lem.utilities.post_image.story_facts_for", return_value=[]):
            out = ccm.generate_curated_commentary(1, SOURCE, "link", profile_synthesis="voice")
        assert "In practice" not in out
        assert out.startswith("According to Jane Doe, the agents run inside Office apps.")

    def test_in_practice_stands_when_a_story_fact_backs_it(self, passthrough_lint):
        drafted = "In practice, the agents run inside Office apps. " + BASE
        with patch(f"{_M}._complete", return_value=drafted), \
                patch("cqc_lem.utilities.post_image.story_facts_for",
                      return_value=["I rolled out Copilot agents for a client's finance team"]):
            out = ccm.generate_curated_commentary(1, SOURCE, "link", profile_synthesis="voice")
        assert out.startswith("In practice")

    def test_a_template_draft_is_trimmed(self, passthrough_lint):
        drafted = ("For a small-business owner, " + BASE[0].lower() + BASE[1:]
                   + "\n\nThat is the whole move.\n\nWhich form would you start with?")
        with patch(f"{_M}._complete", return_value=drafted):
            out = ccm.generate_curated_commentary(1, SOURCE, "link", profile_synthesis="voice")
        assert out.startswith("Jane Doe reports") and "Which form" not in out
