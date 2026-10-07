"""Showcase round 6 in the shared content core.

Multipliers, every deck count phrasing, template slides, stock openers, and the profile
industry kept as context.
"""

import pytest

from cqc_lem.utilities.ai import content_framework as cf, slop_lint as sl, story_bank as sb

pytestmark = pytest.mark.unit


class TestMultipliersAreClaims:
    def test_a_spelled_out_multiplier_is_graded(self):
        report = cf.fact_grounding_report("Inference fees have fallen roughly thirteen‑fold.",
                                          ["nothing numeric"])
        assert [c["value"] for c in report["unverified"]] == ["13"]

    def test_a_sourced_multiplier_passes_in_either_spelling(self):
        anchors = ["Stanford AI Index 2025: inference cost fell thirteen-fold."]
        assert cf.fact_grounding_report("Costs fell 13× in two years.", anchors)["passes"]
        assert cf.fact_grounding_report("Costs fell 13-fold.", anchors)["passes"]

    def test_symbol_and_suffix_forms_normalise_to_the_number(self):
        values = [c["value"] for c in cf.numeric_claims("It ran 13× faster, 4-fold cheaper, 79 %.")]
        assert values == ["13", "4", "79"]


class TestDeckCountPhrasings:
    @pytest.mark.parametrize("text,count", [
        ("A 6-part snapshot of the launch", 6),
        ("A five-step fix", 5),
        ("My 7-point checklist", 7),
        ("3 insights from the week", 3),
        ("four tips I wish I had", 4),
    ])
    def test_every_phrasing_is_a_count(self, text, count):
        assert [c["count"] for c in cf.deck_count_claims(text)] == [count]

    def test_claims_come_back_in_text_order(self):
        claims = cf.deck_count_claims("The 6-part series covers 3 steps.")
        assert [c["phrase"] for c in claims] == ["6-part", "3 steps"]

    def test_the_cover_count_is_rewritten_in_its_own_form(self):
        deck = {"cover": {"title": "A 6-part snapshot", "content": "A Five-step read"},
                "contents": [{"title": "a", "content": "1"}, {"title": "b", "content": "2"},
                             {"title": "c", "content": "3"}, {"title": "d", "content": "4"}],
                "call_to_action": {"title": "Save", "content": "Save it."}}
        out, caption, changes = cf.reconcile_deck_counts(deck, "My 6-part teardown.")
        assert out["cover"] == {"title": "A 4-part snapshot", "content": "A Four-step read"}
        assert caption == "My 4-part teardown."
        assert "6-part" in changes

    def test_a_count_that_matches_is_left_alone(self):
        deck = {"cover": {"title": "A 2-part note", "content": ""},
                "contents": [{"title": "a", "content": "1"}, {"title": "b", "content": "2"}],
                "call_to_action": {"title": "Save", "content": "x"}}
        _, caption, changes = cf.reconcile_deck_counts(deck, "My 2-part note.")
        assert changes == [] and caption == "My 2-part note."


def _deck(*bodies):
    return {"cover": {"title": "What I learned", "content": "A case snapshot"},
            "contents": [{"title": t, "content": c} for t, c in bodies],
            "call_to_action": {"title": "Follow", "content": "More soon."}}


class TestDeckSubstance:
    POST = ("Two take-home projects. One candidate kept business logic on the server, wrote a "
            "key-rotation runbook and found a real bug by running the code.")

    def test_template_slides_are_named(self):
        deck = _deck(("The Initial Challenge", "Both candidates were proficient but differed."),
                     ("Decisive Moves Made", "The stronger approach made a difference."),
                     ("Step 1: Identify the key areas", "Focus on what matters to your team."))
        report = cf.deck_substance_report(deck, self.POST)
        assert report["checked"] and not report["passes"]
        assert report["thin_slides"] == ["The Initial Challenge", "Decisive Moves Made",
                                         "Step 1: Identify the key areas"]
        directive = cf.deck_substance_directive(report)
        assert "The Initial Challenge" in directive and "Invent nothing" in directive

    def test_concrete_slides_pass(self):
        deck = _deck(("The runbook", "A key-rotation runbook shipped with the server logic."),
                     ("The bug", "Running the code surfaced a real bug before review."),
                     ("Pin the tag", "Set IMAGE_TAG to the release tag, never latest."),
                     ("Ask Claude", "Hand the assessment to Claude and read the output."),
                     ("The cost", "It cost 3 hours."),
                     ("The rule", "If the review takes longer than a day, roll back and split the change."))
        report = cf.deck_substance_report(deck, self.POST)
        assert report["passes"], report["reasons"]

    def test_story_facts_count_as_substance(self):
        deck = _deck(("Where it broke", "The phishing email spoofed our support address."),)
        assert not cf.deck_substance_report(deck, "A short caption.")["passes"]
        assert cf.deck_substance_report(
            deck, "A short caption.",
            ["A phishing email arrived from our own support address."])["passes"]

    def test_nothing_to_grade_fails_open(self):
        assert cf.deck_substance_report({}, self.POST)["checked"] is False
        assert cf.deck_substance_report(_deck(("a", "b")), "", [])["checked"] is False
        assert cf.deck_substance_directive({"reasons": []}) == ""


class TestStockOpeners:
    @pytest.mark.parametrize("text", [
        "Some decisions should never go to AI.\n\nIn today's AI-driven e-commerce landscape, "
        "the allure of automation is strong.",
        "I sent 51 emails.\n\nIn a world driven by data, even pros misfire.",
        "Could it come from your domain?\n\nIn the fast-paced world of e-commerce, owners juggle.",
        "In this day and age, nobody waits.",
        "As we navigate the AI shift, costs climb.",
    ])
    def test_a_stock_opener_is_hard_on_a_post(self, text):
        report = sl.lint_report(text, "post")
        assert not report["passes"]
        assert sl.CHECK_STOCK_OPENER in [v["check"] for v in report["hard"]]

    @pytest.mark.parametrize("text", [
        "We priced it for today's market and it held.",
        "In 2024 I shipped the first version.",
        "In my first month the bill doubled.",
    ])
    def test_ordinary_sentences_pass(self, text):
        assert sl.stock_opener_hits(text) == []

    def test_other_surfaces_are_not_graded(self):
        text = "In today's digital landscape, we ship."
        assert sl.check_severity(sl.CHECK_STOCK_OPENER, "comment") == sl.SEVERITY_OFF
        assert sl.CHECK_STOCK_OPENER not in [v["check"] for v in
                                             sl.lint_report(text, "comment")["violations"]]


class TestIndustryIsContext:
    def test_the_directives_carry_the_rule(self):
        entry = {"kind": "anecdote", "title": "t", "body": "A phishing email came in."}
        assert sb.INDUSTRY_CONTEXT_RULE in sb.story_directive(entry)
        assert sb.INDUSTRY_CONTEXT_RULE in sb.no_story_directive()

    def test_industry_terms_in(self):
        assert sb.industry_terms_in('{"industry": "E-commerce", "about": "Retail and SaaS"}') == [
            "e-commerce", "retail", "saas"]
        assert sb.industry_terms_in(None) == []

    @pytest.mark.parametrize("draft", [
        "In AI-driven e-commerce, understanding your target is key.",
        "In today's AI-driven e-commerce landscape, the allure is strong.",
        "Owners in the fast-paced world of e-commerce juggle a lot.",
        "The e-commerce landscape rewards speed.",
    ])
    def test_a_profile_industry_framing_the_post_is_flagged(self, draft):
        bank = ["I sent 51 cold emails and got zero replies."]
        assert sb.unsourced_industry_claims(draft, bank, ["e-commerce"]) == ["e-commerce"]

    def test_the_story_naming_it_clears_it(self):
        bank = ["My e-commerce client lost 40 orders to a checkout bug."]
        draft = "In AI-driven e-commerce, a checkout bug is expensive."
        assert sb.unsourced_industry_claims(draft, bank, ["e-commerce"]) == []

    def test_a_passing_mention_is_not_a_frame(self):
        draft = "Shoppers abandon carts when e-commerce checkouts stall."
        assert sb.unsourced_industry_claims(draft, ["bank"], ["e-commerce"]) == []
