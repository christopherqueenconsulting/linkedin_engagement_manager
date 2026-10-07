"""Showcase round 7: decks claim what they show, and count what the reader counts.

slot_143 said "A 4-part conversation starter" over a "1 / 6" page counter; 128/133/143/144 headed
slides "The Stakes:", "The Challenge:", "Step 1: Identify…"; 144's cover promised "From Silence to
Success" over a deck with no success. docs/content-core.md, "Showcase round 7".
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import content_framework as cf

pytestmark = pytest.mark.unit

FACTS = ["Sent 51 cold emails to executives; 0 replies"]
CAPTION = "I sent 51 cold emails and got 0 replies. Here is what I learned."


def _deck(**over):
    deck = {"cover": {"title": "From Silence to Success: 3 Steps",
                      "content": "My cold email failure, broken down."},
            "contents": [{"title": "The Stakes: AI Reliability",
                          "content": "Two candidates, one verification step. The gap was clear."},
                         {"title": "Step 1: Identify Key Metrics",
                          "content": "Determine which metrics drive your business."},
                         {"title": "The Challenge: 63 recipients were execs",
                          "content": "We emailed 51 executives. 63 recipients never opened it."}],
            "call_to_action": {"title": "Follow for more", "content": "Share what you learned."}}
    deck.update(over)
    return deck


class TestSelfLength:
    @pytest.mark.parametrize("text,want", [
        ("A 4-part conversation starter.", "A conversation starter."),
        ("An 8-part guide to it.", "A guide to it."),
        ("Swipe this six-slide deck.", "Swipe this deck."),
        ("a 3-page explainer", "an explainer"),
    ])
    def test_a_deck_never_counts_its_own_length(self, text, want):
        out, dropped = cf.drop_self_length(text)
        assert out == want and dropped

    def test_nothing_to_drop(self):
        assert cf.drop_self_length("3 steps to a clean close") == ("3 steps to a clean close", [])
        assert cf.drop_self_length(None) == (None, [])

    def test_slot_143_cover_and_caption(self):
        deck = {"cover": {"title": "AI vs Human", "content": "A 4-part conversation starter."},
                "contents": [{"title": str(n), "content": "x"} for n in range(4)],
                "call_to_action": {"title": "CTA", "content": "y"}}
        out, caption, changes = cf.reconcile_deck_counts(deck, "My 4-part take on it.")
        assert out["cover"]["content"] == "A conversation starter."
        assert caption == "My take on it."
        assert changes == ["A 4-part", "4-part"]


class TestTemplateHeadings:
    @pytest.mark.parametrize("title", ["The Stakes: AI Reliability", "The Challenge: none",
                                       "The Setup: a pilot", "Step 1: Identify Key Metrics",
                                       "The Initial Challenge — scope", "Part 2. The fix"])
    def test_role_labels(self, title):
        assert cf.template_heading(title)

    @pytest.mark.parametrize("title", ["Candidate 1 never ran the core workflow",
                                       "Stakes rose when the vendor changed", ""])
    def test_claims_are_not_labels(self, title):
        assert cf.template_heading(title) == ""

    def test_heading_as_claim_keeps_an_asserting_rest(self):
        assert cf.heading_as_claim("The Stakes: one missed step costs a client", "x") == \
            "One missed step costs a client"

    def test_heading_as_claim_lifts_the_body_for_a_bare_topic(self):
        assert cf.heading_as_claim("The Stakes: AI Reliability",
                                   "Two candidates, one verification step. More.") == \
            "Two candidates, one verification step"

    def test_heading_as_claim_fallbacks(self):
        assert cf.heading_as_claim("A real claim here", "body") == "A real claim here"
        assert cf.heading_as_claim("The Stakes: Reliability", "Why? Because.") == "Reliability"
        assert cf.heading_as_claim(None, "x") == ""


class TestCoverPromise:
    def test_from_silence_to_success_without_a_success(self):
        assert cf.cover_promise_gap(_deck()) == "success"

    def test_a_slide_that_shows_it_delivers(self):
        deck = _deck(contents=[{"title": "The reply that came", "content": "A success at last."}])
        assert cf.cover_promise_gap(deck) == ""

    def test_no_cover_or_no_promise(self):
        assert cf.cover_promise_gap({"contents": []}) == ""
        assert cf.cover_promise_gap(_deck(cover={"title": "51 emails, 0 replies"})) == ""


class TestFigures:
    def test_a_slide_figure_nothing_backs(self):
        issues = cf.deck_figure_issues(_deck(), CAPTION, FACTS)
        assert issues == [{"slide": "The Challenge: 63 recipients were execs", "figures": ["63"]}]

    def test_a_count_promise_is_not_a_figure(self):
        assert cf.deck_figure_issues(_deck(contents=[]), CAPTION, FACTS) == []

    def test_a_source_named_on_a_slide_vouches(self):
        deck = _deck(contents=[{"title": "Gartner's survey",
                                "content": "According to Gartner, 63% of firms overspend."}])
        assert cf.deck_figure_issues(deck, CAPTION, []) == []


class TestReportAndDirective:
    def test_report(self):
        report = cf.deck_claims_report(_deck(), CAPTION, FACTS)
        assert not report["passes"] and report["problems"] == 5
        assert report["promise"] == "success" and len(report["headings"]) == 3

    def test_directive(self):
        directive = cf.deck_claims_directive(cf.deck_claims_report(_deck(), CAPTION, FACTS))
        assert "MADE CLAIMS IT DID NOT SUPPORT" in directive and "The Stakes:" in directive
        assert cf.deck_claims_directive({"reasons": []}) == ""
        assert cf.deck_claims_directive(None) == ""


class TestFinalize:
    def test_every_fault_is_fixed_in_code(self):
        out, changes = cf.finalize_deck_claims(_deck(), CAPTION, FACTS)
        assert cf.deck_claims_report(out, CAPTION, FACTS)["passes"]
        assert out["contents"][0] == {"title": "Two candidates, one verification step",
                                      "content": "The gap was clear."}
        assert out["contents"][2]["content"] == "We emailed 51 executives."
        assert out["cover"]["title"] == out["contents"][0]["title"]
        assert len(changes) == 5
        assert _deck()["contents"][0]["title"] == "The Stakes: AI Reliability"  # input untouched

    def test_a_clean_deck_and_a_non_dict(self):
        clean = {"cover": {"title": "51 emails, 0 replies", "content": ""},
                 "contents": [{"title": "I wrote to execs", "content": "I sent 51."}],
                 "call_to_action": {"title": "Save", "content": "x"}}
        assert cf.finalize_deck_claims(clean, CAPTION, FACTS) == (clean, [])
        assert cf.finalize_deck_claims(None) == (None, [])

    def test_a_slide_that_is_only_its_figure_keeps_its_text(self):
        deck = {"cover": {"title": "A", "content": ""},
                "contents": [{"title": "Reach", "content": "Reach fell 63%."}],
                "call_to_action": {"title": "Save", "content": "x"}}
        out, _ = cf.finalize_deck_claims(deck, CAPTION, FACTS)
        assert out["contents"][0]["content"] == "Reach fell 63%."


class TestRepairWiring:
    """`_repair_carousel_substance` spends ONE regeneration on substance AND claims."""

    def _run(self, first, retry):
        from cqc_lem.utilities.ai import ai_helper

        draft = MagicMock(return_value=(CAPTION, retry, True))
        with patch("cqc_lem.utilities.carousel_creator.missing_carousel_fields",
                   return_value=[]):
            out = ai_helper._repair_carousel_substance(
                draft, CAPTION, first, FACTS, MagicMock(), {"required": False, "passes": True},
                False, 1, claim_sources=FACTS)
        return out, draft

    def test_a_better_retry_replaces_the_deck(self):
        fixed, _ = cf.finalize_deck_claims(_deck(), CAPTION, FACTS)
        (text, deck), draft = self._run(_deck(), fixed)
        assert deck == fixed
        assert "MADE CLAIMS IT DID NOT SUPPORT" in draft.call_args.args[0]

    def test_a_retry_no_better_keeps_the_deck(self):
        (text, deck), _ = self._run(_deck(), _deck())
        assert deck == _deck()

    def test_a_clean_deck_costs_no_call(self):
        clean, _ = cf.finalize_deck_claims(_deck(), CAPTION, FACTS)
        with patch.object(cf, "deck_substance_report",
                          return_value={"checked": True, "passes": True, "thin_slides": [],
                                        "reasons": []}):
            (_text, deck), draft = self._run(clean, clean)
        draft.assert_not_called()
