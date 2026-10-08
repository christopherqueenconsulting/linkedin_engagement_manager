"""Issue #2316, copy half: the round-10 escapes, each pinned on the post that shipped it.

docs/content-core.md (#2316): 128's 51 vs 63, 123/130's stitched stories, 141's "6%" and 130's
"30%", 143/144's shared September 15, the ONE figure gate on decks (143's "5,878", 144's "82
issues, 13 blocking" and "3 steps"), cover_18's dropped 53.7%, and the curated opener, close and
chart label.
"""
import dataclasses

import pytest

from cqc_lem.utilities.ai import (
    content_framework as cf,
    curated_commentary as cc,
    fact_consistency as fc,
    image_concept as ic,
    image_graphics as ig,
)

pytestmark = pytest.mark.unit

POST_123 = (
    "Last Tuesday I stared at a ticking clock—55 minutes left to finish a compliance assessment.\n\n"
    "Retail Dive’s October 8 update says Meta Muse for Small Business now hooks Shopify and "
    "QuickBooks, letting you require human approval before any purchase is made.\n\n"
    "During the assessment I found three bugs and asked Claude to draft a remediation plan.")
POST_128 = (
    "When I sent 51 cold emails and got zero replies, I learned a hard lesson.\n\n"
    "Every one of the 63 recipients was a founder or CEO, not the person actually hiring.")
POST_130 = (
    "How to stop AI hallucinations and security leaks from destroying your customer trust.\n\n"
    "In April 2026, a phishing email slipped through my own business support address.\n\n"
    "I reported it right away.\n\n"
    "For small businesses, trust is everything. I recently worked with a client whose AI chatbot "
    "started giving out wrong refund info.\n\n"
    "Error rates dropped 30% within three months.")
POST_141 = (
    "6% of owners skip this simple step...\n\n"
    "I learned it the hard way on February 14, 2026. I offered to rebuild a website for free, "
    "promising a January launch.\n\n"
    "By February the owner had to text, leave a voicemail, then email just to get an update.")
POST_143 = (
    "Can we build AI solutions entirely from free public data?\n\n"
    "On September 15, 2026, I dove into 134,858 Form 990-PF tax returns. From these, I identified "
    "22,878 distinct preparer firms, with 5,123 having three or more foundation clients.")
POST_144 = (
    "Speed can be a double-edged sword.\n\n"
    "On September 15, 2026, I launched a niche product with payment links going live that "
    "afternoon. By evening a review showed it offered features that weren't there.\n\n"
    "I had to deactivate all three payment links that night.")


# --- 128: "every one of the 63 recipients" is a total ---------------------------------------------

class TestCountTotals:
    def test_every_one_of_counts_the_whole_set(self):
        issues = fc.count_conflicts(POST_128)
        assert issues and "Every one of the 63 recipients" in issues[0]

    def test_one_sentence_relating_both_is_fine(self):
        assert fc.count_conflicts("I sent 51 emails to each of the 63 contacts.") == []


# --- 141 / 130: a fact vouches for a figure only in its own kind ----------------------------------

class TestKindedProvenance:
    @pytest.mark.parametrize("raw,kind", [("6%", "%"), ("12 percent", "%"), ("$30K", "$"),
                                          ("3x", "x"), ("51", "")])
    def test_figure_kind(self, raw, kind):
        assert fc.figure_kind(raw) == kind

    def test_a_count_fact_never_vouches_for_a_percentage(self):
        assert not fc.fact_vouches("6", ["Shipped 6 tools in a week"], "6%")
        assert fc.fact_vouches("6", ["6% of owners skip it"], "6%")
        # A bare figure has no kind to check, so it matches by digits as before.
        assert fc.fact_vouches("6", ["Shipped 6 tools"], "6")
        assert fc.fact_vouches("6", ["Shipped 6 tools"])

    def test_141s_hook_is_caught_despite_a_fact_holding_a_6(self):
        issues = fc.hook_provenance_issues(POST_141, ["Shipped 6 tools in a week"])
        assert issues and '"6%"' in issues[0]

    def test_130s_body_statistic_laundered_by_digits_is_caught(self):
        issues = fc.statistic_provenance_issues(POST_130, ["Cleared a 30 day backlog"])
        assert issues and '"30%"' in issues[0]
        # No fact mentions 30 at all: the fact-grounding gate owns it, at its own severity.
        assert fc.statistic_provenance_issues(POST_130, ["Cleared a backlog"]) == []
        # The same kind vouches; a figure another finding already names is skipped.
        assert fc.statistic_provenance_issues(POST_130, ["Errors fell 30% in 3 months"]) == []
        assert fc.statistic_provenance_issues(POST_130, ["a 30 day backlog"], ["30%"]) == []

    def test_the_report_carries_both(self):
        report = fc.consistency_report(POST_130, hook_facts=["Cleared a 30 day backlog"])
        assert any('"30%"' in i for i in report["provenance"])


# --- 123 / 130: two stories in one post, with no anchor ------------------------------------------

class TestStitchedStories:
    def test_an_anecdote_and_a_dated_news_item(self):
        kinds = [e["kind"] for e in fc.story_events(POST_123)]
        assert kinds == [fc.EVENT_ANECDOTE, fc.EVENT_NEWS]
        issue = fc.stitched_story_issues(POST_123)
        assert issue and "Last Tuesday" in issue[0] and "October 8" in issue[0]

    def test_an_anecdote_and_a_new_client_case(self):
        issue = fc.stitched_story_issues(POST_130)
        assert issue and "In April 2026" in issue[0] and "client" in issue[0]

    @pytest.mark.parametrize("text", [POST_141, POST_143, POST_144, POST_128])
    def test_one_story_is_never_flagged(self, text):
        assert fc.stitched_story_issues(text) == []

    def test_two_anecdotes_days_apart_are_one_story(self):
        text = ("On September 15, 2026, I launched the product.\n\n"
                "On September 18, 2026, I pulled the payment links.")
        assert fc.stitched_story_issues(text) == []

    def test_two_anecdotes_months_apart_are_two(self):
        text = ("On March 3, 2026, I launched the product.\n\n"
                "On September 18, 2026, I rebuilt a client's site.")
        assert fc.stitched_story_issues(text)

    def test_a_relative_and_an_absolute_date_read_as_one_story(self):
        text = ("Last Tuesday I opened the logs.\n\nOn September 18, 2026, I fixed the bug.")
        assert fc.stitched_story_issues(text) == []

    def test_two_different_news_items(self):
        text = ("Gartner's May 2026 survey says buyers wait.\n\n"
                "Forrester's June 2026 report says they do not.")
        assert fc.stitched_story_issues(text)

    def test_the_report_carries_it_without_an_anchor(self):
        report = fc.consistency_report(POST_123)
        assert report["stories"] and not report["passes"]

    def test_the_anchor_rule_wins_when_both_fire(self):
        anchored = fc.consistency_report(POST_130, story_text="A Lincoln Nautilus purchase")
        assert len(anchored["stories"]) == 1


# --- 143 / 144: the same dated story in one batch ------------------------------------------------

class TestRepeatedStoryDate:
    def test_a_recent_post_on_the_same_day(self):
        issue = fc.repeated_story_date_issues(POST_144, [POST_143])
        assert issue and "September 15" in issue[0]

    def test_another_day_another_year_or_no_history(self):
        other_day = POST_143.replace("September 15", "September 25")
        other_year = POST_143.replace("2026", "2025")
        assert fc.repeated_story_date_issues(POST_144, [other_day]) == []
        assert fc.repeated_story_date_issues(POST_144, [other_year]) == []
        assert fc.repeated_story_date_issues(POST_144, None) == []
        assert fc.repeated_story_date_issues(POST_144, [POST_144, ""]) == []
        assert fc.repeated_story_date_issues(POST_128, [POST_143]) == []

    def test_the_report_reads_the_window(self):
        assert fc.consistency_report(POST_144, recent_texts=[POST_143])["stories"]
        assert fc.consistency_report(POST_144)["passes"]


# --- THE figure gate -----------------------------------------------------------------------------

class TestFigureGate:
    def test_a_slide_figure_the_body_never_states(self):
        assert fc.figures_absent_from_body("Found 5,878 target firms", POST_143) == ["5,878"]
        assert fc.figures_absent_from_body("22,878 preparer firms", POST_143) == []

    def test_number_words_in_the_body_state_a_figure(self):
        assert fc.figures_absent_from_body("3+ foundation clients", POST_143) == []
        assert fc.figures_absent_from_body("Found 82 issues, 13 blocking", POST_144) == \
            ["82", "13"]

    def _deck_144(self, steps=False):
        titles = (["Step 1: Launch", "Step 2: Review", "Step 3: Pull links"] if steps else
                  ["Launched in a day", "Found 82 issues, 13 blocking", "Pulled the links"])
        return {"cover": {"title": "How I Caught My Own Error",
                          "content": "The quick launch that almost cost me - and the 3 steps "
                                     "that saved it."},
                "contents": [{"title": titles[0], "content": "Here's what was missing."},
                             {"title": titles[1], "content": "The reality check I needed."},
                             {"title": titles[2], "content": "All three links, that night."}],
                "call_to_action": {"title": "Follow", "content": "For more launch lessons."}}

    def test_every_slide_is_read_cover_included(self):
        found = {f["slide"]: f["figures"] for f in cf.deck_absent_figures(self._deck_144(),
                                                                          POST_144)}
        assert found == {"How I Caught My Own Error": ["3"],
                         "Found 82 issues, 13 blocking": ["82", "13"]}

    def test_a_count_the_deck_proves_is_exempt(self):
        assert cf.deck_absent_figures(self._deck_144(steps=True), POST_144) == []

    def test_finalize_repairs_what_it_can_and_the_rest_holds(self):
        out, changes = cf.finalize_deck_claims(self._deck_144(), POST_144)
        assert out["contents"][1]["title"] == "The reality check I needed"
        assert any("82" in c for c in changes)
        # The cover's one sentence cannot lose "3 steps" without emptying it: it is kept, and
        # the post is HELD.
        issues = cf.deck_figure_gate_issues(out, POST_144)
        assert len(issues) == 1 and "How I Caught My Own Error" in issues[0]
        assert cf.deck_figure_gate_issues(self._deck_144(steps=True), POST_144) == []

    def test_the_regeneration_report_names_them(self):
        report = cf.deck_claims_report(self._deck_144(), POST_144, [])
        assert any("82" in r for r in report["reasons"]) and not report["passes"]


# --- cover_18: a sourced title figure is shown ---------------------------------------------------

_TITLE_18 = "53.7% of LinkedIn Posts Miss Their Mark - Why"
_BODY_18 = ("According to Originality.ai, 53.7% of long-form LinkedIn posts in 2025 were "
            "probably AI-generated. Talk to your audience like a person.")


def _concept(**kw) -> ic.ImageConcept:
    base = dict(thesis="Expertise beats automation on LinkedIn", audience="B2B creators",
                specific_entities=("Originality.ai",), emotional_beat="caution",
                hook_phrase="Expertise wins over automation", treatment="editorial_concept",
                treatment_rationale="x", archetype="checklist",
                archetype_ranking=("checklist", "editorial_concept"))
    base.update(kw)
    return ic.ImageConcept(**base)


class TestTitleFigureShown:
    def test_title_figure_hook(self):
        assert ic.title_figure_hook("53.7%", "of LinkedIn posts miss their mark - why") == \
            "53.7% of LinkedIn posts miss their mark"
        assert ic.title_figure_hook("$30K", "we nearly lost: a story") == "$30K we nearly lost"
        assert ic.title_figure_hook("5", "of") == ""

    def test_cover_18_now_leads_with_its_figure(self):
        source = f"{_TITLE_18}\n\n{_BODY_18}"
        got = ic.show_title_figure(_concept(), _TITLE_18, source)
        assert got.hook_phrase == "53.7% of LinkedIn posts miss their mark"
        assert got.hook_options["title"] == got.hook_phrase

    def test_an_unsourced_or_already_shown_figure_is_left_alone(self):
        unsourced = f"{_TITLE_18}\n\nTalk to your audience like a person."
        assert ic.show_title_figure(_concept(), _TITLE_18, unsourced).hook_phrase == \
            "Expertise wins over automation"
        shown = _concept(hook_phrase="53.7% miss")
        assert ic.show_title_figure(shown, _TITLE_18, f"{_TITLE_18}\n\n{_BODY_18}") is shown
        drawn = _concept(archetype="stat_card", graphic={"stat": {"display": "53.7%"}})
        assert ic.show_title_figure(drawn, _TITLE_18, f"{_TITLE_18}\n\n{_BODY_18}") is drawn
        plain = _concept()
        assert ic.show_title_figure(plain, "Miss Their Mark", _BODY_18) is plain


# --- Curated posts --------------------------------------------------------------------------------

_C1 = ("Google reports a new experimental gaming platform called Playground.\n\n"
       "It suggests assets and writes scripts so a shop owner can spin up a mini-game fast. "
       "The flip side is that experimental means the service is still being refined.\n\n"
       "What trade-off feels most acceptable for your next marketing experiment?\n\n"
       "Source: Google, \"Introducing Playground\".")
_C3 = ("What if your tools could anticipate the next step?\n\n"
       "Jared Spataro reports that Microsoft is reimagining Copilot with Home, Code and "
       "Autopilot.\n\n"
       "The upside is faster turnaround on routine tasks and prototypes without new hires. The "
       "flip side is a growing reliance on one vendor's stack, with lock-in and higher costs.\n\n"
       "If you're weighing a unified assistant, what trade-off feels most pressing for you?")


class TestCuratedRotation:
    def test_a_report_opener_without_that_still_counts(self):
        assert cc.opener_form(_C1) == "reports_that"
        assert cc.opener_form(_C3) == "reports_that"  # after an opening question
        assert cc.opener_form("Plain opener.\n\nGoogle reports that x.") == ""

    def test_the_window_is_four_posts(self):
        recent = ["Plain.", "Plain again.", _C1]
        out = cc.rotate_curated_opener(_C3, recent, "Jared Spataro")
        assert "Jared Spataro has a new claim" in out or "According to Jared Spataro" in out
        assert out.startswith("What if your tools")
        assert cc.rotate_curated_opener(_C3, ["a", "b", "c", _C1], "x") == _C3
        assert cc.rotate_curated_opener(_C3, [], "x") == _C3

    def test_a_shared_closing_question_is_cut(self):
        out = cc.drop_repeated_close(_C3, [_C1])
        assert "what trade-off feels most" not in out.lower()
        assert out.rstrip().endswith("higher costs.")
        assert cc.drop_repeated_close(_C3, ["What do you think?"]) == _C3
        assert cc.drop_repeated_close(_C3, None) == _C3

    def test_cutting_never_leaves_a_stub(self):
        short = "A take.\n\nWhat trade-off feels most acceptable for you?"
        assert cc.drop_repeated_close(short, [_C1]) == short


class TestChartLabelNoun:
    _HAIKU = "Claude Haiku 5.5 costs around 75% less than Claude Haiku 4.5 for most tasks."

    def test_a_comparative_takes_the_measured_noun(self):
        assert ig.complete_context("costs", self._HAIKU, "75") == "less cost than Claude Haiku 4.5"

    def test_no_noun_means_no_line_and_a_named_one_is_kept(self):
        assert ig.comparative_with_noun("less than rivals", "it runs about ") == ""
        assert ig.comparative_with_noun("less engagement than people", "") == \
            "less engagement than people"
        assert ig.comparative_with_noun("per quarter", "") == "per quarter"
        assert ig.comparative_with_noun("", "") == ""


def test_the_concept_is_a_frozen_dataclass():
    """``show_title_figure`` returns a replaced concept, never a mutated one."""
    concept = _concept()
    got = ic.show_title_figure(concept, _TITLE_18, f"{_TITLE_18}\n\n{_BODY_18}")
    assert got is not concept and dataclasses.is_dataclass(got)
    assert concept.hook_phrase == "Expertise wins over automation"
