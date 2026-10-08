"""Showcase round 8: the numbers/timeline gate, deck copy, endings and curated drafts.

Each test names the round-8 sample it was written from (docs/content-core.md, "Showcase round 8").
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import (
    content_alignment as ca,
    content_framework as cf,
    curated_commentary as cc,
    fact_consistency as fc,
)

pytestmark = pytest.mark.unit


# --- Numbers, counts and timelines ----------------------------------------------------------------

class TestHedgedFiguresStayHedged:
    BODY = "You could cut your AI spend by 60% without even changing your models."

    def test_an_image_may_not_assert_what_the_body_only_hedges(self):
        # rhythm_3: "could cut … 60%" in the post, "AI spend cut by 60%" on the image.
        assert fc.unprovenanced_figures("AI spend cut by 60%", self.BODY, ["cut 60% of the bill"]) \
            == ["60%"]

    def test_a_hedged_surface_is_fine(self):
        assert fc.unprovenanced_figures("You could cut AI spend by 60%", self.BODY,
                                        ["cut 60% of the bill"]) == []

    def test_a_body_that_asserts_it_anywhere_vouches_for_it(self):
        body = self.BODY + " I cut my own spend by 60% in May."
        assert not fc.upgrades_hedge("60", "AI spend cut by 60%", body)

    def test_a_figure_the_body_never_states_is_no_upgrade(self):
        assert not fc.upgrades_hedge("70", "cut by 70%", self.BODY)
        assert fc.is_hedged("up to 40% faster") and not fc.is_hedged("40% faster")


class TestCountsReconcile:
    def test_two_totals_of_one_thing_conflict(self):
        # slot_143: "51 cold emails" then "Out of 63 recipients".
        issues = fc.count_conflicts("I sent 51 cold emails and got zero replies. Out of 63 "
                                    "recipients, none were hiring managers.")
        assert len(issues) == 1 and "51" in issues[0] and "63" in issues[0]

    def test_one_sentence_that_relates_them_is_fine(self):
        assert fc.count_conflicts("I sent 51 emails to 63 recipients. Out of 63 recipients, "
                                  "none replied.") == []

    def test_a_subset_is_not_a_total(self):
        assert fc.count_conflicts("I sent 51 emails. 3 emails bounced.") == []

    def test_the_report_holds_on_it(self):
        report = fc.consistency_report("I sent 51 emails. Out of 63 recipients, none replied.")
        assert not report["passes"] and report["counts"]


class TestADatedEventNeedsTimeForItsResults:
    TEXT = ("On Oct 1 2026 I helped three early-stage startups run multi-agent systems. "
            "Those teams stayed stable and delivered on schedule.")

    def test_a_week_old_story_cannot_report_results(self):
        # slot_125, generated October 8.
        issues = fc.dated_outcome_violations(self.TEXT, date(2026, 10, 8))
        assert len(issues) == 1 and "stayed" in issues[0]

    def test_an_old_enough_story_can(self):
        assert fc.dated_outcome_violations(self.TEXT, date(2026, 11, 8)) == []

    def test_a_plan_or_a_future_date_is_never_flagged(self):
        plan = "On Oct 1 2026 I started a pilot. It could cut costs by a third."
        assert fc.dated_outcome_violations(plan, date(2026, 10, 8)) == []
        assert fc.dated_outcome_violations(self.TEXT, date(2026, 9, 1)) == []

    def test_it_joins_the_timeline_half_of_the_report(self):
        report = fc.consistency_report(self.TEXT, now=date(2026, 10, 8))
        assert report["timeline"] and not report["passes"]


# --- Deck copy -------------------------------------------------------------------------------------

class TestBareRoleHeadings:
    @pytest.mark.parametrize("title", ["The Challenge", "The Solution", "The Launch Day Surprise",
                                       "The Quick Decision"])
    def test_a_bare_the_noun_heading_is_a_template_label(self, title):
        assert cf.template_heading(title) == title

    @pytest.mark.parametrize("title", ["The fix took 3 days", "The Fix Worked",
                                       "The Vendor Was Wrong", "Merge, then email the reporter"])
    def test_a_claim_is_not(self, title):
        assert cf.template_heading(title) == ""

    def test_a_bare_label_is_re_headed_from_the_body(self):
        assert cf.heading_as_claim("The Challenge", "Reporters never heard back. That cost us.") \
            == "Reporters never heard back"


class TestPlatitudes:
    @pytest.mark.parametrize("line", ["Trustworthy data is the foundation.",
                                      "Clarity drives results."])
    def test_a_platitude_is_caught(self, line):
        assert cf.platitude_line(line)

    @pytest.mark.parametrize("line", ["I deactivated all three payment links.",
                                      "Stripe refunded 4 customers that night.",
                                      "Why would anyone trust that?"])
    def test_a_particular_is_not(self, line):
        assert not cf.platitude_line(line)


POST = ("In September 2026, I launched a product and activated payment links that same day. "
        "A closer look that evening showed the advertised features were not live.\n\n"
        "I immediately deactivated all three payment links. It was necessary to keep my "
        "customers' trust. Speed can hide the details that make a launch.")


def _deck(**overrides):
    deck = {"cover": {"title": "How I Saved My Launch: 3 Steps", "content": "launch night"},
            "challenge": {"title": "The Launch Day Surprise",
                          "content": "The advertised features were not live."},
            "solution": {"title": "The Quick Decision", "content": "Clarity drives results."},
            "results": {"title": "What it cost", "content": "Three links off for a weekend."},
            "call_to_action": {"title": "Follow", "content": "More launches soon."}}
    deck.update(overrides)
    return deck


class TestFinalizeDeckRound8:
    def test_a_count_no_slide_delivers_and_a_promise_word_leave_the_cover(self):
        # slot_144: "How I Saved My Launch: 3 Steps" over three narrative slides.
        out, changes = cf.finalize_deck_claims(_deck(), POST, [])
        assert "3 Steps" not in out["cover"]["title"]
        assert "Saved" not in out["cover"]["title"]
        assert any("3 Steps" in c for c in changes)

    def test_template_headings_and_platitudes_go(self):
        out, _ = cf.finalize_deck_claims(_deck(), POST, [])
        titles = [out[k]["title"] for k in ("challenge", "solution")]
        assert not any(cf.template_heading(t) for t in titles)
        assert "Clarity drives results" not in out["solution"]["content"]
        # The emptied slide carries the post's own material.
        assert len(out["solution"]["content"].split()) >= cf.THIN_SLIDE_MIN_WORDS

    def test_a_counted_caption_backs_the_cover(self):
        deck = _deck(cover={"title": "3 Steps to a Safe Launch", "content": ""})
        caption = POST + "\n\nThe 3 steps I now take before every launch:"
        assert cf.unbacked_count_promise(deck, caption) == ""

    def test_enumerated_slides_back_the_cover(self):
        deck = _deck(challenge={"title": "Step 1: Check the page", "content": "x y z w v"})
        assert cf.unbacked_count_promise(deck, POST) == ""

    def test_a_thin_slide_in_a_list_is_dropped(self):
        deck = {"cover": {"title": "Launch night", "content": ""},
                "story_slides": [{"title": "Links went live", "content": "At 6pm the links went "
                                                                         "live for everyone."},
                                 {"title": "Then", "content": "Clarity drives results."}],
                "call_to_action": {"title": "Follow", "content": ""}}
        out, changes = cf.finalize_deck_claims(deck, POST, [])
        assert len(out["story_slides"]) == 1
        assert any("dropped the empty slide" in c for c in changes)

    def test_a_slide_never_inverts_the_bodys_route(self):
        # slot_142: "came from my own support address" vs the body's "through".
        body = "It came through my business support group address. I reported it."
        deck = {"cover": {"title": "A fake refund email", "content": ""},
                "insights": [{"title": "It looked internal",
                              "content": "It came from my own support address last April."}],
                "call_to_action": {"title": "Follow", "content": ""}}
        out, changes = cf.finalize_deck_claims(deck, body, [])
        assert "through my own support address" in out["insights"][0]["content"]
        assert any("route" in c for c in changes)

    def test_route_inversions_need_the_body_to_say_otherwise(self):
        assert cf.route_inversions("from my support address", "A note on billing.") == []
        assert cf.route_inversions("through my support address",
                                   "It came through my support group address.") == []

    def test_the_report_counts_an_unbacked_count(self):
        report = cf.deck_claims_report(_deck(), POST, [])
        assert report["promise"] and not report["passes"]


# --- Endings and CTAs ------------------------------------------------------------------------------

class TestShareAsks:
    @pytest.mark.parametrize("line", ["Know a startup founder? Forward this.", "Pass this along.",
                                      "Pass this on to them.", "Share this with your team."])
    def test_every_share_wording_is_one_type(self, line):
        assert cf.cta_type_of(line) == cf.CTA_TYPE_SHARE

    def test_a_reply_ask_is_not_a_share(self):
        assert cf.cta_type_of("Share your strategies in the comments.") == cf.CTA_TYPE_NONE

    def test_one_share_ask_in_six_posts(self):
        post = "A.\n\nB.\n\nKnow someone hiring? Forward this."
        recent = ["x"] * 4 + ["y\n\nPass this along."]
        out, reason = cf.enforce_close_caps(post, recent)
        assert out == "A.\n\nB." and "share" in reason
        assert cf.enforce_close_caps(post, ["x"] * 5 + ["y\n\nPass this along."]) == (post, "")


class TestQuestionCloses:
    def test_at_most_a_third_close_on_a_question(self):
        post = "A.\n\nB.\n\nTransparency protects trust. How do you keep it on track?"
        recent = ["a?\n\nWhat do you think?", "b", "c\n\nWould you?"]
        out, reason = cf.enforce_close_caps(post, recent)
        assert out.endswith("Transparency protects trust.") and "question" in reason

    def test_under_the_cap_it_stays(self):
        post = "A.\n\nB.\n\nHow do you keep it on track?"
        assert cf.enforce_close_caps(post, ["a\n\nWhat?", "b", "c"]) == (post, "")

    def test_a_question_only_paragraph_goes_whole(self):
        assert cf.drop_closing_question("A.\n\nB.\n\nWhat would you do?") == "A.\n\nB."

    def test_a_post_that_cannot_stand_without_it_is_kept(self):
        assert cf.drop_closing_question("A.\n\nWhat would you do?") == "A.\n\nWhat would you do?"
        assert cf.drop_closing_question("No question.") == "No question."
        assert cf.drop_closing_question("") == ""

    def test_the_rotation_offers_neither_over_its_cap(self):
        recent = [cf.CTA_TYPE_SAVE, cf.CTA_TYPE_QUESTION, cf.CTA_TYPE_NONE, cf.CTA_TYPE_QUESTION,
                  cf.CTA_TYPE_SHARE]
        pick = cf.select_cta_type(recent, sequence_index=0)
        assert pick not in (cf.CTA_TYPE_SHARE, cf.CTA_TYPE_QUESTION)


class TestKeywordAskNamesItsAsset:
    MAGNET = {"enabled": True, "keyword": "AUDIT", "message": "My Friday status-email script"}

    def test_dm_it_to_you_names_the_resource(self):
        # slot_130: "Comment AUDIT and I'll DM it to you."
        out = ca.name_cta_asset("Body.\n\nComment AUDIT and I'll DM it to you.", self.MAGNET)
        assert out.endswith("Comment AUDIT and I'll DM you my Friday status-email script.")

    def test_only_the_mechanic_line_is_touched(self):
        text = "I'll send it your way later.\n\nComment AUDIT and I'll send it your way."
        out = ca.name_cta_asset(text, self.MAGNET)
        assert out.startswith("I'll send it your way later.")
        assert out.endswith("send my Friday status-email script your way.")

    def test_a_named_ask_or_no_keyword_is_untouched(self):
        named = "Comment AUDIT and I'll DM you the checklist."
        assert ca.name_cta_asset(named, self.MAGNET) == named
        assert ca.name_cta_asset("Comment AUDIT and I'll DM it to you.", {}) \
            == "Comment AUDIT and I'll DM it to you."

    def test_the_repair_path_names_it(self):
        out = ca.ensure_lead_magnet_cta("Body text here.\n\nComment AUDIT and I'll DM it to you.",
                                        self.MAGNET, post_id=7, every_n=1)
        assert "DM you my Friday status-email script" in out


# --- Curated drafts --------------------------------------------------------------------------------

class TestCuratedSubstance:
    def test_two_sentences_are_too_thin(self):
        # curated_2.
        text = ("Google DeepMind reports that EmbeddingGemma 2 is an open, lightweight multimodal "
                "embedding model designed for broad accessibility. In my view, EmbeddingGemma 2 "
                "opens a practical path for small businesses to experiment with multimodal AI "
                "without large upfront infrastructure costs and with very little risk overall.")
        assert cc.thin_commentary_reason(text)

    def test_a_real_take_passes(self):
        text = " ".join(["The source makes one strong claim about cost and latency today."] * 7)
        assert cc.thin_commentary_reason(text) == ""


class TestCuratedOpenerAndPivot:
    RECENT = ["Google reports that Playground is live.\n\nMore.\n\nSource: Google, \"x\"."]

    def test_a_repeated_reports_that_opener_rotates(self):
        out = cc.rotate_curated_opener("Google DeepMind reports that EmbeddingGemma 2 is open.",
                                       self.RECENT, "Google DeepMind")
        assert out == "According to Google DeepMind, EmbeddingGemma 2 is open."

    def test_the_next_form_is_used_when_the_last_two_took_the_first(self):
        recent = ["According to X, y.", "Google reports that y."]
        out = cc.rotate_curated_opener("AWS reports that Haiku is live.", recent, "AWS")
        assert out == "AWS has a new claim: Haiku is live."

    def test_no_repeat_no_change(self):
        text = "Google DeepMind reports that EmbeddingGemma 2 is open."
        assert cc.rotate_curated_opener(text, ["Something else."], "x") == text
        assert cc.rotate_curated_opener("No opener here.", self.RECENT, "x") == "No opener here."
        assert cc.opener_form("New from AWS: Haiku.") == "form_2"
        assert cc.opener_form("Plain.") == ""

    def test_a_repeated_small_business_pivot_is_cut(self):
        text = "Point one.\n\nFor a small business, this shifts AI into the background."
        recent = ["Before.\n\nFrom a small-business perspective, it helps."]
        assert cc.drop_repeated_pivot(text, recent) == ("Point one.\n\nThis shifts AI into the "
                                                        "background.")
        assert cc.drop_repeated_pivot(text, ["No pivot."]) == text


class TestChartFactsParseTolerantly:
    def test_a_fenced_reply_still_parses(self):
        source = {"excerpt": "Haiku costs 75% less."}
        with patch.object(cc, "_complete", return_value='```json\n{"stat": 1}\n```'):
            assert cc.extract_chart_facts(source) == {"stat": 1}

    def test_prose_is_no_chart_and_no_warning(self):
        source = {"excerpt": "Haiku costs 75% less."}
        with patch.object(cc, "_complete", return_value="I could not find figures."), \
             patch.object(cc, "log_warning") as warn:
            assert cc.extract_chart_facts(source) == {}
        warn.assert_not_called()

    def test_a_call_failure_still_warns(self):
        with patch.object(cc, "_complete", side_effect=RuntimeError("down")), \
             patch.object(cc, "log_warning") as warn:
            assert cc.extract_chart_facts({"excerpt": "x"}) == {}
        warn.assert_called_once()


class TestSourceFaithfulRotation:
    def test_the_faithfulness_pass_applies_both_rotations(self):
        recent = ["Google reports that Playground is live.\n\nFor a small business, it helps."]
        text = ("Google DeepMind reports that the model is open.\n\nFor a small business, it "
                "means cheap tests.")
        with patch.object(cc, "_recent_curated_texts", return_value=recent), \
             patch.object(cc, "unsourced_names", return_value=[]), \
             patch.object(cc, "first_hand_claims", return_value=[]):
            out = cc._source_faithful(text, {"publisher": "Google DeepMind"}, "voice",
                                      MagicMock(), 1)
        assert out.startswith("According to Google DeepMind, the model is open.")
        assert "For a small business" not in out


class TestCloseCapsInTheContentPlan:
    SHARE = "A.\n\nB.\n\nKnow someone hiring? Forward this."

    def _ctx(self, cta_type=None):
        from cqc_lem.domain.models import PostDraftContext

        blueprint = {"format": "personal_lesson", **({"cta_type": cta_type} if cta_type else {})}
        return PostDraftContext(user_id=1, stage="awareness", post_type="thought_leadership",
                                user_profile=MagicMock(), prefs={}, profile_synthesis="v",
                                blueprint=blueprint, post_id=4, history_directive="",
                                story_directive="")

    def test_a_post_with_no_cta_type_still_answers_the_caps(self):
        from cqc_lem.app import run_content_plan as rcp

        with patch.object(rcp, "log_info") as info:
            out = rcp._enforce_cta_rotation(self._ctx(), self.SHARE, ["x\n\nPass this along."])
        assert out == "A.\n\nB." and "share ask" in info.call_args.args[0]

    def test_a_rotated_close_that_breaks_a_cap_is_cut(self):
        from cqc_lem.app import run_content_plan as rcp

        out = rcp._enforce_cta_rotation(self._ctx(cf.CTA_TYPE_SHARE), self.SHARE,
                                        ["a", "b", "c", "d\n\nPass this on to them."])
        assert out == "A.\n\nB."

    def test_a_deck_caption_answers_them_too(self):
        from cqc_lem.app import run_content_plan as rcp

        out = rcp._enforce_batch_variety(1, 4, self.SHARE, ["x\n\nShare this with your team."])
        assert out == "A.\n\nB."

    def test_the_keyword_close_is_never_cut(self):
        from cqc_lem.app import run_content_plan as rcp

        post = "A.\n\nB.\n\nComment AUDIT and I'll DM you my checklist."
        assert rcp._apply_close_caps(1, 4, post, ["x\n\nForward this."] * 3) == post
