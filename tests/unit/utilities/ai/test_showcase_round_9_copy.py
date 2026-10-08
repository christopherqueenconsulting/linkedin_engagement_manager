"""Showcase round 9, copy half: the round-8 rules that did not bind, and why (docs/content-core.md).

Each test names the showcase item it pins: slot_135's 51 vs 63 (a verb tense the count rule did
not read), slot_128's "6 tools" (a slide count no rule reconciled), slot_130's two stories, its
duplicate bullet and its unnamed asset, the "It works." tics, the save-ask cap, cover_16's
contradicting hook, cover_18's sourced 53.7%, and curated_4's phantom chart and "75% costs".
"""
from unittest.mock import patch

import pytest

from cqc_lem.app import run_content_plan as rcp, run_curated_sources as rcs
from cqc_lem.utilities.ai import (
    content_alignment as ca,
    content_framework as cf,
    curated_commentary as cc,
    fact_consistency as fc,
    image_concept as ic,
    image_graphics as ig,
)

pytestmark = pytest.mark.unit


# --- slot_135: two totals of one thing -------------------------------------------------------------

class TestCountsReadEveryTense:
    @pytest.mark.parametrize("opener", ["after sending 51 cold emails", "I send 51 cold emails",
                                        "emailing 51 prospects", "while messaging 51 contacts"])
    def test_a_present_or_progressive_verb_is_a_total(self, opener):
        text = f"Last week, {opener} — zero replies.\n\nAll 63 recipients were executives."
        issues = fc.count_conflicts(text)
        assert issues and "51" in issues[0] and "63" in issues[0]

    def test_one_sentence_relating_both_still_passes(self):
        assert fc.count_conflicts("Sending 51 emails to all 63 recipients took a day.") == []


# --- cover_18: a figure sourced by a sentence that restates its finding -----------------------------

_BODY_18 = ("53.7% of long-form LinkedIn posts in 2025 were probably AI-generated.\n\n"
            "The full story\n\n"
            "In late 2025 I saw a striking result from Originality.ai: they scanned more than 3,000 "
            "posts from influential profiles and found that over half were likely AI-generated.")


class TestRestatedSource:
    def test_a_source_two_paragraphs_on_vouches_for_the_figure(self):
        assert fc.figure_provenance("53.7", _BODY_18) == fc.PROVENANCE_SOURCE
        assert fc.figure_provenance("53.7", _BODY_18, []) == fc.PROVENANCE_SOURCE

    def test_an_unrelated_source_sentence_does_not(self):
        body = ("53.7% of our trial users churned in March.\n\n"
                "According to Gartner, AI budgets will grow next year.")
        assert fc.figure_provenance("53.7", body, []) == ""
        assert not fc.restated_by_source(["53.7% of our trial users churned in March."], body)

    def test_a_result_from_a_named_publisher_names_a_source(self):
        assert fc._names_source("A striking result from Originality.ai showed it.")


# --- slot_130: one story per post ------------------------------------------------------------------

_ANCHOR = "Using an AI agent to buy a $75K SUV for $62K out the door. Five dealer tactics."
_STITCHED = ("On Jan 16 2026 my AI agent got me a Lincoln Nautilus for $62K. It worked.\n\n"
             "A client needed trustworthy AI for a critical platform but felt swamped.\n\n"
             "Within six months the client cut disruptions by 30%.")


class TestOneStoryPerPost:
    def test_a_second_case_beside_the_anchor_is_a_finding(self):
        issues = fc.second_story_issues(_STITCHED, _ANCHOR)
        assert issues and "second story" in issues[0] and "A client needed" in issues[0]
        report = fc.consistency_report(_STITCHED, story_text=_ANCHOR)
        assert not report["passes"] and report["stories"] == issues

    @pytest.mark.parametrize("opener", ["I worked with a retail client last spring.",
                                        "One startup founder asked me the same thing."])
    def test_other_case_openers(self, opener):
        assert fc.second_story_issues(f"My story.\n\n{opener}", _ANCHOR)

    def test_the_anchors_own_protagonist_and_no_anchor_pass(self):
        anchor = "A client's invoices were 60 days late."
        assert fc.second_story_issues("A client paid late.\n\nSo I changed terms.", anchor) == []
        assert fc.second_story_issues(_STITCHED, None) == []
        assert fc.consistency_report(_STITCHED)["stories"] == []


class TestRepeatedListLines:
    def test_the_duplicate_governance_bullet_goes(self):
        text = ("Intro.\n\n- Built a custom evaluation framework and added safety guardrails so "
                "models stay compliant at every deployment stage.\n"
                "- Added safeguards that keep models compliant at every deployment step.\n"
                "- Route edge cases to a human.")
        out, dropped = cf.dedupe_repeated_lines(text)
        assert dropped == ["- Added safeguards that keep models compliant at every deployment step."]
        assert "Route edge cases" in out and out.count("compliant") == 1

    def test_parallel_items_and_prose_are_kept(self):
        text = ("- Approve the data path.\n- Test the data path.\n\n"
                "Models stay compliant at every deployment stage. Models stay compliant at every "
                "deployment stage.")
        assert cf.dedupe_repeated_lines(text) == (text, [])
        assert cf.dedupe_repeated_lines("") == ("", [])

    def test_mostly_the_same_words_is_a_repeat(self):
        text = "- Pull answers only from approved knowledge sources\n- Approved knowledge sources only answers pull"
        assert cf.dedupe_repeated_lines(text)[1]


class TestVerbalTics:
    @pytest.mark.parametrize("tic", ["It works.", "It worked.", "It matters.", "That works."])
    def test_a_standalone_tic_sentence_is_cut(self, tic):
        out, n = cf.strip_verbal_tics(f"The guardrails hold. {tic}\n\nNext paragraph.")
        assert n == 1 and tic not in out and out.startswith("The guardrails hold.")

    def test_a_tic_on_its_own_line_and_a_real_sentence(self):
        assert cf.strip_verbal_tics("Done.\nIt works.\nMore.")[0] == "Done.\nMore."
        kept = "It works best when the data is fresh."
        assert cf.strip_verbal_tics(kept) == (kept, 0)
        assert cf.strip_verbal_tics(None) == (None, 0)

    def test_the_curated_finish_strips_them_too(self):
        assert "It works." not in cc.finish_post_text("A fair point about costs. It works.")


class TestCleanupWiring:
    def test_the_deterministic_cleanup_runs_both(self):
        draft = ("Hook. It worked.\n\n- Added guardrails so models stay compliant at every "
                 "deployment stage.\n- Added safeguards so models stay compliant at every "
                 "deployment step.")
        out = rcp._deterministic_fact_cleanup(draft, user_id=1, post_id=2)
        assert "It worked." not in out and out.count("compliant") == 1


# --- The save-ask cap -------------------------------------------------------------------------------

class TestSaveCap:
    def test_one_save_ask_in_six(self):
        save = "Body.\n\nMore body.\n\nWorth saving for your next planning session."
        recent = ["Other.\n\nWorth saving for the next access request."]
        assert "save ask" in cf.close_cap_reason(save, recent)
        trimmed, reason = cf.enforce_close_caps(save, recent)
        assert reason and "Worth saving" not in trimmed

    def test_a_save_outside_the_window_is_fine(self):
        save = "Body.\n\nMore.\n\nWorth saving."
        recent = ["a"] * (cf.SAVE_CTA_WINDOW - 1) + ["x\n\nWorth saving."]
        assert cf.close_cap_reason(save, recent) == ""


# --- slot_128: every slide's counts reconcile with the caption -------------------------------------

_CAPTION_128 = ("One candidate crafted a single, comprehensive tool. In contrast, the other "
                "candidate delivered three task-focused tools.")


def _deck(content: str, title: str = "Candidate 2's verification found a real bug") -> dict:
    return {"cover": {"title": "Verification", "content": "Two take-homes."},
            "slides": [{"title": title, "content": content}],
            "cta": {"title": "Your turn", "content": "How do you verify?"}}


class TestSlideCounts:
    def test_a_slide_count_the_caption_contradicts_is_set_to_the_caption(self):
        deck = _deck("Result: 6 tools that worked.")
        assert cf.slide_count_conflicts(deck, _CAPTION_128)[0]["counts"] == [3]
        out, changes = cf.finalize_deck_claims(deck, _CAPTION_128)
        assert out["slides"][0]["content"] == "Result: 3 tools that worked."
        assert any("6 tools" in c for c in changes)

    def test_a_word_count_stays_a_word(self):
        out, _ = cf.finalize_deck_claims(_deck("Six tools shipped."), _CAPTION_128)
        assert out["slides"][0]["content"] == "Three tools shipped."

    def test_two_caption_counts_drop_the_sentence(self):
        caption = "We shipped 3 tools in May and 5 tools in June."
        out, changes = cf.finalize_deck_claims(_deck("We built 6 tools. The rest held up well."),
                                               caption)
        assert "6 tools" not in out["slides"][0]["content"]
        assert any("dropped" in c for c in changes)

    def test_units_and_agreeing_counts_are_left_alone(self):
        caption = "The test took 55 minutes and I wrote 3 tools."
        assert cf.slide_count_conflicts(_deck("It ran for 10 minutes. 3 tools held."),
                                        caption) == []
        deck = _deck("Three tools held up under load.")
        assert cf.finalize_deck_claims(deck, caption)[0]["slides"][0]["content"] == \
            "Three tools held up under load."


# --- slot_130: "Comment AUDIT and I'll DM you the resource" -----------------------------------------

class TestKeywordAskNamesItsAsset:
    _LM = {"enabled": True, "keyword": "AUDIT",
           "message": "Thanks for commenting! Here's my AI vendor-risk scorecard: https://x.y/z"}

    def test_a_sentence_shaped_message_names_its_offer(self):
        assert ca._resource_label(self._LM) == "my AI vendor-risk scorecard"

    def test_the_generic_fallback_line_is_renamed(self):
        out = ca.name_cta_asset("Body.\n\nComment AUDIT and I'll DM you the resource.", self._LM)
        assert out.endswith("Comment AUDIT and I'll DM you my AI vendor-risk scorecard.")

    @pytest.mark.parametrize("message,label", [
        ("I made a checklist that helps founders tune their ops end to end", "my checklist"),
        ("We built an audit template you can copy", "my template"),
        ("Thanks! Here's the link: https://x.y", ca.GENERIC_RESOURCE_LABEL),
    ])
    def test_a_resource_noun_or_nothing(self, message, label):
        assert ca._resource_label({"keyword": "AUDIT", "message": message}) == label


# --- cover_16: a hook that contradicts its title ----------------------------------------------------

class TestHookKeepsTheTitlesFrame:
    _TITLE = "The $30K We Nearly Squandered on AI Content"

    def test_a_done_outcome_under_a_near_miss_title_is_refused(self):
        assert ic.contradicts_title("Audit cut $30K waste", self._TITLE)
        assert "nearly happened" in ic.hook_rejection("Audit cut $30K waste", self._TITLE)

    def test_a_hedged_hook_or_a_plain_title_is_fine(self):
        assert not ic.contradicts_title("Audit nearly missed $30K", self._TITLE)
        assert not ic.contradicts_title("Audit cut $30K waste", "How our audit cut AI waste")
        assert not ic.contradicts_title("", self._TITLE)

    def test_the_panel_line_loses_the_figures_determiner(self):
        assert ig.hook_without_figures(self._TITLE) == "We Nearly Squandered on AI Content"


# --- curated_4: the phantom chart and "75% costs" ---------------------------------------------------

_HAIKU = ("Claude Haiku 5.5 costs around 75% less than Claude Haiku 4.5 for most tasks.")


class TestCuratedVisualClaims:
    def test_a_one_word_label_gives_way_to_the_words_after_the_figure(self):
        # #2316: the words after the figure now carry the noun the comparative measures.
        assert ig.complete_context("costs", _HAIKU, "75").startswith(
            "less cost than Claude Haiku 4.5")

    def test_a_chart_claim_goes_when_no_chart_drew(self):
        text = ("Aamna reports 75% less.\n\nI redrew the cost comparison chart to show the gap. "
                "The visual shows a steep drop. The takeaway is simple.")
        out = cc.drop_unrendered_chart_claims(text, chart_drawn=False)
        assert "redrew" not in out and "visual" not in out and "The takeaway is simple." in out
        assert cc.drop_unrendered_chart_claims(text, chart_drawn=True) == text
        assert cc.drop_unrendered_chart_claims(None, chart_drawn=False) == ""

    @pytest.mark.parametrize("graphic,kept", [({"stat": {}}, False),
                                              ({"comparison": {}}, True)])
    def test_the_drafter_strips_it_over_a_stat_card(self, graphic, kept):
        draft = "Teams cut handling time.\n\nI redrew the chart from the report. Worth a read."
        archetype = ig.HIGHLIGHT_CHART if kept else ig.STAT_CARD
        with patch(f"{rcs.__name__}.update_curated_source_status"), \
             patch(f"{rcs.__name__}.attach_curated_source_to_post", return_value=True), \
             patch(f"{rcs.__name__}.update_db_post_image_url"), \
             patch(f"{rcs.__name__}.track_curated_source"), \
             patch(f"{rcs.__name__}._link_preview", return_value="https://x/og.png"), \
             patch(f"{rcs.__name__}.extract_chart_facts", return_value={"thesis_stat": {}}), \
             patch(f"{rcs.__name__}.validated_chart", return_value=graphic), \
             patch(f"{rcs.__name__}.rechart_archetype", return_value=archetype), \
             patch(f"{rcs.__name__}._render_rechart", return_value="https://x/a.png"), \
             patch(f"{rcs.__name__}.generate_curated_commentary", return_value=draft):
            source = {"id": 4, "platform": "rss", "url": "https://example.com/a",
                      "author": "Jane Doe", "publisher": "Example Co", "title": "Agents",
                      "excerpt": "Teams cut handling time by 30%.", "licence": "editorial",
                      "canonical_id": None, "link_only": False}
            out = rcs.draft_curated_post(1, 10, source, "value")
        assert ("redrew the chart" in out) is kept

    def test_rechart_archetype(self):
        assert rcs.rechart_archetype({}) == ig.STAT_CARD
